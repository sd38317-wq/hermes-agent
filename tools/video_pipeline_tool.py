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
from tools.video_captions import (
    DEFAULT_FONT_SCALE,
    DEFAULT_TITLE_FONT_SCALE,
    MAX_FONT_SCALE,
    MIN_FONT_SCALE,
    POSITIONS,
    PRESETS,
)
from tools.video_pipeline import (
    ALL_STAGES,
    DEFAULT_CUE_SECONDS,
    DEFAULT_THUMBNAIL_COUNT,
    DEFAULT_TITLE_COUNT,
    MAX_CUE_SECONDS,
    MAX_THUMBNAIL_COUNT,
    MAX_TITLE_COUNT,
    DEFAULT_STAGES,
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
        "The `burn` stage renders styled captions into the picture (hard subs) "
        "and writes an editable .ass alongside — that is what short-form "
        "platforms need, since a sidecar .srt displays nowhere on Shorts, "
        "Reels or TikTok. It re-encodes the video, so it is opt-in: pass it in "
        "`stages` and style it with `caption_style`.\n"
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
                    "Which stages to run. Defaults to everything except "
                    "`burn` (which re-encodes the video). `titles` and `burn` "
                    "both imply transcription, so either works without asking "
                    "for `subtitles`."
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
            "script": {
                "type": "string",
                "description": (
                    "The video's script, if you already have one — scripted "
                    "content (an AI short, a narrated explainer, an ad) has "
                    "exact words that do not need guessing. The recognizer is "
                    "still used for timing, but each cue's text is taken from "
                    "the script, so spelling, spacing and names are the "
                    "author's. Pass the text itself or a path to a .txt file. "
                    "Cues the script does not cover keep their recognized text."
                ),
            },
            "subtitles_path": {
                "type": "string",
                "description": (
                    "Path to an existing .srt/.vtt to use instead of "
                    "transcribing — the way to re-burn after fixing wording by "
                    "hand. Its timings and text are used as-is: no "
                    "speech-to-text, no cleanup, no cost."
                ),
            },
            "clean_captions": {
                "type": "boolean",
                "description": (
                    "Correct the recognizer's spacing, misheard words and "
                    "punctuation with the `caption_cleanup` auxiliary model "
                    "before writing the cues (default true). Corrections are "
                    "matched cue by cue and rejected if they drift from what "
                    "was heard, so timings never move and nothing is invented. "
                    "Set false to keep the raw transcript."
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
            "title_overlay": {
                "type": "object",
                "description": (
                    "Burn a hook title over the video (used by the `burn` "
                    "stage). Omit for no title. With `text` left out it uses "
                    "the best candidate from the `titles` stage, so \"title it "
                    "and burn it on\" is one call."
                ),
                "properties": {
                    "text": {
                        "type": "string",
                        "description": (
                            "Title to display. Defaults to the first generated "
                            "title candidate."
                        ),
                    },
                    "duration": {
                        "type": "number",
                        "description": (
                            "Seconds the title stays up. Omit to keep it on "
                            "screen for the whole video, which is how "
                            "short-form hooks are usually cut."
                        ),
                    },
                    "start": {
                        "type": "number",
                        "description": "When the title appears (default 0).",
                    },
                    "font": {"type": "string", "description": "Font family or file path."},
                    "font_scale": {
                        "type": "number",
                        "description": (
                            "Title height as a fraction of the video height "
                            f"(default {DEFAULT_TITLE_FONT_SCALE}) — bigger "
                            "than the captions by default."
                        ),
                    },
                    "font_size": {"type": "integer", "description": "Absolute size in pixels."},
                    "primary_color": {"type": "string", "description": "#RRGGBB."},
                    "outline_color": {"type": "string", "description": "#RRGGBB."},
                    "position": {
                        "type": "string",
                        "enum": list(POSITIONS),
                        "description": "Default top, so it clears the captions.",
                    },
                    "margin_scale": {
                        "type": "number",
                        "description": "Distance from that edge, as a fraction of height.",
                    },
                    "box": {"type": "boolean", "description": "Translucent plate behind the title."},
                    "max_lines": {"type": "integer", "description": "Maximum lines, 1-4."},
                },
            },
            "caption_style": {
                "type": "object",
                "description": (
                    "How burned-in captions look (only used by the `burn` "
                    "stage). Sizes are fractions of the video height, so one "
                    "style renders the same on a vertical Short and a "
                    "landscape export."
                ),
                "properties": {
                    "preset": {
                        "type": "string",
                        "enum": sorted(PRESETS),
                        "description": (
                            "Starting look, overridden by any field below. "
                            "`shorts` = big bold white with a heavy outline; "
                            "`yellow` = the same in yellow; `minimal` = small "
                            "and low; `boxed` = on a translucent plate."
                        ),
                    },
                    "font": {
                        "type": "string",
                        "description": (
                            "Font family name (e.g. 'Pretendard', 'Noto Sans KR') "
                            "or a path to a .ttf/.otf file. Defaults to the best "
                            "installed font that can draw the transcript's "
                            "script — Korean text needs a Korean font or it "
                            "renders as boxes."
                        ),
                    },
                    "font_scale": {
                        "type": "number",
                        "description": (
                            f"Caption height as a fraction of the video height "
                            f"({MIN_FONT_SCALE}-{MAX_FONT_SCALE}, default "
                            f"{DEFAULT_FONT_SCALE}). 0.05 is typical for "
                            "short-form."
                        ),
                    },
                    "font_size": {
                        "type": "integer",
                        "description": "Absolute size in pixels; overrides font_scale.",
                    },
                    "primary_color": {
                        "type": "string",
                        "description": "Text colour as #RRGGBB (default #FFFFFF).",
                    },
                    "outline_color": {
                        "type": "string",
                        "description": "Outline colour as #RRGGBB (default #000000).",
                    },
                    "position": {
                        "type": "string",
                        "enum": list(POSITIONS),
                        "description": "Vertical placement (default bottom).",
                    },
                    "margin_scale": {
                        "type": "number",
                        "description": (
                            "Distance from that edge as a fraction of height "
                            "(default 0.16 — clears the platform's own UI)."
                        ),
                    },
                    "max_lines": {
                        "type": "integer",
                        "description": "Maximum lines per cue, 1-4 (default 2).",
                    },
                    "box": {
                        "type": "boolean",
                        "description": "Draw a translucent plate behind the text.",
                    },
                    "uppercase": {
                        "type": "boolean",
                        "description": "Upper-case the text (Latin scripts only).",
                    },
                },
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


SCRIPT_FILE_SUFFIXES = (".txt", ".md", ".srt", ".vtt", ".text")


def _looks_like_script_path(value: str) -> bool:
    """True when ``script`` is a filename rather than the script itself.

    The script is usually passed inline, and a multi-line block of dialogue is
    not a path — asking the filesystem about one raises "File name too long"
    rather than answering False.
    """
    text = value.strip()
    if not text or "\n" in text or len(text) > 255:
        return False
    return text.lower().endswith(SCRIPT_FILE_SUFFIXES) or "/" in text or "\\" in text


def _coerce_stages(raw: Any) -> Optional[List[str]]:
    """Normalize the ``stages`` argument, or None when it names nothing valid."""
    if raw is None:
        return list(DEFAULT_STAGES)
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
    caption_style: Any = None,
    title_overlay: Any = None,
    clean_captions: Any = None,
    script: Any = None,
    subtitles_path: Any = None,
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

    script_text = script if isinstance(script, str) else None
    if script_text and _looks_like_script_path(script_text):
        candidate = _resolve_video_path(script_text)
        try:
            is_file = candidate.is_file()
        except OSError:
            is_file = False
        if is_file:
            try:
                script_text = candidate.read_text(encoding="utf-8-sig")
            except (OSError, UnicodeDecodeError) as exc:
                return tool_error(f"could not read the script file: {exc}")

    try:
        result = run_pipeline(
            str(resolved),
            stages=selected_stages,
            output_dir=output_dir,
            cue_seconds=_coerce_float(cue_seconds, DEFAULT_CUE_SECONDS, MIN_CUE_SECONDS, MAX_CUE_SECONDS),
            subtitle_formats=formats,
            clean_captions=clean_captions is not False,
            script=script_text,
            subtitles_path=(subtitles_path or "").strip() or None,
            title_count=_coerce_int(title_count, DEFAULT_TITLE_COUNT, 1, MAX_TITLE_COUNT),
            title_language=(title_language or "").strip() or None,
            thumbnail_count=_coerce_int(
                thumbnail_count, DEFAULT_THUMBNAIL_COUNT, 1, MAX_THUMBNAIL_COUNT
            ),
            caption_style=caption_style if isinstance(caption_style, dict) else None,
            title_overlay=title_overlay if isinstance(title_overlay, (dict, bool)) else None,
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
        caption_style=args.get("caption_style"),
        title_overlay=args.get("title_overlay"),
        clean_captions=args.get("clean_captions"),
        script=args.get("script"),
        subtitles_path=args.get("subtitles_path"),
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
