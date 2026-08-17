#!/usr/bin/env python3
"""
Caption cleanup — fixing what the recognizer got wrong
=====================================================

Speech recognizers hand back text that is *heard* correctly but *written*
badly: Korean models routinely emit whole utterances with no spacing at all
("고민을내려놓고칼을들어"), every model mishears words that a reader would
disambiguate from context ("호미를" → "고민을"), and punctuation lands
wherever the acoustic model felt a pause. Burned into a video, all three read
as sloppiness.

This pass sends the cues to the ``caption_cleanup`` auxiliary model and asks
for the same cues back, corrected. The hard requirement is that it may only
*rewrite* — never restructure — because the cue timings are already correct
and the burn depends on them:

* the reply is matched back cue by cue on an explicit index; anything missing
  keeps its original text,
* a correction that drifts too far from what was heard is rejected as a
  hallucination and the original is kept,
* a failed or unconfigured model leaves the captions exactly as they were.

Those three rules are why this is worth doing at all. An unvalidated LLM pass
over subtitles will occasionally invent a sentence, and a fabricated subtitle
burned into someone's video is worse than a missing space.
"""

from __future__ import annotations

import difflib
import json
import logging
import re
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

CLEANUP_TASK = "caption_cleanup"

# Cues per request. Enough context for the model to fix a word from the
# surrounding sentence, small enough that one bad batch cannot cost the whole
# transcript.
CLEANUP_BATCH_SIZE = 40
# How far a corrected cue may drift, measured in characters changed (spacing
# and punctuation excluded, since fixing those is the point).
#
# A single ratio cannot do this job: swapping one misheard word out of three
# characters — "고민을" → "호미를", the real case this pass exists for — is a
# 0.33 ratio, while the same fix inside a longer sentence is 0.73. So the
# budget is absolute, with a floor that keeps short cues correctable and a
# ceiling that stops a long cue from being quietly rewritten wholesale.
EDIT_BUDGET_FLOOR = 3
EDIT_BUDGET_RATIO = 0.35
EDIT_BUDGET_CEILING = 8
# A correction may not balloon: a model that starts explaining rather than
# correcting shows up here first.
MAX_LENGTH_RATIO = 1.8

_STRIP_RE = re.compile(r"[\s.,!?…·~\-\"'“”‘’]+")

CLEANUP_PROMPT = """You are correcting subtitle text produced by a speech recognizer.

For each cue you are given, return the same line with only these fixes:
- Insert correct word spacing (recognizers often return text with none).
- Fix words the recognizer misheard, using the surrounding cues as context.
- Fix obvious spelling and punctuation.

Hard rules:
- Return EXACTLY the same cues, with the same "i" values, in the same order.
- Never merge, split, reorder, add or drop a cue.
- Never translate. Keep the original language.
- Never add words that were not spoken, and never summarize or explain.
- If a cue is already correct, return it unchanged.

Respond with JSON only: {"cues": [{"i": 1, "t": "corrected text"}, ...]}"""


def _normalized(text: str) -> str:
    """Text with spacing and punctuation removed, for comparison only."""
    return _STRIP_RE.sub("", text or "")


def similarity(original: str, corrected: str) -> float:
    """How much of the recognized text survived the correction (0-1).

    Compared with spacing and punctuation stripped, since those are exactly
    what the pass is meant to change.
    """
    left, right = _normalized(original), _normalized(corrected)
    if not left and not right:
        return 1.0
    if not left or not right:
        return 0.0
    return difflib.SequenceMatcher(None, left, right).ratio()


def edit_budget(original: str) -> float:
    """How many characters of *original* a correction may change."""
    length = len(_normalized(original))
    return max(EDIT_BUDGET_FLOOR, min(length * EDIT_BUDGET_RATIO, EDIT_BUDGET_CEILING))


def changed_characters(original: str, corrected: str) -> float:
    """Characters of *original* that the correction did not preserve."""
    length = len(_normalized(original))
    return length * (1.0 - similarity(original, corrected))


