#!/usr/bin/env python3
"""Retention for porch's saved run outputs and its steerable run registry.

`porch prune` lists what the policy would remove; `porch prune --apply` removes
it. Only finished runs are ever touched:

  outputs   <PORCH_OUTPUT_DIR>/run-*      whole folder after --older-than;
                                          raw/ and normalized/ streams after
                                          --detail-older-than or over the cap
  registry  <PORCH_STEER_DIR>/runs/<id>   whole entry after --older-than

A run counts as running, and is kept, while its supervisor or launching porch
process is alive. Python standard library only.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from steer.registry import (  # noqa: E402
    Registry,
    RegistryError,
    TERMINAL_STATUSES,
    current_uid,
    default_registry_root,
)
from steer.util import lock_exclusive, open_lock_fd, pid_alive  # noqa: E402

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 5

DEFAULT_RUN_AGE = "30d"
DEFAULT_DETAIL_AGE = "3d"
DEFAULT_DETAIL_MAX = "2G"

# The size cap never takes the streams of a run that finished less than a day
# ago: its caller may still be reading them.
CAP_HOLD_SECONDS = 24 * 3600
# An output folder that records no launching process (written before porch
# recorded one, or on Windows) is treated as running while it is this fresh.
UNKNOWN_LIVENESS_QUIET_SECONDS = 6 * 3600
# A terminal registry run whose supervisor is still alive may be finishing
# backend cleanup.
TERMINAL_SETTLE_SECONDS = 3600

DETAIL_DIRS = ("raw", "normalized")
MARKER = "retention.json"
LOCK_NAME = ".retention.lock"
RUN_DIR_RE = re.compile(r"run-[A-Za-z0-9._-]+\Z")
_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 7 * 86400}


class UsageError(ValueError):
    pass


def parse_age(text: str, source: str) -> float:
    match = re.fullmatch(r"\s*([0-9]+(?:\.[0-9]+)?)\s*([smhdw]?)\s*", str(text), re.I)
    if not match:
        raise UsageError(f"{source}: invalid age {text!r}; use for example 30d, 12h or 45m (a bare number is days)")
    return float(match.group(1)) * _UNITS[(match.group(2) or "d").lower()]


def parse_size(text: str, source: str) -> int:
    match = re.fullmatch(r"\s*([0-9]+(?:\.[0-9]+)?)\s*([KMGT]?)(?:i?B)?\s*", str(text), re.I)
    if not match:
        raise UsageError(f"{source}: invalid size {text!r}; use for example 4G or 500M (binary units; 0 disables the cap)")
    power = "KMGT".find(match.group(2).upper()) + 1 if match.group(2) else 0
    return int(float(match.group(1)) * (1024 ** power))


def human(size: float) -> str:
    size = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(size) < 1024 or unit == "TiB":
            return f"{int(size)} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TiB"


def human_age(seconds: float) -> str:
    for unit, span in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= span:
            value = seconds / span
            return f"{value:g}{unit}" if value == int(value) else f"{value:.1f}{unit}"
    return f"{seconds:g}s"


def day(ts: Optional[float]) -> str:
    return "unknown" if ts is None else time.strftime("%Y-%m-%d", time.localtime(ts))


def parse_iso(value: Any) -> Optional[float]:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def allocated(st: os.stat_result) -> int:
    blocks = getattr(st, "st_blocks", None)
    return blocks * 512 if blocks is not None else st.st_size


def measure(path: Path) -> Dict[str, Any]:
    """Allocated bytes, stream bytes and the newest file mtime under a run folder."""
    total = detail = 0
    newest: Optional[float] = None
    files = 0
    for current, dirs, names in os.walk(path, followlinks=False):
        rel = os.path.relpath(current, path)
        top = "" if rel == "." else rel.split(os.sep)[0]
        for name in names:
            try:
                st = os.lstat(os.path.join(current, name))
            except OSError:
                continue
            size = allocated(st)
            total += size
            files += 1
            if top in DETAIL_DIRS:
                detail += size
            if name != MARKER and (newest is None or st.st_mtime > newest):
                newest = st.st_mtime
    if newest is None:
        newest = os.lstat(path).st_mtime
    return {"bytes": total, "detail_bytes": detail, "newest": newest, "files": files}


def real(path: Any) -> str:
    try:
        return os.path.realpath(str(path))
    except OSError:
        return str(path)


# --------------------------------------------------------------------------
# Inventory
# --------------------------------------------------------------------------

def registry_state(meta: Dict[str, Any], finished: float, now: float):
    status = str(meta.get("effective_status") or "")
    if status == "stale":
        return "finished", "supervisor is gone"
    if status in TERMINAL_STATUSES:
        if meta.get("pid_alive") and now - finished < TERMINAL_SETTLE_SECONDS:
            return "running", "supervisor still alive after the run ended"
        return "finished", status
    return "running", f"status {status or 'unknown'}, supervisor alive"


def meta_stamp(meta: Dict[str, Any]) -> Optional[float]:
    return parse_iso(meta.get("finished_at")) or parse_iso(meta.get("updated_at"))


def registry_inventory(reg: Registry, now: float) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for meta in reg.list_runs():
        run_id = str(meta.get("run_id") or "")
        if meta.get("unreadable") or not run_id:
            rows.append({"store": "registry", "name": run_id, "state": "unreadable",
                         "reason": str(meta.get("unreadable") or "no run id")})
            continue
        path = reg.runs_dir / run_id
        size = measure(path)
        finished = max(value for value in (meta_stamp(meta), size["newest"]) if value is not None)
        state, reason = registry_state(meta, finished, now)
        handle = meta.get("session_handle")
        anchor = handle.get("anchor_run_id") if isinstance(handle, dict) else None
        rows.append({
            "store": "registry", "name": run_id, "path": str(path), "state": state, "reason": reason,
            "finished": finished, "bytes": size["bytes"], "detail_bytes": 0,
            "chain": anchor if isinstance(anchor, str) and anchor else run_id,
            "artifacts_dir": real(meta["artifacts_dir"]) if meta.get("artifacts_dir") else None,
        })
    return rows


def live_artifact_dirs(reg: Registry, now: float) -> set:
    """Output folders that a running registry run writes to (metadata only)."""
    live = set()
    for meta in reg.list_runs():
        if meta.get("unreadable") or not meta.get("artifacts_dir"):
            continue
        if registry_state(meta, meta_stamp(meta) or now, now)[0] == "running":
            live.add(real(meta["artifacts_dir"]))
    return live


def outputs_inventory(outputs_dir: Path, registry_rows: List[Dict[str, Any]], now: float) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    if not outputs_dir.is_dir():
        return rows
    live_dirs = {row["artifacts_dir"] for row in registry_rows
                 if row.get("state") == "running" and row.get("artifacts_dir")}
    uid = current_uid()
    for entry in sorted(os.scandir(outputs_dir), key=lambda item: item.name):
        if not RUN_DIR_RE.match(entry.name):
            continue
        if entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
            continue
        path = Path(entry.path)
        try:
            st = os.lstat(path)
        except OSError as exc:
            rows.append({"store": "outputs", "name": entry.name, "state": "unreadable", "reason": str(exc)})
            continue
        if os.name != "nt" and st.st_uid != uid:
            rows.append({"store": "outputs", "name": entry.name, "state": "unreadable",
                         "reason": f"owned by uid {st.st_uid}"})
            continue
        size = measure(path)
        state, reason = output_liveness(path, size["newest"], live_dirs, now)
        rows.append({"store": "outputs", "name": entry.name, "path": str(path), "state": state,
                     "reason": reason, "finished": size["newest"], "bytes": size["bytes"],
                     "detail_bytes": size["detail_bytes"]})
    return rows


def output_liveness(path: Path, newest: float, live_dirs: set, now: float):
    if real(path) in live_dirs:
        return "running", "a live registry run writes here"
    owner = None
    try:
        with open(path / "meta.json", encoding="utf-8") as stream:
            meta = json.load(stream)
        owner = meta.get("owner_pid") if isinstance(meta, dict) else None
    except (OSError, ValueError):
        pass
    if isinstance(owner, int) and not isinstance(owner, bool) and owner > 0:
        if pid_alive(owner):
            return "running", f"launching process {owner} is alive"
        return "finished", "launching process is gone"
    if now - newest < UNKNOWN_LIVENESS_QUIET_SECONDS:
        return "running", "no launching process recorded and written recently"
    return "finished", "no launching process recorded, quiet"


# --------------------------------------------------------------------------
# Plan
# --------------------------------------------------------------------------

def build_plan(rows: List[Dict[str, Any]], *, now: float, run_age: float, detail_age: float,
               detail_max: int) -> List[Dict[str, Any]]:
    """Decide an action per row: remove, slim or keep. Mutates and returns rows."""
    chains: Dict[str, List[Dict[str, Any]]] = {}
    for row in rows:
        row["action"] = "keep"
        if row["state"] != "finished":
            row["why"] = row.get("reason", row["state"])
            continue
        row["age"] = max(0.0, now - row["finished"])
        if row["store"] == "registry":
            chains.setdefault(row["chain"], []).append(row)
    # A session chain shares one anchor entry (session.json, session.lock), so
    # it goes as a whole or not at all.
    chain_sizes: Dict[str, int] = {}
    for row in rows:
        if row["store"] == "registry":
            chain_sizes[row.get("chain", row["name"])] = chain_sizes.get(row.get("chain", row["name"]), 0) + 1
    for row in rows:
        if row["state"] != "finished":
            continue
        if row["store"] == "registry":
            members = chains.get(row["chain"], [])
            whole = len(members) == chain_sizes.get(row["chain"], 0)
            if whole and all(member["age"] >= run_age for member in members):
                row["action"], row["why"] = "remove", f"finished {human_age(row['age'])} ago"
            elif row["age"] >= run_age:
                row["why"] = "a newer run continues the same session"
            else:
                row["why"] = "within retention"
        elif row["age"] >= run_age:
            row["action"], row["why"] = "remove", f"finished {human_age(row['age'])} ago"
        elif row["detail_bytes"] and row["age"] >= detail_age:
            row["action"], row["why"] = "slim", f"streams older than {human_age(detail_age)}"
        else:
            row["why"] = "within retention"
    if detail_max > 0:
        kept = [row for row in rows if row["store"] == "outputs" and row["state"] == "finished"
                and row["action"] == "keep" and row["detail_bytes"]]
        over = sum(row["detail_bytes"] for row in kept) - detail_max
        for row in sorted(kept, key=lambda item: item["finished"]):
            if over <= 0:
                break
            if row["age"] < CAP_HOLD_SECONDS:
                continue
            row["action"], row["why"] = "slim", f"streams exceed the {human(detail_max)} cap"
            over -= row["detail_bytes"]
    for row in rows:
        row["frees"] = (row["bytes"] if row["action"] == "remove"
                        else row["detail_bytes"] if row["action"] == "slim" else 0)
    return rows


# --------------------------------------------------------------------------
# Apply
# --------------------------------------------------------------------------

class LiveDirs:
    """The live registry runs' output folders, re-read at most every few seconds."""

    def __init__(self, reg: Registry, max_age: float = 5.0):
        self.reg, self.max_age, self.read_at, self.dirs = reg, max_age, 0.0, set()

    def get(self) -> set:
        if time.monotonic() - self.read_at > self.max_age or not self.read_at:
            self.dirs = live_artifact_dirs(self.reg, time.time())
            self.read_at = time.monotonic()
        return self.dirs


