#!/usr/bin/env python3
"""porch sessions — search and navigate local coding-agent session histories.

Read-only, index-free reader over the native on-disk session stores of CLI
coding agents. Emits bounded JSONL fragments with source locators and honest
coverage diagnostics. `turns`/`flags`/`stats` add a turn-level reflection
layer (delegated to lib/analyze.py). See references/session-history.md for
the format recipes and the navigation algorithm this implements.

Never execute stored content. Never mutate source files. SQLite stores are
opened `mode=ro` + `PRAGMA query_only` — never `immutable=1`, never manual
WAL parsing.
"""

from __future__ import annotations

import argparse
import codecs
import ctypes
import fnmatch
import io
import json
import os
import re
import sqlite3
import sys
import threading
import time
import urllib.parse
import uuid
from datetime import datetime, timezone
from itertools import islice
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator
from steer.launcher import launcher_instance

HOME = Path.home()
IS_MACOS = sys.platform == "darwin"
IS_WINDOWS = sys.platform == "win32"


def _win_appdata() -> Path:
    """Roaming AppData (per-machine sync'd) — where Claude Desktop/Cursor keep state."""
    return Path(os.environ.get("APPDATA") or (HOME / "AppData" / "Roaming"))


def _win_localapp() -> Path:
    """Local AppData — caches and machine-local state."""
    return Path(os.environ.get("LOCALAPPDATA") or (HOME / "AppData" / "Local"))

EXIT_OK = 0
EXIT_USAGE = 5
EXIT_PARTIAL = 2
EXIT_FAILED = 3
EXIT_MEMORY_LIMIT = 6
EXIT_QUEUE_TIMEOUT = 7
MEMORY_ENV = "PORCH_SESSIONS_MEMORY_LIMIT_MB"
PARALLEL_ENV = "PORCH_SESSIONS_MAX_PARALLEL"
QUEUE_TIMEOUT_ENV = "PORCH_SESSIONS_QUEUE_TIMEOUT_S"
MEMORY_SAMPLE_S = 0.2

# ---------------------------------------------------------------------------
# Normalized fragment model
#
# kind:      prompt | assistant | context | tool_call | tool_result |
#            reasoning | permission | compaction | boundary | metadata | unknown
# authorship: human | agent | system | unknown   (who authored the text)
# visible:   True | False | None(unknown)        (shown to the user)
# Every fragment carries `locator` so the reader can jump back to the source.
# ---------------------------------------------------------------------------

KINDS_CONVO = {"prompt", "assistant"}
KINDS_TOOLS = {"tool_call", "tool_result"}
KINDS_SYSTEM = {"context", "metadata", "boundary", "permission", "compaction"}
SCOPES = {
    "convo": KINDS_CONVO,
    "prompts": {"prompt"},
    "assistant": {"assistant"},
    "tools": KINDS_TOOLS,
    "reasoning": {"reasoning"},
    "system": KINDS_SYSTEM | {"unknown"},
    "all": None,  # no filtering
}

AUTOCONTEXT_MARKERS = (
    "<command-name>",
    "<command-message>",
    "<command-args>",
    "<local-command-stdout>",
    "<local-command-stderr>",
    "<system-reminder>",
    "<environment_context>",
    "Caveat:",
)

# Codex response-layer injections arrive as role=user/developer records that
# never appear in event_msg.user_message. They are recognizable by their XML/
# INSTRUCTIONS wrappers; free text without these markers is a genuine prompt
# (or an unverifiable steer) even when the event_msg mirror is missing or
# arrives later in the file.
CODEX_INJECTION_MARKERS = (
    "<environment_context", "<app-context", "<skills_instructions",
    "<recommended_plugins", "<user_instructions", "<INSTRUCTIONS",
    "# AGENTS.md instructions", "<permissions", "<collaboration_mode",
    "<session_context", "<steering", "<turn_context", "<developer_message",
)


def _trunc(text: Any, limit: int) -> tuple[str, bool]:
    if not isinstance(text, str):
        text = json.dumps(text, ensure_ascii=False)
    if limit and len(text) > limit:
        return text[:limit], True
    return text, False


def frag(
    harness: str,
    store: str,
    session_id: str,
    seq: int,
    kind: str,
    native_type: str,
    locator: dict[str, Any],
    *,
    text: Any = None,
    ts: Any = None,
    authorship: str = "unknown",
    visible: bool | None = None,
    basis: str = "",
    model: str | None = None,
    provider: str | None = None,
    cwd: str | None = None,
    rel: dict[str, Any] | None = None,
    phase: str | None = None,
    status: str | None = None,
    error: bool = False,
    usage: Any = None,
    interrupted: bool = False,
    origin: str | None = None,
    origin_basis: str | None = None,
    native: dict[str, Any] | None = None,
    max_chars: int = 400,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "harness": harness,
        "store": store,
        "session_id": session_id,
        "seq": seq,
        "kind": kind,
        "native_type": native_type,
        "locator": locator,
    }
    if text is not None:
        out["text"], truncated = _trunc(text, max_chars)
        if truncated:
            out["truncated"] = True
    if origin is not None:
        out["origin"] = origin
        if origin_basis:
            out["origin_basis"] = origin_basis
    if native is not None:
        out["native"] = native
    if ts is not None:
        out["ts"] = _iso(ts)
    if authorship != "unknown":
        out["authorship"] = authorship
    if visible is not None:
        out["visible"] = visible
    if basis:
        out["basis"] = basis
    if model:
        out["model"] = model
    if provider:
        out["provider"] = provider
    if cwd:
        out["cwd"] = cwd
    if rel:
        out["rel"] = rel
    if phase:
        out["phase"] = phase
    if status:
        out["status"] = status
    if error:
        out["error"] = True
    if usage is not None:
        out["usage"] = _compact_usage(usage)
    if interrupted:
        out["interrupted"] = True
    return out


def _compact_usage(u: Any) -> dict[str, int] | None:
    """Reduce harness usage dicts to {input, output, cache_read, cache_write, total}."""
    if not isinstance(u, dict):
        return {"total": u} if isinstance(u, (int, float)) else None
    out: dict[str, int] = {}
    for dst, keys in (
        ("input", ("input_tokens", "input", "prompt_tokens")),
        ("output", ("output_tokens", "output", "completion_tokens")),
        ("cache_read", ("cache_read_input_tokens", "cache_read")),
        ("cache_write", ("cache_creation_input_tokens", "cache_write")),
        ("total", ("total_tokens", "total", "tokens_used")),
    ):
        for k in keys:
            v = u.get(k)
            if isinstance(v, (int, float)) and v:
                out[dst] = int(v)
                break
    return out or None


def _iso(value: Any) -> str | None:
    """Normalize a timestamp (ISO str, epoch s/ms) to ISO-8601 UTC."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        seconds = value / 1000.0 if value > 1e12 else float(value)
        try:
            return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat().replace("+00:00", "Z")
        except (OverflowError, OSError, ValueError):
            return str(value)
    if isinstance(value, str):
        return value
    return str(value)


def parse_time_bound(value: str | None) -> float | None:
    """--since/--until: ISO-8601 (naive = local tz) or epoch seconds."""
    if not value:
        return None
    value = value.strip()
    if re.fullmatch(r"\d+(\.\d+)?", value):
        epoch = float(value)
        return epoch / 1000.0 if epoch > 1e12 else epoch
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        raise SystemExit(f"invalid time bound: {value!r} (expected ISO-8601 or epoch)")
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt.timestamp()


def ts_epoch(value: Any) -> float | None:
    """Parse a stored timestamp back to epoch seconds for filtering."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return value / 1000.0 if value > 1e12 else float(value)
    if isinstance(value, str):
        try:
            return parse_time_bound(value)
        except SystemExit:
            return None
    return None


# ---------------------------------------------------------------------------
# IO helpers
# ---------------------------------------------------------------------------

def _jsonl_lines(chunks: Iterable[bytes], max_lines: int | None = None) -> Iterator[tuple[str, bool]]:
    """Decode chunks into newline-delimited strings without splitting JSON's Unicode separators.

    The boolean is true when a line ended in LF. At most one line is retained,
    in addition to the current input chunk; a line itself is necessarily parsed
    as one JSON value and may therefore be arbitrarily large.
    """
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    pending: list[str] = []
    lines = 0
    if max_lines is not None and max_lines <= 0:
        return
    for raw in chunks:
        text = decoder.decode(raw)
        # Scan by index: re-slicing the remainder after every line copies the
        # chunk once per line, which is quadratic on files of short lines.
        start = 0
        while True:
            newline = text.find("\n", start)
            if newline < 0:
                if start < len(text):
                    pending.append(text[start:])
                break
            pending.append(text[start:newline])
            line = "".join(pending)
            pending.clear()
            yield line, True
            lines += 1
            if max_lines is not None and lines >= max_lines:
                return
            start = newline + 1
    tail = decoder.decode(b"", final=True)
    if tail:
        pending.append(tail)
    if pending and (max_lines is None or lines < max_lines):
        line = "".join(pending)
        pending.clear()
        yield line, False


def _bounded_chunks(fh, size: int, chunk_size: int = 64 * 1024) -> Iterator[bytes]:
    """Read no more than size bytes from an already-open binary file."""
    remaining = size
    while remaining:
        chunk = fh.read(min(chunk_size, remaining))
        if not chunk:
            break
        remaining -= len(chunk)
        yield chunk


class _LimitedFile(io.RawIOBase):
    """A non-owning, non-seekable view bounded to an already opened file size."""
    def __init__(self, fh, size: int):
        super().__init__()
        self.fh = fh
        self.remaining = size

    def readable(self) -> bool:
        return True

    def readinto(self, target) -> int:
        data = self.fh.read(min(len(target), self.remaining))
        target[:len(data)] = data
        self.remaining -= len(data)
        return len(data)


def iter_jsonl(path: Path, stats: dict[str, int], max_lines: int | None = None) -> Iterator[tuple[int, dict[str, Any]]]:
    """Yield (line_no, record) for each parseable JSON object line.

    Reads are bounded to the file size observed at open time (or to
    max_lines lines when a listing only needs the head); a truncated
    trailing line is reported via stats['truncated_tail'], malformed interior
    lines via stats['malformed'].
    """
    compressed = path.name.endswith(".jsonl.zst")
    line_no = 0
    try:
        if compressed:
            # Python 3.14 includes Zstandard; do not silently skip archives on
            # older runtimes or treat a corrupt stream as a complete scan.
            from compression import zstd
            try:
                with path.open("rb") as raw:
                    before = os.fstat(raw.fileno())
                    with zstd.open(io.BufferedReader(_LimitedFile(raw, before.st_size)), "rb") as check:
                        while check.read(64 * 1024):
                            pass
                    raw.seek(0)
                    with zstd.open(io.BufferedReader(_LimitedFile(raw, before.st_size)), "rb") as fh:
                        chunks = iter(lambda: fh.read(64 * 1024), b"")
                        for line, terminated in _jsonl_lines(chunks, max_lines):
                            line_no += 1
                            if not terminated and max_lines is None and line.strip():
                                stats["truncated_tail"] = stats.get("truncated_tail", 0) + 1
                            record = _parse_jsonl_line(line, stats)
                            if record is not None:
                                yield line_no, record
                    after = os.fstat(raw.fileno())
                    fields = ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns')
                    if any(getattr(before, field) != getattr(after, field) for field in fields):
                        stats['changed_during_read'] = stats.get('changed_during_read', 0) + 1
            except zstd.ZstdError as exc:
                raise ValueError(str(exc)) from exc
        else:
            with path.open("rb") as fh:
                before = os.fstat(fh.fileno())
                size = before.st_size
                for line, terminated in _jsonl_lines(_bounded_chunks(fh, size), max_lines):
                    line_no += 1
                    if not terminated and max_lines is None and line.strip():
                        stats["truncated_tail"] = stats.get("truncated_tail", 0) + 1
                    record = _parse_jsonl_line(line, stats)
                    if record is not None:
                        yield line_no, record
                after = os.fstat(fh.fileno())
                fields = ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns')
                if any(getattr(before, field) != getattr(after, field) for field in fields):
                    stats['changed_during_read'] = stats.get('changed_during_read', 0) + 1
    except (OSError, ImportError, EOFError, ValueError) as exc:
        stats["unreadable"] = stats.get("unreadable", 0) + 1
        if compressed:
            stats["compressed_errors"] = stats.get("compressed_errors", 0) + 1
            detail = "Zstandard requires Python 3.14+" if isinstance(exc, ImportError) else str(exc)
            print(f"cannot read compressed session {path}: {detail}", file=sys.stderr)


def _parse_jsonl_line(line: str, stats: dict[str, int]) -> dict[str, Any] | None:
    line = line.strip()
    if not line:
        return None
    try:
        record = json.loads(line)
    except json.JSONDecodeError:
        stats["malformed"] = stats.get("malformed", 0) + 1
        return None
    if isinstance(record, dict):
        return record
    stats["malformed"] = stats.get("malformed", 0) + 1
    return None


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return None


def tail_line(path: Path, chunk: int = 8192) -> str | None:
    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            fh.seek(max(0, size - chunk))
            data = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return None
    lines = [ln for ln in data.splitlines() if ln.strip()]
    return lines[-1] if lines else None


def open_sqlite_ro(path: Path) -> sqlite3.Connection:
    """Short-lived read-only connection. Never immutable=1 (unsafe on live DBs),
    never manual WAL parsing. Raises on failure → caller reports unavailable."""
    # as_posix() keeps Windows paths valid inside a file: URI (C:/x/y.db)
    conn = sqlite3.connect(
        f"file:{urllib.parse.quote(path.as_posix(), safe='/:')}?mode=ro",
        uri=True, timeout=2.0)
    conn.execute("PRAGMA query_only=ON")
    return conn


def table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def table_columns(conn: sqlite3.Connection, name: str) -> set[str]:
    try:
        return {r[1] for r in conn.execute(f"PRAGMA table_info({name})")}
    except sqlite3.Error:
        return set()


def content_blocks_text(content: Any, kinds: tuple[str, ...]) -> list[str]:
    if isinstance(content, str):
        return [content] if "text" in kinds else []
    if not isinstance(content, list):
        return []
    return [
        str(b.get(kinds[0] if kinds == ("text",) else "text") or b.get("text") or b.get("thinking") or "")
        for b in content
        if isinstance(b, dict) and b.get("type") in kinds
    ]


# ---------------------------------------------------------------------------
# Harness descriptors
# ---------------------------------------------------------------------------

# evidence: verified = reader validated against live data on a dev machine;
#           spec    = reader written from reference-repo schemas/fixtures only
HARNESS_EVIDENCE = {
    "claude-code": "verified",
    "codex": "verified",
    "opencode": "verified",
    "grok": "verified",
    "devin": "verified",
    "gemini": "verified",
    "porch": "verified",
    "claude-desktop": "verified",
    "cursor": "spec",
    "qwen-code": "spec",
    "kimi-code": "spec",
    "omp": "spec",
    "hermes": "spec",
    "agy": "verified",
}

ENV_ROOT = "PORCH_HISTORY_ROOT_"


def _root_override(slug: str) -> Path | None:
    raw = os.environ.get(ENV_ROOT + slug.upper().replace("-", "_"))
    return Path(raw).expanduser() if raw else None


def _steer_root() -> Path:
    env = os.environ.get("PORCH_STEER_DIR")
    if env:
        return Path(env).expanduser()
    if IS_MACOS:
        return HOME / "Library" / "Caches" / "pragmatic-orchestration" / "steer"
    if IS_WINDOWS:
        return _win_localapp() / "pragmatic-orchestration" / "steer"
    return Path(os.environ.get("XDG_CACHE_HOME", HOME / ".cache")) / "pragmatic-orchestration" / "steer"


def _walk(root: Path, pattern: str, stats: dict[str, int] | None = None, *, include_dirs: bool = True) -> Iterator[Path]:
    """root.rglob(pattern) that follows directory symlinks, each real dir once.

    Path.rglob does not descend into symlinked directories; several history
    stores keep project folders as links into an archive volume, so session
    discovery must follow them. Every directory is tracked by (st_dev,
    st_ino): a symlink loop or two links to one directory are walked once,
    under the alphabetically first link name for determinism. Matching runs
    on the walked path under root — never the resolved target — so session
    ids keep their store-relative form, and symlinked files match like real
    ones. Like rglob, directories whose own name matches are yielded too.
    A link to a real directory also under root is always pruned — the real
    path reports those files, keeping ids identical to what rglob saw.
    Unreadable directories are skipped and counted in
    stats['unreadable_dirs'].
    """
    parts = tuple(p for p in pattern.split("/") if p not in ("", "."))
    if not parts:
        return
    froot = os.fspath(root)
    if not os.path.isdir(froot):
        return  # rglob on a missing root yields nothing, silently
    depth = len(parts)

    def _tail_match(rel_parts: tuple[str, ...]) -> bool:
        # rglob(pattern) is glob("**/" + pattern): the pattern's components
        # match the tail of the relative path at any depth.
        if len(rel_parts) < depth:
            return False
        return all(fnmatch.fnmatch(name, pat) for name, pat in zip(rel_parts[-depth:], parts))

    def _dir_key(dirpath: str) -> tuple[int, int] | None:
        try:
            st = os.stat(dirpath)
        except OSError:
            return None
        return (st.st_dev, st.st_ino)

    visited: set[tuple[int, int]] = set()
    root_key = _dir_key(froot)
    if root_key is not None:
        visited.add(root_key)

    # Inodes of real directories reachable without crossing a link: a
    # symlink to one is pruned so ids stay on the real path rglob used.
    real_keys: set[tuple[int, int]] = set()
    for dirpath, _dirs, _files in os.walk(froot, followlinks=False):
        key = _dir_key(dirpath)
        if key is not None:
            real_keys.add(key)

    def _onerror(_exc: OSError) -> None:
        if stats is not None:
            stats["unreadable_dirs"] = stats.get("unreadable_dirs", 0) + 1

    for dirpath, dirnames, filenames in os.walk(froot, followlinks=True, onerror=_onerror):
        dirnames.sort()
        relative = os.path.relpath(dirpath, froot)
        rel = () if relative == os.curdir else tuple(relative.split(os.sep))
        # Entries are matched before pruning so a deduplicated link still
        # yields its own path, exactly as rglob lists it without descending.
        dir_matches = [name for name in dirnames if _tail_match(rel + (name,))]
        kept = []
        for name in dirnames:
            full = os.path.join(dirpath, name)
            key = _dir_key(full)
            if key is not None and key in visited:
                continue
            if (key is not None and key in real_keys
                    and os.path.islink(full)):
                continue
            if key is not None:
                visited.add(key)
            kept.append(name)
        dirnames[:] = kept
        for name in filenames:
            full = os.path.join(dirpath, name)
            if os.path.islink(full) and not os.path.exists(full) and stats is not None:
                stats['unreadable_dirs'] = stats.get('unreadable_dirs', 0) + 1
                stats['broken_symlinks'] = stats.get('broken_symlinks', 0) + 1
            if _tail_match(rel + (name,)):
                yield Path(full)
        if include_dirs:
            for name in dir_matches:
                yield Path(dirpath) / name


class Store:
    """One physical history store a reader knows about."""

    def __init__(self, harness: str, name: str, path: Path, kind: str):
        self.harness = harness
        self.name = name
        self.path = path
        self.kind = kind  # jsonl_dir | session_dirs | sqlite | json_dir | run_registry
        self.status = "ok" if path.exists() else "missing"
        self.error: str | None = None
        self.walk_stats: dict[str, int] = {}  # _walk diagnostics (unreadable_dirs)

    def as_dict(self) -> dict[str, Any]:
        out = {
            "harness": self.harness,
            "store": self.name,
            "path": str(self.path),
            "kind": self.kind,
            "status": self.status,
            "error": self.error,
            "evidence": HARNESS_EVIDENCE.get(self.harness, "spec"),
        }
        if self.walk_stats.get("unreadable_dirs"):
            out["unreadable_dirs"] = self.walk_stats["unreadable_dirs"]
        return out


def stores_for(harness: str) -> list[Store]:
    ov = _root_override(harness)
    if harness == "hermes":
        base = ov or (HOME / ".hermes")
        return [Store(harness, "state.db", base if base.suffix == ".db" else base / "state.db", "sqlite")]
    if harness == "claude-code":
        roots = [ov] if ov else [HOME / ".claude" / "projects", HOME / ".claude.back" / "projects"]
        return [Store(harness, f"{r.parent.name}/{r.name}", r, "jsonl_dir") for r in roots]
    if harness == "codex":
        if ov:
            return [Store(harness, "sessions", ov, "jsonl_dir")]
        codex_home = Path(os.environ.get("CODEX_HOME") or (HOME / ".codex"))
        out = [
            Store(harness, "sessions", codex_home / "sessions", "jsonl_dir"),
            Store(harness, "archived", codex_home / "archived_sessions", "jsonl_dir"),
        ]
        for p in sorted(codex_home.glob("state_*.sqlite")):
            out.append(Store(harness, f"state:{p.name}", p, "sqlite"))
        out.append(Store(harness, "catalog", codex_home / "sqlite" / "codex-dev.db", "sqlite"))
        out.append(Store(harness, "summaries",
                         codex_home / "sqlite" / "codex-thread-summaries-dev.db", "sqlite"))
        out.append(Store(harness, "history", codex_home / "history.jsonl", "jsonl_dir"))
        return out
    if harness == "opencode":
        def _storage_store(path: Path) -> Store | None:
            # V1 file storage: storage/{session,message,part}. An override may
            # point at the storage dir itself or at the opencode data dir.
            if (path / "session").is_dir() and (path / "message").is_dir():
                return Store(harness, "storage", path, "json_dir")
            nested = path / "storage"
            if nested.is_dir():
                return Store(harness, "storage", nested, "json_dir")
            return None
        if ov:
            if ov.is_file() or ov.suffix in (".db", ".sqlite", ".sqlite3"):
                return [Store(harness, "db", ov, "sqlite")]
            if (ov / "session").is_dir() and (ov / "message").is_dir():
                return [_storage_store(ov)]  # type: ignore[list-item]
            out = [Store(harness, "db", ov / "opencode.db", "sqlite")]
            storage = _storage_store(ov)
            if storage is not None:
                out.append(storage)
            return out
        base = Path(os.environ.get("XDG_DATA_HOME") or (HOME / ".local" / "share")) / "opencode"
        configured = os.environ.get("OPENCODE_DB")
        if configured == ":memory:":
            paths = []
        elif configured is not None:
            paths = [Path(os.path.abspath(base / configured))]
        else:
            # Stable and non-stable OpenCode channels can use different files.
            paths = [base / "opencode.db", *sorted(base.glob("opencode-*.db"))]
        out = [Store(harness, "db" if i == 0 else f"db:{path.name}", path, "sqlite")
               for i, path in enumerate(paths)]
        storage = _storage_store(base)
        if storage is not None:
            out.append(storage)
        return out
    if harness == "grok":
        return [Store(harness, "sessions", ov or (HOME / ".grok" / "sessions"), "session_dirs")]
    if harness == "devin":
        if ov:
            return [
                Store(harness, "sessions.db", ov / "sessions.db", "sqlite"),
                Store(harness, "transcripts", ov / "transcripts", "json_dir"),
            ]
        if IS_WINDOWS:
            # No verified Windows install yet: docs map ~/.config/devin ->
            # %APPDATA%\devin, so offer Roaming first and LocalAppData as fallback.
            bases = [_win_appdata() / "devin" / "cli",
                     _win_localapp() / "devin" / "cli"]
        else:
            bases = [HOME / ".local" / "share" / "devin" / "cli"]
        out = []
        for base in bases:
            suffix = "" if len(bases) == 1 else f":{base.parent.parent.name.lower()}"
            out.append(Store(harness, f"sessions.db{suffix}", base / "sessions.db", "sqlite"))
            out.append(Store(harness, f"transcripts{suffix}", base / "transcripts", "json_dir"))
        return out
    if harness == "gemini":
        base = ov or (HOME / ".gemini")
        return [
            Store(harness, "chats", base / "tmp", "json_dir"),
            Store(harness, "projects-map", base / "projects.json", "json_dir"),
        ]
    if harness == "claude-desktop":
        base = ov or (_win_appdata() / "Claude" if IS_WINDOWS
                      else HOME / ".config" / "Claude" if not IS_MACOS
                      else HOME / "Library" / "Application Support" / "Claude")
        if ov:
            return [Store(harness, "cowork", base, "session_dirs")]
        return [
            Store(harness, "cowork", base / "local-agent-mode-sessions", "session_dirs"),
            Store(harness, "code-sessions", base / "claude-code-sessions", "json_dir"),
        ]
    if harness == "cursor":
        if ov:
            return [Store(harness, "acp" if ov.is_dir() else "custom", ov,
                          "session_dirs" if ov.is_dir() else "sqlite")]
        base = (_win_appdata() / "Cursor" / "User" if IS_WINDOWS
                else HOME / ".config" / "Cursor" / "User" if not IS_MACOS
                else HOME / "Library" / "Application Support" / "Cursor" / "User")
        out = [Store(harness, "globalStorage", base / "globalStorage" / "state.vscdb", "sqlite")]
        out.append(Store(harness, "acp", HOME / ".cursor" / "acp-sessions", "session_dirs"))
        ws = base / "workspaceStorage"
        if ws.is_dir():
            for sub in sorted(ws.iterdir()):
                db = sub / "state.vscdb"
                if db.exists():
                    out.append(Store(harness, f"workspace:{sub.name[:8]}", db, "sqlite"))
        return out
    if harness == "qwen-code":
        return [Store(harness, "projects", ov or (HOME / ".qwen" / "projects"), "jsonl_dir")]
    if harness == "kimi-code":
        home = os.environ.get("KIMI_CODE_HOME")
        return [Store(harness, "sessions", ov or Path(home or (HOME / ".kimi-code")) / "sessions", "session_dirs")]
    if harness == "omp":
        return [Store(harness, "sessions", ov or (HOME / ".omp" / "agent" / "sessions"), "jsonl_dir")]
    if harness == "porch":
        return [Store(harness, "runs", _steer_root() / "runs", "run_registry")]
    if harness == "agy":
        base = ov or (HOME / ".gemini" / "antigravity-cli")
        # An override may name the CLI root itself or its ~/.gemini parent.
        if not (base / "cache").exists() and (base / "antigravity-cli").is_dir():
            base = base / "antigravity-cli"
        return [Store(harness, "cli", base, "agy_root")]
    return []


ALL_HARNESSES = [
    "claude-code", "codex", "opencode", "grok", "devin", "gemini",
    "claude-desktop", "cursor", "qwen-code", "kimi-code", "omp", "porch", "hermes",
    "agy",
]


# A source describes a physical revision without reading any session content.
class Source:
    def __init__(self, key: tuple, role: str, stat: tuple | None,
                 session_id: str | None = None, wal_stat: tuple | None = None,
                 error: str | None = None):
        self.key, self.role, self.stat = key, role, stat
        self.session_id, self.wal_stat, self.error = session_id, wal_stat, error


def _source_stat(path: Path, errors: list[str] | None = None) -> tuple | None:
    try:
        st = path.stat()
        return st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns
    except FileNotFoundError:
        return None
    except OSError as exc:
        if errors is not None:
            errors.append(f"{path}: {exc}")
        return None


def _file_source(path: Path, role: str, sid: str | None = None) -> Source:
    errors: list[str] = []
    stat = _source_stat(path, errors)
    return Source((str(path),), role, stat, sid, error="; ".join(errors) or None)


def _store_sources(store: Store) -> Iterator[Source]:
    """Enumerate physical sources only; shared DB rows are discovered by fingerprints."""
    root, h = store.path, store.harness
    if store.kind == "sqlite":
        errors: list[str] = []
        db_stat = _source_stat(root, errors)
        wal_stat = _source_stat(Path(str(root) + "-wal"), errors)
        if db_stat is not None or errors:
            yield Source((str(root),), "content", db_stat, wal_stat=wal_stat, error="; ".join(errors) or None)
        return
    if root.is_file():
        yield _file_source(root, "content")
        return
    if not root.is_dir():
        return
    # One traversal per store retains _walk's symlink and error semantics.
    for path in _walk(root, "*", store.walk_stats, include_dirs=False):
        rel = path.relative_to(root).parts if h in ('opencode', 'agy', 'porch') else ()
        role, sid = None, None
        if h == "claude-code" and path.suffix == ".jsonl":
            role, sid = "content", path.stem
        elif h == "codex" and re.match(r"rollout-\d{4}-\d\d-\d\dT\d\d-\d\d-\d\d-.+\.jsonl(?:\.zst)?$", path.name):
            role = "content"
            sid = _codex_filename_id(path.name)
        elif h == "claude-desktop":
            if path.name.startswith("local_") and path.suffix == ".json":
                role = "metadata"
            elif path.name == "audit.jsonl":
                role, sid = "content", path.parent.name
        elif h == "opencode" and path.suffix == ".json" and rel[0] in ("session", "message", "part"):
            role = "metadata" if rel[0] == "session" else "content"
            sid = path.stem if rel[0] == "session" else rel[1] if rel[0] == "message" and len(rel) > 2 else None
        elif h == "devin" and path.suffix == ".json":
            role, sid = "content", path.stem
        elif h == "agy":
            if rel == ("cache", "conversation_metadata.json"):
                role = "metadata"
            elif len(rel) == 2 and rel[0] == "annotations" and path.suffix == ".pbtxt":
                role, sid = "metadata", path.stem
            elif len(rel) >= 5 and rel[0] == "brain" and rel[-1] in ("transcript.jsonl", "transcript_full.jsonl"):
                role, sid = "content", rel[1]
            elif len(rel) >= 5 and rel[0] == "brain" and "subagents" in rel and path.suffix == ".json":
                role, sid = "lineage", path.stem
            elif len(rel) == 2 and rel[0] == "conversations" and path.suffix == ".db":
                role, sid = "metadata", path.stem
        elif h == "porch" and len(rel) >= 2:
            sid = rel[0]
            if len(rel) == 2 and path.name == "meta.json": role = "metadata"
            elif len(rel) == 2 and path.name == "final.txt": role = "content"
            elif len(rel) == 3 and rel[1] == "normalized" and path.suffix == ".jsonl": role = "content"
        elif h not in ("claude-code", "codex", "claude-desktop", "opencode", "devin", "agy", "porch"):
            role = "content"
        if role:
            source = _file_source(path, role, sid)
            if source.error:
                store.walk_stats["unreadable_dirs"] = store.walk_stats.get("unreadable_dirs", 0) + 1
            yield source


SOURCES = {harness: _store_sources for harness in ALL_HARNESSES}


def session_fingerprints(store: Store, conn: sqlite3.Connection) -> dict[str, tuple]:
    """Return session revisions using the caller's SQLite read transaction."""
    if store.harness == "devin":
        rows = conn.execute("SELECT s.id,s.last_activity_at,count(m.row_id),max(m.row_id),s.main_chain_id "
                            "FROM sessions s LEFT JOIN message_nodes m ON m.session_id=s.id GROUP BY s.id")
        return {row[0]: tuple(row[1:]) for row in rows}
    if store.harness == "opencode":
        rows = conn.execute("SELECT s.*,m.n,m.seq,m.updated,p.updated FROM session_v2 s "
                            "LEFT JOIN (SELECT session_id,count(*) n,max(seq) seq,max(time_updated) updated "
                            "FROM session_message GROUP BY session_id) m ON m.session_id=s.id "
                            "LEFT JOIN (SELECT session_id,max(time_updated) updated FROM session_part GROUP BY session_id) p ON p.session_id=s.id")
        columns = [d[0] for d in rows.description]
        idx = columns.index("id")
        return {row[idx]: tuple(row[:idx]) + tuple(row[idx + 1:]) for row in rows}
    raise ValueError("session fingerprints are supported for Devin and OpenCode only")


def sqlite_session_sources(store: Store, conn: sqlite3.Connection) -> Iterator[Source]:
    """Expand a changed DB into per-session sources inside the caller's read transaction."""
    errors: list[str] = []
    db_stat = _source_stat(store.path, errors)
    wal_stat = _source_stat(Path(str(store.path) + "-wal"), errors)
    def emit(sid: str, tables: tuple) -> Iterator[Source]:
        for table, role in tables:
            if table_exists(conn, table):
                yield Source((str(store.path), table, sid), role, db_stat, sid, wal_stat, "; ".join(errors) or None)
    if store.harness == "devin":
        for sid in session_fingerprints(store, conn):
            yield from emit(sid, (("sessions", "metadata"), ("message_nodes", "content")))
    elif store.harness == "opencode":
        v2_ids = {row[0] for row in conn.execute("SELECT id FROM session_v2")} if table_exists(conn, "session_v2") else set()
        for sid in v2_ids:
            yield from emit(sid, (("session_v2", "metadata"), ("session_message", "content"), ("session_part", "content")))
        if table_exists(conn, "session"):
            for (sid,) in conn.execute("SELECT id FROM session"):
                if sid not in v2_ids:
                    yield from emit(sid, (("session", "metadata"), ("message", "content"), ("part", "content")))
    else:
        raise ValueError("per-session SQLite sources are supported for Devin and OpenCode only")


class SessionRef:
    def __init__(self, harness: str, store: str, session_id: str, **kw: Any):
        self.harness = harness
        self.store = store
        self.session_id = session_id
        self.locator = kw.get("locator") or {}
        self.title = kw.get("title")
        self.cwd = kw.get("cwd")
        self.created = kw.get("created")
        self.updated = kw.get("updated")
        self.models = kw.get("models") or []
        self.parent = kw.get("parent")
        self.agent = kw.get("agent")          # subagent identity when present
        self.hidden = kw.get("hidden")
        self.extra = kw.get("extra") or {}
        # A light ref has its identity and locator but not the metadata that
        # takes a full read (title, dates, models); see LIGHT_READERS.
        self.light = False

    def matches(self, cwd: str | None, since: float | None, until: float | None) -> bool:
        if cwd and not (self.cwd and cwd in self.cwd):
            return False
        created, updated = ts_epoch(self.created), ts_epoch(self.updated)
        if since and updated is not None and updated < since:
            return False
        if until and created is not None and created > until:
            return False
        return True

    def as_dict(self) -> dict[str, Any]:
        out = {
            "harness": self.harness,
            "store": self.store,
            "session_id": self.session_id,
            "locator": self.locator,
        }
        for key in ("title", "cwd", "parent", "agent", "hidden"):
            value = getattr(self, key)
            if value is not None:
                out[key] = value
        if self.created:
            out["created"] = _iso(self.created)
        if self.updated:
            out["updated"] = _iso(self.updated)
        if self.models:
            out["models"] = sorted(set(self.models))
        out.update(self.extra)
        return out


