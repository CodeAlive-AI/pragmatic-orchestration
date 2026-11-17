"""Claude stream-json -> normalized events: tools, results, system noise, steer acks."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from steer.adapters.claude import ClaudeAdapter, summarize_tool_input  # noqa: E402


def assistant(*blocks, uuid="a"):
    return {"type": "assistant", "uuid": uuid, "message": {"role": "assistant", "content": list(blocks)}}


def tool_use(tid, name, **tool_input):
    return {"type": "tool_use", "id": tid, "name": name, "input": tool_input}


def tool_result(tid, content="ok", is_error=False):
    return {"type": "tool_result", "tool_use_id": tid, "content": content, "is_error": is_error}


def user(*blocks, uuid="u"):
    return {"type": "user", "uuid": uuid, "message": {"role": "user", "content": list(blocks)}}


class ClaudeEventTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.adapter = ClaudeAdapter(binary="unused", model="t", cwd=self.tmp.name,
                                     artifacts_dir=self.tmp.name, agent_id="claude")
        self.now = 1000.0
        self.adapter._clock = lambda: self.now

    def feed(self, *objs):
        for o in objs:
            self.adapter._handle_obj(o)
        return [(e.kind, e.data) for e in self.adapter.poll_events()]

    def test_whole_message_tools_results_and_flood(self):
        self.adapter._pending_steers["steer-9"] = {"content": "go", "replayed": False}
        stream = [{"type": "system", "subtype": "init", "session_id": "s"}]
        for i in range(200):
            stream.append({"type": "system", "subtype": "hook_started", "hook_id": str(i)})
            stream.append({"type": "system", "subtype": "hook_response", "exit_code": 0, "outcome": "success"})
            stream.append({"type": "command_lifecycle", "state": "started"})
            stream.append({"type": "system", "subtype": "mystery"})
            stream.append({"type": "rate_limit_event", "rate_limit_info": {}})
        stream += [
            user({"type": "text", "text": "initial task"}, uuid="initial"),
            assistant({"type": "thinking", "thinking": "SECRET THOUGHT"},
                      {"type": "text", "text": "Looking. "},
                      tool_use("t1", "Bash", command="echo " + "x" * 400),
                      tool_use("t2", "Edit", file_path="/a/b.py", old_string="OLD", new_string="NEW")),
            user(tool_result("t1"), tool_result("t2", content="boom failed", is_error=True), uuid="tr"),
            user({"type": "text", "text": "steer text"}, uuid="steer-9"),
            assistant({"type": "text", "text": "done"}),
            {"type": "result", "result": "FINAL", "uuid": "r"},
        ]
        events = self.feed(*stream)
        kinds = [k for k, _ in events]
        tools = [d for k, d in events if k == "tool"]
        self.assertEqual(len(tools), 4, tools)
        self.assertTrue(tools[0].startswith("Bash: echo xxx"))
        self.assertLessEqual(len(tools[0]), 200)
        self.assertEqual(tools[1], "Edit: /a/b.py")
        self.assertEqual(tools[2], "completed:Bash")
        self.assertEqual(tools[3], "completed:Edit [error] boom failed")
        blob = json.dumps(events)
        for leak in ("SECRET THOUGHT", "OLD", "NEW\""):
            self.assertNotIn(leak, blob)
        replays = [d for k, d in events if k == "user_replay"]
        self.assertEqual(replays, ["initial task", "steer text"])
        acks = [e for e in events if e[0] == "steer_ack"]
        self.assertEqual(acks, [("steer_ack", "steer-9")])
        # Flood is bounded: init + one coalesced mystery + one rate limit event.
        progress = [d for k, d in events if k == "progress"]
        self.assertEqual(sorted(progress), ["rate_limit_event", "system:init", "system:mystery"])
        self.assertEqual(self.adapter.final_text(), "FINAL")
        self.assertEqual([d for k, d in events if k == "text"], ["Looking. ", "done"])
        self.assertIn("done", kinds)

    def test_final_text_without_result_comes_from_assistant_text(self):
        self.feed(assistant({"type": "text", "text": "a"}), assistant({"type": "text", "text": "b"}))
        self.assertEqual(self.adapter.final_text(), "ab")

    def test_coalescing_window_reopens(self):
        first = self.feed({"type": "system", "subtype": "mystery"})
        again = self.feed({"type": "system", "subtype": "mystery"})
        self.now += 31
        later = self.feed({"type": "system", "subtype": "mystery"})
        self.assertEqual((len(first), len(again), len(later)), (1, 0, 1))

    def test_substantive_system_subtypes_survive(self):
        events = self.feed(
            {"type": "system", "subtype": "compact_boundary", "trigger": "auto"},
            {"type": "system", "subtype": "api_retry", "error": "rate_limit"},
            {"type": "system", "subtype": "hook_response", "exit_code": 2, "outcome": "error"},
        )
        self.assertEqual([d for _, d in events],
                         ["system:compact_boundary auto", "system:api_retry rate_limit", "system:hook_failed"])
        self.assertIsNone(getattr(self.adapter, "_error", None))

    def test_tool_result_uuid_never_acks_and_unknown_id_is_named_tool(self):
        events = self.feed(user(tool_result("never-seen"), uuid="steer-x"))
        self.assertEqual(events, [("tool", "completed:tool")])

    def test_streamed_and_whole_message_tool_are_not_double_counted(self):
        self.feed({"type": "content_block_start", "index": 1,
                   "content_block": {"type": "tool_use", "id": "t1", "name": "Bash"}})
        events = self.feed(assistant(tool_use("t1", "Bash", command="ls")))
        self.assertEqual([e for e in events if e[0] == "tool"], [])
        self.feed({"type": "content_block_stop", "index": 1})
        events = self.feed(user(tool_result("t1")))
        self.assertEqual(events, [])

    def test_summaries(self):
        cases = [
            ("Read", {"file_path": "/x", "limit": 5}, "/x"),
            ("Write", {"file_path": "/w", "content": "BODY"}, "/w"),
            ("Grep", {"pattern": "foo", "path": "/p"}, "foo"),
            ("Glob", {"pattern": "**/*.py"}, "**/*.py"),
            ("mcp__x__y", {"b": 1, "a": 2}, "keys: a,b"),
            ("Bash", {"command": "a\n  b"}, "a b"),
            ("Bash", None, ""),
        ]
        for name, args, want in cases:
            self.assertEqual(summarize_tool_input(name, args), want)
        self.assertLessEqual(len(summarize_tool_input("Read", {"file_path": "p" * 999})), 200)


if __name__ == "__main__":
    unittest.main()
