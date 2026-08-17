"""Tests for caption styling and burn-in (``tools/video_captions.py``).

The layout and colour maths are pure functions and always run; the burn-in
itself needs a real ffmpeg with libass and is skipped without one.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from tools.video_captions import (
    DEFAULT_PRESET,
    MAX_FONT_SCALE,
    MIN_FONT_SCALE,
    PRESETS,
    CaptionStyle,
    FontChoice,
    build_burn_command,
    chars_per_line,
    chunk_caption_blocks,
    escape_filter_path,
    format_ass,
    format_ass_timestamp,
    hex_to_ass_color,
    resolve_font,
    split_cue_duration,
    style_from_options,
    title_overlay_from_options,
    wrap_lines,
)
from tools.video_pipeline import Cue, find_ffmpeg, find_ffprobe, run_pipeline

HAS_FFMPEG = bool(find_ffmpeg() and find_ffprobe())
requires_ffmpeg = pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg/ffprobe not installed")

KOREAN = "모두가 끝났다고 말했다"


# ── colour ──────────────────────────────────────────────────────────────────


def test_hex_colour_is_byte_reversed_for_ass():
    """ASS stores BGR, so a red hex must not come out as blue on screen."""
    assert hex_to_ass_color("#FF0000") == "&H000000FF"
    assert hex_to_ass_color("#0000FF") == "&H00FF0000"
    assert hex_to_ass_color("#FFFFFF") == "&H00FFFFFF"


def test_hex_colour_accepts_shorthand_and_missing_hash():
    assert hex_to_ass_color("fff") == hex_to_ass_color("#FFFFFF")
    assert hex_to_ass_color("FFE000") == hex_to_ass_color("#FFE000")


def test_invalid_colour_falls_back_to_white_instead_of_raising():
    assert hex_to_ass_color("not a colour") == "&H00FFFFFF"


def test_alpha_is_inverted_because_ass_stores_transparency():
    """A colour picker's 100% opacity is ASS' 00 transparency byte."""
    assert hex_to_ass_color("#000000", alpha=0.0).startswith("&H00")
    assert hex_to_ass_color("#000000", alpha=1.0).startswith("&HFF")


# ── line layout ─────────────────────────────────────────────────────────────


def test_wrap_breaks_on_spaces_within_the_limit():
    lines = wrap_lines("one two three four five", 9)
    assert all(len(line) <= 9 for line in lines)
    assert " ".join(lines) == "one two three four five"


def test_wrap_hard_splits_a_token_longer_than_the_line():
    lines = wrap_lines("https://example.com/an/extremely/long/path", 10)
    assert all(len(line) <= 10 for line in lines)


def test_wrap_keeps_korean_words_intact():
    lines = wrap_lines(KOREAN, 8)
    assert all(len(line) <= 8 for line in lines)
    assert "".join(lines).replace(" ", "") == KOREAN.replace(" ", "")


def test_overflowing_text_becomes_extra_blocks_not_a_long_line():
    """Regression: folding overflow into the last line pushed text off-frame."""
    text = "그런데 그 순간 빛이 내려왔다 그리고 그녀가 걸어 나왔다"
    blocks = chunk_caption_blocks(text, 9, 2)
    assert len(blocks) > 1
    for block in blocks:
        lines = block.split("\\N")
        assert len(lines) <= 2
        assert all(len(line) <= 9 for line in lines)


def test_blocks_preserve_every_word():
    text = "하나 둘 셋 넷 다섯 여섯 일곱 여덟 아홉 열"
    blocks = chunk_caption_blocks(text, 7, 2)
    rendered = " ".join(" ".join(b.split("\\N")) for b in blocks)
    assert rendered.split() == text.split()


def test_empty_text_produces_no_blocks():
    assert chunk_caption_blocks("   ", 10, 2) == []


def test_cjk_fits_fewer_characters_per_line_than_latin():
    """Hangul is full-width; treating it as Latin overflows the frame."""
    assert chars_per_line(KOREAN, 64, 720) < chars_per_line("plain latin text", 64, 720)


def test_chars_per_line_scales_with_width_and_size():
    assert chars_per_line(KOREAN, 64, 1080) > chars_per_line(KOREAN, 64, 720)
    assert chars_per_line(KOREAN, 96, 720) < chars_per_line(KOREAN, 48, 720)


# ── cue timing ──────────────────────────────────────────────────────────────


def test_split_covers_the_whole_cue_without_gaps():
    spans = split_cue_duration(3.0, 9.0, ["a" * 10, "b" * 20, "c" * 10])
    assert spans[0][0] == 3.0
    assert spans[-1][1] == 9.0
    for earlier, later in zip(spans, spans[1:]):
        assert earlier[1] == pytest.approx(later[0])


def test_a_longer_block_gets_more_screen_time():
    short, long = split_cue_duration(0.0, 10.0, ["hi", "a much longer block of text"])
    assert (long[1] - long[0]) > (short[1] - short[0])


def test_every_block_stays_readable_even_when_lopsided():
    spans = split_cue_duration(0.0, 6.0, ["x", "y" * 200, "z"])
    assert all(end - start >= 0.5 for start, end in spans)


def test_single_block_keeps_the_original_timing():
    assert split_cue_duration(1.0, 4.0, ["only"]) == [(1.0, 4.0)]


def test_ass_timestamps_use_centiseconds():
    assert format_ass_timestamp(3661.5) == "1:01:01.50"
    assert format_ass_timestamp(0) == "0:00:00.00"
    assert format_ass_timestamp(-1) == "0:00:00.00"


# ── style resolution ────────────────────────────────────────────────────────


def test_default_preset_is_used_when_none_is_named():
    style, warnings = style_from_options(None)
    assert style == PRESETS[DEFAULT_PRESET].normalized()
    assert warnings == []


def test_named_preset_is_applied_and_fields_override_it():
    style, warnings = style_from_options({"preset": "yellow", "position": "top"})
    assert style.primary_color == PRESETS["yellow"].primary_color
    assert style.position == "top"
    assert warnings == []


def test_unknown_preset_warns_and_falls_back():
    style, warnings = style_from_options({"preset": "neon-explosion"})
    assert style == PRESETS[DEFAULT_PRESET].normalized()
    assert any("unknown caption preset" in w for w in warnings)


def test_unknown_style_key_is_reported_rather_than_swallowed():
    _style, warnings = style_from_options({"font_colour": "#fff"})
    assert any("font_colour" in w for w in warnings)


def test_out_of_range_style_values_are_clamped():
    style, _ = style_from_options({"font_scale": 5.0, "margin_scale": -1.0, "max_lines": 99})
    assert MIN_FONT_SCALE <= style.font_scale <= MAX_FONT_SCALE
    assert style.margin_scale >= 0.0
    assert style.max_lines <= 4


def test_invalid_position_falls_back_to_bottom():
    style, _ = style_from_options({"position": "sideways"})
    assert style.position == "bottom"


# ── fonts ───────────────────────────────────────────────────────────────────


def test_a_font_file_is_used_directly_with_its_directory(tmp_path):
    font_file = tmp_path / "MyFace.ttf"
    font_file.write_bytes(b"not really a font")
    choice = resolve_font(str(font_file), KOREAN)
    assert choice.fonts_dir == str(tmp_path)
    assert choice.family


def test_a_missing_font_file_warns_instead_of_silently_substituting(tmp_path):
    choice = resolve_font(str(tmp_path / "absent.ttf"), KOREAN)
    assert any("not found" in w for w in choice.warnings)
    assert choice.family


def test_auto_selection_always_names_a_family():
    assert resolve_font("", KOREAN).family
    assert resolve_font("", "latin only").family


# ── ASS document ────────────────────────────────────────────────────────────


def _ass(cues, **options) -> str:
    style, _ = style_from_options(options or None)
    return format_ass(
        cues, style, width=720, height=1280, font=FontChoice(family="Test Sans")
    )


def test_ass_declares_the_video_resolution_it_was_laid_out_for():
    """PlayRes is what makes the layout resolution-independent downstream."""
    document = _ass([Cue(0, 2, KOREAN)])
    assert "PlayResX: 720" in document
    assert "PlayResY: 1280" in document


def test_ass_style_line_carries_the_resolved_font_and_derived_size():
    document = _ass([Cue(0, 2, KOREAN)], font_scale=0.05)
    style_line = next(l for l in document.splitlines() if l.startswith("Style:"))
    assert "Test Sans" in style_line
    assert ",64," in style_line  # 0.05 * 1280


def test_absolute_font_size_overrides_the_scale():
    document = _ass([Cue(0, 2, KOREAN)], font_size=42, font_scale=0.2)
    assert ",42," in next(l for l in document.splitlines() if l.startswith("Style:"))


def test_position_maps_to_the_ass_alignment_code():
    codes = {}
    for position in ("bottom", "center", "top"):
        line = next(
            l for l in _ass([Cue(0, 2, "x")], position=position).splitlines()
            if l.startswith("Style:")
        )
        codes[position] = line.split(",")[18]
    assert codes == {"bottom": "2", "center": "5", "top": "8"}


def test_box_style_switches_the_border_mode():
    plain = next(l for l in _ass([Cue(0, 2, "x")]).splitlines() if l.startswith("Style:"))
    boxed = next(
        l for l in _ass([Cue(0, 2, "x")], box=True).splitlines() if l.startswith("Style:")
    )
    assert plain.split(",")[15] == "1"
    assert boxed.split(",")[15] == "3"


def test_each_block_becomes_its_own_dialogue_line():
    long_cue = Cue(0, 10, "그런데 그 순간 빛이 내려왔다 그리고 그녀가 걸어 나왔다 모두가 놀랐다")
    dialogues = [l for l in _ass([long_cue]).splitlines() if l.startswith("Dialogue:")]
    assert len(dialogues) > 1
    assert all("\\N" in d or d.split(",,")[-1] for d in dialogues)


def test_blank_cues_are_dropped():
    document = _ass([Cue(0, 1, "   "), Cue(1, 2, "real")])
    assert len([l for l in document.splitlines() if l.startswith("Dialogue:")]) == 1


def test_override_braces_in_text_cannot_inject_ass_tags():
    document = _ass([Cue(0, 2, "{\\pos(0,0)}드론 샷")])
    dialogue = next(l for l in document.splitlines() if l.startswith("Dialogue:"))
    assert "{" not in dialogue and "}" not in dialogue
    assert "드론" in dialogue


def test_uppercase_option_applies_to_the_rendered_text():
    document = _ass([Cue(0, 2, "hello there")], uppercase=True)
    assert "HELLO" in document


def test_dialogue_timings_stay_inside_the_cue():
    cue = Cue(4.0, 9.0, "아주 긴 문장 하나 둘 셋 넷 다섯 여섯 일곱 여덟 아홉 열 열하나 열둘")
    for line in _ass([cue]).splitlines():
        if not line.startswith("Dialogue:"):
            continue
        start, end = line.split(",")[1:3]
        for stamp in (start, end):
            hours, minutes, rest = stamp.split(":")
            seconds = int(hours) * 3600 + int(minutes) * 60 + float(rest)
            assert 4.0 - 0.01 <= seconds <= 9.0 + 0.01


# ── hook title overlay ──────────────────────────────────────────────────────


def test_no_overlay_is_built_when_none_is_requested():
    assert title_overlay_from_options(None)[0] is None
    assert title_overlay_from_options(False)[0] is None


def test_overlay_falls_back_to_the_generated_title():
    """`titles` then `burn` should not need the title repeated by hand."""
    overlay, warnings = title_overlay_from_options({}, generated_title="버려진 그녀의 귀환")
    assert overlay is not None
    assert overlay.text == "버려진 그녀의 귀환"
    assert warnings == []


def test_explicit_text_wins_over_the_generated_title():
    overlay, _ = title_overlay_from_options(
        {"text": "직접 쓴 제목"}, generated_title="자동 생성 제목"
    )
    assert overlay.text == "직접 쓴 제목"


def test_overlay_with_no_text_anywhere_warns():
    overlay, warnings = title_overlay_from_options({})
    assert overlay is None
    assert any("no title text" in w for w in warnings)


def test_overlay_runs_the_whole_video_unless_a_duration_is_given():
    overlay, _ = title_overlay_from_options({"text": "제목"}, video_duration=30.0)
    assert (overlay.start, overlay.end) == (0.0, 30.0)

    shorter, _ = title_overlay_from_options(
        {"text": "제목", "duration": 5}, video_duration=30.0
    )
    assert shorter.end == 5.0


def test_overlay_is_clipped_to_the_end_of_the_video():
    overlay, _ = title_overlay_from_options(
        {"text": "제목", "duration": 999}, video_duration=12.0
    )
    assert overlay.end == 12.0


def test_overlay_starting_past_the_end_is_skipped():
    overlay, warnings = title_overlay_from_options(
        {"text": "제목", "start": 40}, video_duration=30.0
    )
    assert overlay is None
    assert any("after the video ends" in w for w in warnings)


def test_overlay_defaults_sit_above_the_captions_and_larger():
    overlay, _ = title_overlay_from_options({"text": "제목"}, video_duration=10.0)
    caption_style, _ = style_from_options(None)
    assert overlay.style.position == "top"
    assert caption_style.position == "bottom"
    assert overlay.style.font_scale > caption_style.font_scale


def test_overlay_style_fields_are_overridable_and_validated():
    overlay, warnings = title_overlay_from_options(
        {"text": "제목", "position": "center", "font_scale": 0.09, "glow": True},
        video_duration=10.0,
    )
    assert overlay.style.position == "center"
    assert overlay.style.font_scale == pytest.approx(0.09)
    assert any("glow" in w for w in warnings)


def test_title_gets_its_own_style_row_so_it_can_be_restyled_alone():
    overlay, _ = title_overlay_from_options({"text": "버려진 그녀"}, video_duration=10.0)
    style, _ = style_from_options(None)
    document = format_ass(
        [Cue(0, 2, KOREAN)], style, width=720, height=1280,
        font=FontChoice(family="Test Sans"), title=overlay, video_duration=10.0,
    )
    style_names = [l.split(",")[0].removeprefix("Style: ") for l in document.splitlines() if l.startswith("Style:")]
    assert style_names == ["Default", "Title"]
    title_line = next(l for l in document.splitlines() if l.startswith("Dialogue:") and ",Title," in l)
    assert "버려진 그녀" in title_line


def test_a_long_title_wraps_but_is_never_split_into_sequential_blocks():
    """A hook title is one on-screen unit — splitting it in time would hide it."""
    overlay, _ = title_overlay_from_options(
        {"text": "모두가 버렸던 그녀가 전쟁의 신으로 돌아와 세상을 뒤집었다"},
        video_duration=30.0,
    )
    style, _ = style_from_options(None)
    document = format_ass(
        [], style, width=720, height=1280,
        font=FontChoice(family="Test Sans"), title=overlay, video_duration=30.0,
    )
    title_lines = [l for l in document.splitlines() if ",Title," in l]
    assert len(title_lines) == 1
    assert "\\N" in title_lines[0]


def test_no_title_means_no_title_style_or_events():
    style, _ = style_from_options(None)
    document = format_ass(
        [Cue(0, 2, KOREAN)], style, width=720, height=1280,
        font=FontChoice(family="Test Sans"),
    )
    assert ",Title," not in document
    assert "Style: Title" not in document


# ── burn command ────────────────────────────────────────────────────────────


def test_filter_paths_escape_the_characters_that_break_filtergraphs():
    escaped = escape_filter_path(r"C:\videos\subs.ass")
    assert escaped == r"C\:\\videos\\subs.ass"


def test_burn_command_renders_subtitles_and_copies_the_audio():
    command = build_burn_command(
        "ffmpeg", "in.mp4", "/tmp/subs.ass", "out.mp4", fonts_dir="/fonts"
    )
    joined = " ".join(command)
    assert "ass='/tmp/subs.ass'" in joined
    assert "fontsdir='/fonts'" in joined
    assert "-c:a copy" in joined
    assert command[-1] == "out.mp4"


def test_burn_command_omits_fontsdir_when_not_needed():
    command = build_burn_command("ffmpeg", "in.mp4", "subs.ass", "out.mp4")
    assert "fontsdir" not in " ".join(command)


# ── end to end ──────────────────────────────────────────────────────────────


def _fake_transcriber(path, model=None):
    return {"success": True, "transcript": "모두가 끝났다고 말했다", "provider": "fake"}


@requires_ffmpeg
def test_burn_stage_writes_an_ass_and_a_captioned_video(tmp_path):
    ffmpeg = find_ffmpeg()
    source = tmp_path / "clip.mp4"
    subprocess.run(
        [ffmpeg, "-y", "-nostdin",
         "-f", "lavfi", "-i", "testsrc=size=360x640:rate=15:duration=6",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=6",
         "-pix_fmt", "yuv420p", "-shortest", str(source)],
        capture_output=True, check=True, timeout=180,
    )

    result = run_pipeline(
        str(source),
        stages=["burn"],
        output_dir=str(tmp_path / "out"),
        cue_seconds=3.0,
        caption_style={"preset": "shorts", "font_scale": 0.06},
        transcriber=_fake_transcriber,
    )

    assert "burn" in result.stages
    ass_text = (tmp_path / "out" / "subtitles.ass").read_text(encoding="utf-8")
    assert "PlayResY: 640" in ass_text
    assert "Dialogue:" in ass_text
    burned = tmp_path / "out" / "clip_captioned.mp4"
    assert burned.stat().st_size > 0
    assert result.captions["video"] == str(burned)
    assert result.captions["font"]


@requires_ffmpeg
def test_burned_output_keeps_the_source_dimensions_and_duration(tmp_path):
    """Burning must not silently rescale or truncate the video."""
    ffmpeg, ffprobe = find_ffmpeg(), find_ffprobe()
    source = tmp_path / "clip.mp4"
    subprocess.run(
        [ffmpeg, "-y", "-nostdin",
         "-f", "lavfi", "-i", "testsrc=size=360x640:rate=15:duration=5",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=5",
         "-pix_fmt", "yuv420p", "-shortest", str(source)],
        capture_output=True, check=True, timeout=180,
    )
    result = run_pipeline(
        str(source),
        stages=["burn"],
        output_dir=str(tmp_path / "out"),
        cue_seconds=3.0,
        transcriber=_fake_transcriber,
    )
    probe = subprocess.run(
        [ffprobe, "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-show_entries", "format=duration",
         "-of", "default=nw=1", result.captions["video"]],
        capture_output=True, text=True, check=True, timeout=60,
    ).stdout
    assert "width=360" in probe and "height=640" in probe
    duration = float(re.search(r"duration=([\d.]+)", probe).group(1))
    assert duration == pytest.approx(result.video["duration"], abs=0.5)


@requires_ffmpeg
def test_generated_title_is_burned_over_the_captions(tmp_path):
    """The titles stage feeds the overlay, so one call titles and burns."""
    ffmpeg = find_ffmpeg()
    source = tmp_path / "clip.mp4"
    subprocess.run(
        [ffmpeg, "-y", "-nostdin",
         "-f", "lavfi", "-i", "testsrc=size=360x640:rate=15:duration=6",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=6",
         "-pix_fmt", "yuv420p", "-shortest", str(source)],
        capture_output=True, check=True, timeout=180,
    )

    class _Response:
        choices = [type("C", (), {"message": type("M", (), {"content": '{"titles": ["버려진 그녀의 귀환"]}'})()})()]

    result = run_pipeline(
        str(source),
        stages=["titles", "burn"],
        output_dir=str(tmp_path / "out"),
        cue_seconds=3.0,
        title_overlay={},
        transcriber=_fake_transcriber,
        llm=lambda **_kwargs: _Response(),
    )

    assert result.titles == ["버려진 그녀의 귀환"]
    assert result.captions["title_overlay"]["text"] == "버려진 그녀의 귀환"
    ass_text = (tmp_path / "out" / "subtitles.ass").read_text(encoding="utf-8")
    assert "Style: Title" in ass_text
    title_line = next(l for l in ass_text.splitlines() if ",Title," in l)
    # The title wraps to the frame width, so compare the words, not the string.
    assert title_line.split(",,")[-1].replace("\\N", " ").split() == "버려진 그녀의 귀환".split()
    assert Path(result.captions["video"]).stat().st_size > 0


@requires_ffmpeg
def test_burn_without_any_cues_warns_instead_of_producing_a_silent_no_op(tmp_path):
    def _empty(path, model=None):
        return {"success": True, "transcript": "", "provider": "fake"}

    ffmpeg = find_ffmpeg()
    source = tmp_path / "clip.mp4"
    subprocess.run(
        [ffmpeg, "-y", "-nostdin",
         "-f", "lavfi", "-i", "testsrc=size=320x240:rate=15:duration=4",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=4",
         "-pix_fmt", "yuv420p", "-shortest", str(source)],
        capture_output=True, check=True, timeout=180,
    )
    result = run_pipeline(
        str(source),
        stages=["burn"],
        output_dir=str(tmp_path / "out"),
        transcriber=_empty,
    )
    assert "burn" not in result.stages
    assert any("burn" in warning for warning in result.warnings)
