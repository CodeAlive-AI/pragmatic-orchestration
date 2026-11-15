"""Delivery records: proof that a run's final answer reached a caller.

Each time porch hands a terminal run's final text to a caller — `delegate
wait` printing it, or a foreground `delegate` supervisor printing it — one
JSON line is appended to ``<run_dir>/deliveries.jsonl``. Observers such as
``porch sessions trace`` read that file to distinguish a collected answer
from one that was left unread. Recording is best-effort: callers catch
OSError and warn, never changing the delivered result.
"""
from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Any, Optional

from .launcher import launcher_metadata
from .util import append_jsonl, utc_now_iso

# Sentinel: derive the collector from this process's environment.
_FROM_ENV = object()


def _stdout_kind() -> str:
    """Classify fd 1 for the record: tty | pipe | file | other."""
    try:
        if os.isatty(1):
            return "tty"
        mode = os.fstat(1).st_mode
    except OSError:
        return "other"
    if stat.S_ISFIFO(mode):
        return "pipe"
    if stat.S_ISREG(mode):
        return "file"
    return "other"


def _collector_instance() -> Optional[str]:
    """Opaque caller identity; a bad PORCH_LAUNCHER degrades to null."""
    try:
        return launcher_metadata(os.environ).get("launcher_instance")
    except ValueError:
        return None


def record_delivery(run_dir: Path, via: str, fmt: str, collector: Any = _FROM_ENV) -> None:
    """Append one delivery record to ``run_dir/deliveries.jsonl``.

    ``via`` is ``wait`` or ``foreground``; ``fmt`` is ``text`` or ``json``.
    ``collector`` defaults to the ``launcher_instance`` derived from this
    process's environment — right for `wait`, whose env is the collector's.
    A supervisor must pass its launch-time ``meta["launcher_instance"]``
    instead: it rewrites PORCH_LAUNCHER* for nested launches before printing,
    so its own environment no longer names the caller. Pass None explicitly
    for an unknown collector. Raises OSError when the record cannot be
    written. Never stores a raw caller session id.
    """
    if collector is _FROM_ENV:
        collector = _collector_instance()
    append_jsonl(
        Path(run_dir) / "deliveries.jsonl",
        {
            "ts": utc_now_iso(),
            "via": via,
            "format": fmt,
            "stdout": _stdout_kind(),
            "collector": collector,
        },
    )
