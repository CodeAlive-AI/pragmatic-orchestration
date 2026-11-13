#!/usr/bin/env python3
"""porch desktop — sessions of the Claude Desktop app (Code tab) and the Codex app.

Find, search and read sessions from the apps' own stores (read-only, through
sessions.py). Start, open and archive them in the running app through the macOS
accessibility tree with `agent-desktop` (https://github.com/vercel-labs/agent-desktop):
headless actions move no pointer and do not activate the app. Codex unarchive, and
Codex archive while the app is closed, go through the official `codex app-server`.

Standard library only. Output: JSONL for list/search/read, one JSON object for
actions; errors are a JSON object on stderr with the next command to try.
"""

from __future__ import annotations

import argparse
import collections
import ctypes
import ctypes.util
import json
import os
import re
import select
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sessions as S  # noqa: E402

EXIT_OK, EXIT_PARTIAL, EXIT_FAILED, EXIT_CONFIG, EXIT_USAGE, EXIT_NEEDS_USER = 0, 2, 3, 4, 5, 6

APPS: dict[str, dict[str, Any]] = {
    "claude": {
        "harness": "claude-desktop",
        "store": "code-sessions",
        "ax_app": "Claude",
        "bundle": "com.anthropic.claudefordesktop",
        "new_prefix": "New session in ",
        "composer": ("textfield", "Prompt"),
        "url": "claude://claude.ai/epitaxy/{id}",
    },
    "codex": {
        # The Codex app ships inside ChatGPT.app (bundle id com.openai.codex).
        "harness": "codex",
        "store": "state:",
        "ax_app": "ChatGPT",
        "bundle": "com.openai.codex",
        "new_prefix": "Start new chat in ",
        "composer": ("textfield", "Do anything"),
        "url": "codex://threads/{id}",
        "skip_windows": ("Computer Use",),
    },
}


class Fail(Exception):
    def __init__(self, message: str, code: int = EXIT_FAILED, kind: str = "failed", next_step: str | None = None):
        super().__init__(message)
        self.code, self.kind, self.next_step = code, kind, next_step


def out(obj: dict[str, Any]) -> None:
    print(json.dumps(obj, ensure_ascii=False, default=str), flush=True)


# ---------------------------------------------------------------------------
# Session catalogue (read-only)
# ---------------------------------------------------------------------------

def _norm(text: str | None) -> str:
    return re.sub(r"[^0-9a-zа-яё]+", "", (text or "").casefold())


def _codex_state_store() -> S.Store | None:
    stores = [s for s in S.selected_stores(["codex"]) if s.name.startswith("state:") and s.status == "ok"]
    if not stores:
        return None
    # state_5.sqlite supersedes state_4.sqlite; take the highest schema number.
    return max(stores, key=lambda s: int(re.sub(r"\D", "", s.name) or 0))


def _codex_app_fields(store: S.Store) -> dict[str, dict[str, Any]]:
    """The app-visible thread name and pin state, which sessions.py does not surface."""
    try:
        conn = S.open_sqlite_ro(store.path)
    except sqlite3.Error:
        return {}
    try:
        have = S.table_columns(conn, "threads")
        cols = [c for c in ("name", "is_pinned") if c in have]
        if not cols:
            return {}
        return {row[0]: dict(zip(cols, row[1:])) for row in conn.execute(f"SELECT id, {', '.join(cols)} FROM threads")}
    except sqlite3.Error:
        return {}
    finally:
        conn.close()


