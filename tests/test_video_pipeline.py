"""Tests for the video auto-processing pipeline (``tools/video_pipeline.py``).

Three layers:

1. **Pure logic** — silence parsing, cue-window planning, subtitle formatting,
   frame scoring, thumbnail selection, title parsing. These run everywhere and
   carry the behavioural contracts.
2. **Orchestration** — ``run_pipeline`` with the STT and LLM calls injected, so
   stage wiring (dependencies, skips, warnings, file writes) is exercised
   without a provider.
3. **Real ffmpeg** — an end-to-end run over a video synthesized by ffmpeg
   itself, skipped when ffmpeg is not installed.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from tools.video_pipeline import (
    ALL_STAGES,
    DEFAULT_STAGES,
    adaptive_noise_floor,
    SCORE_FRAME_SIZE,
    Cue,
    FrameScore,
    VideoPipelineError,
    build_title_input,
    candidate_times,
    default_output_dir,
    detect_silences,
    find_ffmpeg,
    find_ffprobe,
    format_srt,
    format_timestamp,
    format_vtt,
    join_transcript,
    measure_mean_volume,
    parse_silence_log,
    parse_title_response,
    plan_cue_windows,
    probe_video,
    run_pipeline,
    score_frame,
    select_thumbnail_times,
    speech_regions,
    strip_overlap,
)

HAS_FFMPEG = bool(find_ffmpeg() and find_ffprobe())
requires_ffmpeg = pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg/ffprobe not installed")


# ── silence parsing ─────────────────────────────────────────────────────────


SILENCE_LOG = """\
[silencedetect @ 0x55] silence_start: 2.5
[silencedetect @ 0x55] silence_end: 4.0 | silence_duration: 1.5
[silencedetect @ 0x55] silence_start: 9.25
[silencedetect @ 0x55] silence_end: 10.0 | silence_duration: 0.75
"""


def test_parse_silence_log_pairs_starts_with_ends():
    assert parse_silence_log(SILENCE_LOG, duration=20.0) == [(2.5, 4.0), (9.25, 10.0)]


def test_parse_silence_log_closes_trailing_silence_at_duration():
    """A file that fades out logs a start with no matching end."""
    log = SILENCE_LOG + "[silencedetect @ 0x55] silence_start: 18.0\n"
    assert parse_silence_log(log, duration=20.0)[-1] == (18.0, 20.0)


def test_parse_silence_log_ignores_unrelated_output():
    assert parse_silence_log("frame=  120 fps=0.0 q=-1.0 size=N/A\n", duration=5.0) == []


def test_silence_end_beyond_duration_is_clamped():
    log = "silence_start: 4.0\nsilence_end: 99.0 | silence_duration: 95.0\n"
    assert parse_silence_log(log, duration=5.0) == [(4.0, 5.0)]


# ── silence threshold ───────────────────────────────────────────────────────


def test_adaptive_floor_tracks_the_material_level():
    """Quiet material needs a lower floor; loud material needs a higher one."""
    assert adaptive_noise_floor(-48.0) < adaptive_noise_floor(-22.0)
    assert adaptive_noise_floor(-22.0) == -34


def test_adaptive_floor_is_clamped_to_a_workable_range():
    assert -60 <= adaptive_noise_floor(-95.0) <= -25
    assert -60 <= adaptive_noise_floor(-2.0) <= -25


def test_adaptive_floor_falls_back_when_the_level_is_unmeasurable():
    from tools.video_pipeline import SILENCE_NOISE_DB

    assert adaptive_noise_floor(None) == SILENCE_NOISE_DB


@requires_ffmpeg
def test_quiet_audio_yields_the_same_cues_as_loud_audio(tmp_path):
    """Regression: a fixed -32 dB floor read a quiet recording as mostly silence,
    dropping 60% of the dialogue out of the subtitles."""
    ffmpeg = find_ffmpeg()

    def _bursts(path: Path, gain_db: int) -> Path:
        # Three 3-second tones separated by one second of true silence.
        parts = []
        for index in range(3):
            part = tmp_path / f"tone-{gain_db}-{index}.wav"
            subprocess.run(
                [ffmpeg, "-y", "-nostdin", "-f", "lavfi",
                 "-i", f"sine=frequency={300 + index * 60}:duration=3",
                 "-af", f"volume={gain_db}dB", "-ar", "16000", "-ac", "1", str(part)],
                capture_output=True, check=True, timeout=120,
            )
            parts.append(part)
        gap = tmp_path / "gap.wav"
        subprocess.run(
            [ffmpeg, "-y", "-nostdin", "-f", "lavfi",
             "-i", "anullsrc=r=16000:cl=mono", "-t", "1", "-ar", "16000", str(gap)],
            capture_output=True, check=True, timeout=120,
        )
        listing = tmp_path / f"list-{gain_db}.txt"
        order = [gap, parts[0], gap, parts[1], gap, parts[2], gap]
        listing.write_text("".join(f"file '{p}'\n" for p in order), encoding="utf-8")
        subprocess.run(
            [ffmpeg, "-y", "-nostdin", "-f", "concat", "-safe", "0",
             "-i", str(listing), "-c", "copy", str(path)],
            capture_output=True, check=True, timeout=120,
        )
        return path

    duration = 13.0
    loud = _bursts(tmp_path / "loud.wav", 0)
    quiet = _bursts(tmp_path / "quiet.wav", -25)

    def _plan(path):
        return plan_cue_windows(duration, detect_silences(str(path), duration), cue_seconds=8.0)

    loud_windows, quiet_windows = _plan(loud), _plan(quiet)
    assert len(quiet_windows) == len(loud_windows) == 3
    loud_covered = sum(end - start for start, end in loud_windows)
    quiet_covered = sum(end - start for start, end in quiet_windows)
    assert quiet_covered == pytest.approx(loud_covered, abs=0.3)


@requires_ffmpeg
def test_mean_volume_tracks_the_attenuation_applied(tmp_path):
    """The measurement has to move with the material, not just return a number."""
    ffmpeg = find_ffmpeg()

    def _tone(name: str, gain_db: int) -> str:
        path = tmp_path / name
        subprocess.run(
            [ffmpeg, "-y", "-nostdin", "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
             "-af", f"volume={gain_db}dB", "-ar", "16000", "-ac", "1", str(path)],
            capture_output=True, check=True, timeout=120,
        )
        return str(path)

    loud = measure_mean_volume(_tone("loud.wav", 0), 2.0)
    quiet = measure_mean_volume(_tone("quiet.wav", -20), 2.0)
    assert loud is not None and quiet is not None
    assert quiet == pytest.approx(loud - 20, abs=1.0)
    assert adaptive_noise_floor(quiet) < adaptive_noise_floor(loud)


# ── speech regions ──────────────────────────────────────────────────────────


def test_speech_regions_are_the_complement_of_silence():
    regions = speech_regions(20.0, [(2.5, 4.0), (9.25, 10.0)])
    assert regions == [(0.0, 2.5), (4.0, 9.25), (10.0, 20.0)]


def test_speech_regions_drop_slivers_between_adjacent_silences():
    """A 0.1s gap between two silences is a click, not speech worth a cue."""
    regions = speech_regions(10.0, [(0.0, 3.0), (3.1, 8.0)])
    assert all(end - start >= 0.35 for start, end in regions)
    assert (3.0, 3.1) not in regions


def test_speech_regions_handle_overlapping_silences():
    assert speech_regions(10.0, [(1.0, 5.0), (2.0, 4.0)]) == [(0.0, 1.0), (5.0, 10.0)]


# ── cue planning ────────────────────────────────────────────────────────────


def _covers_no_overlap(windows):
    return all(windows[i][1] <= windows[i + 1][0] + 1e-6 for i in range(len(windows) - 1))


def test_cue_windows_never_exceed_the_hard_ceiling():
    """A long unbroken monologue still gets chopped into cue-sized pieces."""
    windows = plan_cue_windows(120.0, [], cue_seconds=8.0)
    assert windows
    assert max(end - start for start, end in windows) <= 8.0 * 1.75 + 0.5
    assert _covers_no_overlap(windows)


def test_cue_windows_stay_inside_the_file():
    windows = plan_cue_windows(30.0, [(10.0, 12.0)], cue_seconds=5.0)
    assert windows[0][0] >= 0.0
    assert windows[-1][1] <= 30.0


def test_cue_windows_do_not_cover_silence():
    """Silence is never sent to the STT provider — it costs money and invites
    whisper-family hallucination on empty audio."""
    windows = plan_cue_windows(30.0, [(10.0, 20.0)], cue_seconds=8.0)
    for start, end in windows:
        midpoint = (start + end) / 2
        assert not (10.0 < midpoint < 20.0)


def test_higher_cue_seconds_produces_fewer_windows():
    """The documented cost dial: longer cues mean proportionally fewer STT calls."""
    short = plan_cue_windows(180.0, [], cue_seconds=4.0)
    long = plan_cue_windows(180.0, [], cue_seconds=24.0)
    assert len(long) < len(short)


def test_cue_seconds_is_clamped_to_the_supported_range():
    """An absurd request still yields usable cues rather than one giant window."""
    windows = plan_cue_windows(300.0, [], cue_seconds=9999.0)
    assert max(end - start for start, end in windows) <= 30.0 * 1.75 + 0.5


def test_short_neighbouring_phrases_are_merged_into_one_cue():
    """Two words split by a 0.4s breath belong in the same cue."""
    silences = [(2.0, 2.4)]
    windows = plan_cue_windows(6.0, silences, cue_seconds=8.0)
    assert len(windows) == 1


def test_cue_windows_are_empty_for_silent_audio():
    assert plan_cue_windows(10.0, [(0.0, 10.0)], cue_seconds=5.0) == []


# ── boundary overlap ────────────────────────────────────────────────────────


def test_overlap_repeated_at_a_join_is_trimmed():
    """Cue slices overlap so boundary words survive; the second copy must go."""
    assert strip_overlap("나와함께싸울거야?", "싸울거야? 184년") == "184년"


def test_overlap_trimming_ignores_spacing_and_punctuation():
    """Recognizers place spaces and periods inconsistently between runs."""
    assert strip_overlap("we fight together", "together, in 184") == "in 184"


def test_unrelated_neighbours_are_left_alone():
    assert strip_overlap("굶어죽을거야?", "내가바로장각이다") == "내가바로장각이다"


def test_a_single_repeated_character_is_not_treated_as_overlap():
    """One shared syllable is a coincidence, not a duplicated word."""
    assert strip_overlap("칼을들어", "어머니가돌아왔다") == "어머니가돌아왔다"


def test_a_genuine_repetition_longer_than_the_window_survives():
    previous = "빨리 가자 가자 가자 지금 당장 움직여야 한다 서둘러"
    assert strip_overlap(previous, "우리는 이미 늦었다") == "우리는 이미 늦었다"


def test_overlap_trimming_handles_empty_sides():
    assert strip_overlap("", "첫 큐입니다") == "첫 큐입니다"
    assert strip_overlap("이전 큐", "") == ""


# ── subtitle formatting ─────────────────────────────────────────────────────


def test_format_timestamp_srt_and_vtt_separators():
    assert format_timestamp(3661.5) == "01:01:01,500"
    assert format_timestamp(3661.5, separator=".") == "01:01:01.500"
    assert format_timestamp(-4.0) == "00:00:00,000"


def test_srt_is_sequentially_numbered_with_arrow_ranges():
    srt = format_srt([Cue(0.0, 2.0, "안녕하세요"), Cue(2.5, 4.25, "반갑습니다")])
    lines = srt.splitlines()
    assert lines[0] == "1"
    assert lines[1] == "00:00:00,000 --> 00:00:02,000"
    assert lines[2] == "안녕하세요"
    assert "2" in lines
    assert "00:00:02,500 --> 00:00:04,250" in lines


def test_vtt_starts_with_the_webvtt_header_and_dot_decimals():
    vtt = format_vtt([Cue(1.0, 2.0, "hello")])
    assert vtt.startswith("WEBVTT")
    assert "00:00:01.000 --> 00:00:02.000" in vtt
    assert "," not in vtt.split("-->")[0]


def test_srt_and_vtt_describe_the_same_cues():
    cues = [Cue(0.0, 1.0, "one"), Cue(1.0, 2.0, "two"), Cue(2.0, 3.0, "three")]
    srt, vtt = format_srt(cues), format_vtt(cues)
    for cue in cues:
        assert cue.text in srt and cue.text in vtt
    assert srt.count("-->") == vtt.count("-->") == len(cues)


def test_join_transcript_skips_blank_cues():
    assert join_transcript([Cue(0, 1, "a"), Cue(1, 2, "  "), Cue(2, 3, "b")]) == "a b"


# ── frame scoring ───────────────────────────────────────────────────────────


def _solid_frame(red: int, green: int, blue: int, size: int = SCORE_FRAME_SIZE) -> bytes:
    return bytes([red, green, blue]) * (size * size)


def _checkerboard(size: int = SCORE_FRAME_SIZE) -> bytes:
    pixels = bytearray()
    for row in range(size):
        for column in range(size):
            value = 235 if (row + column) % 2 == 0 else 20
            pixels.extend((value, value, value))
    return bytes(pixels)


def _gradient(size: int = SCORE_FRAME_SIZE) -> bytes:
    pixels = bytearray()
    for row in range(size):
        for column in range(size):
            pixels.extend((column * 4 % 256, row * 4 % 256, 128))
    return bytes(pixels)


def test_solid_black_frame_is_rejected_as_flat():
    result = score_frame(_solid_frame(0, 0, 0), SCORE_FRAME_SIZE, SCORE_FRAME_SIZE)
    assert result.flat is True
    assert result.score == 0.0


def test_solid_white_frame_is_rejected_as_flat():
    assert score_frame(_solid_frame(255, 255, 255), SCORE_FRAME_SIZE, SCORE_FRAME_SIZE).flat


def test_mid_grey_frame_is_rejected_despite_ideal_exposure():
    """Perfect exposure must not rescue a frame with no detail in it."""
    result = score_frame(_solid_frame(128, 128, 128), SCORE_FRAME_SIZE, SCORE_FRAME_SIZE)
    assert result.flat is True
    assert result.exposure > 0.9


def test_detailed_frame_outscores_a_flat_one():
    detailed = score_frame(_gradient(), SCORE_FRAME_SIZE, SCORE_FRAME_SIZE)
    flat = score_frame(_solid_frame(90, 90, 90), SCORE_FRAME_SIZE, SCORE_FRAME_SIZE)
    assert detailed.score > flat.score


def test_sharp_frame_scores_higher_sharpness_than_a_soft_gradient():
    assert (
        score_frame(_checkerboard(), SCORE_FRAME_SIZE, SCORE_FRAME_SIZE).sharpness
        > score_frame(_gradient(), SCORE_FRAME_SIZE, SCORE_FRAME_SIZE).sharpness
    )


def test_colourful_frame_scores_higher_colorfulness_than_greyscale():
    size = SCORE_FRAME_SIZE
    grey = bytearray()
    colour = bytearray()
    for index in range(size * size):
        value = (index * 7) % 256
        grey.extend((value, value, value))
        colour.extend((value, (value + 90) % 256, (value + 180) % 256))
    grey_score = score_frame(bytes(grey), size, size)
    colour_score = score_frame(bytes(colour), size, size)
    assert colour_score.colorfulness > grey_score.colorfulness


def test_score_components_stay_normalised():
    for buffer in (_checkerboard(), _gradient(), _solid_frame(200, 40, 40)):
        result = score_frame(buffer, SCORE_FRAME_SIZE, SCORE_FRAME_SIZE)
        for value in (
            result.score,
            result.sharpness,
            result.contrast,
            result.colorfulness,
            result.exposure,
            result.brightness,
        ):
            assert 0.0 <= value <= 1.0


def test_truncated_buffer_is_rejected_rather_than_crashing():
    result = score_frame(b"\x00\x01\x02", SCORE_FRAME_SIZE, SCORE_FRAME_SIZE)
    assert result.flat is True and result.score == 0.0


def test_frame_score_serialises_time_and_metrics():
    payload = score_frame(_gradient(), SCORE_FRAME_SIZE, SCORE_FRAME_SIZE, time=12.5).as_dict()
    assert payload["time"] == 12.5
    assert set(payload["metrics"]) == {
        "sharpness", "contrast", "colorfulness", "exposure", "brightness",
    }


# ── thumbnail candidates and selection ──────────────────────────────────────


def test_candidate_times_skip_the_intro_and_outro_edges():
    times = candidate_times(100.0, thumbnail_count=3)
    assert times
    assert min(times) >= 4.0
    assert max(times) <= 96.0


def test_candidate_times_are_bounded_and_sorted():
    times = candidate_times(7200.0, thumbnail_count=10, scene_times=[float(t) for t in range(0, 7000, 5)])
    assert times == sorted(times)
    assert len(times) <= 40


def test_candidate_times_include_scene_changes():
    times = candidate_times(100.0, thumbnail_count=2, scene_times=[41.7])
    assert any(abs(t - 41.7) < 0.5 for t in times)


def test_candidate_times_cover_the_whole_video_after_thinning():
    """Thinning must not drop the tail: scene detection can front-load candidates."""
    scene_times = [float(t) for t in range(5, 60)]
    times = candidate_times(600.0, thumbnail_count=3, scene_times=scene_times)
    assert max(times) > 300.0


def test_selection_spreads_picks_across_the_video():
    """Three near-identical high scores in one shot must not fill every slot."""
    scores = [
        FrameScore(10.0, 0.90, 0.9, 0.9, 0.9, 0.9, 0.5),
        FrameScore(10.5, 0.89, 0.9, 0.9, 0.9, 0.9, 0.5),
        FrameScore(11.0, 0.88, 0.9, 0.9, 0.9, 0.9, 0.5),
        FrameScore(200.0, 0.60, 0.6, 0.6, 0.6, 0.6, 0.5),
        FrameScore(400.0, 0.55, 0.5, 0.5, 0.5, 0.5, 0.5),
    ]
    picked = select_thumbnail_times(scores, count=3, duration=600.0)
    times = sorted(pick.time for pick in picked)
    assert times == [10.0, 200.0, 400.0]


def test_selection_ignores_flat_frames():
    scores = [
        FrameScore(5.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, flat=True),
        FrameScore(50.0, 0.4, 0.4, 0.4, 0.4, 0.4, 0.5),
    ]
    picked = select_thumbnail_times(scores, count=3, duration=100.0)
    assert [pick.time for pick in picked] == [50.0]


def test_selection_fills_the_quota_when_spacing_cannot_be_met():
    """A 4-second clip has nowhere to spread to — return the best frames anyway."""
    scores = [
        FrameScore(1.0, 0.8, 0.8, 0.8, 0.8, 0.8, 0.5),
        FrameScore(1.4, 0.7, 0.7, 0.7, 0.7, 0.7, 0.5),
        FrameScore(1.8, 0.6, 0.6, 0.6, 0.6, 0.6, 0.5),
    ]
    picked = select_thumbnail_times(scores, count=3, duration=4.0)
    assert len(picked) == 3
    assert len({pick.time for pick in picked}) == 3


def test_selection_returns_results_ranked_by_score():
    scores = [
        FrameScore(10.0, 0.3, 0.3, 0.3, 0.3, 0.3, 0.5),
        FrameScore(100.0, 0.9, 0.9, 0.9, 0.9, 0.9, 0.5),
    ]
    picked = select_thumbnail_times(scores, count=2, duration=200.0)
    assert [pick.score for pick in picked] == sorted(
        [pick.score for pick in picked], reverse=True
    )


# ── title generation ────────────────────────────────────────────────────────


def test_parse_title_response_reads_the_requested_json_object():
    titles = parse_title_response('{"titles": ["첫 번째 제목", "두 번째 제목"]}', limit=5)
    assert titles == ["첫 번째 제목", "두 번째 제목"]


def test_parse_title_response_reads_a_fenced_bare_array():
    titles = parse_title_response('```json\n["A", "B"]\n```', limit=5)
    assert titles == ["A", "B"]


def test_parse_title_response_falls_back_to_a_numbered_list():
    """Small models drop out of JSON mode; good titles should survive that."""
    titles = parse_title_response("1. First title\n2. Second title\n", limit=5)
    assert titles == ["First title", "Second title"]


def test_parse_title_response_deduplicates_and_respects_the_limit():
    titles = parse_title_response('{"titles": ["Same", "same", "Other", "Third"]}', limit=2)
    assert titles == ["Same", "Other"]


def test_parse_title_response_handles_objects_and_empty_input():
    assert parse_title_response('{"titles": [{"title": "Wrapped"}]}', limit=3) == ["Wrapped"]
    assert parse_title_response("", limit=3) == []


def test_build_title_input_keeps_the_opening_and_the_close():
    transcript = "HEAD" + ("x" * 20000) + "TAIL"
    trimmed = build_title_input(transcript)
    assert trimmed.startswith("HEAD")
    assert trimmed.endswith("TAIL")
    assert len(trimmed) < len(transcript)


def test_build_title_input_leaves_short_transcripts_alone():
    assert build_title_input("  short transcript  ") == "short transcript"


# ── orchestration (STT + LLM injected) ──────────────────────────────────────


class _FakeChoice:
    def __init__(self, content):
        self.message = type("Message", (), {"content": content})()


class _FakeResponse:
    def __init__(self, content):
        self.choices = [_FakeChoice(content)]


def _fake_llm(**_kwargs):
    return _FakeResponse('{"titles": ["A hooky title", "Another angle"]}')


def _fake_transcriber(path, model=None):
    return {"success": True, "transcript": "spoken words", "provider": "fake"}


def _make_video(path: Path, seconds: int = 6) -> Path:
    """Synthesize a test video with tone audio and moving colour bars."""
    subprocess.run(
        [
            find_ffmpeg(), "-y", "-nostdin",
            "-f", "lavfi", "-i", f"testsrc=size=320x240:rate=15:duration={seconds}",
            "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
            "-pix_fmt", "yuv420p",
            "-shortest",
            str(path),
        ],
        capture_output=True,
        check=True,
        timeout=120,
    )
    return path


def test_run_pipeline_rejects_a_missing_file(tmp_path):
    with pytest.raises(VideoPipelineError, match="not found"):
        run_pipeline(str(tmp_path / "nope.mp4"))


def test_run_pipeline_rejects_an_empty_stage_list(tmp_path):
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"not a video")
    with pytest.raises(VideoPipelineError, match="no valid stages"):
        run_pipeline(str(video), stages=["nonsense"])


def test_default_output_dir_is_under_hermes_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    target = default_output_dir("/videos/My Clip (final).mov")
    assert str(tmp_path / "home") in str(target)
    assert target.parent.name == "video_pipeline"
    assert " " not in target.name


@requires_ffmpeg
def test_pipeline_end_to_end_writes_every_asset(tmp_path):
    video = _make_video(tmp_path / "clip.mp4")
    out = tmp_path / "out"

    result = run_pipeline(
        str(video),
        output_dir=str(out),
        cue_seconds=3.0,
        thumbnail_count=2,
        transcriber=_fake_transcriber,
        llm=_fake_llm,
    )
    payload = result.as_dict()

    assert set(result.stages) == set(DEFAULT_STAGES)
    assert Path(result.audio_path).exists()
    assert Path(payload["subtitles"]["srt"]).read_text(encoding="utf-8").startswith("1\n")
    assert Path(payload["subtitles"]["vtt"]).read_text(encoding="utf-8").startswith("WEBVTT")
    assert payload["titles"] == ["A hooky title", "Another angle"]
    assert len(payload["thumbnails"]) == 2
    for thumbnail in payload["thumbnails"]:
        assert Path(thumbnail["path"]).stat().st_size > 0
        assert 0.0 < thumbnail["time"] < result.video["duration"]
    assert Path(result.transcript_path).read_text(encoding="utf-8")


@requires_ffmpeg
def test_subtitle_cue_times_stay_within_the_video(tmp_path):
    """Cue timing is only trustworthy if it can't run past the media itself."""
    video = _make_video(tmp_path / "clip.mp4")
    result = run_pipeline(
        str(video),
        stages=["subtitles"],
        output_dir=str(tmp_path / "out"),
        cue_seconds=2.0,
        transcriber=_fake_transcriber,
    )
    srt = Path(result.subtitles["srt"]).read_text(encoding="utf-8")
    duration = result.video["duration"]
    for line in srt.splitlines():
        if "-->" not in line:
            continue
        for stamp in line.split(" --> "):
            hours, minutes, rest = stamp.split(":")
            seconds = float(rest.replace(",", "."))
            assert int(hours) * 3600 + int(minutes) * 60 + seconds <= duration + 0.05


