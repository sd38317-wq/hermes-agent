"""Tests for subtitle cleanup (``tools/video_caption_cleanup.py``).

The pass exists to fix recognizer spacing and mishearings, but an unvalidated
LLM rewrite of someone's subtitles is worse than the raw text — a fabricated
line gets burned into the video. Most of these tests are therefore about what
the pass must *refuse* to do.
"""

from __future__ import annotations

import json

import pytest

from tools.video_caption_cleanup import (
    CLEANUP_BATCH_SIZE,
    accept_correction,
    changed_characters,
    clean_cue_texts,
    edit_budget,
    parse_cleanup_response,
    similarity,
)

# What a Korean recognizer actually returns, and its correction.
RAW = "고민을내려놓고칼을들어"
FIXED = "호미를 내려놓고 칼을 들어"


class _Response:
    def __init__(self, content: str):
        self.choices = [
            type("C", (), {"message": type("M", (), {"content": content})()})()
        ]


def _llm_returning(mapping):
    """A stand-in model that returns *mapping* (index -> text) as JSON."""
    calls = []

    def _llm(**kwargs):
        calls.append(kwargs)
        payload = {"cues": [{"i": i, "t": t} for i, t in mapping.items()]}
        return _Response(json.dumps(payload, ensure_ascii=False))

    _llm.calls = calls
    return _llm


# ── how much drift is allowed ───────────────────────────────────────────────


def test_spacing_only_changes_count_as_no_drift():
    """Adding spaces is the whole point, so it must not spend the budget."""
    assert similarity("고민을내려놓고칼을들어", "고민을 내려놓고 칼을 들어") == 1.0
    assert changed_characters("고민을내려놓고칼을들어", "고민을 내려놓고 칼을 들어") == 0


def test_short_cues_keep_a_usable_budget():
    """A single misheard word in a three-character cue is the whole cue — a
    plain similarity threshold would refuse to ever fix it."""
    assert edit_budget("고민을") >= 2
    assert accept_correction("고민을", "호미를")[0]


def test_long_cues_cannot_be_rewritten_wholesale():
    long_cue = "그러니까 우리가 지금 여기서 해야 할 일은 아주 분명하다고 생각합니다"
    assert edit_budget(long_cue) <= 8
    assert not accept_correction(long_cue, "완전히 다른 이야기를 여기에 새로 적어 넣습니다 전혀 무관하게")[0]


def test_a_rewritten_sentence_exceeds_the_budget():
    assert changed_characters(RAW, "농기구를 버리고 무기를 손에 쥐어라") > edit_budget(RAW)


# ── accepting or rejecting one correction ───────────────────────────────────


def test_a_spacing_fix_is_accepted():
    accepted, text = accept_correction("굶어죽을거야?", "굶어 죽을 거야?")
    assert accepted and text == "굶어 죽을 거야?"


def test_a_misheard_word_fixed_from_context_is_accepted():
    accepted, text = accept_correction(RAW, FIXED)
    assert accepted and text == FIXED


def test_a_hallucinated_rewrite_is_rejected():
    """The failure that matters: an invented line burned into the video."""
    accepted, text = accept_correction(RAW, "그녀는 조용히 미소지으며 돌아섰다")
    assert not accepted and text == RAW


def test_an_explanation_instead_of_a_correction_is_rejected():
    accepted, text = accept_correction(
        RAW, "이 문장은 '호미를 내려놓고 칼을 들어'로 고치는 것이 맞습니다. 이유는 문맥상"
    )
    assert not accepted and text == RAW


def test_an_empty_or_non_string_correction_is_rejected():
    assert accept_correction(RAW, "") == (False, RAW)
    assert accept_correction(RAW, "   ") == (False, RAW)
    assert accept_correction(RAW, None) == (False, RAW)
    assert accept_correction(RAW, 42) == (False, RAW)


def test_an_unchanged_cue_is_not_counted_as_a_correction():
    assert accept_correction(RAW, RAW) == (False, RAW)


def test_a_translation_is_rejected_as_drift():
    accepted, _ = accept_correction("굶어죽을거야?", "Are you going to starve to death?")
    assert not accepted


# ── parsing ─────────────────────────────────────────────────────────────────


