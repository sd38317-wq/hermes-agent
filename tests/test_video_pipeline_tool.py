"""Tests for the ``video_pipeline`` tool wrapper (``tools/video_pipeline_tool.py``).

Covers registry wiring, the service gate, argument validation and coercion, and
the failure envelope — the pipeline mechanics themselves live in
``tests/test_video_pipeline.py``.
"""

from __future__ import annotations

import json

import pytest

import tools.video_pipeline_tool as vpt
from tools.registry import registry
from tools.video_pipeline import (
    ALL_STAGES,
    DEFAULT_STAGES,
    PipelineResult,
    VideoPipelineError,
)
from toolsets import TOOLSETS


@pytest.fixture
def video_file(tmp_path):
    """A file that exists — the wrapper validates the path before probing."""
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"\x00" * 32)
    return path


# ── registry wiring ─────────────────────────────────────────────────────────


def test_tool_is_registered_and_exposed_by_its_toolset():
    """A registered tool that no toolset lists is invisible to every agent."""
    entry = registry.get_entry("video_pipeline")
    assert entry is not None
    assert entry.toolset in TOOLSETS
    assert "video_pipeline" in TOOLSETS[entry.toolset]["tools"]


def test_schema_declares_video_path_as_the_only_required_argument():
    parameters = vpt.VIDEO_PIPELINE_SCHEMA["parameters"]
    assert parameters["required"] == ["video_path"]
    assert set(parameters["properties"]["stages"]["items"]["enum"]) == set(ALL_STAGES)


def test_check_fn_follows_ffmpeg_availability(monkeypatch):
    """The tool is service-gated: no ffmpeg, no schema footprint."""
    monkeypatch.setattr(vpt, "find_ffmpeg", lambda: "/usr/bin/ffmpeg")
    monkeypatch.setattr(vpt, "find_ffprobe", lambda: "/usr/bin/ffprobe")
    assert vpt.check_video_pipeline_requirements() is True

    monkeypatch.setattr(vpt, "find_ffmpeg", lambda: None)
    assert vpt.check_video_pipeline_requirements() is False


# ── argument validation ─────────────────────────────────────────────────────


def test_missing_video_path_is_an_error():
    assert "error" in json.loads(vpt.video_pipeline(""))


def test_nonexistent_file_reports_the_resolved_path(tmp_path):
    result = json.loads(vpt.video_pipeline(str(tmp_path / "absent.mp4")))
    assert "not found" in result["error"]


def test_directory_argument_is_rejected(tmp_path):
    result = json.loads(vpt.video_pipeline(str(tmp_path)))
    assert "not a file" in result["error"]


def test_unknown_stage_names_are_rejected(video_file):
    result = json.loads(vpt.video_pipeline(str(video_file), stages=["subtitle"]))
    assert "stages must name" in result["error"]