@requires_ffmpeg
def test_cleaned_text_is_what_reaches_the_subtitle_files(tmp_path):
    """The correction has to land before the cues are written, not after."""
    class _Response:
        def __init__(self, content):
            self.choices = [type("C", (), {"message": type("M", (), {"content": content})()})()]

    def _cleanup_llm(**kwargs):
        payload = json.loads(kwargs["messages"][1]["content"].split("\n\n", 1)[1])
        fixed = [{"i": c["i"], "t": "띄어쓰기 된 문장"} for c in payload["cues"]]
        return _Response(json.dumps({"cues": fixed}, ensure_ascii=False))

    def _spaceless(path, model=None):
        return {"success": True, "transcript": "띄어쓰기없는문장", "provider": "fake"}

    video = _make_video(tmp_path / "clip.mp4")
    result = run_pipeline(
        str(video),
        stages=["subtitles"],
        output_dir=str(tmp_path / "out"),
        cue_seconds=3.0,
        transcriber=_spaceless,
        llm=_cleanup_llm,
    )
    assert result.cleaned_cues > 0
    srt = Path(result.subtitles["srt"]).read_text(encoding="utf-8")
    assert "띄어쓰기 된 문장" in srt
    assert "띄어쓰기없는문장" not in srt


@requires_ffmpeg
def test_cleanup_can_be_turned_off(tmp_path):
    def _explode(**_kwargs):  # pragma: no cover - must never be called
        raise AssertionError("the cleanup model ran with clean_captions=False")

    def _spaceless(path, model=None):
        return {"success": True, "transcript": "띄어쓰기없는문장", "provider": "fake"}

    video = _make_video(tmp_path / "clip.mp4")
    result = run_pipeline(
        str(video),
        stages=["subtitles"],
        output_dir=str(tmp_path / "out"),
        cue_seconds=3.0,
        clean_captions=False,
        transcriber=_spaceless,
        llm=_explode,
    )
    assert result.cleaned_cues == 0
    assert "띄어쓰기없는문장" in Path(result.subtitles["srt"]).read_text(encoding="utf-8")


