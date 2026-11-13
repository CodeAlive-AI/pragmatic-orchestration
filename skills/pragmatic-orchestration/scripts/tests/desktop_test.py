"""Offline tests for lib/desktop.py — synthetic fixtures and a fake agent-desktop, no real apps."""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import sqlite3
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

LIB = Path(__file__).resolve().parents[1] / "lib"
sys.path.insert(0, str(LIB))
import desktop  # noqa: E402
import sessions  # noqa: E402

CLAUDE_ID = "local_11111111-aaaa-bbbb-cccc-000000000001"
CLAUDE_ARCHIVED = "local_11111111-aaaa-bbbb-cccc-000000000002"
CLI_SID = "22222222-aaaa-bbbb-cccc-000000000001"
CODEX_ID = "33333333-aaaa-bbbb-cccc-000000000001"
CODEX_CLI = "33333333-aaaa-bbbb-cccc-000000000002"
CODEX_CHILD = "33333333-aaaa-bbbb-cccc-000000000003"


def write_jsonl(path: Path, records: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records), encoding="utf-8")


def write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")


class Fixture(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="porch desktop ")
        self.addCleanup(tmp.cleanup)
        home = Path(tmp.name)
        self.home = home
        # Claude Desktop code-tab manifests + linked claude-code transcript.
        man = home / "Library" / "Application Support" / "Claude" / "claude-code-sessions" / "org" / "user"
        write_json(man / f"{CLAUDE_ID}.json", {
            "sessionId": CLAUDE_ID, "cliSessionId": CLI_SID, "cwd": "/work/repo/.worktrees/x",
            "originCwd": "/work/repo", "title": "Fix flaky login test", "isArchived": False,
            "createdAt": 1767225600000, "lastActivityAt": 1767229200000, "completedTurns": 2,
            "model": "claude-opus-5-5", "permissionMode": "acceptEdits"})
        write_json(man / f"{CLAUDE_ARCHIVED}.json", {
            "sessionId": CLAUDE_ARCHIVED, "cliSessionId": "gone", "cwd": "/work/other",
            "title": "Old spike", "isArchived": True,
            "createdAt": 1767100000000, "lastActivityAt": 1767100000000})
        write_jsonl(home / ".claude" / "projects" / "-work-repo" / f"{CLI_SID}.jsonl", [
            {"type": "user", "timestamp": "2026-01-01T00:00:01Z", "sessionId": CLI_SID,
             "origin": {"kind": "human"}, "message": {"role": "user", "content": "why does login flake?"}},
            {"type": "assistant", "timestamp": "2026-01-01T00:00:02Z", "sessionId": CLI_SID,
             "message": {"role": "assistant", "model": "claude-opus-5-5",
                         "content": [{"type": "text", "text": "A race in the session fixture."}]}},
            {"type": "user", "timestamp": "2026-01-01T00:00:03Z", "sessionId": CLI_SID,
             "origin": {"kind": "human"}, "message": {"role": "user", "content": "fix it"}},
            {"type": "assistant", "timestamp": "2026-01-01T00:00:04Z", "sessionId": CLI_SID,
             "message": {"role": "assistant", "model": "claude-opus-5-5",
                         "content": [{"type": "text", "text": "Fixed the race; tests pass."}]}},
        ])
        # Codex state index + canonical rollout.
        codex = home / ".codex"
        rollout = codex / "sessions" / "2026" / "01" / "02" / f"rollout-2026-01-02T00-00-00-{CODEX_ID}.jsonl"
        write_jsonl(rollout, [
            {"timestamp": "2026-01-02T00:00:00Z", "type": "session_meta", "payload": {"id": CODEX_ID, "cwd": "/work/web"}},
            {"timestamp": "2026-01-02T00:00:01Z", "type": "event_msg",
             "payload": {"type": "user_message", "turn_id": "t1", "message": "add dark mode"}},
            {"timestamp": "2026-01-02T00:00:02Z", "type": "event_msg",
             "payload": {"type": "agent_message", "turn_id": "t1", "message": "Dark mode added.", "phase": "final"}},
        ])
        conn = sqlite3.connect(codex / "state_5.sqlite")
        conn.executescript("""
            CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT, created_at TEXT, updated_at_ms INTEGER,
                source TEXT, model_provider TEXT, cwd TEXT, title TEXT, archived INTEGER, first_user_message TEXT,
                name TEXT, is_pinned INTEGER, model TEXT, agent_nickname TEXT);
            CREATE TABLE thread_spawn_edges (parent_thread_id TEXT, child_thread_id TEXT PRIMARY KEY);
        """)
        rows = [
            (CODEX_ID, str(rollout), "2026-01-02T00:00:00Z", 1767312000000, "vscode", "openai", "/work/web",
             "add dark mode", 0, "add dark mode", "Dark mode for the web app", 1, "gpt-6.1", None),
            (CODEX_CLI, None, "2026-01-01T00:00:00Z", 1767225600000, "cli", "openai", "/work/web",
             "cli thread", 0, "cli thread", None, 0, "gpt-6.1", None),
            (CODEX_CHILD, None, "2026-01-02T00:00:00Z", 1767312000001, "vscode", "openai", "/work/web",
             "child", 0, "child", None, 0, "gpt-6.1", "Planck"),
        ]
        conn.executemany("INSERT INTO threads VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        conn.execute("INSERT INTO thread_spawn_edges VALUES (?,?)", (CODEX_ID, CODEX_CHILD))
        conn.commit()
        conn.close()
        env = {"CODEX_HOME": str(codex), "PORCH_HISTORY_ROOT_CLAUDE_CODE": str(home / ".claude" / "projects")}
        for p in (patch.object(sessions, "HOME", home), patch.object(sessions, "IS_MACOS", True),
                  patch.object(sessions, "IS_WINDOWS", False), patch.dict(os.environ, env)):
            p.start()
            self.addCleanup(p.stop)

    def run_cli(self, *argv: str, stdin: str = "") -> tuple[int, list[dict], str]:
        out, err = io.StringIO(), io.StringIO()
        with patch.object(sys, "argv", ["desktop.py", *argv]), patch.object(sys, "stdin", io.StringIO(stdin)), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = desktop.main()
        lines = [json.loads(l) for l in out.getvalue().splitlines() if l.strip()]
        return code, lines, err.getvalue()


class CatalogueTests(Fixture):
    def test_list_defaults_to_active_desktop_sessions_newest_first(self):
        code, lines, _ = self.run_cli("list")
        self.assertEqual(code, 0)
        recs = [l for l in lines if not l.get("_summary")]
        self.assertEqual([r["id"] for r in recs], [CODEX_ID, CLAUDE_ID])  # cli thread, subagent, archived left out
        codex, claude = recs
        self.assertEqual(codex["title"], "Dark mode for the web app")   # app-visible name, not first message
        self.assertEqual(codex["first_message"], "add dark mode")
        self.assertTrue(codex["pinned"])
        self.assertEqual(codex["url"], f"codex://threads/{CODEX_ID}")
        self.assertEqual(claude["project"], "repo")                      # from originCwd, not the worktree
        self.assertEqual(claude["url"], f"claude://claude.ai/epitaxy/{CLAUDE_ID}")
        self.assertEqual(claude["read"], f"porch desktop read claude:{CLAUDE_ID}")

    def test_filters(self):
        _, lines, _ = self.run_cli("list", "--state", "archived")
        self.assertEqual([l["id"] for l in lines if "id" in l], [CLAUDE_ARCHIVED])
        _, lines, _ = self.run_cli("list", "--app", "codex", "--source", "any", "--include-subagents")
        self.assertEqual({l["id"] for l in lines if "id" in l}, {CODEX_ID, CODEX_CLI, CODEX_CHILD})
        _, lines, _ = self.run_cli("list", "--project", "REPO")
        self.assertEqual([l["id"] for l in lines if "id" in l], [CLAUDE_ID])
        _, lines, _ = self.run_cli("list", "--title", "dark MODE")
        self.assertEqual([l["id"] for l in lines if "id" in l], [CODEX_ID])


class SearchReadTests(Fixture):
    def test_search_groups_hits_per_session_with_read_hint(self):
        code, lines, _ = self.run_cli("search", "-i", "RACE")
        self.assertEqual(code, 0)
        recs = [l for l in lines if "id" in l]
        self.assertEqual([r["id"] for r in recs], [CLAUDE_ID])
        hits = recs[0]["hits"]
        self.assertTrue(all("race" in h["text"].lower() for h in hits))
        self.assertIn("--around", recs[0]["read"])
        self.assertTrue(lines[-1]["scan_complete"])

    def test_search_matches_titles_and_codex_rollouts(self):
        _, lines, _ = self.run_cli("search", "Dark mode")
        rec = next(l for l in lines if l.get("id") == CODEX_ID)
        self.assertEqual(rec["hits"][0]["where"], "title")
        self.assertTrue(any(h.get("kind") == "assistant" for h in rec["hits"]))

    def test_read_tail_around_and_resolution(self):
        _, lines, _ = self.run_cli("read", f"claude:{CLAUDE_ID}", "--tail", "2")
        frags = [l for l in lines if "seq" in l]
        self.assertEqual([f["text"] for f in frags], ["fix it", "Fixed the race; tests pass."])
        first = self.run_cli("read", CLAUDE_ID, "--all")[1]
        seqs = [l["seq"] for l in first if "seq" in l]
        around = self.run_cli("read", CLAUDE_ID, "--around", str(seqs[1]), "--context", "1")[1]
        self.assertEqual([l["seq"] for l in around if "seq" in l], seqs[0:3])
        # exact title and id prefix resolve; codex reads through the index to the rollout
        _, lines, _ = self.run_cli("read", "Dark mode for the web app", "--tail", "1")
        self.assertEqual([l["text"] for l in lines if "seq" in l], ["Dark mode added."])

    def test_unknown_and_ambiguous_targets_say_what_to_do(self):
        code, _, err = self.run_cli("read", "nope")
        self.assertEqual(code, desktop.EXIT_FAILED)
        self.assertEqual(json.loads(err)["error"], "not_found")
        self.assertIn("porch desktop list", json.loads(err)["next"])
        code, _, err = self.run_cli("read", "33333333")
        self.assertEqual(code, desktop.EXIT_USAGE)
        self.assertEqual(json.loads(err)["error"], "ambiguous")


class HelperTests(unittest.TestCase):
    def test_parse_target(self):
        self.assertEqual(desktop.parse_target("claude:local_x"), ("claude", "local_x"))
        self.assertEqual(desktop.parse_target("local_x"), ("claude", "local_x"))
        self.assertEqual(desktop.parse_target("codex:abc"), ("codex", "abc"))
        self.assertEqual(desktop.parse_target("Some: title"), (None, "Some: title"))

    def test_pick_project(self):
        projects = [("my-projects · ~", "r1"), ("Garden Planner", "r2"), ("demo", "r3"), ("demo-landing", "r4")]
        self.assertEqual(desktop._pick_project(projects, "my-projects")[1], "r1")
        self.assertEqual(desktop._pick_project(projects, "garden-planner")[1], "r2")
        self.assertEqual(desktop._pick_project(projects, "DEMO")[1], "r3")
        with self.assertRaises(desktop.Fail) as e:
            desktop._pick_project(projects, "nothing")
        self.assertEqual(e.exception.kind, "not_found")

    def test_excerpt_centres_on_match(self):
        text = "a" * 100 + "NEEDLE" + "b" * 100
        ex = desktop._excerpt(text, (100, 106), 20)
        self.assertIn("NEEDLE", ex)
        self.assertTrue(ex.startswith("…") and ex.endswith("…"))


FAKE_AD = r'''#!/usr/bin/env python3
import json, os, sys
log = os.environ["FAKE_AD_LOG"]
with open(log, "a") as fh:
    fh.write(json.dumps(sys.argv[1:]) + "\n")
cmd = sys.argv[1]
ok = lambda data: print(json.dumps({"ok": True, "data": data}))
if cmd == "list-windows":
    ok([{"id": "w-1", "pid": 1, "title": "Claude", "accessible": True, "visible": True,
         "bounds": {"width": 1000, "height": 800}}])
elif cmd == "find":
    args = sys.argv[2:]
    name = args[args.index("--name") + 1] if "--name" in args else None
    if name and name.startswith("More options for "):
        ok({"matches": [{"ref_id": "@s:e9", "name": name, "path": ["window:Claude", "complementary:Sidebar"]}]})
    elif "--role" in args and args[args.index("--role") + 1] == "webarea":
        ok({"matches": [{"ref_id": "@s:e1", "name": "Something else - Claude Code"}]})
    elif "--text" in args:
        text = args[args.index("--text") + 1]
        ok({"matches": [{"ref_id": "@s:e5", "name": text + " Started by Claude from X",
                         "path": ["complementary:Sidebar"]}]})
    else:
        ok({"matches": []})
else:
    ok({})
'''


class UIGuardTests(Fixture):
    def setUp(self):
        super().setUp()
        bindir = self.home / "bin"
        bindir.mkdir()
        fake = bindir / "agent-desktop"
        fake.write_text(FAKE_AD, encoding="utf-8")
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
        self.log = self.home / "ad.log"
        for p in (patch.dict(os.environ, {"PATH": f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}",
                                          "FAKE_AD_LOG": str(self.log)}),
                  patch.object(desktop.sys, "platform", "darwin"),
                  patch.object(desktop, "_ax_manual_accessibility", lambda pid: "enabled")):
            p.start()
            self.addCleanup(p.stop)

    def calls(self) -> list[list[str]]:
        return [json.loads(l) for l in self.log.read_text().splitlines()] if self.log.exists() else []

    def test_start_refuses_when_the_user_is_in_the_app(self):
        with patch.object(desktop, "frontmost_bundle", lambda: "com.anthropic.claudefordesktop"):
            code, _, err = self.run_cli("start", "--app", "claude", "--project", "repo", "--prompt", "hi")
        self.assertEqual(code, desktop.EXIT_NEEDS_USER)
        self.assertIn("--allow-frontmost", json.loads(err)["next"])
        self.assertFalse(any(c[0] == "click" for c in self.calls()))

    def test_archive_dry_run_resolves_control_and_clicks_nothing(self):
        with patch.object(desktop, "frontmost_bundle", lambda: "company.example.Other"):
            code, lines, err = self.run_cli("archive", CLAUDE_ID, "--dry-run")
        self.assertEqual(code, 0, err)
        self.assertEqual(lines[0]["status"], "dry_run")
        self.assertTrue(lines[0]["focus_kept"])
        calls = self.calls()
        self.assertFalse(any(c[0] == "click" for c in calls))
        self.assertTrue(all("w-1" in c for c in calls if c[0] == "find"))
        self.assertTrue(any("More options for Fix flaky login test" in c for c in calls))

    def test_archive_of_open_session_in_frontmost_app_needs_the_user(self):
        with patch.object(desktop, "frontmost_bundle", lambda: "com.anthropic.claudefordesktop"), \
                patch.object(desktop.UI, "page_title", lambda self: "Fix flaky login test"):
            code, _, err = self.run_cli("archive", CLAUDE_ID)
        self.assertEqual(code, desktop.EXIT_NEEDS_USER)
        self.assertEqual(json.loads(err)["error"], "app_in_use")

    def test_already_archived_is_a_no_op(self):
        code, lines, _ = self.run_cli("archive", CLAUDE_ARCHIVED)
        self.assertEqual((code, lines[0]["status"]), (0, "already_archived"))
        self.assertEqual(self.calls(), [])

    def test_claude_unarchive_explains_the_alternative(self):
        code, _, err = self.run_cli("unarchive", CLAUDE_ARCHIVED)
        self.assertEqual(code, desktop.EXIT_NEEDS_USER)
        self.assertIn("unarchive_session", json.loads(err)["next"])


if __name__ == "__main__":
    unittest.main()
