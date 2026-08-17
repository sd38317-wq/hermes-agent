#!/usr/bin/env python3
"""
Video Auto-Processing Pipeline (core)
=====================================

Turns one local video file into the assets a publisher actually needs:

    video → audio (ffmpeg)
          → timed transcript → subtitles (.srt / .vtt)
          → hook-style title candidates (auxiliary LLM)
          → thumbnail stills, ranked by image quality

This module holds the mechanics. ``tools/video_pipeline_tool.py`` is the thin
model-facing wrapper (schema, argument validation, registry entry).

Design notes
------------

**Timing comes from the cut, not from the provider.** Hermes' STT surface
(:func:`tools.transcription_tools.transcribe_audio`) is deliberately
provider-agnostic and returns *text only* — there is no timestamp channel that
every backend (faster-whisper, a local shell command, Groq, OpenAI, Mistral,
xAI, ElevenLabs, DeepInfra, or any plugin-registered provider) can fill. Rather
than reimplementing per-provider ``verbose_json`` parsing for the subset that
supports it, the pipeline slices the audio into cue-sized windows at *silence
boundaries* first and transcribes each window separately. The cue's timing is
then exact by construction — it is the window we cut — and every STT backend
works, including ones that ship after this file.

The cost is one STT request per cue window. ``cue_seconds`` is the dial: 8s
(default) gives natural subtitle cues; 20-30s cuts request count ~3x at the
price of long on-screen lines. Silence-only stretches are never sent at all,
which is both cheaper and the standard defence against whisper-family
hallucination on empty audio.

**Thumbnail scoring is stdlib-only.** Hermes core has no Pillow/numpy/OpenCV
dependency and is not going to grow one for this. ffmpeg does the decoding and
hands back a tiny raw RGB buffer (64x64 by default); sharpness, contrast,
colorfulness and exposure are computed over those few thousand bytes in plain
Python. That keeps the whole feature dependency-free and makes
:func:`score_frame` a pure function that unit tests can drive with synthetic
buffers.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import shutil
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

STAGE_AUDIO = "audio"
STAGE_SUBTITLES = "subtitles"
STAGE_TITLES = "titles"
STAGE_THUMBNAILS = "thumbnails"
STAGE_BURN = "burn"
ALL_STAGES = (STAGE_AUDIO, STAGE_SUBTITLES, STAGE_TITLES, STAGE_THUMBNAILS, STAGE_BURN)
# Burn re-encodes the video, so it is not part of the default set — a caller
# that just wants subtitle files should not pay an H.264 pass for them.
DEFAULT_STAGES = (STAGE_AUDIO, STAGE_SUBTITLES, STAGE_TITLES, STAGE_THUMBNAILS)

# Audio handed to STT: 16 kHz mono is what whisper-family models resample to
# anyway, so anything richer is bytes we upload and pay for twice.
AUDIO_SAMPLE_RATE = 16000

DEFAULT_CUE_SECONDS = 8.0
MIN_CUE_SECONDS = 2.0
MAX_CUE_SECONDS = 30.0
# Hard ceiling on a single cue window, independent of the target: a speech run
# with no pause in it still has to be chopped somewhere.
CUE_HARD_MAX_FACTOR = 1.75
# Speech shorter than this is a click or a breath, not a cue.
MIN_SPEECH_SECONDS = 0.35
# Silence shorter than this is a comma, not a cue boundary.
MIN_SILENCE_SECONDS = 0.30
# Cue windows are padded outward so consonants at the edges aren't clipped.
CUE_PAD_SECONDS = 0.12
# Two cues separated by less than this and short enough to merge become one.
CUE_MERGE_GAP_SECONDS = 0.6

# Silence threshold. A fixed absolute floor mis-reads quiet material: a phone
# recording averaging -48 dB sits entirely under a -32 dB floor, so most of the
# dialogue is classified as silence and never transcribed (measured: 12.7s of
# speech covered dropped to 5.0s on the same audio attenuated by 25 dB). The
# floor is therefore derived from the material's own mean level, and the fixed
# value is only the fallback for when the measurement fails.
SILENCE_NOISE_DB = -32
SILENCE_FLOOR_OFFSET_DB = 12
SILENCE_FLOOR_MIN_DB = -60
SILENCE_FLOOR_MAX_DB = -25
DEFAULT_STT_CONCURRENCY = 2
MAX_STT_CONCURRENCY = 8
# Above this many cue windows the run is worth flagging: one request each adds
# up on a metered provider.
LOUD_REQUEST_COUNT = 200

DEFAULT_TITLE_COUNT = 5
MAX_TITLE_COUNT = 12
# Transcript budget for the title call: the opening states the topic and the
# close states the payoff; the middle is where a long video repeats itself.
TITLE_HEAD_CHARS = 4000
TITLE_TAIL_CHARS = 2000
TITLE_TASK = "video_titles"

DEFAULT_THUMBNAIL_COUNT = 3
MAX_THUMBNAIL_COUNT = 10
# Candidate frames scored per run. Each costs one cheap ffmpeg seek+decode.
MAX_THUMBNAIL_CANDIDATES = 40
THUMBNAIL_CANDIDATES_PER_PICK = 5
MIN_THUMBNAIL_CANDIDATES = 12
# Scoring resolution. 64x64 keeps the Python scoring loop at ~4k pixels while
# still resolving enough detail for a Laplacian to mean something.
SCORE_FRAME_SIZE = 64
# Frames closer together than this are near-duplicates of the same shot.
MIN_THUMBNAIL_GAP_FRACTION = 0.03
MIN_THUMBNAIL_GAP_SECONDS = 1.5
# Scene detection decodes keyframes only (``-skip_frame nokey``), so it stays
# cheap on long videos; the threshold is ffmpeg's 0-1 scene score.
SCENE_CHANGE_THRESHOLD = 0.35

# Subprocess timeouts. Media work is slow, so these are generous floors that
# scale with the material rather than fixed values that fail on long videos.
PROBE_TIMEOUT = 60
FRAME_TIMEOUT = 60
MIN_FFMPEG_TIMEOUT = 120
FFMPEG_TIMEOUT_PER_SECOND = 2.0
MAX_FFMPEG_TIMEOUT = 3600

class VideoPipelineError(Exception):
    """A stage failed in a way that makes the rest of the run meaningless."""


@dataclass
class Cue:
    """One subtitle cue: a window of audio and the text transcribed from it."""

    start: float
    end: float
    text: str

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


@dataclass
class FrameScore:
    """Quality metrics for one candidate thumbnail frame."""

    time: float
    score: float
    sharpness: float
    contrast: float
    colorfulness: float
    exposure: float
    brightness: float
    flat: bool = False

    def as_dict(self) -> Dict[str, Any]:
        return {
            "time": round(self.time, 3),
            "score": round(self.score, 4),
            "metrics": {
                "sharpness": round(self.sharpness, 4),
                "contrast": round(self.contrast, 4),
                "colorfulness": round(self.colorfulness, 4),
                "exposure": round(self.exposure, 4),
                "brightness": round(self.brightness, 4),
            },
        }


@dataclass
class PipelineResult:
    """Everything a run produced, ready to be JSON-serialized by the tool."""

    output_dir: str
    stages: List[str] = field(default_factory=list)
    video: Dict[str, Any] = field(default_factory=dict)
    audio_path: Optional[str] = None
    transcript_path: Optional[str] = None
    transcript_chars: int = 0
    transcript_preview: str = ""
    subtitles: Dict[str, Any] = field(default_factory=dict)
    captions: Dict[str, Any] = field(default_factory=dict)
    titles: List[str] = field(default_factory=list)
    thumbnails: List[Dict[str, Any]] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "success": True,
            "output_dir": self.output_dir,
            "stages": list(self.stages),
            "video": self.video,
        }
        if self.audio_path:
            payload["audio_path"] = self.audio_path
        if self.transcript_path:
            payload["transcript"] = {
                "path": self.transcript_path,
                "chars": self.transcript_chars,
                "preview": self.transcript_preview,
            }
        if self.subtitles:
            payload["subtitles"] = self.subtitles
        if self.captions:
            payload["captions"] = self.captions
        if self.titles:
            payload["titles"] = self.titles
        if self.thumbnails:
            payload["thumbnails"] = self.thumbnails
        if self.warnings:
            payload["warnings"] = self.warnings
        return payload


# ---------------------------------------------------------------------------
# Binaries
# ---------------------------------------------------------------------------


def find_ffmpeg() -> Optional[str]:
    """Locate ffmpeg, reusing the STT module's Homebrew-aware lookup."""
    return _find_media_binary("ffmpeg")