@requires_ffmpeg
def test_thumbnails_only_run_skips_audio_and_stt(tmp_path):
    """Asking for one stage must not silently pay for the others."""
    def _explode(*_args, **_kwargs):  # pragma: no cover - must never be called
        raise AssertionError("transcription ran for a thumbnails-only request")

    video = _make_video(tmp_path / "clip.mp4")
    result = run_pipeline(
        str(video),
        stages=["thumbnails"],
        output_dir=str(tmp_path / "out"),
        thumbnail_count=1,
        transcriber=_explode,
    )
    assert result.stages == ["thumbnails"]
    assert result.audio_path is None
    assert not result.subtitles
    assert not (tmp_path / "out" / "transcript.txt").exists()


@requires_ffmpeg
def test_titles_without_subtitles_still_transcribes_but_writes_no_cue_files(tmp_path):
    video = _make_video(tmp_path / "clip.mp4")
    result = run_pipeline(
        str(video),
        stages=["titles"],
        output_dir=str(tmp_path / "out"),
        cue_seconds=3.0,
        transcriber=_fake_transcriber,
        llm=_fake_llm,
    )
    assert result.titles
    assert not result.subtitles
    assert Path(result.transcript_path).exists()


@requires_ffmpeg
def test_failing_stt_windows_warn_instead_of_failing_the_run(tmp_path):
    """One bad slice must not cost the caller the thumbnails it also asked for."""
    def _failing(path, model=None):
        return {"success": False, "transcript": "", "error": "provider is down"}

    video = _make_video(tmp_path / "clip.mp4")
    result = run_pipeline(
        str(video),
        output_dir=str(tmp_path / "out"),
        cue_seconds=3.0,
        thumbnail_count=1,
        transcriber=_failing,
    )
    assert "thumbnails" in result.stages
    assert not result.subtitles
    assert any("provider is down" in warning for warning in result.warnings)


