#!/usr/bin/env python3
"""
Caption styling and burn-in for the video pipeline
==================================================

``tools/video_pipeline.py`` produces cues with timings. This module decides
what they *look like* and burns them into the picture — the part short-form
platforms actually need, since a sidecar ``.srt`` shows up nowhere on TikTok,
Reels or Shorts.

Two outputs:

* **``subtitles.ass``** — Advanced SubStation Alpha. Unlike SRT/VTT it carries
  the styling (font, size, colours, outline, position), so an editor can drop
  it into Premiere/Resolve and see the intended look, and libass can render it
  identically.
* **a burned MP4** — the same file with the captions rendered into the frames.

Styling is deliberately expressed in *ratios of the video height*, not pixels.
A caption authored at 64px on a 720x1280 vertical clip has to stay the same
relative size when the same pipeline runs on a 1080x1920 export or a 1920x1080
landscape interview; a fixed pixel size silently becomes unreadable on one and
enormous on the other.

Font resolution is a real failure mode, not a detail: libass silently
substitutes a fallback when a family is missing, and the usual result is
Korean text rendered as tofu boxes. :func:`resolve_font` therefore checks what
is actually installed before rendering and reports what it picked, so a wrong
font is a warning in the result rather than a ruined export discovered later.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Style
# ---------------------------------------------------------------------------

# Caption metrics as a fraction of the video's height. The defaults are tuned
# for vertical short-form, where captions sit above the platform's own UI
# furniture (username, caption text, buttons) in roughly the lower third.
DEFAULT_FONT_SCALE = 0.052
MIN_FONT_SCALE = 0.02
MAX_FONT_SCALE = 0.20
DEFAULT_MARGIN_SCALE = 0.16
DEFAULT_OUTLINE_RATIO = 0.09   # of the font size
DEFAULT_SHADOW_RATIO = 0.04
DEFAULT_MAX_LINES = 2

# Korean-capable families, best first. A caption font has to carry Hangul or
# the export is boxes; these are the ones that are actually present on real
# machines (Pretendard is the de-facto choice in Korean design work, the Noto
# CJK pair ships on Linux, the last two are the macOS/Windows system faces).
KOREAN_FONT_CANDIDATES = (
    "Pretendard",
    "Noto Sans KR",
    "Noto Sans CJK KR",
    "NanumGothic",
    "Nanum Gothic",
    "Apple SD Gothic Neo",
    "Malgun Gothic",
    "Spoqa Han Sans Neo",
)
LATIN_FONT_CANDIDATES = (
    "Montserrat",
    "Helvetica Neue",
    "Arial",
    "DejaVu Sans",
    "Noto Sans",
)

POSITIONS = ("bottom", "center", "top")
# ASS alignment codes: 1-3 bottom, 4-6 middle, 7-9 top (left/centre/right).
_ALIGNMENT = {"bottom": 2, "center": 5, "top": 8}

_HANGUL_RE = re.compile(r"[\uac00-\ud7a3\u1100-\u11ff\u3130-\u318f]")
_CJK_RE = re.compile(r"[\u3000-\u9fff\uac00-\ud7a3\uff00-\uff60]")


@dataclass
class CaptionStyle:
    """How burned-in captions should look.

    Sizes are fractions of the video height unless an absolute ``font_size``
    is given, so one style renders consistently across resolutions.
    """

    font: str = ""                      # family name or path to a font file
    font_size: Optional[int] = None     # absolute px; overrides font_scale
    font_scale: float = DEFAULT_FONT_SCALE
    bold: bool = True
    primary_color: str = "#FFFFFF"
    outline_color: str = "#000000"
    outline_width: Optional[float] = None
    shadow: Optional[float] = None
    box: bool = False                   # opaque box behind the text
    box_color: str = "#000000"
    box_opacity: float = 0.6
    position: str = "bottom"
    margin_scale: float = DEFAULT_MARGIN_SCALE
    max_lines: int = DEFAULT_MAX_LINES
    max_chars_per_line: Optional[int] = None
    uppercase: bool = False

    def normalized(self) -> "CaptionStyle":
        """Clamp every field into a range that renders sanely."""
        return replace(
            self,
            font_scale=max(MIN_FONT_SCALE, min(MAX_FONT_SCALE, float(self.font_scale))),
            font_size=(
                max(8, min(400, int(self.font_size))) if self.font_size else None
            ),
            margin_scale=max(0.0, min(0.45, float(self.margin_scale))),
            box_opacity=max(0.0, min(1.0, float(self.box_opacity))),
            max_lines=max(1, min(4, int(self.max_lines))),
            position=self.position if self.position in POSITIONS else "bottom",
        )


# Named looks. `shorts` is the default because that is what the captions are
# for; the others cover the cases where the default is wrong (a talking-head
# interview that should not shout, a busy background that needs a plate).
PRESETS: Dict[str, CaptionStyle] = {
    "shorts": CaptionStyle(),
    "yellow": CaptionStyle(primary_color="#FFE000", outline_color="#000000"),
    "minimal": CaptionStyle(
        font_scale=0.038,
        bold=False,
        margin_scale=0.07,
        outline_width=None,
        max_lines=2,
    ),
    "boxed": CaptionStyle(font_scale=0.042, box=True, margin_scale=0.09),
}
DEFAULT_PRESET = "shorts"


def style_from_options(options: Optional[Dict[str, Any]]) -> Tuple[CaptionStyle, List[str]]:
    """Build a style from a preset name plus per-field overrides.

    Returns ``(style, warnings)``; unknown keys are reported rather than
    silently ignored, because a typo'd style key is otherwise invisible until
    someone looks closely at the export.
    """
    options = dict(options or {})
    warnings: List[str] = []

    preset_name = str(options.pop("preset", DEFAULT_PRESET) or DEFAULT_PRESET).lower()
    if preset_name not in PRESETS:
        warnings.append(
            f"unknown caption preset {preset_name!r} — using {DEFAULT_PRESET!r} "
            f"(available: {', '.join(sorted(PRESETS))})"
        )
        preset_name = DEFAULT_PRESET
    style = replace(PRESETS[preset_name])

    known = {f for f in style.__dataclass_fields__}  # type: ignore[attr-defined]
    for key, value in options.items():
        if value is None:
            continue
        if key not in known:
            warnings.append(f"ignored unknown caption style option {key!r}")
            continue
        try:
            setattr(style, key, value)
        except Exception:  # pragma: no cover - dataclass setattr does not validate
            warnings.append(f"ignored caption style option {key!r}")

    return style.normalized(), warnings


# ---------------------------------------------------------------------------
# Colour
# ---------------------------------------------------------------------------


def hex_to_ass_color(value: str, *, alpha: float = 0.0) -> str:
    """Convert ``#RRGGBB`` to ASS ``&HAABBGGRR``.

    ASS stores colours byte-reversed and treats the alpha byte as
    *transparency* (00 = opaque), which is the opposite of every colour picker
    a user will have taken the value from.
    """
    text = (value or "").strip().lstrip("#")
    if len(text) == 3:
        text = "".join(channel * 2 for channel in text)
    if len(text) != 6 or not re.fullmatch(r"[0-9a-fA-F]{6}", text):
        text = "FFFFFF"
    red, green, blue = text[0:2], text[2:4], text[4:6]
    transparency = int(round(max(0.0, min(1.0, alpha)) * 255))
    return f"&H{transparency:02X}{blue}{green}{red}".upper()


# ---------------------------------------------------------------------------
# Fonts
# ---------------------------------------------------------------------------


@dataclass
class FontChoice:
    """The font that will actually be used, and where libass can find it."""

    family: str
    fonts_dir: Optional[str] = None
    warnings: List[str] = field(default_factory=list)


def _fc_list_families() -> List[str]:
    """Families known to fontconfig, or [] where fontconfig is absent."""
    binary = shutil.which("fc-list")
    if not binary:
        return []
    try:
        proc = subprocess.run(
            [binary, "--format", "%{family}\n"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=30, stdin=subprocess.DEVNULL, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    families: List[str] = []
    for line in (proc.stdout or "").splitlines():
        # fontconfig reports localized aliases comma-separated.
        families.extend(part.strip() for part in line.split(",") if part.strip())
    return families


def _font_family_of_file(path: Path) -> Optional[str]:
    """Read a font file's family name via fc-query."""
    binary = shutil.which("fc-query")
    if not binary:
        return None
    try:
        proc = subprocess.run(
            [binary, "--format", "%{family[0]}", str(path)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=30, stdin=subprocess.DEVNULL, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    family = (proc.stdout or "").strip()
    return family or None


def _platform_default_font(needs_hangul: bool) -> str:
    """System face to fall back on when fontconfig can't answer."""
    if sys.platform == "darwin":
        return "Apple SD Gothic Neo" if needs_hangul else "Helvetica Neue"
    if sys.platform == "win32":
        return "Malgun Gothic" if needs_hangul else "Arial"
    return "Noto Sans CJK KR" if needs_hangul else "DejaVu Sans"


def resolve_font(requested: str, text: str = "") -> FontChoice:
    """Pick the font to render with, preferring one that can draw *text*.

    A font file path is used directly (its directory becomes libass'
    ``fontsdir``). A family name is checked against fontconfig and reported
    when missing rather than silently substituted. With nothing requested, the
    first installed candidate wins — Korean-capable when the text contains
    Hangul.
    """
    needs_hangul = bool(_HANGUL_RE.search(text or ""))
    installed = _fc_list_families()
    warnings: List[str] = []

    requested = (requested or "").strip()
    if requested:
        candidate = Path(requested).expanduser()
        if candidate.suffix.lower() in {".ttf", ".otf", ".ttc", ".otc"} or candidate.exists():
            if not candidate.is_file():
                warnings.append(f"font file not found: {candidate} — falling back to a system font")
            else:
                family = _font_family_of_file(candidate) or candidate.stem
                return FontChoice(family=family, fonts_dir=str(candidate.parent), warnings=warnings)
        else:
            if installed and requested not in installed:
                warnings.append(
                    f"font {requested!r} is not installed — libass will substitute a "
                    "fallback, which usually means missing glyphs"
                )
            return FontChoice(family=requested, warnings=warnings)

    candidates = KOREAN_FONT_CANDIDATES if needs_hangul else (
        LATIN_FONT_CANDIDATES + KOREAN_FONT_CANDIDATES
    )
    for family in candidates:
        if family in installed:
            return FontChoice(family=family, warnings=warnings)

    fallback = _platform_default_font(needs_hangul)
    if installed:
        warnings.append(
            f"no preferred caption font is installed — using {fallback!r}. "
            "Install Pretendard or Noto Sans KR for proper Korean captions."
        )
    return FontChoice(family=fallback, warnings=warnings)


# ---------------------------------------------------------------------------
# Text layout
# ---------------------------------------------------------------------------


def _em_factor(text: str) -> float:
    """Average glyph width in ems: CJK glyphs are full-width, Latin about half."""
    if not text:
        return 0.55
    cjk = len(_CJK_RE.findall(text))
    ratio = cjk / max(1, len(text))
    return 0.55 + 0.45 * ratio


def chars_per_line(text: str, font_size: int, video_width: int, *, safe: float = 0.88) -> int:
    """How many characters fit on one line at this size and width."""
    usable = max(1.0, video_width * safe)
    return max(6, int(usable / max(1.0, font_size * _em_factor(text))))


def wrap_lines(text: str, max_chars: int) -> List[str]:
    """Break *text* into lines of at most *max_chars*, preferring word breaks.

    Korean is written with spaces between 어절, so word wrapping works; a token
    longer than the line (a URL, a long compound) is hard-split rather than
    allowed to overflow the frame.
    """
    text = " ".join((text or "").split())
    if not text:
        return []
    max_chars = max(4, int(max_chars))

    lines: List[str] = []
    current = ""
    for word in text.split(" "):
        while len(word) > max_chars:
            if current:
                lines.append(current)
                current = ""
            lines.append(word[:max_chars])
            word = word[max_chars:]
        if not current:
            current = word
        elif len(current) + 1 + len(word) <= max_chars:
            current = f"{current} {word}"
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def chunk_caption_blocks(text: str, max_chars: int, max_lines: int) -> List[str]:
    """Split a cue's text into on-screen blocks of at most *max_lines* lines.

    A cue whose text does not fit becomes several blocks shown one after the
    other, which is what caption editors do. The alternatives are worse:
    folding the overflow into the last line pushes text off the side of the
    frame, and dropping it loses words the viewer just heard.
    """
    lines = wrap_lines(text, max_chars)
    if not lines:
        return []
    max_lines = max(1, int(max_lines))
    return [
        "\\N".join(lines[index : index + max_lines])
        for index in range(0, len(lines), max_lines)
    ]


def split_cue_duration(
    start: float, end: float, blocks: Sequence[str], *, min_block: float = 0.6
) -> List[Tuple[float, float]]:
    """Divide ``[start, end)`` between *blocks*, weighted by their length.

    A block with twice the text gets roughly twice the time on screen, but
    never less than *min_block* — a two-character block flashing by is
    unreadable even if it is proportionally "correct".
    """
    if not blocks:
        return []
    span = max(0.0, end - start)
    if len(blocks) == 1 or span <= 0:
        return [(start, end)]

    weights = [max(1, len(block.replace("\\N", " "))) for block in blocks]
    total = sum(weights)
    floor = min(min_block, span / len(blocks))

    spans: List[Tuple[float, float]] = []
    cursor = start
    for index, weight in enumerate(weights):
        if index == len(weights) - 1:
            spans.append((cursor, end))
            break
        length = max(floor, span * weight / total)
        # Leave enough room for the blocks that still have to be shown.
        remaining_floor = floor * (len(weights) - index - 1)
        length = min(length, max(floor, end - cursor - remaining_floor))
        spans.append((cursor, cursor + length))
        cursor += length
    return spans


def _escape_ass_text(text: str) -> str:
    """Neutralize ASS override syntax in caption text."""
    return text.replace("{", "(").replace("}", ")")


def format_ass_timestamp(seconds: float) -> str:
    """ASS uses ``H:MM:SS.cc`` — one digit of hours, centiseconds."""
    seconds = max(0.0, seconds)
    centis = int(round(seconds * 100))
    hours, centis = divmod(centis, 360000)
    minutes, centis = divmod(centis, 6000)
    secs, centis = divmod(centis, 100)
    return f"{hours:d}:{minutes:02d}:{secs:02d}.{centis:02d}"


# ---------------------------------------------------------------------------
# ASS document
# ---------------------------------------------------------------------------


def format_ass(
    cues: Sequence[Any],
    style: CaptionStyle,
    *,
    width: int,
    height: int,
    font: Optional[FontChoice] = None,
) -> str:
    """Render cues as a styled ASS subtitle document.

    *cues* are :class:`tools.video_pipeline.Cue` instances (anything with
    ``start`` / ``end`` / ``text``).
    """
    style = style.normalized()
    width = max(16, int(width))
    height = max(16, int(height))

    font_size = style.font_size or max(12, int(round(height * style.font_scale)))
    outline = (
        style.outline_width
        if style.outline_width is not None
        else max(1.0, round(font_size * DEFAULT_OUTLINE_RATIO, 1))
    )
    shadow = (
        style.shadow
        if style.shadow is not None
        else round(font_size * DEFAULT_SHADOW_RATIO, 1)
    )
    margin_v = int(round(height * style.margin_scale))
    margin_h = int(round(width * 0.06))

    sample = " ".join(getattr(cue, "text", "") for cue in cues)
    if font is None:
        font = resolve_font(style.font, sample)

    border_style = 3 if style.box else 1
    back_color = (
        hex_to_ass_color(style.box_color, alpha=1.0 - style.box_opacity)
        if style.box
        else hex_to_ass_color("#000000", alpha=0.5)
    )

    header = [
        "[Script Info]",
        "; Generated by Hermes video_pipeline",
        "ScriptType: v4.00+",
        f"PlayResX: {width}",
        f"PlayResY: {height}",
        "WrapStyle: 2",          # we wrap explicitly; libass must not re-wrap
        "ScaledBorderAndShadow: yes",
        "YCbCr Matrix: TV.709",
        "",
        "[V4+ Styles]",
        (
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
            "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
            "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
            "Alignment, MarginL, MarginR, MarginV, Encoding"
        ),
        (
            f"Style: Default,{font.family},{font_size},"
            f"{hex_to_ass_color(style.primary_color)},"
            f"{hex_to_ass_color(style.primary_color)},"
            f"{hex_to_ass_color(style.outline_color)},"
            f"{back_color},"
            f"{-1 if style.bold else 0},0,0,0,100,100,0,0,"
            f"{border_style},{outline},{shadow},"
            f"{_ALIGNMENT[style.position]},{margin_h},{margin_h},{margin_v},1"
        ),
        "",
        "[Events]",
        (
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, "
            "MarginV, Effect, Text"
        ),
    ]

    events: List[str] = []
    for cue in cues:
        text = (getattr(cue, "text", "") or "").strip()
        if not text:
            continue
        if style.uppercase:
            text = text.upper()
        limit = style.max_chars_per_line or chars_per_line(text, font_size, width)
        blocks = chunk_caption_blocks(text, limit, style.max_lines)
        spans = split_cue_duration(
            float(getattr(cue, "start", 0.0)), float(getattr(cue, "end", 0.0)), blocks
        )
        for block, (start, end) in zip(blocks, spans):
            events.append(
                f"Dialogue: 0,{format_ass_timestamp(start)},"
                f"{format_ass_timestamp(end)},Default,,0,0,0,,{_escape_ass_text(block)}"
            )

    return "\n".join(header + events) + "\n"


# ---------------------------------------------------------------------------
# Burn-in
# ---------------------------------------------------------------------------


def escape_filter_path(path: str) -> str:
    """Escape a path for use inside an ffmpeg filtergraph argument.

    Windows drive letters are the reason this exists: an unescaped ``C:\\`` in
    a filter argument parses as an option separator.
    """
    return str(path).replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")


def build_burn_command(
    ffmpeg: str,
    video_path: str,
    ass_path: str,
    output_path: str,
    *,
    fonts_dir: Optional[str] = None,
    crf: int = 20,
    preset: str = "veryfast",
) -> List[str]:
    """Build the ffmpeg command that renders *ass_path* into the picture."""
    filter_arg = f"ass='{escape_filter_path(ass_path)}'"
    if fonts_dir:
        filter_arg += f":fontsdir='{escape_filter_path(fonts_dir)}'"
    return [
        ffmpeg, "-y", "-nostdin",
        "-i", video_path,
        "-vf", filter_arg,
        "-c:v", "libx264",
        "-preset", preset,
        "-crf", str(crf),
        "-pix_fmt", "yuv420p",
        "-c:a", "copy",
        "-movflags", "+faststart",
        output_path,
    ]