def find_ffprobe() -> Optional[str]:
    """Locate ffprobe (ships alongside ffmpeg in every distribution)."""
    return _find_media_binary("ffprobe")


def _find_media_binary(name: str) -> Optional[str]:
    try:
        from tools.transcription_tools import _find_binary

        return _find_binary(name)
    except Exception:  # pragma: no cover - defensive: STT module refactor
        return shutil.which(name)


def _hide_flags() -> int:
    """Windows ``creationflags`` that keep child consoles from flashing."""
    try:
        from hermes_cli._subprocess_compat import windows_hide_flags

        return windows_hide_flags()
    except Exception:  # pragma: no cover - defensive
        return 0


def _run(
    command: Sequence[str],
    *,
    timeout: float,
    binary_stdout: bool = False,
) -> subprocess.CompletedProcess:
    """Run a media subprocess with no shell, no stdin, and a hard timeout.

    A timeout is reported as a non-zero return like any other ffmpeg failure,
    so every caller's existing failure branch covers it — an optional step
    degrades, a required one raises.
    """
    try:
        return subprocess.run(
            list(command),
            capture_output=True,
            text=not binary_stdout,
            encoding=None if binary_stdout else "utf-8",
            errors=None if binary_stdout else "replace",
            timeout=timeout,
            stdin=subprocess.DEVNULL,
            creationflags=_hide_flags(),
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        logger.warning("media command timed out after %.0fs: %s", timeout, command[0])
        message = f"timed out after {timeout:.0f}s"
        return subprocess.CompletedProcess(
            args=list(command),
            returncode=124,
            stdout=(exc.stdout or (b"" if binary_stdout else "")),
            stderr=message.encode() if binary_stdout else message,
        )


def _ffmpeg_timeout(duration: float) -> float:
    """Timeout for a whole-file ffmpeg pass over *duration* seconds of media."""
    return min(MAX_FFMPEG_TIMEOUT, max(MIN_FFMPEG_TIMEOUT, duration * FFMPEG_TIMEOUT_PER_SECOND))


def _stderr_tail(proc: subprocess.CompletedProcess, limit: int = 400) -> str:
    """Last few hundred chars of ffmpeg's stderr — the part that names the error."""
    raw = proc.stderr
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    text = (raw or "").strip()
    return text[-limit:]


# ---------------------------------------------------------------------------
# Probe
# ---------------------------------------------------------------------------


def probe_video(video_path: str, ffprobe: Optional[str] = None) -> Dict[str, Any]:
    """Return duration/dimensions/stream facts for *video_path*.

    Raises :class:`VideoPipelineError` when ffprobe is missing or the file is
    not decodable media — every later stage depends on the duration, so this
    is the one place that fails the whole run.
    """
    ffprobe = ffprobe or find_ffprobe()
    if not ffprobe:
        raise VideoPipelineError("ffprobe not found — install ffmpeg to use the video pipeline")

    proc = _run(
        [
            ffprobe,
            "-v", "error",
            "-print_format", "json",
            "-show_format",
            "-show_streams",
            video_path,
        ],
        timeout=PROBE_TIMEOUT,
    )
    if proc.returncode != 0:
        raise VideoPipelineError(f"ffprobe could not read the file: {_stderr_tail(proc)}")
    try:
        data = json.loads(proc.stdout or "{}")
    except ValueError as exc:
        raise VideoPipelineError(f"ffprobe returned unparseable output: {exc}") from exc

    streams = data.get("streams") or []
    video_stream = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio_stream = next((s for s in streams if s.get("codec_type") == "audio"), None)

    duration = _coerce_float((data.get("format") or {}).get("duration"))
    if duration is None and video_stream is not None:
        duration = _coerce_float(video_stream.get("duration"))
    if not duration or duration <= 0:
        raise VideoPipelineError(
            "could not determine the video duration — the file may be truncated or not a video"
        )

    info: Dict[str, Any] = {
        "path": video_path,
        "duration": round(duration, 3),
        "has_video": video_stream is not None,
        "has_audio": audio_stream is not None,
    }
    if video_stream is not None:
        info["width"] = video_stream.get("width")
        info["height"] = video_stream.get("height")
        info["video_codec"] = video_stream.get("codec_name")
        fps = _parse_fraction(video_stream.get("avg_frame_rate"))
        if fps:
            info["fps"] = round(fps, 3)
    if audio_stream is not None:
        info["audio_codec"] = audio_stream.get("codec_name")
    return info


def _coerce_float(value: Any) -> Optional[float]:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _parse_fraction(value: Any) -> Optional[float]:
    """Parse ffprobe's ``"30000/1001"`` rational form."""
    if not isinstance(value, str) or "/" not in value:
        return _coerce_float(value)
    num, _, den = value.partition("/")
    numerator = _coerce_float(num)
    denominator = _coerce_float(den)
    if not numerator or not denominator:
        return None
    return numerator / denominator


# ---------------------------------------------------------------------------
# Stage 1 — audio extraction
# ---------------------------------------------------------------------------


def extract_audio(
    video_path: str,
    output_path: str,
    *,
    duration: float,
    ffmpeg: Optional[str] = None,
) -> str:
    """Extract a 16 kHz mono WAV from *video_path*. Returns *output_path*."""
    ffmpeg = ffmpeg or find_ffmpeg()
    if not ffmpeg:
        raise VideoPipelineError("ffmpeg not found — install ffmpeg to use the video pipeline")

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    proc = _run(
        [
            ffmpeg, "-y", "-nostdin",
            "-i", video_path,
            "-vn",
            "-ac", "1",
            "-ar", str(AUDIO_SAMPLE_RATE),
            "-c:a", "pcm_s16le",
            output_path,
        ],
        timeout=_ffmpeg_timeout(duration),
    )
    if proc.returncode != 0 or not Path(output_path).exists():
        raise VideoPipelineError(f"audio extraction failed: {_stderr_tail(proc)}")
    return output_path


# ---------------------------------------------------------------------------
# Stage 2 — cue windows and transcription
# ---------------------------------------------------------------------------


_SILENCE_START_RE = re.compile(r"silence_start:\s*(-?[\d.]+)")
_SILENCE_END_RE = re.compile(r"silence_end:\s*(-?[\d.]+)")
_MEAN_VOLUME_RE = re.compile(r"mean_volume:\s*(-?[\d.]+)\s*dB")


def measure_mean_volume(
    audio_path: str, duration: float, *, ffmpeg: Optional[str] = None
) -> Optional[float]:
    """Return the track's mean level in dBFS, or None when it can't be measured."""
    ffmpeg = ffmpeg or find_ffmpeg()
    if not ffmpeg:
        return None
    proc = _run(
        [ffmpeg, "-nostdin", "-i", audio_path, "-af", "volumedetect", "-f", "null", "-"],
        timeout=_ffmpeg_timeout(duration),
    )
    match = _MEAN_VOLUME_RE.search(proc.stderr or "")
    return _coerce_float(match.group(1)) if match else None


def adaptive_noise_floor(mean_db: Optional[float]) -> int:
    """Pick a silence threshold relative to the material's own mean level.

    Quiet material needs a lower floor or its dialogue reads as silence; loud,
    compressed material needs a higher one or its room tone reads as speech.
    The clamps keep the derived value inside the range where ``silencedetect``
    behaves, and an unmeasurable track falls back to the fixed default.
    """
    if mean_db is None:
        return SILENCE_NOISE_DB
    floor = mean_db - SILENCE_FLOOR_OFFSET_DB
    return int(round(max(SILENCE_FLOOR_MIN_DB, min(SILENCE_FLOOR_MAX_DB, floor))))


def detect_silences(
    audio_path: str,
    duration: float,
    *,
    ffmpeg: Optional[str] = None,
    noise_db: Optional[int] = None,
    min_silence: float = MIN_SILENCE_SECONDS,
) -> List[Tuple[float, float]]:
    """Return ``[(start, end)]`` silence spans, or ``[]`` when detection fails.

    ``noise_db`` defaults to a floor derived from the track's own mean level
    (see :func:`adaptive_noise_floor`); pass a value to pin it.

    Failure is not fatal: with no silence map the window planner falls back to
    fixed-length cuts, which is worse for phrasing but still produces valid
    subtitles.
    """
    ffmpeg = ffmpeg or find_ffmpeg()
    if not ffmpeg:
        return []
    if noise_db is None:
        noise_db = adaptive_noise_floor(
            measure_mean_volume(audio_path, duration, ffmpeg=ffmpeg)
        )
        logger.debug("silence floor for %s: %d dB", audio_path, noise_db)
    proc = _run(
        [
            ffmpeg, "-nostdin",
            "-i", audio_path,
            "-af", f"silencedetect=noise={noise_db}dB:d={min_silence}",
            "-f", "null", "-",
        ],
        timeout=_ffmpeg_timeout(duration),
    )
    return parse_silence_log(proc.stderr or "", duration)


def parse_silence_log(stderr: str, duration: float) -> List[Tuple[float, float]]:
    """Parse ffmpeg ``silencedetect`` stderr into ordered ``(start, end)`` spans."""
    spans: List[Tuple[float, float]] = []
    pending: Optional[float] = None
    for line in stderr.splitlines():
        start_match = _SILENCE_START_RE.search(line)
        if start_match:
            pending = max(0.0, float(start_match.group(1)))
            continue
        end_match = _SILENCE_END_RE.search(line)
        if end_match and pending is not None:
            end = min(duration, float(end_match.group(1)))
            if end > pending:
                spans.append((pending, end))
            pending = None
    # A file that fades out ends mid-silence: ffmpeg logs the start, never the end.
    if pending is not None and duration > pending:
        spans.append((pending, duration))
    spans.sort()
    return spans


def speech_regions(
    duration: float,
    silences: Sequence[Tuple[float, float]],
    *,
    min_speech: float = MIN_SPEECH_SECONDS,
) -> List[Tuple[float, float]]:
    """Complement of *silences* within ``[0, duration]``, minus slivers."""
    regions: List[Tuple[float, float]] = []
    cursor = 0.0
    for start, end in sorted(silences):
        start = max(0.0, min(start, duration))
        end = max(0.0, min(end, duration))
        if start > cursor:
            regions.append((cursor, start))
        cursor = max(cursor, end)
    if cursor < duration:
        regions.append((cursor, duration))
    return [(s, e) for s, e in regions if e - s >= min_speech]


def plan_cue_windows(
    duration: float,
    silences: Sequence[Tuple[float, float]],
    *,
    cue_seconds: float = DEFAULT_CUE_SECONDS,
    min_speech: float = MIN_SPEECH_SECONDS,
    pad: float = CUE_PAD_SECONDS,
) -> List[Tuple[float, float]]:
    """Split ``[0, duration]`` into cue-sized windows aligned to speech.

    Long speech runs are divided into equal parts so no cue exceeds
    ``cue_seconds * CUE_HARD_MAX_FACTOR``; short neighbouring runs separated by
    a brief pause are merged back together so a cue is a phrase rather than a
    word. Windows are then padded outward without crossing each other.
    """
    cue_seconds = max(MIN_CUE_SECONDS, min(MAX_CUE_SECONDS, cue_seconds))
    hard_max = cue_seconds * CUE_HARD_MAX_FACTOR

    regions = speech_regions(duration, silences, min_speech=min_speech)
    if not regions:
        if silences:
            # The detector found speech nowhere — the track is silence, music
            # under a threshold, or dead air. Transcribing it would spend one
            # request per window on nothing.
            return []
        # No silence map at all (detection failed, or wall-to-wall speech):
        # fall back to fixed cuts so the run still produces subtitles.
        regions = [(0.0, duration)]

    windows: List[Tuple[float, float]] = []
    for start, end in regions:
        span = end - start
        if span <= hard_max:
            windows.append((start, end))
            continue
        parts = max(1, int(math.ceil(span / cue_seconds)))
        step = span / parts
        for index in range(parts):
            windows.append((start + index * step, start + (index + 1) * step))

    merged = _merge_short_windows(windows, cue_seconds)
    return _pad_windows(merged, duration, pad)


def _merge_short_windows(
    windows: Sequence[Tuple[float, float]], cue_seconds: float
) -> List[Tuple[float, float]]:
    """Join adjacent short windows separated by a brief pause."""
    merged: List[Tuple[float, float]] = []
    for start, end in windows:
        if not merged:
            merged.append((start, end))
            continue
        prev_start, prev_end = merged[-1]
        gap = start - prev_end
        if gap <= CUE_MERGE_GAP_SECONDS and (end - prev_start) <= cue_seconds:
            merged[-1] = (prev_start, end)
        else:
            merged.append((start, end))
    return merged


def _pad_windows(
    windows: Sequence[Tuple[float, float]], duration: float, pad: float
) -> List[Tuple[float, float]]:
    """Pad each window outward, stopping at neighbours and the file bounds."""
    padded: List[Tuple[float, float]] = []
    for index, (start, end) in enumerate(windows):
        prev_end = windows[index - 1][1] if index > 0 else 0.0
        next_start = windows[index + 1][0] if index + 1 < len(windows) else duration
        new_start = max(0.0, prev_end, start - pad)
        new_end = min(duration, next_start, end + pad)
        if new_end - new_start > 0.01:
            padded.append((round(new_start, 3), round(new_end, 3)))
    return padded


def _slice_audio(
    ffmpeg: str, audio_path: str, start: float, end: float, out_path: str
) -> bool:
    """Cut ``[start, end)`` out of *audio_path* into *out_path*."""
    proc = _run(
        [
            ffmpeg, "-y", "-nostdin",
            "-ss", f"{start:.3f}",
            "-t", f"{max(0.05, end - start):.3f}",
            "-i", audio_path,
            "-ac", "1",
            "-ar", str(AUDIO_SAMPLE_RATE),
            "-c:a", "pcm_s16le",
            out_path,
        ],
        timeout=FRAME_TIMEOUT,
    )
    if proc.returncode != 0 or not Path(out_path).exists():
        logger.debug("cue slice %.2f-%.2f failed: %s", start, end, _stderr_tail(proc))
        return False
    return True


def _resolve_stt_concurrency(requested: Optional[int]) -> int:
    """Clamp the requested worker count, forcing 1 for local STT backends.

    faster-whisper serves every caller from one process-global model instance;
    running windows through it concurrently multiplies memory and contends for
    the same CPU threads with nothing to gain.
    """
    workers = DEFAULT_STT_CONCURRENCY if requested is None else int(requested)
    workers = max(1, min(MAX_STT_CONCURRENCY, workers))
    try:
        from tools.transcription_tools import (
            _get_provider,
            _is_local_stt_provider,
            _load_stt_config,
        )

        stt_config = _load_stt_config()
        if _is_local_stt_provider(_get_provider(stt_config), stt_config):
            return 1
    except Exception:  # pragma: no cover - defensive: STT module refactor
        logger.debug("could not resolve the STT provider for concurrency", exc_info=True)
    return workers


def transcribe_windows(
    audio_path: str,
    windows: Sequence[Tuple[float, float]],
    *,
    model: Optional[str] = None,
    concurrency: Optional[int] = None,
    ffmpeg: Optional[str] = None,
    transcriber=None,
) -> Tuple[List[Cue], List[str]]:
    """Transcribe each cue window. Returns ``(cues, warnings)``.

    *transcriber* defaults to :func:`tools.transcription_tools.transcribe_audio`
    and is injectable so tests can drive the planner/formatter path without an
    STT backend. Windows that fail or come back empty are dropped with a
    warning rather than failing the run — one bad slice should not cost the
    other 200 cues.
    """
    if not windows:
        return [], []

    ffmpeg = ffmpeg or find_ffmpeg()
    if not ffmpeg:
        raise VideoPipelineError("ffmpeg not found — install ffmpeg to use the video pipeline")

    if transcriber is None:
        from tools.transcription_tools import transcribe_audio as transcriber  # type: ignore

    workers = _resolve_stt_concurrency(concurrency)
    warnings: List[str] = []
    results: List[Optional[Cue]] = [None] * len(windows)

    work_dir = tempfile.mkdtemp(prefix="hermes-video-cues-")
    try:
        def _one(index: int) -> Optional[str]:
            start, end = windows[index]
            slice_path = os.path.join(work_dir, f"cue-{index:05d}.wav")
            if not _slice_audio(ffmpeg, audio_path, start, end, slice_path):
                return f"cue {index + 1} could not be cut from the audio"
            try:
                try:
                    response = transcriber(slice_path, model=model) or {}
                except Exception as exc:  # noqa: BLE001 - one window must not kill the run
                    logger.debug("cue %d transcription raised", index + 1, exc_info=True)
                    return f"cue {index + 1} ({start:.1f}s) raised: {exc}"
                if not response.get("success"):
                    return (
                        f"cue {index + 1} ({start:.1f}s) failed: "
                        f"{response.get('error') or 'unknown STT error'}"
                    )
                text = (response.get("transcript") or "").strip()
                if text:
                    results[index] = Cue(start=start, end=end, text=text)
                return None
            finally:
                try:
                    os.unlink(slice_path)
                except OSError:
                    pass

        if workers == 1:
            problems = [_one(i) for i in range(len(windows))]
        else:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                problems = list(pool.map(_one, range(len(windows))))
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)

    failures = [p for p in problems if p]
    if failures:
        # Keep the tool result bounded: report the first few verbatim and count
        # the rest. A 40-minute video can produce hundreds of these.
        warnings.extend(failures[:3])
        if len(failures) > 3:
            warnings.append(f"...and {len(failures) - 3} more cue windows failed")

    return [cue for cue in results if cue is not None], warnings