@requires_ffmpeg
def test_a_transcriber_that_raises_is_reported_not_propagated(tmp_path):
    """A provider SDK blowing up on one slice must not abort the whole run."""
    def _raising(path, model=None):
        raise RuntimeError("connection reset by peer")

    video = _make_video(tmp_path / "clip.mp4")
    result = run_pipeline(
        str(video),
        stages=["subtitles", "thumbnails"],
        output_dir=str(tmp_path / "out"),
        cue_seconds=3.0,
        thumbnail_count=1,
        transcriber=_raising,
    )
    assert result.thumbnails
    assert any("connection reset" in warning for warning in result.warnings)


@requires_ffmpeg
def test_failed_audio_extraction_still_yields_thumbnails(tmp_path, monkeypatch):
    """Losing the audio track costs the transcript stages, not the visual ones."""
    import tools.video_pipeline as pipeline

    def _fail(*_args, **_kwargs):
        raise VideoPipelineError("audio extraction failed: disk is full")

    monkeypatch.setattr(pipeline, "extract_audio", _fail)
    video = _make_video(tmp_path / "clip.mp4")
    result = run_pipeline(
        str(video),
        output_dir=str(tmp_path / "out"),
        thumbnail_count=1,
        transcriber=_fake_transcriber,
        llm=_fake_llm,
    )
    assert result.stages == ["thumbnails"]
    assert result.audio_path is None
    assert any("disk is full" in warning for warning in result.warnings)