# ---------------------------------------------------------------------------
# Extraction contract shared by readers and the session index
# ---------------------------------------------------------------------------
# Readers add what they know, so raw listings only gain fields:
# - SessionRef.extra keys: native_id, surface, session_class, class_basis,
#   edges, native_status, sources.
# - prompt fragments: origin (one of ORIGINS) and origin_basis.
# - assistant fragments: native (the format's own evidence, e.g. stop_reason).
# Record readers stay per-record (RawPrefilter feeds them candidate lines);
# logic that needs neighbouring records lives in TURN_RULES.

ORIGINS = ("human", "delegated", "injected", "unknown")
SESSION_CLASSES = ("interactive", "headless", "subagent", "unknown")
EDGE_TYPES = ("subagent", "fork", "continuation", "spawned", "caller_run",
              "worker_session", "parent_run", "alias")
# Stored edges point from child to parent except a run's worker edge.
EDGE_DIRECTION = {kind: ('forward' if kind == 'worker_session' else 'identity' if kind == 'alias' else 'reverse')
                  for kind in EDGE_TYPES}

_EXECUTOR_PREFIX: str | None = None


def executor_prefix() -> str:
    """First non-empty line of the delegate executor contract.

    `porch delegate` puts prompts/delegate-executor.txt in front of every new
    task, so a prompt starting with this line is a delegated brief.
    """
    global _EXECUTOR_PREFIX
    if _EXECUTOR_PREFIX is None:
        path = Path(__file__).resolve().parents[2] / "prompts" / "delegate-executor.txt"
        _EXECUTOR_PREFIX = ""
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    _EXECUTOR_PREFIX = line.strip()
                    break
        except OSError:
            pass
    return _EXECUTOR_PREFIX


def is_delegated_brief(text: Any) -> bool:
    prefix = executor_prefix()
    return bool(prefix) and isinstance(text, str) and text.lstrip().startswith(prefix)


TurnRule = Callable[["SessionRef", list[dict[str, Any]]], list[dict[str, Any]]]
TURN_RULES: dict[str, TurnRule] = {}


