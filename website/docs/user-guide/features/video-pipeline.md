---
title: Video Pipeline
description: Turn a local video into subtitles (.srt/.vtt), hook-style title candidates, and ranked thumbnail stills with one tool call.
sidebar_label: Video Pipeline
sidebar_position: 12
---

# Video Pipeline

The `video_pipeline` tool takes one local video file and produces the assets you need to publish it:

| Stage | Output |
|---|---|
| `audio` | 16 kHz mono WAV extracted with ffmpeg |
| `subtitles` | `subtitles.srt` + `subtitles.vtt` with real cue timings, plus `transcript.txt` |
| `titles` | Hook-style title candidates drafted from the transcript |
| `thumbnails` | JPEG stills picked by image quality, spread across the video |
| `burn` | Styled `subtitles.ass` + a `_captioned.mp4` with the captions rendered into the picture |

Everything except the transcription request and the title request runs locally through ffmpeg.

`burn` is the one stage that is **not** in the default set — it re-encodes the video, so you ask for it explicitly.

## Setup

1. **Install ffmpeg** — `brew install ffmpeg` (macOS), `apt install ffmpeg` (Debian/Ubuntu), `winget install ffmpeg` (Windows). The tool does not appear in the model's schema until both `ffmpeg` and `ffprobe` are on the PATH.
2. **Enable the toolset** — run `hermes tools`, tick **🎞️ Video Pipeline**. It ships off by default.
3. **Configure speech-to-text** — `hermes tools` → **🎙️ Speech-to-Text**. Any backend works: local faster-whisper (free), Groq, OpenAI, Mistral, xAI, ElevenLabs, DeepInfra, or a plugin-registered provider. Only the `subtitles` and `titles` stages need it.

## Usage

Ask for it in plain language:

```
Make subtitles and 5 title options for ~/Movies/interview.mp4, plus 3 thumbnails
```

The agent calls the tool and gets back the output directory, file paths, the title candidates, and each thumbnail's timestamp and score. Files land in `~/.hermes/video_pipeline/<video-name>/` unless you name an `output_dir`.

### Arguments

| Argument | Default | Notes |
|---|---|---|
| `video_path` | *(required)* | Local path; relative paths resolve against the working directory |
| `stages` | all four | e.g. `["thumbnails"]` to skip transcription entirely |
| `output_dir` | per-video folder under the Hermes home | |
| `cue_seconds` | `8` | Target subtitle cue length, 2–30 |
| `clean_captions` | `true` | Fix recognizer spacing/mishearings — see below |
| `subtitle_formats` | `["srt", "vtt"]` | |
| `title_count` | `5` | 1–12 |
| `title_language` | transcript language | e.g. `"Korean"` |
| `thumbnail_count` | `3` | 1–10 |
| `caption_style` | `{"preset": "shorts"}` | Burned-in caption look — see below |

## Burned-in captions

A sidecar `.srt` displays nowhere on Shorts, Reels or TikTok, so short-form needs the text rendered into the frames. Ask for the `burn` stage:

```
이 영상 자막 구워서 내보내줘 — 노란색으로, 조금 크게
```

You get two files: `subtitles.ass` (the styling in an editable format Premiere and Resolve both read) and `<name>_captioned.mp4` (H.264, `+faststart`, audio stream-copied — only the video is re-encoded).

### Style

| Key | Default | Notes |
|---|---|---|
| `preset` | `shorts` | `shorts` (big bold white, heavy outline), `yellow`, `minimal`, `boxed` |
| `font` | best installed | Family name or a path to a `.ttf`/`.otf` |
| `font_scale` | `0.052` | Fraction of video **height**, not pixels |
| `font_size` | — | Absolute pixels; overrides `font_scale` |
| `primary_color` / `outline_color` | `#FFFFFF` / `#000000` | |
| `position` | `bottom` | `bottom`, `center`, `top` |
| `margin_scale` | `0.16` | Distance from that edge, as a fraction of height |
| `max_lines` | `2` | Longer cues split into consecutive blocks, not overflowing lines |
| `box` / `uppercase` | `false` | Translucent plate behind the text / upper-case (Latin) |

Sizes are fractions of the height so one style renders the same on a 720x1280 phone export and a 1080x1920 master. A cue whose text does not fit in `max_lines` is split into consecutive on-screen blocks with the time divided between them — folding the overflow into the last line would push text off the side of the frame.

### Hook title

`title_overlay` burns a title over the video — the line a scrolling viewer reads before deciding to stay. Leave `text` out and it uses the best candidate the `titles` stage just wrote, so titling and burning is one call:

```
제목 뽑아서 그중 제일 좋은 걸로 영상 위에 박아줘
```

| Key | Default | Notes |
|---|---|---|
| `text` | first generated title | |
| `duration` | whole video | Seconds on screen from `start` |
| `start` | `0` | |
| `font_scale` | `0.062` | Larger than the captions by default |
| `position` | `top` | So it clears the bottom captions |
| `margin_scale` | `0.09` | |
| `max_lines` | `3` | |
| `font` / `font_size` / `primary_color` / `outline_color` / `box` | as captions | |