# ---------------------------------------------------------------------------
# Subtitle formatting
# ---------------------------------------------------------------------------


def format_timestamp(seconds: float, *, separator: str = ",") -> str:
    """Format seconds as ``HH:MM:SS,mmm`` (SRT) or ``HH:MM:SS.mmm`` (VTT)."""
    seconds = max(0.0, seconds)
    milliseconds = int(round(seconds * 1000))
    hours, milliseconds = divmod(milliseconds, 3_600_000)
    minutes, milliseconds = divmod(milliseconds, 60_000)
    secs, milliseconds = divmod(milliseconds, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}{separator}{milliseconds:03d}"


def format_srt(cues: Sequence[Cue]) -> str:
    """Render cues as SubRip (.srt)."""
    blocks: List[str] = []
    for index, cue in enumerate(cues, start=1):
        blocks.append(
            f"{index}\n"
            f"{format_timestamp(cue.start)} --> {format_timestamp(cue.end)}\n"
            f"{cue.text}\n"
        )
    return "\n".join(blocks)


def format_vtt(cues: Sequence[Cue]) -> str:
    """Render cues as WebVTT (.vtt)."""
    blocks = ["WEBVTT\n"]
    for cue in cues:
        blocks.append(
            f"{format_timestamp(cue.start, separator='.')} --> "
            f"{format_timestamp(cue.end, separator='.')}\n"
            f"{cue.text}\n"
        )
    return "\n".join(blocks)