def test_credential_files_are_refused_before_any_processing(tmp_path, monkeypatch):
    """ffmpeg and the STT provider must never be pointed at an auth store."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    auth = tmp_path / "auth.json"
    auth.write_text("{}", encoding="utf-8")

    def _explode(*_args, **_kwargs):  # pragma: no cover - must never be called
        raise AssertionError("the pipeline ran on a credential file")

    monkeypatch.setattr(vpt, "run_pipeline", _explode)
    result = json.loads(vpt.video_pipeline(str(auth)))
    assert "error" in result


# ── argument coercion ───────────────────────────────────────────────────────


def _capture(monkeypatch):
    """Replace run_pipeline with a recorder that returns an empty-but-valid run."""
    captured = {}

    def _fake(video_path, **kwargs):
        captured.update(kwargs)
        captured["video_path"] = video_path
        result = PipelineResult(output_dir="/tmp/out")
        result.stages.append("thumbnails")
        return result

    monkeypatch.setattr(vpt, "run_pipeline", _fake)
    return captured


def test_defaults_are_applied_when_arguments_are_omitted(video_file, monkeypatch):
    captured = _capture(monkeypatch)
    vpt.video_pipeline(str(video_file))
    assert captured["stages"] == list(DEFAULT_STAGES)
    assert captured["subtitle_formats"] == ["srt", "vtt"]
    assert captured["title_count"] == vpt.DEFAULT_TITLE_COUNT
    assert captured["thumbnail_count"] == vpt.DEFAULT_THUMBNAIL_COUNT
    assert captured["title_language"] is None


def test_out_of_range_counts_are_clamped_not_rejected(video_file, monkeypatch):
    captured = _capture(monkeypatch)
    vpt.video_pipeline(
        str(video_file), title_count=500, thumbnail_count=0, cue_seconds=0.1
    )
    assert captured["title_count"] == vpt.MAX_TITLE_COUNT
    assert captured["thumbnail_count"] == 1
    assert captured["cue_seconds"] == vpt.MIN_CUE_SECONDS


def test_unparseable_numbers_fall_back_to_defaults(video_file, monkeypatch):
    captured = _capture(monkeypatch)
    vpt.video_pipeline(str(video_file), title_count="lots", cue_seconds="soon")
    assert captured["title_count"] == vpt.DEFAULT_TITLE_COUNT
    assert captured["cue_seconds"] == vpt.DEFAULT_CUE_SECONDS


def test_comma_separated_strings_are_accepted_for_list_arguments(video_file, monkeypatch):
    """Models routinely send "a,b" where the schema asks for an array."""
    captured = _capture(monkeypatch)
    vpt.video_pipeline(str(video_file), stages="titles, thumbnails", subtitle_formats="vtt")
    assert captured["stages"] == ["titles", "thumbnails"]
    assert captured["subtitle_formats"] == ["vtt"]


def test_burn_is_opt_in_because_it_re_encodes_the_video(video_file, monkeypatch):
    """A default run must not spend an H.264 pass nobody asked for."""
    captured = _capture(monkeypatch)
    vpt.video_pipeline(str(video_file))
    assert "burn" not in captured["stages"]

    vpt.video_pipeline(str(video_file), stages=["burn"])
    assert captured["stages"] == ["burn"]


def test_caption_style_is_forwarded_only_as_a_mapping(video_file, monkeypatch):
    captured = _capture(monkeypatch)
    vpt.video_pipeline(str(video_file), caption_style={"preset": "yellow", "font_scale": 0.06})
    assert captured["caption_style"] == {"preset": "yellow", "font_scale": 0.06}

    vpt.video_pipeline(str(video_file), caption_style="yellow please")
    assert captured["caption_style"] is None


def test_stage_order_is_normalised_to_pipeline_order(video_file, monkeypatch):
    captured = _capture(monkeypatch)
    vpt.video_pipeline(str(video_file), stages=["thumbnails", "audio"])
    assert captured["stages"] == ["audio", "thumbnails"]


# ── result envelope ─────────────────────────────────────────────────────────


def test_successful_run_returns_paths_and_stages(video_file, monkeypatch):
    result = PipelineResult(output_dir="/tmp/out", video={"duration": 12.0})
    result.stages.extend(["audio", "subtitles"])
    result.subtitles = {"srt": "/tmp/out/subtitles.srt", "cue_count": 4}
    result.warnings.append("scene detection failed")
    monkeypatch.setattr(vpt, "run_pipeline", lambda *_a, **_k: result)

    payload = json.loads(vpt.video_pipeline(str(video_file)))
    assert payload["success"] is True
    assert payload["stages"] == ["audio", "subtitles"]
    assert payload["subtitles"]["cue_count"] == 4
    assert payload["warnings"] == ["scene detection failed"]


def test_a_run_that_produced_nothing_is_reported_as_an_error(video_file, monkeypatch):
    """An empty output directory must not come back as success."""
    empty = PipelineResult(output_dir="/tmp/out")
    empty.warnings.append("the video has no audio track")
    monkeypatch.setattr(vpt, "run_pipeline", lambda *_a, **_k: empty)

    payload = json.loads(vpt.video_pipeline(str(video_file)))
    assert "error" in payload
    assert payload["details"] == ["the video has no audio track"]


def test_pipeline_errors_become_tool_errors(video_file, monkeypatch):
    def _raise(*_args, **_kwargs):
        raise VideoPipelineError("ffprobe could not read the file")

    monkeypatch.setattr(vpt, "run_pipeline", _raise)
    assert "ffprobe" in json.loads(vpt.video_pipeline(str(video_file)))["error"]


def test_handler_returns_a_json_string(video_file, monkeypatch):
    """Every registered handler must hand the registry a JSON string."""
    _capture(monkeypatch)
    raw = vpt._handle_video_pipeline({"video_path": str(video_file)})
    assert isinstance(raw, str)
    assert isinstance(json.loads(raw), dict)