def test_a_timed_out_media_command_reports_failure_instead_of_raising(monkeypatch):
    """Timeouts flow through the same failure branch as any ffmpeg error."""
    import subprocess as sp

    import tools.video_pipeline as pipeline

    def _timeout(*_args, **_kwargs):
        raise sp.TimeoutExpired(cmd="ffmpeg", timeout=5)

    monkeypatch.setattr(sp, "run", _timeout)
    proc = pipeline._run(["ffmpeg", "-i", "x"], timeout=5)
    assert proc.returncode != 0
    assert "timed out" in proc.stderr


@requires_ffmpeg
def test_video_without_audio_track_warns_and_still_makes_thumbnails(tmp_path):
    silent = tmp_path / "silent.mp4"
    subprocess.run(
        [
            find_ffmpeg(), "-y", "-nostdin",
            "-f", "lavfi", "-i", "testsrc=size=320x240:rate=15:duration=5",
            "-pix_fmt", "yuv420p", str(silent),
        ],
        capture_output=True, check=True, timeout=120,
    )
    result = run_pipeline(
        str(silent),
        output_dir=str(tmp_path / "out"),
        thumbnail_count=1,
        transcriber=_fake_transcriber,
    )
    assert result.thumbnails
    assert any("no audio track" in warning for warning in result.warnings)


