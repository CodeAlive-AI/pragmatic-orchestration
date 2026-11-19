"""Offline Cursor CLI normalizer and process-per-turn steering tests."""
from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path

LIB = Path(__file__).resolve().parents[1] / "lib"
FAKE = Path(__file__).resolve().parent / "fakes" / "fake-cursor"
import sys

sys.path.insert(0, str(LIB))

import cursor_config  # noqa: E402
from normalize_stream import normalize_cursor  # noqa: E402
from steer.adapters.cursor import CursorAdapter  # noqa: E402


SESSION = "11111111-2222-4333-8444-555555555555"


def _run_adapter(adapter: CursorAdapter, timeout: float = 10.0) -> list:
    deadline = time.time() + timeout
    events = []
    while time.time() < deadline:
        events.extend(adapter.poll_events())
        if adapter.is_done():
            events.extend(adapter.poll_events())
            break
        time.sleep(0.02)
    return events


class NormalizeCursorTests(unittest.TestCase):
    def test_result_beats_delta_and_failures_are_errors(self):
        self.assertEqual(
            normalize_cursor({"type": "assistant", "message": {"content": [{"type": "text", "text": "P"}]}}),
            ("text", "P"),
        )
        self.assertEqual(
            normalize_cursor({"type": "result", "subtype": "success", "is_error": False, "result": "PONG"}),
            ("result", "PONG"),
        )
        self.assertEqual(
            normalize_cursor({"type": "result", "is_error": False, "result": ""}),
            ("result", ""),
        )
        self.assertEqual(
            normalize_cursor({"type": "result", "subtype": "success", "is_error": True, "result": "nope"}),
            ("error", "nope"),
        )
        self.assertEqual(
            normalize_cursor({"type": "result", "subtype": "error", "is_error": False, "result": ""}),
            ("error", "error"),
        )
        self.assertEqual(normalize_cursor({"type": "system", "subtype": "init"})[0], "progress")
        self.assertEqual(normalize_cursor({"type": "user", "message": {"content": "secret"}})[0], "progress")
        self.assertEqual(
            normalize_cursor({"type": "thinking", "subtype": "delta", "text": "because"}),
            ("thought", "because"),
        )
        self.assertEqual(
            normalize_cursor({"type": "thinking", "subtype": "completed", "text": ""}),
            ("progress", "completed"),
        )
        self.assertEqual(
            normalize_cursor({"type": "tool_call", "subtype": "started", "tool_call": {"editToolCall": {}}}),
            ("tool_started", "edit"),
        )
        self.assertEqual(
            normalize_cursor({"type": "tool_call", "subtype": "completed", "tool_call": {"createPlanToolCall": {}}}),
            ("tool_completed", "createPlan"),
        )
        self.assertEqual(
            normalize_cursor({"type": "interaction_query", "query_type": "createPlanRequestQuery"}),
            ("progress", "createPlanRequestQuery"),
        )
        self.assertEqual(normalize_cursor({"type": "event", "mystery": 1}), ("progress", "event"))

    def test_flush_after_deltas_emits_only_unseen_suffix(self):
        """cursor-agent streams deltas, then one full message: emit text once."""
        state: dict = {}

        def ev(text: str, timestamped: bool = True):
            obj = {"type": "assistant",
                   "message": {"role": "assistant",
                               "content": [{"type": "text", "text": text}]}}
            if timestamped:
                obj["timestamp_ms"] = 1
            return normalize_cursor(obj, state)

        self.assertEqual(ev("P"), ("text", "P"))
        self.assertEqual(ev("ONG"), ("text", "ONG"))
        # The complete-message flush repeats the deltas: nothing new to emit.
        self.assertEqual(ev("PONG", timestamped=False), ("progress", "assistant"))
        # A flush that adds text emits just the suffix.
        self.assertEqual(ev("PA"), ("text", "PA"))
        self.assertEqual(ev("PAX", timestamped=False), ("text", "X"))
        # A later turn's fresh text still emits in full.
        self.assertEqual(ev("NEXT"), ("text", "NEXT"))


