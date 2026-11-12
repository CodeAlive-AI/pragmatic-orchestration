# Media mode

`porch media` transcribes audio and video, describes video, and works with images through one
exact profile. Every operation runs read-only in a private staging directory:
no shell, no web, no writes to the caller's repository. Porch prepares the
inputs, and collects the results itself instead of trusting the agent's claims.

```bash
"$PORCH" media capabilities                      # which profiles can do what
"$PORCH" media transcribe -a agy-gemini-4-argon meeting.m4a > meeting.txt
"$PORCH" media transcribe -a agy-gemini-3.8-flash --format srt --out talk.srt talk.mp4
"$PORCH" media video describe -a agy-gemini-3.8-flash --input demo.mov "List the steps the presenter shows"
"$PORCH" media image describe -a claude-sonnet --input chart.png "What trend does it show?"
"$PORCH" media image generate -a codex --aspect 16:9 --out-dir art "A lighthouse at sunset, flat illustration"
"$PORCH" media image edit -a codex --input photo.png --name night "Turn it into a night scene"
"$PORCH" media image edit -a agy-gemini-4-argon --input a.png --input b.jpg "Combine both into one panorama"
"$PORCH" media image animate -a codex --frames 8 --frame-ms 120 --name ball "A red ball bouncing on a wooden floor"
```

Write image prompts with the official guidance for the model behind the
profile: [prompting-gpt-image.md](prompting-gpt-image.md) for Codex and
[prompting-nano-banana.md](prompting-nano-banana.md) for Antigravity.

## Support matrix

Verified live on 2026-11-10 (agy 1.5.4, codex-cli 0.167.0, Claude Code 2.1.322).