def join_transcript(cues: Sequence[Cue]) -> str:
    """Flatten cues into one plain-text transcript."""
    return " ".join(cue.text.strip() for cue in cues if cue.text.strip()).strip()


# ---------------------------------------------------------------------------
# Stage 3 — title candidates
# ---------------------------------------------------------------------------


_TITLE_SYSTEM_PROMPT = """You write titles for published video.

You are given a transcript of a video. Return title candidates that would make
someone click without lying about what the video contains.

Rules:
- Write in the SAME LANGUAGE as the transcript unless told otherwise.
- Each title must be specific to THIS video — no generic filler that would fit
  any video on the topic.
- Lead with the concrete hook: the number, the claim, the surprise, the stake.
- No clickbait that the transcript does not support. No emoji. No hashtags.
- No surrounding quotes. Keep each title under 70 characters.
- Vary the angles across candidates (question, result, contrast, how-to).

Respond with JSON only: {"titles": ["...", "..."]}"""

_TITLE_RESPONSE_FORMAT = {"type": "json_object"}


def build_title_input(transcript: str) -> str:
    """Trim the transcript to the head+tail budget the title call is given."""
    transcript = transcript.strip()
    if len(transcript) <= TITLE_HEAD_CHARS + TITLE_TAIL_CHARS:
        return transcript
    head = transcript[:TITLE_HEAD_CHARS]
    tail = transcript[-TITLE_TAIL_CHARS:]
    return f"{head}\n\n[...transcript trimmed...]\n\n{tail}"


