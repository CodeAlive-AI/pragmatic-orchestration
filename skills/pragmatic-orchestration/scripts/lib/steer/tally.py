"""Counts of streamed events, written in place of one record per event.

A backend streams its answer, its thinking and its protocol chatter in small
pieces: thousands of events per run. The registry's audit.jsonl and a detached
run's supervisor.log used to get a record for each. No reader used them (the
normalized stream holds the events themselves), and together they outgrew the
runs' answers many times over.

A StreamTally counts those events between the records that are still written,
so the audit keeps where content flowed and how much of it, and a rare
diagnostic label (`stream_reconnected attempt=2`) is not lost among the chatter.
"""
from __future__ import annotations

from typing import Any, Dict

# Adapter kinds that arrive once per streamed piece. Every other kind (turns,
# tools, steer acknowledgements, errors, done) is still recorded one by one.
STREAM_KINDS = frozenset({"text", "thought", "progress"})
# `progress` carries a label: a protocol method or a short diagnostic. Labels
# are counted separately so an unusual one stays visible.
LABELLED_KINDS = frozenset({"progress"})
LABEL_MAX_CHARS = 60
# Some backends put free text into the label; past this many distinct labels
# in one window the rest are counted together.
LABELS_MAX = 32
OTHER_LABEL = "(other)"


class StreamTally:
    """Counts stream events since the last take()."""

    def __init__(self) -> None:
        self._plain: Dict[str, int] = {}
        self._labelled: Dict[str, Dict[str, int]] = {}

    def add(self, kind: str, data: Any = "") -> None:
        if kind not in LABELLED_KINDS:
            self._plain[kind] = self._plain.get(kind, 0) + 1
            return
        label = " ".join(str(data or "").split())[:LABEL_MAX_CHARS] or "(none)"
        labels = self._labelled.setdefault(kind, {})
        if label not in labels and len(labels) >= LABELS_MAX:
            label = OTHER_LABEL
        labels[label] = labels.get(label, 0) + 1

    def __bool__(self) -> bool:
        return bool(self._plain or self._labelled)

    def totals(self) -> Dict[str, int]:
        """Events per kind, labels summed."""
        out = dict(self._plain)
        for kind, labels in self._labelled.items():
            out[kind] = sum(labels.values())
        return out

    def brief(self) -> str:
        """One log line's worth: `text=120 thought=40 progress=312`."""
        totals = self.totals()
        return " ".join(f"{kind}={totals[kind]}" for kind in sorted(totals))

    def take(self) -> Dict[str, Any]:
        """The counts as a JSON-ready dict; the tally starts again from zero."""
        out: Dict[str, Any] = dict(self._plain)
        for kind, labels in self._labelled.items():
            out[kind] = dict(labels)
        self._plain = {}
        self._labelled = {}
        return out