| Operation | Backends | Notes |
|---|---|---|
| `transcribe` | Antigravity (`agy`) | The only harness whose model hears audio headlessly. Codex attaches an audio file but its model cannot listen; Claude Code, OpenCode and Devin have no audio input. |
| `video describe` | Antigravity (verified 2026-11-12, agy 1.5.6) | agy sees frames through `view_file` but not sound, so Porch adds the audio track as a separate MP3 (`--no-audio` skips it). See [Video](#video). |
| `image describe` | Antigravity, Codex, Claude Code | Images are copied into the staging directory; Codex also gets them as `--image` attachments. |
| `image generate` | Antigravity, Codex | Codex `image_gen` (`gpt-image-2`, PNG); agy `generate_image` through its image-generator subagent (Gemini image model, JPEG). |
| `image edit` | Antigravity (≤3 inputs), Codex (≤5 inputs) | One input edits it; several inputs are combined. |
| `image animate` | Antigravity, Codex (needs ffmpeg) | Looping GIF from a generated spritesheet. |

Grok Build (Imagine) and OpenCode (`image_generate` with a configured image
provider) also ship image generation, but neither is wired into `porch media`
yet: Grok's output location and OpenCode's provider setup were not verified
end to end.

Use the shipped `agy-gemini-4-argon` (best quality, paid Google AI plan) or
`agy-gemini-3.8-flash` (every plan, faster) profiles for Antigravity.

## Transcription

1. `ffmpeg` (required) converts the input — any audio or video container — to
   mono 16 kHz MP3, or WAV when the local ffmpeg has no MP3 encoder. agy rejects
   M4A and FLAC, and video frames add cost without helping speech recognition.
2. Recordings longer than `--segment-minutes` (default 15) are split; each
   segment is a separate agy run.
3. Each run must return JSON matching a schema (`agy --json-schema`): segments
   with `start`/`end` seconds and verbatim text. Session-only deny rules block
   file writes, shell commands, MCP and web, so the model cannot "infer" a
   transcript from file metadata; an unreadable file must be reported in the
   `error` field and fails the command.
4. Porch offsets timestamps by each segment's measured duration and renders
   `--format text` (default), `srt`, `vtt` or `json` (with source, model,
   language, duration and segments) to stdout or `--out`.

Options: `--language` (hint), `--speakers` (best-effort labels),
`--instructions` (vocabulary, spelling), `--segment-minutes`.

Limits: speaker labels are only consistent within one segment — raise
`--segment-minutes` for interviews when labels matter. A word cut at a segment
boundary can be lost or duplicated. agy's own size limits are undocumented;
15-minute MP3 segments stay small (about 5 MB).

## Images

- Inputs: `--input` is repeatable; PNG, JPEG, WebP and GIF are accepted (checked
  by content, not extension).
- `generate` and `edit` take `--count` (1–4), `--aspect` (`1:1`, `16:9`, `9:16`,
  …; a request to the model, which agy honors and Codex treats as guidance),
  `--out-dir` (default `.`) and `--name` (file stem; default from the prompt).
  Existing files are never overwritten: the next free `-N` suffix is used.
- The harness's own image tool saves each image in its session store: Codex in
  `$CODEX_HOME/generated_images/<thread_id>/`, agy in
  `~/.gemini/antigravity-cli/brain/<conversation_id>/` (`PORCH_AGY_BRAIN_DIR`
  overrides the brain root). Porch reads the session id from the run's raw
  event stream, copies the images created during this run to `--out-dir`, and
  prints a JSON manifest on stdout: `operation`, `agent`, `model`,
  `requested`, `produced`, `images[{path, format, bytes, source}]`,
  `agent_note`.
- `describe` prints the answer on stdout.

## Animated GIFs

`image animate` asks the model for one spritesheet — a grid of equal square
cells with no borders or labels, frames in reading order, a still camera and a
seamless loop — and Porch assembles the GIF with ffmpeg (`untile`, palette
generation, infinite loop). Options: `--frames` 2–16 (default 8), `--frame-ms`
20–2000 in steps of 10 (default 150; GIF delays are centiseconds), `--width`
to scale frames, `--input` for a reference subject or style, `--out-dir`,
`--name`. The grid is an exact factor pair of `--frames` when its ratio is at
most 3:1 (8 → 4×2, 9 → 3×3, 6 → 3×2), otherwise a near-square grid whose last
cells stay empty (7 → 3×3). The GIF and the spritesheet are both saved and
listed in the manifest, so a sheet can be re-assembled or edited.

Codex's own animated output (the Images API `gif` format, 2–16 frames) sits
behind the under-development `image_generation_animation` feature in Codex's
source and is absent from codex-cli 0.167.0; the spritesheet route works with
both harnesses today. Expect small frame-to-frame drift: describe a starting
pose the motion returns to, keep the camera still, and keep motion simple.

## Video

`video describe` takes one `--input` video and an optional question or
instruction, and prints the answer to stdout. How Porch prepares it:

- **Pass-through.** MP4, MOV or WebM under 95 MB is staged unchanged.
- **Re-encoding.** Anything else, or any larger file, is re-encoded with ffmpeg to a silent MP4 at 2 fps, at most 480 px high. This is needed because `view_file` refuses files over 100 MB. If the result is still too big, the command fails and asks you to trim the input.
- **Audio.** The audio track is extracted to mono 16 kHz MP3. If the video has no audio track, only the frames are described.

Timestamps are approximate, because the model sees sampled frames. Use
`transcribe` when you need exact speech with timings.

agy 1.5.6 has no video generator: no Veo tool or subagent
(google-antigravity/antigravity-cli#734 is still open). For short motion, use
`image animate`.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Done |
| 2 | Fewer images than `--count` (the produced ones are saved and listed) |
| 4 | Unknown or incapable profile (the error names capable ones), or ffmpeg missing for transcribe/animate/video |
| 5 | Usage error: missing input, too many inputs, not an image or video, video too large, bad `--aspect`/`--count` |
| 66 | Nothing transcribed, or no image or spritesheet produced |
| other | The backend run failed; its diagnostics are on stderr |

`--progress full|compact|none` (default `compact`, or `PORCH_PROGRESS`) controls
stderr progress. Progress never reaches stdout.
