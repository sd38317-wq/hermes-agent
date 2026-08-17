#!/usr/bin/env python3
"""
Scripts and hand-edited subtitles
=================================

Two ways to put text you trust into the pipeline instead of text a recognizer
guessed:

**A script you already have.** Scripted video — an AI short, a narrated
explainer, an ad — is written before it is shot, so the words are known
exactly. Recognizing them again only introduces errors. :func:`align_script`
keeps the recognizer for *timing* and takes the words from the script: the
recognized transcript and the script are aligned character by character, and
each cue's window is cut out of the script at the matching position. The cue
keeps its own start and end; only the text changes.

**Subtitles you fixed by hand.** Nobody gets every word from audio, and the
last 5% is faster to type than to coax out of a model. :func:`parse_srt` reads
an edited ``.srt`` back in so the burn can be re-run from it without paying
for transcription again — edit, re-burn, done.

Alignment is done on *normalized* text (spacing and punctuation dropped),
because that is exactly what differs between what a recognizer writes and what
a writer wrote. Positions map back to the original strings, so the script's
own spacing and punctuation are what land in the subtitle.
"""

from __future__ import annotations

import difflib
import logging
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

# A cue is only re-texted from the script when the recognizer's version is
# recognizably the same passage. Below this, the script is assumed not to
# cover that cue (an ad-lib, a scene with no script, the wrong file).
MIN_ALIGNMENT_RATIO = 0.35

_STRIP_RE = re.compile(r"[\s.,!?…·~\-\"'“”‘’()\[\]]+")
_SRT_TIME_RE = re.compile(
    r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})\s*-->\s*(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})"
)


def _normalize(text: str) -> Tuple[str, List[int]]:
    """Return comparable text plus, for each kept character, its source index."""
    kept: List[str] = []
    positions: List[int] = []
    for index, char in enumerate(text or ""):
        if not _STRIP_RE.match(char):
            kept.append(char)
            positions.append(index)
    return "".join(kept), positions


# ---------------------------------------------------------------------------
# Script alignment
# ---------------------------------------------------------------------------


def align_script(
    cue_texts: Sequence[str],
    script: str,
    *,
    min_ratio: float = MIN_ALIGNMENT_RATIO,
) -> Tuple[List[str], List[str], int]:
    """Replace recognized cue text with the matching passage of *script*.

    Returns ``(texts, warnings, replaced_count)``. Cues the script does not
    appear to cover keep their recognized text, so a partial script — an intro
    written out but the rest ad-libbed — still helps where it applies instead
    of corrupting the rest.
    """
    originals = [text or "" for text in cue_texts]
    script = (script or "").strip()
    if not script or not any(text.strip() for text in originals):
        return originals, [], 0

    # Concatenate the cues, remembering where each one starts in the joined
    # normalized string, so cue boundaries can be found again after alignment.
    joined_parts: List[str] = []
    bounds: List[Tuple[int, int]] = []
    cursor = 0
    for text in originals:
        normalized, _ = _normalize(text)
        joined_parts.append(normalized)
        bounds.append((cursor, cursor + len(normalized)))
        cursor += len(normalized)
    recognized = "".join(joined_parts)

    script_normalized, script_positions = _normalize(script)
    if not recognized or not script_normalized:
        return originals, ["nothing to align — the script or the transcript is empty"], 0

    matcher = difflib.SequenceMatcher(None, recognized, script_normalized, autojunk=False)
    matching = [block for block in matcher.get_matching_blocks() if block.size]

    # Judge the match by how much of what was HEARD the script accounts for,
    # not by how similar the two strings are overall. A script that covers the
    # audio and then keeps going — a full episode script against one clip, a
    # draft with unshot scenes — is the normal case and must still align; a
    # symmetric ratio would score it as low as a completely wrong script.
    covered = sum(block.size for block in matching)
    coverage = covered / len(recognized)
    if coverage < min_ratio:
        return originals, [
            f"the script does not match the audio (only {coverage:.0%} of what was "
            "heard appears in it) — keeping the recognized text; check that it is "
            "the right script"
        ], 0

    # Map every recognized position to a script position using the matching
    # blocks; positions inside a gap fall to the start of the next block.
    mapping: Dict[int, int] = {}
    for block in matcher.get_matching_blocks():
        for offset in range(block.size):
            mapping[block.a + offset] = block.b + offset

    def _script_position(recognized_index: int, *, default: int) -> int:
        if recognized_index in mapping:
            return mapping[recognized_index]
        for probe in range(recognized_index, len(recognized)):
            if probe in mapping:
                return mapping[probe]
        return default

    last_matched = (matching[-1].b + matching[-1].size) if matching else 0

    # Cut points, not independent slices. Each cue runs from the previous
    # boundary to its own, so unmatched script — the very words the recognizer
    # got wrong, which by definition do not map to anything — always lands in
    # a cue instead of falling into a gap between two of them. That is the
    # whole point: "호미를", misheard as "고민을", matches nothing and would
    # otherwise be dropped from the subtitle it belongs to.
    #
    # The script's tail goes to the last cue, but only as far as the last
    # matched position plus a small allowance: a script that runs well past
    # the audio (a draft with unshot scenes, the wrong take) must not dump
    # every remaining line into the final subtitle.
    spoken = [index for index, (start, end) in enumerate(bounds) if end > start]
    tail_allowance = max(10, int(len(recognized) * 0.1))
    final_end = min(len(script_normalized), last_matched + tail_allowance)

    cut_points: Dict[int, Tuple[int, int]] = {}
    previous_end = 0
    for position, index in enumerate(spoken):
        _start, end = bounds[index]
        if position == len(spoken) - 1:
            script_end = max(previous_end, final_end)
        else:
            script_end = max(previous_end, _script_position(end, default=previous_end))
            script_end = min(script_end, len(script_normalized))
        cut_points[index] = (previous_end, script_end)
        previous_end = script_end

    warnings: List[str] = []
    result = list(originals)
    replaced = 0
    for index, (start, end) in enumerate(bounds):
        if start == end:
            continue
        script_start, script_end = cut_points[index]
        if script_end <= script_start:
            continue

        # Slice the ORIGINAL script so its spacing and punctuation survive.
        # The slice runs up to where the NEXT kept character starts, not to the
        # end of this one — otherwise the sentence-final "?" or ".", which
        # normalization dropped, is left behind on the wrong side of the cut.
        text_start = script_positions[script_start]
        text_end = (
            script_positions[script_end]
            if script_end < len(script_positions)
            else len(script)
        )
        # The script's own line breaks are layout, not content: a cue that
        # spans two script lines is still one line of speech.
        passage = re.sub(r"\s+", " ", script[text_start:text_end]).strip()
        if passage:
            result[index] = passage
            replaced += 1

    leftover = len(script_normalized) - max(previous_end, last_matched)
    if leftover > max(10, len(script_normalized) * 0.15):
        warnings.append(
            f"about {leftover} characters of the script were not matched to any cue — "
            "the audio may be shorter than the script"
        )
    return result, warnings, replaced


