#!/usr/bin/env python3
"""
Video Pipeline Tool
===================

Model-facing wrapper around :mod:`tools.video_pipeline`: one ``video_pipeline``
call turns a local video file into subtitles, title candidates and ranked
thumbnail stills.

The tool is service-gated (``check_fn``) — it only appears in the schema when
ffmpeg and ffprobe are actually installed, and it lives in its own opt-in
``video_pipeline`` toolset, so sessions that never touch video pay nothing for
it. Everything it does runs locally except transcription, which goes through
the STT provider the user already configured in ``hermes tools`` → Speech-to-Text,
and the title call, which uses the ``video_titles`` auxiliary model.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from tools.registry import registry, tool_error
from tools.video_pipeline import (
    ALL_STAGES,
    DEFAULT_CUE_SECONDS,
    DEFAULT_THUMBNAIL_COUNT,
    DEFAULT_TITLE_COUNT,
    MAX_CUE_SECONDS,
    MAX_THUMBNAIL_COUNT,
    MAX_TITLE_COUNT,
    MIN_CUE_SECONDS,
    VideoPipelineError,
    find_ffmpeg,
    find_ffprobe,
    run_pipeline,
)

logger = logging.getLogger(__name__)

SUBTITLE_FORMATS = ("srt", "vtt")


VIDEO_PIPELINE_SCHEMA: Dict[str, Any] = {
    "name": "video_pipeline",
    "description": (
        "Process a local video file into publishing assets: extract the audio, "
        "transcribe it into timed subtitles (.srt/.vtt), draft hook-style title "
        "candidates from the transcript, and pick thumbnail frames ranked by "
        "sharpness, contrast, colour and exposure.\n"
        "\n"
        "Runs locally via ffmpeg; transcription uses the configured "
        "speech-to-text provider and titles use the `video_titles` auxiliary "
        "model. Files are written to an output directory and the result returns "
        "paths plus a transcript preview — read the files when you need the full "
        "text.\n"
        "\n"
        "Use `stages` to run only part of the work (e.g. thumbnails only for a "
        "video you already have subtitles for). Subtitles are transcribed one "
        "cue window at a time, so a long video means many STT requests — raise "
        "`cue_seconds` to trade subtitle granularity for fewer requests."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "video_path": {
                "type": "string",
                "description": (
                    "Path to a video file on this machine. Relative paths "
                    "resolve against the working directory."
                ),
            },
            "stages": {
                "type": "array",
                "items": {"type": "string", "enum": list(ALL_STAGES)},
                "description": (
                    "Which stages to run. Defaults to all of them. "
                    "`titles` implies transcription, so it works even when "
                    "`subtitles` is not requested (it just writes no subtitle "
                    "files)."
                ),
            },
            "output_dir": {
                "type": "string",
                "description": (
                    "Directory for the generated files. Defaults to a new "
                    "per-video folder under the Hermes home directory."
                ),
            },
            "cue_seconds": {
                "type": "number",
                "description": (
                    f"Target subtitle cue length in seconds "
                    f"({MIN_CUE_SECONDS:g}-{MAX_CUE_SECONDS:g}, default "
                    f"{DEFAULT_CUE_SECONDS:g}). Cues are cut at silence, so this "
                    "is a target, not an exact length. Higher values mean "
                    "proportionally fewer speech-to-text requests."
                ),
            },
            "subtitle_formats": {
                "type": "array",
                "items": {"type": "string", "enum": list(SUBTITLE_FORMATS)},
                "description": "Subtitle files to write. Defaults to both.",
            },
            "title_count": {
                "type": "integer",
                "description": (
                    f"How many title candidates to draft (1-{MAX_TITLE_COUNT}, "
                    f"default {DEFAULT_TITLE_COUNT})."
                ),
            },
            "title_language": {
                "type": "string",
                "description": (
                    "Language to write the titles in (e.g. 'Korean', 'English'). "
                    "Defaults to the language of the transcript."
                ),
            },
            "thumbnail_count": {
                "type": "integer",
                "description": (
                    f"How many thumbnail stills to save (1-{MAX_THUMBNAIL_COUNT}, "
                    f"default {DEFAULT_THUMBNAIL_COUNT}). Picks are spread across "
                    "the video so they are not all from one shot."
                ),
            },
        },
        "required": ["video_path"],
    },
}


def check_video_pipeline_requirements() -> bool:
    """The tool is only offered when ffmpeg and ffprobe are installed."""
    return bool(find_ffmpeg() and find_ffprobe())


def _resolve_video_path(raw: str) -> Path:
    """Expand ``~`` and anchor relative paths to the session working directory."""
    candidate = Path(os.path.expanduser(raw.strip()))
    if candidate.is_absolute():
        return candidate
    try:
        from agent.runtime_cwd import resolve_agent_cwd

        return resolve_agent_cwd() / candidate
    except Exception:  # pragma: no cover - defensive
        return Path.cwd() / candidate


def _coerce_stages(raw: Any) -> Optional[List[str]]:
    """Normalize the ``stages`` argument, or None when it names nothing valid."""
    if raw is None:
        return list(ALL_STAGES)
    if isinstance(raw, str):
        raw = [part.strip() for part in raw.split(",")]
    if not isinstance(raw, (list, tuple)):
        return None
    stages = [str(item).strip().lower() for item in raw if str(item).strip()]
    selected = [stage for stage in ALL_STAGES if stage in stages]
    return selected or None


def _coerce_int(raw: Any, default: int, low: int, high: int) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, value))


def _coerce_float(raw: Any, default: float, low: float, high: float) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, value))


def video_pipeline(
    video_path: str,
    stages: Any = None,
    output_dir: Optional[str] = None,
    cue_seconds: Any = None,
    subtitle_formats: Any = None,
    title_count: Any = None,
    title_language: Optional[str] = None,
    thumbnail_count: Any = None,
) -> str:
    """Run the video pipeline and return its JSON result."""
    if not isinstance(video_path, str) or not video_path.strip():
        return tool_error("video_path is required")

    resolved = _resolve_video_path(video_path)

    # Never hand a credential store to ffmpeg/an STT provider. Mirrors the
    # guards on image-gen, video-gen and transcription.
    try:
        from agent.file_safety import get_read_block_error

        blocked = get_read_block_error(str(resolved))
        if blocked:
            return tool_error(blocked)
    except ImportError:  # pragma: no cover - defensive
        pass

    if not resolved.exists():
        return tool_error(f"video file not found: {resolved}")
    if not resolved.is_file():
        return tool_error(f"not a file: {resolved}")

    selected_stages = _coerce_stages(stages)
    if selected_stages is None:
        return tool_error(f"stages must name at least one of: {', '.join(ALL_STAGES)}")

    formats = subtitle_formats
    if isinstance(formats, str):
        formats = [part.strip() for part in formats.split(",")]
    if isinstance(formats, (list, tuple)):
        formats = [str(item).strip().lower() for item in formats]
        formats = [fmt for fmt in SUBTITLE_FORMATS if fmt in formats]
    if not formats:
        formats = list(SUBTITLE_FORMATS)

    try:
        result = run_pipeline(
            str(resolved),
            stages=selected_stages,
            output_dir=output_dir,
            cue_seconds=_coerce_float(cue_seconds, DEFAULT_CUE_SECONDS, MIN_CUE_SECONDS, MAX_CUE_SECONDS),
            subtitle_formats=formats,
            title_count=_coerce_int(title_count, DEFAULT_TITLE_COUNT, 1, MAX_TITLE_COUNT),
            title_language=(title_language or "").strip() or None,
            thumbnail_count=_coerce_int(
                thumbnail_count, DEFAULT_THUMBNAIL_COUNT, 1, MAX_THUMBNAIL_COUNT
            ),
        )
    except VideoPipelineError as exc:
        return tool_error(str(exc))
    except OSError as exc:
        return tool_error(f"video pipeline could not write its output: {exc}")

    payload = result.as_dict()
    if not result.stages:
        # Every stage was skipped — report that as a failure so the model does
        # not read "success" over an empty output directory.
        return tool_error(
            "the video pipeline produced no assets",
            details=payload.get("warnings") or [],
            output_dir=payload.get("output_dir"),
        )
    return json.dumps(payload, ensure_ascii=False)


def _handle_video_pipeline(args: Dict[str, Any], **_kwargs: Any) -> str:
    return video_pipeline(
        video_path=args.get("video_path", ""),
        stages=args.get("stages"),
        output_dir=args.get("output_dir"),
        cue_seconds=args.get("cue_seconds"),
        subtitle_formats=args.get("subtitle_formats"),
        title_count=args.get("title_count"),
        title_language=args.get("title_language"),
        thumbnail_count=args.get("thumbnail_count"),
    )


registry.register(
    name="video_pipeline",
    toolset="video_pipeline",
    schema=VIDEO_PIPELINE_SCHEMA,
    handler=_handle_video_pipeline,
    check_fn=check_video_pipeline_requirements,
    requires_env=[],
    is_async=False,
    emoji="🎞️",
)