class CursorConfigTests(unittest.TestCase):
    def test_prepare_copies_config_and_links_the_rest(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "real-cursor"
            (src / "chats").mkdir(parents=True)
            (src / "acp-sessions").mkdir()
            (src / "cli-config.json").write_text('{"model":{"modelId":"default"}}', encoding="utf-8")
            (src / "mcp.json").write_text("{}", encoding="utf-8")
            root, warnings = cursor_config.prepare(
                Path(tmp) / "isolated", env={"CURSOR_CONFIG_DIR": str(src)})
            root_path = Path(root)
            seeded = root_path / "cli-config.json"
            self.assertTrue(seeded.is_file())
            self.assertFalse(seeded.is_symlink())
            self.assertEqual(seeded.read_text(encoding="utf-8"),
                             '{"model":{"modelId":"default"}}')
            # Writable copy: a mutated model lands on the copy, never the source.
            seeded.write_text('{"model":{"modelId":"grok-4.7"}}', encoding="utf-8")
            self.assertEqual((src / "cli-config.json").read_text(encoding="utf-8"),
                             '{"model":{"modelId":"default"}}')
            if os.name != "nt":
                self.assertEqual(warnings, [])
                self.assertTrue((root_path / "chats").is_symlink())
                self.assertTrue((root_path / "acp-sessions").is_symlink())
                self.assertTrue((root_path / "mcp.json").is_symlink())

    def test_prepare_without_source_creates_empty_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, _ = cursor_config.prepare(
                env={"CURSOR_CONFIG_DIR": str(Path(tmp) / "absent")})
            self.assertTrue(Path(root).is_dir())


class CursorSteerTests(unittest.TestCase):
    def test_queued_steer_resumes_a_new_process(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "argv.jsonl"
            env = {
                "PORCH_FAKE_ARGV_LOG": str(log),
                "PORCH_FAKE_CURSOR_DELAY": "0.6",
                "PORCH_FAKE_CURSOR_MODE": "ok",
            }
            previous = {key: os.environ.get(key) for key in env}
            os.environ.update(env)
            try:
                adapter = CursorAdapter(
                    binary=str(FAKE), model="", cwd=tmp,
                    artifacts_dir=tmp, agent_id="cursor",
                )
                seen: list[str] = []

                def ready(session_id: str) -> None:
                    seen.append(session_id)

                adapter.on_session_ready = ready
                adapter.start("first prompt")
                queued = adapter.steer("second prompt", "queue", "cid-1")
                interrupted = adapter.steer("stop", "interrupt", "cid-2")
                self.assertTrue(queued.ok)
                self.assertEqual(queued.status, "queued")
                self.assertEqual(queued.meta.get("promptId"), "cid-1")
                self.assertFalse(interrupted.ok)
                self.assertEqual(interrupted.status, "rejected")
                deadline = time.time() + 8
                events = []
                while time.time() < deadline:
                    events.extend(adapter.poll_events())
                    if adapter.is_done():
                        events.extend(adapter.poll_events())
                        break
                    time.sleep(0.02)
                self.assertTrue(adapter.is_done(), "adapter did not finish")
                self.assertEqual(adapter.final_text(), "FAKE_CURSOR_STEERED")
                self.assertEqual(adapter.session_id, SESSION)
                self.assertEqual(seen, [SESSION])
                self.assertEqual(adapter.exit_code(), 0)
                late = adapter.steer("too late", "auto", "cid-3")
                self.assertFalse(late.ok)
                acks = [ev.raw for ev in events if ev.kind == "steer_ack" and isinstance(ev.raw, dict)]
                self.assertTrue(any(item.get("promptId") == "cid-1" and item.get("status") == "completed" for item in acks), acks)
                rows = [json.loads(line) for line in log.read_text().splitlines() if line.strip()]
                self.assertEqual(len(rows), 2)
                self.assertNotIn("--resume", rows[0]["argv"])
                self.assertNotIn("--model", rows[0]["argv"])
                self.assertIn("--force", rows[0]["argv"])
                self.assertNotIn("--mode", rows[0]["argv"])
                self.assertEqual(rows[0]["stdin"], "first prompt")
                self.assertIn("--resume", rows[1]["argv"])
                self.assertEqual(rows[1]["argv"][rows[1]["argv"].index("--resume") + 1], SESSION)
                self.assertEqual(rows[1]["stdin"], "second prompt")
                self.assertNotIn("first prompt", " ".join(rows[0]["argv"]))
            finally:
                for key, value in previous.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
                adapter.close()

    def test_failed_turn_surfaces_stderr_and_fails_queued_steers(self):
        """A process that fails on stderr explains itself; queued steers get an
        explicit failed ack instead of disappearing."""
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "argv.jsonl"
            home = Path(tmp) / "home"
            home.mkdir()
            env = {
                "PORCH_FAKE_ARGV_LOG": str(log),
                "PORCH_FAKE_CURSOR_MODE": "stderr-fail",
                "PORCH_FAKE_CURSOR_DELAY": "0.4",
                "HOME": str(home),
            }
            previous = {key: os.environ.get(key) for key in
                        (*env, "CURSOR_CONFIG_DIR", "XDG_CONFIG_HOME")}
            os.environ.update(env)
            os.environ.pop("CURSOR_CONFIG_DIR", None)
            os.environ.pop("XDG_CONFIG_HOME", None)
            adapter = CursorAdapter(
                binary=str(FAKE), model="", cwd=tmp,
                artifacts_dir=tmp, agent_id="cursor",
            )
            try:
                adapter.start("first prompt")
                queued = adapter.steer("second prompt", "queue", "cid-1")
                self.assertTrue(queued.ok)
                self.assertEqual(queued.status, "queued")
                events = _run_adapter(adapter)
                self.assertTrue(adapter.is_done(), "adapter did not finish")
                self.assertNotEqual(adapter.exit_code(), 0)
                errors = [ev.data for ev in events if ev.kind == "error"]
                self.assertTrue(
                    any("ActionRequiredError" in str(e) for e in errors), errors)
                acks = [ev.raw for ev in events
                        if ev.kind == "steer_ack" and isinstance(ev.raw, dict)]
                self.assertTrue(
                    any(a.get("promptId") == "cid-1" and a.get("status") == "failed"
                        and a.get("evidence") == "turn_failed" for a in acks), acks)
            finally:
                for key, value in previous.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
                adapter.close()

    def test_adapter_spawns_with_isolated_config_dir(self):
        """Every spawned turn gets CURSOR_CONFIG_DIR aimed at a seeded copy,
        never the user's real cli-config.json."""
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "argv.jsonl"
            home = Path(tmp) / "home"
            real_cfg = home / ".cursor"
            real_cfg.mkdir(parents=True)
            (real_cfg / "cli-config.json").write_text(
                '{"model":{"modelId":"default"}}', encoding="utf-8")
            env = {
                "PORCH_FAKE_ARGV_LOG": str(log),
                "PORCH_FAKE_CURSOR_MODE": "ok",
                "HOME": str(home),
            }
            previous = {key: os.environ.get(key) for key in
                        (*env, "CURSOR_CONFIG_DIR", "XDG_CONFIG_HOME")}
            os.environ.update(env)
            os.environ.pop("CURSOR_CONFIG_DIR", None)
            os.environ.pop("XDG_CONFIG_HOME", None)
            adapter = CursorAdapter(
                binary=str(FAKE), model="grok-4.7", cwd=tmp,
                artifacts_dir=tmp, agent_id="cursor",
            )
            try:
                adapter.start("first prompt")
                _run_adapter(adapter)
                self.assertTrue(adapter.is_done(), "adapter did not finish")
                rows = [json.loads(line) for line in log.read_text().splitlines()
                        if line.strip()]
                cfg_dir = rows[0].get("CURSOR_CONFIG_DIR")
                self.assertTrue(cfg_dir, "spawn had no CURSOR_CONFIG_DIR")
                self.assertNotEqual(cfg_dir, str(real_cfg))
                self.assertTrue(
                    (Path(cfg_dir) / "cli-config.json").is_file())
                self.assertEqual(
                    (real_cfg / "cli-config.json").read_text(encoding="utf-8"),
                    '{"model":{"modelId":"default"}}')
            finally:
                for key, value in previous.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
                adapter.close()


if __name__ == "__main__":
    unittest.main()