# ---------------------------------------------------------------------------
# Reading hand-edited subtitles back in
# ---------------------------------------------------------------------------


def parse_srt(text: str) -> Tuple[List[Tuple[float, float, str]], List[str]]:
    """Parse SubRip text into ``[(start, end, text)]``.

    Tolerant on purpose — this reads files people have edited by hand, so a
    missing index line, CRLF endings, a stray BOM or a blank cue must not throw
    the whole file away. WebVTT's ``.`` decimal separator is accepted too.
    """
    warnings: List[str] = []
    cues: List[Tuple[float, float, str]] = []
    if not text:
        return cues, ["the subtitle file is empty"]

    blocks = re.split(r"\r?\n\s*\r?\n", text.lstrip("﻿").replace("WEBVTT", "", 1).strip())
    for number, block in enumerate(blocks, start=1):
        lines = [line for line in block.splitlines() if line.strip()]
        if not lines:
            continue
        timing_index = next(
            (i for i, line in enumerate(lines) if _SRT_TIME_RE.search(line)), None
        )
        if timing_index is None:
            warnings.append(f"skipped block {number}: no timing line")
            continue
        match = _SRT_TIME_RE.search(lines[timing_index])
        start = _to_seconds(*match.groups()[:4])
        end = _to_seconds(*match.groups()[4:])
        body = " ".join(line.strip() for line in lines[timing_index + 1:]).strip()
        if not body:
            continue
        if end <= start:
            warnings.append(f"skipped block {number}: end is not after start")
            continue
        cues.append((start, end, body))

    if not cues and not warnings:
        warnings.append("no cues found in the subtitle file")
    return cues, warnings


def _to_seconds(hours: str, minutes: str, seconds: str, millis: str) -> float:
    return (
        int(hours) * 3600
        + int(minutes) * 60
        + int(seconds)
        + int(millis.ljust(3, "0")) / 1000.0
    )


def load_subtitle_cues(path: str) -> Tuple[List[Any], List[str]]:
    """Read an edited ``.srt``/``.vtt`` into :class:`~tools.video_pipeline.Cue`s."""
    from pathlib import Path

    from tools.video_pipeline import Cue, VideoPipelineError

    source = Path(path).expanduser()
    if not source.is_file():
        raise VideoPipelineError(f"subtitle file not found: {source}")
    try:
        text = source.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        # Subtitle files edited on Windows are often cp949 in Korean locales.
        text = source.read_text(encoding="cp949", errors="replace")
    except OSError as exc:
        raise VideoPipelineError(f"could not read the subtitle file: {exc}") from exc

    parsed, warnings = parse_srt(text)
    if not parsed:
        raise VideoPipelineError(
            f"no usable cues in {source.name}: {'; '.join(warnings) or 'unknown format'}"
        )
    return [Cue(start=start, end=end, text=body) for start, end, body in parsed], warnings