A short flash is a common cut — `duration: 2` puts the hook up for the first two seconds and gets out of the way. Whether the viewer can actually *read* it in that time depends on its length, and nothing in the render reveals that they couldn't, so the pipeline checks: a title too long for its screen time comes back as a warning with the length that would fit (Korean and other CJK text is read at roughly 7 characters per second, Latin script at about 17).

The title gets its own style row in the `.ass`, so it can be repositioned or recoloured without touching the captions — in the file or in an editor. Unlike a spoken cue, a title that does not fit wraps onto more lines rather than splitting into consecutive blocks: it is one unit the viewer reads at a glance.

### Fonts

**Korean captions need a Korean font.** libass silently substitutes when a family is missing, and the usual result is a wall of tofu boxes discovered after the export. The pipeline checks what is installed before rendering, prefers a Hangul-capable face when the transcript contains Hangul (Pretendard → Noto Sans KR → Noto Sans CJK KR → NanumGothic → the macOS/Windows system faces), and reports what it picked in `captions.font`. A missing requested font is a warning in the result, not a surprise later.

```bash
# Linux, if nothing Korean is installed
apt install fonts-noto-cjk
```

## How subtitle timing works

Hermes' speech-to-text layer is provider-agnostic and returns text, not timestamps — every backend can fill that contract, including ones that ship later. So the pipeline gets its timing from the cut instead of from the provider:

1. `silencedetect` maps the pauses in the extracted audio. Its threshold is derived from the track's own mean level rather than fixed, so a quietly-recorded video does not read as one long silence — measured on the same audio attenuated by 25 dB, a fixed floor dropped speech coverage from 12.7s to 5.0s; the adaptive floor gives an identical cue plan at both levels.
2. Speech is split into cue-sized windows **at those pauses**, so cues break on phrase boundaries.
3. Each window is transcribed on its own. The cue's start and end are the window's — exact by construction.
4. Silence-only stretches are never sent, which saves requests and avoids the empty-audio hallucinations whisper-family models are prone to.

**This means one speech-to-text request per cue.** A 10-minute talking-head video at the default `cue_seconds: 8` is roughly 60–75 requests. Local faster-whisper makes that free; on a paid API, raise `cue_seconds` to 20–30 to cut the request count by 3x, at the cost of longer lines on screen. Local backends are transcribed one window at a time (they share a single model instance); cloud backends run a couple of windows in parallel.

## Fixing what the recognizer got wrong

Speech recognizers hand back text that is heard correctly but written badly. Korean models routinely return whole utterances with **no spacing at all** (`고민을내려놓고칼을들어`), every model mishears words a reader would get from context (`호미를` → `고민을`), and punctuation lands wherever the acoustic model felt a pause. Burned into a video, all three read as sloppiness.

The `caption_cleanup` auxiliary model corrects the cues before they are written, so the `.srt`, the burn and the titles all get the corrected text:

```
고민을내려놓고칼을들어       →  호미를 내려놓고 칼을 들어.
잃을게없는여자들이세상을뒤집으로간다  →  잃을 게 없는 여자들이 세상을 뒤집으러 간다.
```

An unvalidated LLM pass over subtitles is dangerous — it will occasionally invent a line, and a fabricated subtitle burned into a video is worse than a missing space. So the pass may only rewrite, never restructure:

- corrections come back keyed by cue index and are matched back one by one; a cue the model drops keeps its original text,
- a correction that changes more than its **edit budget** is rejected as a hallucination and the original is kept (the budget is absolute — 3 characters minimum so a short cue's one misheard word can still be fixed, 8 maximum so a long cue cannot be quietly rewritten wholesale),
- an unconfigured or failing model leaves every cue exactly as it was, with a warning.

Cues go out in batches of 40, so one bad batch cannot cost the whole transcript. The number of corrected cues comes back as `subtitles.cleaned_cues`. Set `clean_captions: false` to keep the raw recognizer output.

## How thumbnails are picked

Candidate frames come from an even grid across the video plus keyframe scene changes, with the first and last few percent excluded — that is where intros fade in and outros fade to black. Each candidate is decoded to a tiny raw RGB buffer and scored on four components:

- **Sharpness** — Laplacian energy, so a motion-blurred frame loses to an in-focus one.
- **Contrast** — luma spread.
- **Colourfulness** — Hasler-Süsstrunk metric.
- **Exposure** — distance from a mid-tone average.

Frames that are essentially one flat colour (the black between shots, a white flash) are rejected outright. The winners must be spread apart in time, so three thumbnails come from three different moments rather than three frames of the same shot. Each returned thumbnail carries its score and metrics, so you can see why a frame won.

No image library is involved — ffmpeg decodes, and the scoring is plain Python over a few thousand bytes.

## Titles

Title candidates are drafted by the `video_titles` auxiliary task, which defaults to your main model. Point it at something cheap and fast in `config.yaml`:

```yaml
auxiliary:
  video_titles:
    provider: "auto"
    model: ""        # e.g. a flash/mini tier
    timeout: 60
```

The transcript is trimmed to its opening and close before the call — that is where a video states its topic and its payoff.

## Notes

- A run reports partial results rather than failing outright: if the speech-to-text provider is down, you still get thumbnails, with the failure listed under `warnings`.
- A video with no audio track skips the audio, subtitle, and title stages and says so.
- Credential files (`auth.json`, `.env`, token stores) are refused before ffmpeg or any provider sees them.