def still_prunable(row: Dict[str, Any], reg: Registry, outputs_dir: Path, live: LiveDirs) -> Optional[str]:
    """Re-check one row right before it is touched. Returns a reason to skip."""
    path = Path(row["path"])
    now = time.time()
    if path.is_symlink() or not path.is_dir():
        return "no longer a folder"
    size = measure(path)
    if size["newest"] > row["finished"]:
        return "written since it was listed"
    if row["store"] == "registry":
        try:
            meta = dict(reg.load_meta(row["name"]))
        except RegistryError as exc:
            return f"unreadable: {exc}"
        meta["effective_status"] = reg.effective_status(meta)
        meta["pid_alive"] = pid_alive(int(meta.get("pid") or 0))
        state, reason = registry_state(meta, row["finished"], now)
        return None if state == "finished" else reason
    if path.parent != outputs_dir or not RUN_DIR_RE.match(path.name):
        return "outside the outputs folder"
    state, reason = output_liveness(path, size["newest"], live.get(), now)
    return None if state == "finished" else reason


def slim(path: Path, finished: float, detail_bytes: int) -> None:
    for name in DETAIL_DIRS:
        target = path / name
        if target.is_symlink():
            target.unlink()
        elif target.is_dir():
            shutil.rmtree(target)
    marker = path / MARKER
    with open(marker, "w", encoding="utf-8") as stream:
        json.dump({"streams_removed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                   "streams_removed_bytes": detail_bytes,
                   "note": "porch prune removed raw/ and normalized/; the final answers remain"},
                  stream, indent=2)
        stream.write("\n")
    # The marker must not make the run look younger than it is.
    os.utime(marker, (finished, finished))


