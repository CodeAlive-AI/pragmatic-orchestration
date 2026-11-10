# Media mode

`porch media` transcribes audio and video and works with images through one
exact profile. Every operation runs read-only in a private staging directory:
no shell, no web, no writes to the caller's repository. Porch prepares the
inputs, and collects the results itself instead of trusting the agent's claims.

```bash
"$PORCH" media capabilities                      # which profiles can do what
"$PORCH" media transcribe -a agy-gemini-4-argon meeting.m4a > meeting.txt
"$PORCH" media transcribe -a agy-gemini-3.8-flash --format srt --out talk.srt talk.mp4
"$PORCH" media image describe -a claude-sonnet --input chart.png "What trend does it show?"
"$PORCH" media image generate -a codex --aspect 16:9 --out-dir art "A lighthouse at sunset, flat illustration"
"$PORCH" media image edit -a codex --input photo.png --name night "Turn it into a night scene"
"$PORCH" media image edit -a agy-gemini-4-argon --input a.png --input b.jpg "Combine both into one panorama"
```

## Support matrix

Verified live on 2026-11-10 (agy 1.5.4, codex-cli 0.167.0, Claude Code 2.1.322).

| Operation | Backends | Notes |
|---|---|---|
| `transcribe` | Antigravity (`agy`) | The only harness whose model hears audio headlessly. Codex attaches an audio file but its model cannot listen; Claude Code, OpenCode and Devin have no audio input. |
| `image describe` | Antigravity, Codex, Claude Code | Images are copied into the staging directory; Codex also gets them as `--image` attachments. |
| `image generate` | Antigravity, Codex | Codex `image_gen` (`gpt-image-2`, PNG); agy `generate_image` through its image-generator subagent (Gemini image model, JPEG). |
| `image edit` | Antigravity (≤3 inputs), Codex (≤5 inputs) | One input edits it; several inputs are combined. |

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

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Done |
| 2 | Fewer images than `--count` (the produced ones are saved and listed) |
| 4 | Unknown or incapable profile (the error names capable ones), or ffmpeg missing |
| 5 | Usage error: missing input, too many inputs, not an image, bad `--aspect`/`--count` |
| 66 | Nothing transcribed or no image produced |
| other | The backend run failed; its diagnostics are on stderr |

`--progress full|compact|none` (default `compact`, or `PORCH_PROGRESS`) controls
stderr progress. Progress never reaches stdout.