def _record(app: str, store: S.Store, ref: S.SessionRef, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    extra = extra or {}
    rec: dict[str, Any] = {"app": app, "id": ref.session_id, "title": extra.get("name") or ref.title}
    if app == "claude":
        data = S.read_json(Path(ref.locator.get("file", ""))) or {}
        root = data.get("originCwd") or ref.cwd
        rec.update(project=Path(root).name if root else None, cwd=ref.cwd, archived=bool(data.get("isArchived")),
                   turns=data.get("completedTurns"), model=data.get("model"),
                   permission_mode=data.get("permissionMode"))
        if root and root != ref.cwd:
            rec["project_root"] = root
    else:
        rec.update(project=Path(ref.cwd).name if ref.cwd else None, cwd=ref.cwd,
                   archived=bool(ref.extra.get("archived")), source=ref.extra.get("source"),
                   model=(ref.models or [None])[0], git_branch=ref.extra.get("git_branch"),
                   pinned=bool(extra.get("is_pinned")) or None)
        if extra.get("name") and ref.title and ref.title != extra.get("name"):
            rec["first_message"] = ref.title[:160]
    rec["created"] = S._iso(ref.created) if ref.created else None
    rec["updated"] = S._iso(ref.updated) if ref.updated else None
    rec["url"] = APPS[app]["url"].format(id=ref.session_id)
    rec["read"] = f"porch desktop read {app}:{ref.session_id}"
    return {k: v for k, v in rec.items() if v is not None}


def catalogue(app: str, *, include_subagents: bool = False, source: str = "vscode") -> list[tuple[S.Store, S.SessionRef, dict[str, Any]]]:
    rows = []
    if app == "claude":
        stores = [s for s in S.selected_stores(["claude-desktop"], "code-sessions") if s.status == "ok"]
    else:
        st = _codex_state_store()
        stores = [st] if st else []
    for store in stores:
        fields = _codex_app_fields(store) if app == "codex" else {}
        for ref in S.iter_all_sessions(store, None, None, None):
            if app == "codex":
                if ref.parent and not include_subagents:
                    continue
                if source != "any" and ref.extra.get("source") != source:
                    continue
            rows.append((store, ref, _record(app, store, ref, fields.get(ref.session_id))))
    rows.sort(key=lambda r: S.ts_epoch(r[1].updated) or 0, reverse=True)
    return rows


def _apps(raw: str) -> list[str]:
    return list(APPS) if raw == "all" else [raw]


def _filtered(args: argparse.Namespace) -> list[tuple[S.Store, S.SessionRef, dict[str, Any]]]:
    since, until = S.parse_time_bound(args.since), S.parse_time_bound(args.until)
    rows = []
    for app in _apps(args.app):
        for store, ref, rec in catalogue(app, include_subagents=args.include_subagents, source=args.source):
            if args.state == "active" and rec["archived"] or args.state == "archived" and not rec["archived"]:
                continue
            if args.project:
                p = _norm(args.project)
                if p not in _norm(rec.get("project")) and p not in _norm(rec.get("project_root")) and p not in _norm(rec.get("cwd")):
                    continue
            if args.title and args.title.casefold() not in ((rec.get("title") or "") + "\n" + (rec.get("first_message") or "")).casefold():
                continue
            upd = S.ts_epoch(ref.updated)
            if since and (upd is None or upd < since) or until and upd is not None and upd > until:
                continue
            rows.append((store, ref, rec))
    rows.sort(key=lambda r: S.ts_epoch(r[1].updated) or 0, reverse=True)
    return rows


def lookup(app: str | None, sid: str) -> tuple[str, S.Store, S.SessionRef, dict[str, Any]]:
    """Find one session by id (prefix allowed) or exact title across the chosen apps."""
    apps = [app] if app else list(APPS)
    hits = []
    for a in apps:
        for store, ref, rec in catalogue(a, include_subagents=True, source="any"):
            if ref.session_id == sid or ref.session_id.startswith(sid) or (rec.get("title") or "") == sid:
                hits.append((a, store, ref, rec))
    exact = [h for h in hits if h[2].session_id == sid]
    hits = exact or hits
    if not hits:
        raise Fail(f"No Desktop session matches {sid!r}.", EXIT_FAILED, "not_found",
                   "porch desktop list --app all --state any --title '<part of the title>'")
    if len(hits) > 1:
        names = "; ".join(f"{a}:{r.session_id} {rec.get('title')!r}" for a, _, r, rec in hits[:6])
        raise Fail(f"{sid!r} matches several sessions: {names}", EXIT_USAGE, "ambiguous",
                   "Pass the full id as <app>:<id>.")
    return hits[0]


def parse_target(raw: str) -> tuple[str | None, str]:
    app, sep, sid = raw.partition(":")
    if sep and app in APPS:
        return app, sid
    if raw.startswith("local_"):
        return "claude", raw
    return None, raw


# ---------------------------------------------------------------------------
# list / search / read
# ---------------------------------------------------------------------------

def cmd_list(args: argparse.Namespace) -> int:
    rows = _filtered(args)
    for _s, _r, rec in rows[: args.limit or None]:
        out(rec)
    out({"_summary": True, "sessions_matched": len(rows), "emitted": min(len(rows), args.limit or len(rows))})
    return EXIT_OK


def _matcher(args: argparse.Namespace):
    if args.regex:
        rx = re.compile(args.pattern, re.IGNORECASE if args.ignore_case else 0)
        return lambda t: (m.span() if (m := rx.search(t)) else None)
    needle = args.pattern.casefold() if args.ignore_case else args.pattern

    def match(t: str):
        hay = t.casefold() if args.ignore_case else t
        pos = hay.find(needle)
        # casefold can change lengths for a few scripts; the excerpt stays close enough.
        return (pos, pos + len(needle)) if pos >= 0 else None
    return match


def _excerpt(text: str, span: tuple[int, int], width: int) -> str:
    if width <= 0 or len(text) <= width:
        return text
    start = max(0, min(span[0] - (width - (span[1] - span[0])) // 2, len(text) - width))
    return ("…" if start else "") + text[start:start + width] + ("…" if start + width < len(text) else "")


def cmd_search(args: argparse.Namespace) -> int:
    match = _matcher(args)
    rows = _filtered(args)
    stats: dict[str, int] = {}
    scanned = found = 0
    for store, ref, rec in rows:
        scanned += 1
        hits: list[dict[str, Any]] = []
        more = False
        title = rec.get("title") or ""
        if (span := match(title)) is not None:
            hits.append({"where": "title", "text": title})
        for frag in S.iter_all_fragments(store, ref, 0, stats):
            if not S.in_scope(frag, args.scope):
                continue
            text = frag.get("text")
            span = match(text) if text else None
            if span is None:
                continue
            if len(hits) >= args.per_session:
                more = True
                break
            hits.append({k: v for k, v in {
                "seq": frag.get("seq"), "kind": frag.get("kind"), "authorship": frag.get("authorship"),
                "ts": frag.get("ts"), "text": _excerpt(text, span, args.max_chars)}.items() if v is not None})
        if hits:
            found += 1
            first_seq = next((h["seq"] for h in hits if "seq" in h), None)
            rec = dict(rec, hits=hits, more_hits=more)
            if first_seq is not None:
                rec["read"] = f"porch desktop read {rec['app']}:{rec['id']} --around {first_seq}"
            out(rec)
            if args.limit and found >= args.limit:
                break
    complete = not stats.get("unreadable") and not stats.get("malformed")
    out({"_summary": True, "sessions_scanned": scanned, "sessions_matched": found,
         "stopped_at_limit": bool(args.limit and found >= args.limit), "scan_complete": complete})
    return EXIT_OK if complete else EXIT_PARTIAL


def cmd_read(args: argparse.Namespace) -> int:
    app, sid = parse_target(args.session)
    app, store, ref, rec = lookup(app, sid)
    if app == "codex":
        # Read through the state index to the canonical rollout file.
        store, ref = S._resolve_session(["codex"], ref.session_id, store.name)
    out({"_session": rec})
    stats: dict[str, int] = {}
    frags = (f for f in S.iter_all_fragments(store, ref, args.max_chars, stats) if S.in_scope(f, args.scope))
    keep = ("seq", "kind", "authorship", "ts", "model", "text", "truncated") + (("locator",) if args.locators else ())
    slim = lambda f: {k: f[k] for k in keep if f.get(k) is not None}  # noqa: E731
    emitted = 0
    if args.around is not None:
        window: collections.deque = collections.deque(maxlen=args.context)
        after = None
        for f in frags:
            if after is not None:
                out(slim(f)); emitted += 1; after -= 1
                if after <= 0:
                    break
            elif (f.get("seq") or -1) >= args.around:
                for w in window:
                    out(slim(w)); emitted += 1
                out(slim(f)); emitted += 1
                after = args.context
                if after <= 0:
                    break
            else:
                window.append(f)
    elif args.all:
        for f in frags:
            out(slim(f)); emitted += 1
    else:
        for f in collections.deque(frags, maxlen=args.tail):
            out(slim(f)); emitted += 1
    out({"_summary": True, "fragments_emitted": emitted,
         "scan_complete": not stats.get("unreadable") and not stats.get("malformed")})
    return EXIT_OK


# ---------------------------------------------------------------------------
# Accessibility layer (agent-desktop)
# ---------------------------------------------------------------------------

def frontmost_bundle() -> str:
    asn = subprocess.run(["lsappinfo", "front"], capture_output=True, text=True).stdout.strip()
    info = subprocess.run(["lsappinfo", "info", "-only", "bundleID", asn], capture_output=True, text=True).stdout
    return info.split("=", 1)[-1].strip().strip('"') if "=" in info else ""


def _ax_manual_accessibility(pid: int) -> str:
    """Ask a Chromium/Electron app for its full accessibility tree.

    Chromium can drop to a reduced mode in which elements keep their names but lose
    their press actions; agent-desktop re-enables it only when the tree is empty.
    Setting AXManualAccessibility is what assistive tools do; it changes nothing visible.
    """
    if sys.platform != "darwin":
        return "skipped"
    try:
        ax = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")
        cf = ctypes.cdll.LoadLibrary(ctypes.util.find_library("CoreFoundation"))
        cf.CFStringCreateWithCString.restype = ctypes.c_void_p
        cf.CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]
        ax.AXUIElementCreateApplication.restype = ctypes.c_void_p
        ax.AXUIElementCreateApplication.argtypes = [ctypes.c_int]
        ax.AXUIElementSetAttributeValue.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
        ax.AXUIElementSetAttributeValue.restype = ctypes.c_int
        app = ax.AXUIElementCreateApplication(pid)
        name = cf.CFStringCreateWithCString(None, b"AXManualAccessibility", 0x08000100)
        err = ax.AXUIElementSetAttributeValue(app, name, ctypes.c_void_p.in_dll(cf, "kCFBooleanTrue"))
        return "enabled" if err == 0 else f"unsupported({err})"
    except OSError as e:
        return f"unavailable({e})"


class UI:
    def __init__(self, app: str, window_id: str | None = None, allow_frontmost: bool = False):
        if sys.platform != "darwin":
            raise Fail("App automation is implemented for macOS only.", EXIT_CONFIG, "unsupported_platform")
        if not shutil.which("agent-desktop"):
            raise Fail("agent-desktop is not installed.", EXIT_CONFIG, "missing_tool", "npm install -g agent-desktop")
        self.app, self.cfg = app, APPS[app]
        self.front_before = frontmost_bundle()
        if self.front_before == self.cfg["bundle"] and not allow_frontmost:
            raise Fail(f"{self.cfg['ax_app']} is the frontmost app, so the user is probably working in it; "
                       "acting there would change what they see.", EXIT_NEEDS_USER, "app_in_use",
                       "Ask the user, then rerun with --allow-frontmost.")
        self.win, pid = self._main_window(window_id)
        self.ax_mode = _ax_manual_accessibility(pid)

    def ad(self, *args: str, stdin: str | None = None, retries: int = 0) -> dict[str, Any]:
        for attempt in range(retries + 1):
            proc = subprocess.run(["agent-desktop", *args], input=stdin, capture_output=True, text=True, timeout=90)
            try:
                env = json.loads(proc.stdout)
            except json.JSONDecodeError:
                raise Fail(f"agent-desktop {args[0]}: no JSON output: {proc.stderr.strip()[:300]}")
            if env.get("ok"):
                return env
            err = env.get("error", {})
            retry_ok = err.get("disposition", {}).get("retry") == "safe" or err.get("code") in ("APP_UNRESPONSIVE", "TIMEOUT")
            if attempt < retries and retry_ok:
                time.sleep(0.8)
                continue
            if err.get("code") == "PERM_DENIED":
                raise Fail(err.get("message", "permission denied"), EXIT_CONFIG, "permission",
                           "agent-desktop permissions --guide")
            raise Fail(f"agent-desktop {args[0]}: {err.get('code')}: {err.get('message')}", EXIT_FAILED, "ax_error")
        raise AssertionError("unreachable")

    def _main_window(self, window_id: str | None) -> tuple[str, int]:
        data = self.ad("list-windows", "--app", self.cfg["ax_app"], retries=2)["data"]
        wins = data if isinstance(data, list) else data.get("windows", [])
        wins = [w for w in wins if w.get("accessible") and w.get("visible")
                and not any(w.get("title", "").startswith(p) for p in self.cfg.get("skip_windows", ()))]
        if window_id:
            wins = [w for w in wins if w["id"] == window_id]
        if not wins:
            raise Fail(f"{self.cfg['ax_app']} has no visible main window (closed or minimized).", EXIT_FAILED,
                       "no_window", f"open -g -b {self.cfg['bundle']}  # then rerun")
        win = max(wins, key=lambda w: w["bounds"]["width"] * w["bounds"]["height"])
        return win["id"], int(win["pid"])

    def find(self, role: str, *, name: str | None = None, text: str | None = None,
             root: str | None = None, sidebar: bool = False) -> list[dict[str, Any]]:
        args = ["find", "--app", self.cfg["ax_app"], "--window-id", self.win, "--role", role]
        if name is not None:
            args += ["--name", name, "--exact"]
        if text is not None:
            args += ["--text", text]
        if root:
            args += ["--root", root]
        matches = self.ad(*args, retries=3)["data"]["matches"]
        if sidebar and self.app == "claude":
            matches = [m for m in matches if "complementary:Sidebar" in m.get("path", [])]
        return matches

    def click(self, ref: str) -> None:
        self.ad("click", ref)

    def value(self, ref: str) -> str:
        return (self.ad("get", ref, "--property", "value", retries=2)["data"].get("value") or "").strip()

    def window_title(self) -> str:
        data = self.ad("list-windows", "--app", self.cfg["ax_app"], retries=1)["data"]
        wins = data if isinstance(data, list) else data.get("windows", [])
        return next((w.get("title", "") for w in wins if w["id"] == self.win), "")

    def page_title(self) -> str:
        if self.app == "codex":
            return self.window_title()
        pages = self.find("webarea")
        return (pages[0].get("name") or "").removesuffix(" - Claude Code") if pages else ""

    def focus_kept(self) -> bool:
        return frontmost_bundle() == self.front_before

    # -- sidebar -----------------------------------------------------------

    def projects(self) -> list[tuple[str, str]]:
        prefix = self.cfg["new_prefix"]
        return [(m["name"][len(prefix):], m["ref_id"]) for m in self.find("button", text=prefix, sidebar=True)
                if (m.get("name") or "").startswith(prefix)]

    def session_rows(self, title: str) -> list[dict[str, Any]]:
        rows = []
        for m in self.find("button", text=title, sidebar=True):
            name = m.get("name") or ""
            # Claude appends ", rename session" (header) or " Started by …"/" Forked from …" (row).
            if name == title or (self.app == "claude" and re.match(re.escape(title) + r" (Started by|Forked from) ", name)):
                rows.append(m)
        return rows

    def expand_project(self, project: str | None) -> str | None:
        """Claude only: click the project's sidebar group header to expand it; return its ref.

        The header toggles, so the caller clicks it again when the row is still missing.
        Codex project headers do not expand through accessibility, so it is not tried there.
        """
        if not project or self.app != "claude":
            return None
        want = _norm(project)
        for m in self.find("button", sidebar=True):
            name = m.get("name") or ""
            if _norm(name.split(" · ")[0]) == want and not name.startswith(self.cfg["new_prefix"]):
                self.click(m["ref_id"])
                time.sleep(0.8)
                return m["ref_id"]
        return None


# ---------------------------------------------------------------------------
# projects / start / open / archive / unarchive
# ---------------------------------------------------------------------------

def _base(label: str) -> str:
    return _norm(label.split(" · ")[0])


def _pick_project(projects: list[tuple[str, str]], query: str) -> tuple[str, str]:
    q = query.strip()
    for pred in (lambda l: l.casefold() == q.casefold(), lambda l: _base(l) == _norm(q)):
        hits = [p for p in projects if pred(p[0])]
        if len(hits) == 1:
            return hits[0]
    hits = [p for p in projects if _norm(q) in _norm(p[0])]
    if len(hits) == 1:
        return hits[0]
    names = ", ".join(p[0] for p in projects) or "(none)"
    if not hits:
        raise Fail(f"Project {query!r} is not in the sidebar. Visible: {names}", EXIT_FAILED, "not_found",
                   "porch desktop start --path <absolute folder> …  (opens via deep link)")
    raise Fail(f"Project {query!r} is ambiguous: {', '.join(p[0] for p in hits)}", EXIT_USAGE, "ambiguous")


def cmd_projects(args: argparse.Namespace) -> int:
    for app in _apps(args.app):
        ui = UI(app, args.window_id, allow_frontmost=True)  # read-only
        out({"app": app, "projects": [label for label, _ in ui.projects()]})
    return EXIT_OK


def _verify_composer_project(ui: UI, label: str | None) -> str:
    if ui.app == "codex":
        names = [m.get("name", "") for m in ui.find("combobox", text="Change project:")]
        shown = names[0].removeprefix("Change project:").strip() if names else ""
        if not shown:
            raise Fail("Could not read the project in the composer; nothing was sent.")
        if label is not None and _base(shown) != _base(label):
            raise Fail(f"The composer shows project {shown!r}, not {label!r}; nothing was sent.")
        return shown
    folder = [m.get("name", "") for m in ui.find("combobox", text="Folder:")]
    env = [m.get("name", "") for m in ui.find("combobox", text="Environment:")]
    if folder:
        path = folder[0].removeprefix("Folder:").strip()
        if path.startswith("Choose folder") or not os.path.isdir(os.path.expanduser(path)):
            raise Fail(f"The project's folder is gone ({path}); its sidebar group is stale. Nothing was sent.",
                       EXIT_FAILED, "stale_project")
        return path
    if env:
        shown = env[0].removeprefix("Environment:").strip()
        if label is not None and _base(shown) != _base(label):
            raise Fail(f"The composer shows environment {shown!r}, not {label!r}; nothing was sent.")
        return shown
    raise Fail("Could not read the selected project in the composer; nothing was sent.")


def _composer(ui: UI) -> str:
    role, name = ui.cfg["composer"]
    hits = ui.find(role, name=name)
    if len(hits) != 1:
        raise Fail(f"Expected one composer {name!r}, found {len(hits)}.")
    return hits[0]["ref_id"]


def _send(ui: UI, prompt: str) -> None:
    ref = _composer(ui)
    # set-value, not type: Claude's editor ignores headless `type`; set-value reaches app state.
    ui.ad("set-value", ref, "--stdin", stdin=prompt)
    if ui.value(ref) != prompt.strip():
        raise Fail("The composer did not take the prompt; nothing was sent.")
    send = ui.find("button", name="Send")
    if len(send) != 1:
        raise Fail(f"Expected one Send button, found {len(send)}; the prompt is left in the composer.")
    ui.click(send[0]["ref_id"])
    for _ in range(20):
        time.sleep(0.5)
        if not ui.find("button", name="Send"):
            return
        try:
            if ui.value(_composer(ui)) != prompt.strip():
                return
        except Fail:
            return
    raise Fail("Clicked Send, but the prompt is still in the composer. Check the app.")


def _new_session_id(app: str, since: float, prompt: str | None) -> str | None:
    for _ in range(16):
        for _s, ref, rec in catalogue(app, source="any")[:15]:
            created = S.ts_epoch(ref.created) or 0
            if created >= since - 2:
                if app == "codex" and prompt and (ref.extra.get("first_user_message") or "").strip() != prompt.strip():
                    continue
                return ref.session_id
        time.sleep(0.5)
    return None


def _deeplink_new(app: str, path: str, prompt: str | None) -> str:
    q = {("path" if app == "codex" else "folder"): os.path.abspath(os.path.expanduser(path))}
    if prompt:
        q["prompt" if app == "codex" else "q"] = prompt
    base = "codex://threads/new?" if app == "codex" else "claude://code/new?"
    return base + urllib.parse.urlencode(q, quote_via=urllib.parse.quote)


def _restore_front(before: str, app_bundle: str) -> None:
    if before and before != app_bundle:
        subprocess.run(["osascript", "-e", f'tell application id "{before}" to activate'], capture_output=True)


def _read_prompt(args: argparse.Namespace) -> str | None:
    text = args.prompt
    if args.prompt_file:
        text = sys.stdin.read() if args.prompt_file == "-" else Path(args.prompt_file).read_text(encoding="utf-8")
    if text is not None:
        text = text.strip()
        if not text:
            raise Fail("Empty prompt.", EXIT_USAGE, "usage")
    return text


def cmd_start(args: argparse.Namespace) -> int:
    prompt = _read_prompt(args)
    if bool(args.project) == bool(args.path):
        raise Fail("Pass exactly one of --project or --path.", EXIT_USAGE, "usage")
    if prompt is None and not args.no_send:
        raise Fail("A prompt is required unless --no-send.", EXIT_USAGE, "usage")
    t0 = time.time()
    ui = UI(args.app, args.window_id, args.allow_frontmost)
    res: dict[str, Any] = {"app": args.app, "action": "start"}
    if args.path:
        if not os.path.isdir(os.path.expanduser(args.path)):
            raise Fail(f"No such folder: {args.path}", EXIT_USAGE, "usage")
        subprocess.run(["open", "-g", _deeplink_new(args.app, args.path, prompt)], check=True)
        time.sleep(2.0)
        # The app activates itself on a deep link; hand focus straight back.
        _restore_front(ui.front_before, ui.cfg["bundle"])
        res["via"] = "deeplink"
        if args.app == "claude":
            res.update(status="needs_user", note="Claude shows 'Trust this workspace?' for every folder opened by "
                       "link. The user confirms it; the prompt is prefilled and they press Send.")
            out(res)
            return EXIT_NEEDS_USER
        res["project"] = _verify_composer_project(ui, None)
    else:
        label, ref = _pick_project(ui.projects(), args.project)
        ui.click(ref)
        time.sleep(1.5)
        res["via"] = "sidebar"
        res["project"] = _verify_composer_project(ui, label)
    if args.no_send:
        if prompt:
            ui.ad("set-value", _composer(ui), "--stdin", stdin=prompt)
        res["status"] = "opened"
    else:
        _send(ui, prompt)
        res["status"] = "sent"
        sid = _new_session_id(args.app, t0, prompt)
        if sid:
            res.update(id=sid, read=f"porch desktop read {args.app}:{sid}")
    res["focus_kept"] = ui.focus_kept()
    out(res)
    return EXIT_OK


def _find_row(ui: UI, rec: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    title = rec.get("title") or ""
    rows = ui.session_rows(title)
    expanded = None
    if not rows:
        expanded = ui.expand_project(rec.get("project"))
        rows = ui.session_rows(title) if expanded else []
        if expanded and not rows:
            ui.click(expanded)  # put the group back the way the user had it
            expanded = None
    if len(rows) > 1:
        raise Fail(f"Several sidebar rows are titled {title!r}; refusing to guess.", EXIT_USAGE, "ambiguous",
                   f"porch desktop open {rec['app']}:{rec['id']} --focus   # deep link opens the exact session")
    return (rows[0] if rows else None), expanded


def cmd_open(args: argparse.Namespace) -> int:
    app, sid = parse_target(args.session)
    app, _store, _ref, rec = lookup(app, sid)
    res = {"app": app, "action": "open", "id": rec["id"], "title": rec.get("title")}
    if args.focus:
        subprocess.run(["open", rec["url"]], check=True)
        res.update(status="opened", via="deeplink", focus_kept=False)
        out(res)
        return EXIT_OK
    if rec.get("archived"):
        raise Fail("The session is archived, so it has no sidebar row.", EXIT_FAILED, "archived",
                   f"porch desktop open {app}:{rec['id']} --focus")
    ui = UI(app, args.window_id, args.allow_frontmost)
    row, _expanded = _find_row(ui, rec)
    if row is None:
        raise Fail(f"No sidebar row for {rec.get('title')!r} (not loaded or hidden behind 'Show more').",
                   EXIT_FAILED, "not_in_sidebar", f"porch desktop open {app}:{rec['id']} --focus")
    ui.click(row["ref_id"])
    time.sleep(1.2)
    shown = ui.page_title()
    res.update(via="sidebar", page_title=shown, focus_kept=ui.focus_kept(),
               status="opened" if _norm(shown) == _norm(rec.get("title")) else "unverified")
    out(res)
    return EXIT_OK if res["status"] == "opened" else EXIT_PARTIAL


def _wait_archived(app: str, sid: str, want: bool, seconds: float = 8.0) -> bool:
    end = time.time() + seconds
    while time.time() < end:
        _a, _s, _r, rec = lookup(app, sid)
        if rec.get("archived") == want:
            return True
        time.sleep(0.5)
    return False


class CodexAppServer:
    """Minimal JSON-RPC client for `codex app-server` (stdio)."""

    def __init__(self) -> None:
        exe = shutil.which("codex") or "/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex"
        if not os.path.exists(exe) and not shutil.which(exe):
            raise Fail("codex CLI not found.", EXIT_CONFIG, "missing_tool")
        self.p = subprocess.Popen([exe, "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL, text=True, bufsize=1)
        self.n = 0
        self.call("initialize", {"clientInfo": {"name": "porch-desktop", "version": "1"}})
        self._send({"method": "initialized"})

    def _send(self, obj: dict[str, Any]) -> None:
        assert self.p.stdin
        self.p.stdin.write(json.dumps(obj) + "\n")
        self.p.stdin.flush()

    def call(self, method: str, params: dict[str, Any], timeout: float = 30) -> dict[str, Any]:
        self.n += 1
        self._send({"id": self.n, "method": method, "params": params})
        end = time.time() + timeout
        assert self.p.stdout
        while time.time() < end:
            ready, _, _ = select.select([self.p.stdout], [], [], 0.5)
            if not ready:
                continue
            line = self.p.stdout.readline()
            if not line:
                break
            msg = json.loads(line)
            if msg.get("id") == self.n:
                if "error" in msg:
                    raise Fail(f"codex app-server {method}: {msg['error'].get('message')}", EXIT_FAILED, "app_server")
                return msg.get("result") or {}
        raise Fail(f"codex app-server {method}: no answer in {timeout:.0f}s", EXIT_FAILED, "timeout")

    def close(self) -> None:
        try:
            if self.p.stdin:
                self.p.stdin.close()
            self.p.terminate()
            self.p.wait(timeout=5)
        except Exception:
            self.p.kill()


def _app_running(app: str) -> bool:
    return bool(subprocess.run(["pgrep", "-f", f"/Applications/{APPS[app]['ax_app']}.app/Contents/MacOS/"],
                               capture_output=True).stdout.strip())


def cmd_archive(args: argparse.Namespace) -> int:
    app, sid = parse_target(args.session)
    app, _store, _ref, rec = lookup(app, sid)
    res: dict[str, Any] = {"app": app, "action": "archive", "id": rec["id"], "title": rec.get("title")}
    if rec.get("archived"):
        out(dict(res, status="already_archived"))
        return EXIT_OK
    if app == "codex" and not _app_running(app):
        res["via"] = "app-server"
        if args.dry_run:
            out(dict(res, status="dry_run"))
            return EXIT_OK
        srv = CodexAppServer()
        try:
            srv.call("thread/archive", {"threadId": rec["id"]})
        finally:
            srv.close()
    else:
        # Archiving a sidebar row leaves the user's view alone unless that session is
        # the one on screen, so only that case needs their say-so.
        ui = UI(app, args.window_id, allow_frontmost=True)
        if (ui.front_before == ui.cfg["bundle"] and not args.allow_frontmost
                and _norm(ui.page_title()) == _norm(rec.get("title"))):
            raise Fail("This session is open in the app the user is working in.", EXIT_NEEDS_USER, "app_in_use",
                       "Ask the user, then rerun with --allow-frontmost.")
        row, expanded = _find_row(ui, rec)
        if app == "codex":
            if row is None:
                raise Fail("The running Codex app shows no sidebar row for this thread (older threads sit in "
                           "collapsed project groups).", EXIT_FAILED, "not_in_sidebar",
                           f"porch desktop open codex:{rec['id']} --focus   # it then appears under Recents; rerun archive")
            target = ui.find("button", name="Archive chat", root=row["ref_id"])
        else:
            target = [m for m in ui.find("combobox", name=f"More options for {rec.get('title')}", sidebar=True)]
        if len(target) != 1:
            raise Fail(f"Expected one archive control for {rec.get('title')!r}, found {len(target)}.",
                       EXIT_FAILED, "control_not_found",
                       "In Claude Desktop itself use its archive_session tool; otherwise archive it in the app.")
        res["via"] = "sidebar"
        if args.dry_run:
            out(dict(res, status="dry_run", focus_kept=ui.focus_kept()))
            return EXIT_OK
        ui.click(target[0]["ref_id"])
        if app == "claude":
            time.sleep(0.6)
            item = ui.find("menuitem", name="Archive")
            if len(item) != 1:
                ui.ad("press", "escape", "--window-id", ui.win)
                raise Fail("The session menu has no single 'Archive' item.", EXIT_FAILED, "control_not_found")
            ui.click(item[0]["ref_id"])
        res["focus_kept"] = ui.focus_kept()
        if expanded:
            res["note"] = "expanded the project's sidebar group to reach the row"
    ok = _wait_archived(app, rec["id"], True)
    res["status"] = "archived" if ok else "unverified"
    out(res)
    return EXIT_OK if ok else EXIT_PARTIAL


def cmd_unarchive(args: argparse.Namespace) -> int:
    app, sid = parse_target(args.session)
    app, _store, _ref, rec = lookup(app, sid)
    res: dict[str, Any] = {"app": app, "action": "unarchive", "id": rec["id"], "title": rec.get("title")}
    if not rec.get("archived"):
        out(dict(res, status="not_archived"))
        return EXIT_OK
    if app == "claude":
        raise Fail("Claude Desktop has no external unarchive path.", EXIT_NEEDS_USER, "unsupported",
                   "Inside Claude Desktop use its unarchive_session tool; otherwise restore it from the app's archived list.")
    srv = CodexAppServer()
    try:
        srv.call("thread/unarchive", {"threadId": rec["id"]})
    finally:
        srv.close()
    ok = _wait_archived(app, rec["id"], False)
    res.update(status="unarchived" if ok else "unverified", via="app-server",
               note="A running Codex app may show the thread only after it refreshes its sidebar.")
    out(res)
    return EXIT_OK if ok else EXIT_PARTIAL


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _filters(p: argparse.ArgumentParser, default_limit: int) -> None:
    p.add_argument("--app", choices=("claude", "codex", "all"), default="all")
    p.add_argument("--project", help="project/folder name (fuzzy: case and punctuation ignored)")
    p.add_argument("--title", help="case-insensitive substring of the title")
    p.add_argument("--state", choices=("active", "archived", "any"), default="active")
    p.add_argument("--since", help="updated at or after (ISO-8601 or epoch)")
    p.add_argument("--until", help="updated at or before")
    p.add_argument("--source", default="vscode",
                   help="Codex thread source: vscode (the Codex app; default), cli, exec, or any")
    p.add_argument("--include-subagents", action="store_true", help="include Codex subagent threads")
    p.add_argument("--limit", type=int, default=default_limit, help=f"max sessions (default {default_limit}, 0=all)")


def _ui_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument("--window-id", help="window id from `agent-desktop list-windows`")
    p.add_argument("--allow-frontmost", action="store_true",
                   help="act even when the app is frontmost (the user may be working in it)")


def main() -> int:
    ap = argparse.ArgumentParser(prog="porch desktop", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("list", help="sessions newest first, with project, state and how to read/open them")
    _filters(p, 30)

    p = sub.add_parser("search", help="find sessions whose title or transcript matches PATTERN")
    _filters(p, 20)
    p.add_argument("pattern")
    p.add_argument("-i", "--ignore-case", action="store_true")
    p.add_argument("--regex", action="store_true")
    p.add_argument("--scope", default="convo", choices=sorted(S.SCOPES), help="fragment kinds searched (default convo)")
    p.add_argument("--per-session", type=int, default=3, help="hits shown per session (default 3)")
    p.add_argument("--max-chars", type=int, default=240, help="excerpt width (0=full)")

    p = sub.add_parser("read", help="read a session: last N fragments, a window around SEQ, or all")
    p.add_argument("session", help="claude:local_<id> | codex:<thread-id> | id prefix | exact title")
    p.add_argument("--tail", type=int, default=20)
    p.add_argument("--around", type=int, metavar="SEQ")
    p.add_argument("--context", type=int, default=6)
    p.add_argument("--all", action="store_true")
    p.add_argument("--scope", default="convo", choices=sorted(S.SCOPES))
    p.add_argument("--max-chars", type=int, default=4000, help="per-fragment cap (0=full)")
    p.add_argument("--locators", action="store_true", help="include raw-source locators")

    p = sub.add_parser("projects", help="projects the app's sidebar offers for new tasks")
    p.add_argument("--app", choices=("claude", "codex", "all"), default="all")
    p.add_argument("--window-id")

    p = sub.add_parser("start", help="start a new task for a project, in the background")
    p.add_argument("--app", choices=("claude", "codex"), required=True)
    p.add_argument("--project", help="project name as the sidebar shows it")
    p.add_argument("--path", help="folder not in the sidebar: deep link (activates the app briefly)")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--prompt")
    g.add_argument("--prompt-file", help="file with the prompt, '-' for stdin")
    p.add_argument("--no-send", action="store_true", help="fill the composer, let the user send")
    _ui_flags(p)

    p = sub.add_parser("open", help="show a session in its app (sidebar click; --focus uses the deep link)")
    p.add_argument("session")
    p.add_argument("--focus", action="store_true", help="open by deep link and bring the app forward")
    _ui_flags(p)

    p = sub.add_parser("archive", help="archive a session and verify it in the app's store")
    p.add_argument("session")
    p.add_argument("--dry-run", action="store_true", help="resolve the session and its control, click nothing")
    _ui_flags(p)

    p = sub.add_parser("unarchive", help="restore an archived Codex thread (codex app-server)")
    p.add_argument("session")

    args = ap.parse_args()
    handlers = {"list": cmd_list, "search": cmd_search, "read": cmd_read, "projects": cmd_projects,
                "start": cmd_start, "open": cmd_open, "archive": cmd_archive, "unarchive": cmd_unarchive}
    try:
        return handlers[args.cmd](args)
    except Fail as e:
        err = {"ok": False, "error": e.kind, "message": str(e)}
        if e.next_step:
            err["next"] = e.next_step
        print(json.dumps(err, ensure_ascii=False), file=sys.stderr)
        return e.code
    except SystemExit:
        raise
    except BrokenPipeError:
        return EXIT_OK
    except Exception as e:  # never leak a bare traceback as the only diagnostic
        print(json.dumps({"ok": False, "error": "internal", "message": f"{type(e).__name__}: {e}"},
                         ensure_ascii=False), file=sys.stderr)
        return EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main())