def parse_title_response(content: str, limit: int) -> List[str]:
    """Pull a clean title list out of the model's reply.

    Accepts the requested JSON object, a bare JSON array, or a plain numbered
    list — small models drop out of JSON mode often enough that refusing to
    parse prose would just throw away good titles.
    """
    text = (content or "").strip()
    if not text:
        return []
    fenced = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.DOTALL)
    if fenced:
        text = fenced.group(1).strip()

    candidates: List[Any] = []
    try:
        payload = json.loads(text)
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        for key in ("titles", "title_candidates", "candidates", "results"):
            value = payload.get(key)
            if isinstance(value, list):
                candidates = value
                break
    elif isinstance(payload, list):
        candidates = payload

    if not candidates:
        for line in text.splitlines():
            stripped = re.sub(r"^\s*(?:[-*•]|\d+[.)])\s*", "", line).strip()
            if stripped:
                candidates.append(stripped)

    titles: List[str] = []
    seen = set()
    for entry in candidates:
        if isinstance(entry, dict):
            entry = entry.get("title") or entry.get("text") or ""
        if not isinstance(entry, str):
            continue
        cleaned = entry.strip().strip('"').strip("'").strip()
        if not cleaned or cleaned.lower() in seen:
            continue
        seen.add(cleaned.lower())
        titles.append(cleaned)
        if len(titles) >= limit:
            break
    return titles


def generate_titles(
    transcript: str,
    *,
    count: int = DEFAULT_TITLE_COUNT,
    language: Optional[str] = None,
    timeout: Optional[float] = None,
    llm=None,
) -> Tuple[List[str], Optional[str]]:
    """Ask the ``video_titles`` auxiliary model for title candidates.

    Returns ``(titles, error)``. A failed title call is never fatal — the run
    still has subtitles and thumbnails to hand back.
    """
    transcript = (transcript or "").strip()
    if not transcript:
        return [], "no transcript text to title"

    count = max(1, min(MAX_TITLE_COUNT, int(count)))
    if llm is None:
        from agent.auxiliary_client import call_llm as llm  # type: ignore

    instruction = f"Return exactly {count} title candidates."
    if language:
        instruction += f" Write them in {language}."
    messages = [
        {"role": "system", "content": _TITLE_SYSTEM_PROMPT},
        {"role": "user", "content": f"{instruction}\n\nTranscript:\n{build_title_input(transcript)}"},
    ]

    try:
        response = llm(
            task=TITLE_TASK,
            messages=messages,
            max_tokens=600,
            temperature=0.8,
            timeout=timeout,
            extra_body={"response_format": _TITLE_RESPONSE_FORMAT},
        )
        content = response.choices[0].message.content or ""
    except Exception as exc:
        logger.warning("video title generation failed: %s", exc)
        logger.debug("video title generation traceback", exc_info=True)
        return [], f"title generation failed: {exc}"

    titles = parse_title_response(content, count)
    if not titles:
        return [], "the title model returned no usable candidates"
    return titles, None


# ---------------------------------------------------------------------------
# Stage 4 — thumbnails
# ---------------------------------------------------------------------------


_SHOWINFO_TIME_RE = re.compile(r"pts_time:([\d.]+)")


def detect_scene_changes(
    video_path: str,
    duration: float,
    *,
    ffmpeg: Optional[str] = None,
    threshold: float = SCENE_CHANGE_THRESHOLD,
) -> List[float]:
    """Return timestamps of keyframe scene changes (best-effort, may be empty).

    ``-skip_frame nokey`` decodes keyframes only, which keeps this cheap on
    long videos at the cost of missing cuts inside a GOP — acceptable for
    "where are the visually distinct moments" candidate generation.
    """
    ffmpeg = ffmpeg or find_ffmpeg()
    if not ffmpeg:
        return []
    proc = _run(
        [
            ffmpeg, "-nostdin",
            "-skip_frame", "nokey",
            "-i", video_path,
            "-an",
            "-vf", f"select='gt(scene,{threshold})',showinfo",
            "-vsync", "vfr",
            "-f", "null", "-",
        ],
        timeout=_ffmpeg_timeout(duration),
    )
    times: List[float] = []
    for match in _SHOWINFO_TIME_RE.finditer(proc.stderr or ""):
        value = _coerce_float(match.group(1))
        if value is not None and 0.0 <= value <= duration:
            times.append(value)
    return times


