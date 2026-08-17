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

Everything except the transcription request and the title request runs locally through ffmpeg.

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
| `subtitle_formats` | `["srt", "vtt"]` | |
| `title_count` | `5` | 1–12 |
| `title_language` | transcript language | e.g. `"Korean"` |
| `thumbnail_count` | `3` | 1–10 |

## How subtitle timing works

Hermes' speech-to-text layer is provider-agnostic and returns text, not timestamps — every backend can fill that contract, including ones that ship later. So the pipeline gets its timing from the cut instead of from the provider:

1. `silencedetect` maps the pauses in the extracted audio.
2. Speech is split into cue-sized windows **at those pauses**, so cues break on phrase boundaries.
3. Each window is transcribed on its own. The cue's start and end are the window's — exact by construction.
4. Silence-only stretches are never sent, which saves requests and avoids the empty-audio hallucinations whisper-family models are prone to.

**This means one speech-to-text request per cue.** A 10-minute talking-head video at the default `cue_seconds: 8` is roughly 60–75 requests. Local faster-whisper makes that free; on a paid API, raise `cue_seconds` to 20–30 to cut the request count by 3x, at the cost of longer lines on screen. Local backends are transcribed one window at a time (they share a single model instance); cloud backends run a couple of windows in parallel.

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
