#!/usr/bin/env python3
"""porch media — transcription and image work through capable harnesses.

Every operation runs the selected profile through backend_run.sh in the
read-only `media` mode (no shell, no web) with a private staging directory as
its working directory. Inputs are prepared there by Porch:

- transcribe: ffmpeg converts audio or video to mono 16 kHz MP3 (WAV when the
  local ffmpeg has no MP3 encoder) and splits long recordings into segments.
  Antigravity rejects M4A and FLAC, and video frames are not needed for
  speech, so the harness only ever sees formats it can hear. Each segment is
  transcribed against a JSON schema; Porch offsets timestamps and renders
  text, SRT, WebVTT or JSON.
- image describe/generate/edit: input images are copied into the staging
  directory. Generated images are collected from the harness's own session
  artifact store (Codex: $CODEX_HOME/generated_images/<thread_id>, agy:
  ~/.gemini/antigravity-cli/brain/<conversation_id>), identified by the
  session id in the run's raw event stream, then copied to --out-dir. The
  agent never needs write access, and an image it merely claims to have saved
  does not count.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from backend_contract import (  # noqa: E402
    get_agent,
    load_config,
    media_operation_limits,
    media_operations,
    resolve_agent,
)

LIB_DIR = Path(__file__).resolve().parent
BACKEND_RUN = LIB_DIR / "backend_run.sh"

EXIT_OK = 0
EXIT_PARTIAL = 2
EXIT_CONFIG = 4
EXIT_USAGE = 5
EXIT_EMPTY = 66

AUDIO_EXTS = {".wav", ".mp3", ".m4a", ".aac", ".ogg", ".oga", ".opus", ".flac",
              ".aiff", ".aif", ".wma", ".amr", ".caf"}
VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi", ".mpeg", ".mpg",
              ".3gp", ".wmv", ".flv", ".ts"}
FORMATS = ("text", "srt", "vtt", "json")
ASPECT_RE = re.compile(r"^\d{1,2}:\d{1,2}$")

TRANSCRIPT_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "language": {"type": "string"},
        "error": {"type": "string"},
        "segments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "start": {"type": "number"},
                    "end": {"type": "number"},
                    "speaker": {"type": "string"},
                    "text": {"type": "string"},
                },
                "required": ["start", "end", "text"],
            },
        },
    },
    "required": ["segments"],
}


class MediaError(Exception):
    def __init__(self, message: str, code: int = 1):
        super().__init__(message)
        self.code = code


def info(message: str) -> None:
    if os.environ.get("PORCH_PROGRESS_STYLE", "full") != "none":
        print(f"[porch] media {message}", file=sys.stderr, flush=True)


# ---- capability and input checks -------------------------------------------

def capable_profiles(operation: str) -> List[str]:
    agents = (load_config().get("agents") or {})
    return sorted(name for name, agent in agents.items()
                  if operation in media_operations(str(agent.get("backend") or "")))


def require_operation(agent_id: str, operation: str):
    try:
        get_agent(agent_id)
    except KeyError:
        raise MediaError(f"unknown agent id: {agent_id} (list profiles with: porch --list-agents)",
                         EXIT_CONFIG)
    resolved = resolve_agent(agent_id, mode="media")
    if operation not in media_operations(resolved.backend):
        options = capable_profiles(operation)
        hint = (f"profiles that can: {', '.join(options)} — for example: porch media "
                f"{operation.replace('.', ' ')} -a {options[0]} …") if options else \
            "no configured profile supports it"
        raise MediaError(
            f"agent '{agent_id}' (backend {resolved.backend}) does not support media "
            f"{operation}; {hint}", EXIT_CONFIG)
    return resolved


def image_kind(path: Path) -> str:
    with open(path, "rb") as f:
        head = f.read(16)
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    return ""


def existing_file(value: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_file():
        raise MediaError(f"input file not found: {value}", EXIT_USAGE)
    return path.resolve()


def read_prompt(positional: Optional[str], prompt_file: Optional[str], *, required: bool) -> str:
    if positional and prompt_file:
        raise MediaError("cannot combine --prompt-file with a positional prompt", EXIT_USAGE)
    text = ""
    if prompt_file:
        try:
            text = Path(prompt_file).read_text(encoding="utf-8")
        except OSError as e:
            raise MediaError(f"cannot read prompt file: {e}", EXIT_USAGE)
    elif positional:
        text = positional
    elif not sys.stdin.isatty():
        text = sys.stdin.read()
    if required and not text.strip():
        raise MediaError("no prompt provided (positional argument, --prompt-file, or stdin)",
                         EXIT_USAGE)
    return text.strip()


# ---- backend invocation ------------------------------------------------------

def run_backend(agent_id: str, prompt: str, stage: Path, run_dir: Path, *,
                extra_env: Optional[Dict[str, str]] = None) -> str:
    prompt_path = stage / ".porch-prompt.txt"
    prompt_path.write_text(prompt, encoding="utf-8")
    env = dict(os.environ)
    for key in ("PORCH_RAW_PROMPT", "PORCH_SKIP_OUTPUT_TEMPLATE", "PORCH_ARTIFACT_KEY",
                "PORCH_MEDIA_IMAGES", "PORCH_OUTPUT_SCHEMA"):
        env.pop(key, None)
    env.update({"PORCH_RUN_DIR": str(run_dir), "PORCH_SAVE_OUTPUTS": "1",
                "PORCH_SUPPRESS_SHELL_WARN": "1"})
    env.update(extra_env or {})
    try:
        proc = subprocess.run(
            ["bash", str(BACKEND_RUN), "--mode", "media", "--agent-id", agent_id,
             "--raw", "--prompt-file", str(prompt_path)],
            cwd=str(stage), env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, text=True)
    finally:
        prompt_path.unlink(missing_ok=True)
    if proc.returncode != 0:
        raise MediaError(f"{agent_id} failed (exit {proc.returncode})", proc.returncode)
    return proc.stdout


def raw_events(run_dir: Path, agent_id: str) -> List[Dict[str, Any]]:
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", agent_id)
    path = run_dir / "raw" / f"{safe}.jsonl"
    events = []
    if path.is_file():
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                events.append(obj)
    return events


def artifact_dir(backend: str, events: Sequence[Dict[str, Any]]) -> Path:
    """Locate the harness's own store of images generated in this session."""
    if backend == "codex-cli":
        thread = next((e.get("thread_id") for e in events
                       if e.get("type") == "thread.started" and e.get("thread_id")), "")
        if not thread:
            raise MediaError("codex stream has no thread.started event; cannot locate its images")
        home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
        return home / "generated_images" / re.sub(r"[^A-Za-z0-9._-]", "_", str(thread))
    if backend == "antigravity-cli":
        conv = next((e.get("conversation_id") for e in events
                     if e.get("event") == "init" and e.get("conversation_id")), "")
        if not conv:
            raise MediaError("agy stream has no init conversation_id; cannot locate its images")
        brain = Path(os.environ.get("PORCH_AGY_BRAIN_DIR")
                     or Path.home() / ".gemini" / "antigravity-cli" / "brain")
        return brain / re.sub(r"[^A-Za-z0-9._-]", "_", str(conv))
    raise MediaError(f"backend {backend} has no known image artifact store")


