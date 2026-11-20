"""What a run saves: compact event streams that still read back as before,
and a registry audit and detached log without a record per streamed piece.

Offline. The steerable runs go through the real `porch` CLI and the fake
backends; nothing here touches a provider or the user's own run folders.
"""
import collections
import contextlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
SCRIPTS_DIR = TESTS_DIR.parent
LIB_DIR = SCRIPTS_DIR / "lib"
PORCH = SCRIPTS_DIR / "porch"
FAKES = TESTS_DIR / "fakes"
sys.path.insert(0, str(LIB_DIR))

from events import (  # noqa: E402
    KNOWN_EVENT_TYPES,
    LIFECYCLE_RAW_MAX,
    PAYLOAD_COPY_TYPES,
    PorchEvent,
)
from steer import supervisor as supervisor_module  # noqa: E402
from steer.adapters.base import AdapterEvent  # noqa: E402
from steer.adapters.codex import CodexAdapter, wire_classify  # noqa: E402
from steer.jsonrpc import FLUSH, HOLD, JsonRpcProcess, RawWire  # noqa: E402
from steer.registry import Registry  # noqa: E402
from steer.tally import LABEL_MAX_CHARS, LABELS_MAX, OTHER_LABEL, STREAM_KINDS, StreamTally  # noqa: E402


def lines(path: Path):
    return path.read_text(encoding="utf-8").splitlines()


def records(path: Path):
    return [json.loads(line) for line in lines(path)]


class FullSwitch:
    """Set or clear PORCH_STREAM_FULL for one test, in this process."""

    def __init__(self, value):
        self.value = value

    def __enter__(self):
        self.saved = os.environ.pop("PORCH_STREAM_FULL", None)
        if self.value is not None:
            os.environ["PORCH_STREAM_FULL"] = self.value

    def __exit__(self, *exc):
        os.environ.pop("PORCH_STREAM_FULL", None)
        if self.saved is not None:
            os.environ["PORCH_STREAM_FULL"] = self.saved


class NormalizedRecordTest(unittest.TestCase):
    def test_content_events_are_written_without_the_backend_payload(self):
        payload = {"method": "item/completed", "params": {"output": "x" * 4000}}
        with FullSwitch(None):
            for kind in sorted(PAYLOAD_COPY_TYPES):
                event = PorchEvent(type=kind, backend="codex-cli", agent_id="codex",
                                   data="item/completed", raw=payload, tool_name="shell")
                record = event.to_stream_dict()
                self.assertNotIn("raw", record, kind)
                # What the readers use is untouched.
                self.assertEqual(record["type"], kind)
                self.assertEqual(record["data"], "item/completed")
                self.assertEqual(record["tool_name"], "shell")
                self.assertEqual(record["agent_id"], "codex")
                # The object in memory still carries the payload for the supervisor.
                self.assertEqual(event.raw, payload)

    def test_lifecycle_events_keep_the_detail_only_they_record(self):
        with FullSwitch(None):
            started = PorchEvent(type="run_started", data="start",
                                 raw={"model": "m", "effort": "high", "access_policy": "readonly"})
            self.assertEqual(started.to_stream_dict()["raw"]["effort"], "high")
            steer = PorchEvent(type="steer_completed", data="completed:ack",
                               raw={"promptId": "p1", "status": "completed", "evidence": "ack"})
            self.assertEqual(steer.to_stream_dict()["raw"]["promptId"], "p1")
            done = PorchEvent(type="run_completed", data="0", raw={"completion_evidence": "prompt_result"})
            self.assertEqual(done.to_stream_dict()["raw"], {"completion_evidence": "prompt_result"})

    def test_a_backend_payload_packed_into_a_lifecycle_event_is_not_written(self):
        with FullSwitch(None):
            big = {"method": "turn/completed", "params": {"items": ["y" * (LIFECYCLE_RAW_MAX + 1)]}}
            record = PorchEvent(type="run_completed", data="0", raw=big).to_stream_dict()
            self.assertNotIn("raw", record)
            self.assertEqual(record["data"], "0")

    def test_the_debug_switch_writes_every_payload_again(self):
        payload = {"params": {"delta": "hello"}}
        with FullSwitch("1"):
            self.assertEqual(PorchEvent(type="answer_delta", data="hello", raw=payload).to_stream_dict()["raw"],
                             payload)
        with FullSwitch("0"):
            self.assertNotIn("raw", PorchEvent(type="answer_delta", data="hello", raw=payload).to_stream_dict())

    def test_every_event_type_is_either_a_payload_copy_or_a_lifecycle_event(self):
        self.assertLessEqual(PAYLOAD_COPY_TYPES, KNOWN_EVENT_TYPES)
        lifecycle = KNOWN_EVENT_TYPES - PAYLOAD_COPY_TYPES
        self.assertEqual(
            {t for t in lifecycle if not t.startswith("steer_")},
            {"run_started", "run_completed", "run_failed", "retry_scheduled", "prompt_complete"},
        )