def turns_fallback(ref: SessionRef, fragments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Turns for a harness without native final-answer evidence.

    A turn starts at a prompt that is not injected; its final answer is the
    last non-empty assistant text before the next such prompt. The last turn
    without any assistant text is open.
    """
    turns: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None

    def close(turn: dict[str, Any], last: bool) -> dict[str, Any]:
        final = turn.pop("_last")
        turn["final"] = final
        turn["finals"] = [final] if final is not None else []
        turn["final_basis"] = "fallback" if final is not None else "none"
        turn["open"] = last and final is None
        return turn

    for frag in fragments:
        kind = frag.get("kind")
        if kind == "prompt" and frag.get("origin", "unknown") != "injected":
            if current is not None:
                turns.append(close(current, last=False))
            current = {"prompts": [frag], "_last": None, "ts_start": frag.get("ts"), "ts_end": frag.get("ts")}
        elif current is not None and kind == "assistant":
            text = frag.get("text")
            if isinstance(text, str) and text.strip():
                current["_last"] = frag
                current["ts_end"] = frag.get("ts") or current["ts_end"]
    if current is not None:
        turns.append(close(current, last=True))
    return turns


def interpret_session(store: Store, ref: SessionRef, fragments: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group a full session's fragments into turns with their final answers.

    Each turn has prompts, all completed finals, the last final, its basis,
    an open flag, and start/end timestamps.
    """
    rule = TURN_RULES.get(store.harness, turns_fallback)
    return rule(ref, list(fragments))


def refine_class(ref: SessionRef, turns: list[dict[str, Any]]) -> None:
    """Upgrade an unknown discovery class from the first genuine prompt."""
    if ref.extra.get("session_class") not in (None, "unknown") or not turns:
        return
    prompts = next((turn.get("prompts") for turn in turns if turn.get("prompts")), [])
    origin = prompts[0].get("origin") if prompts else None
    if origin == "delegated":
        ref.extra.update(session_class="headless", class_basis="executor-prefix")
    elif origin == "human":
        ref.extra.update(session_class="interactive", class_basis="genuine prompt")


def _answer_segments(fragments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep the leading segment so native answers before any prompt survive."""
    segments: list[dict[str, Any]] = []
    current: dict[str, Any] = {"prompts": [], "frags": [], "ts_start": None, "ts_end": None}
    for f in fragments:
        if f.get("kind") == "prompt" and f.get("origin", "unknown") != "injected":
            segments.append(current)
            current = {"prompts": [f], "frags": [f], "ts_start": f.get("ts"), "ts_end": f.get("ts")}
        else:
            current["frags"].append(f)
            current["ts_start"] = current["ts_start"] or f.get("ts")
            current["ts_end"] = f.get("ts") or current["ts_end"]
    segments.append(current)
    return segments


def _claude_turns(ref: SessionRef, fragments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    segments = _answer_segments(fragments)
    for index, segment in enumerate(segments):
        messages: dict[Any, dict[str, Any]] = {}
        interrupted = False
        for pos, f in enumerate(segment.pop("frags")):
            if f.get("interrupted"):
                interrupted = True
            native = f.get("native") or {}
            if not native or "stop_reason" not in native:
                continue
            mid = native.get("message_id") or ("seq", f.get("seq"), pos)
            item = messages.setdefault(mid, {"text": None, "reason": None, "order": pos})
            if f.get("kind") == "assistant" and str(f.get("text") or "").strip():
                item["text"] = f
            if native.get("stop_reason"):
                item["reason"] = native["stop_reason"]
                item["order"] = pos
        complete = sorted((v for v in messages.values() if v["text"] and v["reason"] in ("end_turn", "stop_sequence")), key=lambda v: v["order"])
        finals = [v["text"] for v in complete]
        basis = "native:" + complete[-1]["reason"] if complete else "none"
        if not finals and not interrupted and not any(v["reason"] for v in messages.values()) and segment["prompts"]:
            fallback = next((v["text"] for v in reversed(list(messages.values())) if v["text"]), None)
            if fallback:
                finals, basis = [fallback], "fallback"
        if not segment["prompts"] and not complete:
            continue
        final = finals[-1] if finals else None
        segment.update(finals=finals, final=final, final_basis=basis, open=bool(segment["prompts"]) and (interrupted or (index == len(segments) - 1 and final is None)))
        if interrupted:
            segment.update(aborted=True, abort_reason='interrupted')
        out.append(segment)
    return out


TURN_RULES["claude-code"] = _claude_turns


def _codex_turns(ref: SessionRef, fragments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # Pair exact event/response copies within native turn boundaries, even if
    # other records appear between them. Keep the event at the earlier position
    # so an unusually ordered mirror cannot strand earlier assistant text.
    prepared = list(fragments)
    start = 0
    boundaries = {"event_msg.task_started", "event_msg.task_complete", "event_msg.turn_complete", "event_msg.turn_aborted"}
    for end in range(len(prepared) + 1):
        if end != len(prepared) and prepared[end].get("native_type") not in boundaries:
            continue
        events: dict[str, list[int]] = {}
        for j in range(start, end):
            f = prepared[j]
            if f.get("native_type") == "event_msg.user_message":
                events.setdefault(f.get("text", ""), []).append(j)
        for j in range(start, end):
            f = prepared[j]
            if f.get("native_type") == "response_item.user" and f.get("kind") == "prompt":
                matches = events.get(f.get("text", "")) or []
                if matches:
                    k = matches.pop(0)
                    if k < j:
                        prepared[j] = {}
                    else:
                        prepared[j], prepared[k] = prepared[k], {}
        start = end + 1
    fragments = [f for f in prepared if f]
    # The two layers can be separated by context records; the event layer is
    # authoritative until another assistant or terminal event starts a new turn.
    selected: list[dict[str, Any]] = []
    for f in fragments:
        if f.get("kind") == "prompt" and selected:
            previous = next((x for x in reversed(selected) if x.get("kind") in ("prompt", "assistant") or x.get("native_type") in ("event_msg.task_started", "event_msg.task_complete", "event_msg.turn_complete", "event_msg.turn_aborted")), None)
            types = {f.get("native_type"), previous.get("native_type")} if previous else set()
            if types == {"response_item.user", "event_msg.user_message"}:
                if f.get("native_type") == "event_msg.user_message":
                    selected.remove(previous)
                else:
                    continue
        selected.append(f)
    turns = []
    segments = _answer_segments(selected)
    for i, turn in enumerate(segments):
        segment = turn.pop("frags")
        commentary = {f.get("text") for f in segment if f.get("kind") == "assistant" and (f.get("native") or {}).get("phase") == "commentary"}
        choices = [f for f in segment if f.get("kind") == "assistant" and f.get("text") and
                   (f.get("native") or {}).get("phase") != "commentary" and not (
                       (f.get("native") or {}).get("layer") == "response" and
                       not (f.get("native") or {}).get("phase") and f.get("text") in commentary)]
        candidates: list[tuple[dict[str, Any], str]] = []
        native_group: list[dict[str, Any]] = []
        choice_ids = {id(f) for f in choices}
        def flush() -> None:
            explicit = next((f for f in reversed(native_group) if (f.get("native") or {}).get("phase") == "final_answer"), None)
            saved = next((f for f in reversed(native_group) if (f.get("native") or {}).get("layer") in ("task_complete", "turn_complete")), None)
            final = explicit or saved
            if final:
                basis = "native:final_answer" if explicit else "native:" + final["native"]["layer"]
                if not any(old.get("text") == final.get("text") for old, _ in candidates):
                    candidates.append((final, basis))
            native_group.clear()
        for pos, f in enumerate(segment):
            nt = f.get("native_type")
            if nt == "event_msg.task_started" and native_group:
                flush()
            if id(f) in choice_ids:
                native_group.append(f)
                if (f.get("native") or {}).get("layer") in ("task_complete", "turn_complete"):
                    flush()
            if nt == "event_msg.turn_aborted":
                flush()
            elif nt in ("event_msg.task_complete", "event_msg.turn_complete"):
                following = segment[pos + 1] if pos + 1 < len(segment) else {}
                if (following.get("native") or {}).get("layer") not in ("task_complete", "turn_complete"):
                    flush()
        flush()
        finals = [f for f, _ in candidates]
        basis = candidates[-1][1] if candidates else "none"
        terminal = next((f for f in reversed(segment) if f.get("native_type") in
                         ("event_msg.turn_aborted", "event_msg.task_complete", "event_msg.turn_complete") or
                         (f.get("kind") == "assistant" and (f.get("native") or {}).get("phase") == "final_answer")), None)
        aborted = bool(terminal and terminal.get("native_type") == "event_msg.turn_aborted")
        abort_event = terminal if aborted else None
        if not finals and not aborted and not any((f.get("native") or {}).get("phase") == "final_answer" or (f.get("native") or {}).get("layer") in ("task_complete", "turn_complete") for f in choices) and turn["prompts"]:
            final = choices[-1] if choices else None
            if final:
                finals, basis = [final], "fallback"
        if not turn["prompts"] and not candidates:
            continue
        final = finals[-1] if finals else None
        closed = aborted or any(f.get("native_type") in ("event_msg.task_complete", "event_msg.turn_complete") for f in segment)
        turn.update(finals=finals, final=final, final_basis=basis, open=not closed and final is None and i == len(segments) - 1, aborted=aborted)
        reason = (abort_event.get("native") or {}).get("reason") if abort_event else None
        if reason is not None:
            turn["abort_reason"] = reason
        turns.append(turn)
    return turns


def _desktop_turns(ref: SessionRef, fragments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if ref.extra.get("surface") == "code-tab":
        return _claude_turns(ref, fragments)
    main = [f for f in fragments if not (f.get("rel") or {}).get("parent_tool_use_id")]
    if any(f.get("native_type") == "user" and f.get("kind") == "prompt" for f in main):
        main = [f for f in main if f.get("native_type") != "manifest.initialMessage"]
    turns = []
    segments = _answer_segments(main)
    for index, turn in enumerate(segments):
        finals = []
        pending = None
        results = 0
        for f in turn.pop("frags"):
            if f.get("kind") == "assistant" and str(f.get("text") or "").strip():
                pending = f
            elif str(f.get("native_type", "")).startswith("result."):
                results += 1
                if pending:
                    finals.append(pending)
                pending = None
        if not turn["prompts"] and not finals:
            continue
        turn.update(finals=finals, final=finals[-1] if finals else None, final_basis="native:result" if finals else "none", open=not results and index == len(segments) - 1)
        turns.append(turn)
    return turns


def _porch_turns(ref: SessionRef, fragments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    turns = turns_fallback(ref, fragments)
    final = next((f for f in reversed(fragments) if f.get("native_type") == "final.txt" and isinstance(f.get("text"), str) and f["text"].strip()), None)
    if final:
        if turns:
            turns[-1].update(finals=[final], final=final, final_basis="native:final.txt", open=False)
        else:
            # Run registries may retain the final answer but not the brief.
            turns.append({"prompts": [], "finals": [final], "final": final, "final_basis": "native:final.txt", "open": False,
                          "ts_start": final.get("ts"), "ts_end": final.get("ts")})
    return turns


TURN_RULES.update({"codex": _codex_turns, "claude-desktop": _desktop_turns, "porch": _porch_turns})


def _devin_turns(ref: SessionRef, fragments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    nodes = [f for f in fragments if "canonical" in (f.get("native") or {})]
    unknown_chain = bool(nodes) and all(f["native"]["canonical"] is None for f in nodes)
    selected = [f for f in nodes if unknown_chain or f["native"]["canonical"] is True]
    selected.sort(key=lambda f: (ts_epoch(f.get("ts")) if ts_epoch(f.get("ts")) is not None else float("inf"),
                                 (f.get("native") or {}).get("node_id", 0), f.get("seq", 0)))
    last: dict[str, dict[str, Any]] = {}
    for f in selected:
        mid = (f.get("native") or {}).get("message_id")
        if mid and f.get("kind") == "prompt" and f.get("origin") != "injected":
            last[mid] = f
    selected = [f for f in selected if f.get("kind") != "prompt" or f.get("origin") == "injected" or not (f.get("native") or {}).get("message_id") or last[f["native"]["message_id"]] is f]
    turns = []
    segments = _answer_segments(selected)
    for i, turn in enumerate(segments):
        segment = turn.pop("frags")
        finals = [f for f in segment if f.get("kind") == "assistant" and f.get("text") and (f.get("native") or {}).get("finish_reason") == "stop" and not f["native"].get("has_tool_calls")]
        marked = any(f.get("native_type") in ("message_nodes.assistant", "message_nodes.thinking", "message_nodes.tool_call", "message_nodes.assistant_status") and (f.get("native") or {}).get("finish_reason") is not None for f in segment)
        if finals:
            basis = "fallback" if unknown_chain else "native:stop"
        elif marked:
            basis = "none"
        else:
            final = next((f for f in reversed(segment) if f.get("kind") == "assistant" and isinstance(f.get("text"), str) and f["text"].strip()), None) if turn["prompts"] else None
            finals = [final] if final else []
            basis = "fallback" if finals else "none"
        if not turn["prompts"] and not finals:
            continue
        final = finals[-1] if finals else None
        turn.update(finals=finals, final=final, final_basis=basis, open=final is None and i == len(segments) - 1)
        turns.append(turn)
    return turns


TURN_RULES["devin"] = _devin_turns

_PORCH_WORKERS: dict[Path, set[tuple[str, str]]] = {}
BACKEND_HARNESS = {"claude-code": "claude-code", "codex-cli": "codex", "devin-cli": "devin", "opencode": "opencode", "antigravity-cli": "agy"}


def reset_discovery_caches() -> None:
    """Refresh process-local discovery snapshots before a new index sync."""
    _PORCH_WORKERS.clear()
    _CODEX_HOME_CACHE.clear()
    _ROLLOUT_INDEX.clear()
    _CLAUDE_TRANSCRIPTS.clear()


def porch_workers(root: Path | None = None) -> set[tuple[str, str]]:
    """Snapshot the run registry once per root for session classification."""
    root = root or _steer_root() / "runs"
    if root not in _PORCH_WORKERS:
        workers: set[tuple[str, str]] = set()
        if root.is_dir():
            for directory in root.iterdir():
                meta = read_json(directory / "meta.json")
                if isinstance(meta, dict):
                    harness = BACKEND_HARNESS.get(meta.get("backend"))
                    sid = meta.get("native_session_id")
                    if harness and sid:
                        workers.add((harness, str(sid)))
        _PORCH_WORKERS[root] = workers
    return _PORCH_WORKERS[root]


# ---------------------------------------------------------------------------
# claude-code
# ---------------------------------------------------------------------------

def claude_iter_sessions(store: Store) -> Iterator[SessionRef]:
    for path in sorted(_walk(store.path, "*.jsonl", store.walk_stats)):
        yield claude_ref_for_file(store, path)


def _claude_session_ref(store: Store, path: Path, *, light: bool = False) -> SessionRef:
    """Build a session's ref from its file.

    A light ref carries only what the path gives (id, locator, lineage) and
    reads nothing; the full ref needs one parse of the whole file, because a
    title record can come at any line.
    """
    rel = path.relative_to(store.path).with_suffix("").as_posix()
    project_dir = path.relative_to(store.path).parts[0] if len(path.relative_to(store.path).parts) > 1 else ""
    first_ts = last_ts = cwd = title = None
    models: list[str] = []
    parent = None
    agent = None
    agent_id = None
    human = delegated = False
    if not light:
        stats: dict[str, int] = {}
        for line_no, rec in iter_jsonl(path, stats):
            if first_ts is None:
                first_ts = rec.get("timestamp")
            if rec.get("timestamp"):
                last_ts = rec["timestamp"]
            t = rec.get("type")
            agent_id = agent_id or rec.get("agentId")
            if line_no <= 300 and t == "user" and not rec.get("isMeta") and not rec.get("isCompactSummary") and isinstance(rec.get("message"), dict):
                content = rec["message"].get("content")
                texts = [content] if isinstance(content, str) else [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"] if isinstance(content, list) else []
                texts = [s for s in texts if isinstance(s, str) and not s.lstrip().startswith(("[Request interrupted by user", *AUTOCONTEXT_MARKERS))]
                delegated |= any(is_delegated_brief(s) for s in texts)
                human |= bool(texts) and isinstance(rec.get("origin"), dict) and rec["origin"].get("kind") == "human"
            if cwd is None and rec.get("cwd"):
                cwd = rec["cwd"]
            if t == "custom-title" and rec.get("customTitle"):
                title = rec["customTitle"]
            elif title is None and t == "ai-title" and rec.get("aiTitle"):
                title = rec["aiTitle"]
            elif title is None and t == "summary" and rec.get("summary"):
                title = rec["summary"]
            if t == "assistant" and isinstance(rec.get("message"), dict):
                m = rec["message"].get("model")
                if m and m != "<synthetic>" and m not in models:
                    models.append(m)
    if "subagents/" in rel:
        parent = rel.split("/subagents/")[0]
        agent = path.stem
    worker = ("claude-code", path.stem) in porch_workers()
    ref = SessionRef(
        "claude-code", store.name, rel,
        locator={"file": str(path)},
        title=title, cwd=cwd,
        created=first_ts, updated=last_ts, models=models,
        parent=parent, agent=agent,
        extra={**({"project_dir": project_dir} if project_dir else {}),
               "native_id": path.stem, "surface": "cli",
               "launcher_key": launcher_instance("claude", path.stem),
               "session_class": "subagent" if parent else "headless" if delegated or worker else "interactive" if human else "unknown",
               "class_basis": "subagents/" if parent else "porch registry" if worker else "executor-prefix" if delegated else "origin.kind=human" if human else "no evidence",
               "edges": ([{"type": "subagent", "target_harness": "claude-code", "target_id": Path(parent).name,
                           "unresolved": None, "basis": "subagents/", "locator": {"file": str(path), "agentId": agent_id}}] if parent else []),
               "sources": [{"path": str(path), "role": "content"}]},
    )
    ref.light = light
    return ref


def claude_ref_for_file(store: Store, path: Path) -> SessionRef:
    return _claude_session_ref(store, path)


def _claude_head_cwd(path: Path) -> str | None:
    """The cwd a full parse would report: the first non-empty one in the file.

    It usually sits in the first records, so the read stops there.
    """
    for _line_no, rec in iter_jsonl(path, {}):
        if rec.get("cwd"):
            return rec["cwd"]
    return None


def _claude_light_sessions(store: Store, need_cwd: bool) -> Iterator[SessionRef]:
    for path in sorted(_walk(store.path, "*.jsonl", store.walk_stats)):
        ref = _claude_session_ref(store, path, light=True)
        if need_cwd:
            ref.cwd = _claude_head_cwd(path)
        yield ref


def _claude_hydrate(store: Store, ref: SessionRef) -> SessionRef:
    return _claude_session_ref(store, Path(ref.locator["file"]))


def claude_iter_fragments(store: Store, ref: SessionRef, max_chars: int, stats: dict[str, int]) -> Iterator[dict[str, Any]]:
    yield from _claude_record_fragments(store, ref, Path(ref.locator["file"]), iter_jsonl(Path(ref.locator["file"]), stats), max_chars)


def _claude_record_fragments(store: Store, ref: SessionRef, path: Path,
                             records: Iterable[tuple[int, dict[str, Any]]], max_chars: int) -> Iterator[dict[str, Any]]:
    """Fragments of the given records; each record's fragments depend on that record alone."""
    sidechain = "subagents/" in ref.session_id
    seq = 0
    for line_no, rec in records:
        seq += 1
        loc = {"file": str(path), "line": line_no}
        t = rec.get("type")
        ts = rec.get("timestamp")
        cwd = rec.get("cwd")
        rel_ids = {"uuid": rec.get("uuid"), "parent": rec.get("parentUuid"), "sessionId": rec.get("sessionId")}
        if rec.get("isSidechain") or sidechain:
            rel_ids["sidechain"] = True
        msg = rec.get("message") if isinstance(rec.get("message"), dict) else None

        if t == "user" and msg is not None:
            content = msg.get("content")
            origin_kind = (rec.get("origin") or {}).get("kind") if isinstance(rec.get("origin"), dict) else None
            if isinstance(content, str):
                blocks = [{"type": "text", "text": content}]
            elif isinstance(content, list):
                blocks = [b for b in content if isinstance(b, dict)]
            else:
                blocks = []
            auto = bool(rec.get("isMeta") or rec.get("isCompactSummary"))
            for b in blocks:
                seq += 1
                bt = b.get("type")
                text = b.get("text") or ("" if bt != "tool_result" else _tool_result_text(b))
                if bt == "tool_result" or isinstance(b.get("content"), list):
                    yield frag("claude-code", store.name, ref.session_id, seq, "tool_result", "user.tool_result", loc,
                               text=text, ts=ts, authorship="system", visible=False,
                               basis="tool_result block inside user-role record",
                               error=bool(b.get("is_error")),
                               cwd=cwd, rel={**rel_ids, "call_id": b.get("tool_use_id")}, max_chars=max_chars)
                else:
                    txt = b.get("text") or ""
                    interrupted = txt.startswith("[Request interrupted by user")
                    marker = interrupted or auto or any(txt.lstrip().startswith(m) for m in AUTOCONTEXT_MARKERS)
                    auth = "human" if origin_kind == "human" else ("system" if marker else ("agent" if origin_kind else "unknown"))
                    if auto:
                        origin, origin_basis = "injected", "isMeta" if rec.get("isMeta") else "isCompactSummary"
                    elif marker:
                        origin, origin_basis = "injected", "user interrupt" if interrupted else "wrapper"
                    elif is_delegated_brief(txt):
                        origin, origin_basis = "delegated", "executor-prefix"
                    elif origin_kind:
                        origin, origin_basis = {"human": "human", "peer": "delegated"}.get(origin_kind, "injected"), f"origin.kind={origin_kind}"
                    else:
                        origin, origin_basis = "unknown", "no origin"
                    yield frag("claude-code", store.name, ref.session_id, seq,
                               "context" if marker and auth == "system" else "prompt",
                               "user", loc, text=b.get("text"), ts=ts,
                               authorship=auth, visible=not marker, interrupted=interrupted,
                               basis=("user interrupt record" if interrupted
                                      else f"origin.kind={origin_kind!r} isMeta={auto}"),
                               cwd=cwd, rel=rel_ids, origin=origin, origin_basis=origin_basis, max_chars=max_chars)
        elif t == "assistant" and msg is not None:
            model = msg.get("model")
            usage = msg.get("usage")
            if not msg.get("content") and msg.get("stop_reason"):
                yield frag("claude-code", store.name, ref.session_id, seq, "metadata", "assistant.stop_reason", loc,
                           ts=ts, authorship="agent", native={"message_id": msg.get("id"), "stop_reason": msg.get("stop_reason")}, max_chars=max_chars)
            for b in msg.get("content") or []:
                if not isinstance(b, dict):
                    continue
                seq += 1
                bt = b.get("type")
                if bt == "thinking" or bt == "redacted_thinking":
                    yield frag("claude-code", store.name, ref.session_id, seq, "reasoning", "assistant.thinking", loc,
                               text=b.get("thinking") or b.get("data"), ts=ts, authorship="agent",
                               model=model, cwd=cwd, rel={**rel_ids, "message_id": msg.get("id")}, native={"message_id": msg.get("id"), "stop_reason": msg.get("stop_reason")}, max_chars=max_chars)
                elif bt == "tool_use" or bt == "server_tool_use":
                    yield frag("claude-code", store.name, ref.session_id, seq, "tool_call", f"assistant.{bt}", loc,
                               text=json.dumps({"name": b.get("name"), "input": b.get("input")}, ensure_ascii=False),
                               ts=ts, authorship="agent", model=model, cwd=cwd, usage=usage,
                               rel={**rel_ids, "call_id": b.get("id"), "message_id": msg.get("id"), "tool": b.get("name")}, native={"message_id": msg.get("id"), "stop_reason": msg.get("stop_reason")},
                               max_chars=max_chars)
                elif bt in ("text",):
                    yield frag("claude-code", store.name, ref.session_id, seq, "assistant", "assistant.text", loc,
                               text=b.get("text"), ts=ts, authorship="agent", model=model, cwd=cwd,
                               usage=usage,
                               rel={**rel_ids, "message_id": msg.get("id")}, native={"message_id": msg.get("id"), "stop_reason": msg.get("stop_reason")}, max_chars=max_chars)
                else:
                    yield frag("claude-code", store.name, ref.session_id, seq, "unknown", f"assistant.{bt}", loc,
                               text=b, ts=ts, model=model, cwd=cwd, rel=rel_ids, native={"message_id": msg.get("id"), "stop_reason": msg.get("stop_reason")}, max_chars=max_chars)
        elif t in ("summary", "custom-title", "ai-title"):
            yield frag("claude-code", store.name, ref.session_id, seq, "metadata", t, loc,
                       text=rec.get("summary") or rec.get("customTitle") or rec.get("aiTitle"),
                       ts=ts, authorship="system", visible=False, basis="title/bookkeeping record",
                       cwd=cwd, rel=rel_ids, max_chars=max_chars)
        elif t in ("permission-mode", "mode"):
            yield frag("claude-code", store.name, ref.session_id, seq, "permission", t, loc,
                       text=rec.get("permissionMode") or rec.get("mode"), ts=ts, authorship="system",
                       cwd=cwd, rel=rel_ids, max_chars=max_chars)
        elif t is not None:
            yield frag("claude-code", store.name, ref.session_id, seq, "metadata", str(t), loc,
                       text={k: v for k, v in rec.items() if k not in ("message",)} if t else None,
                       ts=ts, authorship="system", visible=False, cwd=cwd, rel=rel_ids, max_chars=max_chars)


def _text_value(value: Any) -> str:
    """A JSON value as text: strings as they are, null as nothing, the rest as JSON.

    Never Python's repr: "None" or "{'a': 1}" is text the session does not
    contain, and grep's raw prefilter relies on that.
    """
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    return json.dumps(value, ensure_ascii=False)


def _tool_result_text(block: dict[str, Any]) -> str:
    inner = block.get("content")
    if isinstance(inner, str):
        return inner
    if isinstance(inner, list):
        return "\n".join(_text_value(b.get("text", "")) for b in inner if isinstance(b, dict))
    return json.dumps(inner, ensure_ascii=False)


# ---------------------------------------------------------------------------
# codex-cli
# ---------------------------------------------------------------------------

def _codex_state_paths(home: Path, sources: dict[str, Path] | None = None) -> dict[str, str]:
    paths: dict[str, str] = {}
    for db in sorted(home.glob("state_*.sqlite"), reverse=True):
        try:
            conn = open_sqlite_ro(db)
            try:
                if {"id", "rollout_path"} <= table_columns(conn, "threads"):
                    for sid, path in conn.execute("SELECT id, rollout_path FROM threads WHERE rollout_path IS NOT NULL"):
                        if path:
                            if str(sid) not in paths:
                                paths[str(sid)] = path
                                if sources is not None:
                                    sources[str(sid)] = db
            finally:
                conn.close()
        except sqlite3.Error as exc:
            raise sqlite3.DatabaseError(f"cannot read Codex state database {db}: {exc}") from exc
    return paths


def _codex_state_edges(home: Path) -> dict[str, tuple[str, Path]]:
    edges: dict[str, tuple[str, Path]] = {}
    for db in sorted(home.glob("state_*.sqlite"), reverse=True):
        try:
            conn = open_sqlite_ro(db)
            try:
                if table_exists(conn, "thread_spawn_edges"):
                    for parent, child in conn.execute("SELECT parent_thread_id, child_thread_id FROM thread_spawn_edges"):
                        if parent and child:
                            edges.setdefault(str(child), (str(parent), db))
            finally:
                conn.close()
        except sqlite3.Error as exc:
            raise sqlite3.DatabaseError(f"cannot read Codex lineage database {db}: {exc}") from exc
    return edges


def _codex_resolve_stored_path(stored: str | None) -> Path | None:
    if stored:
        path = Path(stored)
        if path.is_file():
            return path
        for candidate in _codex_rollout_fallbacks(stored):
            if candidate.is_file():
                return candidate
    return None


_CODEX_HOME_CACHE: dict[tuple[Path, tuple[Path, ...]], tuple[dict[str, tuple[Path, list[str], str | None]], dict[str, tuple[str, Path]], dict[str, Path], dict[Path, list[Path]]]] = {}
_CODEX_ROLLOUT_NAME = re.compile(r"^rollout-(\d{4}-\d{2}-\d{2})T(\d{2})-(\d{2})-(\d{2})-(.+)\.jsonl(?:\.zst)?$")
_CODEX_UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"


def _codex_filename_id(name: str) -> str | None:
    match = _CODEX_ROLLOUT_NAME.match(name)
    if not match:
        return None
    identifier = match.group(5)
    # Some rollout filenames append a second UUID after an underscore.
    variant = re.fullmatch(rf"({_CODEX_UUID})_{_CODEX_UUID}", identifier)
    return variant.group(1) if variant else identifier


def _codex_revision_choices(roots: list[Path], state_paths: dict[str, str], stats: dict[str, int] | None = None,
                            paths_by_root: dict[Path, list[Path]] | None = None) -> dict[str, tuple[Path, list[str], str | None]]:
    """Choose one rollout per thread; alternatives remain separate sources."""
    grouped: dict[str, list[Path]] = {}
    discovered: set[Path] = set()
    for root in roots:
        for pattern in ("rollout-*.jsonl*",):
            for path in _walk(root, pattern, stats):
                if not path.name.endswith((".jsonl", ".jsonl.zst")):
                    continue
                if path.name.endswith(".zst") and path.with_suffix("").is_file():
                    continue
                if paths_by_root is not None:
                    paths_by_root.setdefault(root, []).append(path)
                discovered.add(path)
                match = _CODEX_ROLLOUT_NAME.match(path.name)
                sid = _codex_filename_id(path.name) if match else None
                if sid is None:
                    for _, rec in iter_jsonl(path, {}, max_lines=300):
                        if rec.get("type") == "session_meta":
                            payload = rec.get("payload") or {}
                            sid = payload.get("id") or payload.get("session_id")
                            break
                if sid:
                    grouped.setdefault(str(sid), []).append(path)
    resolved: dict[str, Path | None] = {}
    for sid, stored in state_paths.items():
        stored_path = Path(stored)
        sibling = stored_path.with_name(stored_path.name[:-4] if stored_path.name.endswith(".zst") else stored_path.name + ".zst")
        candidates = (stored_path, sibling, roots[-1] / stored_path.name, roots[-1] / sibling.name)
        path = next((candidate for candidate in candidates if candidate in discovered), None)
        if path is None or (path != stored_path and stored_path.is_file()):
            path = _codex_resolve_stored_path(stored)
        resolved[sid] = path
        if path and path not in grouped.setdefault(sid, []):
            grouped[sid].append(path)
    choices = {}
    for sid, paths in grouped.items():
        if not paths:
            continue
        paths = list(dict.fromkeys(paths))
        def mtime(path: Path) -> int:
            try:
                return path.stat().st_mtime_ns
            except OSError:
                return 0
        if len(paths) > 1:
            paths.sort(key=lambda p: (mtime(p), str(p)), reverse=True)
        preferred = resolved.get(sid)
        chosen = preferred if preferred in paths else paths[0]
        alternatives = [str(p) for p in paths if p != chosen]
        choices[sid] = (chosen, alternatives, "ambiguous" if preferred is None and alternatives else None)
    return choices


def _codex_home_data(home: Path, roots: list[Path], stats: dict[str, int] | None = None):
    """Snapshot discovery metadata once per home and root set, not per store."""
    key = (home, tuple(roots))
    if key not in _CODEX_HOME_CACHE:
        state_sources: dict[str, Path] = {}
        state_paths = _codex_state_paths(home, state_sources)
        state_edges = _codex_state_edges(home)
        paths_by_root: dict[Path, list[Path]] = {}
        choices = _codex_revision_choices(roots, state_paths, stats, paths_by_root)
        for paths in paths_by_root.values():
            paths.sort(key=lambda p: (p.name.endswith(".zst"), str(p)))
        _CODEX_HOME_CACHE[key] = choices, state_edges, state_sources, paths_by_root
    return _CODEX_HOME_CACHE[key]


def _codex_enrich_ref(ref: SessionRef, path: Path | None = None, *, alternatives: list[str] | None = None,
                      revision: str | None = None, state_parent: str | None = None,
                      state_db: Path | None = None, meta: dict[str, Any] | None = None,
                      delegated: bool = False) -> SessionRef:
    """Classify using only already-read head or state-row evidence."""
    meta = meta or {}
    delegated |= is_delegated_brief(ref.extra.get("first_user_message") or "")
    status: dict[str, Any] = {"alternates": alternatives or []}
    if revision:
        status["revision"] = revision
    source = meta.get("source") or ref.extra.get("source") or ref.extra.get("source_kind")
    source_kind, parent, _, _ = _codex_source_kind(source)
    parent_basis = 'thread_spawn.parent_thread_id'
    if not parent and source_kind == 'subagent' and meta.get('parent_thread_id'):
        parent, parent_basis = meta['parent_thread_id'], 'session_meta.parent_thread_id'
    edge_list = []
    locator = {"file": str(path)} if path and not ref.locator.get("db") else ref.locator
    for kind, target, basis, loc in (
        ("subagent", parent, parent_basis, locator),
        ("subagent", state_parent, "thread_spawn_edges", {"db": str(state_db or ref.locator.get("db")), "table": "thread_spawn_edges", "key": ref.session_id}),
        ("fork", meta.get("forked_from_id"), "forked_from_id", locator),
    ):
        if target and not any(edge['type'] == kind and edge['target_id'] == target for edge in edge_list):
            edge_list.append({"type": kind, "target_harness": "codex", "target_id": target, "unresolved": None, "basis": basis, "locator": loc})
    worker = ("codex", ref.session_id) in porch_workers()
    originator = meta.get("originator") or ref.extra.get("originator")
    interactive = originator in ("codex-tui", "codex_work_desktop", "Codex Desktop") or (not originator and source_kind == "vscode")
    is_subagent = source_kind == "subagent" or bool(state_parent)
    surface = "cli" if originator == "codex-tui" else "app" if originator in ("codex_work_desktop", "Codex Desktop") else "ide" if source_kind == "vscode" else None
    sources = [{**ref.locator, "role": "metadata"}] if ref.locator.get("db") else []
    if path:
        sources.append({"path": str(path), "role": "content"})
    if state_db:
        sources.append({"db": str(state_db), "table": "thread_spawn_edges", "key": ref.session_id, "role": "lineage"})
    sources.extend({"path": alternate, "role": "content"} for alternate in alternatives or [])
    ref.parent = ref.parent or parent or state_parent or meta.get("forked_from_id")
    ref.extra.update(native_id=ref.session_id, surface=surface,
                     launcher_key=launcher_instance("codex", ref.session_id),
                     session_class="subagent" if is_subagent else "headless" if worker or delegated else "interactive" if interactive else "unknown",
                     class_basis="thread_spawn" if is_subagent else "porch registry" if worker else "executor-prefix" if delegated else "interactive client" if interactive else "no evidence",
                     edges=edge_list, native_status=status, sources=sources)
    if path:
        ref.extra["selected_rollout"] = str(path)
    return ref

def codex_iter_sessions(store: Store) -> Iterator[SessionRef]:
    if store.kind == "sqlite":
        yield from _codex_sqlite_sessions(store)
        return
    if store.name == "history":
        yield from _codex_history_sessions(store)
        return
    home = store.path.parent if store.path.name in ("sessions", "archived_sessions") else store.path
    roots = [home / "sessions", home / "archived_sessions"] if store.path.name in ("sessions", "archived_sessions") else [store.path]
    try:
        choices, state_edges, state_sources, paths_by_root = _codex_home_data(home, roots, store.walk_stats)
    except sqlite3.Error as exc:
        store.status, store.error = "unavailable", str(exc)
        return
    paths = paths_by_root.get(store.path, [])
    shared = (choices, state_edges, state_sources)
    for path in paths:
        ref = codex_ref_for_rollout(store, path, shared)
        if ref is not None:
            yield ref


def codex_rollout_shared(store: Store, *, state_snapshot: tuple | None = None) -> tuple:
    home = store.path.parent if store.path.name in ("sessions", "archived_sessions") else store.path
    roots = [home / "sessions", home / "archived_sessions"] if store.path.name in ("sessions", "archived_sessions") else [store.path]
    if state_snapshot is not None:
        state_paths, edges, sources = state_snapshot
        return _codex_revision_choices(roots, state_paths, store.walk_stats), edges, sources
    choices, edges, sources, _ = _codex_home_data(home, roots, store.walk_stats)
    return choices, edges, sources


def codex_ref_for_rollout(store: Store, path: Path, shared: tuple) -> SessionRef | None:
    choices, state_edges, state_sources = shared
    if path.name.endswith(".zst") and path.with_suffix("").exists():
        return None  # the uncompressed rollout is the canonical copy
    # Cheap listing: session identity + dates come from the filename
    # (rollout-YYYY-MM-DDTHH-MM-SS-<id>.jsonl) and mtime; session_meta is
    # the first record, so a bounded head read suffices for cwd/lineage.
    # --since/--until filtering then skips full parses entirely.
    name_m = re.match(r"rollout-(\d{4}-\d{2}-\d{2})T(\d{2})-(\d{2})-(\d{2})-(.+)\.jsonl(?:\.zst)?$", path.name)
    created_name = f"{name_m.group(1)}T{name_m.group(2)}:{name_m.group(3)}:{name_m.group(4)}Z" if name_m else None
    try:
        updated = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat().replace("+00:00", "Z")
    except OSError:
        updated = None
    stats: dict[str, int] = {}
    meta = None
    first_ts = None
    models: list[str] = []
    delegated = False
    for line_no, rec in iter_jsonl(path, stats, max_lines=300):
        if first_ts is None:
            first_ts = rec.get("timestamp")
        t = rec.get("type")
        payload = rec.get("payload") or {}
        if t == "session_meta" and meta is None:
            meta = payload
        elif t == "turn_context" and payload.get("model"):
            if payload["model"] not in models:
                models.append(payload["model"])
        if t == "event_msg" and payload.get("type") == "user_message":
            delegated |= is_delegated_brief(payload.get("message") or "")
        elif t == "response_item" and payload.get("type") == "message" and payload.get("role") == "user":
            delegated |= is_delegated_brief(_codex_message_text(payload))
        if meta is not None and first_ts is not None and models:
            break
    if stats.get("unreadable"):
        store.status, store.error = "unavailable", f"cannot read session: {path}"
        return None
    meta = meta or {}
    source_kind, source_parent, _, _ = _codex_source_kind(meta.get("source"))
    lineage = {"parent_thread_id": source_parent} if source_kind == "subagent" else None
    parent = meta.get("parent_thread_id") or meta.get("forked_from_id") or (lineage or {}).get("parent_thread_id")
    sid = str(meta.get("id") or meta.get("session_id") or _codex_filename_id(path.name) or path.stem)
    choice = choices.get(sid)
    ref = SessionRef(
        "codex", store.name, sid,
        locator={"file": str(path)},
        cwd=meta.get("cwd"), created=first_ts or created_name or meta.get("timestamp"),
        updated=updated,
        models=models or ([meta["model_provider"]] if meta.get("model_provider") else []),
        parent=parent,
        agent=meta.get("agent_nickname") or meta.get("agent_path"),
        extra={k: meta[k] for k in ("originator", "cli_version") if meta.get(k)},
    )
    state_edge = state_edges.get(sid)
    _codex_enrich_ref(ref, path, alternatives=choice[1] if choice else [], revision=choice[2] if choice else None,
                      state_parent=state_edge[0] if state_edge else None, state_db=state_edge[1] if state_edge else None,
                      meta=meta, delegated=delegated)
    if choice:
        ref.extra["selected_rollout"] = str(choice[0])
        ref.extra["sources"] = [{"path": str(choice[0]), "role": "content"}] + [
            {"path": p, "role": "content"} for p in choice[1]
        ] + [s for s in ref.extra["sources"] if s.get("db")]
    if sid in state_sources:
        ref.extra["sources"].append({"db": str(state_sources[sid]), "table": "threads", "key": sid, "role": "metadata"})
    return ref


def _codex_source_kind(src: Any) -> tuple[str, str | None, str | None, str | None]:
    """threads.source / session_meta.source → (kind, parent_thread, nickname, path)."""
    if isinstance(src, dict):
        subagent = src.get("subagent")
        sp = subagent.get("thread_spawn") or {} if isinstance(subagent, dict) else {}
        if isinstance(sp, dict) and sp:
            return "subagent", sp.get("parent_thread_id"), sp.get("agent_nickname"), sp.get("agent_path")
        if subagent is not None:
            return "subagent", None, None, None
        return "other", None, None, None
    if isinstance(src, str) and src.startswith("{"):
        try:
            return _codex_source_kind(json.loads(src))
        except json.JSONDecodeError:
            return src, None, None, None
    return str(src or ""), None, None, None


def _codex_sqlite_sessions(store: Store) -> Iterator[SessionRef]:
    try:
        conn = open_sqlite_ro(store.path)
    except sqlite3.Error as e:
        store.status, store.error = "unavailable", str(e)
        return
    try:
        if store.name.startswith("state:"):
            if not table_exists(conn, "threads"):
                store.status, store.error = "unsupported", "no threads table"
                return
            edges: dict[str, str] = {}
            home = store.path.parent
            choices, _, state_sources, _ = _codex_home_data(home, [home / "sessions", home / "archived_sessions"])
            if table_exists(conn, "thread_spawn_edges"):
                try:
                    for p, c in conn.execute("SELECT parent_thread_id, child_thread_id FROM thread_spawn_edges"):
                        edges[c] = p
                except sqlite3.Error:
                    pass
            wanted = ["id", "title", "cwd", "created_at", "updated_at_ms", "rollout_path",
                      "source", "model", "reasoning_effort", "git_branch", "archived",
                      "tokens_used", "preview", "first_user_message", "agent_nickname",
                      "model_provider", "originator", "project_id", "name"]
            have = table_columns(conn, "threads")
            cols = [c for c in wanted if c in have]
            for row in conn.execute(f"SELECT {', '.join(cols)} FROM threads"):
                rec = dict(zip(cols, row))
                src_kind, parent, nick, agent_path = _codex_source_kind(rec.get("source"))
                parent = parent or edges.get(rec["id"])
                title = (rec.get("title") or rec.get("name") or rec.get("preview")
                         or rec.get("first_user_message") or "")[:160] or None
                ref = SessionRef(
                    "codex", store.name, rec["id"],
                    locator={"db": str(store.path), "table": "threads", "key": rec["id"]},
                    title=title, cwd=rec.get("cwd"), created=rec.get("created_at"),
                    updated=rec.get("updated_at_ms") or rec.get("created_at"),
                    models=[m for m in (rec.get("model"), rec.get("model_provider")) if m],
                    parent=parent, agent=rec.get("agent_nickname") or nick or agent_path,
                    extra={"rollout_path": rec.get("rollout_path"), "source": src_kind,
                           "effort": rec.get("reasoning_effort"), "git_branch": rec.get("git_branch"),
                           "archived": bool(rec.get("archived")), "tokens_used": rec.get("tokens_used"),
                           "originator": rec.get("originator"), "project_id": rec.get("project_id"),
                           "first_user_message": rec.get("first_user_message") or None},
                )
                choice = choices.get(rec["id"])
                path = choice[0] if choice else None if rec["id"] in state_sources else _codex_resolve_stored_path(rec.get("rollout_path"))
                _codex_enrich_ref(ref, path, alternatives=choice[1] if choice else [], revision=choice[2] if choice else None,
                                  state_parent=edges.get(rec["id"]), meta={"source": rec.get("source"), "originator": rec.get("originator")})
                yield ref
        elif store.name == "catalog":
            if not table_exists(conn, "local_thread_catalog"):
                store.status, store.error = "unsupported", "no local_thread_catalog table"
                return
            home = store.path.parent.parent
            choices, _, _, _ = _codex_home_data(home, [home / "sessions", home / "archived_sessions"])
            for row in conn.execute(
                "SELECT thread_id, display_title, cwd, source_created_at, source_updated_at, "
                "source_kind, git_branch, model_provider, project_id, conversation_origin, "
                "missing_candidate, host_id FROM local_thread_catalog"
            ):
                ref = SessionRef(
                    "codex", "catalog", row[0],
                    locator={"db": str(store.path), "table": "local_thread_catalog", "key": row[0]},
                    title=row[1] or None, cwd=row[2], created=row[3], updated=row[4],
                    models=[row[7]] if row[7] else [],
                    extra={"source_kind": row[5], "git_branch": row[6], "project_id": row[8],
                           "conversation_origin": row[9], "host_id": row[11],
                           "missing_candidate": bool(row[10])},
                )
                choice = choices.get(ref.session_id)
                _codex_enrich_ref(ref, choice[0] if choice else None, alternatives=choice[1] if choice else [], revision=choice[2] if choice else None)
                yield ref
        elif store.name == "summaries":
            if not table_exists(conn, "thread_turn_summaries"):
                store.status, store.error = "unsupported", "no thread_turn_summaries table"
                return
            home = store.path.parent.parent if store.path.parent.name == "sqlite" else store.path.parent
            choices, _, _, _ = _codex_home_data(home, [home / "sessions", home / "archived_sessions"])
            for row in conn.execute(
                "SELECT thread_id, summary, compact_summary, updated_at FROM thread_turn_summaries"
            ):
                ref = SessionRef(
                    "codex", "summaries", row[0],
                    locator={"db": str(store.path), "table": "thread_turn_summaries", "key": row[0]},
                    title=(row[2] or (row[1] or "")[:80]) or None, updated=row[3],
                    extra={"cloud_summary": True},
                )
                choice = choices.get(ref.session_id)
                _codex_enrich_ref(ref, choice[0] if choice else None, alternatives=choice[1] if choice else [], revision=choice[2] if choice else None)
                yield ref
    except sqlite3.Error as e:
        store.status, store.error = "unavailable", str(e)
    finally:
        conn.close()


def _codex_history_sessions(store: Store) -> Iterator[SessionRef]:
    """~/.codex/history.jsonl — flat {session_id, ts, text} prompt log."""
    if not store.path.is_file():
        store.status = "missing"
        return
    stats: dict[str, int] = {}
    agg: dict[str, dict[str, Any]] = {}
    for _ln, rec in iter_jsonl(store.path, stats):
        sid = str(rec.get("session_id") or "")
        if not sid:
            continue
        e = agg.setdefault(sid, {"n": 0, "first": None, "last": None, "title": None, "delegated": False})
        e["n"] += 1
        e["delegated"] |= is_delegated_brief(rec.get("text") or "")
        ts = rec.get("ts")
        if e["first"] is None:
            e["first"] = ts
            e["title"] = (rec.get("text") or "")[:80]
        if ts:
            e["last"] = ts
    for sid, e in agg.items():
        ref = SessionRef(
            "codex", "history", sid,
            locator={"file": str(store.path)},
            title=e["title"], created=e["first"], updated=e["last"],
            extra={"prompt_count": e["n"]},
        )
        _codex_enrich_ref(ref)
        if e["delegated"] and ref.extra["session_class"] != "headless":
            ref.extra["session_class"], ref.extra["class_basis"] = "headless", "executor-prefix"
        ref.extra["sources"] = [{"path": str(store.path), "role": "content"}]
        yield ref


_ROLLOUT_INDEX: dict[Path, list[tuple[dict[str, Path], dict[str, Path]]]] = {}


def _rollout_index(codex_home: Path) -> list[tuple[dict[str, Path], dict[str, Path]]]:
    """Per base directory, the first .jsonl and .jsonl.zst rollout for every id.

    One walk replaces two rglob calls per lookup (about 10 ms each on a
    real ~/.codex, thousands of lookups per grep). A name matches the glob
    rollout-*-<id>.jsonl for every id that follows one of its dashes, so
    each such suffix is indexed. The walk is a snapshot for this process: a
    rollout created after it is not found by index-store refs.
    """
    if codex_home not in _ROLLOUT_INDEX:
        bases = []
        for base in (codex_home / "sessions", codex_home / "archived_sessions"):
            plain: dict[str, Path] = {}
            packed: dict[str, Path] = {}
            if base.is_dir():
                for pattern, suffix, index in (("rollout-*.jsonl", ".jsonl", plain),
                                               ("rollout-*.jsonl.zst", ".jsonl.zst", packed)):
                    for path in _walk(base, pattern):
                        core = path.name[len("rollout-"):-len(suffix)]
                        for pos, char in enumerate(core):
                            if char == "-":
                                index.setdefault(core[pos + 1:], path)
                        sid = _codex_filename_id(path.name)
                        if sid:
                            index.setdefault(sid, path)
            bases.append((plain, packed))
        _ROLLOUT_INDEX[codex_home] = bases
    return _ROLLOUT_INDEX[codex_home]


def _find_codex_rollout(thread_id: str) -> Path | None:
    codex_home = Path(os.environ.get("CODEX_HOME") or (HOME / ".codex"))
    for plain, packed in _rollout_index(codex_home):
        hit = plain.get(thread_id) or packed.get(thread_id)
        if hit:
            return hit
    return None


def _codex_rollout_fallbacks(stored: str) -> Iterator[Path]:
    """Candidate locations for a state row's stale rollout_path, in order.

    First the same path with the other compression (.jsonl.zst rollouts are
    compressed in place), then the same basename — either compression — under
    archived_sessions, which is flat on disk. The archived root is the
    sibling of the 'sessions' component in the stored path when there is
    one, plus CODEX_HOME's. The caller's index lookup stays the last resort.
    """
    p = Path(stored)
    if p.name.endswith(".zst"):
        yield p.with_name(p.name[:-4])
    else:
        yield p.with_name(p.name + ".zst")
    names = [p.name, p.name[:-4] if p.name.endswith(".zst") else p.name + ".zst"]
    seen: set[Path] = set()
    for parent in p.parents:
        if parent.name == "sessions":
            root = parent.parent / "archived_sessions"
            if root not in seen:
                seen.add(root)
                for name in names:
                    yield root / name
            break
    root = Path(os.environ.get("CODEX_HOME") or (HOME / ".codex")) / "archived_sessions"
    if root not in seen:
        for name in names:
            yield root / name


def _codex_rollout_for(ref: SessionRef) -> str | None:
    """The rollout file codex_iter_fragments reads for a non-history ref, or None for a db-only thread."""
    file = ref.locator.get("file")
    if file:
        return file
    selected = ref.extra.get("selected_rollout")
    if selected and Path(selected).is_file():
        return selected
    rollout = ref.extra.get("rollout_path")
    if rollout:
        path = Path(rollout)
        if path.exists():
            return str(path)
        for cand in _codex_rollout_fallbacks(rollout):
            if cand.exists():
                return str(cand)
    rollout = _find_codex_rollout(ref.session_id)
    if rollout and Path(rollout).exists():
        return str(rollout)
    return None


def _codex_db_only_fragments(store: Store, ref: SessionRef, max_chars: int, stats: dict[str, int]) -> Iterator[dict[str, Any]]:
    """No rollout file — emit what the index layer still knows."""
    seq = 0
    info = {"title": ref.title, "cwd": ref.cwd, **{k: v for k, v in ref.extra.items()
            if k not in ("rollout_path",)}}
    yield frag("codex", store.name, ref.session_id, seq, "metadata", "index_row", ref.locator,
               text=info, ts=ref.created, authorship="system", visible=False,
               basis="no local rollout file — index/catalog row only", max_chars=max_chars)
    first = ref.extra.get("first_user_message")
    if first:
        seq += 1
        yield frag("codex", store.name, ref.session_id, seq, "prompt", "threads.first_user_message",
                   ref.locator, text=first, ts=ref.created, authorship="unknown", visible=True,
                   basis="index-level first prompt preview; full transcript unavailable",
                   origin="delegated" if is_delegated_brief(first) else "unknown", origin_basis="executor-prefix" if is_delegated_brief(first) else "threads.first_user_message",
                   max_chars=max_chars)
    sdb = store.path if store.name == "summaries" else None
    if sdb is None:
        for cand in (store.path.parent / "codex-thread-summaries-dev.db",
                     store.path.parent / "sqlite" / "codex-thread-summaries-dev.db"):
            if cand.exists():
                sdb = cand
                break
    if sdb and sdb.exists():
        try:
            conn = open_sqlite_ro(sdb)
            row = conn.execute(
                "SELECT summary, compact_summary, updated_at FROM thread_turn_summaries WHERE thread_id=?",
                (ref.session_id,),
            ).fetchone()
            conn.close()
        except sqlite3.Error:
            row = None
        if row:
            seq += 1
            yield frag("codex", store.name, ref.session_id, seq, "metadata", "thread_turn_summaries",
                       {"db": str(sdb), "table": "thread_turn_summaries", "key": ref.session_id},
                       text=row[0], ts=row[2], authorship="system", visible=True,
                       basis="cloud/app-generated thread summary", max_chars=max_chars)


def codex_iter_fragments(store: Store, ref: SessionRef, max_chars: int, stats: dict[str, int]) -> Iterator[dict[str, Any]]:
    if store.name == "history":
        seq = 0
        for line_no, rec in iter_jsonl(store.path, stats):
            if str(rec.get("session_id")) != ref.session_id:
                continue
            seq += 1
            yield frag("codex", "history", ref.session_id, seq, "prompt", "history.entry",
                       {"file": str(store.path), "line": line_no},
                       text=rec.get("text"), ts=rec.get("ts"), authorship="unknown",
                       visible=True, basis="prompt log entry (no per-record authorship proof)",
                       origin="delegated" if is_delegated_brief(rec.get("text") or "") else "unknown", origin_basis="executor-prefix" if is_delegated_brief(rec.get("text") or "") else "history.entry",
                       rel={"sessionId": rec.get("session_id")}, max_chars=max_chars)
        return
    file = _codex_rollout_for(ref)
    if not file:
        yield from _codex_db_only_fragments(store, ref, max_chars, stats)
        return
    yield from _codex_record_fragments(store, ref, Path(file), iter_jsonl(Path(file), stats), max_chars)


def _codex_record_fragments(store: Store, ref: SessionRef, path: Path,
                            records: Iterable[tuple[int, dict[str, Any]]], max_chars: int) -> Iterator[dict[str, Any]]:
    """Fragments of the given rollout records.

    Each record is classified independently, including when called by the
    raw prefilter on a subset of the rollout.
    """
    seq = 0
    for line_no, rec in records:
        seq += 1
        loc = {"file": str(path), "line": line_no}
        t = rec.get("type")
        ts = rec.get("timestamp")
        payload = rec.get("payload") if isinstance(rec.get("payload"), dict) else {}
        pt = payload.get("type")

        if t == "session_meta":
            _, source_parent, _, _ = _codex_source_kind(payload.get("source"))
            lineage = {"parent_thread_id": source_parent} if source_parent else None
            yield frag("codex", store.name, ref.session_id, seq, "metadata", "session_meta", loc,
                       text={k: payload.get(k) for k in ("id", "cwd", "originator", "cli_version", "model_provider", "thread_source", "agent_nickname", "agent_path") if payload.get(k)},
                       ts=ts, authorship="system", visible=False,
                       cwd=payload.get("cwd"),
                       rel={"parent_thread": payload.get("parent_thread_id") or payload.get("forked_from_id") or (lineage or {}).get("parent_thread_id")},
                       max_chars=max_chars)
        elif t == "turn_context":
            yield frag("codex", store.name, ref.session_id, seq, "boundary", "turn_context", loc,
                       text={k: payload.get(k) for k in ("turn_id", "model", "effort", "approval_policy", "collaboration_mode") if payload.get(k) is not None},
                       ts=ts, authorship="system", visible=False,
                       model=payload.get("model"), cwd=payload.get("cwd"),
                       rel={"turn_id": payload.get("turn_id")}, max_chars=max_chars)
        elif t == "response_item":
            if pt == "message":
                role = payload.get("role")
                text = _codex_message_text(payload)
                if role == "assistant":
                    kind, native, auth, vis, basis = "assistant", "response_item.assistant", "agent", True, "response layer"
                elif role in ("user", "developer"):
                    # verbatim human prompts are confirmed by event_msg.user_message;
                    # injections carry known wrappers — anything else is a genuine
                    # (if unverified) user-layer record, not invisible context
                    injected = role == "developer" or any(m in text for m in CODEX_INJECTION_MARKERS) or \
                        any(m in text for m in AUTOCONTEXT_MARKERS)
                    if injected:
                        kind, native, auth, vis = "context", "response_item." + role, "system", False
                        basis = "response-layer injection (bootstrap/env/steer)"
                    else:
                        kind, native, auth, vis = "prompt", "response_item." + role, "unknown", None
                        basis = "response-layer user text without injection markers"
                else:
                    kind, native, auth, vis, basis = "metadata", f"response_item.{role}", "system", False, "non-conversational role"
                yield frag("codex", store.name, ref.session_id, seq, kind, native, loc,
                           text=text, ts=ts, authorship=auth, visible=vis, basis=basis,
                           cwd=None, rel={"item_id": payload.get("id")},
                           origin=("injected" if kind == "context" else "delegated" if is_delegated_brief(text) else "unknown") if role in ("user", "developer") else None,
                           origin_basis=("injection marker" if kind == "context" else "executor-prefix" if is_delegated_brief(text) else "response_item.user") if role in ("user", "developer") else None,
                           native={"phase": payload.get("phase"), "turn_id": payload.get("turn_id"), "layer": "response"} if kind == "assistant" else None,
                           max_chars=max_chars)
            elif pt == "reasoning":
                yield frag("codex", store.name, ref.session_id, seq, "reasoning", "response_item.reasoning", loc,
                           text=(payload.get("summary") or [{}])[0].get("text") if payload.get("summary") else payload.get("content"),
                           ts=ts, authorship="agent", visible=False, rel={"item_id": payload.get("id")}, max_chars=max_chars)
            elif pt in ("function_call", "custom_tool_call", "tool_search_call"):
                yield frag("codex", store.name, ref.session_id, seq, "tool_call", f"response_item.{pt}", loc,
                           text=json.dumps({"name": payload.get("name"), "arguments": payload.get("arguments") or payload.get("input")}, ensure_ascii=False),
                           ts=ts, authorship="agent", visible=False,
                           rel={"call_id": payload.get("call_id"), "tool": payload.get("name")}, max_chars=max_chars)
            elif pt in ("function_call_output", "custom_tool_call_output", "tool_search_output"):
                yield frag("codex", store.name, ref.session_id, seq, "tool_result", f"response_item.{pt}", loc,
                           text=payload.get("output"), ts=ts, authorship="system", visible=False,
                           error=_codex_output_is_error(payload),
                           rel={"call_id": payload.get("call_id")}, max_chars=max_chars)
            else:
                yield frag("codex", store.name, ref.session_id, seq, "unknown", f"response_item.{pt}", loc,
                           text=payload, ts=ts, rel={"item_id": payload.get("id")}, max_chars=max_chars)
        elif t == "event_msg":
            if pt == "user_message":
                yield frag("codex", store.name, ref.session_id, seq, "prompt", "event_msg.user_message", loc,
                           text=payload.get("message"), ts=ts, authorship="unknown", visible=True,
                           basis="verbatim submitted prompt (authorship not guaranteed — may be orchestrator-injected)",
                           rel={"turn_id": payload.get("turn_id")}, origin="delegated" if is_delegated_brief(payload.get("message") or "") else "human",
                           origin_basis="executor-prefix" if is_delegated_brief(payload.get("message") or "") else "event_msg.user_message", max_chars=max_chars)
            elif pt == "agent_message":
                yield frag("codex", store.name, ref.session_id, seq, "assistant", "event_msg.agent_message", loc,
                           text=payload.get("message"), ts=ts, authorship="agent", visible=True,
                           phase=payload.get("phase"), rel={"turn_id": payload.get("turn_id")},
                           native={"phase": payload.get("phase"), "turn_id": payload.get("turn_id"), "layer": "event"}, max_chars=max_chars)
            elif pt in ("task_started", "task_complete", "turn_complete", "turn_aborted"):
                yield frag("codex", store.name, ref.session_id, seq, "boundary", f"event_msg.{pt}", loc,
                           text={k: payload.get(k) for k in ("turn_id", "reason", "msg", "last_agent_message") if payload.get(k)},
                           ts=ts, authorship="system", visible=False,
                           status={"task_complete": "completed", "turn_complete": "completed", "turn_aborted": "aborted"}.get(pt),
                           interrupted=(pt == "turn_aborted"),
                           native={"reason": payload.get("reason")} if pt == "turn_aborted" else None,
                           rel={"turn_id": payload.get("turn_id")}, max_chars=max_chars)
                if pt in ("task_complete", "turn_complete") and payload.get("last_agent_message"):
                    yield frag("codex", store.name, ref.session_id, seq, "assistant", f"event_msg.{pt}.last_agent_message", loc,
                               text=payload["last_agent_message"], ts=ts, authorship="agent", visible=True,
                               native={"phase": None, "turn_id": payload.get("turn_id"), "layer": pt}, max_chars=max_chars)
            elif pt == "guardian_assessment":
                yield frag("codex", store.name, ref.session_id, seq, "permission", "event_msg.guardian_assessment", loc,
                           text={k: payload.get(k) for k in ("target_item_id", "status", "decision_source") if payload.get(k)},
                           ts=ts, authorship="system", visible=False,
                           rel={"call_id": payload.get("target_item_id")}, max_chars=max_chars)
            elif pt in ("context_compacted",):
                yield frag("codex", store.name, ref.session_id, seq, "compaction", f"event_msg.{pt}", loc,
                           text=payload.get("message") or payload, ts=ts, authorship="system", visible=False,
                           max_chars=max_chars)
            else:
                info = payload.get("info") if pt == "token_count" else None
                yield frag("codex", store.name, ref.session_id, seq, "metadata", f"event_msg.{pt}", loc,
                           text=None if pt == "token_count" else payload, ts=ts, authorship="system",
                           visible=False, error=(pt == "error"),
                           usage=(info or {}).get("total_token_usage") or (info or {}).get("total") or info,
                           rel={"turn_id": payload.get("turn_id")}, max_chars=max_chars)
        elif t == "compacted":
            yield frag("codex", store.name, ref.session_id, seq, "compaction", "compacted", loc,
                       text=payload.get("message") or payload, ts=ts, authorship="system",
                       visible=False, basis="history compaction record", max_chars=max_chars)
        else:
            yield frag("codex", store.name, ref.session_id, seq, "metadata", str(t), loc,
                       text=payload or None, ts=ts, authorship="system", visible=False, max_chars=max_chars)


def _codex_output_is_error(payload: dict[str, Any]) -> bool:
    """function_call_output carries JSON text like {"output": ..., "metadata": {"exit_code": N}}."""
    out = payload.get("output")
    if isinstance(out, dict):
        out = out.get("output")
    if isinstance(out, str) and out.startswith("{"):
        try:
            out = json.loads(out)
        except json.JSONDecodeError:
            pass
    meta = out.get("metadata") if isinstance(out, dict) else None
    if isinstance(meta, dict) and meta.get("exit_code") not in (None, 0):
        return True
    return bool(payload.get("is_error") or payload.get("status") == "error")


def _codex_message_text(payload: dict[str, Any]) -> str:
    parts = []
    for block in payload.get("content") or []:
        if isinstance(block, dict) and block.get("type") in ("input_text", "output_text", "text"):
            parts.append(_text_value(block.get("text", "")))
    return "\n".join(p for p in parts if p)


# ---------------------------------------------------------------------------
# OpenCode — SQLite session_v2 (2.x) + legacy session/message/part tables and
# the V1 file storage (1.x). One V1 JSON shape serves files and legacy rows.
# Canonical source order: session_v2 > legacy tables > file storage.
# ---------------------------------------------------------------------------

_OPENCODE_SUBAGENT_PREFIX = "You are a subagent spawned by another session."
_OPENCODE_V2_FINAL_FINISH = {"stop", "length", "content-filter"}


def _oc_prompt_origin(text: Any) -> tuple[str, str, str]:
    """(origin, origin_basis, authorship) for a user-role prompt text."""
    if not isinstance(text, str) or not text.strip():
        return "unknown", "missing text", "unknown"
    if text.lstrip().startswith(_OPENCODE_SUBAGENT_PREFIX):
        return "delegated", "subagent prefix", "agent"
    if is_delegated_brief(text):
        return "delegated", "executor-prefix", "agent"
    return "human", "user message", "human"


def _oc_session_class(parent_id: Any, first_prompt: str | None, sid: str) -> tuple[str, str]:
    if parent_id:
        return "subagent", "parent_id"
    if ("opencode", sid) in porch_workers():
        return "headless", "porch registry"
    if first_prompt:
        if first_prompt.lstrip().startswith(_OPENCODE_SUBAGENT_PREFIX):
            return "headless", "subagent-spawn prefix"
        if is_delegated_brief(first_prompt):
            return "headless", "executor-prefix"
        return "interactive", "user prompt"
    return "unknown", "no user prompt"


def _oc_first_user_text_v2(conn: sqlite3.Connection) -> dict[str, str]:
    """session_id -> text of its first user message (for class detection).

    One bounded query: first user row per session, text via json_extract —
    no per-session content reads at discovery."""
    if not table_exists(conn, "session_message"):
        return {}
    try:
        rows = conn.execute(
            "SELECT session_id, json_extract(data,'$.text') FROM ("
            "  SELECT session_id, seq, data,"
            "    ROW_NUMBER() OVER (PARTITION BY session_id ORDER BY seq) AS rn"
            "  FROM session_message WHERE type='user')"
            " WHERE rn=1").fetchall()
    except sqlite3.Error:
        return {}
    out: dict[str, str] = {}
    for sid, text in rows:
        if isinstance(text, str):
            out[sid] = text
    return out


def _oc_edges_v2(parent_id: Any, fork_id: Any, db: Path) -> list[dict[str, Any]]:
    edges = []
    if parent_id:
        edges.append({"type": "subagent", "target_harness": "opencode", "target_id": parent_id,
                      "unresolved": None, "basis": "parent_id",
                      "locator": {"db": str(db), "table": "session_v2", "key": parent_id}})
    if fork_id:
        edges.append({"type": "fork", "target_harness": "opencode", "target_id": fork_id,
                      "unresolved": None, "basis": "fork_session_id",
                      "locator": {"db": str(db), "table": "session_v2", "key": fork_id}})
    return edges


def _oc_native_status_v2(row: dict[str, Any]) -> dict[str, Any]:
    out = {}
    for key in ("time_idle", "idle_outcome", "time_busy", "time_archived"):
        if row.get(key) is not None:
            out[key] = row[key]
    return out


def _oc_v2_sources(db: Path, sid: str, has_parts: bool) -> list[dict[str, Any]]:
    sources = [{"db": str(db), "table": "session_v2", "key": sid, "role": "metadata"},
               {"db": str(db), "table": "session_message", "key": sid, "role": "content"}]
    if has_parts:
        sources.append({"db": str(db), "table": "session_part", "key": sid, "role": "content"})
    return sources


def _opencode_db_sessions(store: Store) -> Iterator[SessionRef]:
    try:
        conn = open_sqlite_ro(store.path)
    except sqlite3.Error as exc:
        store.status, store.error = "unavailable", str(exc)
        return
    try:
        cols = table_columns(conn, "session_v2")
        has_v2 = "id" in cols and table_exists(conn, "session_message")
        has_v1 = all(table_exists(conn, t) for t in ("session", "message", "part"))
        if not has_v2 and not has_v1:
            store.status, store.error = "unavailable", "OpenCode session tables not found"
            return
        first_user: dict[str, str] = {}
        if has_v2:
            first_user = _oc_first_user_text_v2(conn)
        v2_ids: set[str] = set()
        if has_v2:
            want = [c for c in ("id", "directory", "title", "time_created", "time_updated",
                                "parent_id", "fork_session_id", "agent", "model", "version",
                                "time_idle", "idle_outcome", "time_busy", "time_archived")
                    if c in cols]
            if "id" not in want:
                want = ["id"]
            rows = conn.execute(
                f"SELECT {', '.join(want)} FROM session_v2").fetchall()
            colidx = {c: i for i, c in enumerate(want)}
            has_parts = table_exists(conn, "session_part")
            for row in rows:
                rec = {c: row[i] for c, i in colidx.items()}
                sid = rec["id"]
                v2_ids.add(sid)
                yield _oc_v2_ref_from_record(store, rec, first_user.get(sid), has_parts)
        if has_v1:
            for ref in _oc_legacy_db_sessions(conn, store, v2_ids):
                yield ref
    except sqlite3.Error as exc:
        store.status, store.error = "unavailable", str(exc)
    finally:
        conn.close()


def _oc_v2_ref_from_record(store: Store, rec: dict, first_prompt: str | None, has_parts: bool) -> SessionRef:
    sid = rec["id"]
    model_raw = rec.get("model")
    try:
        model = json.loads(model_raw).get("id") if model_raw else None
    except (json.JSONDecodeError, AttributeError):
        model = model_raw
    parent_id, fork_id = rec.get("parent_id"), rec.get("fork_session_id")
    session_class, class_basis = _oc_session_class(parent_id, first_prompt, sid)
    return SessionRef("opencode", store.name, sid,
        locator={"table": "session_v2", "key": sid, "db": str(store.path)},
        title=rec.get("title"), cwd=rec.get("directory"),
        created=rec.get("time_created"), updated=rec.get("time_updated"),
        parent=parent_id, models=[model] if model else [],
        extra={
            "agent": rec.get("agent"), "version": rec.get("version"),
            "oc_source": "v2",
            "native_id": sid, "surface": "cli",
            "session_class": session_class, "class_basis": class_basis,
            "edges": _oc_edges_v2(parent_id, fork_id, store.path),
            "native_status": _oc_native_status_v2(rec),
            "sources": _oc_v2_sources(store.path, sid, has_parts),
        })


def opencode_v2_ref(store: Store, conn: sqlite3.Connection, sid: str) -> SessionRef | None:
    cols = table_columns(conn, "session_v2")
    want = [c for c in ("id", "directory", "title", "time_created", "time_updated", "parent_id", "fork_session_id", "agent", "model", "version", "time_idle", "idle_outcome", "time_busy", "time_archived") if c in cols]
    if "id" not in want:
        return None
    row = conn.execute(f"SELECT {', '.join(want)} FROM session_v2 WHERE id=?", (sid,)).fetchone()
    if not row:
        return None
    first_row = conn.execute("SELECT json_extract(data,'$.text') FROM session_message WHERE session_id=? AND type='user' ORDER BY seq LIMIT 1", (sid,)).fetchone()
    first_prompt = first_row[0] if first_row and isinstance(first_row[0], str) else None
    return _oc_v2_ref_from_record(store, dict(zip(want, row)), first_prompt, table_exists(conn, "session_part"))


def opencode_iter_sessions(store: Store) -> Iterator[SessionRef]:
    if store.kind != "sqlite":
        yield from _oc_v1_file_sessions(store)
        return
    yield from _opencode_db_sessions(store)


# --- OpenCode 1.x shared V1 shapes (file storage + legacy tables) -----------

def _oc_v1_message_info(msg: dict[str, Any]) -> dict[str, Any]:
    t = msg.get("time") if isinstance(msg.get("time"), dict) else {}
    path = msg.get("path") if isinstance(msg.get("path"), dict) else {}
    model = msg.get("model") if isinstance(msg.get("model"), dict) else {}
    return {
        "id": msg.get("id"), "role": msg.get("role"),
        "created": t.get("created"), "completed": t.get("completed"),
        "finish": msg.get("finish"), "error": msg.get("error"),
        "cwd": path.get("cwd"),
        "model": msg.get("modelID") or model.get("modelID") or model.get("id"),
        "provider": msg.get("providerID") or model.get("providerID"),
        "agent": msg.get("agent"),
    }


def _oc_v1_part_fragments(store: Store, ref: SessionRef, msg: dict[str, Any],
                          parts: list[tuple[dict[str, Any], dict[str, Any]]],
                          max_chars: int, stats: dict[str, int]) -> Iterator[dict[str, Any]]:
    info = _oc_v1_message_info(msg)
    role = info["role"]
    mid = info["id"]
    base_ts = info["created"]
    last_tool = -1
    for i, (part, _ploc) in enumerate(parts):
        if part.get("type") == "tool":
            last_tool = i
    seq = 0
    for i, (part, ploc) in enumerate(parts):
        ptype = part.get("type")
        pts = ((part.get("time") or {}).get("start") if isinstance(part.get("time"), dict) else None) or base_ts
        if ptype == "text":
            text = part.get("text")
            if role == "user":
                if part.get("synthetic"):
                    seq += 1
                    yield frag("opencode", store.name, ref.session_id, seq, "context",
                               "part.text", ploc, text=text, ts=pts, authorship="system",
                               visible=False, basis="synthetic text part", cwd=ref.cwd,
                               max_chars=max_chars)
                elif part.get("ignored"):
                    seq += 1
                    yield frag("opencode", store.name, ref.session_id, seq, "context",
                               "part.text", ploc, text=text, ts=pts, authorship="system",
                               visible=False, basis="ignored text part", cwd=ref.cwd,
                               max_chars=max_chars)
                else:
                    origin, origin_basis, authorship = _oc_prompt_origin(text)
                    seq += 1
                    yield frag("opencode", store.name, ref.session_id, seq, "prompt",
                               "part.text", ploc, text=text, ts=pts, authorship=authorship,
                               visible=True, basis="user text part", cwd=ref.cwd,
                               model=info["model"], provider=info["provider"],
                               origin=origin, origin_basis=origin_basis, max_chars=max_chars)
            elif role == "assistant":
                completed = info["completed"] is not None
                seq += 1
                yield frag("opencode", store.name, ref.session_id, seq, "assistant",
                           "part.text", ploc, text=text, ts=pts, authorship="agent",
                           visible=True, basis="assistant text part", cwd=ref.cwd,
                           model=info["model"], provider=info["provider"],
                           native={"message_id": mid, "finish": info["finish"],
                                   "completed": completed, "part_index": i,
                                   "after_last_tool": last_tool < 0 or i > last_tool},
                           max_chars=max_chars)
            else:
                seq += 1
                yield frag("opencode", store.name, ref.session_id, seq, "context",
                           "part.text", ploc, text=text, ts=pts, authorship="unknown",
                           visible=False, basis=f"text part in {role} message", cwd=ref.cwd,
                           max_chars=max_chars)
        elif ptype == "reasoning":
            seq += 1
            yield frag("opencode", store.name, ref.session_id, seq, "reasoning",
                       "part.reasoning", ploc, text=part.get("text"), ts=pts,
                       authorship="agent", visible=False, basis="reasoning part",
                       cwd=ref.cwd, model=info["model"], provider=info["provider"],
                       max_chars=max_chars)
        elif ptype == "tool":
            state = part.get("state") if isinstance(part.get("state"), dict) else {}
            status = state.get("status")
            rel = {"call_id": part.get("callID") or part.get("id"), "tool": part.get("tool")}
            seq += 1
            yield frag("opencode", store.name, ref.session_id, seq, "tool_call",
                       "part.tool", ploc,
                       text={"tool": part.get("tool"), "input": state.get("input"), "status": status},
                       ts=pts, authorship="agent", visible=False, rel=rel, max_chars=max_chars)
            output = state.get("output") or state.get("error")
            if output is not None:
                seq += 1
                yield frag("opencode", store.name, ref.session_id, seq, "tool_result",
                           "part.tool", ploc, text=output, ts=pts, authorship="system",
                           visible=False, rel=rel, error=(status == "error"), max_chars=max_chars)
        elif ptype == "step-start":
            seq += 1
            yield frag("opencode", store.name, ref.session_id, seq, "boundary",
                       "part.step-start", ploc, ts=pts, authorship="system",
                       visible=False, basis="step-start", cwd=ref.cwd, max_chars=max_chars)
        elif ptype == "step-finish":
            usage = None
            tokens = part.get("tokens") if isinstance(part.get("tokens"), dict) else None
            if tokens:
                usage = {"input_tokens": tokens.get("input"), "output_tokens": tokens.get("output"),
                         "total_tokens": tokens.get("total")}
            seq += 1
            yield frag("opencode", store.name, ref.session_id, seq, "boundary",
                       "part.step-finish", ploc, ts=pts, authorship="system",
                       visible=False, basis="step-finish", status=part.get("reason"),
                       usage=usage, cwd=ref.cwd, max_chars=max_chars)
        elif ptype == "subtask":
            seq += 1
            yield frag("opencode", store.name, ref.session_id, seq, "prompt",
                       "part.subtask", ploc, text=part.get("prompt"), ts=pts,
                       authorship="agent", visible=True, basis="subtask part",
                       origin="delegated", origin_basis="subtask part", cwd=ref.cwd,
                       rel={"agent": part.get("agent"), "description": part.get("description")},
                       max_chars=max_chars)
        elif ptype == "file":
            seq += 1
            yield frag("opencode", store.name, ref.session_id, seq, "context",
                       "part.file", ploc,
                       text={"mime": part.get("mime"), "filename": part.get("filename"),
                             "url": part.get("url")},
                       ts=pts, authorship="system", visible=False, basis="file part",
                       cwd=ref.cwd, max_chars=max_chars)
        elif ptype == "agent":
            seq += 1
            yield frag("opencode", store.name, ref.session_id, seq, "context",
                       "part.agent", ploc, text=part.get("name"), ts=pts,
                       authorship="system", visible=False, basis="agent part",
                       cwd=ref.cwd, max_chars=max_chars)
        elif ptype == "compaction":
            seq += 1
            yield frag("opencode", store.name, ref.session_id, seq, "compaction",
                       "part.compaction", ploc, ts=pts, authorship="system",
                       visible=False, basis="compaction part", cwd=ref.cwd, max_chars=max_chars)
        elif ptype == "retry":
            seq += 1
            yield frag("opencode", store.name, ref.session_id, seq, "metadata",
                       "part.retry", ploc, text=part.get("error"), ts=pts,
                       authorship="system", visible=False, error=True,
                       basis="retry part", cwd=ref.cwd, max_chars=max_chars)
        elif ptype == "patch":
            seq += 1
            yield frag("opencode", store.name, ref.session_id, seq, "context",
                       "part.patch", ploc, text={"hash": part.get("hash"), "files": part.get("files")},
                       ts=pts, authorship="system", visible=False, basis="patch part",
                       cwd=ref.cwd, max_chars=max_chars)
        else:
            seq += 1
            yield frag("opencode", store.name, ref.session_id, seq, "metadata",
                       f"part.{ptype or 'unknown'}", ploc, text=part, ts=pts,
                       authorship="system", visible=False,
                       basis="unrecognized part", cwd=ref.cwd, max_chars=max_chars)


def _oc_v1_file_messages(storage: Path, sid: str,
                               stats: dict[str, int]) -> Iterator[tuple[dict[str, Any], dict[str, Any], list[tuple[dict[str, Any], dict[str, Any]]]]]:
    msg_dir = storage / "message" / sid
    if not msg_dir.is_dir():
        return
    loaded = []
    for mf in sorted(msg_dir.glob("*.json")):
        msg = read_json(mf)
        if not isinstance(msg, dict):
            stats["malformed"] = stats.get("malformed", 0) + 1
            continue
        loaded.append((msg, mf))
    # V1 ids are opaque; time.created is the reliable order.
    loaded.sort(key=lambda t: (((t[0].get("time") or {}).get("created")) or 0,
                               str(t[0].get("id") or t[1].stem)))
    for msg, mf in loaded:
        mid = msg.get("id") or mf.stem
        parts = []
        pdir = storage / "part" / mid
        if pdir.is_dir():
            for pf in sorted(pdir.glob("*.json")):
                part = read_json(pf)
                if isinstance(part, dict):
                    parts.append((part, {"file": str(pf)}))
                else:
                    stats["malformed"] = stats.get("malformed", 0) + 1
        # Part ids are creation-ordered (verified against the store); boundary
        # parts carry no time field, so id is the reliable sort key.
        parts.sort(key=lambda t: str(t[0].get("id") or ""))
        yield msg, {"file": str(mf)}, parts


def _oc_v1_db_messages(conn: sqlite3.Connection, sid: str, db: Path,
                             stats: dict[str, int]) -> Iterator[tuple[dict[str, Any], dict[str, Any], list[tuple[dict[str, Any], dict[str, Any]]]]]:
    rows = conn.execute(
        "SELECT id, data FROM message WHERE session_id=? ORDER BY time_created, id", (sid,)).fetchall()
    part_rows = conn.execute(
        "SELECT message_id, id, data FROM part WHERE session_id=? ORDER BY message_id, time_created, id",
        (sid,)).fetchall()
    parts_by_msg: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = {}
    for msg_id, part_id, raw in part_rows:
        try:
            part = json.loads(raw)
            if not isinstance(part, dict):
                raise ValueError("part is not an object")
        except (json.JSONDecodeError, ValueError, TypeError):
            stats["malformed"] = stats.get("malformed", 0) + 1
            continue
        part.setdefault("id", part_id)
        ploc = {"db": str(db), "table": "part", "key": part_id}
        parts_by_msg.setdefault(msg_id, []).append((part, ploc))
    for mid, raw in rows:
        try:
            msg = json.loads(raw)
            if not isinstance(msg, dict):
                raise ValueError("message is not an object")
        except (json.JSONDecodeError, ValueError, TypeError):
            stats["malformed"] = stats.get("malformed", 0) + 1
            continue
        msg.setdefault("id", mid)
        mloc = {"db": str(db), "table": "message", "key": mid}
        # parts_by_msg lists already follow the query's time_created,id order.
        yield msg, mloc, parts_by_msg.get(mid, [])


def _oc_v1_fragments(store: Store, ref: SessionRef, max_chars: int, stats: dict[str, int], conn: sqlite3.Connection | None = None) -> Iterator[dict[str, Any]]:
    if ref.extra.get("oc_source") == "legacy":
        owned = conn is None
        try:
            if owned:
                conn = open_sqlite_ro(store.path)
        except sqlite3.Error as exc:
            store.status, store.error = "unavailable", str(exc)
            stats["unreadable"] = stats.get("unreadable", 0) + 1
            return
        try:
            for msg, mloc, parts in _oc_v1_db_messages(conn, ref.session_id, store.path, stats):
                yield from _oc_v1_part_fragments(store, ref, msg, parts, max_chars, stats)
        finally:
            if owned:
                conn.close()
        return
    storage = store.path
    if not (storage / "session").is_dir() and (storage / "storage" / "session").is_dir():
        storage = storage / "storage"
    for msg, mloc, parts in _oc_v1_file_messages(storage, ref.session_id, stats):
        yield from _oc_v1_part_fragments(store, ref, msg, parts, max_chars, stats)


def _oc_v1_file_sessions(store: Store) -> Iterator[SessionRef]:
    storage = store.path
    session_root = storage / "session"
    if not session_root.is_dir() and (storage / "storage" / "session").is_dir():
        storage = storage / "storage"
        session_root = storage / "session"
    if not session_root.is_dir():
        store.status, store.error = "empty", "no V1 session directory"
        return
    claimed = _oc_db_claimed_ids(storage.parent)
    for path in sorted(_walk(session_root, "*.json", store.walk_stats)):
        ref = opencode_file_ref(store, path, claimed)
        if ref is not None:
            yield ref


def opencode_file_ref(store: Store, path: Path, claimed: set[str] | None = None) -> SessionRef | None:
    storage = store.path if (store.path / "session").is_dir() else store.path / "storage"
    if claimed is None:
        claimed = _oc_db_claimed_ids(storage.parent)
    data = read_json(path)
    if not isinstance(data, dict):
        return None
    sid = str(data.get("id") or path.stem)
    if sid in claimed:
        return None
    t = data.get("time") if isinstance(data.get("time"), dict) else {}
    parent_id = data.get("parentID")
    model_info = data.get("model") if isinstance(data.get("model"), dict) else {}
    model = data.get("modelID") or model_info.get("modelID") or model_info.get("id")
    first_prompt = _oc_v1_first_prompt(storage, sid)
    session_class, class_basis = _oc_session_class(parent_id, first_prompt, sid)
    edges = []
    if parent_id:
        edges.append({"type": "subagent", "target_harness": "opencode", "target_id": parent_id,
                      "unresolved": None, "basis": "parentID",
                      "locator": {"file": str(path)}})
    native_status = {}
    if t.get("archived") is not None:
        native_status["time_archived"] = t.get("archived")
    return SessionRef("opencode", store.name, sid,
        locator={"file": str(path)},
        title=data.get("title"), cwd=data.get("directory"),
        created=t.get("created"), updated=t.get("updated"),
        parent=parent_id, models=[model] if model else [],
        extra={
            "oc_source": "file", "version": data.get("version"),
            "native_id": sid, "surface": "cli",
            "session_class": session_class, "class_basis": class_basis,
            "edges": edges, "native_status": native_status,
            "sources": [
                {"path": str(path), "role": "metadata"},
                {"path": str(storage / "message" / sid), "role": "content"},
                {"path": str(storage / "part"), "role": "content"},
            ],
        })


def _oc_v1_first_prompt(storage: Path, sid: str) -> str | None:
    """First genuine user text of a file-storage session (bounded head read:
    at most 8 message files and the first user message's parts)."""
    msg_dir = storage / "message" / sid
    if not msg_dir.is_dir():
        return None
    for mf in sorted(msg_dir.glob("*.json"))[:8]:
        msg = read_json(mf)
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        mid = msg.get("id") or mf.stem
        pdir = storage / "part" / mid
        if not pdir.is_dir():
            return None
        for pf in sorted(pdir.glob("*.json"))[:16]:
            part = read_json(pf)
            if (isinstance(part, dict) and part.get("type") == "text"
                    and not part.get("synthetic") and not part.get("ignored")):
                text = part.get("text")
                return text if isinstance(text, str) else None
        return None
    return None


def _oc_v1_first_prompt_db(conn: sqlite3.Connection, sid: str) -> str | None:
    """First genuine user text part of a legacy-table session (lazy probe)."""
    try:
        rows = conn.execute(
            "SELECT id, data FROM message WHERE session_id=? "
            "AND json_extract(data,'$.role')='user' ORDER BY time_created, id LIMIT 1",
            (sid,)).fetchall()
    except sqlite3.Error:
        return None
    if not rows:
        return None
    mid, _raw = rows[0]
    try:
        cursor = conn.execute(
            "SELECT data FROM part WHERE message_id=? ORDER BY time_created, id", (mid,))
    except sqlite3.Error:
        return None
    for (raw_part,) in cursor:  # lazy: stops at the first genuine text part
        try:
            part = json.loads(raw_part)
        except (json.JSONDecodeError, TypeError):
            continue
        if (isinstance(part, dict) and part.get("type") == "text"
                and not part.get("synthetic") and not part.get("ignored")):
            text = part.get("text")
            if isinstance(text, str):
                return text
            return None
    return None


def _oc_db_claimed_ids(base: Path) -> set[str]:
    """Session ids covered by sibling OpenCode DBs (session_v2 + legacy)."""
    configured = os.environ.get("OPENCODE_DB")
    if configured == ":memory:":
        paths = []
    elif configured is not None:
        paths = [Path(os.path.abspath(base / configured))]
    else:
        paths = [base / "opencode.db", *sorted(base.glob("opencode-*.db"))]
    claimed: set[str] = set()
    for path in paths:
        if not path.is_file():
            continue
        try:
            conn = open_sqlite_ro(path)
        except sqlite3.Error:
            continue
        try:
            if table_exists(conn, "session_v2"):
                claimed.update(r[0] for r in conn.execute("SELECT id FROM session_v2"))
            if table_exists(conn, "session"):
                claimed.update(r[0] for r in conn.execute("SELECT id FROM session"))
        except sqlite3.Error:
            pass
        finally:
            conn.close()
    return claimed


def _oc_legacy_db_sessions(conn: sqlite3.Connection, store: Store,
                           v2_ids: set[str]) -> Iterator[SessionRef]:
    cols = table_columns(conn, "session")
    want = [c for c in ("id", "parent_id", "slug", "directory", "title", "version",
                        "agent", "model", "time_created", "time_updated", "time_archived")
            if c in cols]
    if "id" not in want:
        return
    rows = conn.execute(f"SELECT {', '.join(want)} FROM session").fetchall()
    colidx = {c: i for i, c in enumerate(want)}
    for row in rows:
        rec = {c: row[i] for c, i in colidx.items()}
        sid = rec["id"]
        if sid in v2_ids:
            continue
        yield _oc_legacy_ref_from_record(conn, store, rec)


def _oc_legacy_ref_from_record(conn: sqlite3.Connection, store: Store, rec: dict) -> SessionRef:
    sid = rec["id"]
    parent_id = rec.get("parent_id")
    model_raw = rec.get("model")
    try:
        model = json.loads(model_raw).get("id") if model_raw else None
    except (json.JSONDecodeError, AttributeError):
        model = model_raw
    session_class, class_basis = _oc_session_class(parent_id, _oc_v1_first_prompt_db(conn, sid), sid)
    edges = []
    if parent_id:
        edges.append({"type": "subagent", "target_harness": "opencode", "target_id": parent_id,
                      "unresolved": None, "basis": "parent_id",
                      "locator": {"db": str(store.path), "table": "session", "key": parent_id}})
    native_status = {}
    if rec.get("time_archived") is not None:
        native_status["time_archived"] = rec["time_archived"]
    return SessionRef("opencode", store.name, sid,
        locator={"db": str(store.path), "table": "session", "key": sid},
        title=rec.get("title"), cwd=rec.get("directory"),
        created=rec.get("time_created"), updated=rec.get("time_updated"),
        parent=parent_id, models=[model] if model else [],
        extra={
            "oc_source": "legacy", "agent": rec.get("agent"), "version": rec.get("version"),
            "native_id": sid, "surface": "cli",
            "session_class": session_class, "class_basis": class_basis,
            "edges": edges, "native_status": native_status,
            "sources": [
                {"db": str(store.path), "table": "session", "key": sid, "role": "metadata"},
                {"db": str(store.path), "table": "message", "key": sid, "role": "content"},
                {"db": str(store.path), "table": "part", "key": sid, "role": "content"},
            ],
        })



def opencode_legacy_ref(store: Store, conn: sqlite3.Connection, sid: str) -> SessionRef | None:
    if table_exists(conn, "session_v2") and conn.execute("SELECT 1 FROM session_v2 WHERE id=?", (sid,)).fetchone():
        return None
    cols = table_columns(conn, "session")
    want = [c for c in ("id", "parent_id", "slug", "directory", "title", "version", "agent", "model", "time_created", "time_updated", "time_archived") if c in cols]
    if "id" not in want:
        return None
    row = conn.execute(f"SELECT {', '.join(want)} FROM session WHERE id=?", (sid,)).fetchone()
    return _oc_legacy_ref_from_record(conn, store, dict(zip(want, row))) if row else None


def _opencode_v2_fragments(store: Store, ref: SessionRef, max_chars: int, stats: dict[str, int], conn: sqlite3.Connection | None = None) -> Iterator[dict[str, Any]]:
    owned = conn is None
    if owned:
        conn = open_sqlite_ro(store.path)
    try:
        rows = conn.execute("SELECT id, type, time_created, data FROM session_message WHERE session_id=? ORDER BY seq", (ref.session_id,)).fetchall()
        part_rows = conn.execute(
            "SELECT message_id, type, data FROM session_part WHERE session_id=? ORDER BY message_id, ordinal",
            (ref.session_id,),
        ).fetchall() if table_exists(conn, "session_part") else []
    finally:
        if owned:
            conn.close()
    parts_by_message: dict[str, list[dict[str, Any]]] = {}
    for message_id, part_type, raw_part in part_rows:
        try:
            part = json.loads(raw_part)
            if not isinstance(part, dict):
                raise ValueError("part is not an object")
        except (json.JSONDecodeError, ValueError, TypeError):
            stats["malformed"] = stats.get("malformed", 0) + 1
            continue
        parts_by_message.setdefault(message_id, []).append({**part, "type": part_type})
    seq = 0
    for row_id, row_type, timestamp, raw in rows:
        try:
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError("message is not an object")
        except (json.JSONDecodeError, ValueError, TypeError):
            stats["malformed"] = stats.get("malformed", 0) + 1
            continue
        locator = {"db": str(store.path), "table": "session_message", "key": row_id}
        model_info = data.get("model") if isinstance(data.get("model"), dict) else {}
        model, provider = model_info.get("id"), model_info.get("providerID")
        fragments = []
        if row_type == "user":
            origin, origin_basis, authorship = _oc_prompt_origin(data.get("text"))
            fragments.append({"kind": "prompt", "text": data.get("text"), "authorship": authorship,
                              "origin": origin, "origin_basis": origin_basis})
        elif row_type == "assistant":
            # V2 may store parts in rows, inline, or both after a downgrade.
            # OpenCode reads stored rows first and appends inline parts.
            combined = parts_by_message.get(row_id, []) + (data.get("content") or [])
            last_tool = -1
            for pi, part in enumerate(combined):
                if isinstance(part, dict) and part.get("type") == "tool":
                    last_tool = pi
            msg_time = data.get("time") if isinstance(data.get("time"), dict) else {}
            completed = msg_time.get("completed") is not None
            finish = data.get("finish")
            for pi, part in enumerate(combined):
                if not isinstance(part, dict):
                    stats["malformed"] = stats.get("malformed", 0) + 1
                    continue
                kind = part.get("type")
                if kind in ("text", "reasoning"):
                    entry = {"kind": "assistant" if kind == "text" else "reasoning",
                             "text": part.get("text"), "authorship": "agent"}
                    if kind == "text":
                        entry["native"] = {"message_id": row_id, "finish": finish,
                                           "completed": completed, "part_index": pi,
                                           "after_last_tool": last_tool < 0 or pi > last_tool}
                    fragments.append(entry)
                elif kind == "tool":
                    state = part.get("state") if isinstance(part.get("state"), dict) else {}
                    rel = {"call_id": part.get("id"), "tool": part.get("name")}
                    fragments.append({"kind": "tool_call",
                                      "text": {"tool": part.get("name"), "input": state.get("input"),
                                               "status": state.get("status")},
                                      "authorship": "agent", "rel": rel})
                    output = state.get("content") or state.get("output") or state.get("error")
                    if output is not None:
                        fragments.append({"kind": "tool_result", "text": output,
                                          "authorship": "system", "rel": rel})
        elif row_type == "shell":
            fragments.append({"kind": "tool_call", "text": data.get("command"), "authorship": "human",
                              "rel": {"tool": "shell", "call_id": data.get("shellID")}})
            output = data.get("output")
            if isinstance(output, dict):
                output = output.get("output")
            if output is not None:
                fragments.append({"kind": "tool_result", "text": output, "authorship": "system",
                                  "rel": {"tool": "shell", "call_id": data.get("shellID")}})
        elif row_type in ("synthetic", "system", "skill"):
            fragments.append({"kind": "context", "text": data.get("text") or data,
                              "authorship": "system", "visible": False,
                              "basis": f"session_message.{row_type}"})
        else:
            fragments.append({"kind": "metadata", "text": data, "authorship": "system"})
        for entry in fragments:
            seq += 1
            yield frag("opencode", store.name, ref.session_id, seq, entry["kind"],
                       f"session_message.{row_type}", locator,
                       text=entry.get("text"), ts=timestamp,
                       authorship=entry.get("authorship", "unknown"),
                       visible=entry.get("visible", entry["kind"] in KINDS_CONVO),
                       basis=entry.get("basis"), model=model, provider=provider,
                       cwd=ref.cwd, rel=entry.get("rel"), native=entry.get("native"),
                       origin=entry.get("origin"), origin_basis=entry.get("origin_basis") or "",
                       max_chars=max_chars)


def opencode_iter_fragments(store: Store, ref: SessionRef, max_chars: int, stats: dict[str, int], conn: sqlite3.Connection | None = None) -> Iterator[dict[str, Any]]:
    source = ref.extra.get("oc_source")
    if source in ("file", "legacy") or (source is None and store.kind != "sqlite"):
        yield from _oc_v1_fragments(store, ref, max_chars, stats, conn)
        return
    yield from _opencode_v2_fragments(store, ref, max_chars, stats, conn)


def opencode_turns(ref: SessionRef, fragments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """OpenCode turns (v1 file/legacy + v2 share one rule).

    v2 collects completed messages (stop/length/content-filter) after their
    last tool part. v1 collects text from each stopped or length-limited step.
    The last candidate remains available as final.
    """
    is_v1 = ref.extra.get("oc_source") in ("file", "legacy") or any(
        f.get("native_type") in ("part.step-start", "part.step-finish") for f in fragments)
    turns = _answer_segments(fragments)

    def _join(frags: list[dict[str, Any]]) -> dict[str, Any] | None:
        texts = [f for f in frags if isinstance(f.get("text"), str) and f["text"].strip()]
        if not texts:
            return None
        if len(texts) == 1:
            return texts[0]
        merged = dict(texts[-1])
        merged["text"] = "\n\n".join(t["text"] for t in texts)
        return merged

    out = []
    last_i = len(turns) - 1
    for i, turn in enumerate(turns):
        final, final_basis = None, "none"
        finals: list[dict[str, Any]] = []
        if is_v1:
            # Steps bracketed by step-start/step-finish boundary fragments.
            step_texts: list[dict[str, Any]] = []
            in_step = False
            last_reason = None
            for f in turn["frags"]:
                nt = f.get("native_type")
                if nt == "part.step-start":
                    step_texts, in_step = [], True
                elif nt == "part.step-finish":
                    in_step = False
                    if f.get("status") in ("stop", "length"):
                        candidate = _join(step_texts)
                        if candidate is not None:
                            last_reason = f.get("status")
                            final = candidate
                            finals.append(candidate)
                    step_texts = []
                elif in_step and f.get("kind") == "assistant":
                    step_texts.append(f)
            if final is not None:
                final_basis = f"native:step-finish={last_reason}"
        else:
            # Group assistant fragments by message_id; last completed message
            # with a final finish reason wins; final = post-last-tool texts.
            by_msg: dict[str, list[dict[str, Any]]] = {}
            order: list[str] = []
            for f in turn["frags"]:
                if f.get("kind") != "assistant":
                    continue
                mid = (f.get("native") or {}).get("message_id")
                key = str(mid) if mid is not None else f"seq{f.get('seq')}"
                if key not in by_msg:
                    by_msg[key] = []
                    order.append(key)
                by_msg[key].append(f)
            last_msg_state = None
            for key in order:
                group = by_msg[key]
                native = group[0].get("native") or {}
                finish = native.get("finish")
                if native.get("completed") and finish in _OPENCODE_V2_FINAL_FINISH:
                    texts = [f for f in group
                             if (f.get("native") or {}).get("after_last_tool")]
                    candidate = _join(texts)
                    if candidate is not None:
                        final = candidate
                        finals.append(candidate)
                        final_basis = f"native:finish={finish}"
                last_msg_state = (native.get("completed"), finish)
            session_live = (last_msg_state is None
                            or last_msg_state[0] is not True
                            or last_msg_state[1] in (None, "tool-calls"))
        # open: last turn, prompts seen, no final, session possibly live.
        open_ = False
        if i == last_i and final is None and turn["prompts"]:
            if is_v1:
                # Live when the fragment stream ends inside an unclosed step or
                # the last assistant message is incomplete / tool-call bound.
                live = False
                in_step = False
                last_state = None
                for f in fragments:
                    nt = f.get("native_type")
                    if nt == "part.step-start":
                        in_step = True
                    elif nt == "part.step-finish":
                        in_step = False
                    if f.get("kind") == "assistant":
                        n = f.get("native") or {}
                        last_state = (n.get("completed"), n.get("finish"))
                live = in_step or last_state is None or last_state[0] is not True \
                    or last_state[1] in (None, "tool-calls")
                open_ = live
            else:
                open_ = session_live
        if not turn["prompts"] and not finals:
            continue
        out.append({"prompts": turn["prompts"], "final": final, "finals": finals,
                    "final_basis": final_basis, "open": open_,
                    "ts_start": turn["ts_start"], "ts_end": turn["ts_end"]})
    return out


TURN_RULES["opencode"] = opencode_turns


# ---------------------------------------------------------------------------
# grok-build
# ---------------------------------------------------------------------------

def grok_iter_sessions(store: Store) -> Iterator[SessionRef]:
    for proj in sorted(store.path.iterdir() if store.path.is_dir() else []):
        if not proj.is_dir():
            continue
        cwd = urllib.parse.unquote(proj.name)
        for sess in sorted(proj.iterdir()):
            if not sess.is_dir():
                continue
            summary = read_json(sess / "summary.json") or {}
            info = summary.get("info") or {}
            yield SessionRef(
                "grok", store.name, str(info.get("id") or sess.name),
                locator={"dir": str(sess)},
                title=summary.get("generated_title") or summary.get("session_summary"),
                cwd=info.get("cwd") or cwd,
                created=summary.get("created_at"), updated=summary.get("updated_at") or summary.get("last_active_at"),
                models=[summary["current_model_id"]] if summary.get("current_model_id") else [],
                extra={k: summary[k] for k in ("agent_name", "sandbox_profile", "reasoning_effort", "num_messages") if summary.get(k) is not None},
            )


def grok_iter_fragments(store: Store, ref: SessionRef, max_chars: int, stats: dict[str, int]) -> Iterator[dict[str, Any]]:
    sess_dir = Path(ref.locator["dir"])
    prompts: set[str] = set()
    for line_no, rec in iter_jsonl(sess_dir.parent / "prompt_history.jsonl", stats):
        if rec.get("session_id") == ref.session_id and rec.get("prompt"):
            prompts.add(rec["prompt"])
    seq = 0
    for line_no, rec in iter_jsonl(sess_dir / "chat_history.jsonl", stats):
        seq += 1
        loc = {"file": str(sess_dir / "chat_history.jsonl"), "line": line_no}
        t = rec.get("type")
        if t == "user":
            texts = [b.get("text", "") for b in rec.get("content") or [] if isinstance(b, dict) and b.get("type") == "text"]
            text = "\n".join(texts)
            bootstrap = "<user_info>" in text or text.lstrip().startswith("<rules")
            auto = any(m in text for m in AUTOCONTEXT_MARKERS)
            yield frag("grok", store.name, ref.session_id, seq,
                       "context" if (bootstrap or auto) else "prompt", "user", loc, text=text,
                       authorship="system" if (bootstrap or auto) else ("human" if text in prompts else "unknown"),
                       visible=not (bootstrap or auto),
                       basis=("bootstrap wrapper" if bootstrap else "auto-context marker" if auto else
                              "matches prompt_history verbatim" if text in prompts else "no verbatim confirmation"),
                       max_chars=max_chars)
        elif t == "assistant":
            yield frag("grok", store.name, ref.session_id, seq, "assistant", "assistant", loc,
                       text=rec.get("content"), authorship="agent", max_chars=max_chars)
            for call in rec.get("tool_calls") or []:
                seq += 1
                yield frag("grok", store.name, ref.session_id, seq, "tool_call", "assistant.tool_call", loc,
                           text=json.dumps({"name": call.get("name"), "arguments": call.get("arguments")}, ensure_ascii=False),
                           authorship="agent", visible=False,
                           rel={"call_id": call.get("id"), "tool": call.get("name")}, max_chars=max_chars)
        elif t == "reasoning":
            texts = [s.get("text", "") for s in rec.get("summary") or [] if isinstance(s, dict)]
            yield frag("grok", store.name, ref.session_id, seq, "reasoning", "reasoning", loc,
                       text="\n".join(texts) or "(encrypted reasoning)", authorship="agent", visible=False,
                       rel={"reasoning_id": rec.get("id")}, max_chars=max_chars)
        elif t == "tool_result":
            yield frag("grok", store.name, ref.session_id, seq, "tool_result", "tool_result", loc,
                       text=rec.get("content"), authorship="system", visible=False,
                       error=bool(rec.get("is_error") or rec.get("isError")),
                       rel={"call_id": rec.get("tool_call_id")}, max_chars=max_chars)
        elif t == "backend_tool_call":
            kind = rec.get("kind") or {}
            yield frag("grok", store.name, ref.session_id, seq, "tool_call", "backend_tool_call", loc,
                       text=json.dumps(kind.get("action") or kind, ensure_ascii=False),
                       authorship="agent", visible=False, status=rec.get("status"),
                       error=rec.get("status") == "error",
                       rel={"tool": kind.get("tool_type"), "call_id": rec.get("id")}, max_chars=max_chars)
        elif t == "system":
            yield frag("grok", store.name, ref.session_id, seq, "context", "system", loc,
                       text=rec.get("content"), authorship="system", visible=False,
                       basis="system prompt record", max_chars=max_chars)
        else:
            yield frag("grok", store.name, ref.session_id, seq, "unknown", str(t), loc,
                       text=rec, max_chars=max_chars)


# ---------------------------------------------------------------------------
# devin-cli
# ---------------------------------------------------------------------------

def devin_iter_sessions(store: Store) -> Iterator[SessionRef]:
    if store.name == "transcripts":
        if not store.path.is_dir():
            store.status = "missing"
            return
        for path in sorted(store.path.glob("*.json")):
            data = read_json(path)
            if not isinstance(data, dict):
                continue
            agent = data.get("agent") or {}
            steps = data.get("steps") or []
            ts = [s.get("timestamp") for s in steps if isinstance(s, dict) and s.get("timestamp")]
            yield SessionRef(
                "devin", "transcripts", str(data.get("session_id") or path.stem),
                locator={"file": str(path)},
                cwd=None, created=min(ts) if ts else None, updated=max(ts) if ts else None,
                models=[agent["model_name"]] if agent.get("model_name") else [],
                extra={"agent_version": agent.get("version"), "exported": True},
            )
        return
    try:
        conn = open_sqlite_ro(store.path)
    except sqlite3.Error as e:
        store.status, store.error = "unavailable", str(e)
        return
    try:
        workers = porch_workers()
        for row in conn.execute(
            "SELECT id, working_directory, model, agent_mode, created_at, last_activity_at, title, hidden, main_chain_id FROM sessions"
        ):
            yield _devin_ref_from_row(store, row, workers)
    except sqlite3.Error as e:
        store.status, store.error = "unavailable", str(e)
    finally:
        conn.close()


def _devin_ref_from_row(store: Store, row: tuple, workers: set) -> SessionRef:
    worker = ("devin", row[0]) in workers
    # subagent_heads are chains within this session; /fork exposes no parent field.
    return SessionRef(
        "devin", "sessions.db", row[0],
        locator={"table": "sessions", "key": row[0], "db": str(store.path)},
        title=row[6], cwd=row[1], created=row[4], updated=row[5],
        models=[row[2]] if row[2] else [],
        hidden=bool(row[7]),
        extra={"agent_mode": row[3], "main_chain_id": row[8], "native_id": row[0], "surface": "cli",
               "session_class": "headless" if worker else "unknown",
               "class_basis": "porch registry" if worker else "no discovery evidence", "edges": [],
               "native_status": {"hidden": bool(row[7]), "agent_mode": row[3]},
               "sources": [{"db": str(store.path), "table": "sessions", "key": row[0], "role": "metadata"},
                           {"db": str(store.path), "table": "message_nodes", "key": row[0], "role": "content"}]},
    )


def devin_ref_for_session(store: Store, conn: sqlite3.Connection, sid: str) -> SessionRef | None:
    row = conn.execute("SELECT id, working_directory, model, agent_mode, created_at, last_activity_at, title, hidden, main_chain_id FROM sessions WHERE id=?", (sid,)).fetchone()
    return _devin_ref_from_row(store, row, porch_workers()) if row else None


def devin_iter_fragments(store: Store, ref: SessionRef, max_chars: int, stats: dict[str, int], conn: sqlite3.Connection | None = None) -> Iterator[dict[str, Any]]:
    if store.name == "transcripts":
        path = Path(ref.locator["file"])
        data = read_json(path) or {}
        for seq, step in enumerate(data.get("steps") or [], 1):
            if not isinstance(step, dict):
                continue
            source = step.get("source")
            kind = {"user": "prompt", "agent": "assistant", "tool": "tool_result", "system": "context"}.get(str(source), "unknown")
            yield frag("devin", "transcripts", ref.session_id, seq, kind, f"step.{source}",
                       {"file": str(path), "step": step.get("step_id") or seq},
                       text=step.get("message"), ts=step.get("timestamp"),
                       authorship={"user": "unknown", "agent": "agent", "tool": "system", "system": "system"}.get(str(source), "unknown"),
                       visible=kind in ("prompt", "assistant"), max_chars=max_chars)
        return
    owned = conn is None
    if owned:
        conn = open_sqlite_ro(store.path)
        try:
            conn.execute('BEGIN')
        except BaseException:
            conn.close()
            raise
    try:
        yield from _devin_db_fragments(store, ref, max_chars, stats, conn)
    finally:
        if owned:
            conn.close()


def _devin_db_fragments(store: Store, ref: SessionRef, max_chars: int, stats: dict[str, int],
                        conn: sqlite3.Connection) -> Iterator[dict[str, Any]]:
    """Keep chain metadata in memory and stream message bodies from one snapshot."""
    node_metadata = "metadata" if "metadata" in table_columns(conn, "message_nodes") else "NULL"
    nodes = {row[0]: row for row in conn.execute(
        f"SELECT node_id, parent_node_id, {node_metadata} FROM message_nodes WHERE session_id=? ORDER BY node_id", (ref.session_id,))}
    heads: list[tuple] = []
    if table_exists(conn, "subagent_heads"):
        try:
            heads = conn.execute("SELECT * FROM subagent_heads WHERE session_id=?", (ref.session_id,)).fetchall()
        except sqlite3.Error:
            heads = []
    canonical: set[Any] = set()
    tip = ref.extra.get("main_chain_id")
    has_chain = tip in nodes
    pending = [tip] if has_chain else []
    while pending:
        node_id = pending.pop()
        if node_id not in nodes or node_id in canonical:
            continue
        canonical.add(node_id)
        row = nodes[node_id]
        try:
            metadata = json.loads(row[2] or "{}") or {}
        except (ValueError, TypeError):
            metadata = {}
        if not isinstance(metadata, dict):
            metadata = {}
        pending.extend((row[1], metadata.get("summarized_from")))
        pending.extend((metadata.get("extensions") or {}).get("compact/prior_node_ids") or [])
    seq = 0
    rows = conn.execute("SELECT row_id, node_id, parent_node_id, chat_message, created_at FROM message_nodes WHERE session_id=? ORDER BY node_id",
                        (ref.session_id,))
    for row_id, node_id, parent_id, blob, created in rows:
        seq += 1
        loc = {"db": str(store.path), "table": "message_nodes", "row_id": row_id, "node_id": node_id}
        try:
            msg = json.loads(blob)
        except json.JSONDecodeError:
            stats["malformed"] = stats.get("malformed", 0) + 1
            continue
        finally:
            blob = None
        role = msg.get("role")
        metadata = msg.get("metadata") or {}
        created = metadata.get("created_at") or created
        rel = {"node_id": node_id, "parent_node_id": parent_id, "message_id": msg.get("message_id")}
        evidence = {"node_id": node_id, "message_id": msg.get("message_id"), "finish_reason": metadata.get("finish_reason"), "has_tool_calls": bool(msg.get("tool_calls")), "canonical": node_id in canonical if has_chain else None}
        if role == "user":
            yield frag("devin", "sessions.db", ref.session_id, seq, "prompt", "message_nodes.user", loc,
                       text=msg.get("content"), ts=created, authorship="unknown", visible=True,
                       basis="role=user (may include orchestrator prompts)", rel=rel,
                       origin=("delegated" if is_delegated_brief(msg.get("content") or "") else "human") if metadata.get("is_user_input") else "injected",
                       origin_basis="is_user_input absent" if not metadata.get("is_user_input") else "executor-prefix" if is_delegated_brief(msg.get("content") or "") else "is_user_input=1",
                       native=evidence, max_chars=max_chars)
        elif role == "assistant":
            thinking = (msg.get("thinking") or {}).get("thinking")
            if thinking:
                yield frag("devin", "sessions.db", ref.session_id, seq, "reasoning", "message_nodes.thinking", loc,
                           text=thinking, ts=created, authorship="agent", visible=False, rel=rel, native=evidence, max_chars=max_chars)
                seq += 1
            if msg.get("content"):
                yield frag("devin", "sessions.db", ref.session_id, seq, "assistant", "message_nodes.assistant", loc,
                           text=msg.get("content"), ts=created, authorship="agent", visible=True, rel=rel,
                           status=metadata.get("finish_reason"), native=evidence, max_chars=max_chars)
                seq += 1
            for call in msg.get("tool_calls") or []:
                yield frag("devin", "sessions.db", ref.session_id, seq, "tool_call", "message_nodes.tool_call", loc,
                           text=json.dumps({"name": call.get("name"), "arguments": call.get("arguments")}, ensure_ascii=False),
                           ts=created, authorship="agent", visible=False,
                           rel={**rel, "call_id": call.get("id"), "tool": call.get("name")}, native=evidence, max_chars=max_chars)
                seq += 1
            if not thinking and not msg.get("content") and not msg.get("tool_calls"):
                yield frag("devin", "sessions.db", ref.session_id, seq, "metadata", "message_nodes.assistant_status", loc,
                           ts=created, authorship="agent", visible=False, rel=rel, native=evidence, max_chars=max_chars)
        elif role == "tool":
            yield frag("devin", "sessions.db", ref.session_id, seq, "tool_result", "message_nodes.tool", loc,
                       text=msg.get("content"), ts=created, authorship="system", visible=False, rel=rel,
                       native=evidence, max_chars=max_chars)
        elif role == "system":
            yield frag("devin", "sessions.db", ref.session_id, seq, "context", "message_nodes.system", loc,
                       text=msg.get("content"), ts=created, authorship="system", visible=False, rel=rel,
                       native=evidence, max_chars=max_chars)
        else:
            yield frag("devin", "sessions.db", ref.session_id, seq, "unknown", f"message_nodes.{role}", loc,
                       text=msg.get("content") or msg, ts=created, rel=rel, native=evidence, max_chars=max_chars)
    for head in heads:
        seq += 1
        yield frag("devin", "sessions.db", ref.session_id, seq, "metadata", "subagent_heads", 
                   {"db": str(store.path), "table": "subagent_heads"},
                   text=json.dumps(head[1:] if isinstance(head, tuple) else head, ensure_ascii=False, default=str),
                   authorship="system", visible=False, basis="subagent chain head within this session",
                   max_chars=max_chars)


# ---------------------------------------------------------------------------
# gemini-cli
# ---------------------------------------------------------------------------

def _gemini_projects_map(base: Path) -> dict[str, str]:
    data = read_json(base / "projects.json")
    out = {}
    if isinstance(data, dict):
        for cwd, slug in (data.get("projects") or {}).items():
            out[slug] = cwd
    return out


def gemini_iter_sessions(store: Store) -> Iterator[SessionRef]:
    if store.name == "projects-map":
        # informational store — sessions live under chats
        store.status = "ok" if store.path.exists() else "missing"
        return
    slug2cwd = _gemini_projects_map(store.path.parent)
    for path in sorted(_walk(store.path, "chats/session-*.json", store.walk_stats)):
        proj_hash = path.relative_to(store.path).parts[0]
        data = read_json(path)
        if not isinstance(data, dict):
            continue
        msgs = data.get("messages") or []
        models = [m["model"] for m in msgs if isinstance(m, dict) and m.get("model")]
        yield SessionRef(
            "gemini", "chats", str(data.get("sessionId") or path.stem),
            locator={"file": str(path)},
            cwd=slug2cwd.get(proj_hash) or data.get("cwd"),
            created=data.get("startTime"), updated=data.get("lastUpdated"),
            models=models, extra={"project_hash": proj_hash},
        )


def gemini_iter_fragments(store: Store, ref: SessionRef, max_chars: int, stats: dict[str, int]) -> Iterator[dict[str, Any]]:
    path = Path(ref.locator["file"])
    data = read_json(path)
    if not isinstance(data, dict):
        stats["malformed"] = stats.get("malformed", 0) + 1
        return
    for seq, msg in enumerate(data.get("messages") or [], 1):
        if not isinstance(msg, dict):
            continue
        loc = {"file": str(path), "message_index": seq - 1}
        t = msg.get("type")
        ts = msg.get("timestamp")
        rel = {"message_id": msg.get("id")}
        if t == "user":
            yield frag("gemini", "chats", ref.session_id, seq, "prompt", "user", loc,
                       text=msg.get("content"), ts=ts, authorship="human", visible=True,
                       basis="type=user checkpoint record", rel=rel, max_chars=max_chars)
        elif t == "gemini":
            yield frag("gemini", "chats", ref.session_id, seq, "assistant", "gemini", loc,
                       text=msg.get("content") or msg.get("text"), ts=ts, authorship="agent",
                       model=msg.get("model"), rel=rel, max_chars=max_chars)
            for call in msg.get("toolCalls") or []:
                yield frag("gemini", "chats", ref.session_id, seq, "tool_call", "gemini.toolCalls", loc,
                           text=json.dumps(call, ensure_ascii=False), ts=ts, authorship="agent",
                           visible=False, rel={**rel, "tool": call.get("name")}, max_chars=max_chars)
        elif t in ("info", "error", "warning"):
            yield frag("gemini", "chats", ref.session_id, seq, "metadata", t, loc,
                       text=msg.get("content"), ts=ts, authorship="system", visible=True, rel=rel,
                       max_chars=max_chars)
        else:
            yield frag("gemini", "chats", ref.session_id, seq, "unknown", str(t), loc,
                       text=msg.get("content") or msg, ts=ts, rel=rel, max_chars=max_chars)


# ---------------------------------------------------------------------------
# claude-desktop (local agent mode)
# ---------------------------------------------------------------------------

def _claude_desktop_manifest(store: Store, meta_path: Path, data: dict[str, Any]) -> SessionRef:
    sid = str(data.get("sessionId") or data.get("session_id") or meta_path.stem)
    sess_dir = meta_path.with_suffix("")
    locator: dict[str, Any] = {"file": str(meta_path)}
    if sess_dir.is_dir():
        locator["dir"] = str(sess_dir)
    edges = []
    def add_edge(kind: str, harness: str, target: Any, basis: str, attrs: dict[str, Any] | None = None) -> None:
        if target:
            edges.append({"type": kind, "target_harness": harness, "target_id": target, "unresolved": None,
                          "basis": basis, "locator": {"file": str(meta_path), **(attrs or {})}})
    add_edge("alias", "claude-code", data.get("cliSessionId"), "cliSessionId")
    spawned = data.get("spawnedFrom")
    if isinstance(spawned, dict):
        add_edge("spawned", "claude-desktop", spawned.get("sessionId"), "spawnedFrom.sessionId", spawned)
    add_edge("fork", "claude-desktop", data.get("forkedFromSessionId"), "forkedFromSessionId",
             {"forkedAtMessageUuid": data.get("forkedAtMessageUuid")})
    for prior in data.get("priorCliSessionIds") or []:
        add_edge("continuation", "claude-code", prior, "priorCliSessionIds")
    status = {k: data[k] for k in ("interruptedAwaitingPerson", "cleanShutdown", "error", "errorCategory", "errorAt", "completedTurns", "isArchived", "lastAssistantCliSessionId") if k in data}
    return SessionRef(
        "claude-desktop", store.name, sid,
        locator=locator,
        title=data.get("title") or (data.get("initialMessage") or "")[:80] or None,
        cwd=data.get("cwd") or data.get("workingDirectory"),
        created=data.get("createdAt") or data.get("created_at"),
        updated=data.get("lastActivityAt") or data.get("updatedAt") or data.get("updated_at"),
        models=[data["model"]] if data.get("model") else [],
        hidden=bool(data.get("isArchived")),
        extra={k: v for k, v in {
            "cli_session_id": data.get("cliSessionId"),
            "prior_cli_session_ids": data.get("priorCliSessionIds"),
            "effort": data.get("effort") or data.get("effortOverride"),
            "permission_mode": data.get("permissionMode"),
            "turns": data.get("completedTurns"),
            "process_name": data.get("processName"),
            "titleSource": data.get("titleSource"),
            "surface": "code-tab" if store.name == "code-sessions" else "cowork",
            "native_id": sid, "session_class": "interactive", "class_basis": "desktop manifest",
            "edges": edges, "native_status": status, "sources": [{"path": str(meta_path), "role": "metadata"}],
        }.items() if v is not None},
    )


def claude_desktop_iter_sessions(store: Store) -> Iterator[SessionRef]:
    if not store.path.is_dir():
        store.status = "missing"
        return
    for meta_path in sorted(_walk(store.path, "local_*.json", store.walk_stats)):
        ref = claude_desktop_ref_for_file(store, meta_path)
        if ref is not None:
            yield ref


def claude_desktop_ref_for_file(store: Store, path: Path) -> SessionRef | None:
    data = read_json(path)
    return _claude_desktop_manifest(store, path, data) if isinstance(data, dict) else None


def _claude_desktop_head(store: Store, ref: SessionRef, max_chars: int) -> tuple[list[dict[str, Any]], dict[str, Any], int]:
    """The manifest's own fragments, its parsed data, and the last seq used."""
    out: list[dict[str, Any]] = []
    seq = 0
    meta: dict[str, Any] = {}
    meta_file = ref.locator.get("file") or ref.locator.get("meta")
    if meta_file:
        data = read_json(Path(meta_file))
        if isinstance(data, dict):
            meta = data
            out.append(frag("claude-desktop", store.name, ref.session_id, seq, "metadata", "manifest", 
                       {"file": str(meta_file)},
                       text={k: meta.get(k) for k in
                             ("title", "model", "effort", "permissionMode", "completedTurns",
                              "cliSessionId", "cwd", "processName", "isArchived") if meta.get(k) is not None},
                       ts=meta.get("createdAt"), authorship="system", visible=False,
                       model=meta.get("model"), cwd=meta.get("cwd"),
                       basis="desktop session manifest", max_chars=max_chars))
    for prior in meta.get("priorCliSessionIds") or []:
        seq += 1
        out.append(frag("claude-desktop", store.name, ref.session_id, seq, "metadata", "prior_cli_session",
                   {"file": str(meta_file)} if meta_file else ref.locator,
                   text=None, authorship="system", visible=False,
                   basis="session continues an earlier CLI transcript",
                   rel={"cli_session_id": prior, "continued_from": True}, max_chars=max_chars))

    return out, meta, seq


_CLAUDE_TRANSCRIPTS: dict[Path, dict[str, Path]] = {}


def _claude_transcript(cli_sid: str) -> Path | None:
    """The claude-code transcript of a desktop code session, as rglob(f"{cli_sid}.jsonl")[0] found it.

    One walk per process replaces a walk of ~/.claude/projects per session.
    """
    root = _root_override("claude-code") or (HOME / ".claude" / "projects")
    if not root.is_dir():
        return None
    if root not in _CLAUDE_TRANSCRIPTS:
        index: dict[str, Path] = {}
        for path in _walk(root, "*.jsonl"):
            index.setdefault(path.name, path)
        _CLAUDE_TRANSCRIPTS[root] = index
    return _CLAUDE_TRANSCRIPTS[root].get(f"{cli_sid}.jsonl")


def claude_desktop_iter_fragments(store: Store, ref: SessionRef, max_chars: int, stats: dict[str, int]) -> Iterator[dict[str, Any]]:
    head, meta, seq = _claude_desktop_head(store, ref, max_chars)
    yield from head
    meta_file = ref.locator.get("file") or ref.locator.get("meta")

    if store.name == "code-sessions":
        cli_sid = meta.get("cliSessionId")
        transcript = _claude_transcript(cli_sid) if cli_sid else None
        hits = [transcript] if transcript else []
        if hits:
            sub = Store("claude-code", "via-claude-desktop", hits[0].parent, "jsonl_dir")
            sref = SessionRef("claude-code", "via-claude-desktop", cli_sid,
                              locator={"file": str(hits[0])})
            for f in claude_iter_fragments(sub, sref, max_chars, stats):
                seq += 1
                f["seq"] = seq
                f["harness"] = "claude-desktop"
                f["store"] = store.name
                f.setdefault("rel", {})["cli_session_id"] = cli_sid
                yield f
        else:
            seq += 1
            yield frag("claude-desktop", store.name, ref.session_id, seq, "metadata", "cli_transcript",
                       ref.locator, text={"cliSessionId": cli_sid},
                       authorship="system", visible=False,
                       basis="cliSessionId has no transcript under ~/.claude/projects",
                       rel={"cli_session_id": cli_sid}, max_chars=max_chars)
        return

    sess_dir = ref.locator.get("dir")
    if not sess_dir:
        seq += 1
        yield frag("claude-desktop", store.name, ref.session_id, seq, "metadata", "no_workdir",
                   ref.locator, text=None, authorship="system", visible=False,
                   basis="manifest has no sibling local_* working dir", max_chars=max_chars)
        return
    if meta.get("initialMessage"):
        seq += 1
        yield frag("claude-desktop", store.name, ref.session_id, seq, "prompt", "manifest.initialMessage",
                   {"file": str(meta_file)} if meta_file else {"dir": sess_dir},
                   text=meta["initialMessage"], ts=meta.get("createdAt"), authorship="human",
                   visible=True, basis="user-entered first message recorded in the manifest", origin="human", origin_basis="initialMessage",
                   max_chars=max_chars)
    inner = Path(sess_dir) / ".claude" / "projects"
    if inner.is_dir():
        seq += 1
        yield frag("claude-desktop", store.name, ref.session_id, seq, "metadata", "inner_transcript",
                   {"dir": str(inner)}, text=None, authorship="system", visible=False,
                   basis="inner claude-code subprocess transcripts live under .claude/projects",
                   max_chars=max_chars)
    audit = Path(sess_dir) / "audit.jsonl"
    for line_no, rec in iter_jsonl(audit, stats):
        seq += 1
        loc = {"file": str(audit), "line": line_no}
        t = rec.get("type")
        ts = rec.get("timestamp") or rec.get("_audit_timestamp")
        sidechain = bool(rec.get("parent_tool_use_id"))
        rel = {"uuid": rec.get("uuid"), "parent_tool_use_id": rec.get("parent_tool_use_id")}
        msg = rec.get("message") if isinstance(rec.get("message"), dict) else None
        if t == "user" and msg is not None:
            content = msg.get("content")
            blocks = [{"type": "text", "text": content}] if isinstance(content, str) else [b for b in (content or []) if isinstance(b, dict)]
            for b in blocks:
                seq += 1
                bt = b.get("type")
                if bt == "tool_result":
                    yield frag("claude-desktop", store.name, ref.session_id, seq, "tool_result", "user.tool_result", loc,
                               text=_tool_result_text(b), ts=ts, authorship="system", visible=False,
                               error=bool(b.get("is_error")),
                               rel={**rel, "call_id": b.get("tool_use_id")}, max_chars=max_chars)
                else:
                    marker = (b.get("text") or "").lstrip().startswith("<system-reminder>")
                    yield frag("claude-desktop", store.name, ref.session_id, seq,
                               "context" if marker else "prompt", "user", loc, text=b.get("text"), ts=ts,
                               authorship="agent" if sidechain else ("system" if marker else "unknown"),
                               visible=False if (sidechain or marker) else True,
                               basis="parent_tool_use_id set → subagent record" if sidechain else None,
                               rel=rel, origin="injected" if sidechain or marker else "human",
                               origin_basis="parent_tool_use_id" if sidechain else "wrapper" if marker else "audit user", max_chars=max_chars)
        elif t == "assistant" and msg is not None:
            model = msg.get("model")
            for b in msg.get("content") or []:
                if not isinstance(b, dict):
                    continue
                seq += 1
                bt = b.get("type")
                kind = {"thinking": "reasoning", "text": "assistant", "tool_use": "tool_call"}.get(str(bt), "unknown")
                yield frag("claude-desktop", store.name, ref.session_id, seq, kind, f"assistant.{bt}", loc,
                           text=b.get("text") or b.get("thinking") or json.dumps({"name": b.get("name"), "input": b.get("input")}, ensure_ascii=False),
                           ts=ts, authorship="agent", visible=(kind == "assistant"), model=model,
                           rel={**rel, "call_id": b.get("id") if bt == "tool_use" else None, "tool": b.get("name")},
                           native={"message_id": msg.get("id"), "stop_reason": msg.get("stop_reason"), "parent_tool_use_id": rec.get("parent_tool_use_id")} if kind == "assistant" else None,
                           max_chars=max_chars)
        elif t == "system" and rec.get("subtype") in ("permission_request", "permission_response"):
            yield frag("claude-desktop", store.name, ref.session_id, seq, "permission",
                       f"system.{rec['subtype']}", loc,
                       text={k: rec.get(k) for k in
                             ("tool_name", "tool_use_id", "decision", "behavior", "suggestions")
                             if rec.get(k) is not None} or None,
                       ts=ts, authorship="system", visible=False,
                       basis="permission/approval event", rel=rel, max_chars=max_chars)
        elif t == "system" and rec.get("subtype") == "init":
            yield frag("claude-desktop", store.name, ref.session_id, seq, "metadata", "system.init", loc,
                       text={k: rec.get(k) for k in ("cliSessionId", "claude_code_version", "permissionMode", "cwd", "model") if rec.get(k)},
                       ts=ts, authorship="system", visible=False, model=rec.get("model"),
                       cwd=rec.get("cwd"), rel=rel, max_chars=max_chars)
        elif t == "result":
            yield frag("claude-desktop", store.name, ref.session_id, seq, "boundary", f"result.{rec.get('subtype')}", loc,
                       text=rec.get("result"), ts=ts, authorship="system", visible=False,
                       status=rec.get("subtype"), basis="turn boundary with cumulative usage",
                       rel=rel, max_chars=max_chars)
        else:
            yield frag("claude-desktop", store.name, ref.session_id, seq, "metadata", str(t), loc,
                       text=None, ts=ts, authorship="system", visible=False, rel=rel, max_chars=max_chars)


# ---------------------------------------------------------------------------
# cursor (state.vscdb)
# ---------------------------------------------------------------------------

def _proto_fields(data: bytes) -> dict[int, list[bytes]]:
    """Decode protobuf wire fields used by Cursor ACP; reject malformed nodes."""
    offset = 0
    fields: dict[int, list[bytes]] = {}
    def varint() -> int:
        nonlocal offset
        value = 0
        for index in range(10):
            if offset >= len(data):
                raise ValueError("truncated protobuf varint")
            byte = data[offset]
            offset += 1
            if index == 9 and byte > 1:
                raise ValueError("invalid protobuf varint")
            value |= (byte & 127) << (index * 7)
            if byte < 128:
                return value
        raise ValueError("invalid protobuf varint")
    while offset < len(data):
        key = varint()
        number, wire = key >> 3, key & 7
        if not number:
            raise ValueError("invalid protobuf field")
        if wire == 0:
            varint()
        elif wire == 2:
            length = varint()
            if length > len(data) - offset:
                raise ValueError("truncated protobuf field")
            fields.setdefault(number, []).append(data[offset:offset + length])
            offset += length
        elif wire in (1, 5):
            offset += 8 if wire == 1 else 4
            if offset > len(data):
                raise ValueError("truncated protobuf field")
        else:
            raise ValueError("unsupported protobuf wire type")
    return fields


def _cursor_acp_read(path: Path) -> tuple[dict[str, Any], dict[str, Any], list[tuple[str, str, str]], int]:
    # Validate the full graph before emitting any text. One SQLite read
    # transaction keeps the root and referenced blobs on the same snapshot.
    if path.is_symlink() or path.parent.is_symlink() or path.parent.parent.is_symlink():
        raise ValueError("symlinked ACP session path")
    session_id = str(uuid.UUID(path.parent.name))
    sidecar_path = path.parent / "meta.json"
    if sidecar_path.is_symlink():
        raise ValueError("symlinked ACP metadata")
    sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
    if not isinstance(sidecar, dict) or sidecar.get("schemaVersion") != 1:
        raise ValueError("unsupported ACP metadata version")
    conn = open_sqlite_ro(path)
    try:
        conn.execute("BEGIN")
        row = conn.execute("SELECT value FROM meta WHERE key='0'").fetchone()
        if not row:
            raise ValueError("missing ACP root")
        metadata = json.loads(bytes.fromhex(row[0]))
        if str(uuid.UUID(metadata["agentId"])) != session_id:
            raise ValueError("ACP session identity mismatch")
        def blob(key: bytes) -> bytes:
            if len(key) != 32:
                raise ValueError("invalid ACP blob id")
            row = conn.execute("SELECT data FROM blobs WHERE id=?", (key.hex(),)).fetchone()
            if not row or not isinstance(row[0], bytes):
                raise ValueError("missing ACP blob")
            return row[0]
        root = _proto_fields(blob(bytes.fromhex(metadata["latestRootBlobId"])))
        result: list[tuple[str, str, str]] = []
        omitted = 0
        for turn_id in root.get(8, []):
            turn = _proto_fields(blob(turn_id))
            agents, shells = turn.get(1, []), turn.get(2, [])
            if len(agents) + len(shells) != 1:
                raise ValueError("invalid ACP turn")
            if shells:
                _proto_fields(shells[0])
                omitted += 1
                continue
            agent = _proto_fields(agents[0])
            users = agent.get(1, [])
            if len(users) != 1:
                raise ValueError("invalid ACP user reference")
            user = _proto_fields(blob(users[0])).get(1, [])
            if not user or not user[0]:
                raise ValueError("missing ACP user text")
            result.append(("prompt", user[0].decode("utf-8"), users[0].hex()))
            for step_id in agent.get(2, []):
                step = _proto_fields(blob(step_id))
                variants = [field for field in (1, 2, 3) if step.get(field)]
                if len(variants) != 1 or len(step[variants[0]]) != 1:
                    raise ValueError("invalid ACP step")
                if variants[0] != 1:
                    omitted += 1
                    continue
                assistant_id = step[1][0]
                assistant = _proto_fields(blob(assistant_id)).get(1, [])
                if not assistant or not assistant[0]:
                    raise ValueError("missing ACP assistant text")
                result.append(("assistant", assistant[0].decode("utf-8"), assistant_id.hex()))
        return sidecar, metadata, result, omitted
    finally:
        conn.close()


def cursor_acp_sessions(store: Store) -> Iterator[SessionRef]:
    for path in sorted(store.path.glob("*/store.db")):
        try:
            sidecar, meta, _, _ = _cursor_acp_read(path)
            sid = "cursor-acp:" + str(uuid.UUID(path.parent.name))
            yield SessionRef("cursor", "acp", sid, locator={"db": str(path), "table": "meta", "key": "0"},
                             title=meta.get("name"), cwd=sidecar.get("cwd"), created=meta.get("createdAt"))
        except (OSError, sqlite3.Error, ValueError, TypeError, KeyError) as exc:
            store.status, store.error = "unavailable", f"ACP store {path}: {exc}"


def cursor_acp_fragments(store: Store, ref: SessionRef, max_chars: int, stats: dict[str, int]) -> Iterator[dict[str, Any]]:
    path = Path(ref.locator["db"])
    try:
        _, _, records, omitted = _cursor_acp_read(path)
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError) as exc:
        store.status, store.error = "unavailable", f"ACP store {path}: {exc}"
        stats["unreadable"] = stats.get("unreadable", 0) + 1
        return
    stats["unsupported_fragments"] = stats.get("unsupported_fragments", 0) + omitted
    for seq, (kind, text_value, blob_id) in enumerate(records, 1):
        yield frag("cursor", "acp", ref.session_id, seq, kind, "acp." + kind,
                   {"db": str(path), "table": "blobs", "key": blob_id}, text=text_value,
                   authorship="unknown" if kind == "prompt" else "agent", visible=True,
                   basis="Cursor ACP text projection", cwd=ref.cwd, max_chars=max_chars)

def _folder_uri_to_path(uri: Any) -> str | None:
    """'file:///Users/x/proj' -> '/Users/x/proj'; plain paths pass through."""
    if not isinstance(uri, str) or not uri:
        return None
    if uri.startswith("file://"):
        from urllib.parse import unquote, urlparse
        return unquote(urlparse(uri).path) or None
    return uri


def cursor_iter_sessions(store: Store) -> Iterator[SessionRef]:
    if store.name == "acp":
        yield from cursor_acp_sessions(store)
        return
    try:
        conn = open_sqlite_ro(store.path)
    except sqlite3.Error as e:
        store.status, store.error = "unavailable", str(e)
        return
    try:
        if not table_exists(conn, "cursorDiskKV"):
            store.status = "empty"
            return
        headers: dict[str, dict[str, Any]] = {}
        if table_exists(conn, "composerHeaders"):
            try:
                for cid, value in conn.execute("SELECT composerId, value FROM composerHeaders"):
                    try:
                        headers[cid] = json.loads(value) if isinstance(value, str) else {}
                    except json.JSONDecodeError:
                        headers[cid] = {}
            except sqlite3.Error:
                pass
        ws_json = read_json(store.path.parent / "workspace.json") or {}
        ws_cwd = _folder_uri_to_path(ws_json.get("folder"))
        for (key, value) in conn.execute("SELECT key, value FROM cursorDiskKV WHERE key LIKE 'composerData:%'"):
            try:
                data = json.loads(value)
            except (json.JSONDecodeError, TypeError):
                continue
            cid = key.split(":", 1)[1]
            header = headers.get(cid) or {}
            ws = (header.get("workspaceIdentifier") or {})
            cwd = None
            if isinstance(ws, dict):
                uri = (ws.get("uri") or {})
                cwd = uri.get("fsPath") if isinstance(uri, dict) else None
            cwd = cwd or ws_cwd
            models = list((data.get("usageData") or {}).keys())
            yield SessionRef(
                "cursor", store.name, cid,
                locator={"db": str(store.path), "table": "cursorDiskKV", "key": key},
                title=data.get("name"), cwd=cwd,
                created=data.get("createdAt"), updated=data.get("lastUpdatedAt"),
                models=models,
                extra={"archived": header.get("isArchived"), "is_subagent": header.get("isSubagent")},
            )
    except sqlite3.Error as e:
        store.status, store.error = "unavailable", str(e)
    finally:
        conn.close()


def cursor_iter_fragments(store: Store, ref: SessionRef, max_chars: int, stats: dict[str, int]) -> Iterator[dict[str, Any]]:
    if store.name == "acp":
        yield from cursor_acp_fragments(store, ref, max_chars, stats)
        return
    conn = open_sqlite_ro(store.path)
    try:
        key = ref.locator["key"]
        row = conn.execute("SELECT value FROM cursorDiskKV WHERE key=?", (key,)).fetchone()
        composer = json.loads(row[0]) if row else {}
        refs = composer.get("fullConversationHeadersOnly") or [
            {"bubbleId": b} for b in (composer.get("conversation") or [])
        ]
        out_rows = []
        for r in refs:
            bid = r.get("bubbleId")
            brow = conn.execute(
                "SELECT value FROM cursorDiskKV WHERE key=?", (f"bubbleId:{ref.session_id}:{bid}",)
            ).fetchone()
            if not brow:
                continue
            try:
                out_rows.append((bid, r.get("type"), json.loads(brow[0])))
            except json.JSONDecodeError:
                stats["malformed"] = stats.get("malformed", 0) + 1
    finally:
        conn.close()
    for seq, (bid, btype, bubble) in enumerate(out_rows, 1):
        loc = {"db": str(store.path), "table": "cursorDiskKV", "key": f"bubbleId:{ref.session_id}:{bid}"}
        if bubble.get("isThought"):
            yield frag("cursor", store.name, ref.session_id, seq, "reasoning", "bubble.thought", loc,
                       text=bubble.get("text"), authorship="agent", visible=False,
                       rel={"bubble_id": bid}, max_chars=max_chars)
            continue
        if btype == 1:
            yield frag("cursor", store.name, ref.session_id, seq, "prompt", "bubble.user", loc,
                       text=bubble.get("text"), authorship="human", visible=True,
                       basis="bubble type=1 (user)", rel={"bubble_id": bid}, max_chars=max_chars)
        elif btype == 2:
            yield frag("cursor", store.name, ref.session_id, seq, "assistant", "bubble.assistant", loc,
                       text=bubble.get("text"), authorship="agent", visible=True,
                       rel={"bubble_id": bid}, max_chars=max_chars)
        tool = bubble.get("toolFormerData")
        if isinstance(tool, dict) and tool.get("name"):
            is_err = str(tool.get("status") or "").lower() in ("error", "failed", "rejected")
            yield frag("cursor", store.name, ref.session_id, seq, "tool_call", "bubble.toolFormerData", loc,
                       text=json.dumps({"name": tool.get("name"), "params": tool.get("params"), "status": tool.get("status")}, ensure_ascii=False),
                       authorship="agent", visible=False, error=is_err,
                       rel={"call_id": tool.get("toolCallId"), "tool": tool.get("name")}, max_chars=max_chars)
            if tool.get("result") is not None:
                yield frag("cursor", store.name, ref.session_id, seq, "tool_result", "bubble.toolFormerData.result", loc,
                           text=tool.get("result"), authorship="system", visible=False, error=is_err,
                           rel={"call_id": tool.get("toolCallId")}, max_chars=max_chars)


# ---------------------------------------------------------------------------
# qwen-code
# ---------------------------------------------------------------------------

QWEN_HUMAN_SUBTYPES = {None, "mid_turn_user_message"}
QWEN_NONHUMAN_SUBTYPES = {"notification", "cron"}


def qwen_iter_sessions(store: Store) -> Iterator[SessionRef]:
    if not store.path.is_dir():
        store.status = "missing"
        return
    for path in sorted(_walk(store.path, "*.jsonl", store.walk_stats)):
        rel = path.relative_to(store.path).as_posix()
        stats: dict[str, int] = {}
        first_ts = last_ts = cwd = sid = None
        models: list[str] = []
        for line_no, rec in iter_jsonl(path, stats):
            if first_ts is None:
                first_ts = rec.get("timestamp")
            if rec.get("timestamp"):
                last_ts = rec["timestamp"]
            sid = sid or rec.get("sessionId") or rec.get("agentId")
            cwd = cwd or rec.get("cwd")
            if rec.get("model") and rec["model"] not in models:
                models.append(rec["model"])
        parent = None
        agent = None
        if "/subagents/" in rel:
            parent = rel.split("/subagents/")[1].split("/")[0]
            agent = path.stem
        yield SessionRef(
            "qwen-code", store.name, str(sid or path.stem),
            locator={"file": str(path)},
            cwd=cwd, created=first_ts, updated=last_ts, models=models,
            parent=parent, agent=agent,
            extra={"rel_path": rel},
        )


def qwen_iter_fragments(store: Store, ref: SessionRef, max_chars: int, stats: dict[str, int]) -> Iterator[dict[str, Any]]:
    path = Path(ref.locator["file"])
    sidechain = "/subagents/" in ref.extra.get("rel_path", "")
    seq = 0
    for line_no, rec in iter_jsonl(path, stats):
        seq += 1
        loc = {"file": str(path), "line": line_no}
        t, subtype = rec.get("type"), rec.get("subtype")
        ts, cwd, model = rec.get("timestamp"), rec.get("cwd"), rec.get("model")
        rel = {"uuid": rec.get("uuid"), "parent": rec.get("parentUuid"), "sessionId": rec.get("sessionId")}
        msg = rec.get("message") if isinstance(rec.get("message"), dict) else {}
        parts = msg.get("parts") or []
        if t == "user" and any(isinstance(p, dict) and p.get("functionResponse") for p in parts):
            text = "\n".join(
                json.dumps(p.get("functionResponse", {}).get("response"), ensure_ascii=False)
                for p in parts if isinstance(p, dict) and p.get("functionResponse")
            ) or json.dumps(rec.get("toolCallResult"), ensure_ascii=False)
            is_err = any(
                isinstance((p.get("functionResponse") or {}).get("response"), dict)
                and (p["functionResponse"]["response"].get("error")
                     or p["functionResponse"]["response"].get("errorMessage"))
                for p in parts if isinstance(p, dict))
            yield frag("qwen-code", store.name, ref.session_id, seq, "tool_result", "user.functionResponse", loc,
                       text=text, ts=ts, authorship="system", visible=False, cwd=cwd, error=is_err,
                       rel={**rel, "call_id": (rec.get("toolCallResult") or {}).get("callId")}, max_chars=max_chars)
        elif t == "user":
            if subtype in QWEN_HUMAN_SUBTYPES:
                auth, kind = ("agent" if sidechain else "human"), "prompt"
                basis = f"subtype={subtype or 'plain'}" + ("; subagent file → task prompt" if sidechain else "")
            elif subtype in QWEN_NONHUMAN_SUBTYPES:
                auth, kind = "system", "context"
                basis = f"subtype={subtype} (auto/scheduled)"
            else:
                auth, kind, basis = "unknown", "prompt", f"unrecognized subtype={subtype!r}"
            text = "\n".join(str(p.get("text", "")) for p in parts if isinstance(p, dict) and p.get("text"))
            yield frag("qwen-code", store.name, ref.session_id, seq, kind, f"user.{subtype or 'plain'}", loc,
                       text=text, ts=ts, authorship=auth, visible=(kind == "prompt"),
                       basis=basis, cwd=cwd, rel=rel, max_chars=max_chars)
        elif t == "assistant":
            for p in parts:
                if not isinstance(p, dict):
                    continue
                seq += 1
                if p.get("functionCall"):
                    fc = p["functionCall"]
                    yield frag("qwen-code", store.name, ref.session_id, seq, "tool_call", "assistant.functionCall", loc,
                               text=json.dumps({"name": fc.get("name"), "args": fc.get("args")}, ensure_ascii=False),
                               ts=ts, authorship="agent", model=model, cwd=cwd,
                               rel={**rel, "call_id": fc.get("id"), "tool": fc.get("name")}, max_chars=max_chars)
                elif p.get("thought") or p.get("type") == "thought":
                    yield frag("qwen-code", store.name, ref.session_id, seq, "reasoning", "assistant.thought", loc,
                               text=p.get("text"), ts=ts, authorship="agent", model=model, cwd=cwd,
                               rel=rel, max_chars=max_chars)
                else:
                    yield frag("qwen-code", store.name, ref.session_id, seq, "assistant", "assistant.text", loc,
                               text=p.get("text"), ts=ts, authorship="agent", model=model, cwd=cwd,
                               rel=rel, max_chars=max_chars)
        elif t == "tool_result":
            text = json.dumps(rec.get("toolCallResult"), ensure_ascii=False)
            yield frag("qwen-code", store.name, ref.session_id, seq, "tool_result", "tool_result", loc,
                       text=text, ts=ts, authorship="system", visible=False, cwd=cwd,
                       rel={**rel, "call_id": (rec.get("toolCallResult") or {}).get("callId")}, max_chars=max_chars)
        elif t == "system":
            yield frag("qwen-code", store.name, ref.session_id, seq, "metadata", f"system.{subtype}", loc,
                       text=None, ts=ts, authorship="system", visible=False, cwd=cwd, rel=rel, max_chars=max_chars)
        else:
            yield frag("qwen-code", store.name, ref.session_id, seq, "unknown", f"{t}.{subtype}", loc,
                       text=rec, ts=ts, cwd=cwd, rel=rel, max_chars=max_chars)


# ---------------------------------------------------------------------------
# kimi-code
# ---------------------------------------------------------------------------

KIMI_ORIGIN_HUMAN = {"user"}
KIMI_ORIGIN_NONHUMAN = {"system_trigger", "background_task", "subagent"}


def kimi_iter_sessions(store: Store) -> Iterator[SessionRef]:
    if not store.path.is_dir():
        store.status = "missing"
        return
    index: dict[str, str] = {}
    for _ln, rec in iter_jsonl(store.path.parent / "session_index.jsonl", {}):
        if rec.get("sessionId") and rec.get("workDir"):
            index[str(rec["sessionId"])] = rec["workDir"]
    for state_path in sorted(_walk(store.path, "state.json", store.walk_stats)):
        state = read_json(state_path)
        sess_dir = state_path.parent
        sid = sess_dir.name
        if not isinstance(state, dict):
            state = {}
        yield SessionRef(
            "kimi-code", store.name, sid,
            locator={"dir": str(sess_dir)},
            title=state.get("title"),
            cwd=state.get("workDir") or index.get(sid),
            created=state.get("createdAt"), updated=state.get("updatedAt"),
            parent=state.get("forkedFrom"),
            extra={"lastPrompt": state.get("lastPrompt"), "agents": list((state.get("agents") or {}).keys())},
        )


def kimi_iter_fragments(store: Store, ref: SessionRef, max_chars: int, stats: dict[str, int]) -> Iterator[dict[str, Any]]:
    sess_dir = Path(ref.locator["dir"])
    merged: list[tuple[float, int, Path, int, dict[str, Any]]] = []
    wire_files = sorted(sess_dir.glob("agents/*/wire.jsonl"))
    for order, wpath in enumerate(wire_files):  # 'main' first, then subagents
        wstats: dict[str, int] = {}
        for line_no, rec in iter_jsonl(wpath, wstats):
            stats.update({k: stats.get(k, 0) + v for k, v in wstats.items()})
            wstats.clear()
            merged.append((ts_epoch(rec.get("time")) or 0.0, 0 if wpath.parent.name == "main" else 1, wpath, line_no, rec))
    merged.sort(key=lambda x: (x[0], x[1], x[3]))
    seen_prompt_texts: set[str] = set()
    seq = 0
    for _t, _ord, wpath, line_no, rec in merged:
        seq += 1
        agent_id = wpath.parent.name
        loc = {"file": str(wpath), "line": line_no, "agent": agent_id}
        t = rec.get("type")
        ts = rec.get("time")
        rel = {"agent": agent_id, "turn_id": None}
        if t in ("turn.prompt", "turn.steer"):
            origin = ((rec.get("origin") or {}).get("kind"))
            text = "\n".join(str(i.get("text", "")) for i in (rec.get("input") or []) if isinstance(i, dict))
            seen_prompt_texts.add(text)
            if origin in KIMI_ORIGIN_HUMAN:
                auth = "human"
            elif origin in KIMI_ORIGIN_NONHUMAN:
                auth = "agent" if origin == "subagent" else "system"
            else:
                auth = "unknown"
            yield frag("kimi-code", store.name, ref.session_id, seq, "prompt", t, loc,
                       text=text, ts=ts, authorship=auth, visible=auth == "human",
                       basis=f"origin.kind={origin!r}", rel=rel, max_chars=max_chars)
        elif t == "context.append_message":
            msg = rec.get("message") or {}
            texts = "\n".join(str(c.get("text", "")) for c in (msg.get("content") or []) if isinstance(c, dict))
            yield frag("kimi-code", store.name, ref.session_id, seq, "context", t, loc,
                       text=texts, ts=ts, authorship="system", visible=False,
                       basis="duplicate layer of turn.prompt (kept for provenance)", rel=rel,
                       max_chars=max_chars)
        elif t == "context.append_loop_event":
            ev = rec.get("event") or {}
            et = ev.get("type")
            rel = {**rel, "turn_id": ev.get("turnId"), "step": ev.get("step")}
            if et == "content.part":
                part = ev.get("part") or {}
                pt = part.get("type")
                if pt == "tool.call" or part.get("toolCallId") and part.get("name"):
                    yield frag("kimi-code", store.name, ref.session_id, seq, "tool_call", "loop.tool.call", loc,
                               text=json.dumps({"name": part.get("name"), "args": part.get("arguments") or part.get("args")}, ensure_ascii=False),
                               ts=ts, authorship="agent",
                               rel={**rel, "call_id": part.get("toolCallId") or part.get("id"), "tool": part.get("name")},
                               max_chars=max_chars)
                elif pt == "tool.result":
                    yield frag("kimi-code", store.name, ref.session_id, seq, "tool_result", "loop.tool.result", loc,
                               text=part.get("result") or part.get("output"), ts=ts, authorship="system",
                               visible=False, error=bool(part.get("isError") or part.get("error")),
                               rel={**rel, "call_id": part.get("toolCallId")}, max_chars=max_chars)
                else:
                    kind = "reasoning" if pt == "think" else "assistant"
                    yield frag("kimi-code", store.name, ref.session_id, seq, kind, f"loop.part.{pt}", loc,
                               text=part.get("text") or part.get("think"), ts=ts, authorship="agent",
                               visible=(kind == "assistant"), rel=rel, max_chars=max_chars)
            elif et in ("step.begin", "step.end"):
                yield frag("kimi-code", store.name, ref.session_id, seq, "boundary", f"loop.{et}", loc,
                           text=None if et == "step.begin" else json.dumps({"finishReason": ev.get("finishReason"), "usage": ev.get("usage")}, ensure_ascii=False),
                           ts=ts, authorship="system", visible=False,
                           status=ev.get("finishReason") if et == "step.end" else None,
                           rel=rel, max_chars=max_chars)
            else:
                yield frag("kimi-code", store.name, ref.session_id, seq, "metadata", f"loop.{et}", loc,
                       text=ev, ts=ts, authorship="system", visible=False, rel=rel, max_chars=max_chars)
        elif t == "permission.set_mode":
            yield frag("kimi-code", store.name, ref.session_id, seq, "permission", t, loc,
                       text=rec.get("mode"), ts=ts, authorship="system", visible=False, rel=rel, max_chars=max_chars)
        else:
            yield frag("kimi-code", store.name, ref.session_id, seq, "metadata", str(t), loc,
                       text=None if t in ("usage.record", "llm.request") else rec, ts=ts,
                       authorship="system", visible=False, rel=rel, max_chars=max_chars)


# ---------------------------------------------------------------------------
# omp
# ---------------------------------------------------------------------------

def omp_iter_sessions(store: Store) -> Iterator[SessionRef]:
    if not store.path.is_dir():
        store.status = "missing"
        return
    for path in sorted(_walk(store.path, "*.jsonl", store.walk_stats)):
        stats: dict[str, int] = {}
        first_ts = last_ts = cwd = title = sid = None
        models: list[str] = []
        for line_no, rec in iter_jsonl(path, stats):
            ts = rec.get("timestamp") or rec.get("updatedAt")
            if first_ts is None:
                first_ts = ts
            if ts:
                last_ts = ts
            t = rec.get("type")
            if t == "session":
                sid = sid or rec.get("id")
                cwd = cwd or rec.get("cwd")
                title = title or rec.get("title")
            elif t in ("title", "title_change") and rec.get("title"):
                title = rec["title"]
            elif t == "model_change" and rec.get("model"):
                models.append(rec["model"])
        yield SessionRef(
            "omp", store.name, str(sid or path.stem),
            locator={"file": str(path)}, title=title, cwd=cwd,
            created=first_ts, updated=last_ts, models=models,
        )


def omp_iter_fragments(store: Store, ref: SessionRef, max_chars: int, stats: dict[str, int]) -> Iterator[dict[str, Any]]:
    path = Path(ref.locator["file"])
    seq = 0
    for line_no, rec in iter_jsonl(path, stats):
        seq += 1
        loc = {"file": str(path), "line": line_no}
        t = rec.get("type")
        ts = rec.get("timestamp") or rec.get("updatedAt")
        rel = {"id": rec.get("id"), "parent": rec.get("parentId")}
        if t == "message":
            msg = rec.get("message") or {}
            role = msg.get("role")
            attribution = msg.get("attribution")
            if role == "user":
                yield frag("omp", store.name, ref.session_id, seq, "prompt", "message.user", loc,
                           text="\n".join(str(c.get("text", "")) for c in (msg.get("content") or []) if isinstance(c, dict)),
                           ts=ts, authorship="agent" if attribution == "agent" else "human",
                           visible=attribution != "agent",
                           basis=f"attribution={attribution!r}", model=msg.get("model"),
                           rel=rel, max_chars=max_chars)
            elif role in ("toolResult", "tool"):
                yield frag("omp", store.name, ref.session_id, seq, "tool_result", "message.toolResult", loc,
                           text="\n".join(str(c.get("text", "")) for c in (msg.get("content") or []) if isinstance(c, dict)),
                           ts=ts, authorship="system", visible=False,
                           error=bool(msg.get("isError") or msg.get("is_error")),
                           rel={**rel, "call_id": msg.get("toolCallId"), "tool": msg.get("toolName")},
                           max_chars=max_chars)
            else:
                for c in msg.get("content") or []:
                    if not isinstance(c, dict):
                        continue
                    seq += 1
                    ct = c.get("type")
                    if ct == "toolCall":
                        yield frag("omp", store.name, ref.session_id, seq, "tool_call", "message.toolCall", loc,
                                   text=json.dumps({"name": c.get("name"), "arguments": c.get("arguments") or c.get("partialArgs")}, ensure_ascii=False),
                                   ts=ts, authorship="agent", model=msg.get("model"),
                                   rel={**rel, "call_id": c.get("id"), "tool": c.get("name")}, max_chars=max_chars)
                    elif ct == "thinking":
                        yield frag("omp", store.name, ref.session_id, seq, "reasoning", "message.thinking", loc,
                                   text=c.get("thinking"), ts=ts, authorship="agent", visible=False,
                                   model=msg.get("model"), rel=rel, max_chars=max_chars)
                    else:
                        yield frag("omp", store.name, ref.session_id, seq, "assistant", f"message.{ct}", loc,
                                   text=c.get("text"), ts=ts, authorship="agent", model=msg.get("model"),
                                   status=msg.get("stopReason"), rel=rel, max_chars=max_chars)
        elif t == "compaction":
            yield frag("omp", store.name, ref.session_id, seq, "compaction", "compaction", loc,
                       text=rec.get("summary") or rec.get("shortSummary"), ts=ts, authorship="system",
                       visible=False, rel=rel, max_chars=max_chars)
        elif t in ("session", "title", "title_change", "model_change", "thinking_level_change"):
            yield frag("omp", store.name, ref.session_id, seq, "metadata", t, loc,
                       text=rec.get("title") or rec.get("model") or rec.get("thinkingLevel"),
                       ts=ts, authorship="system", visible=False,
                       model=rec.get("model") if t == "model_change" else None,
                       rel=rel, max_chars=max_chars)
        elif t == "custom":
            ct = rec.get("customType")
            data = rec.get("data") or {}
            kind = "tool_call" if ct == "tool_execution_start" else "metadata"
            yield frag("omp", store.name, ref.session_id, seq, kind, f"custom.{ct}", loc,
                       text=data.get("intent") or data.get("args") or rec.get("text"), ts=ts,
                       authorship="agent" if kind == "tool_call" else "system", visible=False,
                       rel={**rel, "call_id": data.get("toolCallId"), "tool": data.get("toolName")},
                       max_chars=max_chars)
        else:
            yield frag("omp", store.name, ref.session_id, seq, "metadata", str(t), loc,
                       text=None, ts=ts, authorship="system", visible=False, rel=rel, max_chars=max_chars)


# ---------------------------------------------------------------------------
# porch's own delegate runs
# ---------------------------------------------------------------------------

def porch_iter_sessions(store: Store) -> Iterator[SessionRef]:
    if not store.path.is_dir():
        store.status = "missing"
        return
    for run_dir in sorted(store.path.iterdir()):
        ref = porch_ref_for_run(store, run_dir)
        if ref is not None:
            yield ref


def porch_ref_for_run(store: Store, run_dir: Path) -> SessionRef | None:
    meta = read_json(run_dir / "meta.json")
    if not isinstance(meta, dict):
        return None
    handle = meta.get("session_handle") or {}
    edges = []
    locator = {"file": str(run_dir / "meta.json")}
    def edge(kind: str, harness: str | None, target: str | None, unresolved: str | None, basis: str) -> None:
        edges.append({"type": kind, "target_harness": harness, "target_id": target,
                      "unresolved": unresolved, "basis": basis, "locator": locator})
    launcher = meta.get("launcher_instance")
    if launcher:
        edge("caller_run", None, None, launcher, "launcher_instance")
        if launcher.startswith("run:"):
            edge("parent_run", "porch", launcher[4:], None, "launcher_instance")
    worker = meta.get("native_session_id") or handle.get("thread_id")
    harness = BACKEND_HARNESS.get(meta.get("backend"))
    if worker and harness:
        edge("worker_session", harness, worker, None, "native_session_id" if meta.get("native_session_id") else "session_handle.thread_id")
    if meta.get("continued_from"):
        edge("continuation", "porch", meta["continued_from"], None, "continued_from")
    return SessionRef(
        "porch", "runs", str(meta.get("run_id") or run_dir.name),
        locator={"dir": str(run_dir)},
        title=(meta.get("task") or "")[:120] or None,
        cwd=meta.get("cwd"), created=meta.get("launched_at") or meta.get("started_at"),
        updated=meta.get("finished_at") or meta.get("last_event_at"),
        models=[meta["model"]] if meta.get("model") else [],
        extra={
            "agent_id": meta.get("agent_id"), "backend": meta.get("backend"),
            "effort": meta.get("effort"), "status": meta.get("status"),
            "native_session": meta.get("native_session_id") or handle.get("thread_id"),
            "launcher": meta.get("launcher"),
            "launcher_instance": meta.get("launcher_instance"),
            "continued_from": meta.get("continued_from"),
            "exit_code": meta.get("exit_code"),
            "finished_at": meta.get("finished_at"),
            "native_id": str(meta.get("run_id") or run_dir.name),
            "session_class": "headless", "class_basis": "porch run",
            "native_status": {**{k: meta.get(k) for k in ("status", "exit_code", "finished_at", "artifacts_dir", "backend", "continued_from", "agent_id", "model", "error", "final_available")},
                              "started_at": meta.get("started_at") or meta.get("launched_at"), "native_session_id": meta.get("native_session_id")},
            "edges": edges, "sources": [{"path": str(run_dir / "meta.json"), "role": "metadata"}],
        },
    )


def porch_iter_fragments(store: Store, ref: SessionRef, max_chars: int, stats: dict[str, int]) -> Iterator[dict[str, Any]]:
    run_dir = Path(ref.locator["dir"])
    norm_dir = run_dir / "normalized"
    files = sorted(norm_dir.glob("*.jsonl")) if norm_dir.is_dir() else sorted(run_dir.glob("normalized/*.jsonl"))
    meta = read_json(run_dir / "meta.json")
    meta = meta if isinstance(meta, dict) else {}
    if not files:
        if meta.get("artifacts_dir"):
            norm_dir = Path(meta["artifacts_dir"]) / "normalized"
            files = sorted(norm_dir.glob("*.jsonl")) if norm_dir.is_dir() else []
        if not files:
            stats["unreadable"] = stats.get("unreadable", 0) + 1
    seq = 0
    if isinstance(meta.get("task"), str) and meta["task"].strip():
        seq += 1
        yield frag("porch", "runs", ref.session_id, seq, "prompt", "meta.task", {"file": str(run_dir / "meta.json")},
                   text=meta["task"], ts=meta.get("launched_at") or meta.get("started_at"), authorship="agent",
                   visible=False, origin="delegated", origin_basis="run task", max_chars=max_chars)
    for npath in files:
        for line_no, rec in iter_jsonl(npath, stats):
            seq += 1
            t = rec.get("type")
            loc = {"file": str(npath), "line": line_no}
            if t in ("run_started",):
                yield frag("porch", "runs", ref.session_id, seq, "metadata", t, loc,
                           text={k: rec.get(k) for k in ("agent", "backend", "model", "effort", "mode") if rec.get(k)},
                           ts=rec.get("ts") or rec.get("timestamp"), authorship="system", visible=False,
                           max_chars=max_chars)
            elif t in ("steer", "user", "prompt"):
                yield frag("porch", "runs", ref.session_id, seq, "prompt", t, loc,
                           text=rec.get("text") or rec.get("prompt") or rec.get("data"),
                           ts=rec.get("ts") or rec.get("timestamp"), authorship="agent",
                           visible=False, basis="orchestrator steer/prompt", origin="delegated", origin_basis="orchestrator steer/prompt", max_chars=max_chars)
            elif t in ("tool", "tool_call", "tool_started"):
                yield frag("porch", "runs", ref.session_id, seq, "tool_call", t, loc,
                           text=rec.get("data") if t == "tool_started" else json.dumps({"name": rec.get("name"), "chunks": rec.get("chunks")}, ensure_ascii=False),
                           ts=rec.get("ts") or rec.get("timestamp"), authorship="agent", visible=False,
                           rel={"tool": rec.get("name")}, max_chars=max_chars)
            elif t in ("text", "answering", "answer", "message", "answer_delta"):
                yield frag("porch", "runs", ref.session_id, seq, "assistant", t, loc,
                           text=rec.get("text") or rec.get("data"), ts=rec.get("ts") or rec.get("timestamp"),
                           authorship="agent", visible=False, max_chars=max_chars)
            elif t == "thinking_delta":
                yield frag("porch", "runs", ref.session_id, seq, "reasoning", t, loc,
                           text=rec.get("data"), ts=rec.get("ts") or rec.get("timestamp"),
                           authorship="agent", visible=False, max_chars=max_chars)
            elif t in ("end", "run_finished", "completed", "turn_started", "turn_completed", "run_completed"):
                yield frag("porch", "runs", ref.session_id, seq, "boundary", t, loc,
                           text=rec.get("data"), ts=rec.get("ts") or rec.get("timestamp"),
                           authorship="system", status=rec.get("status"),
                           error=rec.get("status") in ("failed", "error"),
                           max_chars=max_chars)
            else:
                yield frag("porch", "runs", ref.session_id, seq, "metadata", str(t), loc,
                           text=rec.get("data") or rec.get("text"), ts=rec.get("ts") or rec.get("timestamp"),
                           authorship="system", visible=False, max_chars=max_chars)
    final = _porch_final_answer(run_dir, meta)
    if final is not None:
        path, text = final
        seq += 1
        yield frag("porch", "runs", ref.session_id, seq, "assistant", "final.txt",
                   {"file": str(path)}, text=text, ts=meta.get("finished_at"),
                   authorship="agent", visible=True,
                   basis="the run's saved final answer (final.txt)", native={"source": "final.txt"}, max_chars=max_chars)


def _porch_final_answer(run_dir: Path, meta: dict[str, Any]) -> tuple[Path, str] | None:
    """The run's saved final answer: run_dir/final.txt, else artifacts_dir's."""
    candidates = [run_dir / "final.txt"]
    artifacts = meta.get("artifacts_dir")
    if artifacts:
        candidates.append(Path(artifacts) / "final.txt")
    for path in candidates:
        try:
            if path.is_file():
                return path, path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
    return None


# ---------------------------------------------------------------------------
# Hermes current state database (do not read its config or system prompts)
# ---------------------------------------------------------------------------

def hermes_iter_sessions(store: Store) -> Iterator[SessionRef]:
    try:
        conn = open_sqlite_ro(store.path)
        try:
            columns = table_columns(conn, "sessions")
            required = {"id", "started_at"}
            if not required.issubset(columns) or not table_exists(conn, "messages"):
                raise sqlite3.OperationalError("unsupported Hermes state database schema")
            optional = ("title", "cwd", "model", "parent_session_id", "ended_at", "source")
            select = ", ".join(name if name in columns else f"NULL AS {name}" for name in optional)
            rows = conn.execute(f"SELECT id, started_at, {select} FROM sessions").fetchall()
        finally:
            conn.close()
        for sid, started, title, cwd, model, parent, ended, source in rows:
            yield SessionRef("hermes", store.name, str(sid),
                locator={"db": str(store.path), "table": "sessions", "key": sid},
                title=title, cwd=cwd, created=started, updated=ended,
                models=[model] if model else [], parent=parent, extra={"source": source})
    except sqlite3.Error as exc:
        store.status, store.error = "unavailable", str(exc)


def hermes_iter_fragments(store: Store, ref: SessionRef, max_chars: int, stats: dict[str, int]) -> Iterator[dict[str, Any]]:
    try:
        conn = open_sqlite_ro(store.path)
        try:
            columns = table_columns(conn, "messages")
            if not {"id", "session_id", "role", "content", "timestamp"}.issubset(columns):
                raise sqlite3.OperationalError("unsupported Hermes messages schema")
            optional = ("tool_calls", "tool_call_id", "tool_name", "reasoning", "reasoning_content")
            select = ", ".join(name if name in columns else f"NULL AS {name}" for name in optional)
            rows = conn.execute(f"SELECT id, role, content, timestamp, {select} FROM messages WHERE session_id=? ORDER BY timestamp, id", (ref.session_id,)).fetchall()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        store.status, store.error = "unavailable", str(exc)
        stats["unreadable"] = stats.get("unreadable", 0) + 1
        return
    seq = 0
    for mid, role, content, timestamp, calls, call_id, tool_name, reasoning, reasoning_content in rows:
        loc = {"db": str(store.path), "table": "messages", "key": mid}
        items = []
        if reasoning or reasoning_content:
            items.append(("reasoning", reasoning or reasoning_content, "agent"))
        kind, author = {"user": ("prompt", "unknown"), "assistant": ("assistant", "agent"),
                        "tool": ("tool_result", "system"), "system": ("context", "system")}.get(role, ("unknown", "unknown"))
        if content:
            items.append((kind, content, author))
        if calls:
            items.append(("tool_call", calls, "agent"))
        for fragment_kind, text_value, authorship in items:
            seq += 1
            yield frag("hermes", store.name, ref.session_id, seq, fragment_kind, f"messages.{role}", loc,
                       text=text_value, ts=timestamp, authorship=authorship, cwd=ref.cwd,
                       basis="role=user; local record does not prove human authorship" if role == "user" else "Hermes message role",
                       rel={"call_id": call_id, "tool": tool_name}, max_chars=max_chars)


# ---------------------------------------------------------------------------
# agy — Google Antigravity CLI (~/.gemini/antigravity-cli)
#
# Content lives in brain/<cid>/.system_generated/logs/transcript_full.jsonl;
# conversation_metadata.json (cache/) lists summaries for both CLI and IDE
# rows. conversations/<cid>.db and the shared summaries DB are NEVER opened:
# read-only SQLite opens create -wal/-shm sidecars in the user's store.
# ---------------------------------------------------------------------------

_AGY_USER_REQUEST_RE = re.compile(r"<USER_REQUEST>(.*?)</USER_REQUEST>", re.DOTALL)
_AGY_SENDER_RE = re.compile(r"\[Message\][^\n]*?sender=(\S+)")
_AGY_MSG_CONTENT_RE = re.compile(r"\[Message\][^\n]*?content=(.*?)</SYSTEM_MESSAGE>", re.DOTALL)
_AGY_TITLE_RE = re.compile(r'title:"((?:[^"\\]|\\.)*)"')
_AGY_STALE_SECONDS = 60


def _agy_metadata(store: Store) -> dict[str, Any]:
    data = read_json(store.path / "cache" / "conversation_metadata.json")
    convs = data.get("conversations") if isinstance(data, dict) else None
    return convs if isinstance(convs, dict) else {}


def _agy_annotation_title(store: Store, cid: str) -> str | None:
    try:
        text = (store.path / "annotations" / f"{cid}.pbtxt").read_text(
            encoding="utf-8", errors="replace")
    except OSError:
        return None
    m = _AGY_TITLE_RE.search(text)
    if not m:
        return None
    raw = m.group(1)
    try:
        return json.loads(f'"{raw}"')
    except json.JSONDecodeError:
        return raw


def _agy_transcript_path(store: Store, cid: str) -> Path | None:
    logs = store.path / "brain" / cid / ".system_generated" / "logs"
    for name in ("transcript_full.jsonl", "transcript.jsonl"):
        path = logs / name
        if path.is_file():
            return path
    return None


def _agy_subagent_parents(store: Store) -> dict[str, tuple[str, Path, dict[str, Any]]]:
    """child conversation id -> (parent cid, descriptor path, descriptor)."""
    brain = store.path / "brain"
    out: dict[str, tuple[str, Path, dict[str, Any]]] = {}
    if not brain.is_dir():
        return out
    for desc in _walk(brain, "*/.system_generated/subagents/*.json", store.walk_stats):
        rel = desc.relative_to(brain)
        if len(rel.parts) < 4:
            continue
        data = read_json(desc)
        data = data if isinstance(data, dict) else {}
        child = data.get("conversationId") or desc.stem
        out[str(child)] = (rel.parts[0], desc, data)
    return out


def _agy_head(store: Store, path: Path, max_lines: int = 8) -> list[dict[str, Any]]:
    stats: dict[str, int] = {}
    return [rec for _ln, rec in iter_jsonl(path, stats, max_lines=max_lines)]


def _agy_sender_parent(content: Any) -> str | None:
    """Parent conversation id from a '[Message] ... sender=<parent>[/task-N]' envelope."""
    if not isinstance(content, str):
        return None
    m = _AGY_SENDER_RE.search(content)
    if not m:
        return None
    return m.group(1).split("/", 1)[0] or None


def _agy_prompt_origin(text: str) -> tuple[str, str]:
    if is_delegated_brief(text):
        return "delegated", "executor-prefix"
    return "human", "USER_REQUEST"


def agy_iter_sessions(store: Store) -> Iterator[SessionRef]:
    meta, parents = agy_shared(store)
    brain = store.path / "brain"
    conv_dir = store.path / "conversations"
    brain_ids = {p.name for p in brain.iterdir() if p.is_dir()} if brain.is_dir() else set()
    db_ids = {p.name[:-3] for p in conv_dir.glob("*.db")} if conv_dir.is_dir() else set()
    for cid in sorted(set(meta) | brain_ids | db_ids):
        yield agy_ref_for_conversation(store, cid, (meta, parents))


def agy_shared(store: Store) -> tuple:
    return _agy_metadata(store), _agy_subagent_parents(store)


def agy_ref_for_conversation(store: Store, cid: str, shared: tuple) -> SessionRef:
    meta, parents = shared
    entry = meta.get(cid)
    entry = entry if isinstance(entry, dict) else {}
    summary = entry.get("summary") if isinstance(entry.get("summary"), dict) else {}
    transcript = _agy_transcript_path(store, cid)
    head = _agy_head(store, transcript) if transcript else []
    first_created = next((r.get("created_at") for r in head if r.get("created_at")), None)
    # Class evidence: subagent edge > IDE row > delegated brief > user prompt.
    parent_edge = parents.get(cid)
    sender_parent = None
    delegated_marker = None
    has_user_input = False
    for rec in head:
        if rec.get("source") == "SYSTEM" and rec.get("type") == "SYSTEM_MESSAGE":
            sender_parent = sender_parent or _agy_sender_parent(rec.get("content"))
        if (rec.get("source") == "USER_EXPLICIT" and rec.get("type") == "USER_INPUT"
                and not has_user_input):
            has_user_input = True
            content = rec.get("content")
            m = _AGY_USER_REQUEST_RE.search(content) if isinstance(content, str) else None
            prompt_text = (m.group(1) if m else content) or ""
            if is_delegated_brief(prompt_text):
                delegated_marker = "executor-prefix"
    preview = summary.get("Preview") or ""
    ide_row = summary.get("AppDataDir") == "antigravity"
    edge_parent, edge_path, edge_data = parent_edge if parent_edge else (None, None, {})
    if edge_parent is None and sender_parent:
        edge_parent, edge_path = sender_parent, transcript
    edges = []
    if edge_parent:
        basis = "subagent descriptor" if parent_edge else "SYSTEM_MESSAGE sender"
        edges.append({
            "type": "subagent", "target_harness": "agy", "target_id": edge_parent,
            "unresolved": None, "basis": basis,
            "locator": {"file": str(edge_path)} if edge_path else {},
        })
    if edge_parent:
        session_class, class_basis = "subagent", edges[0]["basis"]
    elif ("agy", cid) in porch_workers():
        session_class, class_basis = "headless", "porch registry"
    elif ide_row:
        session_class, class_basis = "interactive", "AppDataDir=antigravity"
    elif delegated_marker:
        session_class, class_basis = "headless", delegated_marker
    elif has_user_input:
        session_class = "interactive"
        class_basis = "transcript USER_INPUT"
    else:
        session_class, class_basis = "unknown", "no class evidence"
    # Status facts: content availability and staleness vs conversation
    # metadata. Discovery stays cheap (stat + metadata only): the
    # NumSteps-vs-transcript check runs post-read via agy_content_stale.
    native_status: dict[str, Any] = {}
    num_steps = summary.get("NumSteps")
    if isinstance(num_steps, int):
        native_status["NumSteps"] = num_steps
    updated_raw = summary.get("UpdatedAt") or entry.get("last_modified_time")
    stale = False
    if transcript is None or ide_row:
        # IDE rows are metadata-only by contract: content is never read.
        native_status["content_unavailable"] = True
    else:
        try:
            mtime = transcript.stat().st_mtime
        except OSError:
            mtime = None
        updated_epoch = ts_epoch(updated_raw)
        if mtime is not None and updated_epoch is not None and mtime < updated_epoch - _AGY_STALE_SECONDS:
            stale = True
        if stale:
            native_status["content_stale"] = True
    title = _agy_annotation_title(store, cid) or summary.get("Title") or None
    uris = summary.get("WorkspaceURIs")
    cwd = next((p for p in (_folder_uri_to_path(u) for u in (uris or []) if isinstance(u, str)) if p), None)
    sources = []
    meta_path = store.path / "cache" / "conversation_metadata.json"
    if cid in meta:
        sources.append({"path": str(meta_path), "role": "metadata"})
    ann = store.path / "annotations" / f"{cid}.pbtxt"
    if ann.is_file():
        sources.append({"path": str(ann), "role": "metadata"})
    if transcript is not None:
        sources.append({"path": str(transcript), "role": "content"})
    if parent_edge:
        sources.append({"path": str(parent_edge[1]), "role": "lineage"})
    descriptor = edge_data.get("subagentDescriptor") if isinstance(edge_data.get("subagentDescriptor"), dict) else {}
    agent = descriptor.get("typeName") or descriptor.get("role")
    return SessionRef(
        "agy", store.name, cid,
        locator={"file": str(transcript)} if transcript else {"file": str(meta_path)},
        title=title, cwd=cwd,
        created=first_created,
        updated=updated_raw or (transcript.stat().st_mtime if transcript else None),
        parent=edge_parent, agent=agent,
        extra={
            "native_id": cid, "surface": "ide" if ide_row else "cli",
            "session_class": session_class, "class_basis": class_basis,
            "edges": edges, "native_status": native_status, "sources": sources,
        })


def agy_iter_fragments(store: Store, ref: SessionRef, max_chars: int, stats: dict[str, int]) -> Iterator[dict[str, Any]]:
    path = _agy_transcript_path(store, ref.session_id)
    meta_path = store.path / "cache" / "conversation_metadata.json"
    if ref.extra.get("surface") == "ide" or path is None:
        # IDE rows are metadata-only by contract; CLI rows without a transcript
        # still surface their metadata as one context fragment.
        yield frag("agy", store.name, ref.session_id, 1, "metadata",
                   "conversation_metadata" if path is None else "ide_row",
                   {"file": str(meta_path)},
                   text={"title": ref.title, "cwd": ref.cwd,
                         "native_status": ref.extra.get("native_status")},
                   ts=ref.updated, authorship="system", visible=False,
                   basis="IDE metadata row" if ref.extra.get("surface") == "ide"
                   else "known conversation without transcript",
                   max_chars=max_chars)
        return
    seq = 0
    for line_no, rec in iter_jsonl(path, stats):
        source, rtype = rec.get("source"), rec.get("type")
        status, ts = rec.get("status"), rec.get("created_at")
        content = rec.get("content")
        loc = {"file": str(path), "line": line_no}
        native_type = f"{source}.{rtype}"
        step_idx = rec.get("step_index")
        step_native = {"step": step_idx} if isinstance(step_idx, int) else None

        def emit(kind: str, ntype: str, *, native: dict[str, Any] | None = None,
                 **kw: Any) -> dict[str, Any]:
            merged = dict(step_native or {})
            if native:
                merged.update(native)
            return frag("agy", store.name, ref.session_id, seq, kind, ntype, loc,
                        native=merged or None, **kw)

        if source == "USER_EXPLICIT" and rtype == "USER_INPUT":
            text = content if isinstance(content, str) else ""
            m = _AGY_USER_REQUEST_RE.search(text)
            prompt_text = (m.group(1).strip() if m else text.strip())
            origin, origin_basis = _agy_prompt_origin(prompt_text)
            seq += 1
            yield emit("prompt", native_type,
                       text=prompt_text, ts=ts,
                       authorship="agent" if origin == "delegated" else "human",
                       visible=True, basis="USER_EXPLICIT/USER_INPUT",
                       origin=origin, origin_basis=origin_basis,
                       status=status, cwd=ref.cwd, max_chars=max_chars)
            rest = ((text[:m.start()] + text[m.end():]) if m else "").strip()
            if rest:
                seq += 1
                yield emit("context", native_type + ".metadata",
                           text=rest, ts=ts, authorship="system", visible=False,
                           basis="USER_INPUT non-request blocks",
                           status=status, cwd=ref.cwd, max_chars=max_chars)
        elif source == "SYSTEM" and rtype == "SYSTEM_MESSAGE":
            sender = _agy_sender_parent(content)
            if sender:
                inner = _AGY_MSG_CONTENT_RE.search(content) if isinstance(content, str) else None
                brief = inner.group(1).strip() if inner else (content or "")
                seq += 1
                yield emit("prompt", native_type,
                           text=brief, ts=ts, authorship="system", visible=True,
                           basis="SYSTEM_MESSAGE [Message] envelope",
                           origin="delegated", origin_basis="system-envelope sender",
                           rel={"sender": sender}, status=status, cwd=ref.cwd,
                           max_chars=max_chars)
            else:
                seq += 1
                yield emit("context", native_type,
                           text=content, ts=ts, authorship="system", visible=False,
                           basis="SYSTEM_MESSAGE", status=status, cwd=ref.cwd,
                           max_chars=max_chars)
        elif source == "SYSTEM" and rtype == "ERROR_MESSAGE":
            seq += 1
            yield emit("context", native_type,
                       text=content if content is not None else rec.get("error"), ts=ts,
                       authorship="system", visible=False, error=True,
                       basis="ERROR_MESSAGE", status=status, cwd=ref.cwd,
                       max_chars=max_chars)
        elif source == "SYSTEM" and rtype == "CHECKPOINT":
            seq += 1
            yield emit("compaction", native_type,
                       text=content, ts=ts, authorship="system", visible=False,
                       basis="CHECKPOINT", status=status, cwd=ref.cwd, max_chars=max_chars)
        elif source == "SYSTEM":
            seq += 1
            yield emit("context", native_type,
                       text=content, ts=ts, authorship="system", visible=False,
                       basis=rtype or native_type, status=status, cwd=ref.cwd,
                       max_chars=max_chars)
        elif source == "MODEL" and rtype == "PLANNER_RESPONSE":
            model = rec.get("model")
            usage = {k: rec[k] for k in ("input_tokens", "output_tokens", "cache_read_tokens")
                     if isinstance(rec.get(k), (int, float))}
            tool_calls = [tc for tc in (rec.get("tool_calls") or []) if isinstance(tc, dict)]
            native = {"status": status, "has_tool_calls": bool(tool_calls)}
            if rec.get("thinking"):
                seq += 1
                yield emit("reasoning", native_type + ".thinking",
                           text=rec.get("thinking"), ts=ts,
                           authorship="agent", visible=False, model=model,
                           basis="thinking", status=status, cwd=ref.cwd,
                           max_chars=max_chars)
            seq += 1
            yield emit("assistant", native_type,
                       text=rec.get("content"), ts=ts, authorship="agent", visible=True,
                       model=model, native=native, usage=usage or None,
                       basis="PLANNER_RESPONSE", status=status, cwd=ref.cwd,
                       max_chars=max_chars)
            for tc in tool_calls:
                seq += 1
                yield emit("tool_call", native_type + ".tool_call",
                           text={"name": tc.get("name"), "input": tc.get("args")}, ts=ts,
                           authorship="agent", visible=False, model=model,
                           rel={"tool": tc.get("name")},
                           status=status, cwd=ref.cwd, max_chars=max_chars)
        elif source == "MODEL" and rtype == "GENERIC":
            seq += 1
            yield emit("tool_result", native_type,
                       text=content, ts=ts, authorship="system", visible=False,
                       error=(status == "ERROR" or bool(rec.get("error"))),
                       basis="GENERIC tool result",
                       rel={"step": step_idx},
                       status=status, cwd=ref.cwd, max_chars=max_chars)
        elif source == "MODEL":
            seq += 1
            yield emit("metadata", native_type,
                       text=rec, ts=ts, authorship="system", visible=False,
                       basis=rtype or native_type, status=status, cwd=ref.cwd,
                       max_chars=max_chars)
        elif source == "USER_EXPLICIT":
            seq += 1
            yield emit("context", native_type,
                       text=content, ts=ts, authorship="human", visible=False,
                       basis=rtype or native_type, status=status, cwd=ref.cwd,
                       max_chars=max_chars)
        else:
            seq += 1
            yield emit("metadata", native_type,
                       text=rec, ts=ts, authorship="unknown", visible=False,
                       basis="unrecognized record", status=status, cwd=ref.cwd,
                       max_chars=max_chars)


def agy_content_stale(ref: SessionRef, fragments: list[dict[str, Any]]) -> bool:
    """True when the transcript's distinct step records are fewer than the
    NumSteps the conversation metadata claims — i.e. content was truncated or
    the transcript lags the index. Runs post-read; discovery only stores
    NumSteps in native_status."""
    num = (ref.extra.get("native_status") or {}).get("NumSteps")
    if not isinstance(num, int) or num <= 0:
        return False
    steps = set()
    for f in fragments:
        native = f.get("native")
        if isinstance(native, dict) and isinstance(native.get("step"), int):
            steps.add(native["step"])
    return bool(steps) and len(steps) < num


def agy_turns(ref: SessionRef, fragments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collect each text-bearing, tool-free DONE planner response in order."""
    turns = _answer_segments(fragments)
    out = []
    last_i = len(turns) - 1
    for i, turn in enumerate(turns):
        finals = [f for f in turn["frags"] if f.get("kind") == "assistant" and
                  (f.get("native") or {}).get("has_tool_calls") is False and
                  (f.get("native") or {}).get("status") == "DONE" and str(f.get("text") or "").strip()]
        if not turn["prompts"] and not finals:
            continue
        final = finals[-1] if finals else None
        last_assistant = next((f for f in reversed(turn["frags"]) if f.get("kind") == "assistant"), None)
        last_complete = last_assistant in finals if last_assistant is not None else False
        out.append({
            "prompts": turn["prompts"], "final": final, "finals": finals,
            "final_basis": "native:PLANNER_RESPONSE" if final is not None else "none",
            "open": bool(turn["prompts"]) and i == last_i and not last_complete,
            "ts_start": turn["ts_start"], "ts_end": turn["ts_end"],
        })
    return out


TURN_RULES["agy"] = agy_turns


# ---------------------------------------------------------------------------
# dispatch tables
# ---------------------------------------------------------------------------

# Readers whose fragments can be built from any subset of a file's records,
# for RawPrefilter: (store, ref, path, records, max_chars) -> fragments.
RECORD_READERS = {
    "claude-code": _claude_record_fragments,
    "codex": _codex_record_fragments,
}

LIGHT_READERS = {
    # harness: (iter light refs (store, need_cwd), hydrate (store, ref) -> full ref)
    "claude-code": (_claude_light_sessions, _claude_hydrate),
}

READERS = {
    "claude-code": (claude_iter_sessions, claude_iter_fragments),
    "codex": (codex_iter_sessions, codex_iter_fragments),
    "opencode": (opencode_iter_sessions, opencode_iter_fragments),
    "grok": (grok_iter_sessions, grok_iter_fragments),
    "devin": (devin_iter_sessions, devin_iter_fragments),
    "gemini": (gemini_iter_sessions, gemini_iter_fragments),
    "claude-desktop": (claude_desktop_iter_sessions, claude_desktop_iter_fragments),
    "cursor": (cursor_iter_sessions, cursor_iter_fragments),
    "qwen-code": (qwen_iter_sessions, qwen_iter_fragments),
    "kimi-code": (kimi_iter_sessions, kimi_iter_fragments),
    "omp": (omp_iter_sessions, omp_iter_fragments),
    "porch": (porch_iter_sessions, porch_iter_fragments),
    "hermes": (hermes_iter_sessions, hermes_iter_fragments),
    "agy": (agy_iter_sessions, agy_iter_fragments),
}


def resolve_harnesses(raw: str | None) -> list[str]:
    if not raw:
        return list(ALL_HARNESSES)
    out = []
    for part in raw.split(","):
        part = part.strip()
        if part and part not in READERS:
            raise SystemExit(f"unknown harness: {part!r} (known: {', '.join(ALL_HARNESSES)})")
        if part:
            out.append(part)
    return out


def selected_stores(harnesses: list[str], store_filter: str | None = None) -> list[Store]:
    out: list[Store] = []
    for h in harnesses:
        for s in stores_for(h):
            if store_filter and store_filter not in s.name and store_filter not in str(s.path):
                continue
            out.append(s)
    return out


def iter_all_sessions(store: Store, cwd: str | None, since: float | None, until: float | None, *,
                      cwd_exact: bool = False, light: bool = False) -> Iterator[SessionRef]:
    """Sessions of one store that pass the cwd and date filters.

    light=True lets a harness in LIGHT_READERS yield refs without the
    metadata that costs a full read; the caller hydrates the ones it emits.
    """
    if store.status != "ok":
        return
    light_reader = LIGHT_READERS.get(store.harness) if since is None and until is None else None
    try:
        if light_reader and (light or cwd):
            # Without dates only the cwd decides, and a light reader knows it
            # from the head of the file: the rest is read for kept sessions only.
            iter_light, hydrate = light_reader
            for ref in iter_light(store, bool(cwd)):
                if cwd_exact and cwd is not None and ref.cwd != cwd:
                    continue
                if ref.matches(None if cwd_exact else cwd, None, None):
                    yield ref if light else hydrate(store, ref)
            return
        iter_sessions, _ = READERS[store.harness]
        for ref in iter_sessions(store):
            if cwd_exact and cwd is not None and ref.cwd != cwd:
                continue
            if ref.matches(None if cwd_exact else cwd, since, until):
                yield ref
    except (OSError, sqlite3.Error) as e:
        store.status, store.error = "unavailable", str(e)


def iter_all_fragments(store: Store, ref: SessionRef, max_chars: int, stats: dict[str, int]) -> Iterator[dict[str, Any]]:
    _, iter_fragments = READERS[store.harness]
    try:
        yield from iter_fragments(store, ref, max_chars, stats)
    except (OSError, sqlite3.Error) as e:
        store.status, store.error = "unavailable", str(e)


def in_scope(fragment: dict[str, Any], scope: str) -> bool:
    kinds = SCOPES.get(scope)
    return kinds is None or fragment.get("kind") in kinds


def in_window(fragment: dict[str, Any], since: float | None, until: float | None) -> bool:
    if since is None and until is None:
        return True
    epoch = ts_epoch(fragment.get("ts"))
    if epoch is None:
        return since is None  # undated fragments only surface when no lower bound set
    if since is not None and epoch < since:
        return False
    if until is not None and epoch > until:
        return False
    return True


def emit(obj: dict[str, Any]) -> None:
    print(json.dumps(obj, ensure_ascii=False, default=str), flush=True)


def coverage_summary(stores: list[Store], scanned: int, matched: int, stats: dict[str, int], *, truncated: bool = False) -> dict[str, Any]:
    store_report = [s.as_dict() | {"sessions_scanned": None} for s in stores]
    return {
        "_summary": True,
        "stores": store_report,
        "sessions_scanned": scanned,
        "fragments_emitted": matched,
        "malformed_records": stats.get("malformed", 0),
        "truncated_tail_files": stats.get("truncated_tail", 0),
        "unreadable_files": stats.get("unreadable", 0),
        "compressed_errors": stats.get("compressed_errors", 0),
        "unsupported_fragments": stats.get("unsupported_fragments", 0),
        "scan_complete": not truncated and not stats.get("unreadable") and not stats.get("unsupported_fragments") and all(s.status != "unavailable" for s in stores),
        "note": "empty result means: no matches AND scan_complete — check stores[].status for missing/unavailable",
    }


# ---------------------------------------------------------------------------
# grep: raw-byte prefilter
# ---------------------------------------------------------------------------

_PREFILTER_CHUNK = 1 << 20
_PREFILTER_ERRORS: tuple[type[BaseException], ...] | None = None


def _prefilter_read_errors() -> tuple[type[BaseException], ...]:
    """Errors that mean "cannot read this file", which keep it for the full parse to report."""
    global _PREFILTER_ERRORS
    if _PREFILTER_ERRORS is None:
        try:
            from compression import zstd
            _PREFILTER_ERRORS = (OSError, EOFError, ImportError, zstd.ZstdError)
        except ImportError:
            _PREFILTER_ERRORS = (OSError, EOFError, ImportError)
    return _PREFILTER_ERRORS


# What json.dumps writes between string values: structural characters, the
# space of its separators, numbers and the words json.loads accepts.
_JSON_GAP_WORDS = ("true", "false", "null", "NaN", "Infinity")
_JSON_GAP_CHARS = frozenset("{}[],: 0123456789.+-") | frozenset("".join(_JSON_GAP_WORDS))
# String values the claude-code and codex readers put into fragment text
# themselves: the keys of the tool-call objects they serialize.
_SYNTHETIC_STRINGS = ("name", "input", "arguments")
# What json.dumps(ensure_ascii=False) can write after a backslash: a short
# escape letter, or \u00XX in lowercase hex for a control character. A
# pattern may start inside such an escape.
_ESCAPE_TAILS = tuple(re.compile(p) for p in (
    r"u00[01][0-9a-f]", r"00[01][0-9a-f]", r"0[01][0-9a-f]", r"[01][0-9a-f]", r"[0-9a-fbnrt]"))
_CASE_TABLE: tuple[dict[str, set[str]], list[tuple[str, str]]] | None = None


def _case_table() -> tuple[dict[str, set[str]], list[tuple[str, str]]]:
    """Which characters lowercase to which, over planes 0 and 1.

    Returns {lowercase char: chars whose lower() is exactly it} and the
    characters whose lower() is several characters (U+0130). No character
    beyond plane 1 has a case mapping; the test suite checks that.
    """
    global _CASE_TABLE
    if _CASE_TABLE is None:
        single: dict[str, set[str]] = {}
        multi: list[tuple[str, str]] = []
        for cp in range(0x20000):
            if 0xD800 <= cp <= 0xDFFF:
                continue
            ch = chr(cp)
            low = ch.lower()
            if len(low) != 1:
                multi.append((ch, low))
            elif low != ch:
                single.setdefault(low, set()).add(ch)
        _CASE_TABLE = (single, multi)
    return _CASE_TABLE


def _json_gap_capable(text: str, folded: bool) -> bool:
    """Could text occur in what json.dumps writes between two string values?"""
    words = tuple(w.lower() for w in _JSON_GAP_WORDS) if folded else _JSON_GAP_WORDS
    chars = frozenset(c.lower() for c in _JSON_GAP_CHARS) if folded else _JSON_GAP_CHARS
    if not set(text) <= chars:
        return False
    return all(any(run in w for w in words) for run in re.findall(r"[A-Za-z]+", text))


def _hex_class(digit: str) -> bytes:
    return f"[{digit}{digit.upper()}]".encode() if digit.isalpha() else digit.encode()


class RawPrefilter:
    """Proves from a session file's raw bytes that a literal grep pattern cannot match.

    grep matches decoded fragment text. In the claude-code and codex readers
    that text is a string value of a record, json.dumps(ensure_ascii=False)
    of part of a record, or such pieces joined by newlines. A pattern with
    no quote, backslash or control character therefore lies inside one
    string value, unless json.dumps could also write it between strings or
    it is part of a key the reader adds; then the prefilter is off. The
    pattern may begin inside an escape json.dumps wrote for a control
    character (\\n, \\u001b), so those leading characters are dropped. The
    rest is content of one string value, which the session's writer stored
    as UTF-8 bytes or escaped as \\uXXXX, a surrogate pair or \\/. A file
    with neither the bytes nor such an escape of one of those characters
    holds no match. -i searches ASCII-lowercased bytes, with every other
    character that lowercases into the pattern spelled out.

    Anything in doubt keeps the file: it is then parsed exactly as without
    the prefilter, so the output is the same, only faster.
    """

    def __init__(self, pattern: str, *, ignore_case: bool, regex: bool):
        self.skipped = 0
        self.reason: str | None = None
        self._fold = False  # ASCII-lowercase the bytes before matching
        self._literal: bytes | None = None
        self._regex: re.Pattern[bytes] | None = None
        self._special: tuple[bytes, ...] = ()
        self._escapes: re.Pattern[bytes] | None = None
        if regex:
            self.reason = "regex pattern"
            return
        text = pattern.lower() if ignore_case else pattern
        if not text:
            self.reason = "empty pattern"
            return
        bad = next((c for c in text if c in '"\\' or ord(c) < 0x20
                    or 0xD800 <= ord(c) <= 0xDFFF or c == "\ufffd"), None)
        if bad is not None:
            self.reason = f"pattern contains {bad!r}"
            return
        if _json_gap_capable(text, ignore_case):
            self.reason = "pattern can match JSON syntax outside strings"
            return
        if any(text in (w.lower() if ignore_case else w) for w in _SYNTHETIC_STRINGS):
            self.reason = "pattern is part of a key porch adds to tool calls"
            return
        skip = max((m.end() for m in (tail.match(text) for tail in _ESCAPE_TAILS) if m), default=0)
        rest = text[skip:]
        if not rest:
            self.reason = "pattern can lie inside a JSON escape"
            return
        escaped = set(rest)
        if ignore_case:
            single, multi = _case_table()
            special: set[str] = set()
            parts: list[bytes] = []
            for ch in rest:
                # str.lower() maps a word-final capital sigma to ς, any other to σ.
                variants = set(single.get(ch, ())) | ({"Σ"} if ch == "ς" else set())
                escaped |= variants
                if ch.isascii():
                    # bytes.lower() folds ASCII; other characters that
                    # lowercase to this one (KELVIN SIGN) keep the file.
                    special |= {v for v in variants if not v.isascii()}
                    parts.append(re.escape(ch.encode()))
                else:
                    alts = sorted({ch} | variants)
                    parts.append(b"(?:" + b"|".join(re.escape(v.encode()) for v in alts) + b")")
            for ch, low in multi:
                if set(low) & set(rest):
                    special.add(ch)
                    escaped.add(ch)
            self._special = tuple(sorted(c.encode() for c in special))
            # Other characters' variants are spelled out in the regex.
            self._fold = any(c.isascii() and c.isalpha() for c in rest)
            if rest.isascii():
                self._literal = rest.encode()
            else:
                self._regex = re.compile(b"".join(parts))
        else:
            self._literal = rest.encode()
        alts = [b"".join(_hex_class(d) for d in f"{ord(c):04x}") for c in sorted(escaped) if ord(c) <= 0xFFFF]
        if any(ord(c) > 0xFFFF for c in escaped):
            alts.append(rb"[dD][89abAB][0-9a-fA-F]{2}")
        self._escapes = re.compile(rb"\\u(?:" + b"|".join(alts) + rb")")
        if "/" in escaped:
            self._special += (b"\\/",)

    @property
    def active(self) -> bool:
        return self.reason is None

    def may_match(self, path: Path) -> bool:
        """Byte check alone: False only if no line of the file could hold a match."""
        try:
            return any(True for _ in self._candidate_lines(path))
        except _prefilter_read_errors():
            return True

    def session_may_match(self, path: Path,
                          fragments_of: Callable[[Iterable[tuple[int, dict[str, Any]]]], Iterable[dict[str, Any]]],
                          wanted: Callable[[dict[str, Any]], bool]) -> bool:
        """False only if no fragment of the session can be one that wanted() accepts.

        Every fragment's text comes from its own record, so only lines that
        pass the byte check are parsed, and their records go through the
        harness's own reader (fragments_of) and grep's scope, window and
        match. Words that every session carries in bookkeeping records, such
        as the skill listing, then cost a parse of those lines only.
        A file that cannot be read or decompressed is kept, so that the full
        parse reports it exactly as it would without the prefilter.
        """
        try:
            return any(wanted(f) for f in fragments_of(self._candidate_records(path)))
        except _prefilter_read_errors():
            return True

    def _chunks(self, path: Path) -> Iterator[bytes]:
        if path.name.endswith(".jsonl.zst"):
            from compression import zstd
            with zstd.open(path, "rb") as fh:
                yield from iter(lambda: fh.read(_PREFILTER_CHUNK), b"")
        else:
            with path.open("rb") as fh:
                yield from _bounded_chunks(fh, os.fstat(fh.fileno()).st_size, _PREFILTER_CHUNK)

    def _candidate_lines(self, path: Path) -> Iterator[bytes]:
        """Each line that passes the byte check.

        Nothing the check looks for spans a newline, so a run of complete
        lines that fails it as a whole holds no candidate and is not split.
        """
        partial: list[bytes] = []  # the unfinished last line, kept without copying
        for chunk in self._chunks(path):
            cut = chunk.rfind(b"\n") + 1
            if not cut:
                partial.append(chunk)
                continue
            lines = b"".join([*partial, chunk[:cut]]) if partial else chunk[:cut]
            partial = [chunk[cut:]] if cut < len(chunk) else []
            if self._hit(lines):
                yield from (line for line in lines.split(b"\n")[:-1] if self._hit(line))
        tail = b"".join(partial)
        if tail and self._hit(tail):
            yield tail

    def _candidate_records(self, path: Path) -> Iterator[tuple[int, dict[str, Any]]]:
        # Line numbers would only label locators of fragments that are never emitted.
        for line in self._candidate_lines(path):
            rec = _parse_jsonl_line(line.decode("utf-8", "replace"), {})
            if rec is not None:
                yield 0, rec

    def _hit(self, buf: bytes) -> bool:
        if self._escapes is not None and self._escapes.search(buf):
            return True
        if any(s in buf for s in self._special):
            return True
        hay = buf.lower() if self._fold else buf
        if self._literal is not None:
            return self._literal in hay
        return self._regex is not None and self._regex.search(hay) is not None

    def report(self) -> dict[str, Any]:
        if not self.active:
            return {"active": False, "reason": self.reason,
                    "hint": "a literal pattern without quotes, backslashes or JSON words lets grep skip "
                            "claude-code and codex sessions that cannot match"}
        return {"active": True, "skipped_sessions": self.skipped,
                "note": "skipped sessions provably hold no match; malformed_records and "
                        "truncated_tail_files count parsed sessions only"}


def _raw_prefilter_path(store: Store, ref: SessionRef) -> Path | None:
    """The plain JSONL file this ref's fragments come from, if RawPrefilter covers its reader."""
    if store.harness == "claude-code":
        file = ref.locator.get("file")
    elif store.harness == "codex" and store.name != "history":
        file = _codex_rollout_for(ref)  # None: a db-only thread, emitted from its index row
    else:
        return None
    if not file or not str(file).endswith((".jsonl", ".jsonl.zst")):
        return None
    return Path(file)


def _file_verdict(harness: str, path: Path, store: Store, ref: SessionRef, prefilter: RawPrefilter,
                  wanted: Callable[[dict[str, Any]], bool], verdicts: dict[tuple[str, str], bool]) -> bool:
    """Whether a session file can hold a wanted fragment, judged once per file.

    Codex index rows and desktop code sessions point at files another store
    already judged; the verdict depends on the file's records alone.
    """
    key = (harness, str(path))
    if key not in verdicts:
        verdicts[key] = prefilter.session_may_match(
            path, lambda records: RECORD_READERS[harness](store, ref, path, records, 0), wanted)
    return verdicts[key]


def _prefilter_may_match(store: Store, ref: SessionRef, prefilter: RawPrefilter,
                         wanted: Callable[[dict[str, Any]], bool], verdicts: dict[tuple[str, str], bool]) -> bool:
    """False only if the session provably holds no fragment that wanted() accepts."""
    raw_path = _raw_prefilter_path(store, ref)
    if raw_path is not None:
        return _file_verdict(store.harness, raw_path, store, ref, prefilter, wanted, verdicts)
    if store.harness == "claude-desktop" and store.name == "code-sessions":
        head, meta, _seq = _claude_desktop_head(store, ref, 0)
        cli_sid = meta.get("cliSessionId")
        transcript = _claude_transcript(cli_sid) if cli_sid else None
        if transcript is None or any(wanted(f) for f in head):
            return True
        sub = Store("claude-code", "via-claude-desktop", transcript.parent, "jsonl_dir")
        sref = SessionRef("claude-code", "via-claude-desktop", cli_sid, locator={"file": str(transcript)})
        return _file_verdict("claude-code", transcript, sub, sref, prefilter, wanted, verdicts)
    # Devin's sqlite store is not prefiltered: reading its message rows costs
    # as much as parsing them (an 8 GiB database here), measured 2026-11-15.
    return True


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------

def cmd_roots(args: argparse.Namespace) -> int:
    stores = selected_stores(resolve_harnesses(args.agent), getattr(args, "store", None))
    for s in stores:
        emit(s.as_dict())
    emit({"_summary": True, "stores_total": len(stores),
          "available": sum(1 for s in stores if s.status == "ok"),
          "evidence_levels": {h: HARNESS_EVIDENCE[h] for h in resolve_harnesses(args.agent)}})
    return EXIT_OK


def cmd_list(args: argparse.Namespace) -> int:
    harnesses = resolve_harnesses(args.agent)
    stores = selected_stores(harnesses, getattr(args, "store", None))
    since, until = parse_time_bound(args.since), parse_time_bound(args.until)
    scanned = emitted = 0
    stats: dict[str, int] = {}
    truncated = False
    for store in stores:
        for ref in iter_all_sessions(store, args.cwd, since, until, cwd_exact=args.cwd_exact):
            if args.title and args.title.lower() not in (ref.title or "").lower():
                continue
            scanned += 1
            if args.limit and emitted >= args.limit:
                truncated = True
                break
            emit(ref.as_dict())
            emitted += 1
        if truncated:
            break
    emit(coverage_summary(stores, scanned, emitted, stats, truncated=truncated))
    return EXIT_OK if all(s.status != "unavailable" for s in stores) and not stats.get("unreadable") and not stats.get("unsupported_fragments") else EXIT_PARTIAL


def cmd_grep(args: argparse.Namespace) -> int:
    harnesses = resolve_harnesses(args.agent)
    stores = selected_stores(harnesses, getattr(args, "store", None))
    since, until = parse_time_bound(args.since), parse_time_bound(args.until)
    if args.regex:
        pattern = re.compile(args.pattern, re.IGNORECASE if args.ignore_case else 0)
        def match(t: str) -> tuple[int, int] | None:
            found = pattern.search(t)
            return found.span() if found else None
    else:
        needle = args.pattern if not args.ignore_case else args.pattern.lower()
        def match(t: str) -> tuple[int, int] | None:
            pos = (t if not args.ignore_case else t.lower()).find(needle)
            if pos < 0:
                return None
            if not args.ignore_case or not needle:
                return pos, pos + len(needle)
            # Some Unicode lowercase mappings expand to several characters.
            # Map the match offsets back to the original excerpt's characters.
            lower_offset = 0
            start = end = None
            for index, char in enumerate(t):
                next_offset = lower_offset + len(char.lower())
                if start is None and next_offset > pos:
                    start = index
                if next_offset >= pos + len(needle):
                    end = index + 1
                    break
                lower_offset = next_offset
            return (start, end) if start is not None and end is not None else None
    prefilter = RawPrefilter(args.pattern, ignore_case=args.ignore_case, regex=args.regex)
    verdicts: dict[tuple[str, str], bool] = {}

    def wanted(f: dict[str, Any]) -> bool:  # the same tests the loop below applies
        text = f.get("text")
        return (in_scope(f, args.scope) and in_window(f, since, until)
                and text is not None and match(text) is not None)
    scanned = emitted = 0
    stats: dict[str, int] = {}
    truncated = False
    for store in stores:
        if store.status != "ok":
            continue
        for ref in iter_all_sessions(store, args.cwd, None, None, cwd_exact=args.cwd_exact, light=True):
            scanned += 1
            if prefilter.active and not _prefilter_may_match(store, ref, prefilter, wanted, verdicts):
                prefilter.skipped += 1
                continue
            grouped = []
            # Match the entire decoded fragment; --max-chars limits output only.
            for f in iter_all_fragments(store, ref, 0, stats):
                if not in_scope(f, args.scope) or not in_window(f, since, until):
                    continue
                text = f.get("text")
                span = match(text) if text is not None else None
                if span is None:
                    continue
                if args.max_chars > 0 and len(text) > args.max_chars:
                    start = max(0, min(span[0] - max(0, (args.max_chars - (span[1] - span[0])) // 2),
                                       len(text) - args.max_chars))
                    f["text"] = text[start:start + args.max_chars]
                    f["text_offset"] = start
                    f["truncated"] = True
                if ref.light:
                    ref = LIGHT_READERS[store.harness][1](store, ref)
                f["session"] = {k: v for k, v in ref.as_dict().items() if k in ("session_id", "title", "cwd", "created", "parent", "agent")}
                if args.group_by_session:
                    grouped.append(f)
                else:
                    emit(f)
                emitted += 1
                if args.limit and emitted >= args.limit:
                    truncated = True
                    break
            if grouped:
                emit({"_session": ref.as_dict(), "matches": grouped})
            if truncated:
                break
        if truncated:
            break
    summary = coverage_summary(stores, scanned, emitted, stats, truncated=truncated)
    summary["prefilter"] = prefilter.report()
    emit(summary)
    return EXIT_OK if all(s.status != "unavailable" for s in stores) and not stats.get("unreadable") and not stats.get("unsupported_fragments") else EXIT_PARTIAL


def fast_resolve(store: Store, query_id: str) -> SessionRef | None:
    """Direct locator construction — avoids a full store scan when the id maps
    onto a known file/row name pattern. Returns None to fall back to scan."""
    p = store.path
    if store.harness == "claude-code":
        f = p / f"{query_id}.jsonl"
        if f.exists():
            return SessionRef("claude-code", store.name, query_id, locator={"file": str(f)})
    elif store.harness == "codex":
        # Revision choice and state lineage require the full store context.
        return next((ref for ref in codex_iter_sessions(store) if ref.session_id == query_id and
                     (not ref.extra.get("selected_rollout") or not ref.locator.get("file") or
                      ref.locator["file"] == ref.extra["selected_rollout"] or store.name == "history")), None)
    elif store.harness == "grok":
        hits = [d for d in p.glob(f"*/{query_id}") if d.is_dir()]
        if len(hits) == 1:
            summary = read_json(hits[0] / "summary.json") or {}
            info = summary.get("info") or {}
            return SessionRef("grok", store.name, query_id, locator={"dir": str(hits[0])},
                              cwd=info.get("cwd"), title=summary.get("generated_title"))
    elif store.harness == "opencode":
        return next((ref for ref in opencode_iter_sessions(store) if ref.session_id == query_id), None)
    elif store.harness == "agy":
        if ((p / "brain" / query_id).is_dir()
                or (p / "conversations" / f"{query_id}.db").is_file()
                or query_id in _agy_metadata(store)):
            return next((ref for ref in agy_iter_sessions(store) if ref.session_id == query_id), None)
    elif store.harness == "devin":
        if store.name == "sessions.db":
            try:
                conn = open_sqlite_ro(p)
                row = conn.execute(
                    "SELECT id, working_directory, model, agent_mode, created_at, last_activity_at, title, hidden, main_chain_id FROM sessions WHERE id=?",
                    (query_id,),
                ).fetchone()
                conn.close()
            except sqlite3.Error:
                row = None
            if row:
                return SessionRef("devin", "sessions.db", row[0],
                                  locator={"table": "sessions", "key": row[0], "db": str(p)},
                                  title=row[6], cwd=row[1], created=row[4], updated=row[5],
                                  models=[row[2]] if row[2] else [], hidden=bool(row[7]),
                                  extra={"agent_mode": row[3], "main_chain_id": row[8]})
        else:
            f = p / f"{query_id}.json"
            if f.exists():
                return SessionRef("devin", "transcripts", query_id, locator={"file": str(f)})
    elif store.harness == "gemini" and store.name == "chats":
        hits = list(_walk(p, f"chats/*{query_id[:8]}*.json", store.walk_stats))
        if len(hits) == 1:
            return SessionRef("gemini", "chats", query_id, locator={"file": str(hits[0])})
    elif store.harness == "claude-desktop":
        sid = query_id if query_id.startswith("local_") else f"local_{query_id}"
        metas = list(_walk(p, f"{sid}.json", store.walk_stats))
        if len(metas) == 1:
            loc = {"file": str(metas[0])}
            d = metas[0].with_suffix("")
            if d.is_dir():
                loc["dir"] = str(d)
            return SessionRef("claude-desktop", store.name, sid, locator=loc)
    elif store.harness == "cursor" and store.kind == "sqlite":
        try:
            conn = open_sqlite_ro(p)
            row = conn.execute("SELECT value FROM cursorDiskKV WHERE key=?", (f"composerData:{query_id}",)).fetchone()
            conn.close()
        except sqlite3.Error:
            row = None
        if row:
            return SessionRef("cursor", store.name, query_id,
                              locator={"db": str(p), "table": "cursorDiskKV", "key": f"composerData:{query_id}"})
    elif store.harness == "qwen-code":
        hits = list(_walk(p, f"chats/{query_id}.jsonl", store.walk_stats))
        if len(hits) == 1:
            return SessionRef("qwen-code", store.name, query_id, locator={"file": str(hits[0])})
    elif store.harness == "kimi-code":
        hits = [d for d in p.glob(f"*/{query_id}") if d.is_dir()]
        if len(hits) == 1:
            return SessionRef("kimi-code", store.name, query_id, locator={"dir": str(hits[0])})
    elif store.harness == "omp":
        hits = list(_walk(p, f"{query_id}.jsonl", store.walk_stats))
        if len(hits) == 1:
            return SessionRef("omp", store.name, query_id, locator={"file": str(hits[0])})
    elif store.harness == "porch":
        d = p / query_id
        if d.is_dir():
            return SessionRef("porch", "runs", query_id, locator={"dir": str(d)})
    return None


def _hydrate_ref(store: Store, ref: SessionRef) -> SessionRef:
    """Fill title/cwd/models/etc. on a fast-resolved ref by scanning only the
    narrowest enclosing scope (the file's own directory, not the whole store).
    For sqlite stores there is no narrower scope — match on session_id."""
    if (store.harness == "codex" and ref.extra.get("native_id")) or (ref.title and ref.cwd):
        return ref
    f = ref.locator.get("file")
    scope = store if not f else Store(store.harness, store.name, Path(f).parent, store.kind)
    try:
        if f and store.harness in LIGHT_READERS:
            # The file's own metadata, without parsing every session before it in its directory.
            candidates: Iterable[SessionRef] = [LIGHT_READERS[store.harness][1](scope, ref)]
        else:
            candidates = READERS[store.harness][0](scope)
        for r in candidates:
            if (r.locator.get("file") != f) if f else (r.session_id != ref.session_id):
                continue
            for key in ("title", "cwd", "created", "updated", "parent", "agent", "hidden"):
                val = getattr(r, key)
                if val is not None:
                    setattr(ref, key, val)
            if r.models:
                ref.models = r.models
            ref.extra.update(r.extra)
            break
    except (OSError, sqlite3.Error):
        pass
    return ref


def _resolve_session(harnesses: list[str], query: str, store_filter: str | None = None) -> tuple[Store, SessionRef]:
    """Address forms: 'harness:session-id', bare session-id (must be unique),
    or --file path handled by caller."""
    harness_hint, _, sid = query.partition(":")
    if sid and harness_hint in READERS:
        harnesses = [harness_hint]
        query_id = sid
    else:
        query_id = query
    fast: list[tuple[Store, SessionRef]] = []
    for store in selected_stores(harnesses, store_filter):
        if store.status != "ok":
            continue
        ref = fast_resolve(store, query_id)
        if ref is not None:
            fast.append((store, _hydrate_ref(store, ref)))
    unique_fast = {(r.harness, r.session_id) for _s, r in fast}
    if len(unique_fast) == 1:
        return fast[0]
    candidates: list[tuple[Store, SessionRef]] = []
    for store in selected_stores(harnesses, store_filter):
        if store.status != "ok":
            continue
        for ref in iter_all_sessions(store, None, None, None):
            if ref.session_id == query_id or ref.session_id.endswith(query_id) or Path(ref.session_id).name == query_id:
                candidates.append((store, ref))
    if not candidates:
        # substring fallback
        for store in selected_stores(harnesses, store_filter):
            if store.status != "ok":
                continue
            for ref in iter_all_sessions(store, None, None, None):
                if query_id in ref.session_id:
                    candidates.append((store, ref))
    if not candidates:
        raise SystemExit(f"session not found: {query!r}")
    unique = {(c[1].harness, c[1].session_id) for c in candidates}
    if len(unique) > 1:
        lines = "; ".join(f"{r.harness}:{r.session_id} (store {s.name})" for s, r in candidates[:8])
        raise SystemExit(f"ambiguous session id {query!r} → {lines}. Qualify as harness:session-id and/or pass --store.")
    # Same session visible through several layers (e.g. codex rollout + state
    # index + catalog): prefer the canonical content layer over index rows.
    def _rank(item: tuple[Store, SessionRef]) -> int:
        s, r = item
        if r.locator.get("file") and not r.locator.get("dir"):
            return 0
        if r.extra.get("rollout_path"):
            return 1
        return 2
    return min(candidates, key=_rank)


def cmd_show(args: argparse.Namespace) -> int:
    if getattr(args, 'at', None):
        import sessions_index
        return sessions_index.cmd_show_at(args)
    harnesses = resolve_harnesses(args.agent)
    stats: dict[str, int] = {}
    since, until = parse_time_bound(args.since), parse_time_bound(args.until)
    if args.file:
        path = Path(args.file).expanduser()
        store, ref = _store_for_file(path)
    else:
        store, ref = _resolve_session(harnesses, args.session, getattr(args, "store", None))
    emit({"_session": ref.as_dict()})
    emitted = 0
    target = args.around  # seq of the fragment to center the window on
    context = args.context
    window: list[dict[str, Any]] = []
    after_remaining: int | None = None
    around_seen = around_hit = False
    truncated = False
    for f in iter_all_fragments(store, ref, args.max_chars, stats):
        if target is not None and f.get("seq") == target:
            around_seen = True
        if not in_scope(f, args.scope) or not in_window(f, since, until):
            continue
        if target is None:
            emit(f)
            emitted += 1
            if args.limit and emitted >= args.limit:
                truncated = True
                break
            continue
        if after_remaining is not None:
            emit(f)
            emitted += 1
            after_remaining -= 1
            if after_remaining <= 0:
                break
            continue
        if f.get("seq") == target:
            around_hit = True
            for prev in window:
                emit(prev)
                emitted += 1
            emit(f)
            emitted += 1
            after_remaining = context
        else:
            window.append(f)
            if len(window) > context:
                window.pop(0)
    summary = coverage_summary([store], 1, emitted, stats)
    if truncated:
        summary["truncated"] = True
        summary["note"] = (summary.get("note") or "") + " | output truncated by --limit; pass --limit 0 for the full session"
    if target is not None and not around_hit:
        summary["around_hint"] = (
            f"seq {target} {'exists but was filtered out by --scope/--since/--until — retry with --scope all' if around_seen else 'does not exist in this session'}"
        )
    emit(summary)
    return EXIT_PARTIAL if stats.get("unreadable") or stats.get("unsupported_fragments") or store.status == "unavailable" else EXIT_OK


def _store_for_file(path: Path) -> tuple[Store, SessionRef]:
    """Sniff a single file's harness by structure, then build a one-file store."""
    if not path.exists():
        raise SystemExit(f"no such file: {path}")
    if path.suffix in (".db", ".sqlite", ".sqlite3", ".vscdb"):
        # choose by table shape
        try:
            conn = open_sqlite_ro(path)
            try:
                if table_exists(conn, "message_nodes"):
                    store = Store("devin", "sessions.db", path, "sqlite")
                    sid = _pick_single_session(conn, "sessions", "id")
                    return store, SessionRef("devin", "sessions.db", sid, locator={"table": "sessions", "key": sid, "db": str(path)})
                if table_exists(conn, "cursorDiskKV"):
                    store = Store("cursor", "custom", path, "sqlite")
                    sid = _pick_single_session(conn, "cursorDiskKV", "key", like="composerData:%")
                    return store, SessionRef("cursor", "custom", sid.split(":", 1)[-1], locator={"db": str(path), "table": "cursorDiskKV", "key": sid})
                if table_exists(conn, "session_v2"):
                    store = Store("opencode", "db", path, "sqlite")
                    sid = _pick_single_session(conn, "session_v2", "id")
                    return store, SessionRef("opencode", "db", sid, locator={"table": "session_v2", "key": sid, "db": str(path)})
            finally:
                conn.close()
        except sqlite3.Error as e:
            raise SystemExit(f"cannot open sqlite read-only: {e}")
        raise SystemExit(f"unrecognized sqlite store: {path}")
    # JSON / JSONL: peek first records
    first = "" if path.name.endswith(".jsonl.zst") else path.open("r", encoding="utf-8", errors="replace").read(65536)
    try:
        obj = json.loads(first)
        if isinstance(obj, dict) and "messages" in obj and ("sessionId" in obj or "projectHash" in obj):
            store = Store("gemini", "chats", path.parent.parent.parent, "json_dir")
            return store, SessionRef("gemini", "chats", str(obj.get("sessionId") or path.stem), locator={"file": str(path)})
        if isinstance(obj, dict) and "steps" in obj and "session_id" in obj:
            store = Store("devin", "transcripts", path.parent, "json_dir")
            return store, SessionRef("devin", "transcripts", str(obj.get("session_id") or path.stem), locator={"file": str(path)})
    except json.JSONDecodeError:
        pass
    stats: dict[str, int] = {}
    records = list(islice(iter_jsonl(path, stats), 5))
    types = {r.get("type") for _ln, r in records}
    if "session_meta" in types or "turn_context" in types or "response_item" in types:
        store = Store("codex", "custom", path.parent, "jsonl_dir")
        sid = next((r.get("payload", {}).get("id") for _ln, r in records if r.get("type") == "session_meta"), path.stem)
        return store, SessionRef("codex", "custom", str(sid), locator={"file": str(path)})
    if types & {"turn.prompt", "turn.steer", "context.append_loop_event"}:
        store = Store("kimi-code", "custom", path.parent.parent, "session_dirs")
        return store, SessionRef("kimi-code", "custom", path.parent.parent.name, locator={"dir": str(path.parent.parent)})
    if {"uuid", "parentUuid"} <= set().union(*[set(r.keys()) for _ln, r in records], set()):
        harness = "qwen-code" if any("parts" in (r.get("message") or {}) for _ln, r in records) else "claude-code"
        store = Store(harness, "custom", path.parent, "jsonl_dir")
        sid = next((r.get("sessionId") for _ln, r in records if r.get("sessionId")), path.stem)
        return store, SessionRef(harness, "custom", str(sid), locator={"file": str(path)})
    if any(r.get("chat_history") or r.get("type") in ("backend_tool_call",) for _ln, r in records):
        store = Store("grok", "custom", path.parent.parent, "session_dirs")
        return store, SessionRef("grok", "custom", path.parent.name, locator={"dir": str(path.parent)})
    raise SystemExit(f"could not identify harness of {path} — pass --agent and session id instead")


def _pick_single_session(conn: sqlite3.Connection, table: str, col: str, like: str | None = None) -> str:
    if like:
        rows = conn.execute(f"SELECT {col} FROM {table} WHERE {col} LIKE ? LIMIT 2", (like,)).fetchall()
    else:
        rows = conn.execute(f"SELECT {col} FROM {table} LIMIT 2").fetchall()
    if len(rows) != 1:
        raise SystemExit(f"--file on a multi-session {table} store needs an explicit session id, not implemented: use list first")
    return str(rows[0][0])


# ---------------------------------------------------------------------------
# Process safeguards
# ---------------------------------------------------------------------------

def _nonnegative_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    if not re.fullmatch(r"[0-9]+", raw):
        raise ValueError(f"{name} must be a non-negative integer (for example 0 or 1536); got {raw!r}")
    return int(raw)


class _RUsageV2(ctypes.Structure):
    _fields_ = [("uuid", ctypes.c_ubyte * 16), ("values", ctypes.c_uint64 * 18)]


_MAC_RUSAGE_FUNC = None
_MAC_RUSAGE_UNAVAILABLE = False


def _memory_usage_bytes() -> int | None:
    """Return kernel-accounted memory: macOS physical footprint or Linux RSS+swap."""
    if sys.platform == "darwin":
        global _MAC_RUSAGE_FUNC, _MAC_RUSAGE_UNAVAILABLE
        if _MAC_RUSAGE_UNAVAILABLE:
            return None
        try:
            if _MAC_RUSAGE_FUNC is None:
                libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
                _MAC_RUSAGE_FUNC = libproc.proc_pid_rusage
                _MAC_RUSAGE_FUNC.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.POINTER(_RUsageV2)]
                _MAC_RUSAGE_FUNC.restype = ctypes.c_int
            usage = _RUsageV2()
            if _MAC_RUSAGE_FUNC(os.getpid(), 2, ctypes.byref(usage)) != 0:
                return None
            # rusage_info_v2: uuid, then user/system time, wakeups, pageins,
            # wired size, resident size, physical footprint, ...
            return int(usage.values[7])
        except (AttributeError, OSError):
            _MAC_RUSAGE_UNAVAILABLE = True
            return None
    if sys.platform.startswith("linux"):
        try:
            values: dict[str, int] = {}
            with open("/proc/self/status", encoding="ascii") as status:
                for line in status:
                    if line.startswith(("VmRSS:", "VmSwap:")):
                        key, raw, *_ = line.split()
                        values[key] = int(raw) * 1024
            if "VmRSS:" not in values or "VmSwap:" not in values:
                return None
            return values["VmRSS:"] + values["VmSwap:"]
        except (OSError, ValueError):
            return None
    return None


def _memory_limit_monitor(command: str, limit_mb: int) -> tuple[threading.Event, threading.Thread] | None:
    if limit_mb == 0:
        return None
    initial = _memory_usage_bytes()
    if initial is None:
        return None
    limit = limit_mb * 1024 * 1024
    if initial > limit:
        _memory_limit_exit(command, initial, limit_mb)
    stop = threading.Event()

    def watch() -> None:
        while not stop.wait(MEMORY_SAMPLE_S):
            current = _memory_usage_bytes()
            if current is not None and current > limit:
                _memory_limit_exit(command, current, limit_mb)

    thread = threading.Thread(target=watch, name="porch-sessions-memory", daemon=True)
    thread.start()
    return stop, thread


def _memory_limit_exit(command: str, current: int, limit_mb: int) -> None:
    reached = current / (1024 * 1024)
    guidance = (f"Completed sessions remain indexed; retry or raise {MEMORY_ENV} for this call."
                if command == "index build" else
                "Narrow the query with --file, --limit, or --since, use one session instead of a group, "
                f"or raise {MEMORY_ENV} for this call; output above is incomplete.")
    print(
        f"porch sessions {command}: memory budget exceeded ({reached:.0f} MiB footprint; limit {limit_mb} MiB). "
        + guidance,
        file=sys.stderr,
        flush=True,
    )
    os._exit(EXIT_MEMORY_LIMIT)


def _lock_api():
    if os.name == "nt":
        import msvcrt

        def acquire(handle, blocking: bool) -> bool:
            handle.seek(0)
            try:
                mode = msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK
                msvcrt.locking(handle.fileno(), mode, 1)
                return True
            except OSError:
                return False

        def release(handle) -> None:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return acquire, release
    try:
        import fcntl
    except ImportError:
        return None

    def acquire(handle, blocking: bool) -> bool:
        flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
        try:
            fcntl.flock(handle.fileno(), flags)
            return True
        except BlockingIOError:
            return False

    def release(handle) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    return acquire, release


def _queue_capability() -> bool:
    return _lock_api() is not None


def _ticket_number(directory: Path, lock_api):
    acquire, release = lock_api
    guard = (directory / "sessions-queue-counter.lock").open("a+b")
    acquire(guard, True)
    try:
        counter = directory / "sessions-queue-counter"
        try:
            current = int(counter.read_text(encoding="ascii"))
        except (FileNotFoundError, ValueError):
            current = 0
        number = current + 1
        counter.write_text(str(number), encoding="ascii")
        waiter_path = directory / f"sessions-waiter-{number}.lock"
        waiter = waiter_path.open("a+b")
        acquire(waiter, True)
        return number, waiter_path, waiter
    finally:
        release(guard)
        guard.close()


def _live_waiter_numbers(directory: Path, lock_api) -> list[int]:
    acquire, release = lock_api
    # Ticket creation also holds this lock until its waiter file is locked, so
    # probes cannot mistake a newly-created live waiter for a dead one.
    guard = (directory / "sessions-queue-counter.lock").open("a+b")
    acquire(guard, True)
    try:
        numbers = []
        for path in directory.glob("sessions-waiter-*.lock"):
            try:
                number = int(path.stem.rsplit("-", 1)[1])
                handle = path.open("a+b")
            except (ValueError, OSError):
                continue
            if acquire(handle, False):
                release(handle)
                handle.close()
                try:
                    path.unlink()
                except OSError:
                    pass
            else:
                handle.close()
                numbers.append(number)
        return sorted(numbers)
    finally:
        release(guard)
        guard.close()


def _acquire_scan_slot(max_parallel: int, timeout_s: int):
    """FIFO admission: only the oldest live waiter may claim the next free slot."""
    if max_parallel == 0:
        return None
    lock_api = _lock_api()
    if lock_api is None:
        return None
    directory = _steer_root()
    directory.mkdir(parents=True, exist_ok=True)
    number, waiter_path, waiter = _ticket_number(directory, lock_api)
    acquire, release = lock_api
    start = time.monotonic()
    next_notice = start + 1
    warned = False
    slot = None
    try:
        while True:
            live = _live_waiter_numbers(directory, lock_api)
            if live and live[0] == number:
                for index in range(max_parallel):
                    candidate = (directory / f"sessions-slot-{index}.lock").open("a+b")
                    if acquire(candidate, False):
                        slot = candidate
                        break
                    candidate.close()
                if slot is not None:
                    return slot
            now = time.monotonic()
            if not warned and now - start >= 1 or warned and now >= next_notice:
                print(f"waiting for a free slot: {max_parallel} session scans are already running", file=sys.stderr, flush=True)
                warned = True
                next_notice = now + 60
            if now - start >= timeout_s:
                print(
                    f"porch sessions: timed out waiting for a scan slot after {timeout_s}s; "
                    f"retry later or set {QUEUE_TIMEOUT_ENV} higher (or {PARALLEL_ENV}=0 for this call).",
                    file=sys.stderr,
                    flush=True,
                )
                raise SystemExit(EXIT_QUEUE_TIMEOUT)
            time.sleep(0.05)
    finally:
        release(waiter)
        waiter.close()
        try:
            waiter_path.unlink()
        except OSError:
            pass


_ACTIVE_SLOT = None


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("-a", "--agent", help="comma-separated harness slugs (default: all)")
    p.add_argument("--store", help="only stores whose name or path contains this substring")
    p.add_argument("--cwd", help="only sessions whose recorded cwd contains this substring")
    p.add_argument("--since", help="ISO-8601 or epoch; naive timestamps = local time")
    p.add_argument("--until", help="ISO-8601 or epoch")
    p.add_argument("--scope", choices=sorted(SCOPES), default="convo",
                   help="convo=prompt+assistant (default) | prompts | assistant | tools | reasoning | system | all")
    p.add_argument("--limit", type=int, default=200, help="max fragments/sessions emitted (0=unbounded)")
    p.add_argument("--max-chars", type=int, default=400, help="per-fragment text cap (0=full)")


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="porch sessions",
        description="Search and navigate local coding-agent session histories (read-only).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("roots", help="show detected history stores and availability")
    p.add_argument("-a", "--agent")
    p.add_argument("--store", help="only stores whose name or path contains this substring")

    p = sub.add_parser("list", help="list sessions (id, title, cwd, dates, models)")
    _add_common(p)
    p.add_argument("--cwd-exact", action="store_true", help="match --cwd exactly")
    p.add_argument("--title", help="case-insensitive substring of the session title")

    p = sub.add_parser("find", help="search the session index by literal fragments",
                       description="Find literal fragments: porch sessions find 'задач' 'найти|нашел|поиск'. "
                                   "Groups are ANDed across a session; | separates alternatives, \\| is a literal bar.")
    p.add_argument('groups', nargs='+', metavar='GROUP')
    p.add_argument('-a', '--agent', help='comma-separated harness slugs')
    p.add_argument('--kind', choices=('title','prompt','answer'))
    p.add_argument('--origin', choices=('human','delegated','unknown'))
    p.add_argument('--class', dest='session_class', choices=('interactive','headless','subagent','unknown'))
    p.add_argument('--cwd')
    p.add_argument('--since')
    p.add_argument('--until')
    p.add_argument('--limit', type=int, default=20)
    p.add_argument('--deadline', type=float, default=20, metavar='SECONDS')

    p = sub.add_parser('trace', help='trace indexed session lineage',
                       description='Trace indexed lineage and verdicts; sync first and report coverage. Use find to get a REF.')
    p.add_argument('refs', nargs='*', metavar='REF', help='session references, aliases or run ids')
    p.add_argument('--left-behind', action='store_true', help='without REF, find unfinished, uncollected or orphaned work')
    p.add_argument('--since', help='left-behind lower bound: ISO time, epoch or duration (default 7d)')
    p.add_argument('--cwd', help='left-behind working-directory substring filter')
    p.add_argument('-a', '--agent', help='left-behind comma-separated harness filter')
    p.add_argument('--depth', type=int, default=None, help='maximum descendant depth from roots; seeds and ancestors remain (default unlimited)')
    p.add_argument('--max-nodes', type=int, default=200, help='maximum graph nodes; summary reports truncation (default 200)')
    p.add_argument('--evidence', action='store_true', help='include source pointers and expanded facts for inspecting verdicts')
    p.add_argument('--deadline', type=float, default=20, metavar='SECONDS', help='sync deadline in seconds (default 20); busy lock uses snapshot')

    p = sub.add_parser('index', help='inspect, build or drop the disposable find index',
                       description='Maintain the private index used by sessions find. Status does not sync; '
                                   'build refreshes it; drop removes it without touching session stores.')
    index_sub = p.add_subparsers(dest='index_command', required=True)
    index_sub.add_parser('status', help='show the committed index and last sync coverage')
    build = index_sub.add_parser('build', help='sync all indexed stores without a deadline')
    build.add_argument('--full', action='store_true', help='re-read all sessions, ignoring fingerprints')
    index_sub.add_parser('drop', help='remove the index and keep its lock file')

    p = sub.add_parser("grep", help="literal/regex search over decoded fragment text")
    _add_common(p)
    p.add_argument("--cwd-exact", action="store_true", help="match --cwd exactly")
    p.add_argument("pattern")
    p.add_argument("-i", "--ignore-case", action="store_true")
    p.add_argument("--regex", action="store_true", help="treat pattern as a regex")
    p.add_argument("--group-by-session", action="store_true", help="emit matches grouped by session")

    p = sub.add_parser("show", help="dump one session's fragments with locators")
    _add_common(p)
    p.add_argument("session", nargs="?", help="harness:session-id or unique id substring")
    p.add_argument("--file", help="read a single session file/db directly (harness auto-detected)")
    p.add_argument("--at", help="show the source record addressed by an indexed locator")
    p.add_argument("--around", type=int, metavar="SEQ", help="emit a window around the fragment with this seq")
    p.add_argument("--context", type=int, default=10, help="fragments on each side of --around (default 10)")

    p = sub.add_parser("turns", help="one row per user-triggered turn with per-turn metrics")
    _add_common(p)

    p = sub.add_parser("flags", help="deterministic anti-pattern detections with evidence locators")
    _add_common(p)
    p.add_argument("--kind", help="only this pattern (retry_loop|search_loop|edit_without_read|"
                                 "correction|correction_burst|abandoned|permission_friction|"
                                 "context_pressure|interrupted|error_burst|failed_run)")

    p = sub.add_parser("stats", help="grouped aggregates + coverage (--by model|harness|cwd|day)")
    _add_common(p)
    p.add_argument("--by", choices=("model", "harness", "cwd", "day"), default="model")

    args = parser.parse_args()
    try:
        memory_limit_mb = _nonnegative_env(MEMORY_ENV, 1536)
        max_parallel = _nonnegative_env(PARALLEL_ENV, 2)
        queue_timeout_s = _nonnegative_env(QUEUE_TIMEOUT_ENV, 600)
    except ValueError as exc:
        parser.error(str(exc))
    if getattr(args, "cwd_exact", False) and not args.cwd:
        parser.error("--cwd-exact requires --cwd")
    args.max_chars = getattr(args, "max_chars", 400) or 0
    if getattr(args, "limit", 200) == 0:
        args.limit = 0
    handlers = {"roots": cmd_roots, "list": cmd_list, "grep": cmd_grep, "show": cmd_show}
    if args.command in ('find', 'index', 'trace'):
        import sessions_index
        handlers['find'] = sessions_index.cmd_find
        handlers['index'] = sessions_index.cmd_index
        if args.command == 'trace':
            import sessions_trace
            handlers['trace'] = sessions_trace.cmd_trace
    if args.command in ("turns", "flags", "stats"):
        import analyze  # noqa: E402 — local lib, lazy to keep this file standalone
        handlers.update({"turns": analyze.cmd_turns, "flags": analyze.cmd_flags,
                         "stats": analyze.cmd_stats})
    monitor_command = 'index build' if args.command == 'index' and args.index_command == 'build' else args.command
    monitor = _memory_limit_monitor(monitor_command, memory_limit_mb)
    try:
        if args.command == "roots":
            unavailable = []
            if memory_limit_mb and _memory_usage_bytes() is None:
                unavailable.append("memory budget")
            if max_parallel and not _queue_capability():
                unavailable.append("scan queue")
            if unavailable:
                print(f"porch sessions safeguards unavailable: {', '.join(unavailable)} on this platform", file=sys.stderr)
        global _ACTIVE_SLOT
        if args.command not in ("roots", "find", "trace") and not (args.command == 'index' and args.index_command != 'build'):
            _ACTIVE_SLOT = _acquire_scan_slot(max_parallel, queue_timeout_s)
        result = handlers[args.command](args)
        if monitor:
            monitor[0].set()
            monitor[1].join()
            current = _memory_usage_bytes()
            if current is not None and memory_limit_mb and current > memory_limit_mb * 1024 * 1024:
                _memory_limit_exit(monitor_command, current, memory_limit_mb)
        return result
    except BrokenPipeError:
        # Prevent Python's shutdown flush from retrying the closed pipe.
        try:
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, sys.stdout.fileno())
            os.close(devnull)
        except OSError:
            pass
        return EXIT_OK
    except SystemExit:
        raise
    except Exception as e:  # never leak a traceback as the only diagnostic
        print(json.dumps({"_summary": True, "error": f"{type(e).__name__}: {e}"}, ensure_ascii=False), file=sys.stderr)
        return EXIT_FAILED
    finally:
        if monitor:
            monitor[0].set()


if __name__ == "__main__":
    sys.exit(main())