def collect_images(store: Path, since: float) -> List[Tuple[Path, str]]:
    if not store.is_dir():
        return []
    found = []
    for entry in sorted(store.iterdir(), key=lambda p: p.stat().st_mtime):
        if entry.is_file() and not entry.is_symlink() and entry.stat().st_mtime >= since - 1:
            kind = image_kind(entry)
            if kind:
                found.append((entry, kind))
    return found


def slug(text: str, fallback: str) -> str:
    words = re.findall(r"[A-Za-z0-9]+", text.lower())[:6]
    return "-".join(words)[:48] or fallback


def place(src: Path, kind: str, out_dir: Path, stem: str) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    n = 1
    while True:
        dst = out_dir / f"{stem}-{n}.{kind}"
        try:
            with open(src, "rb") as fin, open(dst, "xb") as fout:
                shutil.copyfileobj(fin, fout)
            return dst
        except FileExistsError:
            n += 1


# ---- transcription -------------------------------------------------------------

def tool(name: str, env_key: str) -> str:
    configured = os.environ.get(env_key)
    found = (configured if configured and os.access(configured, os.X_OK) else "") \
        if configured else shutil.which(name)
    if not found:
        raise MediaError(f"{name} is required for transcription (install ffmpeg, e.g. "
                         f"`brew install ffmpeg` or `apt install ffmpeg`)", EXIT_CONFIG)
    return found