class OneShotStreamTest(unittest.TestCase):
    STREAM = [
        {"type": "thought", "data": "thinking about it", "padding": "p" * 3000},
        {"type": "text", "data": "The answer ", "padding": "p" * 3000},
        {"type": "text", "data": "is 4.", "padding": "p" * 3000},
        {"type": "end", "stopReason": "EndTurn"},
    ]

    def normalize(self, tmp: Path, **env):
        source = tmp / "stream.jsonl"
        source.write_text("".join(json.dumps(row) + "\n" for row in self.STREAM), encoding="utf-8")
        run_env = {k: v for k, v in os.environ.items() if k != "PORCH_STREAM_FULL"}
        run_env.update(env)
        result = subprocess.run(
            [sys.executable, str(LIB_DIR / "normalize_stream.py"), "--backend", "grok-build",
             "--agent-id", "grok", "--model", "grok-test", "--effort", "high",
             "--access-policy", "readonly", "--input", str(source),
             "--raw-out", str(tmp / "raw.jsonl"), "--extract-text", "--text-out", str(tmp / "final.txt")],
            capture_output=True, text=True, env=run_env, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        return source, [json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")]

    def test_one_shot_run_saves_compact_records_and_the_whole_wire(self):
        with tempfile.TemporaryDirectory() as name:
            tmp = Path(name)
            source, events = self.normalize(tmp)
            by_type = {}
            for event in events:
                by_type.setdefault(event["type"], []).append(event)
            # The run's provenance exists only in this record and stays.
            self.assertEqual(by_type["run_started"][0]["raw"],
                             {"model": "grok-test", "effort": "high", "access_policy": "readonly"})
            self.assertEqual([e["data"] for e in by_type["answer_delta"]], ["The answer ", "is 4."])
            self.assertEqual(by_type["thinking_delta"][0]["data"], "thinking about it")
            for event in by_type["answer_delta"] + by_type["thinking_delta"]:
                self.assertNotIn("raw", event)
            # The wire log is the complete record, byte for byte.
            self.assertEqual((tmp / "raw.jsonl").read_text(), source.read_text())
            self.assertEqual((tmp / "final.txt").read_text().strip(), "The answer is 4.")
            normalized_bytes = sum(len(json.dumps(e)) for e in events)
            self.assertLess(normalized_bytes, source.stat().st_size / 5)

    def test_the_debug_switch_restores_the_embedded_payload(self):
        with tempfile.TemporaryDirectory() as name:
            _source, events = self.normalize(Path(name), PORCH_STREAM_FULL="1")
            answer = [e for e in events if e["type"] == "answer_delta"]
            self.assertEqual(answer[0]["raw"]["padding"], "p" * 3000)


class RawWireTest(unittest.TestCase):
    @staticmethod
    def classify(msg):
        if msg.get("method") == "diff":
            return HOLD, f"diff:{msg['turn']}"
        if msg.get("method") == "turn-end":
            return FLUSH, ""
        if msg.get("method") == "boom":
            raise ValueError("classifier fault")
        return None

    def wire(self, tmp: Path, classify=None):
        path = tmp / "wire.jsonl"
        return path, RawWire(open(path, "a", encoding="utf-8"), classify)

    def feed(self, wire, *messages):
        for message in messages:
            wire.write(json.dumps(message), message)

    def test_only_the_last_superseded_message_is_kept_and_written_at_the_boundary(self):
        with tempfile.TemporaryDirectory() as name:
            path, wire = self.wire(Path(name), self.classify)
            self.feed(wire,
                      {"method": "delta", "n": 1},
                      {"method": "diff", "turn": "a", "v": 1},
                      {"method": "delta", "n": 2},
                      {"method": "diff", "turn": "a", "v": 2},
                      {"method": "diff", "turn": "b", "v": 1},
                      {"method": "diff", "turn": "a", "v": 3})
            wire.write(">> " + json.dumps({"method": "steer"}))  # what porch sent: always written
            # Nothing superseded is on disk while the turn runs.
            self.assertEqual([r for r in lines(path) if '"diff"' in r], [])
            self.feed(wire, {"method": "turn-end"}, {"method": "delta", "n": 3})
            wire.close()
            got = [line if line.startswith(">> ") else json.loads(line) for line in lines(path)]
            self.assertEqual(got, [
                {"method": "delta", "n": 1},
                {"method": "delta", "n": 2},
                '>> {"method": "steer"}',
                {"method": "diff", "turn": "a", "v": 3},
                {"method": "diff", "turn": "b", "v": 1},
                {"method": "turn-end"},
                {"method": "delta", "n": 3},
            ])

    def test_held_messages_are_written_when_the_log_closes(self):
        with tempfile.TemporaryDirectory() as name:
            path, wire = self.wire(Path(name), self.classify)
            self.feed(wire, {"method": "diff", "turn": "a", "v": 1}, {"method": "diff", "turn": "a", "v": 2})
            wire.close()
            self.assertEqual(records(path), [{"method": "diff", "turn": "a", "v": 2}])
            wire.write("late line")  # after close: ignored, not an error
            wire.close()

    def test_a_classifier_fault_never_loses_a_line(self):
        with tempfile.TemporaryDirectory() as name:
            path, wire = self.wire(Path(name), self.classify)
            self.feed(wire, {"method": "boom"}, {"method": "delta"})
            wire.write("not json at all")
            wire.close()
            self.assertEqual(lines(path), ['{"method": "boom"}', '{"method": "delta"}', "not json at all"])

    def test_without_a_classifier_every_line_is_written_as_it_arrives(self):
        with tempfile.TemporaryDirectory() as name:
            path, wire = self.wire(Path(name))
            self.feed(wire, {"method": "diff", "turn": "a", "v": 1})
            self.assertEqual(len(lines(path)), 1)
            wire.close()

    def test_codex_classifier_names_only_the_repeated_turn_diff(self):
        diff = {"method": "turn/diff/updated", "params": {"threadId": "t", "turnId": "u1", "diff": "+x"}}
        other_turn = {"method": "turn/diff/updated", "params": {"threadId": "t", "turnId": "u2", "diff": "+y"}}
        self.assertEqual(wire_classify(diff)[0], HOLD)
        self.assertNotEqual(wire_classify(diff)[1], wire_classify(other_turn)[1])
        self.assertEqual(wire_classify({"method": "turn/completed", "params": {}})[0], FLUSH)
        for method in ("item/completed", "item/commandExecution/outputDelta", "item/agentMessage/delta",
                       "turn/started", "thread/tokenUsage/updated"):
            self.assertIsNone(wire_classify({"method": method, "params": {}}), method)
        self.assertIsNone(wire_classify({"id": 3, "result": {}}))
        self.assertEqual(wire_classify({"method": "turn/diff/updated", "params": None})[0], HOLD)

    def test_a_backend_that_exits_mid_turn_still_leaves_its_last_diff(self):
        script = (
            "import json\n"
            "for v in (1, 2, 3):\n"
            "    print(json.dumps({'method': 'turn/diff/updated',"
            " 'params': {'threadId': 't', 'turnId': 'u', 'diff': '+' * v}}), flush=True)\n"
        )
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "wire.jsonl"
            rpc = JsonRpcProcess([sys.executable, "-c", script])
            rpc.set_raw_out(str(path), wire_classify)
            rpc.start()
            try:
                rpc.proc.wait(timeout=30)
                rpc._reader.join(timeout=30)
                self.assertFalse(rpc._reader.is_alive())
                # Written at end of output, before anyone closes the log.
                self.assertEqual([r["params"]["diff"] for r in records(path)], ["+++"])
            finally:
                rpc.terminate()
                for pipe in (rpc.proc.stdin, rpc.proc.stdout, rpc.proc.stderr):
                    pipe.close()
            self.assertEqual(len(lines(path)), 1)

    def test_concurrent_writers_never_interleave_lines(self):
        with tempfile.TemporaryDirectory() as name:
            path, wire = self.wire(Path(name), self.classify)

            def writer(tag):
                for n in range(200):
                    wire.write(json.dumps({"method": "delta", "tag": tag, "n": n, "pad": "z" * 200}),
                               {"method": "delta"})

            threads = [threading.Thread(target=writer, args=(tag,)) for tag in "abcd"]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            wire.close()
            self.assertEqual(len(records(path)), 800)  # every line parses on its own


class WireLogReleaseTest(unittest.TestCase):
    """When the wire log may give way to the native Codex session."""

    def adapter(self, tmp: Path, *, persist: bool, native: bool = True, error: str = ""):
        adapter = CodexAdapter(binary="fake", model="test", cwd=str(tmp), artifacts_dir=str(tmp), agent_id="codex")
        adapter.persist_session = persist
        adapter._thread_id = "thread-1"
        adapter._error = error or None
        (tmp / "raw").mkdir(exist_ok=True)
        (tmp / "raw" / "codex.jsonl").write_text('{"method": "turn/completed"}\n')
        session = tmp / "rollout-thread-1.jsonl"
        if native:
            session.write_text('{"type": "session_meta"}\n')
        adapter.native_session_path = str(session)
        adapter._release_wire_log()
        return (tmp / "raw" / "codex.jsonl").exists(), (tmp / "raw" / "codex.native.json").exists()

    def check(self, expected, **kwargs):
        with FullSwitch(None), tempfile.TemporaryDirectory() as name:
            self.assertEqual(self.adapter(Path(name), **kwargs), expected, kwargs)

    def test_persistent_thread_with_a_saved_session_gives_up_its_wire_log(self):
        self.check((False, True), persist=True)

    def test_ephemeral_thread_keeps_its_wire_log_whatever_the_backend_reports(self):
        self.check((True, False), persist=False)

    def test_run_that_ended_with_a_backend_error_keeps_its_wire_log(self):
        self.check((True, False), persist=True, error="codex exited 1")

    def test_missing_or_empty_session_file_keeps_the_wire_log(self):
        self.check((True, False), persist=True, native=False)
        with FullSwitch(None), tempfile.TemporaryDirectory() as name:
            tmp = Path(name)
            adapter = CodexAdapter(binary="fake", model="test", cwd=name, artifacts_dir=name, agent_id="codex")
            adapter.persist_session = True
            adapter._thread_id = "thread-1"
            (tmp / "raw").mkdir()
            (tmp / "raw" / "codex.jsonl").write_text("x\n")
            (tmp / "empty.jsonl").write_text("")
            adapter.native_session_path = str(tmp / "empty.jsonl")
            adapter._release_wire_log()
            self.assertTrue((tmp / "raw" / "codex.jsonl").exists())


class RunCase(unittest.TestCase):
    """Whole runs through `porch delegate` and the fake backends."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="porch-stream-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.artifacts = self.root / "artifacts"
        self.native = self.root / "codex-sessions"
        env = {k: v for k, v in os.environ.items() if k != "PORCH_STREAM_FULL"}
        env.update({
            "PORCH_CONFIG": str(TESTS_DIR / "fixtures" / "test-config.json"),
            "PORCH_STEER_DIR": str(self.root / "registry"),
            "PORCH_RUN_DIR": str(self.artifacts),
            "PORCH_OUTPUT_DIR": str(self.artifacts),
            "PORCH_SAVE_OUTPUTS": "1",
            "PORCH_RETENTION_AUTO": "0",
            "PORCH_SUPPRESS_SHELL_WARN": "1",
            "XDG_DATA_HOME": str(self.root / "opencode-data"),
            "PORCH_FAKE_STEER_SLOW": "0.05",
            "PORCH_BIN_CODEX": str(FAKES / "steer" / "fake-codex-steer"),
            "PORCH_BIN_DEVIN": str(FAKES / "fake-devin"),
            "PORCH_FAKE_CODEX_DIFFS": "6",
            "PORCH_FAKE_CODEX_NATIVE_DIR": str(self.native),
            "PYTHONPATH": str(LIB_DIR),
        })
        self.env = env

    def delegate(self, *args, **env):
        result = subprocess.run([str(PORCH), "delegate", *args], cwd=self.root, env={**self.env, **env},
                                text=True, input="", capture_output=True, timeout=120)
        return result

    def run_id(self, result):
        for part in result.stderr.split():
            if part.startswith("run_id="):
                return part.split("=", 1)[1]
        self.fail("no run id in: " + result.stderr[-400:])

    def run_dir(self, run_id):
        return self.root / "registry" / "runs" / run_id

    def stream_events(self, agent="codex"):
        """Stream events of the run by adapter kind, as the normalized stream holds them."""
        normalized = records(self.artifacts / "normalized" / f"{agent}.jsonl")
        return collections.Counter(r["kind"] for r in normalized if r.get("kind") in STREAM_KINDS)


class SteerableRunTest(RunCase):
    def wire_diffs(self, agent="codex"):
        path = self.artifacts / "raw" / f"{agent}.jsonl"
        rows = [json.loads(line) for line in lines(path) if not line.startswith(">> ")]
        return rows, [row for row in rows if row.get("method") == "turn/diff/updated"]

    def test_repeated_turn_diffs_collapse_to_the_last_one(self):
        result = self.delegate("-a", "codex", "TASK")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("FAKE_CODEX_STEER_OK", result.stdout)
        rows, diffs = self.wire_diffs()
        self.assertEqual(len(diffs), 1)
        self.assertEqual(diffs[0]["params"]["diff"].count("\n"), 6)  # the final diff, whole
        methods = [row.get("method") for row in rows]
        self.assertEqual(methods.index("turn/diff/updated") + 1, methods.index("turn/completed"))
        # An ephemeral thread has no native session: the wire log is its record.
        self.assertFalse((self.artifacts / "raw" / "codex.native.json").exists())
        self.assertFalse(self.native.exists())

    def test_compact_run_reads_back_through_events_wait_and_status(self):
        result = self.delegate("-a", "codex", "TASK")
        self.assertEqual(result.returncode, 0, result.stderr)
        run_id = self.run_id(result)
        normalized = records(self.artifacts / "normalized" / "codex.jsonl")
        self.assertTrue(normalized)
        for record in normalized:
            if record["type"] in PAYLOAD_COPY_TYPES:
                self.assertNotIn("raw", record, record["type"])
        self.assertEqual(sum(1 for r in normalized if r.get("data") == "turn/diff/updated"), 6)
        self.assertEqual("".join(r["data"] for r in normalized if r["type"] == "answer_delta"), "FAKE_CODEX")

        page = json.loads(self.delegate("events", run_id, "--cursor", "0", "--max-events", "50").stdout)
        kinds = [event["type"] for event in page["events"]]
        self.assertIn("answer_delta", kinds)
        self.assertIn("run_completed", kinds)
        self.assertTrue(all("raw" not in event for event in page["events"]))

        waited = self.delegate("wait", run_id, "--json", "--timeout", "30")
        self.assertEqual(waited.returncode, 0, waited.stderr)
        self.assertEqual(json.loads(waited.stdout)["status"], "completed")
        self.assertIn("FAKE_CODEX_STEER_OK", (self.artifacts / "final.txt").read_text())

    def test_persistent_run_leaves_a_pointer_to_the_native_session(self):
        result = self.delegate("-a", "codex", "--persist-session", "TASK")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("FAKE_CODEX_STEER_OK", result.stdout)
        run_id = self.run_id(result)
        self.assertFalse((self.artifacts / "raw" / "codex.jsonl").exists())
        pointer = json.loads((self.artifacts / "raw" / "codex.native.json").read_text())
        native_file = Path(pointer["native_session_path"])
        self.assertTrue(native_file.is_file())
        self.assertEqual(native_file.parent, self.native)
        meta = json.loads((self.root / "registry" / "runs" / run_id / "meta.json").read_text())
        self.assertEqual(pointer["native_session_id"], meta["native_session_id"])
        self.assertEqual(pointer["read_with"], f"porch sessions show codex:{meta['native_session_id']}")
        # The readable stream and the answer are still porch's own.
        self.assertTrue((self.artifacts / "normalized" / "codex.jsonl").stat().st_size > 0)
        self.assertIn("FAKE_CODEX_STEER_OK", (self.artifacts / "final.txt").read_text())

    def test_follow_up_turn_of_a_persistent_session_also_leaves_a_pointer(self):
        first = self.delegate("-a", "codex", "--persist-session", "TASK")
        self.assertEqual(first.returncode, 0, first.stderr)
        second = self.delegate("-a", "codex", "--continue-run", self.run_id(first), "MORE",
                               PORCH_RUN_DIR=str(self.root / "artifacts-2"),
                               PORCH_OUTPUT_DIR=str(self.root / "artifacts-2"))
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertFalse((self.root / "artifacts-2" / "raw" / "codex.jsonl").exists())
        self.assertTrue((self.root / "artifacts-2" / "raw" / "codex.native.json").is_file())

    def test_wire_log_stays_when_the_native_session_file_is_not_there(self):
        result = self.delegate("-a", "codex", "--persist-session", "TASK", PORCH_FAKE_CODEX_NATIVE_MISSING="1")
        self.assertEqual(result.returncode, 0, result.stderr)
        _rows, diffs = self.wire_diffs()
        self.assertEqual(len(diffs), 1)
        self.assertFalse((self.artifacts / "raw" / "codex.native.json").exists())

    def test_wire_log_stays_when_codex_names_no_session_file(self):
        env = dict(self.env)
        env.pop("PORCH_FAKE_CODEX_NATIVE_DIR")
        result = subprocess.run([str(PORCH), "delegate", "-a", "codex", "--persist-session", "TASK"],
                                cwd=self.root, env=env, text=True, input="", capture_output=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.artifacts / "raw" / "codex.jsonl").is_file())
        self.assertFalse((self.artifacts / "raw" / "codex.native.json").exists())

    def test_the_debug_switch_keeps_every_diff_the_wire_log_and_the_payloads(self):
        result = self.delegate("-a", "codex", "--persist-session", "TASK", PORCH_STREAM_FULL="1")
        self.assertEqual(result.returncode, 0, result.stderr)
        _rows, diffs = self.wire_diffs()
        self.assertEqual(len(diffs), 6)
        self.assertFalse((self.artifacts / "raw" / "codex.native.json").exists())
        normalized = records(self.artifacts / "normalized" / "codex.jsonl")
        progress = [r for r in normalized if r.get("data") == "turn/diff/updated"]
        self.assertEqual(len(progress), 6)
        self.assertTrue(all(r["raw"]["params"]["diff"] for r in progress))

    def test_acp_run_no_longer_saves_each_update_twice(self):
        result = self.delegate("-a", "devin", "TASK")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("FAKE_DEVIN_OK", result.stdout)
        normalized = records(self.artifacts / "normalized" / "devin.jsonl")
        content = [r for r in normalized if r["type"] in PAYLOAD_COPY_TYPES]
        self.assertTrue(content)
        self.assertTrue(all("raw" not in r for r in content))
        self.assertIn("FAKE_DEVIN_OK", "".join(r["data"] for r in normalized if r["type"] == "answer_delta"))
        # The wire log keeps every update the backend sent.
        wire = (self.artifacts / "raw" / "devin.jsonl").read_text()
        self.assertIn("agent_message_chunk", wire)
        self.assertIn("session/update", wire)


def tally_totals(stream):
    """Events per kind in the `stream` field of an audit record."""
    return {kind: sum(v.values()) if isinstance(v, dict) else v for kind, v in stream.items()}


EVENT_LINE = re.compile(r"^\[porch\] event run_id=\S+ (\S+)(?: (.*))?$")


def event_lines(text):
    """(kind, rest) of every `[porch] event` line."""
    found = []
    for line in text.splitlines():
        match = EVENT_LINE.match(line)
        if match:
            found.append((match.group(1), match.group(2) or ""))
    return found


class StreamTallyTest(unittest.TestCase):
    def test_counts_plain_kinds_and_progress_labels_then_starts_again(self):
        tally = StreamTally()
        self.assertFalse(tally)
        for kind, data in [("text", "a"), ("text", "b"), ("thought", "c"),
                           ("progress", "item/started"), ("progress", "item/started"),
                           ("progress", "stream_reconnected attempt=2")]:
            tally.add(kind, data)
        self.assertTrue(tally)
        self.assertEqual(tally.totals(), {"text": 2, "thought": 1, "progress": 3})
        self.assertEqual(tally.brief(), "progress=3 text=2 thought=1")
        self.assertEqual(tally.take(), {
            "text": 2, "thought": 1,
            "progress": {"item/started": 2, "stream_reconnected attempt=2": 1},
        })
        self.assertFalse(tally)
        self.assertEqual(tally.take(), {})

    def test_free_text_labels_cannot_grow_the_record_without_bound(self):
        tally = StreamTally()
        for i in range(LABELS_MAX + 10):
            tally.add("progress", f"label {i}")
        tally.add("progress", "label 0")
        tally.add("progress", "many   words\n" + "x" * 500)
        taken = tally.take()["progress"]
        self.assertEqual(len(taken), LABELS_MAX + 1)
        self.assertEqual(taken["label 0"], 2)
        self.assertEqual(taken[OTHER_LABEL], 11)
        self.assertEqual(sum(taken.values()), LABELS_MAX + 12)
        tally.add("progress", "many   words\n" + "x" * 500)
        tally.add("progress", "")
        labels = list(tally.take()["progress"])
        self.assertEqual(labels[0], ("many words " + "x" * 500)[:LABEL_MAX_CHARS])
        self.assertEqual(labels[1], "(none)")


class SupervisorRecordsTest(unittest.TestCase):
    """What the supervisor writes for each adapter event, without a backend."""

    def supervisor(self, root: Path, *, detached: bool):
        registry = Registry(root / "registry")
        sup = supervisor_module.Supervisor(
            agent_id="codex", task="t", cwd=str(root), artifacts_dir=str(root / "artifacts"),
            registry=registry, log_file=str(root / "supervisor.log") if detached else "")
        sup.run_id = "run_codex-tally-test"
        registry.run_path(sup.run_id).mkdir(parents=True)
        (root / "artifacts" / "normalized").mkdir(parents=True)
        return sup, registry.run_path(sup.run_id) / "audit.jsonl"

    def feed(self, sup, events):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            for kind, data in events:
                sup._handle_event(AdapterEvent(kind=kind, data=data))
        return err.getvalue()

    EVENTS = [("turn_started", "t1"), ("text", "Hel"), ("text", "lo"), ("thought", "hm"),
              ("progress", "item/started"), ("tool", "Read file"), ("text", "!"),
              ("turn_completed", ""), ("thought", "after")]
    PRINTED = [kind for kind, _ in EVENTS if kind != "tool"]  # a tool event never had a line

    def run_events(self, *, detached, full=None):
        with FullSwitch(full), tempfile.TemporaryDirectory() as name:
            sup, audit = self.supervisor(Path(name), detached=detached)
            log = self.feed(sup, self.EVENTS)
            tail = io.StringIO()
            with contextlib.redirect_stderr(tail):
                sup._flush_stream_tallies()
            return log, tail.getvalue(), records(audit)

    def test_audit_records_lifecycle_events_and_counts_the_stream_between_them(self):
        _log, _tail, audit = self.run_events(detached=False)
        self.assertEqual([(r["event"], r.get("kind")) for r in audit], [
            ("adapter_event", "turn_started"), ("adapter_event", "tool"),
            ("adapter_event", "turn_completed"), ("stream_tally", None)])
        self.assertNotIn("stream", audit[0])
        self.assertEqual(audit[1]["stream"], {"text": 2, "thought": 1, "progress": {"item/started": 1}})
        self.assertEqual(audit[1]["data_preview"], "Read file")
        self.assertEqual(audit[2]["stream"], {"text": 1})
        self.assertEqual(audit[3]["stream"], {"thought": 1})  # what no later record carried

    def test_foreground_run_keeps_a_progress_line_per_event(self):
        log, tail, _audit = self.run_events(detached=False)
        self.assertEqual([kind for kind, _ in event_lines(log)], self.PRINTED)
        self.assertEqual(tail, "")
        with FullSwitch(None), tempfile.TemporaryDirectory() as name:
            sup, _audit = self.supervisor(Path(name), detached=False)
            self.assertEqual(event_lines(self.feed(sup, [("text", ""), ("text", "x")])), [("text", "x")])

    def test_detached_log_gets_counts_before_the_next_lifecycle_line(self):
        log, tail, _audit = self.run_events(detached=True)
        self.assertEqual(event_lines(log), [
            ("turn_started", "t1"), ("stream", "progress=1 text=3 thought=1"), ("turn_completed", "")])
        self.assertEqual(event_lines(tail), [("stream", "thought=1")])

    def test_detached_log_writes_counts_once_the_interval_has_passed(self):
        with FullSwitch(None), tempfile.TemporaryDirectory() as name:
            sup, _audit = self.supervisor(Path(name), detached=True)
            first = self.feed(sup, [("text", "a"), ("text", "b")])
            self.assertEqual(event_lines(first), [])
            sup._log_tally_written -= supervisor_module.STREAM_LOG_INTERVAL_S
            second = self.feed(sup, [("text", "c"), ("text", "d")])
            self.assertEqual(event_lines(second), [("stream", "text=3")])

    def test_the_debug_switch_records_every_event_again(self):
        log, tail, audit = self.run_events(detached=True, full="1")
        self.assertEqual([r.get("kind") for r in audit], [kind for kind, _ in self.EVENTS])
        self.assertTrue(all("stream" not in r for r in audit))
        self.assertEqual([kind for kind, _ in event_lines(log)], self.PRINTED)
        self.assertEqual(tail, "")

    def test_a_run_that_ends_without_a_last_record_still_leaves_its_counts(self):
        with FullSwitch(None), tempfile.TemporaryDirectory() as name:
            sup, audit = self.supervisor(Path(name), detached=True)
            self.feed(sup, [("turn_started", "t1"), ("text", "cut"), ("text", "short")])
            err = io.StringIO()
            with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
                sup._finalize(status="cancelled", exit_code=130)
            tallies = [r for r in records(audit) if r["event"] == "stream_tally"]
            self.assertEqual([r["stream"] for r in tallies], [{"text": 2}])
            self.assertIn(("stream", "text=2"), event_lines(err.getvalue()))

    def test_a_steer_acknowledgement_is_still_reconciled(self):
        with FullSwitch(None), tempfile.TemporaryDirectory() as name:
            sup, audit = self.supervisor(Path(name), detached=True)
            seen = []
            sup._reconcile_steer_ack = seen.append
            self.feed(sup, [("steer_ack", "queued")])
            self.assertEqual(len(seen), 1)
            self.assertEqual([r["kind"] for r in records(audit)], ["steer_ack"])


class RegistryLogTest(RunCase):
    """Real `porch delegate` runs: the registry audit and the detached log."""

    def audit(self, run_id):
        return records(self.run_dir(run_id) / "audit.jsonl")

    def audit_stream_totals(self, audit):
        total = collections.Counter()
        for record in audit:
            total.update(tally_totals(record.get("stream") or {}))
        return total

    def test_audit_counts_every_stream_event_and_records_none(self):
        result = self.delegate("-a", "codex", "TASK")
        self.assertEqual(result.returncode, 0, result.stderr)
        audit = self.audit(self.run_id(result))
        kinds = [r.get("kind") for r in audit if r["event"] == "adapter_event"]
        self.assertFalse(STREAM_KINDS.intersection(kinds), kinds)
        self.assertIn("turn_completed", kinds)
        self.assertEqual(kinds[-1], "done")
        streamed = self.stream_events()
        self.assertGreaterEqual(streamed["progress"], 6)
        self.assertGreater(streamed["text"], 0)
        self.assertEqual(self.audit_stream_totals(audit), streamed)
        labels = collections.Counter()
        for record in audit:
            labels.update((record.get("stream") or {}).get("progress") or {})
        self.assertEqual(labels["turn/diff/updated"], 6)
        # The caller of a foreground run still sees each piece as it arrives.
        self.assertEqual(sum(1 for kind, _ in event_lines(result.stderr) if kind == "progress"), streamed["progress"])
        self.assertNotIn("stream", [kind for kind, _ in event_lines(result.stderr)])

    def detached(self, **env):
        started = self.delegate("-a", "codex", "--detach", "TASK", **env)
        self.assertEqual(started.returncode, 0, started.stderr)
        run_id = started.stdout.strip()
        waited = self.delegate("wait", run_id, "--timeout", "60")
        self.assertEqual(waited.returncode, 0, waited.stderr)
        self.assertIn("FAKE_CODEX_STEER_OK", waited.stdout)
        meta = json.loads((self.run_dir(run_id) / "meta.json").read_text())
        return run_id, Path(meta["detach_log"]).read_text(encoding="utf-8")

    def test_detached_run_log_holds_counts_instead_of_a_line_per_event(self):
        run_id, log = self.detached()
        found = event_lines(log)
        kinds = [kind for kind, _ in found]
        self.assertFalse(STREAM_KINDS.intersection(kinds), kinds)
        self.assertIn("turn_completed", kinds)
        self.assertIn("done", kinds)
        counted = collections.Counter()
        for kind, rest in found:
            if kind == "stream":
                counted.update({k: int(v) for k, v in (part.split("=") for part in rest.split())})
        self.assertEqual(counted, self.stream_events())
        self.assertLess(kinds.index("stream"), kinds.index("turn_completed"))
        self.assertEqual(self.audit_stream_totals(self.audit(run_id)), self.stream_events())

    def test_the_debug_switch_writes_a_record_and_a_line_per_event(self):
        run_id, log = self.detached(PORCH_STREAM_FULL="1")
        streamed = self.stream_events()
        audit = self.audit(run_id)
        self.assertEqual(collections.Counter(r["kind"] for r in audit if r.get("kind") in STREAM_KINDS), streamed)
        self.assertTrue(all("stream" not in r for r in audit))
        kinds = [kind for kind, _ in event_lines(log)]
        self.assertEqual(kinds.count("progress"), streamed["progress"])
        self.assertNotIn("stream", kinds)

    def test_watch_shows_the_turns_and_none_of_the_bookkeeping(self):
        result = self.delegate("-a", "codex", "TASK")
        run_id = self.run_id(result)
        with (self.run_dir(run_id) / "audit.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"event": "stream_tally", "stream": {"text": 3}}) + "\n")
        watched = self.delegate("watch", run_id)
        self.assertEqual(watched.returncode, 0, watched.stderr)
        shown = watched.stdout
        self.assertIn("turn_completed", shown)
        self.assertIn("terminal", shown)
        self.assertNotIn("stream_tally", shown)
        self.assertNotIn("stream=", shown)


if __name__ == "__main__":
    unittest.main()
