"""Tests for script alignment and hand-edited subtitles (``tools/video_script.py``).

Two ways to put trusted text into the pipeline: a script the author already
has, and an ``.srt`` they fixed by hand. Both must keep the timings that came
from the audio — the text is the only thing allowed to change.
"""

from __future__ import annotations

import pytest

from tools.video_pipeline import VideoPipelineError
from tools.video_script import align_script, load_subtitle_cues, parse_srt

# What the recognizer produced for a real clip, and the script behind it.
RECOGNIZED = [
    "굶어죽을거야?",
    "아니면. 나와함께싸울거야?",
    "184년이썩어빠진나라에비극이.",
    "고민을내려놓고칼을들어.",
]
SCRIPT = (
    "굶어 죽을 거야? 아니면 나와 함께 싸울 거야?\n"
    "184년, 이 썩어빠진 나라에 비극이\n"
    "호미를 내려놓고 칼을 들어."
)


# ── script alignment ────────────────────────────────────────────────────────


def test_the_script_replaces_the_recognized_words():
    texts, warnings, replaced = align_script(RECOGNIZED, SCRIPT)
    assert replaced == len(RECOGNIZED)
    assert warnings == []
    joined = " ".join(texts)
    assert "호미를" in joined          # the word the recognizer misheard
    assert "고민을" not in joined
    assert "굶어 죽을" in joined       # and the author's spacing


def test_alignment_keeps_the_cue_count_and_order():
    texts, _warnings, _replaced = align_script(RECOGNIZED, SCRIPT)
    assert len(texts) == len(RECOGNIZED)
    assert texts[0].startswith("굶어")
    assert texts[-1].endswith("들어.")


def test_sentence_final_punctuation_is_not_left_behind():
    """Normalization drops '?' for comparison — the slice must still carry it."""
    texts, _warnings, _replaced = align_script(["굶어죽을거야"], "굶어 죽을 거야?")
    assert texts[0].endswith("?")


def test_script_line_breaks_do_not_leak_into_a_cue():
    """A cue spanning two script lines is still one line of speech."""
    texts, _warnings, _replaced = align_script(
        ["지금시작된다더이상참지마"], "지금 시작된다.\n더 이상 참지 마."
    )
    assert "\n" not in texts[0]
    assert "시작된다. 더 이상" in texts[0]


def test_the_wrong_script_is_refused_rather_than_forced_on():
    texts, warnings, replaced = align_script(
        RECOGNIZED, "오늘은 파이썬 데코레이터에 대해 알아보겠습니다. 먼저 함수부터 살펴보죠."
    )
    assert texts == RECOGNIZED
    assert replaced == 0
    assert any("does not match the audio" in w for w in warnings)


def test_a_partial_script_still_helps_where_it_applies():
    """An intro written out and the rest ad-libbed must not corrupt the rest."""
    texts, _warnings, replaced = align_script(
        RECOGNIZED + ["그리고애드리브로한마디더"], SCRIPT
    )
    assert replaced >= len(RECOGNIZED)
    assert "호미를" in " ".join(texts)


def test_a_script_longer_than_the_audio_still_aligns_and_says_so():
    """A full episode script against one clip is normal, not a wrong script."""
    texts, warnings, replaced = align_script(
        ["굶어죽을거야"],
        SCRIPT + " 그리고 아직 촬영하지 않은 장면이 여기에 아주 길게 이어집니다 정말로 길게",
    )
    assert replaced == 1
    assert texts[0].startswith("굶어 죽을 거야")
    assert any("not matched to any cue" in w for w in warnings)


def test_empty_inputs_are_no_ops():
    assert align_script([], SCRIPT) == ([], [], 0)
    assert align_script(RECOGNIZED, "") == (RECOGNIZED, [], 0)
    assert align_script(RECOGNIZED, "   ") == (RECOGNIZED, [], 0)


# ── reading edited subtitles ────────────────────────────────────────────────


SRT = """1
00:00:00,000 --> 00:00:01,977
굶어 죽을 거야?

2
00:00:02,365 --> 00:00:05,108
아니면 나와 함께
싸울 거야?
"""


def test_srt_round_trips_timings_and_text():
    cues, warnings = parse_srt(SRT)
    assert warnings == []
    assert len(cues) == 2
    assert cues[0][0] == 0.0
    assert cues[0][1] == pytest.approx(1.977)
    assert cues[0][2] == "굶어 죽을 거야?"
    assert cues[1][0] == 2.365
    # A two-line cue is one cue.
    assert cues[1][2] == "아니면 나와 함께 싸울 거야?"


def test_webvtt_style_timings_are_accepted():
    vtt = "WEBVTT\n\n00:00:01.500 --> 00:00:03.000\n안녕하세요\n"
    cues, _warnings = parse_srt(vtt)
    assert [(round(s, 3), round(e, 3), t) for s, e, t in cues] == [(1.5, 3.0, "안녕하세요")]


def test_crlf_and_a_bom_survive():
    cues, _warnings = parse_srt("﻿1\r\n00:00:00,000 --> 00:00:02,000\r\n한 줄\r\n")
    assert [(round(s, 3), round(e, 3), t) for s, e, t in cues] == [(0.0, 2.0, "한 줄")]


def test_a_missing_index_line_is_tolerated():
    """Hand-edited files lose their numbering constantly."""
    cues, _warnings = parse_srt("00:00:00,000 --> 00:00:02,000\n번호 없이\n")
    assert [(round(s, 3), round(e, 3), t) for s, e, t in cues] == [(0.0, 2.0, "번호 없이")]


def test_a_broken_block_is_skipped_and_reported_without_losing_the_rest():
    text = SRT + "\n3\n타임코드가 없는 블록\n\n4\n00:00:06,000 --> 00:00:07,000\n마지막\n"
    cues, warnings = parse_srt(text)
    assert [cue[2] for cue in cues][-1] == "마지막"
    assert any("no timing line" in w for w in warnings)


def test_a_backwards_cue_is_rejected():
    cues, warnings = parse_srt("1\n00:00:05,000 --> 00:00:02,000\n거꾸로\n")
    assert cues == []
    assert any("end is not after start" in w for w in warnings)


def test_an_empty_file_reports_rather_than_returning_nothing_silently():
    assert parse_srt("")[1]
    assert parse_srt("아무 자막도 아닌 텍스트")[1]


def test_loading_a_file_produces_cues(tmp_path):
    path = tmp_path / "edited.srt"
    path.write_text(SRT, encoding="utf-8")
    cues, _warnings = load_subtitle_cues(str(path))
    assert [cue.text for cue in cues] == ["굶어 죽을 거야?", "아니면 나와 함께 싸울 거야?"]
    assert cues[0].start == 0.0


def test_loading_a_missing_file_fails_loudly(tmp_path):
    with pytest.raises(VideoPipelineError, match="not found"):
        load_subtitle_cues(str(tmp_path / "absent.srt"))


def test_loading_a_file_with_no_cues_fails_loudly(tmp_path):
    path = tmp_path / "notes.srt"
    path.write_text("자막이 아니라 그냥 메모입니다", encoding="utf-8")
    with pytest.raises(VideoPipelineError, match="no usable cues"):
        load_subtitle_cues(str(path))