def duration_of(ffprobe: str, path: Path) -> float:
    proc = subprocess.run([ffprobe, "-v", "error", "-show_entries", "format=duration",
                           "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
                          capture_output=True, text=True)
    try:
        return float(proc.stdout.strip().splitlines()[0])
    except (IndexError, ValueError):
        raise MediaError(f"ffprobe cannot read the duration of {path.name}: "
                         f"{proc.stderr.strip()[-300:]}")


def prepare_audio(src: Path, stage: Path, segment_seconds: int) -> List[Tuple[Path, float, float]]:
    """Return [(chunk, offset, duration)] in playback order."""
    ffmpeg = tool("ffmpeg", "PORCH_BIN_FFMPEG")
    ffprobe = tool("ffprobe", "PORCH_BIN_FFPROBE")
    encoders = subprocess.run([ffmpeg, "-hide_banner", "-encoders"], capture_output=True,
                              text=True).stdout
    if "libmp3lame" in encoders:
        ext, codec = "mp3", ["-c:a", "libmp3lame", "-b:a", "48k"]
    else:
        ext, codec = "wav", ["-c:a", "pcm_s16le"]
    pattern = stage / f"chunk-%03d.{ext}"
    proc = subprocess.run([ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
                           "-i", str(src), "-vn", "-ac", "1", "-ar", "16000", *codec,
                           "-f", "segment", "-segment_time", str(segment_seconds),
                           "-reset_timestamps", "1", str(pattern)],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        raise MediaError(f"ffmpeg could not extract audio from {src.name}: "
                         f"{proc.stderr.strip()[-500:]}")
    chunks = sorted(stage.glob(f"chunk-*.{ext}"))
    if not chunks:
        raise MediaError(f"{src.name} has no audio stream to transcribe", EXIT_EMPTY)
    out, offset = [], 0.0
    for chunk in chunks:
        length = duration_of(ffprobe, chunk)
        out.append((chunk, offset, length))
        offset += length
    return out


def transcription_prompt(chunk: Path, index: int, total: int, source: str,
                         language: str, speakers: bool, instructions: str) -> str:
    part = f" (part {index + 1} of {total} of the recording \"{source}\")" if total > 1 else \
        f" (the recording \"{source}\")"
    lines = [
        f"Transcribe the audio file ./{chunk.name}{part}.",
        "Listen to it with your file-reading tool (read_file / view_file): it delivers the "
        "audio to you natively. Do not run shell commands, do not use any other tool to "
        "infer the content, and never guess.",
        "Return JSON matching the provided schema. Each segment is a natural phrase or "
        "sentence with start and end in seconds from the beginning of this file and the "
        "verbatim spoken text, in the language it is spoken"
        + (f" (expected language: {language})." if language else "."),
        ("Label speakers consistently as \"Speaker 1\", \"Speaker 2\", … in the speaker field."
         if speakers else "Omit the speaker field."),
        "If you cannot hear the audio, return an empty segments list and put the exact "
        "reason in the error field. Return an empty segments list without error only for "
        "silence or audio without speech.",
    ]
    if instructions:
        lines.append(f"Additional instructions from the user: {instructions}")
    return "\n".join(lines)


def parse_transcript(text: str, label: str) -> Dict[str, Any]:
    body = text.strip()
    if body.startswith("```"):
        body = re.sub(r"^```(?:json)?\s*|\s*```$", "", body)
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        raise MediaError(f"{label}: the agent did not return transcript JSON: {text[:300]!r}")
    if not isinstance(data, dict) or not isinstance(data.get("segments"), list):
        raise MediaError(f"{label}: transcript JSON has no segments list")
    if str(data.get("error") or "").strip():
        raise MediaError(f"{label}: the agent could not transcribe it: {data['error']}")
    return data


def fmt_ts(seconds: float, sep: str) -> str:
    ms = max(0, int(round(seconds * 1000)))
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}{sep}{ms:03d}"


def render(segments: List[Dict[str, Any]], fmt: str, meta: Dict[str, Any]) -> str:
    def label(seg: Dict[str, Any]) -> str:
        return f"{seg['speaker']}: " if seg.get("speaker") else ""

    if fmt == "json":
        return json.dumps({**meta, "segments": segments}, ensure_ascii=False, indent=2) + "\n"
    if fmt == "srt":
        return "".join(f"{i}\n{fmt_ts(s['start'], ',')} --> {fmt_ts(s['end'], ',')}\n"
                       f"{label(s)}{s['text']}\n\n" for i, s in enumerate(segments, 1))
    if fmt == "vtt":
        return "WEBVTT\n\n" + "".join(
            f"{fmt_ts(s['start'], '.')} --> {fmt_ts(s['end'], '.')}\n{label(s)}{s['text']}\n\n"
            for s in segments)
    lines: List[str] = []
    for seg in segments:
        if seg.get("speaker") and lines and lines[-1].startswith(f"{seg['speaker']}: "):
            lines[-1] += " " + seg["text"]
        elif seg.get("speaker"):
            lines.append(label(seg) + seg["text"])
        else:
            lines.append(seg["text"])
    joiner = "\n" if any(s.get("speaker") for s in segments) else " "
    return joiner.join(lines).strip() + "\n"


def cmd_transcribe(args: argparse.Namespace) -> int:
    resolved = require_operation(args.agent, "transcribe")
    src = existing_file(args.file)
    if src.suffix.lower() not in AUDIO_EXTS | VIDEO_EXTS:
        info(f"warning: {src.suffix or 'no extension'} is not a known audio/video extension; "
             "ffmpeg will decide")
    if args.segment_minutes <= 0:
        raise MediaError("--segment-minutes must be positive", EXIT_USAGE)
    with tempfile.TemporaryDirectory(prefix="porch-media-") as tmp:
        stage = Path(tmp)
        chunks = prepare_audio(src, stage, int(args.segment_minutes * 60))
        total = sum(d for _, _, d in chunks)
        info(f"transcribe agent={args.agent} file={src.name} duration={total:.1f}s "
             f"segments={len(chunks)}")
        schema = stage / ".porch-transcript-schema.json"
        schema.write_text(json.dumps(TRANSCRIPT_SCHEMA), encoding="utf-8")
        merged: List[Dict[str, Any]] = []
        language = args.language or ""
        for i, (chunk, offset, length) in enumerate(chunks):
            info(f"segment {i + 1}/{len(chunks)} offset={offset:.1f}s")
            out = run_backend(
                args.agent,
                transcription_prompt(chunk, i, len(chunks), src.name, args.language or "",
                                     args.speakers, args.instructions or ""),
                stage, stage / f"run-{i:03d}",
                extra_env={"PORCH_OUTPUT_SCHEMA": str(schema)})
            data = parse_transcript(out, f"segment {i + 1}")
            language = language or str(data.get("language") or "")
            for seg in data["segments"]:
                try:
                    start = min(max(float(seg["start"]), 0.0), length)
                    end = min(max(float(seg["end"]), start), length)
                except (KeyError, TypeError, ValueError):
                    raise MediaError(f"segment {i + 1}: malformed timestamps in {seg!r}")
                text = str(seg.get("text") or "").strip()
                if not text:
                    continue
                item = {"start": round(offset + start, 3), "end": round(offset + end, 3),
                        "text": text}
                if args.speakers and seg.get("speaker"):
                    item["speaker"] = str(seg["speaker"])
                merged.append(item)
        if not merged:
            raise MediaError(f"no speech was transcribed from {src.name}", EXIT_EMPTY)
        meta = {"source": str(src), "agent": args.agent, "model": resolved.model,
                "effort": resolved.effort, "language": language, "duration": round(total, 3),
                "segment_count": len(chunks)}
        rendered = render(merged, args.format, meta)
    if args.out:
        out_path = Path(args.out).expanduser()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(rendered, encoding="utf-8")
        info(f"wrote {out_path}")
    else:
        sys.stdout.write(rendered)
    return EXIT_OK


# ---- images ----------------------------------------------------------------------

def stage_images(inputs: Sequence[str], stage: Path, limit: int, operation: str) -> List[Path]:
    if len(inputs) > limit:
        raise MediaError(f"{operation} accepts at most {limit} input images for this backend; "
                         f"got {len(inputs)}", EXIT_USAGE)
    staged = []
    for i, value in enumerate(inputs, 1):
        src = existing_file(value)
        kind = image_kind(src)
        if not kind:
            raise MediaError(f"not a PNG, JPEG, WebP or GIF image: {value}", EXIT_USAGE)
        dst = stage / f"image-{i}.{kind}"
        shutil.copyfile(src, dst)
        staged.append(dst)
    return staged


def tool_name(backend: str) -> str:
    return {"codex-cli": "built-in image generation tool (image_gen)",
            "antigravity-cli": "generate_image tool"}.get(backend, "image generation tool")


def image_prompt(operation: str, backend: str, prompt: str, staged: Sequence[Path],
                 count: int, aspect: str) -> str:
    refs = ", ".join(f"./{p.name}" for p in staged)
    if operation == "image.describe":
        return "\n".join([
            f"Look at these image files with your file-reading / image-viewing tool: {refs}."
            + (" They are also attached to this message." if backend == "codex-cli" else ""),
            "Do not run shell commands and do not modify any file.",
            prompt or "Describe each image in detail: subject, composition, colors, text, "
                      "and anything notable.",
        ])
    lines = []
    if operation == "image.edit":
        lines.append(
            f"Reference images: {refs}"
            + (" (attached to this message; pass them to the tool as reference images)."
               if backend == "codex-cli" else
               " (pass their absolute paths to the tool as input images).")
            + " Edit or combine them as instructed.")
    lines += [
        f"Use your {tool_name(backend)} to create {count} image{'s' if count > 1 else ''}"
        f" — call it exactly {count} time{'s' if count > 1 else ''}, one image per call.",
        f"Instructions: {prompt}",
    ]
    if aspect:
        lines.append(f"Aspect ratio: {aspect}.")
    lines += [
        "Do not create, copy, convert, move or edit any file yourself and do not run shell "
        "commands: the tool saves each image and Porch collects it from there.",
        "Finish with one short line per image describing what it shows.",
    ]
    return "\n".join(lines)


def cmd_image(args: argparse.Namespace) -> int:
    operation = f"image.{args.image_command}"
    resolved = require_operation(args.agent, operation)
    limits = media_operation_limits(resolved.backend, operation)
    prompt = read_prompt(args.prompt, args.prompt_file,
                         required=args.image_command != "describe")
    inputs = list(getattr(args, "input", None) or [])
    if args.image_command in ("describe", "edit") and not inputs:
        raise MediaError(f"image {args.image_command} needs at least one --input IMAGE",
                         EXIT_USAGE)
    if args.image_command == "generate" and inputs:
        raise MediaError("image generate takes no --input; use `porch media image edit` "
                         "to transform or combine images", EXIT_USAGE)
    count = getattr(args, "count", 1) or 1
    if count < 1 or count > int(limits.get("max_count", 4)):
        raise MediaError(f"--count must be between 1 and {limits.get('max_count', 4)}",
                         EXIT_USAGE)
    aspect = getattr(args, "aspect", "") or ""
    if aspect and not ASPECT_RE.match(aspect):
        raise MediaError("--aspect must look like 1:1, 16:9 or 9:16", EXIT_USAGE)
    with tempfile.TemporaryDirectory(prefix="porch-media-") as tmp:
        stage = Path(tmp)
        staged = stage_images(inputs, stage, int(limits.get("max_inputs", 1)), operation) \
            if inputs else []
        extra = {}
        if resolved.backend == "codex-cli" and staged:
            extra["PORCH_MEDIA_IMAGES"] = "\n".join(str(p) for p in staged)
        info(f"{operation} agent={args.agent} inputs={len(staged)}"
             + (f" count={count}" if operation != "image.describe" else ""))
        started = time.time()
        run_dir = stage / "run"
        text = run_backend(args.agent, image_prompt(operation, resolved.backend, prompt,
                                                    staged, count, aspect),
                           stage, run_dir, extra_env=extra)
        if operation == "image.describe":
            if not text.strip():
                raise MediaError("the agent returned no description", EXIT_EMPTY)
            sys.stdout.write(text if text.endswith("\n") else text + "\n")
            return EXIT_OK
        store = artifact_dir(resolved.backend, raw_events(run_dir, args.agent))
        produced = collect_images(store, started)
        out_dir = Path(args.out_dir).expanduser().resolve()
        stem = re.sub(r"[^A-Za-z0-9._-]", "-", args.name) if args.name else \
            slug(prompt, "image")
        placed = [{"path": str(place(src, kind, out_dir, stem)), "format": kind,
                   "bytes": src.stat().st_size, "source": str(src)} for src, kind in produced]
    manifest = {"operation": operation, "agent": args.agent, "backend": resolved.backend,
                "model": resolved.model, "requested": count, "produced": len(placed),
                "images": placed, "agent_note": text.strip()}
    if not placed:
        print(json.dumps(manifest, ensure_ascii=False, indent=2))
        raise MediaError(f"the agent produced no image (looked in {store})", EXIT_EMPTY)
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    for item in placed:
        info(f"saved {item['path']}")
    if len(placed) < count:
        info(f"warning: requested {count} images, produced {len(placed)}")
        return EXIT_PARTIAL
    return EXIT_OK


# ---- animated GIF ------------------------------------------------------------

def sheet_grid(frames: int) -> Tuple[int, int]:
    """Columns × rows for a spritesheet: an exact factor pair when its ratio is
    at most 3:1 (both models accept that), otherwise the smallest near-square
    grid with trailing blank cells."""
    for rows in range(int(math.sqrt(frames)), 0, -1):
        if frames % rows == 0 and frames // rows <= 3 * rows:
            return frames // rows, rows
    cols = math.ceil(math.sqrt(frames))
    return cols, math.ceil(frames / cols)


def animation_prompt(backend: str, prompt: str, staged: Sequence[Path], frames: int,
                     cols: int, rows: int) -> str:
    blank = cols * rows - frames
    lines = []
    if staged:
        refs = ", ".join(f"./{p.name}" for p in staged)
        lines.append(f"Reference images: {refs}"
                     + (" (attached to this message; pass them to the tool as reference images)."
                        if backend == "codex-cli" else
                        " (pass their absolute paths to the tool as input images).")
                     + " Keep the subject's identity and style from them.")
    lines += [
        f"Use your {tool_name(backend)} exactly once to create ONE spritesheet image for a "
        f"looping animation of {frames} frames.",
        f"Layout: a grid of {cols} columns × {rows} rows of equal square cells, edge to edge, "
        "with no borders, gutters, frame numbers, labels or captions. Frames run in reading "
        "order: left to right, then top to bottom."
        + (f" Leave the last {blank} cell{'s' if blank > 1 else ''} plain empty background."
           if blank else ""),
        f"Aspect ratio: {cols // math.gcd(cols, rows)}:{rows // math.gcd(cols, rows)}.",
        "Every frame shows the same subject at the same size and position against the same "
        "background, with the camera still unless asked otherwise; only the motion changes, "
        "in small even steps. The last frame must lead smoothly back into the first so the "
        "loop is seamless.",
        f"Animation: {prompt}",
        "Do not create, copy, convert, move or edit any file yourself and do not run shell "
        "commands: the tool saves the image and Porch assembles the GIF.",
        "Finish with one short line describing the spritesheet.",
    ]
    return "\n".join(lines)


def assemble_gif(sheet: Path, out: Path, cols: int, rows: int, frames: int,
                 frame_ms: int, width: int) -> None:
    ffmpeg = tool("ffmpeg", "PORCH_BIN_FFMPEG")
    seconds = frame_ms / 1000
    scale = f",scale={width}:-2:flags=lanczos" if width else ""
    graph = (f"format=rgb24,crop=iw-mod(iw\\,{cols}):ih-mod(ih\\,{rows}):0:0,untile={cols}x{rows},"
             f"trim=end_frame={frames},setpts=N*{seconds}/TB{scale},split[a][b];"
             "[a]palettegen=stats_mode=full[p];[b][p]paletteuse=dither=sierra2_4a")
    proc = subprocess.run([ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
                           "-i", str(sheet), "-vf", graph, "-fps_mode", "passthrough",
                           "-loop", "0", "-final_delay", str(frame_ms // 10), str(out)],
                          capture_output=True, text=True)
    if proc.returncode != 0 or not out.is_file() or image_kind(out) != "gif":
        raise MediaError(f"ffmpeg could not assemble the GIF: {proc.stderr.strip()[-500:]}")


def cmd_animate(args: argparse.Namespace) -> int:
    resolved = require_operation(args.agent, "image.animate")
    limits = media_operation_limits(resolved.backend, "image.animate")
    prompt = read_prompt(args.prompt, args.prompt_file, required=True)
    if not 2 <= args.frames <= 16:
        raise MediaError("--frames must be between 2 and 16", EXIT_USAGE)
    if not 20 <= args.frame_ms <= 2000 or args.frame_ms % 10:
        raise MediaError("--frame-ms must be a multiple of 10 between 20 and 2000 "
                         "(GIF delays are centiseconds)", EXIT_USAGE)
    if args.width and not 16 <= args.width <= 2048:
        raise MediaError("--width must be between 16 and 2048", EXIT_USAGE)
    tool("ffmpeg", "PORCH_BIN_FFMPEG")  # fail before spending a generation
    cols, rows = sheet_grid(args.frames)
    with tempfile.TemporaryDirectory(prefix="porch-media-") as tmp:
        stage = Path(tmp)
        staged = stage_images(args.input or [], stage, int(limits.get("max_inputs", 1)),
                              "image.animate") if args.input else []
        extra = {}
        if resolved.backend == "codex-cli" and staged:
            extra["PORCH_MEDIA_IMAGES"] = "\n".join(str(p) for p in staged)
        info(f"image.animate agent={args.agent} frames={args.frames} grid={cols}x{rows} "
             f"frame_ms={args.frame_ms}")
        started = time.time()
        run_dir = stage / "run"
        text = run_backend(args.agent, animation_prompt(resolved.backend, prompt, staged,
                                                        args.frames, cols, rows),
                           stage, run_dir, extra_env=extra)
        store = artifact_dir(resolved.backend, raw_events(run_dir, args.agent))
        produced = collect_images(store, started)
        if not produced:
            raise MediaError(f"the agent produced no spritesheet (looked in {store})", EXIT_EMPTY)
        sheet, kind = produced[-1]
        gif = stage / "animation.gif"
        assemble_gif(sheet, gif, cols, rows, args.frames, args.frame_ms, args.width)
        out_dir = Path(args.out_dir).expanduser().resolve()
        stem = re.sub(r"[^A-Za-z0-9._-]", "-", args.name) if args.name else slug(prompt, "animation")
        gif_path = place(gif, "gif", out_dir, stem)
        sheet_path = place(sheet, kind, out_dir, f"{stem}-sheet")
    manifest = {"operation": "image.animate", "agent": args.agent, "backend": resolved.backend,
                "model": resolved.model, "frames": args.frames, "frame_ms": args.frame_ms,
                "grid": f"{cols}x{rows}",
                "images": [{"path": str(gif_path), "format": "gif",
                            "bytes": gif_path.stat().st_size, "source": "assembled by porch"},
                           {"path": str(sheet_path), "format": kind,
                            "bytes": sheet_path.stat().st_size, "source": str(sheet)}],
                "agent_note": text.strip()}
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    info(f"saved {gif_path}")
    return EXIT_OK


# ---- capabilities -------------------------------------------------------------

def cmd_capabilities(args: argparse.Namespace) -> int:
    ops = ("transcribe", "image.describe", "image.generate", "image.edit", "image.animate")
    table = {op: capable_profiles(op) for op in ops}
    if args.json:
        print(json.dumps(table, indent=2))
    else:
        for op in ops:
            print(f"{op:16} {', '.join(table[op]) or '-'}")
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="porch media",
                                 description="Transcribe audio/video and work with images "
                                             "through a capable agent profile.")
    sub = ap.add_subparsers(dest="command", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-a", "--agent", required=True,
                        help="exact profile id (see: porch media capabilities)")
    common.add_argument("--progress", choices=("full", "compact", "none"),
                        default=os.environ.get("PORCH_PROGRESS") or "compact",
                        help="stderr progress style (default compact)")

    t = sub.add_parser("transcribe", parents=[common], help="transcribe an audio or video file")
    t.add_argument("file")
    t.add_argument("--format", choices=FORMATS, default="text")
    t.add_argument("--out", help="write the transcript here instead of stdout")
    t.add_argument("--language", help="expected spoken language (hint)")
    t.add_argument("--speakers", action="store_true", help="label speakers (best effort)")
    t.add_argument("--segment-minutes", type=float, default=15.0,
                   help="split longer recordings into segments of this length (default 15)")
    t.add_argument("--instructions", help="extra instructions, e.g. vocabulary or spelling")
    t.set_defaults(func=cmd_transcribe)

    img = sub.add_parser("image", help="describe, generate or edit images")
    isub = img.add_subparsers(dest="image_command", required=True)
    for name, help_text in (("describe", "describe or analyze input images"),
                            ("generate", "generate new images from a text prompt"),
                            ("edit", "edit one image or combine several references")):
        p = isub.add_parser(name, parents=[common], help=help_text)
        p.add_argument("prompt", nargs="?")
        p.add_argument("--prompt-file")
        if name != "generate":
            p.add_argument("--input", action="append", metavar="IMAGE",
                           help="input image (repeatable)")
        if name != "describe":
            p.add_argument("--count", type=int, default=1, help="number of images (default 1)")
            p.add_argument("--aspect", help="aspect ratio such as 1:1, 16:9, 9:16")
            p.add_argument("--out-dir", default=".", help="where to save images (default: .)")
            p.add_argument("--name", help="file name stem (default: derived from the prompt)")
        p.set_defaults(func=cmd_image)

    a = isub.add_parser("animate", parents=[common],
                        help="create a looping animated GIF (spritesheet assembled by porch)")
    a.add_argument("prompt", nargs="?")
    a.add_argument("--prompt-file")
    a.add_argument("--input", action="append", metavar="IMAGE",
                   help="reference image for the subject or style (repeatable)")
    a.add_argument("--frames", type=int, default=8, help="frame count, 2-16 (default 8)")
    a.add_argument("--frame-ms", type=int, default=150,
                   help="milliseconds per frame, multiple of 10 (default 150)")
    a.add_argument("--width", type=int, default=0, help="scale frames to this width in pixels")
    a.add_argument("--out-dir", default=".", help="where to save the GIF and spritesheet")
    a.add_argument("--name", help="file name stem (default: derived from the prompt)")
    a.set_defaults(func=cmd_animate)

    c = sub.add_parser("capabilities", help="list profiles per media operation")
    c.add_argument("--json", action="store_true")
    c.set_defaults(func=cmd_capabilities)
    return ap


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if getattr(args, "progress", None):
        # Shared with backend_run.sh and normalize_stream.py (see progress.sh).
        os.environ["PORCH_PROGRESS_STYLE"] = args.progress
        if args.progress == "compact":
            os.environ.setdefault("PORCH_PROGRESS_INTERVAL", "10")
    try:
        return args.func(args)
    except MediaError as e:
        print(f"Error: {e}", file=sys.stderr)
        return e.code
    except (KeyError, ValueError, FileNotFoundError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return EXIT_CONFIG


if __name__ == "__main__":
    raise SystemExit(main())