def apply_plan(rows: List[Dict[str, Any]], reg: Registry, outputs_dir: Path) -> List[str]:
    errors: List[str] = []
    live = LiveDirs(reg)
    for row in rows:
        if row["action"] == "keep":
            continue
        skip = still_prunable(row, reg, outputs_dir, live)
        if skip:
            row["action"], row["why"], row["frees"] = "keep", skip, 0
            continue
        try:
            if row["action"] == "remove":
                shutil.rmtree(row["path"])
            else:
                slim(Path(row["path"]), row["finished"], row["detail_bytes"])
            row["done"] = True
        except OSError as exc:
            errors.append(f"{row['store']} {row['name']}: {exc}")
            row["error"] = str(exc)
    return errors


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def summarize(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    out: Dict[str, Any] = {"frees_bytes": 0}
    for store in ("outputs", "registry"):
        mine = [row for row in rows if row["store"] == store]
        info = {"runs": len(mine), "bytes": sum(row.get("bytes", 0) for row in mine)}
        for action in ("remove", "slim"):
            picked = [row for row in mine if row.get("action") == action and not row.get("error")]
            info[action] = {"runs": len(picked), "bytes": sum(row["frees"] for row in picked)}
            out["frees_bytes"] += info[action]["bytes"]
        info["kept_running"] = sum(1 for row in mine if row["state"] == "running")
        info["kept_finished"] = sum(1 for row in mine if row["state"] == "finished" and row.get("action") == "keep")
        info["unreadable"] = sum(1 for row in mine if row["state"] == "unreadable")
        out[store] = info
    return out


def render(rows, summary, args, policy, applied: bool, errors: List[str]) -> str:
    verb = "Removed" if applied else "Would remove"
    slim_verb = "removed the streams of" if applied else "would remove the streams of"
    lines = [
        "porch prune{}".format("" if applied else " (dry run, nothing was removed)"),
        f"  outputs:  {args.outputs_dir}",
        f"  registry: {policy['registry_dir']}",
        "  policy:   finished runs go after {}; raw/ and normalized/ streams after {}{}".format(
            human_age(policy["run_age"]), human_age(policy["detail_age"]),
            f", or sooner while they exceed {human(policy['detail_max'])}" if policy["detail_max"] > 0 else ""),
        "",
    ]
    out, reg = summary["outputs"], summary["registry"]
    lines.append("outputs:  {} {} runs ({}), {} {} runs ({}); kept {} running, {} finished{}".format(
        verb.lower(), out["remove"]["runs"], human(out["remove"]["bytes"]), slim_verb, out["slim"]["runs"],
        human(out["slim"]["bytes"]), out["kept_running"], out["kept_finished"],
        f", skipped {out['unreadable']} unreadable" if out["unreadable"] else ""))
    lines.append("registry: {} {} runs ({}); kept {} running, {} finished{}".format(
        verb.lower(), reg["remove"]["runs"], human(reg["remove"]["bytes"]), reg["kept_running"],
        reg["kept_finished"], f", skipped {reg['unreadable']} unreadable" if reg["unreadable"] else ""))
    lines.append("{}: {}".format("Freed" if applied else "Would free", human(summary["frees_bytes"])))
    picked = sorted((row for row in rows if row.get("action") in ("remove", "slim")),
                    key=lambda row: row["frees"], reverse=True)
    shown = picked if args.all else picked[:args.top]
    if shown:
        lines.append("")
        for row in shown:
            lines.append("  {:<6} {:<8} {:>10}  finished {}  {}{}".format(
                row["action"], row["store"], human(row["frees"]), day(row.get("finished")), row["name"],
                "  ERROR: " + row["error"] if row.get("error") else ""))
        if len(shown) < len(picked):
            lines.append(f"  ... and {len(picked) - len(shown)} more (--all lists every run, --json gives the full plan)")
    for error in errors:
        lines.append(f"error: {error}")
    if not applied and picked:
        lines += ["", "Run `porch prune --apply` to prune."]
    return "\n".join(lines)


def env_default(name: str, fallback: str) -> str:
    value = os.environ.get(name, "")
    return value if value.strip() else fallback


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="porch prune",
        description="List (default) or remove finished porch runs that are past retention. "
                    "A running run is never touched.")
    parser.add_argument("--apply", action="store_true", help="prune; without it the command only lists")
    parser.add_argument("--older-than", metavar="AGE",
                        help=f"remove finished runs older than this (PORCH_RETENTION_DAYS; default {DEFAULT_RUN_AGE})")
    parser.add_argument("--detail-older-than", metavar="AGE",
                        help="remove raw/ and normalized/ streams of finished runs older than this "
                             f"(PORCH_RETENTION_DETAIL_DAYS; default {DEFAULT_DETAIL_AGE})")
    parser.add_argument("--max-detail-size", metavar="SIZE",
                        help="cap on the streams of finished runs; the oldest beyond their first day go first, "
                             f"0 disables (PORCH_RETENTION_DETAIL_MAX; default {DEFAULT_DETAIL_MAX})")
    parser.add_argument("--outputs-dir", default=os.environ.get("PORCH_OUTPUT_DIR") or None,
                        help="outputs folder (default: PORCH_OUTPUT_DIR)")
    parser.add_argument("--steer-dir", default=None, help="registry root (default: PORCH_STEER_DIR or the platform default)")
    parser.add_argument("--top", type=int, default=40, help="rows to list, largest first (default 40)")
    parser.add_argument("--all", action="store_true", help="list every run to prune")
    parser.add_argument("--json", action="store_true", help="print the full plan as JSON")
    parser.add_argument("--quiet", action="store_true", help="print one summary line (for the automatic run)")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "prune":
        argv = argv[1:]
    args = build_parser().parse_args(argv)
    try:
        if not args.outputs_dir:
            raise UsageError("no outputs folder: pass --outputs-dir or run through `porch prune`")
        run_age = parse_age(args.older_than, "--older-than") if args.older_than is not None else \
            parse_age(env_default("PORCH_RETENTION_DAYS", DEFAULT_RUN_AGE), "PORCH_RETENTION_DAYS")
        detail_age = parse_age(args.detail_older_than, "--detail-older-than") if args.detail_older_than is not None else \
            parse_age(env_default("PORCH_RETENTION_DETAIL_DAYS", DEFAULT_DETAIL_AGE), "PORCH_RETENTION_DETAIL_DAYS")
        detail_max = parse_size(args.max_detail_size, "--max-detail-size") if args.max_detail_size is not None else \
            parse_size(env_default("PORCH_RETENTION_DETAIL_MAX", DEFAULT_DETAIL_MAX), "PORCH_RETENTION_DETAIL_MAX")
    except UsageError as exc:
        print(f"porch prune: {exc}", file=sys.stderr)
        return EXIT_USAGE
    outputs_dir = Path(args.outputs_dir).expanduser()
    args.outputs_dir = str(outputs_dir)
    reg = Registry(Path(args.steer_dir).expanduser() if args.steer_dir else default_registry_root())
    policy = {"run_age": run_age, "detail_age": min(detail_age, run_age), "detail_max": detail_max,
              "registry_dir": str(reg.runs_dir)}

    lock_fd = None
    if args.apply:
        # One pruner at a time per outputs folder; a second one has nothing to add.
        outputs_dir.mkdir(parents=True, exist_ok=True)
        lock_fd = open_lock_fd(outputs_dir / LOCK_NAME)
        try:
            lock_exclusive(lock_fd, blocking=False)
        except (BlockingIOError, OSError):
            os.close(lock_fd)
            print("porch prune: another prune is running for this outputs folder; nothing done")
            return EXIT_OK
    try:
        now = time.time()
        registry_rows = registry_inventory(reg, now)
        rows = registry_rows + outputs_inventory(outputs_dir, registry_rows, now)
        build_plan(rows, now=now, run_age=policy["run_age"], detail_age=policy["detail_age"],
                   detail_max=policy["detail_max"])
        errors = apply_plan(rows, reg, outputs_dir) if args.apply else []
    finally:
        if lock_fd is not None:
            os.close(lock_fd)
    summary = summarize(rows)
    if args.json:
        print(json.dumps({"applied": bool(args.apply), "outputs_dir": str(outputs_dir), "policy": policy,
                          "summary": summary, "errors": errors, "runs": rows}, indent=2, default=str))
    elif args.quiet:
        print("{} porch prune{}: outputs -{} runs, streams of {} runs; registry -{} runs; {} {}{}".format(
            time.strftime("%Y-%m-%dT%H:%M:%S"), "" if args.apply else " (dry run)",
            summary["outputs"]["remove"]["runs"], summary["outputs"]["slim"]["runs"],
            summary["registry"]["remove"]["runs"], "freed" if args.apply else "would free",
            human(summary["frees_bytes"]), "; errors: " + "; ".join(errors) if errors else ""))
    else:
        print(render(rows, summary, args, policy, bool(args.apply), errors))
    return EXIT_FAILED if errors else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