def accept_correction(original: str, corrected: Any) -> Tuple[bool, str]:
    """Decide whether one corrected cue may replace the original.

    Returns ``(accepted, text)`` — the text is the original when rejected, so
    callers can use the result unconditionally.
    """
    if not isinstance(corrected, str):
        return False, original
    corrected = corrected.strip()
    if not corrected:
        return False, original
    if corrected == original:
        return False, original
    if len(_normalized(corrected)) > max(8, len(_normalized(original)) * MAX_LENGTH_RATIO):
        logger.debug("caption cleanup rejected (too long): %r -> %r", original, corrected)
        return False, original
    if changed_characters(original, corrected) > edit_budget(original):
        logger.debug("caption cleanup rejected (drifted): %r -> %r", original, corrected)
        return False, original
    return True, corrected


def parse_cleanup_response(content: str) -> Dict[int, str]:
    """Read ``{"cues": [{"i": .., "t": ..}]}`` into ``{index: text}``.

    Tolerates a fenced block or a bare array, the same way the title parser
    does — a small model dropping out of JSON mode should not throw away the
    corrections it did produce.
    """
    text = (content or "").strip()
    if not text:
        return {}
    fenced = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.DOTALL)
    if fenced:
        text = fenced.group(1).strip()
    try:
        payload = json.loads(text)
    except ValueError:
        return {}

    entries: Any = payload
    if isinstance(payload, dict):
        for key in ("cues", "results", "items"):
            if isinstance(payload.get(key), list):
                entries = payload[key]
                break
        else:
            return {}
    if not isinstance(entries, list):
        return {}

    corrections: Dict[int, str] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        index, value = entry.get("i"), entry.get("t")
        if value is None:
            value = entry.get("text")
        try:
            index = int(index)
        except (TypeError, ValueError):
            continue
        if isinstance(value, str):
            corrections[index] = value
    return corrections


def _batches(count: int, size: int = CLEANUP_BATCH_SIZE):
    for start in range(0, count, size):
        yield start, min(count, start + size)


def clean_cue_texts(
    texts: Sequence[str],
    *,
    language: Optional[str] = None,
    llm: Optional[Callable[..., Any]] = None,
    timeout: Optional[float] = None,
) -> Tuple[List[str], List[str], int]:
    """Correct recognizer output cue by cue.

    Returns ``(texts, warnings, changed_count)``. Every failure mode — no
    model configured, a request error, an unparseable reply, a cue the model
    dropped, a correction that drifted — leaves that cue's original text in
    place, so the caller can always use the result.
    """
    originals = [text or "" for text in texts]
    if not any(text.strip() for text in originals):
        return originals, [], 0

    if llm is None:
        try:
            from agent.auxiliary_client import call_llm as llm  # type: ignore
        except Exception as exc:  # pragma: no cover - defensive
            return originals, [f"caption cleanup unavailable: {exc}"], 0

    result = list(originals)
    warnings: List[str] = []
    changed = 0

    for start, end in _batches(len(originals)):
        batch = [
            {"i": index + 1, "t": originals[index]}
            for index in range(start, end)
            if originals[index].strip()
        ]
        if not batch:
            continue

        instruction = "Correct these subtitle cues."
        if language:
            instruction += f" The language is {language}."
        messages = [
            {"role": "system", "content": CLEANUP_PROMPT},
            {
                "role": "user",
                "content": f"{instruction}\n\n{json.dumps({'cues': batch}, ensure_ascii=False)}",
            },
        ]

        try:
            response = llm(
                task=CLEANUP_TASK,
                messages=messages,
                temperature=0.0,
                timeout=timeout,
                extra_body={"response_format": {"type": "json_object"}},
            )
            content = response.choices[0].message.content or ""
        except Exception as exc:
            logger.warning("caption cleanup failed: %s", exc)
            logger.debug("caption cleanup traceback", exc_info=True)
            warnings.append(f"caption cleanup failed: {exc}")
            continue

        corrections = parse_cleanup_response(content)
        if not corrections:
            warnings.append("caption cleanup returned nothing usable for one batch")
            continue

        rejected = 0
        for index in range(start, end):
            proposed = corrections.get(index + 1)
            if proposed is None:
                continue
            accepted, text = accept_correction(originals[index], proposed)
            if accepted:
                result[index] = text
                changed += 1
            elif text != proposed:
                rejected += 1
        if rejected:
            warnings.append(
                f"kept the recognized text for {rejected} cue(s) the cleanup model rewrote too heavily"
            )

    return result, warnings, changed