@requires_ffmpeg
def test_thumbnails_are_taken_from_the_interesting_half_of_the_video(tmp_path):
    """The point of scoring: dead air loses to a frame with something in it."""
    ffmpeg = find_ffmpeg()
    flat = tmp_path / "flat.mp4"
    busy = tmp_path / "busy.mp4"
    joined = tmp_path / "mixed.mp4"
    listing = tmp_path / "list.txt"

    subprocess.run(
        [ffmpeg, "-y", "-nostdin", "-f", "lavfi",
         "-i", "color=c=0x0d0d0d:s=320x240:r=15:d=6", "-pix_fmt", "yuv420p", str(flat)],
        capture_output=True, check=True, timeout=120,
    )
    subprocess.run(
        [ffmpeg, "-y", "-nostdin", "-f", "lavfi",
         "-i", "mandelbrot=s=320x240:r=15", "-t", "6", "-pix_fmt", "yuv420p", str(busy)],
        capture_output=True, check=True, timeout=120,
    )
    listing.write_text(f"file '{flat}'\nfile '{busy}'\n", encoding="utf-8")
    subprocess.run(
        [ffmpeg, "-y", "-nostdin", "-f", "concat", "-safe", "0",
         "-i", str(listing), "-c", "copy", str(joined)],
        capture_output=True, check=True, timeout=120,
    )

    result = run_pipeline(
        str(joined),
        stages=["thumbnails"],
        output_dir=str(tmp_path / "out"),
        thumbnail_count=3,
    )
    assert len(result.thumbnails) == 3
    # The first six seconds are a near-black flat colour; nothing there should win.
    assert all(thumbnail["time"] > 6.0 for thumbnail in result.thumbnails)


@requires_ffmpeg
def test_probe_rejects_a_file_that_is_not_media(tmp_path):
    junk = tmp_path / "notes.txt"
    junk.write_text("this is not a video", encoding="utf-8")
    with pytest.raises(VideoPipelineError):
        probe_video(str(junk))