def candidate_times(
    duration: float,
    thumbnail_count: int,
    scene_times: Sequence[float] = (),
    *,
    max_candidates: int = MAX_THUMBNAIL_CANDIDATES,
) -> List[float]:
    """Build the list of timestamps to score.

    Evenly spaced samples guarantee coverage; scene-change times add the shots
    an even grid would walk straight past. The first and last few percent are
    excluded — that is where intros fade in and outros fade to black.
    """
    if duration <= 0:
        return []
    wanted = max(MIN_THUMBNAIL_CANDIDATES, thumbnail_count * THUMBNAIL_CANDIDATES_PER_PICK)
    wanted = min(wanted, max_candidates)

    lower, upper = duration * 0.04, duration * 0.96
    if upper <= lower:
        lower, upper = 0.0, duration
    step = (upper - lower) / max(1, wanted)
    times = [lower + step * (index + 0.5) for index in range(wanted)]
    times.extend(t for t in scene_times if lower <= t <= upper)

    deduped: List[float] = []
    for value in sorted(times):
        if not deduped or value - deduped[-1] >= 0.5:
            deduped.append(round(value, 3))
    if len(deduped) <= max_candidates:
        return deduped
    # Thin evenly rather than truncating, so the tail of the video keeps
    # representation when scene detection front-loads the candidates.
    stride = len(deduped) / max_candidates
    return [deduped[min(len(deduped) - 1, int(index * stride))] for index in range(max_candidates)]