def test_parse_reads_the_requested_shape():
    assert parse_cleanup_response('{"cues": [{"i": 1, "t": "가"}, {"i": 2, "t": "나"}]}') == {
        1: "가", 2: "나",
    }


def test_parse_accepts_a_fenced_block_and_a_bare_array():
    assert parse_cleanup_response('```json\n{"cues": [{"i": 3, "t": "다"}]}\n```') == {3: "다"}
    assert parse_cleanup_response('[{"i": 4, "t": "라"}]') == {4: "라"}


def test_parse_accepts_text_as_an_alias_for_t():
    assert parse_cleanup_response('{"cues": [{"i": 1, "text": "가"}]}') == {1: "가"}


def test_parse_skips_malformed_entries_without_losing_the_rest():
    payload = '{"cues": [{"i": "x", "t": "버림"}, {"t": "인덱스없음"}, {"i": 2, "t": "유지"}]}'
    assert parse_cleanup_response(payload) == {2: "유지"}


def test_parse_returns_nothing_for_prose_or_junk():
    assert parse_cleanup_response("여기 고친 자막입니다!") == {}
    assert parse_cleanup_response("") == {}


# ── the pass as a whole ─────────────────────────────────────────────────────


def test_corrections_are_applied_by_index():
    texts = ["굶어죽을거야?", RAW]
    llm = _llm_returning({1: "굶어 죽을 거야?", 2: FIXED})
    cleaned, warnings, changed = clean_cue_texts(texts, llm=llm)
    assert cleaned == ["굶어 죽을 거야?", FIXED]
    assert changed == 2
    assert warnings == []


def test_a_cue_the_model_dropped_keeps_its_original_text():
    texts = ["굶어죽을거야?", RAW]
    cleaned, _warnings, changed = clean_cue_texts(texts, llm=_llm_returning({1: "굶어 죽을 거야?"}))
    assert cleaned == ["굶어 죽을 거야?", RAW]
    assert changed == 1


def test_a_hallucinated_cue_is_reported_and_the_original_kept():
    cleaned, warnings, changed = clean_cue_texts(
        [RAW], llm=_llm_returning({1: "완전히 다른 문장을 지어냈습니다 여기"})
    )
    assert cleaned == [RAW]
    assert changed == 0
    assert any("rewrote too heavily" in w for w in warnings)


def test_a_failing_model_leaves_every_cue_untouched():
    def _raising(**_kwargs):
        raise RuntimeError("no auxiliary model configured")

    texts = ["굶어죽을거야?", RAW]
    cleaned, warnings, changed = clean_cue_texts(texts, llm=_raising)
    assert cleaned == texts
    assert changed == 0
    assert any("cleanup failed" in w for w in warnings)


def test_an_unparseable_reply_leaves_every_cue_untouched():
    cleaned, warnings, changed = clean_cue_texts(
        [RAW], llm=lambda **_kwargs: _Response("고쳤습니다!")
    )
    assert cleaned == [RAW]
    assert changed == 0
    assert warnings


def test_long_transcripts_are_sent_in_batches():
    """One bad batch must not be able to cost the whole transcript."""
    texts = [f"큐{index}" for index in range(CLEANUP_BATCH_SIZE * 2 + 5)]
    llm = _llm_returning({})
    clean_cue_texts(texts, llm=llm)
    assert len(llm.calls) == 3


def test_blank_input_skips_the_model_entirely():
    llm = _llm_returning({1: "무언가"})
    cleaned, warnings, changed = clean_cue_texts(["", "  "], llm=llm)
    assert cleaned == ["", "  "]
    assert (warnings, changed, llm.calls) == ([], 0, [])


def test_the_language_hint_reaches_the_model():
    llm = _llm_returning({1: "굶어 죽을 거야?"})
    clean_cue_texts(["굶어죽을거야?"], language="Korean", llm=llm)
    prompt = llm.calls[0]["messages"][1]["content"]
    assert "Korean" in prompt


def test_the_pass_uses_its_own_auxiliary_task():
    llm = _llm_returning({1: "가"})
    clean_cue_texts(["가나"], llm=llm)
    assert llm.calls[0]["task"] == "caption_cleanup"
