import datetime
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest

SCRIPT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "coordinator_mode.py"))
SKILL = os.path.join(os.path.dirname(os.path.dirname(SCRIPT)), "SKILL.md")


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = os.path.realpath(tmp.name)
        self.home = os.path.join(self.tmp, "state")
        self.project = os.path.join(self.tmp, "My Project")
        os.mkdir(self.project)

    def run_cli(self, *args, env=None, stdin=None):
        e = {k: v for k, v in os.environ.items()
             if k not in ("CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID", "XDG_STATE_HOME")}
        e["COORDINATOR_MODE_HOME"] = self.home
        e.update(env or {})
        return subprocess.run([sys.executable, SCRIPT, *args], input=stdin, env=e,
                              capture_output=True, text=True)

    def hook(self, event, raw=None):
        return self.run_cli("hook", stdin=raw if raw is not None else json.dumps(event))

    def activate(self, session="s1", journal=None):
        journal = journal or os.path.join(self.tmp, "j.md")
        r = self.run_cli("on", "--journal", journal, "--executors", "codex-1", "--session", session)
        self.assertEqual(r.returncode, 0, r.stderr)
        return journal

    def pointer_file(self, session):
        return os.path.join(self.home, "sessions", hashlib.sha256(session.encode()).hexdigest() + ".json")


class SessionTests(Base):
    def test_round_trip_each_source(self):
        cases = [({"CLAUDE_CODE_SESSION_ID": "a"}, []), ({"CODEX_THREAD_ID": "b"}, []),
                 ({}, ["--session", "c"]),
                 ({"CLAUDE_CODE_SESSION_ID": "same", "CODEX_THREAD_ID": "same"}, [])]
        for env, extra in cases:
            with self.subTest(env=env, extra=extra):
                j = os.path.join(self.tmp, "rt.md")
                r = self.run_cli("on", "--journal", j, *extra, env=env)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertIn("COORDINATOR MODE ACTIVE", r.stdout)
                s = self.run_cli("status", *extra, env=env)
                self.assertIn("Journal: " + j, s.stdout)
                self.assertIn("Skill: " + SKILL, s.stdout)
                self.assertIn("Executors: not chosen yet", s.stdout)
                o = self.run_cli("off", *extra, env=env)
                self.assertEqual(o.stdout.strip(), "Coordinator Mode off for this session. Journal kept: " + j)
                self.assertTrue(os.path.exists(j))
                s = self.run_cli("status", *extra, env=env)
                self.assertEqual(s.stdout.strip(), "Coordinator Mode is not active for this session.")

    def test_session_flag_wins_over_env(self):
        self.activate("flag")
        r = self.run_cli("status", "--session", "flag", env={"CLAUDE_CODE_SESSION_ID": "other"})
        self.assertIn("Journal:", r.stdout)

    def test_conflicting_variables_refused(self):
        r = self.run_cli("status", env={"CLAUDE_CODE_SESSION_ID": "a", "CODEX_THREAD_ID": "b"})
        self.assertEqual(r.returncode, 2)
        self.assertIn("--session", r.stderr)
        self.assertIn("CLAUDE_CODE_SESSION_ID", r.stderr)
        self.assertIn("CODEX_THREAD_ID", r.stderr)

    def test_no_session_refused(self):
        for cmd in (["status"], ["off"], ["on", "--journal", os.path.join(self.tmp, "x.md")]):
            r = self.run_cli(*cmd)
            self.assertEqual(r.returncode, 2, cmd)
            self.assertIn("--session <id>", r.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "x.md")))

    def test_off_without_pointer_exits_zero(self):
        r = self.run_cli("off", "--session", "nobody")
        self.assertEqual(r.returncode, 0)
        self.assertIn("not active", r.stdout)

    def test_pointer_file_named_by_hash_and_has_fields(self):
        j = self.activate("weird/../id")
        with open(self.pointer_file("weird/../id")) as f:
            data = json.load(f)
        self.assertEqual(data["session"], "weird/../id")
        self.assertEqual(data["journal"], j)
        self.assertEqual(data["executors"], "codex-1")
        datetime.datetime.fromisoformat(data["activated_at"])