def score_frame(pixels: bytes, width: int, height: int, *, time: float = 0.0) -> FrameScore:
    """Score one raw RGB24 frame for thumbnail suitability.

    Pure function over the buffer ffmpeg hands back — no image library. The
    four components are the ones that separate a usable still from an unusable
    one: is it in focus (Laplacian energy), does it have tonal range
    (luma spread), is it colourful (Hasler-Süsstrunk), and is it exposed
    somewhere near the middle rather than crushed or blown out.
    """
    expected = width * height * 3
    if width < 3 or height < 3 or len(pixels) < expected:
        return FrameScore(time, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, flat=True)

    gray = bytearray(width * height)
    rg_values: List[int] = []
    yb_values: List[int] = []
    luma_sum = 0
    luma_sq_sum = 0
    for index in range(width * height):
        base = index * 3
        red, green, blue = pixels[base], pixels[base + 1], pixels[base + 2]
        luma = (299 * red + 587 * green + 114 * blue) // 1000
        gray[index] = luma
        luma_sum += luma
        luma_sq_sum += luma * luma
        rg_values.append(red - green)
        yb_values.append((red + green) // 2 - blue)

    pixel_count = width * height
    mean_luma = luma_sum / pixel_count
    variance = max(0.0, luma_sq_sum / pixel_count - mean_luma * mean_luma)
    std_luma = math.sqrt(variance)

    laplacian_total = 0
    for row in range(1, height - 1):
        row_base = row * width
        for column in range(1, width - 1):
            center = row_base + column
            laplacian_total += abs(
                4 * gray[center]
                - gray[center - 1]
                - gray[center + 1]
                - gray[center - width]
                - gray[center + width]
            )
    interior = (width - 2) * (height - 2)
    laplacian_mean = laplacian_total / interior if interior else 0.0

    sharpness = min(1.0, laplacian_mean / 32.0)
    contrast = min(1.0, std_luma / 64.0)
    colorfulness = min(1.0, _colorfulness(rg_values, yb_values) / 110.0)
    brightness = mean_luma / 255.0
    exposure = max(0.0, 1.0 - (abs(brightness - 0.5) / 0.5) ** 2)

    # A frame that is essentially one flat colour — the black between shots,
    # a white flash — scores zero however "well exposed" the average is.
    flat = std_luma < 6.0 or mean_luma < 12 or mean_luma > 243
    if flat:
        return FrameScore(time, 0.0, sharpness, contrast, colorfulness, exposure, brightness, flat=True)

    score = 0.40 * sharpness + 0.25 * contrast + 0.20 * colorfulness + 0.15 * exposure
    return FrameScore(time, score, sharpness, contrast, colorfulness, exposure, brightness)


def _colorfulness(rg_values: Sequence[int], yb_values: Sequence[int]) -> float:
    """Hasler-Süsstrunk colourfulness metric (0 ≈ greyscale, ~110 ≈ vivid)."""
    count = len(rg_values)
    if not count:
        return 0.0
    rg_mean = sum(rg_values) / count
    yb_mean = sum(yb_values) / count
    rg_var = sum((value - rg_mean) ** 2 for value in rg_values) / count
    yb_var = sum((value - yb_mean) ** 2 for value in yb_values) / count
    std_root = math.sqrt(rg_var + yb_var)
    mean_root = math.sqrt(rg_mean * rg_mean + yb_mean * yb_mean)
    return std_root + 0.3 * mean_root


def _grab_score_frame(
    ffmpeg: str, video_path: str, time: float, size: int = SCORE_FRAME_SIZE
) -> Optional[bytes]:
    """Decode one frame at *time* into a raw RGB24 buffer of ``size`` squared."""
    proc = _run(
        [
            ffmpeg, "-nostdin",
            "-ss", f"{time:.3f}",
            "-i", video_path,
            "-frames:v", "1",
            "-vf", f"scale={size}:{size}",
            "-pix_fmt", "rgb24",
            "-f", "rawvideo",
            "-",
        ],
        timeout=FRAME_TIMEOUT,
        binary_stdout=True,
    )
    data = proc.stdout or b""
    if proc.returncode != 0 or len(data) < size * size * 3:
        return None
    return data[: size * size * 3]


def select_thumbnail_times(
    scores: Sequence[FrameScore], count: int, duration: float
) -> List[FrameScore]:
    """Pick the *count* best-scoring frames that are not near-duplicates.

    Enforcing a minimum time gap is what stops all three thumbnails coming from
    the same three seconds of the same shot. If the gap rule cannot fill the
    quota, the remaining slots are filled by score alone.
    """
    ranked = sorted(
        (s for s in scores if not s.flat and s.score > 0),
        key=lambda s: (-s.score, s.time),
    )
    if not ranked:
        return []
    min_gap = max(MIN_THUMBNAIL_GAP_SECONDS, duration * MIN_THUMBNAIL_GAP_FRACTION)

    picked: List[FrameScore] = []
    for candidate in ranked:
        if len(picked) >= count:
            break
        if all(abs(candidate.time - chosen.time) >= min_gap for chosen in picked):
            picked.append(candidate)
    if len(picked) < count:
        for candidate in ranked:
            if len(picked) >= count:
                break
            if candidate not in picked:
                picked.append(candidate)
    return sorted(picked, key=lambda s: -s.score)


def extract_thumbnails(
    video_path: str,
    output_dir: str,
    *,
    duration: float,
    count: int = DEFAULT_THUMBNAIL_COUNT,
    scene_detect: bool = True,
    ffmpeg: Optional[str] = None,
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Score candidate frames and write the best ones as JPEGs.

    Returns ``(thumbnails, warnings)`` where each thumbnail carries its path,
    timestamp and the metrics it was picked on, so a caller that disagrees with
    the ranking can see why a frame won.
    """
    ffmpeg = ffmpeg or find_ffmpeg()
    if not ffmpeg:
        raise VideoPipelineError("ffmpeg not found — install ffmpeg to use the video pipeline")

    count = max(1, min(MAX_THUMBNAIL_COUNT, int(count)))
    warnings: List[str] = []

    scene_times: List[float] = []
    if scene_detect:
        try:
            scene_times = detect_scene_changes(video_path, duration, ffmpeg=ffmpeg)
        except Exception as exc:  # noqa: BLE001 - scene detection is optional
            logger.debug("scene detection failed: %s", exc, exc_info=True)
            warnings.append(f"scene detection failed ({exc}) — using evenly spaced frames only")

    times = candidate_times(duration, count, scene_times)
    scores: List[FrameScore] = []
    for time in times:
        buffer = _grab_score_frame(ffmpeg, video_path, time)
        if buffer is None:
            continue
        scores.append(score_frame(buffer, SCORE_FRAME_SIZE, SCORE_FRAME_SIZE, time=time))

    if not scores:
        warnings.append("no frames could be decoded for thumbnail selection")
        return [], warnings

    Path(output_dir).mkdir(parents=True, exist_ok=True)
    thumbnails: List[Dict[str, Any]] = []
    for rank, chosen in enumerate(select_thumbnail_times(scores, count, duration), start=1):
        out_path = str(Path(output_dir) / f"thumbnail-{rank:02d}.jpg")
        proc = _run(
            [
                ffmpeg, "-y", "-nostdin",
                "-ss", f"{chosen.time:.3f}",
                "-i", video_path,
                "-frames:v", "1",
                "-q:v", "2",
                out_path,
            ],
            timeout=FRAME_TIMEOUT,
        )
        if proc.returncode != 0 or not Path(out_path).exists():
            warnings.append(f"could not write a thumbnail at {chosen.time:.1f}s")
            continue
        entry = chosen.as_dict()
        entry["path"] = out_path
        entry["rank"] = rank
        thumbnails.append(entry)

    if not thumbnails:
        warnings.append("every thumbnail candidate was rejected (blank or undecodable frames)")
    return thumbnails, warnings


# ---------------------------------------------------------------------------
# Stage 5 — burned-in captions
# ---------------------------------------------------------------------------


def write_caption_files(
    cues: Sequence[Cue],
    output_dir: str,
    *,
    width: int,
    height: int,
    style_options: Optional[Dict[str, Any]] = None,
) -> Tuple[Dict[str, Any], List[str]]:
    """Write a styled ``subtitles.ass`` for *cues*. Returns ``(info, warnings)``.

    The ASS file is the styling contract: it is what gets burned in, and it is
    what an editor opens to reproduce or tweak the look by hand.
    """
    from tools.video_captions import format_ass, resolve_font, style_from_options

    style, warnings = style_from_options(style_options)
    font = resolve_font(style.font, " ".join(cue.text for cue in cues))
    warnings.extend(font.warnings)

    ass_path = Path(output_dir) / "subtitles.ass"
    ass_path.parent.mkdir(parents=True, exist_ok=True)
    ass_path.write_text(
        format_ass(cues, style, width=width, height=height, font=font),
        encoding="utf-8",
    )

    info: Dict[str, Any] = {
        "ass": str(ass_path),
        "font": font.family,
        "font_size": style.font_size or max(12, int(round(height * style.font_scale))),
        "position": style.position,
        "primary_color": style.primary_color,
    }
    return info, warnings


def burn_captions(
    video_path: str,
    ass_path: str,
    output_path: str,
    *,
    duration: float,
    fonts_dir: Optional[str] = None,
    ffmpeg: Optional[str] = None,
) -> str:
    """Render *ass_path* into the picture, writing an H.264 MP4 at *output_path*.

    Audio is stream-copied — only the video is touched — and the result is
    ``+faststart`` so it uploads and previews without a remux.
    """
    from tools.video_captions import build_burn_command

    ffmpeg = ffmpeg or find_ffmpeg()
    if not ffmpeg:
        raise VideoPipelineError("ffmpeg not found — install ffmpeg to burn captions")

    command = build_burn_command(
        ffmpeg, video_path, ass_path, output_path, fonts_dir=fonts_dir
    )
    # Re-encoding is the slowest thing the pipeline does; give it real headroom.
    proc = _run(command, timeout=min(MAX_FFMPEG_TIMEOUT, max(600.0, duration * 8)))
    if proc.returncode != 0 or not Path(output_path).exists():
        raise VideoPipelineError(f"caption burn-in failed: {_stderr_tail(proc)}")
    return output_path


# ---------------------------------------------------------------------------
# Output location
# ---------------------------------------------------------------------------


def default_output_dir(video_path: str) -> Path:
    """``<hermes home>/video_pipeline/<slug>`` — profile-aware, never ``~/.hermes``."""
    from hermes_constants import get_hermes_home

    stem = Path(video_path).stem or "video"
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", stem).strip("-.") or "video"
    return Path(get_hermes_home()) / "video_pipeline" / slug[:64]


def _unique_dir(base: Path) -> Path:
    """Return *base*, or ``base-2``/``base-3``/... when it already has output."""
    if not base.exists():
        return base
    for suffix in range(2, 1000):
        candidate = base.parent / f"{base.name}-{suffix}"
        if not candidate.exists():
            return candidate
    return base


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def run_pipeline(
    video_path: str,
    *,
    stages: Sequence[str] = DEFAULT_STAGES,
    output_dir: Optional[str] = None,
    cue_seconds: float = DEFAULT_CUE_SECONDS,
    stt_model: Optional[str] = None,
    stt_concurrency: Optional[int] = None,
    subtitle_formats: Sequence[str] = ("srt", "vtt"),
    caption_style: Optional[Dict[str, Any]] = None,
    title_count: int = DEFAULT_TITLE_COUNT,
    title_language: Optional[str] = None,
    thumbnail_count: int = DEFAULT_THUMBNAIL_COUNT,
    scene_detect: bool = True,
    transcriber=None,
    llm=None,
) -> PipelineResult:
    """Run the requested stages over *video_path* and return the assets.

    Stage dependencies are resolved here rather than pushed onto the caller:
    ``titles`` needs a transcript, and a transcript needs audio, so asking for
    titles alone still runs the audio and transcription work — it just does not
    write subtitle files unless ``subtitles`` was also requested. ``burn``
    likewise implies transcription, and writes a styled ``.ass`` on the way to
    rendering it into the picture.
    """
    requested = {stage for stage in stages if stage in ALL_STAGES}
    if not requested:
        raise VideoPipelineError(f"no valid stages requested (valid: {', '.join(ALL_STAGES)})")

    source = Path(video_path)
    if not source.exists() or not source.is_file():
        raise VideoPipelineError(f"video file not found: {video_path}")

    ffmpeg = find_ffmpeg()
    ffprobe = find_ffprobe()
    if not ffmpeg or not ffprobe:
        raise VideoPipelineError(
            "ffmpeg/ffprobe not found — install ffmpeg (brew install ffmpeg, "
            "apt install ffmpeg, winget install ffmpeg) to use the video pipeline"
        )

    info = probe_video(str(source), ffprobe)
    duration = float(info["duration"])

    target_dir = Path(output_dir).expanduser() if output_dir else _unique_dir(default_output_dir(str(source)))
    target_dir.mkdir(parents=True, exist_ok=True)

    result = PipelineResult(output_dir=str(target_dir), video=info)

    needs_transcript = bool(requested & {STAGE_SUBTITLES, STAGE_TITLES, STAGE_BURN})
    needs_audio = needs_transcript or STAGE_AUDIO in requested

    # ── audio ────────────────────────────────────────────────────────────
    if needs_audio:
        if not info.get("has_audio"):
            result.warnings.append("the video has no audio track — skipping audio, subtitles and titles")
            needs_transcript = False
        else:
            audio_path = str(target_dir / f"{source.stem or 'video'}.wav")
            try:
                extract_audio(str(source), audio_path, duration=duration, ffmpeg=ffmpeg)
            except VideoPipelineError as exc:
                # Thumbnails don't need the audio track — losing it should not
                # cost the caller the stages that can still run.
                result.warnings.append(f"audio extraction failed: {exc}")
                needs_transcript = False
            else:
                result.audio_path = audio_path
                result.stages.append(STAGE_AUDIO)

    # ── transcript + subtitles ───────────────────────────────────────────
    cues: List[Cue] = []
    if needs_transcript and result.audio_path:
        silences = detect_silences(result.audio_path, duration, ffmpeg=ffmpeg)
        windows = plan_cue_windows(duration, silences, cue_seconds=cue_seconds)
        if len(windows) > LOUD_REQUEST_COUNT:
            # One request per cue: on a metered provider a long video is a real
            # bill, so say so rather than quietly spending it.
            result.warnings.append(
                f"{len(windows)} speech-to-text requests for {duration / 60:.0f} minutes of "
                f"audio at cue_seconds={cue_seconds:g} — raise cue_seconds to send fewer"
            )
        cues, cue_warnings = transcribe_windows(
            result.audio_path,
            windows,
            model=stt_model,
            concurrency=stt_concurrency,
            ffmpeg=ffmpeg,
            transcriber=transcriber,
        )
        result.warnings.extend(cue_warnings)

        transcript = join_transcript(cues)
        if transcript:
            transcript_path = target_dir / "transcript.txt"
            transcript_path.write_text(transcript, encoding="utf-8")
            result.transcript_path = str(transcript_path)
            result.transcript_chars = len(transcript)
            result.transcript_preview = transcript[:500]
        else:
            result.warnings.append("transcription produced no text — check the STT provider in `hermes tools`")

        if STAGE_SUBTITLES in requested and cues:
            written: Dict[str, str] = {}
            if "srt" in subtitle_formats:
                srt_path = target_dir / "subtitles.srt"
                srt_path.write_text(format_srt(cues), encoding="utf-8")
                written["srt"] = str(srt_path)
            if "vtt" in subtitle_formats:
                vtt_path = target_dir / "subtitles.vtt"
                vtt_path.write_text(format_vtt(cues), encoding="utf-8")
                written["vtt"] = str(vtt_path)
            if written:
                written["cue_count"] = len(cues)
                result.subtitles = written
                result.stages.append(STAGE_SUBTITLES)

    # ── styled captions + burn-in ────────────────────────────────────────
    if STAGE_BURN in requested and cues:
        caption_info, caption_warnings = write_caption_files(
            cues,
            str(target_dir),
            width=int(info.get("width") or 1080),
            height=int(info.get("height") or 1920),
            style_options=caption_style,
        )
        result.warnings.extend(caption_warnings)
        if info.get("has_video"):
            burned_path = str(target_dir / f"{source.stem or 'video'}_captioned.mp4")
            try:
                burn_captions(
                    str(source),
                    caption_info["ass"],
                    burned_path,
                    duration=duration,
                    ffmpeg=ffmpeg,
                )
            except VideoPipelineError as exc:
                # The .ass still stands on its own — an editor can render it —
                # so a failed re-encode is a warning, not a lost stage.
                result.warnings.append(str(exc))
            else:
                caption_info["video"] = burned_path
                result.stages.append(STAGE_BURN)
        else:
            result.warnings.append("the file has no video track — wrote captions but did not burn them")
        result.captions = caption_info
    elif STAGE_BURN in requested:
        result.warnings.append("skipped burn-in — no cues to render")

    # ── titles ───────────────────────────────────────────────────────────
    if STAGE_TITLES in requested:
        transcript = join_transcript(cues)
        if transcript:
            titles, title_error = generate_titles(
                transcript,
                count=title_count,
                language=title_language,
                llm=llm,
            )
            if titles:
                result.titles = titles
                result.stages.append(STAGE_TITLES)
            if title_error:
                result.warnings.append(title_error)
        else:
            result.warnings.append("skipped titles — no transcript to work from")

    # ── thumbnails ───────────────────────────────────────────────────────
    if STAGE_THUMBNAILS in requested:
        if not info.get("has_video"):
            result.warnings.append("the file has no video track — skipping thumbnails")
        else:
            thumbnails, thumb_warnings = extract_thumbnails(
                str(source),
                str(target_dir),
                duration=duration,
                count=thumbnail_count,
                scene_detect=scene_detect,
                ffmpeg=ffmpeg,
            )
            result.warnings.extend(thumb_warnings)
            if thumbnails:
                result.thumbnails = thumbnails
                result.stages.append(STAGE_THUMBNAILS)

    return result