class OnTests(Base):
    def test_project_task_creates_journal_with_template(self):
        r = self.run_cli("on", "--project", self.project, "--task", "Fix the Bug!!", "--session", "s")
        self.assertEqual(r.returncode, 0, r.stderr)
        digest = hashlib.sha256(self.project.encode()).hexdigest()[:8]
        name = datetime.date.today().isoformat() + "-fix-the-bug.md"
        path = os.path.join(self.home, "journals", "My Project-" + digest, name)
        self.assertIn("Journal: " + path, r.stdout)
        with open(path, encoding="utf-8") as f:
            text = f.read()
        self.assertTrue(text.startswith("# Coordinator journal: fix-the-bug\n\nCOORDINATOR MODE ACTIVE"))
        self.assertIn("Journal: " + path + "\n", text)
        self.assertIn("Project: " + self.project + "\n", text)
        self.assertIn("Started: " + datetime.date.today().isoformat(), text)
        for h in ("Goal and constraints", "Decisions and approvals", "Invariants", "Verification plan",
                  "Work items", "Workers", "Accepted results and evidence",
                  "Open questions and blockers", "Next action", "Log"):
            self.assertIn("\n## " + h + "\n", text)
        self.assertTrue(text.index(r.stdout.strip().splitlines()[0]) < text.index("## Goal"))

    def test_long_slug_truncated_and_empty_slug_refused(self):
        r = self.run_cli("on", "--project", self.project, "--task", "a" * 80, "--session", "s")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("-" + "a" * 48 + ".md", r.stdout)
        r = self.run_cli("on", "--project", self.project, "--task", "!!!", "--session", "s")
        self.assertEqual(r.returncode, 2)
        self.assertIn("--task", r.stderr)

    def test_missing_project_refused(self):
        r = self.run_cli("on", "--project", os.path.join(self.tmp, "nope"), "--task", "x", "--session", "s")
        self.assertEqual(r.returncode, 2)
        self.assertIn("on --project", r.stderr)

    def test_existing_journal_untouched(self):
        j = os.path.join(self.tmp, "keep.md")
        body = b"# mine\r\nnot a template \xc3\xa9\n"
        with open(j, "wb") as f:
            f.write(body)
        self.activate("s", journal=j)
        with open(j, "rb") as f:
            self.assertEqual(f.read(), body)

    def test_second_on_replaces_pointer(self):
        a, b = os.path.join(self.tmp, "a.md"), os.path.join(self.tmp, "b.md")
        self.activate("s", journal=a)
        self.activate("s", journal=b)
        s = self.run_cli("status", "--session", "s").stdout
        self.assertIn("Journal: " + b, s)
        self.assertNotIn(a, s)

    def test_relative_journal_refused_with_next_command(self):
        r = self.run_cli("on", "--journal", "rel/j.md", "--session", "s")
        self.assertEqual(r.returncode, 2)
        self.assertIn("absolute", r.stderr)
        self.assertIn("on --journal /", r.stderr)
        self.assertFalse(os.path.exists(self.pointer_file("s")))

    def test_missing_location_refused(self):
        r = self.run_cli("on", "--session", "s")
        self.assertEqual(r.returncode, 2)
        self.assertIn("--project <dir> --task <slug>", r.stderr)

    def test_skill_override_and_state_root_fallbacks(self):
        r = self.run_cli("on", "--journal", os.path.join(self.tmp, "j.md"), "--skill", "/x/SKILL.md",
                         "--session", "s")
        self.assertIn("Skill: /x/SKILL.md", r.stdout)
        xdg = os.path.join(self.tmp, "xdg")
        r = self.run_cli("on", "--journal", os.path.join(self.tmp, "j2.md"), "--session", "s",
                         env={"COORDINATOR_MODE_HOME": "", "XDG_STATE_HOME": xdg})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(os.path.isdir(os.path.join(xdg, "coordinator-mode", "sessions")))


class HookTests(Base):
    def test_nothing_without_pointer(self):
        for name in ("SessionStart", "PreCompact"):
            r = self.hook({"hook_event_name": name, "session_id": "ghost"})
            self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""))

    def test_session_start_text(self):
        j = self.activate("s1")
        r = self.hook({"hook_event_name": "SessionStart", "session_id": "s1"})
        self.assertEqual(r.returncode, 0)
        self.assertIn("Skill: " + SKILL + "\nJournal: " + j + "\nExecutors: codex-1\n", r.stdout)
        self.assertIn("Your context was just compacted, cleared or resumed.", r.stdout)
        self.assertNotIn("Keep the following", r.stdout)

    def test_pre_compact_text(self):
        j = self.activate("s1")
        r = self.hook({"hook_event_name": "PreCompact", "session_id": "s1"})
        self.assertEqual(r.returncode, 0)
        self.assertTrue(r.stdout.startswith(
            "This session is in Coordinator Mode. Keep the following five lines verbatim"))
        self.assertIn("Skill: " + SKILL + "\nJournal: " + j + "\nExecutors: codex-1\nAfter any", r.stdout)
        self.assertIn("ids of workers that are still running.", r.stdout)

    def test_other_session_and_other_events_silent(self):
        self.activate("s1")
        for ev in ({"hook_event_name": "SessionStart", "session_id": "s2"},
                   {"hook_event_name": "Stop", "session_id": "s1"},
                   {"session_id": "s1"},
                   {"hook_event_name": "SessionStart"},
                   {"hook_event_name": "SessionStart", "session_id": ""}):
            r = self.hook(ev)
            self.assertEqual((r.returncode, r.stdout), (0, ""), ev)

    def test_subagent_silent(self):
        self.activate("s1")
        r = self.hook({"hook_event_name": "PreCompact", "session_id": "s1", "agent_id": "sub-9"})
        self.assertEqual((r.returncode, r.stdout), (0, ""))
        r = self.hook({"hook_event_name": "PreCompact", "session_id": "s1", "agent_id": ""})
        self.assertIn("Coordinator Mode", r.stdout)

    def test_malformed_stdin(self):
        self.activate("s1")
        for raw in ("not json", "", "[1]"):
            r = self.hook(None, raw=raw)
            self.assertEqual((r.returncode, r.stdout), (1, ""), raw)
            self.assertTrue(r.stderr.strip())

    def test_corrupt_pointer(self):
        self.activate("s1")
        for content in ("{broken", "[]", json.dumps({"skill": "x"})):
            with open(self.pointer_file("s1"), "w") as f:
                f.write(content)
            r = self.hook({"hook_event_name": "SessionStart", "session_id": "s1"})
            self.assertEqual((r.returncode, r.stdout), (1, ""), content)
            self.assertEqual(len(r.stderr.strip().splitlines()), 1)
        s = self.run_cli("status", "--session", "s1")
        self.assertEqual(s.returncode, 1)
        self.assertIn("off --session s1", s.stderr)
        self.assertEqual(self.run_cli("off", "--session", "s1").returncode, 0)
        self.assertFalse(os.path.exists(self.pointer_file("s1")))


if __name__ == "__main__":
    unittest.main()
