"""Offline tests for lib/sessions.py — synthetic fixtures only, no real user data."""
from __future__ import annotations

import json
import contextlib
import io
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
import tracemalloc
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

try:
    import resource
except ImportError:  # pragma: no cover - Windows and constrained runtimes
    resource = None

LIB = Path(__file__).resolve().parents[1] / "lib"
sys.path.insert(0, str(LIB))
import sessions  # noqa: E402


def write_jsonl(path: Path, records: list) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for rec in records:
            if isinstance(rec, str):  # raw line, e.g. malformed
                fh.write(rec + "\n")
            else:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return path


def write_json(path: Path, obj) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    return path


class FixtureMixin:
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="porch sessions ")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)


class JsonlStreamingTests(FixtureMixin, unittest.TestCase):
    def test_large_record_does_not_retain_decoded_chunks_during_parse(self):
        size = 8 * 1024 * 1024
        path = self.root/'large.jsonl'
        with path.open('wb') as out:
            out.write(b'{"text":"')
            for _ in range(size // 65536):
                out.write(b'x' * 65536)
            out.write(b'"}\n')
        tracemalloc.start()
        try:
            rows = list(sessions.iter_jsonl(path, {}))
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertEqual(len(rows[0][1]['text']), size)
        self.assertLess(peak, size * 2.7)

    @staticmethod
    def old_reader(path):
        """Small fixture oracle for the former read/split behavior."""
        stats = {}
        size = path.stat().st_size
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            data = fh.read(size + 1)
        if not data.endswith("\n") and data.strip():
            stats["truncated_tail"] = 1
        rows = []
        for line_no, line in enumerate(data.split("\n"), 1):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                stats["malformed"] = stats.get("malformed", 0) + 1
                continue
            if isinstance(record, dict):
                rows.append((line_no, record))
            else:
                stats["malformed"] = stats.get("malformed", 0) + 1
        return rows, stats

    def test_equivalent_to_read_split_for_jsonl_edges(self):
        path = self.root / "edges.jsonl"
        path.write_bytes((
            '{"text":"first   line","emoji":"😀"}\r\n'
            "\r\nnot-json\n[]\r\n"
            '{"text":"last кириллица 😀"}'
        ).encode("utf-8"))
        expected, expected_stats = self.old_reader(path)
        stats = {}
        actual = list(sessions.iter_jsonl(path, stats))
        self.assertEqual(actual, expected)
        self.assertEqual(stats, expected_stats)
        self.assertEqual(stats, {"malformed": 2, "truncated_tail": 1})

    def test_plain_reader_stops_at_size_observed_when_file_grows(self):
        path = self.root / "growing.jsonl"
        line = b'{"value":"' + b"x" * 40_000 + b'"}\n'
        path.write_bytes(line * 4)
        original_chunks = sessions._bounded_chunks

        def append_after_first_chunk(fh, size, chunk_size=64 * 1024):
            for index, chunk in enumerate(original_chunks(fh, size, chunk_size)):
                yield chunk
                if index == 0:
                    with path.open("ab") as writer:
                        writer.write(b'{"appended":true}\n')

        stats = {}
        with patch.object(sessions, "_bounded_chunks", append_after_first_chunk):
            actual = list(sessions.iter_jsonl(path, stats))
        self.assertEqual(stats.get("changed_during_read"), 1)
        self.assertEqual(len(actual), 4)
        self.assertTrue(all("appended" not in row for _, row in actual))

    def test_compressed_reader_detects_growth(self):
        try:
            from compression import zstd
        except ImportError:
            self.skipTest("stdlib Zstandard is unavailable")
        path = self.root / "growing.jsonl.zst"
        path.write_bytes(zstd.compress(b'{"value":"initial"}\n'))
        original = sessions._LimitedFile.readinto
        changed = False
        def append_after_read(view, target):
            nonlocal changed
            count = original(view, target)
            if count and not changed:
                changed = True
                with path.open('ab') as out:
                    out.write(zstd.compress(b'{"value":"later"}\n'))
            return count
        stats = {}
        with patch.object(sessions._LimitedFile, 'readinto', append_after_read):
            result = list(sessions.iter_jsonl(path, stats))
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0][1]['value'], 'initial')
        self.assertEqual(stats.get('changed_during_read'), 1)

    def test_compressed_jsonl_stream_matches_read_split_when_available(self):
        try:
            from compression import zstd
        except ImportError:
            self.skipTest("stdlib Zstandard is unavailable")
        path = self.root / "edges.jsonl.zst"
        raw = ('{"text":"inside   string 😀"}\r\n\nnope\n{"tail":true}').encode("utf-8")
        path.write_bytes(zstd.compress(raw))
        expected = []
        expected_stats = {}
        with zstd.open(path, "rt", encoding="utf-8", errors="replace") as fh:
            data = fh.read()
        if not data.endswith("\n") and data.strip():
            expected_stats["truncated_tail"] = 1
        for line_no, line in enumerate(data.split("\n"), 1):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                expected_stats["malformed"] = expected_stats.get("malformed", 0) + 1
                continue
            if isinstance(row, dict):
                expected.append((line_no, row))
            else:
                expected_stats["malformed"] = expected_stats.get("malformed", 0) + 1
        stats = {}
        self.assertEqual(list(sessions.iter_jsonl(path, stats)), expected)
        self.assertEqual(stats, expected_stats)

    @unittest.skipUnless(resource is not None, "resource module is unavailable")
    def test_show_peak_memory_stays_below_session_size_multiple(self):
        cli = LIB / "sessions.py"
        path = self.root / "memory.jsonl"
        record = {
            "type": "user", "uuid": "u", "parentUuid": None,
            "sessionId": "memory-test",
            "message": {"role": "user", "content": "кириллица 😀 " + "я😀" * 180},
        }
        line = (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")
        count = (64 * 1024 * 1024) // len(line)
        block = line * max(1, min(count, 256))
        with path.open("wb") as fh:
            remaining = count
            while remaining:
                take = min(remaining, 256)
                fh.write(block[:len(line) * take])
                remaining -= take

        # A wrapper gives this assertion a clean RUSAGE_CHILDREN measurement,
        # unaffected by subprocesses run earlier in the unittest process.
        wrapper = (
            "import os,resource,subprocess,sys,time; "
            "cmd=[sys.executable,sys.argv[1],'show','--file',sys.argv[2],'--limit','40']; "
            "p=subprocess.run(cmd,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE); "
            "peak=resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss; "
            "peak=peak/(1024*1024) if sys.platform=='darwin' else peak/1024; "
            "print(f'{p.returncode} {peak:.1f}'); "
            "sys.exit(0 if p.returncode==0 and peak<256 else 1)"
        )
        proc = subprocess.run(
            [sys.executable, "-c", wrapper, str(cli), str(path)],
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(proc.returncode, 0, f"child peak or CLI failed: {proc.stdout} {proc.stderr}")
        self.assertLess(float(proc.stdout.split()[1]), 256)


def check_prefilter_skips(case, store, refs, patterns):
    """Every session _prefilter_may_match drops has no wanted fragment; returns how many were dropped."""
    dropped = 0
    for pattern, fold, scope in patterns:
        prefilter = sessions.RawPrefilter(pattern, ignore_case=fold, regex=False)
        case.assertTrue(prefilter.active, pattern)
        wanted = lambda f: (sessions.in_scope(f, scope) and isinstance(f.get("text"), str)
                            and ((pattern.lower() in f["text"].lower()) if fold else (pattern in f["text"])))
        verdicts = {}
        for ref in refs:
            truth = any(wanted(f) for f in sessions.READERS[store.harness][1](store, ref, 0, {}))
            kept = sessions._prefilter_may_match(store, ref, prefilter, wanted, verdicts)
            case.assertTrue(kept or not truth, (pattern, scope, ref.session_id))
            dropped += not kept
    return dropped


class ClaudeCodeTests(FixtureMixin, unittest.TestCase):
    def test_origins_stop_reasons_and_worker(self):
        root = self.root / "claude"
        path = root / "project" / "session.jsonl"
        def user(text, origin=None, **extra):
            return {"type": "user", "message": {"content": text}, **({"origin": {"kind": origin}} if origin else {}), **extra}
        def assistant(mid, text, reason=None, block="text"):
            return {"type": "assistant", "message": {"id": mid, "stop_reason": reason, "content": [{"type": block, "text": text}]}}
        delegated = sessions.executor_prefix() + "\nassignment"
        records = [user([{"type": "tool_result", "content": "tool"}, {"type": "text", "text": "quote <system-reminder> inside"}], "human"),
                   assistant("m1", "streamed", None), assistant("m1", "", "end_turn", "tool_use"),
                   user("meta", "human", isMeta=True), user("compact", isCompactSummary=True),
                   user("<command-name>start", "human"), user("notice", "task-notification"),
                   user("peer prompt", "peer"), assistant("m2", "sequence final", "stop_sequence"),
                   user("unknown prompt"), assistant("m3", "tool narration", "tool_use"),
                   user(delegated, "human"), assistant("m4", "legacy fallback"),
                   user("open prompt", "human"), user("[Request interrupted by user]", "human")]
        write_jsonl(path, records)
        store = sessions.Store("claude-code", "fixture", root, "jsonl_dir")
        with patch.object(sessions, "porch_workers", return_value={("claude-code", "session")}):
            ref = next(sessions.claude_iter_sessions(store))
        self.assertEqual((ref.extra["session_class"], ref.extra["class_basis"]), ("headless", "porch registry"))
        fragments = list(sessions.claude_iter_fragments(store, ref, 0, {}))
        self.assertEqual(next(f for f in fragments if f.get("text") == "tool")["kind"], "tool_result")
        by_text = {f.get("text"): f for f in fragments if f.get("text")}
        for text in ("meta", "compact", "<command-name>start", "notice", "[Request interrupted by user]"):
            self.assertEqual(by_text[text]["origin"], "injected", text)
        self.assertEqual(by_text["quote <system-reminder> inside"]["origin"], "human")
        self.assertEqual(by_text["peer prompt"]["origin"], "delegated")
        self.assertEqual(by_text["unknown prompt"]["origin"], "unknown")
        self.assertEqual(by_text[delegated]["origin"], "delegated")
        turns = sessions.interpret_session(store, ref, fragments)
        self.assertEqual([t["final_basis"] for t in turns], ["native:end_turn", "native:stop_sequence", "none", "fallback", "none"])
        self.assertEqual([t["final"]["text"] if t["final"] else None for t in turns], ["streamed", "sequence final", None, "legacy fallback", None])
        self.assertTrue(turns[-1]["open"])
        late = root / "project" / "late.jsonl"
        write_jsonl(late, [{"type": "file-history-snapshot"}] * 300 + [user("late human", "human")])
        with patch.object(sessions, "porch_workers", return_value=set()):
            late_ref = sessions._claude_session_ref(store, late)
        self.assertEqual(late_ref.extra["session_class"], "unknown")
        late_turns = sessions.interpret_session(store, late_ref, sessions.claude_iter_fragments(store, late_ref, 0, {}))
        sessions.refine_class(late_ref, late_turns)
        self.assertEqual(late_ref.extra["session_class"], "interactive")

    def test_classification_and_turn_evidence(self):
        root = self.root / "claude"
        path = root / "project" / "parent" / "subagents" / "agent-a.jsonl"
        write_jsonl(path, [
            {"type": "user", "agentId": "a", "origin": {"kind": "human"}, "message": {"content": [
                {"type": "tool_result", "content": "tool"}, {"type": "text", "text": "quote <system-reminder> here"}]}},
            {"type": "assistant", "message": {"id": "m", "content": [{"type": "text", "text": "answer"}]}},
            {"type": "assistant", "message": {"id": "m", "stop_reason": "end_turn", "content": [{"type": "tool_use", "id": "t"}]}},
            {"type": "user", "isCompactSummary": True, "message": {"content": "summary"}},
            {"type": "user", "origin": {"kind": "peer"}, "message": {"content": "next"}},
            {"type": "user", "message": {"content": "[Request interrupted by user]"}},
        ])
        store = sessions.Store("claude-code", "fixture", root, "jsonl_dir")
        ref = next(sessions.claude_iter_sessions(store))
        fragments = list(sessions.claude_iter_fragments(store, ref, 0, {}))
        turns = sessions.interpret_session(store, ref, fragments)
        self.assertEqual(ref.extra["session_class"], "subagent")
        self.assertEqual(ref.extra["edges"][0]["target_id"], "parent")
        self.assertEqual(ref.extra["edges"][0]["locator"]["agentId"], "a")
        self.assertEqual([t["prompts"][0]["origin"] for t in turns], ["human", "delegated"])
        self.assertEqual(turns[0]["final"]["text"], "answer")
        self.assertEqual(turns[0]["final_basis"], "native:end_turn")
        self.assertTrue(turns[1]["open"])
        self.assertIsNone(turns[1]["final"])

    def make(self):
        proj = self.root / "claude" / "-Users-x-proj"
        sid = "11111111-2222-3333-4444-555555555555"
        write_jsonl(proj / f"{sid}.jsonl", [
            {"type": "summary", "summary": "Fix the flaky test", "timestamp": "2026-01-01T10:00:00Z"},
            {"type": "user", "timestamp": "2026-01-01T10:00:01Z", "cwd": "/Users/x/proj",
             "sessionId": sid, "uuid": "u1", "origin": {"kind": "human"},
             "message": {"role": "user", "content": "why is the test flaky?"}},
            {"type": "assistant", "timestamp": "2026-01-01T10:00:02Z", "sessionId": sid, "uuid": "a1",
             "message": {"role": "assistant", "model": "claude-opus-5", "id": "msg_1",
                         "content": [{"type": "thinking", "thinking": "hmm"},
                                     {"type": "text", "text": "Race in the fixture."},
                                     {"type": "tool_use", "id": "tu1", "name": "Read", "input": {"f": "t.py"}}]}},
            {"type": "user", "timestamp": "2026-01-01T10:00:03Z", "sessionId": sid, "uuid": "u2",
             "message": {"role": "user", "content": [
                 {"type": "tool_result", "tool_use_id": "tu1", "content": "file body"}]}},
            {"type": "user", "timestamp": "2026-01-01T10:00:04Z", "sessionId": sid, "uuid": "u3",
             "isMeta": True,
             "message": {"role": "user", "content": "<command-name>/clear</command-name>"}},
        ])
        write_jsonl(proj / "subagents" / "agent-abc.jsonl", [
            {"type": "user", "timestamp": "2026-01-01T10:01:00Z", "sessionId": "sub-1",
             "isSidechain": True, "agentId": "abc", "promptId": "p1",
             "message": {"role": "user", "content": "subagent task"}},
        ])
        return self.root / "claude"

    def test_sessions_and_fragments(self):
        root = self.make()
        store = sessions.Store("claude-code", ".claude/projects", root, "jsonl_dir")
        refs = list(sessions.claude_iter_sessions(store))
        self.assertEqual(len(refs), 2)
        main = next(r for r in refs if "subagents" not in r.session_id)
        sub = next(r for r in refs if "subagents" in r.session_id)
        self.assertEqual(main.title, "Fix the flaky test")
        self.assertEqual(main.cwd, "/Users/x/proj")
        self.assertEqual(main.models, ["claude-opus-5"])
        self.assertTrue(sub.parent)
        self.assertEqual(sub.agent, "agent-abc")

        stats: dict[str, int] = {}
        out = list(sessions.claude_iter_fragments(store, main, 400, stats))
        by_kind = {}
        for f in out:
            by_kind.setdefault(f["kind"], []).append(f)
        prompts = by_kind["prompt"]
        self.assertEqual(len(prompts), 1)
        self.assertEqual(prompts[0]["authorship"], "human")
        self.assertEqual(prompts[0]["text"], "why is the test flaky?")
        self.assertEqual(by_kind["tool_result"][0]["authorship"], "system")
        self.assertEqual(by_kind["reasoning"][0]["text"], "hmm")
        self.assertEqual(by_kind["tool_call"][0]["rel"]["tool"], "Read")
        # isMeta command record → context, not prompt
        self.assertTrue(any(f["kind"] == "context" for f in out))
        self.assertTrue(all("line" in f["locator"] for f in out))


class HermesTests(FixtureMixin, unittest.TestCase):
    def test_state_db_read_only(self):
        db = self.root / "state.db"
        conn = sqlite3.connect(db)
        conn.executescript("""
            CREATE TABLE sessions (id TEXT, started_at REAL, title TEXT, cwd TEXT,
                model TEXT, parent_session_id TEXT, ended_at REAL, source TEXT, system_prompt TEXT);
            CREATE TABLE messages (id INTEGER, session_id TEXT, role TEXT, content TEXT,
                timestamp REAL, tool_calls TEXT, tool_call_id TEXT, tool_name TEXT,
                reasoning TEXT, reasoning_content TEXT);
            INSERT INTO sessions VALUES ('h1', 1000, 'Example', '/repo', 'model', NULL, 1002, 'cli', 'not exported');
            INSERT INTO messages VALUES (1, 'h1', 'user', 'find this', 1000, NULL, NULL, NULL, NULL, NULL);
            INSERT INTO messages VALUES (2, 'h1', 'assistant', 'answer', 1001, '[{"name":"read"}]', NULL, NULL, 'thinking', NULL);
            INSERT INTO messages VALUES (3, 'h1', 'tool', 'file contents', 1002, NULL, 'c1', 'read', NULL, NULL);
        """)
        conn.commit()
        conn.close()
        store = sessions.Store("hermes", "state.db", db, "sqlite")
        refs = list(sessions.hermes_iter_sessions(store))
        self.assertEqual(refs[0].title, "Example")
        self.assertNotIn("not exported", json.dumps(refs[0].as_dict()))
        fragments = list(sessions.hermes_iter_fragments(store, refs[0], 0, {}))
        self.assertEqual([f["kind"] for f in fragments], ["prompt", "reasoning", "assistant", "tool_call", "tool_result"])
        self.assertNotIn("authorship", fragments[0])  # unknown is omitted
        self.assertEqual(fragments[-1]["locator"]["key"], 3)


class CodexTests(FixtureMixin, unittest.TestCase):
    def test_suffixed_uuid_rollout_groups_and_remains_readable(self):
        home = self.root / 'codex_home'
        sid = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
        suffix = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'
        path = home / 'sessions' / f'rollout-2026-01-01T00-00-00-{sid}_{suffix}.jsonl'
        write_jsonl(path, [{'type':'session_meta','payload':{'id':sid, 'forked_from_id':'parent'}},
                           {'type':'event_msg','payload':{'type':'user_message','message':'fixture prompt'}}])
        store = sessions.Store('codex','sessions',home/'sessions','jsonl_dir')
        with patch.dict(os.environ, {'CODEX_HOME':str(home)}):
            sessions.reset_discovery_caches()
            ref = next(sessions.codex_iter_sessions(store))
            self.assertEqual(ref.session_id, sid)
            self.assertEqual(ref.extra['selected_rollout'], str(path))
            self.assertEqual(sessions._find_codex_rollout(sid), path)
            self.assertEqual(sessions._codex_rollout_for(ref), str(path))
            self.assertTrue(any(f.get('text') == 'fixture prompt' for f in sessions.codex_iter_fragments(store, ref, 0, {})))
            self.assertIn(('fork','parent'), {(e['type'],e['target_id']) for e in ref.extra['edges']})

    def test_discovery_reads_only_bounded_heads(self):
        home = self.root / "codex_home"
        sid = "bounded-thread"
        path = home / "sessions" / f"rollout-2026-01-01T00-00-00-{sid}.jsonl"
        write_jsonl(path, [{"type": "session_meta", "payload": {"id": sid, "source": "vscode"}},
                           {"type": "turn_context", "payload": {"model": "test"}}])
        write_jsonl(home / "sessions" / "rollout-unusual.jsonl",
                    [{"type": "session_meta", "payload": {"id": "unusual"}}])
        db = home / "state_1.sqlite"
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT, source TEXT)")
        conn.execute("INSERT INTO threads VALUES (?, ?, ?)", (sid, str(path), "vscode"))
        conn.commit()
        conn.close()
        for store, count in ((sessions.Store("codex", "sessions", home / "sessions", "jsonl_dir"), 2),
                             (sessions.Store("codex", "state:state_1.sqlite", db, "sqlite"), 1)):
            sessions.reset_discovery_caches()
            with patch.object(sessions, "iter_jsonl", wraps=sessions.iter_jsonl) as reader:
                self.assertEqual(len(list(sessions.codex_iter_sessions(store))), count)
                self.assertTrue(reader.called)
                for call in reader.call_args_list:
                    self.assertIsNotNone(call.kwargs.get("max_lines"), call)
                    self.assertLessEqual(call.kwargs["max_lines"], 300)

    def test_classification_and_turns(self):
        home = self.root / "codex_home"
        sid = "12345678-1111-2222-3333-444444444444"
        parent = "parent-thread"
        old = home / "sessions" / "2026" / "01" / f"rollout-2026-01-01T00-00-00-{sid}.jsonl"
        new = home / "sessions" / "2026" / "02" / f"rollout-2026-02-01T00-00-00-{sid}.jsonl"
        def event(kind, **fields):
            return {"type": "event_msg", "payload": {"type": kind, **fields}}
        def message(role, text, phase=None):
            return {"type": "response_item", "payload": {"type": "message", "role": role, "phase": phase,
                    "content": [{"type": "input_text" if role == "user" else "output_text", "text": text}]}}
        meta = {"type": "session_meta", "payload": {"id": sid,
                "source": {"subagent": {"thread_spawn": {"parent_thread_id": parent}}}, "forked_from_id": "fork-parent"}}
        delegated = sessions.executor_prefix() + "\nAssignment"
        records = [meta,
            event("task_started", turn_id="1"), message("user", "# AGENTS.md instructions\nInjected"),
            message("user", "<environment_context>injected</environment_context>"),
            message("user", "first prompt"), event("user_message", turn_id="1", message="first prompt"),
            event("agent_message", turn_id="1", phase="commentary", message="working"),
            message("assistant", "answer one", "final_answer"), event("task_complete", turn_id="1"),
            event("task_started", turn_id="2"), message("user", "response only"),
            event("agent_message", turn_id="2", message="old unphased text"),
            event("turn_complete", turn_id="2", last_agent_message="saved answer"),
            event("task_started", turn_id="3"), event("user_message", turn_id="3", message=delegated),
            event("agent_message", turn_id="3", phase="commentary", message="not final"),
            message("assistant", "not final"),
            event("task_complete", turn_id="3"),
            event("task_started", turn_id="4"), event("user_message", turn_id="4", message="abort this"),
            event("agent_message", turn_id="4", phase="final_answer", message="discard"),
            event("turn_aborted", turn_id="4", reason="interrupted"),
            event("task_started", turn_id="5"), event("user_message", turn_id="5", message="another"),
            event("agent_message", turn_id="5", message="unphased"),
            event("task_complete", turn_id="5", last_agent_message="task answer")]
        write_jsonl(old, records)
        write_jsonl(new, [meta, event("user_message", message="new revision")])
        os.utime(old, (100, 100))
        os.utime(new, (200, 200))
        db = home / "state_5.sqlite"
        conn = sqlite3.connect(db)
        conn.executescript("CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT, source TEXT);"
                           "CREATE TABLE thread_spawn_edges (parent_thread_id TEXT, child_thread_id TEXT);")
        conn.execute("INSERT INTO threads VALUES (?, ?, ?)", (sid, str(old), "cli"))
        conn.execute("INSERT INTO thread_spawn_edges VALUES (?, ?)", ("state-parent", sid))
        conn.commit()
        conn.close()
        store = sessions.Store("codex", "sessions", home / "sessions", "jsonl_dir")
        refs = list(sessions.codex_iter_sessions(store))
        self.assertEqual(len(refs), 2)
        ref = next(r for r in refs if r.locator["file"] == str(old))
        self.assertEqual(ref.locator["file"], str(old))
        self.assertTrue(all(r.extra["selected_rollout"] == str(old) for r in refs))
        self.assertEqual(sessions._codex_rollout_for(next(r for r in refs if r.locator["file"] == str(new))), str(new))
        self.assertEqual(ref.extra["native_id"], sid)
        self.assertEqual(ref.extra["session_class"], "subagent")
        self.assertEqual({(e["type"], e["target_id"]) for e in ref.extra["edges"]},
                         {("subagent", parent), ("subagent", "state-parent"), ("fork", "fork-parent")})
        status = ref.extra["native_status"]
        self.assertTrue({"turn_aborted", "turn_aborted_last_reason", "task_complete"}.isdisjoint(status))
        self.assertEqual(status["alternates"], [str(new)])
        self.assertNotIn("revision", status)
        fragments = list(sessions.codex_iter_fragments(store, ref, 0, {}))
        injected = [f for f in fragments if f.get("origin") == "injected"]
        self.assertEqual(len(injected), 2)
        turns = sessions.interpret_session(store, ref, fragments)
        self.assertEqual(len(turns), 5)
        self.assertEqual([t["prompts"][0]["text"] for t in turns], ["first prompt", "response only", delegated, "abort this", "another"])
        self.assertEqual([t["prompts"][0]["origin"] for t in turns], ["human", "unknown", "delegated", "human", "human"])
        self.assertEqual([t["final"]["text"] if t["final"] else None for t in turns], ["answer one", "saved answer", None, "discard", "task answer"])
        self.assertEqual([t["final_basis"] for t in turns], ["native:final_answer", "native:turn_complete", "none", "native:final_answer", "native:task_complete"])
        self.assertFalse(turns[3]["open"])
        self.assertEqual([t["aborted"] for t in turns], [False, False, False, True, False])
        self.assertEqual(turns[3]["abort_reason"], "interrupted")
        self.assertNotIn("abort_reason", turns[0])
        self.assertEqual(turns[0]["final"]["native"], {"phase": "final_answer", "turn_id": None, "layer": "response"})
        state_store = sessions.Store("codex", "state:state_5.sqlite", db, "sqlite")
        state_ref = next(sessions.codex_iter_sessions(state_store))
        self.assertEqual(sessions._codex_rollout_for(state_ref), str(old))
        self.assertTrue({"turn_aborted", "turn_aborted_last_reason", "task_complete"}.isdisjoint(state_ref.extra["native_status"]))
        self.assertTrue(any(e["basis"] == "thread_spawn_edges" for e in state_ref.extra["edges"]))
        conn = sqlite3.connect(db)
        conn.execute("UPDATE threads SET rollout_path = NULL WHERE id = ?", (sid,))
        conn.commit()
        conn.close()
        sessions.reset_discovery_caches()
        refs = list(sessions.codex_iter_sessions(store))
        self.assertTrue(all(r.extra["selected_rollout"] == str(new) for r in refs))
        ref = next(r for r in refs if r.locator["file"] == str(new))
        self.assertEqual(ref.extra["native_status"]["revision"], "ambiguous")
        self.assertEqual(ref.extra["native_status"]["alternates"], [str(old)])
        state_ref = next(sessions.codex_iter_sessions(state_store))
        self.assertEqual(sessions._codex_rollout_for(state_ref), str(new))
        self.assertEqual(state_ref.extra["native_status"]["revision"], "ambiguous")
        worker = sessions.SessionRef("codex", "fixture", "worker", locator={"file": str(new)})
        with patch.object(sessions, "porch_workers", return_value={("codex", "worker")}):
            sessions._codex_enrich_ref(worker)
        self.assertEqual(worker.extra["session_class"], "headless")
        self.assertEqual(worker.extra["class_basis"], "porch registry")
        delegated_path = self.root / "delegated" / "rollout-2026-01-01T00-00-00-delegated.jsonl"
        write_jsonl(delegated_path, [{"type": "session_meta", "payload": {"id": "delegated", "source": "cli"}},
                                     event("user_message", message=delegated)])
        delegated_store = sessions.Store("codex", "fixture", delegated_path.parent, "jsonl_dir")
        delegated_ref = next(sessions.codex_iter_sessions(delegated_store))
        self.assertEqual(delegated_ref.extra["session_class"], "headless")
        self.assertEqual(delegated_ref.extra["class_basis"], "executor-prefix")
        for originator, expected in (("Codex Desktop", "interactive"), ("codex_work_desktop", "interactive"),
                                     ("codex-tui", "interactive"), ("codex_exec", "unknown"),
                                     ("pragmatic-orchestration-steer", "unknown"), ("unrecognized", "unknown")):
            client = sessions.SessionRef("codex", "fixture", "other", locator={})
            sessions._codex_enrich_ref(client, meta={"source": "vscode", "originator": originator})
            self.assertEqual(client.extra["session_class"], expected, originator)

    def make(self):
        sid = "99999999-8888-7777-6666-555555555555"
        path = self.root / "codex" / "2026" / "01" / "02" / f"rollout-2026-01-02T00-00-00-{sid}.jsonl"
        write_jsonl(path, [
            {"timestamp": "2026-01-02T00:00:00Z", "type": "session_meta",
             "payload": {"id": sid, "cwd": "/work/repo", "originator": "Codex Desktop",
                         "cli_version": "0.1", "model_provider": "openai"}},
            {"timestamp": "2026-01-02T00:00:01Z", "type": "turn_context",
             "payload": {"turn_id": "t1", "model": "gpt-6-astra", "effort": "high"}},
            {"timestamp": "2026-01-02T00:00:02Z", "type": "response_item",
             "payload": {"type": "message", "role": "user", "id": "i1",
                         "content": [{"type": "input_text", "text": "<environment_context>cwd=/work/repo</environment_context>"}]}},
            {"timestamp": "2026-01-02T00:00:03Z", "type": "event_msg",
             "payload": {"type": "user_message", "turn_id": "t1", "message": "add a retry loop"}},
            {"timestamp": "2026-01-02T00:00:04Z", "type": "response_item",
             "payload": {"type": "message", "role": "user", "id": "i2",
                         "content": [{"type": "input_text", "text": "add a retry loop"}]}},
            {"timestamp": "2026-01-02T00:00:05Z", "type": "response_item",
             "payload": {"type": "reasoning", "id": "r1", "summary": [{"text": "planning"}]}},
            {"timestamp": "2026-01-02T00:00:06Z", "type": "response_item",
             "payload": {"type": "function_call", "name": "shell", "call_id": "c1", "arguments": "{}"}},
            {"timestamp": "2026-01-02T00:00:07Z", "type": "response_item",
             "payload": {"type": "function_call_output", "call_id": "c1", "output": "ok"}},
            {"timestamp": "2026-01-02T00:00:08Z", "type": "event_msg",
             "payload": {"type": "agent_message", "turn_id": "t1", "message": "Done.", "phase": "final"}},
            '{"timestamp": "2026-01-02T00:00:09Z", "type": "response_item", GARBAGE-BINARY-\x00\x01',
            {"timestamp": "2026-01-02T00:00:10Z", "type": "event_msg",
             "payload": {"type": "token_count", "info": {"total": 5}}},
        ])
        return self.root / "codex", sid

    def test_classification(self):
        root, sid = self.make()
        store = sessions.Store("codex", "sessions", root, "jsonl_dir")
        refs = list(sessions.codex_iter_sessions(store))
        self.assertEqual(len(refs), 1)
        ref = refs[0]
        self.assertEqual(ref.session_id, sid)
        self.assertEqual(ref.cwd, "/work/repo")
        self.assertIn("gpt-6-astra", ref.models)

        stats: dict[str, int] = {}
        out = list(sessions.codex_iter_fragments(store, ref, 400, stats))
        prompts = [f for f in out if f["kind"] == "prompt"]
        ctx = [f for f in out if f["kind"] == "context"]
        # event_msg.user_message is the authoritative prompt
        self.assertTrue(any(f["native_type"] == "event_msg.user_message" and f["text"] == "add a retry loop"
                            for f in prompts))
        # mirrored response_item user record upgrades to prompt
        self.assertTrue(any(f["native_type"] == "response_item.user" for f in prompts))
        # bootstrap environment context must NOT be a prompt
        self.assertTrue(any("environment_context" in (f.get("text") or "") for f in ctx))
        self.assertTrue(any(f["kind"] == "tool_call" for f in out))
        self.assertTrue(any(f["kind"] == "tool_result" for f in out))
        self.assertTrue(any(f["kind"] == "reasoning" for f in out))
        # malformed interior line counted, not dropped silently
        self.assertEqual(stats.get("malformed"), 1)

    @unittest.skipUnless(sys.version_info >= (3, 14), "stdlib Zstandard requires Python 3.14")
    def test_compressed_rollout_and_corruption(self):
        from compression import zstd
        root, sid = self.make()
        path = next(root.rglob("*.jsonl"))
        compressed = path.with_name(path.name + ".zst")
        compressed.write_bytes(zstd.compress(path.read_bytes()))
        path.unlink()
        store = sessions.Store("codex", "sessions", root, "jsonl_dir")
        refs = list(sessions.codex_iter_sessions(store))
        self.assertEqual([r.session_id for r in refs], [sid])
        stats = {}
        fragments = list(sessions.codex_iter_fragments(store, refs[0], 0, stats))
        self.assertTrue(any(f.get("text") == "add a retry loop" for f in fragments))
        self.assertEqual(fragments[0]["locator"]["file"], str(compressed))
        compressed.write_bytes(b"not a zstd stream")
        self.assertEqual(list(sessions.codex_iter_fragments(store, refs[0], 0, stats)), [])
        self.assertEqual(stats["compressed_errors"], 1)
        self.assertFalse(sessions.coverage_summary([store], 1, 0, stats)["scan_complete"])

    def test_missing_zstd_decoder_is_not_silent(self):
        path = self.root / "rollout-test.jsonl.zst"
        path.write_bytes(b"compressed fixture")
        stats = {}
        with patch.dict(sys.modules, {"compression": None}), contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(list(sessions.iter_jsonl(path, stats)), [])
        self.assertEqual(stats["compressed_errors"], 1)
        self.assertIn("requires Python 3.14", err.getvalue())


class OpenCodeDbTests(FixtureMixin, unittest.TestCase):
    def test_porch_worker_class_and_subagent_precedence(self):
        db = self.root / "opencode.db"
        conn = sqlite3.connect(db)
        conn.executescript("CREATE TABLE session_v2 (id TEXT, parent_id TEXT); CREATE TABLE session_message (session_id TEXT, seq INTEGER, data TEXT, type TEXT);")
        conn.executemany("INSERT INTO session_v2 VALUES (?,?)", [("worker", None), ("child", "parent")])
        conn.commit()
        conn.close()
        store = sessions.Store("opencode", "db", db, "sqlite")
        with patch.object(sessions, "porch_workers", return_value={("opencode", "worker"), ("opencode", "child")}):
            refs = {ref.session_id: ref for ref in sessions.opencode_iter_sessions(store)}
        self.assertEqual((refs["worker"].extra["session_class"], refs["worker"].extra["class_basis"]), ("headless", "porch registry"))
        self.assertEqual(refs["child"].extra["session_class"], "subagent")

    def make(self):
        db = self.root / "opencode.db"
        sqlite3.connect(db).close()
        return db

    def test_store_discovery_follows_opencode_database_configuration(self):
        data = self.root / "data" / "opencode"
        data.mkdir(parents=True)
        (data / "opencode-channel.db").touch()
        with patch.dict(os.environ, {"XDG_DATA_HOME": str(data.parent)}, clear=True):
            stores = sessions.stores_for("opencode")
        self.assertEqual([s.path for s in stores], [data / "opencode.db", data / "opencode-channel.db"])
        with patch.dict(os.environ, {"XDG_DATA_HOME": str(data.parent), "OPENCODE_DB": "nested/custom"}, clear=True):
            self.assertEqual(sessions.stores_for("opencode")[0].path, data / "nested/custom")
        with patch.dict(os.environ, {"XDG_DATA_HOME": str(data.parent), "OPENCODE_DB": ":memory:"}, clear=True):
            self.assertEqual(sessions.stores_for("opencode"), [])

    def test_v2_sessions_and_parts(self):
        db = self.make()
        conn = sqlite3.connect(db)
        conn.executescript("""
            CREATE TABLE session_v2 (id TEXT, directory TEXT, title TEXT,
                time_created INTEGER, time_updated INTEGER, parent_id TEXT,
                agent TEXT, model TEXT, version TEXT, time_archived INTEGER);
            CREATE TABLE session_message (id TEXT, session_id TEXT, type TEXT,
                seq INTEGER, time_created INTEGER, time_updated INTEGER, data TEXT);
            CREATE TABLE session_part (message_id TEXT, session_id TEXT, ordinal INTEGER,
                type TEXT, call_id TEXT, time_created INTEGER, time_updated INTEGER, data TEXT);
        """)
        conn.execute("INSERT INTO session_v2 VALUES (?,?,?,?,?,?,?,?,?,?)",
                     ("ses_1", "/work/repo", "Lossy copy", 1000, 2000, None, None, None, "2", None))
        conn.execute("INSERT INTO session_v2 VALUES (?,?,?,?,?,?,?,?,?,?)",
                     ("ses_v2", "/work/new", "New", 3000, 4000, None, "build", '{"id":"model"}', "2", None))
        rows = [
            ("u", "user", {"text": "new prompt"}),
            ("a", "assistant", {"content": [{"type": "reasoning", "text": "thinking"},
                {"type": "text", "text": "new answer"}, {"type": "tool", "name": "read",
                 "id": "c", "state": {"input": {"path": "x"}, "content": [{"type": "text", "text": "file"}]}}]}),
            ("sh", "shell", {"command": "pwd", "output": {"output": "/work/new"}}),
        ]
        for index, (mid, kind, data) in enumerate(rows):
            conn.execute("INSERT INTO session_message VALUES (?,?,?,?,?,?,?)",
                         (mid, "ses_v2", kind, index, 3000 + index, 3000 + index, json.dumps(data)))
        conn.execute("INSERT INTO session_part VALUES (?,?,?,?,?,?,?,?)",
                     ("a", "ses_v2", 0, "text", None, 3001, 3001,
                      json.dumps({"text": "row answer"})))
        conn.commit()
        conn.close()
        store = sessions.Store("opencode", "db", db, "sqlite")
        refs = {r.session_id: r for r in sessions.opencode_iter_sessions(store)}
        self.assertEqual(refs["ses_1"].locator["table"], "session_v2")
        self.assertEqual(refs["ses_v2"].locator["table"], "session_v2")
        self.assertEqual(sessions.fast_resolve(store, "ses_v2").locator["table"], "session_v2")
        fragments = list(sessions.opencode_iter_fragments(store, refs["ses_v2"], 0, {}))
        self.assertTrue(any(f["kind"] == "prompt" and f.get("text") == "new prompt" for f in fragments))
        self.assertTrue(any(f["kind"] == "assistant" and f.get("text") == "new answer" for f in fragments))
        self.assertEqual([f["text"] for f in fragments if f["kind"] == "assistant"],
                         ["row answer", "new answer"])
        self.assertTrue(any(f["kind"] == "tool_result" and "file" in f.get("text", "") for f in fragments))
        self.assertTrue(any(f["kind"] == "tool_call" and f.get("text") == "pwd" for f in fragments))
        channel = sessions.Store("opencode", "db:opencode-channel.db", db, "sqlite")
        channel_ref = next(r for r in sessions.opencode_iter_sessions(channel) if r.session_id == "ses_v2")
        self.assertEqual(channel_ref.store, channel.name)
        self.assertTrue(all(f["store"] == channel.name for f in sessions.opencode_iter_fragments(channel, channel_ref, 0, {})))
        conn = sqlite3.connect(db)
        conn.execute("UPDATE session_v2 SET time_updated=2500 WHERE id='ses_1'")
        conn.commit()
        conn.close()
        refs = {r.session_id: r for r in sessions.opencode_iter_sessions(store)}
        self.assertEqual(refs["ses_1"].locator["table"], "session_v2")



class GrokTests(FixtureMixin, unittest.TestCase):
    def make(self):
        sess = self.root / "grok" / "%2Fwork%2Frepo" / "sess-1"
        sess.mkdir(parents=True)
        write_json(sess / "summary.json",
                   {"info": {"id": "sess-1", "cwd": "/work/repo"},
                    "generated_title": "Investigate cache", "current_model_id": "grok-4.6",
                    "created_at": "2026-01-03T00:00:00Z", "updated_at": "2026-01-03T01:00:00Z",
                    "reasoning_effort": "high", "num_messages": 4})
        write_jsonl(sess / "chat_history.jsonl", [
            {"type": "system", "content": "you are grok"},
            {"type": "user", "content": [{"type": "text", "text": "<system-reminder>rules…</system-reminder>"}]},
            {"type": "user", "content": [{"type": "text", "text": "find the cache bug"}]},
            {"type": "reasoning", "summary": [{"text": "thinking…"}], "id": "r1"},
            {"type": "assistant", "content": "The bug is in invalidation.",
             "tool_calls": [{"id": "c1", "name": "view", "arguments": "{}"}]},
            {"type": "tool_result", "tool_call_id": "c1", "content": "file…"},
        ])
        write_jsonl(self.root / "grok" / "%2Fwork%2Frepo" / "prompt_history.jsonl", [
            {"session_id": "sess-1", "prompt": "find the cache bug"},
        ])
        return self.root / "grok"

    def test_grok(self):
        root = self.make()
        store = sessions.Store("grok", "sessions", root, "session_dirs")
        refs = list(sessions.grok_iter_sessions(store))
        self.assertEqual(len(refs), 1)
        self.assertEqual(refs[0].title, "Investigate cache")
        self.assertEqual(refs[0].models, ["grok-4.6"])

        stats: dict[str, int] = {}
        out = list(sessions.grok_iter_fragments(store, refs[0], 400, stats))
        prompts = [f for f in out if f["kind"] == "prompt"]
        ctx = [f for f in out if f["kind"] == "context"]
        self.assertEqual(len(prompts), 1)
        self.assertEqual(prompts[0]["authorship"], "human")  # prompt_history match
        self.assertTrue(any("system-reminder" in (f.get("text") or "") for f in ctx))
        self.assertTrue(any(f["kind"] == "reasoning" for f in out))
        self.assertTrue(any(f["kind"] == "tool_call" for f in out))
        self.assertTrue(any(f["kind"] == "tool_result" for f in out))


class DevinTests(FixtureMixin, unittest.TestCase):
    def test_classification_and_turns(self):
        db = self.root / "sessions.db"
        conn = sqlite3.connect(db)
        conn.executescript("""
            CREATE TABLE sessions (id TEXT PRIMARY KEY, working_directory TEXT, model TEXT,
                agent_mode TEXT, created_at TEXT, last_activity_at TEXT, title TEXT, hidden INTEGER, main_chain_id INTEGER);
            CREATE TABLE message_nodes (row_id INTEGER PRIMARY KEY, session_id TEXT, node_id INTEGER,
                parent_node_id INTEGER, chat_message TEXT, created_at TEXT, metadata TEXT);
        """)
        for sid, tip in (("forest", 13), ("missing", None), ("bad-tip", 99), ("worker", None)):
            conn.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?)", (sid, "/repo", "model", "bypass", 1, 2, sid, 0, tip))
        delegated = sessions.executor_prefix() + "\nassignment"
        def add(sid, node, parent, role, content, second, *, message_id=None, user=False, finish=None, calls=False, node_meta=None):
            msg = {"role": role, "content": content, "message_id": message_id,
                   "metadata": {"created_at": f"2026-01-01T00:00:{second:02d}Z", "finish_reason": finish}}
            if user:
                msg["metadata"]["is_user_input"] = 1
            if calls:
                msg["tool_calls"] = [{"id": "call", "name": "read"}]
            conn.execute("INSERT INTO message_nodes (session_id,node_id,parent_node_id,chat_message,created_at,metadata) VALUES (?,?,?,?,?,?)",
                         (sid, node, parent, json.dumps(msg), "2035-01-01T00:00:00Z", json.dumps(node_meta or {})))
        add("forest", 1, None, "user", "first", 1, message_id="first", user=True)
        add("forest", 2, 1, "assistant", "first answer", 2, finish="stop")
        add("forest", 3, 2, "user", "injected", 3)
        add("forest", 4, None, "system", "summary", 3, node_meta={"summarized_from": 3, "extensions": {"compact/prior_node_ids": [2]}})
        add("forest", 5, 4, "user", "latest copy", 5, message_id="dup", user=True)
        add("forest", 6, 5, "user", "older copy", 4, message_id="dup", user=True)
        add("forest", 7, 6, "assistant", "tool commentary", 6, finish="tool_calls", calls=True)
        add("forest", 8, 7, "user", delegated, 7, message_id="delegated", user=True)
        add("forest", 9, 8, "assistant", "unmarked fallback", 8)
        add("forest", 10, 9, "user", "third", 9, user=True)
        add("forest", 11, 10, "assistant", "third answer", 10, finish="stop")
        add("forest", 12, 11, "user", "open", 11, user=True)
        add("forest", 13, 12, "assistant", "", 12, finish="tool_calls", calls=True)
        add("forest", 14, 1, "user", "dead branch", 0, user=True)
        for sid in ("missing", "bad-tip"):
            add(sid, 1, None, "user", "recover me", 1, user=True)
            add(sid, 2, 1, "assistant", "recovered answer", 2, finish="stop")
        conn.commit()
        conn.close()
        store = sessions.Store("devin", "sessions.db", db, "sqlite")
        with patch.object(sessions, "porch_workers", return_value={("devin", "worker")}):
            refs = {ref.session_id: ref for ref in sessions.devin_iter_sessions(store)}
        self.assertEqual(refs["worker"].extra["session_class"], "headless")
        self.assertEqual(refs["worker"].extra["class_basis"], "porch registry")
        ref = refs["forest"]
        self.assertEqual(ref.extra["session_class"], "unknown")
        self.assertEqual(ref.extra["edges"], [])
        self.assertEqual([s["table"] for s in ref.extra["sources"]], ["sessions", "message_nodes"])
        fragments = list(sessions.devin_iter_fragments(store, ref, 0, {}))
        injected = next(f for f in fragments if f.get("text") == "injected")
        self.assertEqual(injected["origin"], "injected")
        self.assertEqual(next(f for f in fragments if f.get("text") == "first")["ts"], "2026-01-01T00:00:01Z")
        turns = sessions.interpret_session(store, ref, fragments)
        self.assertEqual([t["prompts"][0]["text"] for t in turns], ["first", "latest copy", delegated, "third", "open"])
        self.assertEqual([t["final"]["text"] if t["final"] else None for t in turns], ["first answer", None, "unmarked fallback", "third answer", None])
        self.assertEqual([t["final_basis"] for t in turns], ["native:stop", "none", "fallback", "native:stop", "none"])
        self.assertTrue(turns[-1]["open"])
        self.assertEqual(turns[2]["prompts"][0]["origin"], "delegated")
        sessions.refine_class(ref, turns)
        self.assertEqual((ref.extra["session_class"], ref.extra["class_basis"]), ("interactive", "genuine prompt"))
        for sid in ("missing", "bad-tip"):
            ref = refs[sid]
            fragments = list(sessions.devin_iter_fragments(store, ref, 0, {}))
            self.assertTrue(all(f["native"]["canonical"] is None for f in fragments))
            recovered = sessions.interpret_session(store, ref, fragments)
            self.assertEqual(recovered[0]["final"]["text"], "recovered answer")
            self.assertEqual(recovered[0]["final_basis"], "fallback")

    def make(self):
        base = self.root / "devin"
        base.mkdir()
        db = base / "sessions.db"
        conn = sqlite3.connect(db)
        conn.executescript("""
            CREATE TABLE sessions (id TEXT PRIMARY KEY, working_directory TEXT, model TEXT,
                agent_mode TEXT, created_at TEXT, last_activity_at TEXT, title TEXT,
                hidden INTEGER, main_chain_id INTEGER);
            CREATE TABLE message_nodes (row_id INTEGER PRIMARY KEY, session_id TEXT,
                node_id INTEGER, parent_node_id INTEGER, chat_message TEXT, created_at TEXT);
            CREATE TABLE subagent_heads (session_id TEXT, head TEXT);
        """)
        conn.execute("INSERT INTO sessions VALUES ('test-sess', '/work/repo', 'swe-2-high', "
                     "'bypass', '2026-01-04T00:00:00Z', '2026-01-04T01:00:00Z', 'Test title', 0, 7)")
        for row_id, node, parent, msg, ts in [
            (1, 1, None, {"role": "system", "content": "sys"}, "2026-01-04T00:00:01Z"),
            (2, 2, 1, {"role": "user", "content": "do the thing", "message_id": "m1"}, "2026-01-04T00:00:02Z"),
            (3, 3, 2, {"role": "assistant", "content": "Done", "message_id": "m2",
                       "thinking": {"thinking": "hmm"},
                       "tool_calls": [{"id": "c1", "name": "run", "arguments": "{}"}],
                       "metadata": {"finish_reason": "stop"}}, "2026-01-04T00:00:03Z"),
            (4, 4, 3, {"role": "tool", "content": "output", "message_id": "m3"}, "2026-01-04T00:00:04Z"),
        ]:
            conn.execute("INSERT INTO message_nodes VALUES (?, 'test-sess', ?, ?, ?, ?)",
                         (row_id, node, parent, json.dumps(msg), ts))
        conn.execute("INSERT INTO subagent_heads VALUES ('test-sess', 'head-1')")
        conn.commit()
        conn.close()
        write_json(base / "transcripts" / "test-sess.json",
                   {"session_id": "test-sess", "agent": {"model_name": "swe-2-high"},
                    "steps": [{"step_id": "s1", "source": "user", "message": "hi",
                               "timestamp": "2026-01-04T00:00:02Z"},
                              {"step_id": "s2", "source": "agent", "message": "hello",
                               "timestamp": "2026-01-04T00:00:03Z"}]})
        return base

    def test_db_and_transcripts(self):
        base = self.make()
        store = sessions.Store("devin", "sessions.db", base / "sessions.db", "sqlite")
        refs = list(sessions.devin_iter_sessions(store))
        self.assertEqual(refs[0].title, "Test title")
        self.assertEqual(refs[0].extra["agent_mode"], "bypass")

        stats: dict[str, int] = {}
        out = list(sessions.devin_iter_fragments(store, refs[0], 400, stats))
        kinds = {f["kind"] for f in out}
        self.assertIn("prompt", kinds)
        self.assertIn("assistant", kinds)
        self.assertIn("reasoning", kinds)
        self.assertIn("tool_call", kinds)
        self.assertIn("tool_result", kinds)
        self.assertIn("context", kinds)
        self.assertTrue(any(f["native_type"] == "subagent_heads" for f in out))
        node_rel = next(f for f in out if f["kind"] == "prompt")["rel"]
        self.assertEqual(node_rel["node_id"], 2)
        self.assertEqual(node_rel["parent_node_id"], 1)

        tstore = sessions.Store("devin", "transcripts", base / "transcripts", "json_dir")
        trefs = list(sessions.devin_iter_sessions(tstore))
        self.assertEqual(trefs[0].models, ["swe-2-high"])
        tout = list(sessions.devin_iter_fragments(tstore, trefs[0], 400, {}))
        self.assertEqual([f["kind"] for f in tout], ["prompt", "assistant"])


class GeminiTests(FixtureMixin, unittest.TestCase):
    def test_chat(self):
        base = self.root / "gemini"
        write_json(base / "projects.json", {"projects": {"/work/repo": "abc123"}})
        write_json(base / "tmp" / "abc123" / "chats" / "session-2026-01-05T00-00-deadbeef.json", {
            "sessionId": "deadbeef-1234", "startTime": "2026-01-05T00:00:00Z",
            "lastUpdated": "2026-01-05T00:10:00Z",
            "messages": [
                {"type": "user", "id": "m1", "content": "explain auth", "timestamp": "2026-01-05T00:00:01Z"},
                {"type": "gemini", "id": "m2", "content": "Auth works via…", "model": "gemini-3",
                 "timestamp": "2026-01-05T00:00:02Z",
                 "toolCalls": [{"name": "read", "args": {}}]},
                {"type": "info", "id": "m3", "content": "checkpoint", "timestamp": "2026-01-05T00:00:03Z"},
            ]})
        store = sessions.Store("gemini", "chats", base / "tmp", "json_dir")
        refs = list(sessions.gemini_iter_sessions(store))
        self.assertEqual(refs[0].cwd, "/work/repo")  # resolved via projects.json
        stats: dict[str, int] = {}
        out = list(sessions.gemini_iter_fragments(store, refs[0], 400, stats))
        kinds = [f["kind"] for f in out]
        self.assertEqual(kinds, ["prompt", "assistant", "tool_call", "metadata"])


class CursorTests(FixtureMixin, unittest.TestCase):
    def test_vscdb(self):
        wsdir = self.root / "ws" / "deadbeef"
        wsdir.mkdir(parents=True)
        write_json(wsdir / "workspace.json", {"folder": "file:///work/repo"})
        db = wsdir / "state.vscdb"
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE cursorDiskKV (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute("INSERT INTO cursorDiskKV VALUES ('composerData:cid1', ?)",
                     (json.dumps({"name": "Cursor chat", "createdAt": "2026-01-06T00:00:00Z",
                                  "lastUpdatedAt": "2026-01-06T01:00:00Z",
                                  "fullConversationHeadersOnly": [{"bubbleId": "b1", "type": 1},
                                                                  {"bubbleId": "b2", "type": 2}]}),))
        conn.execute("INSERT INTO cursorDiskKV VALUES ('bubbleId:cid1:b1', ?)",
                     (json.dumps({"text": "cursor question"}),))
        conn.execute("INSERT INTO cursorDiskKV VALUES ('bubbleId:cid1:b2', ?)",
                     (json.dumps({"text": "cursor answer"}),))
        conn.commit()
        conn.close()
        store = sessions.Store("cursor", "workspace:deadbeef", db, "sqlite")
        refs = list(sessions.cursor_iter_sessions(store))
        self.assertEqual(refs[0].title, "Cursor chat")
        self.assertEqual(refs[0].cwd, "/work/repo")  # workspace.json fallback
        stats: dict[str, int] = {}
        out = list(sessions.cursor_iter_fragments(store, refs[0], 400, stats))
        self.assertEqual(out[0]["kind"], "prompt")
        self.assertEqual(out[0]["authorship"], "human")
        self.assertEqual(out[1]["kind"], "assistant")


class CursorACPTests(FixtureMixin, unittest.TestCase):
    """ACP sessions share the chats graph: JSON message blobs on root field 1,
    turn blobs on field 8 wrapping {1: user step, 2: agent steps}."""

    @staticmethod
    def _varint(value: int) -> bytes:
        out = bytearray()
        while value > 127:
            out.append(value & 127 | 128)
            value >>= 7
        out.append(value)
        return bytes(out)

    def _field(self, number: int, data: bytes) -> bytes:
        self.assertLess(number, 16)
        return self._varint(number * 8 + 2) + self._varint(len(data)) + data

    def _write_store(self, folder: Path, sid: str, root_id: bytes, nodes: dict,
                     name: str = "ACP") -> Path:
        folder.mkdir(parents=True, exist_ok=True)
        db = folder / "store.db"
        conn = sqlite3.connect(db)
        conn.executescript(
            "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);"
            "CREATE TABLE blobs (id TEXT PRIMARY KEY, data BLOB);")
        meta = {"agentId": sid, "latestRootBlobId": root_id.hex(), "name": name,
                    "createdAt": 1795101019967}
        conn.execute("INSERT INTO meta VALUES ('0', ?)", (json.dumps(meta).encode().hex(),))
        conn.executemany("INSERT INTO blobs VALUES (?, ?)",
                         [(key.hex(), value) for key, value in nodes.items()])
        conn.commit()
        conn.close()
        return db

    def test_acp_graph_prompt_tools_and_step_rescue(self):
        sid = "11111111-2222-3333-4444-555555555555"
        root = self.root / "acp-sessions"
        field = self._field
        ids = [bytes([n]) * 32 for n in range(1, 17)]
        (root_id, system_id, info_id, query_id, answer_id, result_id,
         query2_id, answer2_id, turn0, turn1,
         ustep0, ustep1, rstep, sstep, sstep2, tstep) = ids
        call_id = "call-abc-0\nfc_def_0"
        system = b'{"role":"system","content":"bootstrap"}'
        info = b'{"role":"user","content":"<user_info>hidden</user_info>"}'
        query = json.dumps({"role": "user", "content": [
            {"type": "text", "text": "<timestamp>t</timestamp>\n<user_query>\nrun ls\n</user_query>"}
        ]}).encode()
        answer = json.dumps({"role": "assistant", "content": [
            {"type": "reasoning", "text": ""},
            {"type": "text", "text": "running ls"},
            {"type": "tool-call", "toolCallId": call_id, "toolName": "Shell",
             "args": {"command": "ls"}}]}).encode()
        result = json.dumps({"role": "tool", "content": [
            {"type": "tool-result", "toolCallId": call_id, "toolName": "Shell",
             "result": "x.txt\n"}]}).encode()
        # Second turn's prompt is recorded without <user_query>: the turn user
        # step carries the bare text and must rescue it from wrapper context.
        query2 = json.dumps({"role": "user", "content":
            "<timestamp>t2</timestamp>\nReply with exactly: PLAIN"}).encode()
        answer2 = (b'{"role":"assistant","content":[{"type":"reasoning","text":""},'
                   b'{"type":"text","text":"PLAIN"}]}')
        # Field 57's wire key (57<<3|2 = 458) needs a two-byte varint.
        tool_record = bytes([458 & 127 | 128, 458 >> 7, len(call_id)]) + call_id.encode()
        nodes = {
            system_id: system, info_id: info, query_id: query, answer_id: answer,
            result_id: result, query2_id: query2, answer2_id: answer2,
            ustep0: field(1, b"run ls"),
            ustep1: field(1, b"Reply with exactly: PLAIN"),
            rstep: field(3, field(1, b"thinking about it")),
            sstep: field(1, field(1, b"running ls")),
            sstep2: field(1, field(1, b"PLAIN")),
            tstep: field(2, tool_record),
            turn0: field(1, field(1, ustep0) + field(2, rstep) + field(2, sstep) + field(2, tstep)),
            turn1: field(1, field(1, ustep1) + field(2, sstep2)),
            root_id: (field(1, system_id) + field(1, info_id) + field(1, query_id)
                      + field(1, answer_id) + field(1, result_id)
                      + field(1, query2_id) + field(1, answer2_id)
                      + field(8, turn0) + field(8, turn1)),
        }
        self._write_store(root / sid, sid, root_id, nodes, name="ACP fixture")
        write_json(root / sid / "meta.json",
                   {"schemaVersion": 1, "cwd": "/work/acp", "title": "ACP fixture"})
        store = sessions.Store("cursor", "acp", root, "session_dirs")
        refs = {ref.session_id: ref for ref in sessions.cursor_iter_sessions(store)}
        self.assertEqual(set(refs), {"cursor-acp:" + sid})
        self.assertEqual(store.status, "ok")
        ref = refs["cursor-acp:" + sid]
        self.assertEqual(ref.title, "ACP fixture")
        self.assertEqual(ref.cwd, "/work/acp")
        self.assertEqual(ref.store, "acp")
        stats: dict[str, int] = {}
        fragments = list(sessions.cursor_iter_fragments(store, ref, 400, stats))
        visible = [(f["kind"], f["text"]) for f in fragments
                   if f["kind"] in ("prompt", "assistant", "reasoning")]
        # Turn-step reasoning lands just before the last assistant message.
        self.assertEqual(visible, [
            ("prompt", "run ls"),
            ("assistant", "running ls"),
            ("prompt", "Reply with exactly: PLAIN"),
            ("reasoning", "thinking about it"),
            ("assistant", "PLAIN"),
        ])
        kinds = [f["kind"] for f in fragments]
        self.assertIn("tool_call", kinds)
        self.assertIn("tool_result", kinds)
        self.assertTrue(any(f["kind"] == "context" and "hidden" in f["text"] for f in fragments))
        self.assertEqual(stats.get("unsupported_fragments", 0), 0)
        rescued = next(f for f in fragments if f["text"] == "Reply with exactly: PLAIN")
        self.assertEqual(rescued["authorship"], "human")
        self.assertTrue(rescued["visible"])
        env = dict(os.environ, PORCH_HISTORY_ROOT_CURSOR=str(root), PYTHONDONTWRITEBYTECODE="1")
        proc = subprocess.run(
            [sys.executable, str(LIB / "sessions.py"), "show", "cursor:cursor-acp:" + sid],
            capture_output=True, text=True, env=env, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("run ls", proc.stdout)
        self.assertIn("PLAIN", proc.stdout)
        self.assertNotIn("hidden", proc.stdout)
        proc = subprocess.run(
            [sys.executable, str(LIB / "sessions.py"), "show", "--scope", "tools",
             "cursor:cursor-acp:" + sid],
            capture_output=True, text=True, env=env, timeout=30)
        self.assertIn("Shell", proc.stdout)
        self.assertIn("x.txt", proc.stdout)
        self.assertNotIn("run ls</user_query>", proc.stdout)
        proc = subprocess.run(
            [sys.executable, str(LIB / "sessions.py"), "grep", "-a", "cursor", "PLAIN"],
            capture_output=True, text=True, env=env, timeout=30)
        self.assertIn("PLAIN", proc.stdout)

    def test_acp_listing_tolerates_meta_only_and_bad_sessions(self):
        root = self.root / "acp-sessions"
        good = "11111111-2222-3333-4444-555555555555"
        write_json(root / good / "meta.json", {"schemaVersion": 1, "cwd": "/work/acp"})
        # meta.json without store.db: listed from the sidecar, no fragments.
        meta_only = "22222222-2222-3333-4444-555555555555"
        write_json(root / meta_only / "meta.json",
                   {"schemaVersion": 1, "cwd": "/work/empty", "title": "Empty ACP"})
        # An unreadable session must not hide the others or fail the store.
        bad = "33333333-2222-3333-4444-555555555555"
        write_json(root / bad / "meta.json", {"schemaVersion": 99})
        store = sessions.Store("cursor", "acp", root, "session_dirs")
        refs = {ref.session_id: ref for ref in sessions.cursor_iter_sessions(store)}
        self.assertEqual(set(refs), {"cursor-acp:" + good, "cursor-acp:" + meta_only})
        self.assertEqual(store.status, "ok")
        self.assertIn("unsupported ACP metadata version", store.error or "")
        self.assertGreaterEqual(store.walk_stats.get("unreadable_dirs", 0), 1)
        self.assertEqual(refs["cursor-acp:" + meta_only].title, "Empty ACP")
        self.assertEqual(list(sessions.cursor_iter_fragments(
            store, refs["cursor-acp:" + meta_only], 400, {})), [])
        stats: dict[str, int] = {}
        self.assertEqual(list(sessions.cursor_iter_fragments(store, refs["cursor-acp:" + good], 400, stats)), [])
        # A session whose store.db graph is broken reports per-session, not store-wide.
        field = self._field
        root_id, ghost = bytes([1]) * 32, bytes([2]) * 32
        self._write_store(root / good, good, root_id, {root_id: field(1, ghost)})
        stats = {}
        self.assertEqual(list(sessions.cursor_iter_fragments(store, refs["cursor-acp:" + good], 400, stats)), [])
        self.assertEqual(stats["unreadable"], 1)
        self.assertIn("ACP store", store.error or "")
        self.assertFalse(sessions.coverage_summary([store], 1, 0, stats)["scan_complete"])

    def test_acp_missing_root_marks_store_missing(self):
        store = sessions.Store("cursor", "acp", self.root / "nowhere", "session_dirs")
        self.assertEqual(list(sessions.cursor_iter_sessions(store)), [])
        self.assertEqual(store.status, "missing")


class DateFilterTests(unittest.TestCase):
    """--since/--until apply to every harness through SessionRef.matches."""

    def ref(self, **kw):
        return sessions.SessionRef("codex", "sessions", "s", **kw)

    def test_since_and_until_use_known_timestamps(self):
        # Date-only bounds are local midnight. Keep fixtures a full day away
        # from that midnight so the assertions do not depend on the host offset.
        since = sessions.parse_time_bound("2026-11-19")
        until = sessions.parse_time_bound("2024-06-01")
        created_only = self.ref(created="2024-01-01T00:00:00Z")
        undated = self.ref()
        spanned = self.ref(created="2024-01-01T00:00:00Z", updated="2026-11-20T12:00:00Z")
        fresh = self.ref(created="2026-11-20T12:00:00Z")
        self.assertFalse(created_only.matches(None, since, None))
        self.assertFalse(undated.matches(None, since, None))
        self.assertTrue(spanned.matches(None, since, None))
        self.assertTrue(fresh.matches(None, since, None))
        self.assertTrue(spanned.matches(None, None, until))
        self.assertFalse(fresh.matches(None, None, until))
        self.assertTrue(undated.matches(None, None, until))
        self.assertFalse(undated.matches(None, since, until))


class CursorCLITests(FixtureMixin, unittest.TestCase):
    def _field(self, number: int, data: bytes) -> bytes:
        self.assertLess(len(data), 128)
        self.assertLess(number, 16)
        return bytes([number * 8 + 2, len(data)]) + data

    def test_cli_chats_prompt_answer_and_unsupported(self):
        sid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        root = self.root / "chats"
        created = int(datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc).timestamp() * 1000)
        updated = created + 3_600_000
        ids = [bytes([n]) * 32 for n in range(1, 10)]
        root_id, system_id, info_id, query_id, answer_id, turn_id, reason_id, tool_id, step_id = ids
        field = self._field
        system = b'{"role":"system","content":"bootstrap"}'
        info = b'{"role":"user","content":"<user_info>hidden</user_info>"}'
        query = json.dumps({
            "role": "user",
            "content": [
                {"type": "text", "text": "<system_reminder>hidden-reminder</system_reminder>"},
                {"type": "text", "text": "<timestamp>t</timestamp>\n<user_query>\nask\n</user_query>"},
            ],
        }, ensure_ascii=False).encode()
        answer = (
            b'{"role":"assistant","content":[{"type":"reasoning","text":""},'
            b'{"type":"text","text":"answer"},{"type":"mystery"}]}'
        )
        self.assertLess(len(answer), 128)
        nodes = {
            system_id: system,
            info_id: info,
            query_id: query,
            answer_id: answer,
            reason_id: field(3, field(1, b"because")),
            tool_id: field(4, b"tool-payload"),
            step_id: field(1, field(1, b"answer-from-step")),
            turn_id: field(1, field(2, reason_id) + field(2, tool_id) + field(2, step_id)),
            root_id: (field(1, system_id) + field(1, info_id) + field(1, query_id)
                      + field(1, answer_id) + field(8, turn_id)),
        }
        workspace = "0123456789abcdef0123456789abcdef"
        folder = root / workspace / sid
        folder.mkdir(parents=True)
        write_json(folder / "meta.json", {
            "schemaVersion": 1, "createdAtMs": created, "updatedAtMs": updated,
            "hasConversation": True, "cwd": "/work/cli",
        })
        db = folder / "store.db"
        conn = sqlite3.connect(db)
        conn.executescript(
            "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);"
            "CREATE TABLE blobs (id TEXT PRIMARY KEY, data BLOB);"
        )
        header = {"agentId": sid, "latestRootBlobId": root_id.hex(), "name": "CLI fixture"}
        conn.execute("INSERT INTO meta VALUES ('0', ?)", (json.dumps(header).encode().hex(),))
        conn.executemany("INSERT INTO blobs VALUES (?, ?)", [(key.hex(), value) for key, value in nodes.items()])
        conn.commit()
        conn.close()
        empty_id = "bbbbbbbb-bbbb-cccc-dddd-eeeeeeeeeeee"
        write_json(root / workspace / empty_id / "meta.json", {
            "schemaVersion": 1, "createdAtMs": created, "cwd": "/work/empty", "hasConversation": False,
        })
        old_id = "cccccccc-bbbb-cccc-dddd-eeeeeeeeeeee"
        write_json(root / workspace / old_id / "meta.json", {
            "schemaVersion": 1, "createdAtMs": 1704067200000, "cwd": "/work/old",
            "hasConversation": False,
        })
        write_json(root / workspace / "dddddddd-bbbb-cccc-dddd-eeeeeeeeeeee" / "meta.json",
                   {"schemaVersion": 99})
        store = sessions.Store("cursor", "chats", root, "session_dirs")
        refs = {ref.session_id: ref for ref in sessions.cursor_iter_sessions(store)}
        self.assertEqual(set(refs), {
            "cursor-cli:" + sid, "cursor-cli:" + empty_id, "cursor-cli:" + old_id,
        })
        self.assertEqual(store.status, "ok")
        self.assertGreaterEqual(store.walk_stats.get("unreadable_dirs", 0), 1)
        good = refs["cursor-cli:" + sid]
        self.assertEqual(good.title, "CLI fixture")
        self.assertEqual(good.cwd, "/work/cli")
        self.assertEqual(good.store, "chats")
        stats: dict[str, int] = {}
        fragments = list(sessions.cursor_iter_fragments(store, good, 400, stats))
        visible = [(item["kind"], item["text"]) for item in fragments if item["kind"] in ("prompt", "assistant", "reasoning")]
        self.assertEqual(visible, [("prompt", "ask"), ("reasoning", "because"), ("assistant", "answer")])
        self.assertNotIn("answer-from-step", [item["text"] for item in fragments])
        self.assertTrue(any(item["kind"] == "context" and "hidden" in item["text"] for item in fragments))
        self.assertEqual(stats["unsupported_fragments"], 2)
        self.assertEqual(list(sessions.cursor_iter_fragments(
            store, refs["cursor-cli:" + empty_id], 400, {})), [])
        since = sessions.parse_time_bound("2026-11-19")
        kept = [ref.session_id for ref in refs.values() if ref.matches(None, since, None)]
        self.assertCountEqual(kept, ["cursor-cli:" + sid, "cursor-cli:" + empty_id])
        env = dict(os.environ, PORCH_HISTORY_ROOT_CURSOR=str(root), PYTHONDONTWRITEBYTECODE="1")
        with patch.dict(os.environ, {"PORCH_HISTORY_ROOT_CURSOR": str(root)}):
            override = sessions.stores_for("cursor")
        self.assertEqual([item.name for item in override], ["chats"])
        flat = self.root / "acp-one"
        (flat / sid).mkdir(parents=True)
        write_json(flat / sid / "meta.json", {"schemaVersion": 1, "cwd": "/work"})
        with patch.dict(os.environ, {"PORCH_HISTORY_ROOT_CURSOR": str(flat)}):
            self.assertEqual(sessions.stores_for("cursor")[0].name, "acp")
        proc = subprocess.run(
            [sys.executable, str(LIB / "sessions.py"), "show", "cursor:cursor-cli:" + sid],
            capture_output=True, text=True, env=env, timeout=30,
        )
        # Unsupported tool/unknown parts make the scan partial; the text is still emitted.
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("ask", proc.stdout)
        self.assertIn("answer", proc.stdout)
        self.assertNotIn("hidden", proc.stdout)
        self.assertNotIn("\\\\n", proc.stdout)
        self.assertNotIn("answer-from-step", proc.stdout)
        proc = subprocess.run(
            [sys.executable, str(LIB / "sessions.py"), "grep", "-a", "cursor", "ask"],
            capture_output=True, text=True, env=env, timeout=30,
        )
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("ask", proc.stdout)
        proc = subprocess.run(
            [sys.executable, str(LIB / "sessions.py"), "list", "-a", "cursor", "--since", "2026-11-19"],
            capture_output=True, text=True, env=env, timeout=30,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        listed = [json.loads(line)["session_id"] for line in proc.stdout.splitlines()
                  if line.startswith("{") and '"session_id"' in line]
        self.assertIn("cursor-cli:" + sid, listed)
        self.assertNotIn("cursor-cli:" + old_id, listed)

    def test_cli_chats_tool_calls_and_config_root_override(self):
        sid = "aaaaaaaa-1111-2222-3333-444444444444"
        root = self.root / "chats"
        created = int(datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc).timestamp() * 1000)
        field = self._field
        call_id = "call-abc-0\nfc_def_0"
        ids = [bytes([n]) * 32 for n in range(1, 6)]
        root_id, query_id, answer_id, result_id, step_id = ids
        query = json.dumps({"role": "user", "content": "<user_query>run ls</user_query>"}).encode()
        answer = json.dumps({"role": "assistant", "content": [
            {"type": "tool-call", "toolCallId": call_id, "toolName": "Shell",
             "args": {"command": "ls"}},
            {"type": "text", "text": "done"}]}).encode()
        result = json.dumps({"role": "tool", "content": [
            {"type": "tool-result", "toolCallId": call_id, "toolName": "Shell",
             "result": "x.txt\n"}]}).encode()
        # The protobuf turn step for the same call (field 2, call id at 57):
        # covered by JSON, so it must NOT count as unsupported.
        # Field 57's wire key (57<<3|2 = 458) needs a two-byte varint.
        tool_record = bytes([458 & 127 | 128, 458 >> 7, len(call_id)]) + call_id.encode()
        tool_step = field(2, tool_record)
        turn_id = bytes([9]) * 32
        nodes = {
            query_id: query, answer_id: answer, result_id: result,
            step_id: tool_step,
            turn_id: field(1, field(2, step_id)),
            root_id: (field(1, query_id) + field(1, answer_id)
                      + field(1, result_id) + field(8, turn_id)),
        }
        workspace = "0123456789abcdef0123456789abcdef"
        folder = root / workspace / sid
        folder.mkdir(parents=True)
        write_json(folder / "meta.json", {
            "schemaVersion": 1, "createdAtMs": created, "hasConversation": True,
            "cwd": "/work/tools"})
        conn = sqlite3.connect(folder / "store.db")
        conn.executescript(
            "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);"
            "CREATE TABLE blobs (id TEXT PRIMARY KEY, data BLOB);")
        header = {"agentId": sid, "latestRootBlobId": root_id.hex()}
        conn.execute("INSERT INTO meta VALUES ('0', ?)", (json.dumps(header).encode().hex(),))
        conn.executemany("INSERT INTO blobs VALUES (?, ?)",
                         [(k.hex(), v) for k, v in nodes.items()])
        conn.commit()
        conn.close()

        store = sessions.Store("cursor", "chats", root, "session_dirs")
        ref = next(iter(sessions.cursor_iter_sessions(store)))
        stats: dict[str, int] = {}
        fragments = list(sessions.cursor_iter_fragments(store, ref, 400, stats))
        kinds = [f["kind"] for f in fragments]
        self.assertEqual(kinds, ["prompt", "tool_call", "assistant", "tool_result"])
        tool_call = fragments[1]
        self.assertIn("Shell", tool_call["text"])
        self.assertIn("ls", tool_call["text"])
        self.assertIn("x.txt", fragments[3]["text"])
        # Covered tool step: only the unknown shapes count as unsupported.
        self.assertEqual(stats.get("unsupported_fragments", 0), 0)

        env = dict(os.environ, PORCH_HISTORY_ROOT_CURSOR=str(root),
                   PYTHONDONTWRITEBYTECODE="1")
        proc = subprocess.run(
            [sys.executable, str(LIB / "sessions.py"), "show", "--scope", "tools",
             "cursor:cursor-cli:" + sid],
            capture_output=True, text=True, env=env, timeout=30,
        )
        self.assertIn("Shell", proc.stdout)
        self.assertIn("x.txt", proc.stdout)
        self.assertNotIn("run ls</user_query>", proc.stdout)

        # PORCH_HISTORY_ROOT_CURSOR at the config root discovers chats/ and
        # acp-sessions/ one level below it instead of silently missing them.
        config_root = self.root / "config-root"
        acp = config_root / "acp-sessions"
        chats = config_root / "chats"
        acp.mkdir(parents=True)
        chats.mkdir(parents=True)
        import shutil as _shutil
        _shutil.copytree(root / workspace, chats / workspace)
        acp_sid = "eeeeeeee-1111-2222-3333-444444444444"
        write_json(acp / acp_sid / "meta.json", {"schemaVersion": 1, "cwd": "/work"})
        with patch.dict(os.environ, {"PORCH_HISTORY_ROOT_CURSOR": str(config_root)}):
            stores = sessions.stores_for("cursor")
        self.assertEqual({s.name for s in stores}, {"chats", "acp"})
        chats_store = next(s for s in stores if s.name == "chats")
        self.assertEqual(chats_store.path, chats)
        # A non-empty override matching no known layout fails explicitly.
        junk = self.root / "junk-root"
        (junk / "random").mkdir(parents=True)
        with patch.dict(os.environ, {"PORCH_HISTORY_ROOT_CURSOR": str(junk)}):
            stores = sessions.stores_for("cursor")
        self.assertEqual(stores[0].status, "unavailable")
        self.assertIn("Cursor layout", stores[0].error)


class CodexIndexTests(FixtureMixin, unittest.TestCase):
    """Codex App/Desktop layers: state index, UI catalog, summaries, history."""

    def make(self):
        home = self.root / "codex_home"
        sid = "aaaaaaaa-1111-2222-3333-444444444444"
        child = "bbbbbbbb-1111-2222-3333-444444444444"
        # canonical rollout for the parent thread
        write_jsonl(home / "sessions" / "2026" / "01" / "02"
                    / f"rollout-2026-01-02T00-00-00-{sid}.jsonl", [
            {"timestamp": "2026-01-02T00:00:00Z", "type": "session_meta",
             "payload": {"id": sid, "cwd": "/work/repo"}},
            {"timestamp": "2026-01-02T00:00:01Z", "type": "event_msg",
             "payload": {"type": "user_message", "turn_id": "t1", "message": "real prompt"}},
        ])
        db = home / "state_5.sqlite"
        conn = sqlite3.connect(db)
        conn.executescript("""
            CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT, created_at TEXT,
                updated_at_ms TEXT, source TEXT, model_provider TEXT, cwd TEXT, title TEXT,
                sandbox_policy TEXT, approval_mode TEXT, tokens_used INTEGER,
                has_user_event INTEGER, archived INTEGER, git_sha TEXT, git_branch TEXT,
                git_origin_url TEXT, first_user_message TEXT, agent_nickname TEXT,
                agent_role TEXT, reasoning_effort TEXT, agent_path TEXT, thread_source TEXT,
                preview TEXT, history_mode TEXT, name TEXT, project_id TEXT, originator TEXT,
                model TEXT);
            CREATE TABLE thread_spawn_edges (parent_thread_id TEXT, child_thread_id TEXT PRIMARY KEY,
                status TEXT);
        """)
        conn.execute(
            "INSERT INTO threads (id, rollout_path, created_at, updated_at_ms, source, "
            "model_provider, cwd, title, tokens_used, archived, git_branch, first_user_message, "
            "reasoning_effort, model) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (sid, str(home / "sessions" / "2026" / "01" / "02"
                      / f"rollout-2026-01-02T00-00-00-{sid}.jsonl"),
             "2026-01-02T00:00:00Z", "2026-01-02T01:00:00Z", "vscode", "openai",
             "/work/repo", "Parent thread", 1234, 0, "main", "real prompt", "high", "gpt-6"))
        conn.execute(
            "INSERT INTO threads (id, rollout_path, created_at, source, model_provider, cwd, "
            "title, agent_nickname, agent_path) VALUES (?,?,?,?,?,?,?,?,?)",
            (child, "/nonexistent/rollout.jsonl", "2026-01-02T00:10:00Z",
             json.dumps({"subagent": {"thread_spawn": {"parent_thread_id": sid, "depth": 1,
                                                     "agent_nickname": "Hegel",
                                                     "agent_path": "/root/audit"}}}),
             "openai", "/work/repo", "Child thread", "Hegel", "/root/audit"))
        conn.execute("INSERT INTO thread_spawn_edges VALUES (?, ?, 'done')", (sid, child))
        conn.commit()
        conn.close()
        cat = home / "sqlite" / "codex-dev.db"
        cat.parent.mkdir(parents=True)
        conn = sqlite3.connect(cat)
        conn.execute("CREATE TABLE local_thread_catalog (host_id TEXT, thread_id TEXT, "
                     "display_title TEXT, source_created_at TEXT, source_updated_at TEXT, "
                     "cwd TEXT, source_kind TEXT, source_detail TEXT, model_provider TEXT, "
                     "git_branch TEXT, project_id TEXT, conversation_origin TEXT, "
                     "missing_candidate INTEGER)")
        conn.execute("INSERT INTO local_thread_catalog VALUES ('local', ?, 'Local chat', "
                     "'2026-01-02T00:00:00Z', '2026-01-02T01:00:00Z', '/work/repo', 'vscode', "
                     "NULL, 'openai', 'main', NULL, NULL, 0)", (sid,))
        conn.execute("INSERT INTO local_thread_catalog VALUES ('chatgpt:h1', 'cloud-1', "
                     "'Cloud chat', '2026-01-02T00:00:00Z', NULL, NULL, 'chatgpt', NULL, "
                     "'openai', NULL, NULL, NULL, 0)")
        conn.commit()
        conn.close()
        write_jsonl(home / "history.jsonl", [
            {"session_id": sid, "ts": 1767225600, "text": "logged prompt"},
            {"session_id": "other", "ts": 1767225660, "text": "second prompt"},
        ])
        return home, sid, child

    def test_state_index(self):
        home, sid, child = self.make()
        store = sessions.Store("codex", "state:state_5.sqlite", home / "state_5.sqlite", "sqlite")
        refs = {r.session_id: r for r in sessions.codex_iter_sessions(store)}
        self.assertEqual(set(refs), {sid, child})
        parent = refs[sid]
        self.assertEqual(parent.title, "Parent thread")
        self.assertEqual(parent.cwd, "/work/repo")
        self.assertIn("gpt-6", parent.models)
        self.assertIn("openai", parent.models)
        self.assertEqual(parent.extra["rollout_path"].split("/")[-1][:7], "rollout")
        self.assertEqual(parent.extra["git_branch"], "main")
        # child: subagent lineage via source JSON and thread_spawn_edges
        self.assertEqual(refs[child].parent, sid)
        self.assertEqual(refs[child].extra["source"], "subagent")
        self.assertEqual(refs[child].agent, "Hegel")

    def test_state_fragments_delegate_to_rollout(self):
        home, sid, child = self.make()
        store = sessions.Store("codex", "state:state_5.sqlite", home / "state_5.sqlite", "sqlite")
        refs = {r.session_id: r for r in sessions.codex_iter_sessions(store)}
        stats: dict[str, int] = {}
        out = list(sessions.codex_iter_fragments(store, refs[sid], 400, stats))
        self.assertTrue(any(f["kind"] == "prompt" and f["text"] == "real prompt" for f in out))
        self.assertTrue(all("rollout-" in str(f["locator"].get("file")) for f in out))
        # child has no rollout file → honest index-only fragments
        out = list(sessions.codex_iter_fragments(store, refs[child], 400, stats))
        self.assertTrue(out and all(f["kind"] == "metadata" or f["kind"] == "prompt" for f in out))
        self.assertIn("no local rollout", out[0]["basis"])

    def _stale_state_store(self, rows):
        """A state_*.sqlite whose threads rows point at missing rollout paths."""
        home = self.root / "codex_home"
        db = home / "state_9.sqlite"
        db.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT, "
                     "created_at TEXT, cwd TEXT, title TEXT)")
        for sid, rollout in rows:
            conn.execute("INSERT INTO threads VALUES (?,?,?,?,?)",
                         (sid, rollout, "2026-01-02T00:00:00Z", "/w", sid))
        conn.commit()
        conn.close()
        return home, sessions.Store("codex", "state:state_9.sqlite", db, "sqlite")

    @staticmethod
    def _rollout_records(sid, text):
        return [
            {"timestamp": "2026-01-02T00:00:00Z", "type": "session_meta",
             "payload": {"id": sid, "cwd": "/w"}},
            {"timestamp": "2026-01-02T00:00:01Z", "type": "event_msg",
             "payload": {"type": "user_message", "turn_id": "t", "message": text}},
        ]

    @unittest.skipUnless(sys.version_info >= (3, 14), "stdlib Zstandard requires Python 3.14")
    def test_stale_rollout_path_falls_back_to_zst_and_archived(self):
        from compression import zstd
        home = self.root / "codex_home"
        sid_zst = "cccccccc-1111-2222-3333-444444444444"
        sid_arch = "dddddddd-1111-2222-3333-444444444444"
        packed_dir = home / "sessions" / "2026" / "01" / "02"
        packed_dir.mkdir(parents=True)
        name_zst = f"rollout-2026-01-02T00-00-00-{sid_zst}.jsonl"
        (packed_dir / f"{name_zst}.zst").write_bytes(
            zstd.compress("".join(json.dumps(r) + "\n"
                                  for r in self._rollout_records(sid_zst, "packed sibling")).encode()))
        name_arch = f"rollout-2026-01-03T00-00-00-{sid_arch}.jsonl"
        arch = home / "archived_sessions"
        arch.mkdir(parents=True)
        (arch / f"{name_arch}.zst").write_bytes(
            zstd.compress("".join(json.dumps(r) + "\n"
                                  for r in self._rollout_records(sid_arch, "archived rollout")).encode()))
        _home, store = self._stale_state_store([
            (sid_zst, str(packed_dir / name_zst)),          # .jsonl gone, .jsonl.zst beside it
            (sid_arch, str(home / "sessions" / "2026" / "01" / "03" / name_arch)),  # moved to archived
        ])
        refs = {r.session_id: r for r in sessions.codex_iter_sessions(store)}
        stats: dict[str, int] = {}
        out = list(sessions.codex_iter_fragments(store, refs[sid_zst], 400, stats))
        self.assertTrue(any(f["text"] == "packed sibling" for f in out))
        self.assertTrue(str(out[0]["locator"]["file"]).endswith(".jsonl.zst"))
        out = list(sessions.codex_iter_fragments(store, refs[sid_arch], 400, stats))
        self.assertTrue(any(f["text"] == "archived rollout" for f in out))
        self.assertIn("archived_sessions", out[0]["locator"]["file"])

    def test_stale_rollout_path_finds_archived_plain_rollout(self):
        home = self.root / "codex_home"
        sid = "eeeeeeee-1111-2222-3333-444444444444"
        name = f"rollout-2026-01-04T00-00-00-{sid}.jsonl"
        arch = home / "archived_sessions"
        write_jsonl(arch / name, self._rollout_records(sid, "plain archived"))
        _home, store = self._stale_state_store([
            (sid, str(home / "sessions" / "2026" / "01" / "04" / name)),
        ])
        refs = {r.session_id: r for r in sessions.codex_iter_sessions(store)}
        out = list(sessions.codex_iter_fragments(store, refs[sid], 400, {}))
        self.assertTrue(any(f["text"] == "plain archived" for f in out))
        self.assertIn("archived_sessions", out[0]["locator"]["file"])

    def test_grep_over_layered_stores_judges_each_rollout_once(self):
        home, sid, child = self.make()
        env = {"CODEX_HOME": str(home)}
        real = sessions.RawPrefilter
        calls = []
        original = real.session_may_match

        def counting(self_, path, *rest):
            calls.append(str(path))
            return original(self_, path, *rest)

        for argv in ([("pattern", "real prompt")], [("pattern", "absent words")],
                     [("pattern", "REAL"), ("ignore_case", True), ("scope", "all")]):
            argv = [("agent", "codex"), *argv]
            calls.clear()
            with patch.object(real, "session_may_match", counting):
                fast = GrepPrefilterTests.grep(self, env, *argv)
            self.assertEqual(len(calls), len(set(calls)), calls)
            with patch.object(sessions, "RawPrefilter", lambda p, **kw: real(p, ignore_case=False, regex=True)):
                slow = GrepPrefilterTests.grep(self, env, *argv)
            self.assertEqual(fast[:-1], slow[:-1], argv)
        sessions._ROLLOUT_INDEX.clear()

    def test_catalog_and_history(self):
        home, sid, child = self.make()
        cat = sessions.Store("codex", "catalog", home / "sqlite" / "codex-dev.db", "sqlite")
        refs = {r.session_id: r for r in sessions.codex_iter_sessions(cat)}
        self.assertEqual(refs[sid].extra["source_kind"], "vscode")
        self.assertEqual(refs["cloud-1"].extra["source_kind"], "chatgpt")
        self.assertEqual(refs["cloud-1"].extra["host_id"], "chatgpt:h1")
        # cloud-only row: no rollout → index_row fragment, never fake conversation
        stats: dict[str, int] = {}
        out = list(sessions.codex_iter_fragments(cat, refs["cloud-1"], 400, stats))
        self.assertEqual(len([f for f in out if f["kind"] in ("prompt", "assistant")]), 0)
        self.assertTrue(any(f["native_type"] == "index_row" for f in out))

        hist = sessions.Store("codex", "history", home / "history.jsonl", "jsonl_dir")
        refs = {r.session_id: r for r in sessions.codex_iter_sessions(hist)}
        self.assertEqual(len(refs), 2)
        stats = {}
        out = list(sessions.codex_iter_fragments(hist, refs[sid], 400, stats))
        self.assertEqual([f["text"] for f in out], ["logged prompt"])
        self.assertEqual(out[0]["kind"], "prompt")
        self.assertEqual(out[0]["ts"], "2026-01-01T00:00:00Z")  # epoch → ISO


class ClaudeDesktopTests(FixtureMixin, unittest.TestCase):
    def test_cowork_new_prompt_closes_unfinished_turn(self):
        ref = sessions.SessionRef("claude-desktop", "cowork", "s", extra={"surface": "cowork"})
        store = sessions.Store("claude-desktop", "cowork", self.root, "session_dirs")
        fragments = [
            {"kind": "prompt", "native_type": "user", "origin": "human", "text": "first"},
            {"kind": "assistant", "native_type": "assistant.text", "text": "partial"},
            {"kind": "prompt", "native_type": "user", "origin": "human", "text": "next"},
        ]
        turns = sessions.interpret_session(store, ref, fragments)
        self.assertEqual([turn["open"] for turn in turns], [False, True])
        self.assertTrue(all(turn["final"] is None for turn in turns))

    def test_manifest_lineage_and_stream_turns(self):
        root = self.root / "desktop"
        claude = self.root / "claude"
        sid = "cli-session"
        manifest = {"sessionId": "local_code", "cliSessionId": sid, "title": "Title", "titleSource": "user",
                    "priorCliSessionIds": ["old1", "old2"], "spawnedFrom": {"sessionId": "parent", "taskId": "task", "title": "Spawn"},
                    "forkedFromSessionId": "fork", "forkedAtMessageUuid": "uuid",
                    "interruptedAwaitingPerson": True, "cleanShutdown": False, "error": "failure", "errorCategory": "network",
                    "errorAt": "time", "completedTurns": 2, "isArchived": True}
        write_json(root / "local_code.json", manifest)
        write_jsonl(claude / "project" / f"{sid}.jsonl", [
            {"type": "user", "origin": {"kind": "human"}, "message": {"content": "question"}},
            {"type": "assistant", "message": {"id": "m", "stop_reason": "end_turn", "content": [{"type": "text", "text": "answer"}]}}])
        store = sessions.Store("claude-desktop", "code-sessions", root, "json_dir")
        ref = next(sessions.claude_desktop_iter_sessions(store))
        self.assertEqual((ref.title, ref.extra["titleSource"]), ("Title", "user"))
        self.assertEqual({(e["type"], e["target_id"]) for e in ref.extra["edges"]}, {("alias", sid), ("continuation", "old1"), ("continuation", "old2"), ("spawned", "parent"), ("fork", "fork")})
        self.assertEqual(next(e for e in ref.extra["edges"] if e["type"] == "spawned")["locator"]["taskId"], "task")
        self.assertEqual(next(e for e in ref.extra["edges"] if e["type"] == "fork")["locator"]["forkedAtMessageUuid"], "uuid")
        self.assertEqual(ref.extra["native_status"], {k: manifest[k] for k in ("interruptedAwaitingPerson", "cleanShutdown", "error", "errorCategory", "errorAt", "completedTurns", "isArchived")})
        sessions.reset_discovery_caches()
        with patch.dict(os.environ, {"PORCH_HISTORY_ROOT_CLAUDE_CODE": str(claude)}):
            turns = sessions.interpret_session(store, ref, sessions.claude_desktop_iter_fragments(store, ref, 0, {}))
        self.assertEqual((turns[0]["prompts"][0]["text"], turns[0]["final"]["text"]), ("question", "answer"))
        cowork_root = self.root / "cowork"
        cowork = cowork_root / "local_cowork"
        write_json(cowork_root / "local_cowork.json", {"sessionId": "local_cowork"})
        write_jsonl(cowork / "audit.jsonl", [
            {"type": "user", "message": {"content": "main"}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "main answer"}]}},
            {"type": "user", "parent_tool_use_id": "child", "message": {"content": "child prompt"}},
            {"type": "assistant", "parent_tool_use_id": "child", "message": {"content": [{"type": "text", "text": "child answer"}]}},
            {"type": "result", "subtype": "success"},
            {"type": "user", "message": {"content": "unfinished"}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "partial"}]}}])
        store = sessions.Store("claude-desktop", "cowork", cowork_root, "session_dirs")
        ref = next(sessions.claude_desktop_iter_sessions(store))
        turns = sessions.interpret_session(store, ref, sessions.claude_desktop_iter_fragments(store, ref, 0, {}))
        self.assertEqual([t["prompts"][0]["text"] for t in turns], ["main", "unfinished"])
        self.assertEqual(turns[0]["final"]["text"], "main answer")
        self.assertIsNone(turns[1]["final"])
        self.assertTrue(turns[1]["open"])

    def test_cowork(self):
        root = self.root / "lam"
        sess = root / "proj" / "local_abc"
        write_json(root / "proj" / "local_abc.json",
                   {"sessionId": "local_abc", "title": "Desktop chat", "cwd": "/work/repo",
                    "model": "claude-opus-5", "effort": "medium",
                    "initialMessage": "first user message",
                    "createdAt": "2026-01-07T00:00:00Z",
                    "lastActivityAt": "2026-01-07T01:00:00Z"})
        write_jsonl(sess / "audit.jsonl", [
            {"type": "system", "subtype": "init", "cwd": "/work/repo", "model": "claude-4",
             "timestamp": "2026-01-07T00:00:00Z"},
            {"type": "user", "timestamp": "2026-01-07T00:00:01Z", "uuid": "u1",
             "message": {"role": "user", "content": "desktop prompt"}},
            {"type": "user", "timestamp": "2026-01-07T00:00:02Z", "uuid": "u2",
             "parent_tool_use_id": "tu9",
             "message": {"role": "user", "content": "subagent user msg"}},
            {"type": "assistant", "timestamp": "2026-01-07T00:00:03Z", "uuid": "a1",
             "message": {"role": "assistant", "model": "claude-4",
                         "content": [{"type": "text", "text": "answer"}]}},
            {"type": "system", "subtype": "permission_request",
             "timestamp": "2026-01-07T00:00:04Z"},
            {"type": "result", "subtype": "success", "result": "ok",
             "timestamp": "2026-01-07T00:00:05Z"},
        ])
        # sensitive HMAC key + nested inner claude transcripts — must be left alone
        (sess / ".audit-key").parent.mkdir(parents=True, exist_ok=True)
        (sess / ".audit-key").write_text("SECRET-KEY", encoding="utf-8")
        inner = sess / ".claude" / "projects" / "-work-repo"
        write_jsonl(inner / "inner.jsonl", [
            {"type": "user", "timestamp": "t", "sessionId": "inner",
             "message": {"role": "user", "content": "inner transcript"}},
        ])
        store = sessions.Store("claude-desktop", "cowork", root, "session_dirs")
        refs = list(sessions.claude_desktop_iter_sessions(store))
        self.assertEqual(len(refs), 1)  # inner .claude tree not double-counted
        self.assertEqual(refs[0].title, "Desktop chat")
        self.assertEqual(refs[0].models, ["claude-opus-5"])
        self.assertEqual(refs[0].extra["effort"], "medium")

        stats: dict[str, int] = {}
        out = list(sessions.claude_desktop_iter_fragments(store, refs[0], 400, stats))
        prompts = [f for f in out if f["kind"] == "prompt"]
        # initialMessage is proven human input
        first = next(f for f in prompts if f["native_type"] == "manifest.initialMessage")
        self.assertEqual(first["authorship"], "human")
        self.assertEqual(first["text"], "first user message")
        side = [f for f in prompts if (f.get("rel") or {}).get("parent_tool_use_id")]
        self.assertTrue(side and side[0]["authorship"] == "agent")
        self.assertTrue(any(f["kind"] == "boundary" for f in out))
        self.assertTrue(any(f["kind"] == "permission" for f in out))
        self.assertTrue(any(f["native_type"] == "inner_transcript" for f in out))
        # .audit-key never read or referenced
        self.assertFalse(any(".audit-key" in json.dumps(f["locator"]) for f in out))
        self.assertEqual((sess / ".audit-key").read_text(), "SECRET-KEY")

    def test_code_sessions_linkage(self):
        root = self.root / "ccs"
        cli_sid = "cccccccc-1111-2222-3333-444444444444"
        write_json(root / "grp" / "local_x1.json",
                   {"sessionId": "local_x1", "cliSessionId": cli_sid, "cwd": "/work/repo",
                    "title": "Linked chat", "model": "claude-opus-5", "effort": "medium",
                    "permissionMode": "acceptEdits", "completedTurns": 3,
                    "priorCliSessionIds": ["older-1"],
                    "createdAt": "2026-01-07T00:00:00Z",
                    "lastActivityAt": "2026-01-07T01:00:00Z"})
        write_json(root / "grp" / "local_x2.json",
                   {"sessionId": "local_x2", "cliSessionId": "gone-1", "cwd": "/work/repo",
                    "title": "Orphaned chat", "createdAt": "2026-01-07T00:00:00Z"})
        claude_root = self.root / "claude"
        write_jsonl(claude_root / "-work-repo" / f"{cli_sid}.jsonl", [
            {"type": "user", "timestamp": "2026-01-07T00:00:01Z", "sessionId": cli_sid,
             "origin": {"kind": "human"},
             "message": {"role": "user", "content": "linked prompt"}},
            {"type": "assistant", "timestamp": "2026-01-07T00:00:02Z", "sessionId": cli_sid,
             "message": {"role": "assistant", "model": "claude-opus-5",
                         "content": [{"type": "text", "text": "linked answer"}]}},
        ])
        store = sessions.Store("claude-desktop", "code-sessions", root, "json_dir")
        refs = {r.session_id: r for r in sessions.claude_desktop_iter_sessions(store)}
        self.assertEqual(refs["local_x1"].extra["cli_session_id"], cli_sid)
        self.assertEqual(refs["local_x1"].extra["surface"], "code-tab")

        env = {"PORCH_HISTORY_ROOT_CLAUDE_CODE": str(claude_root)}
        with patch.dict(os.environ, env):
            stats: dict[str, int] = {}
            out = list(sessions.claude_desktop_iter_fragments(store, refs["local_x1"], 400, stats))
        convo = [f for f in out if f["kind"] in ("prompt", "assistant")]
        self.assertTrue(any(f["text"] == "linked prompt" and f["authorship"] == "human"
                            for f in convo))
        self.assertTrue(all(f["harness"] == "claude-desktop" for f in out))
        self.assertTrue(any(f["native_type"] == "prior_cli_session" for f in out))
        # missing transcript → explicit metadata, no fabricated messages
        with patch.dict(os.environ, env):
            out = list(sessions.claude_desktop_iter_fragments(store, refs["local_x2"], 400, stats))
        self.assertFalse(any(f["kind"] in ("prompt", "assistant") for f in out))
        self.assertTrue(any(f["native_type"] == "cli_transcript" and "no transcript" in f["basis"]
                            for f in out))


    def test_raw_prefilter_on_code_sessions(self):
        root = self.root / "ccs"
        claude_root = self.root / "claude"
        for index in range(4):
            cli_sid = f"cccccccc-1111-2222-3333-00000000000{index}"
            write_json(root / "grp" / f"local_{index}.json",
                       {"sessionId": f"local_{index}", "cliSessionId": cli_sid if index < 3 else "gone",
                        "title": f"Chat {index} manifest-word", "cwd": "/work/repo",
                        "initialMessage": "starter", "createdAt": "2026-01-07T00:00:00Z"})
            write_jsonl(claude_root / "-work-repo" / f"{cli_sid}.jsonl", [
                {"type": "attachment", "attachment": {"type": "skill_listing", "content": "listed words"}},
                {"type": "user", "timestamp": "2026-01-07T00:00:01Z", "origin": {"kind": "human"},
                 "message": {"role": "user", "content": f"prompt number {index}"}}])
        store = sessions.Store("claude-desktop", "code-sessions", root, "json_dir")
        with patch.dict(os.environ, {"PORCH_HISTORY_ROOT_CLAUDE_CODE": str(claude_root)}):
            sessions._CLAUDE_TRANSCRIPTS.clear()
            refs = list(sessions.claude_desktop_iter_sessions(store))
            dropped = check_prefilter_skips(self, store, refs, [
                ("prompt number 1", False, "convo"), ("PROMPT", True, "convo"), ("listed words", False, "convo"),
                ("listed words", False, "all"), ("manifest-word", False, "all"), ("manifest-word", False, "convo"),
                ("absent text", False, "all")])
        sessions._CLAUDE_TRANSCRIPTS.clear()
        self.assertGreater(dropped, 8)


class QwenTests(FixtureMixin, unittest.TestCase):
    def test_qwen(self):
        root = self.root / "qwen"
        write_jsonl(root / "-work-repo" / "chats" / "s1.jsonl", [
            {"type": "user", "timestamp": "t1", "sessionId": "s1", "cwd": "/work/repo",
             "message": {"parts": [{"text": "qwen human prompt"}]}},
            {"type": "assistant", "timestamp": "t2", "sessionId": "s1", "model": "qwen3",
             "message": {"parts": [{"functionCall": {"name": "f", "id": "c1", "args": {}}},
                                    {"text": "answer text"}]}},
            {"type": "user", "timestamp": "t3", "sessionId": "s1",
             "message": {"parts": [{"functionResponse": {"response": {"out": 1}}}]}},
            {"type": "user", "subtype": "notification", "timestamp": "t4", "sessionId": "s1",
             "message": {"parts": [{"text": "cron tick"}]}},
        ])
        store = sessions.Store("qwen-code", "projects", root, "jsonl_dir")
        refs = list(sessions.qwen_iter_sessions(store))
        self.assertEqual(refs[0].session_id, "s1")
        stats: dict[str, int] = {}
        out = list(sessions.qwen_iter_fragments(store, refs[0], 400, stats))
        prompts = [f for f in out if f["kind"] == "prompt"]
        self.assertEqual([f["text"] for f in prompts], ["qwen human prompt"])
        self.assertTrue(any(f["kind"] == "tool_result" and f["native_type"] == "user.functionResponse"
                            for f in out))
        self.assertTrue(any(f["kind"] == "context" for f in out))  # notification
        self.assertTrue(any(f["kind"] == "tool_call" for f in out))


class KimiTests(FixtureMixin, unittest.TestCase):
    def test_kimi(self):
        root = self.root / "kimi" / "sessions"
        sess = root / "%2Fwork%2Frepo" / "ks1"
        write_json(sess / "state.json",
                   {"title": "Kimi task", "workDir": "/work/repo",
                    "createdAt": "2026-01-08T00:00:00Z"})
        write_jsonl(sess / "agents" / "main" / "wire.jsonl", [
            {"type": "turn.prompt", "time": "2026-01-08T00:00:01Z", "origin": {"kind": "user"},
             "input": [{"text": "kimi human prompt"}]},
            {"type": "context.append_loop_event", "time": "2026-01-08T00:00:02Z",
             "event": {"type": "content.part", "turnId": "t1",
                       "part": {"type": "think", "think": "hmm"}}},
            {"type": "context.append_loop_event", "time": "2026-01-08T00:00:03Z",
             "event": {"type": "content.part", "turnId": "t1",
                       "part": {"type": "tool.call", "name": "run", "toolCallId": "c1", "arguments": "{}"}}},
            {"type": "context.append_loop_event", "time": "2026-01-08T00:00:04Z",
             "event": {"type": "step.end", "turnId": "t1", "finishReason": "stop"}},
            {"type": "permission.set_mode", "time": "2026-01-08T00:00:05Z", "mode": "auto"},
        ])
        write_jsonl(sess / "agents" / "worker" / "wire.jsonl", [
            {"type": "turn.prompt", "time": "2026-01-08T00:00:06Z", "origin": {"kind": "subagent"},
             "input": [{"text": "sub task"}]},
        ])
        store = sessions.Store("kimi-code", "sessions", root, "session_dirs")
        refs = list(sessions.kimi_iter_sessions(store))
        self.assertEqual(refs[0].session_id, "ks1")
        self.assertEqual(refs[0].cwd, "/work/repo")
        stats: dict[str, int] = {}
        out = list(sessions.kimi_iter_fragments(store, refs[0], 400, stats))
        prompts = [f for f in out if f["kind"] == "prompt"]
        human = [f for f in prompts if f.get("authorship") == "human"]
        sub = [f for f in prompts if f.get("authorship") == "agent"]
        self.assertEqual(len(human), 1)
        self.assertEqual(len(sub), 1)
        self.assertTrue(any(f["kind"] == "reasoning" for f in out))
        self.assertTrue(any(f["kind"] == "tool_call" for f in out))
        self.assertTrue(any(f["kind"] == "permission" for f in out))
        self.assertTrue(any(f["kind"] == "boundary" for f in out))


class OmpTests(FixtureMixin, unittest.TestCase):
    def test_omp(self):
        root = self.root / "omp"
        write_jsonl(root / "2026" / "s1.jsonl", [
            {"type": "session", "id": "s1", "cwd": "/work/repo", "title": "OMP run",
             "timestamp": "2026-01-09T00:00:00Z"},
            {"type": "message", "timestamp": "2026-01-09T00:00:01Z", "id": "m1",
             "message": {"role": "user", "content": [{"text": "omp prompt"}]}},
            {"type": "message", "timestamp": "2026-01-09T00:00:02Z", "id": "m2",
             "message": {"role": "user", "attribution": "agent",
                         "content": [{"text": "injected by orchestrator"}]}},
            {"type": "message", "timestamp": "2026-01-09T00:00:03Z", "id": "m3",
             "message": {"role": "assistant", "model": "m",
                         "content": [{"type": "thinking", "thinking": "t"},
                                     {"type": "toolCall", "id": "c1", "name": "x", "arguments": "{}"},
                                     {"type": "text", "text": "omp answer"}],
                         "stopReason": "stop"}},
            {"type": "compaction", "timestamp": "2026-01-09T00:00:04Z", "summary": "…"},
        ])
        store = sessions.Store("omp", "sessions", root, "jsonl_dir")
        refs = list(sessions.omp_iter_sessions(store))
        self.assertEqual(refs[0].title, "OMP run")
        stats: dict[str, int] = {}
        out = list(sessions.omp_iter_fragments(store, refs[0], 400, stats))
        prompts = [f for f in out if f["kind"] == "prompt"]
        self.assertEqual(len(prompts), 2)
        human = [f for f in prompts if f.get("authorship") == "human"]
        agent = [f for f in prompts if f.get("authorship") == "agent"]
        self.assertEqual(len(human), 1)
        self.assertEqual(len(agent), 1)
        self.assertTrue(any(f["kind"] == "compaction" for f in out))
        self.assertTrue(any(f["kind"] == "reasoning" for f in out))
        self.assertTrue(any(f["kind"] == "tool_call" for f in out))


class PorchRunTests(FixtureMixin, unittest.TestCase):
    def test_normalized_artifact_events_and_coverage(self):
        run = self.root / "runs" / "r"
        artifacts = self.root / "artifacts"
        write_json(run / "meta.json", {"run_id": "r", "artifacts_dir": str(artifacts)})
        write_jsonl(artifacts / "normalized" / "agent.jsonl", [
            {"type": "turn_started", "data": "start"},
            {"type": "thinking_delta", "data": "reasoning"},
            {"type": "tool_started", "data": "tool"},
            {"type": "answer_delta", "data": "answer"},
            {"type": "turn_completed", "data": "done"},
            {"type": "run_completed", "data": "done"},
        ])
        store = sessions.Store("porch", "runs", run.parent, "run_registry")
        ref = next(sessions.porch_iter_sessions(store))
        stats = {}
        fragments = list(sessions.porch_iter_fragments(store, ref, 0, stats))
        self.assertEqual([f["kind"] for f in fragments], ["boundary", "reasoning", "tool_call", "assistant", "boundary", "boundary"])
        self.assertEqual(fragments[3]["text"], "answer")
        self.assertNotIn("unreadable", stats)

    def test_saved_final_without_stored_prompt(self):
        run = self.root / "runs" / "answer-only"
        write_json(run / "meta.json", {"run_id": "answer-only", "status": "completed"})
        (run / "final.txt").write_text("saved result", encoding="utf-8")
        store = sessions.Store("porch", "runs", run.parent, "run_registry")
        ref = next(sessions.porch_iter_sessions(store))
        turns = sessions.interpret_session(store, ref, sessions.porch_iter_fragments(store, ref, 0, {}))
        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0]["prompts"], [])
        self.assertEqual((turns[0]["final"]["text"], turns[0]["final_basis"], turns[0]["open"]),
                         ("saved result", "native:final.txt", False))

    def test_lineage_worker_backends_and_caller_keys(self):
        from steer.launcher import launcher_instance, launcher_metadata
        claude_root = self.root / "claude"
        write_jsonl(claude_root / "project" / "caller-claude.jsonl", [{"type": "user", "message": {"content": "task"}}])
        claude_ref = next(sessions.claude_iter_sessions(sessions.Store("claude-code", "test", claude_root, "jsonl_dir")))
        codex_root = self.root / "codex"
        write_jsonl(codex_root / "rollout-2026-01-01T00-00-00-caller-codex.jsonl", [{"type": "session_meta", "payload": {"id": "caller-codex"}}])
        codex_ref = next(sessions.codex_iter_sessions(sessions.Store("codex", "test", codex_root, "jsonl_dir")))
        self.assertEqual(claude_ref.extra["launcher_key"], launcher_metadata({"CLAUDE_CODE_SESSION_ID": "caller-claude"})["launcher_instance"])
        self.assertEqual(codex_ref.extra["launcher_key"], launcher_metadata({"CODEX_THREAD_ID": "caller-codex"})["launcher_instance"])
        root = self.root / "runs"
        for backend, harness in sessions.BACKEND_HARNESS.items():
            write_json(root / backend / "meta.json", {"run_id": backend, "backend": backend, "native_session_id": "native", "task": "brief",
                       "launcher_instance": "run:parent", "continued_from": "previous"})
            (root / backend / "final.txt").write_text("final", encoding="utf-8")
            write_jsonl(root / backend / "normalized" / "events.jsonl", [{"type": "run_started"}])
        for launcher, native in (("claude", "caller-claude"), ("codex", "caller-codex")):
            write_json(root / launcher / "meta.json", {"run_id": launcher, "launcher_instance": launcher_instance(launcher, native)})
        store = sessions.Store("porch", "runs", root, "run_registry")
        refs = {ref.session_id: ref for ref in sessions.porch_iter_sessions(store)}
        self.assertEqual(refs["claude"].extra["edges"][0]["unresolved"], claude_ref.extra["launcher_key"])
        self.assertEqual(refs["codex"].extra["edges"][0]["unresolved"], codex_ref.extra["launcher_key"])
        for backend, harness in sessions.BACKEND_HARNESS.items():
            ref = refs[backend]
            edges = ref.extra["edges"]
            self.assertEqual(next(e for e in edges if e["type"] == "worker_session")["target_harness"], harness)
            self.assertEqual(next(e for e in edges if e["type"] == "caller_run")["unresolved"], "run:parent")
            self.assertEqual(next(e for e in edges if e["type"] == "parent_run")["target_id"], "parent")
            self.assertEqual(next(e for e in edges if e["type"] == "continuation")["target_id"], "previous")
            turns = sessions.interpret_session(store, ref, sessions.porch_iter_fragments(store, ref, 0, {}))
            self.assertEqual((turns[0]["prompts"][0]["text"], turns[0]["final"]["text"], turns[0]["final_basis"]), ("brief", "final", "native:final.txt"))

    def test_runs(self):
        run = self.root / "runs" / "run_test-1"
        write_json(run / "meta.json",
                   {"run_id": "run_test-1", "agent_id": "grok", "backend": "grok-build",
                    "model": "grok-4.6", "effort": "high", "cwd": "/work/repo",
                    "status": "completed", "task": "do stuff",
                    "launched_at": "2026-01-10T00:00:00Z",
                    "session_handle": {"thread_id": "native-1"}})
        write_jsonl(run / "normalized" / "events.jsonl", [
            {"type": "run_started", "ts": "2026-01-10T00:00:01Z", "agent": "grok", "model": "grok-4.6"},
            {"type": "steer", "ts": "2026-01-10T00:00:02Z", "text": "keep going"},
            {"type": "text", "ts": "2026-01-10T00:00:03Z", "text": "working…"},
            {"type": "end", "ts": "2026-01-10T00:00:04Z", "status": "completed"},
        ])
        store = sessions.Store("porch", "runs", self.root / "runs", "run_registry")
        refs = list(sessions.porch_iter_sessions(store))
        self.assertEqual(refs[0].extra["native_session"], "native-1")
        stats: dict[str, int] = {}
        out = list(sessions.porch_iter_fragments(store, refs[0], 400, stats))
        self.assertTrue(any(f["kind"] == "prompt" and f["authorship"] == "agent" for f in out))
        self.assertTrue(any(f["kind"] == "boundary" and f["status"] == "completed" for f in out))

    def test_run_meta_fields_and_final_answer_fragment(self):
        run = self.root / "runs" / "run_meta-1"
        write_json(run / "meta.json",
                   {"run_id": "run_meta-1", "agent_id": "codex", "backend": "codex-cli",
                    "model": "gpt-6", "cwd": "/work/repo", "task": "answer me",
                    "launched_at": "2026-01-10T00:00:00Z",
                    "finished_at": "2026-01-10T00:05:00Z",
                    "status": "completed", "exit_code": 0,
                    "native_session_id": "native-top-1",
                    "session_handle": {"thread_id": "native-handle-1"},
                    "launcher": "codex", "launcher_instance": "run:parent-9",
                    "continued_from": "run_prev-2"})
        (run / "final.txt").write_text("the saved final answer\n", encoding="utf-8")
        write_jsonl(run / "normalized" / "events.jsonl", [
            {"type": "run_started", "ts": "2026-01-10T00:00:01Z", "agent": "codex"},
            {"type": "end", "ts": "2026-01-10T00:05:00Z", "status": "completed"},
        ])
        store = sessions.Store("porch", "runs", self.root / "runs", "run_registry")
        ref = list(sessions.porch_iter_sessions(store))[0]
        # top-level native_session_id wins over session_handle.thread_id
        self.assertEqual(ref.extra["native_session"], "native-top-1")
        self.assertEqual(ref.extra["launcher"], "codex")
        self.assertEqual(ref.extra["launcher_instance"], "run:parent-9")
        self.assertEqual(ref.extra["continued_from"], "run_prev-2")
        self.assertEqual(ref.extra["exit_code"], 0)
        self.assertEqual(ref.extra["status"], "completed")
        self.assertEqual(ref.extra["finished_at"], "2026-01-10T00:05:00Z")
        out = list(sessions.porch_iter_fragments(store, ref, 400, {}))
        finals = [f for f in out if f.get("native_type") == "final.txt"]
        self.assertEqual(len(finals), 1)
        final = finals[0]
        self.assertEqual(final["kind"], "assistant")
        self.assertEqual(final["authorship"], "agent")
        self.assertEqual(final["text"], "the saved final answer\n")
        self.assertEqual(final["locator"]["file"], str(run / "final.txt"))
        self.assertIn("final answer", final["basis"])
        # emitted after the run's other fragments
        self.assertEqual(out[-1]["native_type"], "final.txt")

    def test_native_session_falls_back_to_session_handle(self):
        run = self.root / "runs" / "run_handle-1"
        write_json(run / "meta.json",
                   {"run_id": "run_handle-1", "task": "t",
                    "session_handle": {"thread_id": "handle-only"}})
        store = sessions.Store("porch", "runs", self.root / "runs", "run_registry")
        ref = list(sessions.porch_iter_sessions(store))[0]
        self.assertEqual(ref.extra["native_session"], "handle-only")


class EnrichmentTests(FixtureMixin, unittest.TestCase):
    """error / usage / interrupted fields propagated from native records."""

    def test_codex_error_interrupted_usage(self):
        sid = "88887777-6666-5555-4444-333322221111"
        path = self.root / "codex" / "2026" / "02" / "01" / f"rollout-2026-02-01T00-00-00-{sid}.jsonl"
        write_jsonl(path, [
            {"timestamp": "2026-02-01T00:00:00Z", "type": "session_meta",
             "payload": {"id": sid, "cwd": "/w", "model_provider": "openai"}},
            {"timestamp": "2026-02-01T00:00:01Z", "type": "event_msg",
             "payload": {"type": "user_message", "turn_id": "t1", "message": "run it"}},
            {"timestamp": "2026-02-01T00:00:02Z", "type": "response_item",
             "payload": {"type": "function_call", "name": "shell", "call_id": "c1", "arguments": "{}"}},
            {"timestamp": "2026-02-01T00:00:03Z", "type": "response_item",
             "payload": {"type": "function_call_output", "call_id": "c1",
                         "output": json.dumps({"output": "boom", "metadata": {"exit_code": 1}})}},
            {"timestamp": "2026-02-01T00:00:04Z", "type": "event_msg",
             "payload": {"type": "turn_aborted", "turn_id": "t1"}},
            {"timestamp": "2026-02-01T00:00:05Z", "type": "event_msg",
             "payload": {"type": "token_count", "info": {"total_token_usage": {"input": 10}}}},
        ])
        store = sessions.Store("codex", "sessions", self.root / "codex", "jsonl_dir")
        ref = list(sessions.codex_iter_sessions(store))[0]
        out = list(sessions.codex_iter_fragments(store, ref, 400, {}))
        err = [f for f in out if f.get("error")]
        self.assertTrue(any(f["kind"] == "tool_result" for f in err))
        self.assertTrue(any(f.get("interrupted") for f in out))
        self.assertTrue(any(f.get("usage") for f in out))

    def test_claude_is_error_and_usage(self):
        proj = self.root / "claude" / "-p"
        sid = "12345678-1234-1234-1234-1234567890ab"
        write_jsonl(proj / f"{sid}.jsonl", [
            {"type": "user", "timestamp": "2026-02-01T00:00:00Z", "sessionId": sid,
             "origin": {"kind": "human"}, "message": {"role": "user", "content": "go"}},
            {"type": "assistant", "timestamp": "2026-02-01T00:00:01Z", "sessionId": sid,
             "message": {"role": "assistant", "model": "claude-opus-5",
                         "usage": {"input_tokens": 5, "output_tokens": 9},
                         "content": [{"type": "tool_use", "id": "t1", "name": "Bash",
                                      "input": {"command": "false"}}]}},
            {"type": "user", "timestamp": "2026-02-01T00:00:02Z", "sessionId": sid,
             "message": {"role": "user", "content": [
                 {"type": "tool_result", "tool_use_id": "t1", "is_error": True,
                  "content": "exit 1"}]}},
        ])
        store = sessions.Store("claude-code", "projects", self.root / "claude", "jsonl_dir")
        ref = list(sessions.claude_iter_sessions(store))[0]
        out = list(sessions.claude_iter_fragments(store, ref, 400, {}))
        self.assertTrue(any(f.get("error") and f["kind"] == "tool_result" for f in out))
        self.assertTrue(any((f.get("usage") or {}).get("output") == 9 for f in out))


def afrag(kind, authorship=None, text=None, tool=None, inp=None, error=False,
          ts=None, seq=0, usage=None, interrupted=False, status=None):
    f = {"kind": kind, "seq": seq, "native_type": "test",
         "locator": {"file": "fx", "line": seq}}
    if text is not None:
        f["text"] = text
    if authorship:
        f["authorship"] = authorship
    if tool:
        f["rel"] = {"tool": tool}
        f["text"] = json.dumps({"name": tool, "input": inp or {}})
    if error:
        f["error"] = True
    if ts:
        f["ts"] = ts
    if usage:
        f["usage"] = usage
    if interrupted:
        f["interrupted"] = True
    if status:
        f["status"] = status
    return f


class AnalyzeTests(FixtureMixin, unittest.TestCase):
    """Turn/flags/stats projections over synthetic fragment streams."""

    def run_session(self, frags, store_name="s"):
        import analyze
        store = sessions.Store("h", store_name, self.root, "jsonl_dir")
        ref = sessions.SessionRef(harness="h", store=store_name, session_id="sess-1",
                                  locator={}, title="t", cwd="/w")
        return analyze.analyze_session(store, ref, frags)

    def test_turn_boundaries_and_prompt_merge(self):
        frags = [
            afrag("prompt", "human", "first", seq=1),
            afrag("prompt", "human", "second same request", seq=2),  # merged
            afrag("assistant", "agent", "reply", seq=3, ts="2026-01-01T00:00:10Z"),
            afrag("prompt", "human", "next task", seq=4, ts="2026-01-01T00:00:20Z"),
            afrag("assistant", "agent", "reply2", seq=5, ts="2026-01-01T00:00:30Z"),
        ]
        rows, flags = self.run_session(frags)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["turn"], 1)
        self.assertEqual(rows[0]["user_msgs"], 2)
        self.assertTrue(rows[0]["has_final"])
        self.assertEqual(rows[1]["turn"], 2)

    def test_technical_prompts_do_not_trigger(self):
        frags = [
            afrag("context", "system", "sys", seq=1),
            afrag("prompt", "system", "auto-injection", seq=2),
            afrag("prompt", "agent", "subagent task", seq=3),
            afrag("prompt", "human", "real prompt", seq=4),
            afrag("assistant", "agent", "ok", seq=5),
        ]
        rows, _ = self.run_session(frags)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["user_msgs"], 1)

    def test_flags_full_session(self):
        frags = [
            afrag("prompt", "human", "do it", seq=1, ts="2026-01-01T00:00:00Z"),
            afrag("tool_call", tool="Read", inp={"file_path": "/a.py"}, seq=2),
            afrag("tool_result", seq=3),
        ]
        # 5 consecutive search calls: search_loop; 3 identical Greps: retry_loop
        frags += [afrag("tool_call", tool="Grep", inp={"pattern": "x"}, seq=4 + i) for i in range(3)]
        frags += [afrag("tool_result", error=True, seq=7),
                  afrag("tool_call", tool="Read", inp={"file_path": "/b.py"}, seq=8),
                  afrag("tool_call", tool="Read", inp={"file_path": "/c.py"}, seq=9)]
        # mutation on a path never read
        frags += [afrag("tool_call", tool="Write", inp={"file_path": "/new.py"}, seq=10),
                  afrag("tool_result", seq=11),
                  afrag("assistant", "agent", "done", seq=12)]
        # permission friction + compaction + interrupt
        frags += [afrag("permission", "system", seq=13 + i) for i in range(3)]
        frags += [afrag("compaction", "system", seq=16),
                  afrag("metadata", "system", seq=17, interrupted=True)]
        # correction prompt → turn 2; then unanswered prompt → abandoned
        frags += [afrag("prompt", "human", "that's not what I asked, revert it", seq=18),
                  afrag("assistant", "agent", "ok reverted", seq=19),
                  afrag("prompt", "human", "and also …", seq=20)]
        rows, flags = self.run_session(frags)
        pats = {f["pattern"] for f in flags}
        self.assertIn("retry_loop", pats)
        self.assertIn("search_loop", pats)
        self.assertIn("edit_without_read", pats)
        self.assertIn("correction", pats)
        self.assertIn("permission_friction", pats)
        self.assertIn("context_pressure", pats)
        self.assertIn("interrupted", pats)
        self.assertIn("abandoned", pats)
        self.assertEqual(len(rows), 3)
        self.assertFalse(rows[-1]["has_final"])
        # evidence locators always present for evidence-bearing flags
        ev = [f for f in flags if f["pattern"] == "retry_loop"][0]
        self.assertTrue(ev["evidence"])
        self.assertIn("file", ev["evidence"][0]["locator"])
        self.assertIn("seq", ev["evidence"][0])

    def test_retry_loop_step_collapsing(self):
        # calls fanned out from one raw record (same locator) = one parallel
        # step — must NOT count as a retry run (grok batch-edit false positive)
        one_loc = {"file": "fx", "line": 7}
        frags = [afrag("prompt", "human", "go", seq=1)]
        for i, p in enumerate(("/a.py", "/b.py", "/c.py")):
            f = afrag("tool_call", tool="Write", inp={"file_path": p}, seq=2 + i)
            f["locator"] = one_loc
            frags.append(f)
        frags.append(afrag("assistant", "agent", "done", seq=6))
        _, flags = self.run_session(frags)
        self.assertNotIn("retry_loop", {f["pattern"] for f in flags})
        # same call across three DISTINCT steps IS a retry loop
        frags = [afrag("prompt", "human", "go", seq=1)]
        for i in range(3):
            frags.append(afrag("tool_call", tool="Bash", inp={"command": "ls"},
                               seq=2 + i * 2))
            frags.append(afrag("tool_result", seq=3 + i * 2))
        _, flags = self.run_session(frags)
        self.assertIn("retry_loop", {f["pattern"] for f in flags})

    def test_retry_loop_unparseable_input(self):
        # truncated/unparseable args must not collapse all calls into sig="{}"
        frags = [afrag("prompt", "human", "go", seq=1)]
        for i, name in enumerate(("/x.py", "/y.py", "/z.py")):
            f = afrag("tool_call", tool="Write", seq=2 + i)
            f["text"] = '{"name":"Write","arguments":"{\\"file_path\\": \\"' + name  # truncated JSON
            frags.append(f)
        _, flags = self.run_session(frags)
        self.assertNotIn("retry_loop", {f["pattern"] for f in flags})

    def test_error_burst(self):
        frags = [afrag("prompt", "human", "go", seq=1)]
        frags += [afrag("tool_result", error=True, seq=2 + i) for i in range(3)]
        frags += [afrag("assistant", "agent", "x", seq=6)]
        _, flags = self.run_session(frags)
        self.assertIn("error_burst", {f["pattern"] for f in flags})

    def test_stats_grouping_and_dedup(self):
        import analyze
        root = self.root / "claude"
        sid = "ded00000-0000-0000-0000-00000000000a"
        proj = root / "-p"
        write_jsonl(proj / f"{sid}.jsonl", [
            {"type": "user", "timestamp": "2026-03-01T00:00:00Z", "sessionId": sid,
             "origin": {"kind": "human"}, "message": {"role": "user", "content": "hi"}},
            {"type": "assistant", "timestamp": "2026-03-01T00:00:05Z", "sessionId": sid,
             "message": {"role": "assistant", "model": "claude-x",
                         "content": [{"type": "text", "text": "hey"}]}},
        ])
        # two stores over the same tree → session counted once
        stores = [sessions.Store("claude-code", "projects", root, "jsonl_dir"),
                  sessions.Store("claude-code", "dup", root, "jsonl_dir")]
        for s in stores:
            s.status = "ok"
        args = type("A", (), {"cwd": None, "since": None, "until": None, "max_chars": 400})
        stats: dict[str, int] = {}
        seen = [(st.name, r.session_id) for st, r, f in analyze.iter_dedup_sessions(stores, args, stats)]
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0][0], "projects")
        self.assertEqual(stats.get("deduped"), 1)

        # cmd_stats aggregates over the deduped stream
        import contextlib
        import io
        import unittest.mock as m
        ns = type("N", (), {})()
        ns.agent = "claude-code"; ns.store = None; ns.cwd = None
        ns.since = None; ns.until = None; ns.max_chars = 400; ns.limit = 0; ns.by = "model"
        with m.patch.object(analyze.S, "selected_stores", return_value=stores):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                analyze.cmd_stats(ns)
        rows = [json.loads(l) for l in buf.getvalue().splitlines()]
        grp = next(r for r in rows if r.get("group") == "claude-x")
        self.assertEqual(grp["sessions"], 1)  # dedup applied
        self.assertEqual(grp["turns"], 1)
        self.assertEqual(grp["sessions_no_model"], 0)
        self.assertTrue(rows[-1]["_summary"])
        self.assertIn("dedup_rule", rows[-1])


class CliAnalyticsTests(FixtureMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1")

    def run_cli(self, *args, env_extra=None):
        env = dict(self.env)
        env.update(env_extra or {})
        return subprocess.run(
            [sys.executable, str(LIB / "sessions.py"), *args],
            capture_output=True, text=True, encoding="utf-8", timeout=30, env=env)

    def lines(self, proc):
        return [json.loads(l) for l in proc.stdout.splitlines() if l.strip()]

    def test_turns_flags_stats_cli(self):
        proj = self.root / "claude" / "-p"
        sid = "aaaabbbb-0000-1111-2222-333344445555"
        write_jsonl(proj / f"{sid}.jsonl", [
            {"type": "user", "timestamp": "2026-03-02T00:00:00Z", "sessionId": sid,
             "origin": {"kind": "human"}, "message": {"role": "user", "content": "build it"}},
            {"type": "assistant", "timestamp": "2026-03-02T00:00:05Z", "sessionId": sid,
             "message": {"role": "assistant", "model": "claude-opus-5",
                         "content": [{"type": "tool_use", "id": "t1", "name": "Read",
                                      "input": {"file_path": "/x.py"}}]}},
            {"type": "user", "timestamp": "2026-03-02T00:00:06Z", "sessionId": sid,
             "message": {"role": "user", "content": [
                 {"type": "tool_result", "tool_use_id": "t1", "is_error": True,
                  "content": "no such file"}]}},
            {"type": "user", "timestamp": "2026-03-02T00:00:30Z", "sessionId": sid,
             "origin": {"kind": "human"},
             "message": {"role": "user", "content": "that's not right, undo it"}},
        ])
        env = {"PORCH_HISTORY_ROOT_CLAUDE_CODE": str(self.root / "claude")}

        proc = self.run_cli("turns", "-a", "claude-code", env_extra=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        rows = [r for r in self.lines(proc) if not r.get("_summary")]
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["turn"], 1)
        self.assertEqual(rows[0]["tool_errors"], 1)
        self.assertEqual(rows[0]["duration_s"], 6.0)
        self.assertFalse(rows[1]["has_final"])

        proc = self.run_cli("flags", "-a", "claude-code", env_extra=env)
        pats = {r["pattern"] for r in self.lines(proc) if not r.get("_summary")}
        self.assertIn("correction", pats)
        self.assertIn("abandoned", pats)

        proc = self.run_cli("stats", "-a", "claude-code", "--by", "model", env_extra=env)
        rows = self.lines(proc)
        grp = next(r for r in rows if r.get("group") == "claude-opus-5")
        self.assertEqual(grp["turns"], 2)
        self.assertEqual(grp["tool_errors"], 1)


class WindowsPathTests(FixtureMixin, unittest.TestCase):
    """Store paths resolve to %APPDATA%/%LOCALAPPDATA% under Windows."""

    def _stores(self, harness, env):
        env = dict(os.environ, **env)
        env.pop("PORCH_STEER_DIR", None)  # keep the host env out of steer-root resolution
        for key in ("XDG_DATA_HOME", "OPENCODE_DB", "PORCH_HISTORY_ROOT_OPENCODE"):
            env.pop(key, None)
        with patch.object(sessions, "IS_WINDOWS", True), \
             patch.object(sessions, "IS_MACOS", False), \
             patch.object(sessions, "HOME", self.root), \
             patch.dict(os.environ, env, clear=True):
            return sessions.stores_for(harness)

    def test_windows_bases(self):
        appdata = str(self.root / "AppData" / "Roaming")
        localapp = str(self.root / "AppData" / "Local")
        env = {"APPDATA": appdata, "LOCALAPPDATA": localapp}
        cur = self._stores("cursor", env)
        self.assertEqual(cur[0].path,
                         self.root / "AppData" / "Roaming" / "Cursor" / "User" / "globalStorage" / "state.vscdb")
        self.assertEqual([item.name for item in cur if item.name in ("acp", "chats")], ["acp", "chats"])
        self.assertEqual(next(item.path for item in cur if item.name == "chats"),
                         self.root / ".cursor" / "chats")
        cd = self._stores("claude-desktop", env)
        self.assertTrue(str(cd[0].path).startswith(appdata))
        dev = self._stores("devin", env)
        # Both Roaming and LocalAppData candidates are offered (order: roaming first)
        self.assertEqual(len(dev), 4)
        self.assertTrue(str(dev[0].path).startswith(appdata))
        self.assertTrue(str(dev[2].path).startswith(localapp))
        oc = self._stores("opencode", env)
        self.assertEqual(oc[0].path, self.root / ".local" / "share" / "opencode" / "opencode.db")
        with patch.object(sessions, "IS_WINDOWS", True), \
             patch.object(sessions, "IS_MACOS", False), \
             patch.object(sessions, "HOME", self.root), \
             patch.dict(os.environ, {**env, "PORCH_STEER_DIR": ""}, clear=False):
            self.assertTrue(str(sessions._steer_root()).startswith(localapp))

    def test_sqlite_uri_windows_path(self):
        # file: URI must use forward slashes even for drive-letter paths
        db = self.root / "sub dir" / "x.db"
        db.parent.mkdir(parents=True)
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE t (a)")
        conn.close()
        c = sessions.open_sqlite_ro(db)
        self.assertIsNotNone(c)
        c.close()


class SessionSafeguardTests(FixtureMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1",
                        PORCH_STEER_DIR=str(self.root / "cache"))

    def child(self, code, **extra):
        env = dict(self.env, **extra)
        return subprocess.Popen([sys.executable, "-c", code], env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def test_budget_kills_process_and_keeps_partial_output_without_summary(self):
        code = f'''import sys, os
sys.path.insert(0, {str(LIB)!r})
import sessions
baseline = sessions._memory_usage_bytes()
os.environ[sessions.MEMORY_ENV] = str(baseline // (1024 * 1024) + 32)
def roots(_args):
    print('{{"partial":true}}', flush=True)
    data = bytearray(96 * 1024 * 1024)
    for i in range(0, len(data), 4096): data[i] = 1
    while True: time.sleep(.05)
import time
sessions.cmd_roots = roots
sys.argv = ["sessions", "roots"]
sessions.main()
'''
        started = time.monotonic()
        proc = self.child(code, PORCH_SESSIONS_MEMORY_LIMIT_MB="80")
        stdout, stderr = proc.communicate(timeout=8)
        self.assertLess(time.monotonic() - started, 2.0, "memory guard did not stop promptly")
        self.assertEqual(proc.returncode, sessions.EXIT_MEMORY_LIMIT, stderr)
        self.assertIn('"partial":true', stdout)
        self.assertNotIn('"_summary"', stdout)
        self.assertIn("porch sessions roots: memory budget exceeded", stderr)
        self.assertIn("footprint", stderr)
        self.assertIn("--file", stderr)
        self.assertIn("output above is incomplete", stderr)

    def test_budget_under_limit_and_zero_are_untouched(self):
        code = f'''import sys
sys.path.insert(0, {str(LIB)!r})
import sessions
sessions.cmd_roots = lambda _args: (print('{{"ok":true}}') or 0)
sys.argv = ["sessions", "roots"]
raise SystemExit(sessions.main())
'''
        for limit in ("256", "0"):
            proc = subprocess.run([sys.executable, "-c", code], env=dict(self.env, PORCH_SESSIONS_MEMORY_LIMIT_MB=limit),
                                  capture_output=True, text=True, timeout=5)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(proc.stdout.strip(), '{"ok":true}')

    def test_malformed_budget_is_refused(self):
        for name in (sessions.MEMORY_ENV, sessions.PARALLEL_ENV, sessions.QUEUE_TIMEOUT_ENV):
            with self.subTest(name=name):
                proc = subprocess.run([sys.executable, str(LIB / "sessions.py"), "roots"],
                                      env=dict(self.env, **{name: "1.5"}),
                                      capture_output=True, text=True, timeout=5)
                self.assertNotEqual(proc.returncode, 0)
                self.assertIn(f"{name} must be a non-negative integer", proc.stderr)

    def test_queue_fifo_capacity_and_all_callers_finish(self):
        code = f'''import sys, time
sys.path.insert(0, {str(LIB)!r})
import sessions
with open(sys.argv[1], "a") as f: f.write(f"request {{sys.argv[2]}} {{time.monotonic_ns()}}\\n")
slot = sessions._acquire_scan_slot(2, 10)
with open(sys.argv[1], "a") as f: f.write(f"enter {{sys.argv[2]}} {{time.monotonic_ns()}}\\n")
time.sleep(1.0)
with open(sys.argv[1], "a") as f: f.write(f"exit {{sys.argv[2]}} {{time.monotonic_ns()}}\\n")
'''
        events = self.root / "events"
        procs = []
        for n in range(5):
            env = dict(self.env, PORCH_SESSIONS_MAX_PARALLEL="2", PORCH_SESSIONS_QUEUE_TIMEOUT_S="10")
            procs.append(subprocess.Popen([sys.executable, "-c", code, str(events), str(n)], env=env,
                                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
            # FIFO is defined by the order callers reach the queue, not by
            # launch order: module import time varies under load, so launch
            # the next caller only after this one has asked for a slot.
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and not (
                    events.exists() and f"request {n} " in events.read_text()):
                time.sleep(.01)
            time.sleep(.1)
        for proc in procs:
            stdout, stderr = proc.communicate(timeout=8)
            self.assertEqual(proc.returncode, 0, stdout + stderr)
        rows = [line.split() for line in events.read_text().splitlines()]
        enters = [int(row[1]) for row in rows if row[0] == "enter"]
        self.assertEqual(len(enters), 5)
        self.assertEqual([int(row[1]) for row in rows if row[0] == "enter"], list(range(5)))
        timeline = sorted((int(row[2]), 1 if row[0] == "enter" else -1) for row in rows if row[0] in ("enter", "exit"))
        active = peak = 0
        for _stamp, delta in timeline:
            active += delta
            peak = max(peak, active)
        self.assertEqual(peak, 2)

    def test_queue_burst_stress_keeps_capacity_and_cleans_waiters(self):
        code = f'''import sys, time
sys.path.insert(0, {str(LIB)!r})
import sessions
slot = sessions._acquire_scan_slot(2, 20)
with open(sys.argv[1], "a") as f: f.write(f"enter {{sys.argv[2]}} {{time.monotonic_ns()}}\\n")
time.sleep(.03)
with open(sys.argv[1], "a") as f: f.write(f"exit {{sys.argv[2]}} {{time.monotonic_ns()}}\\n")
'''
        for round_number in range(3):
            events = self.root / f"burst-{round_number}-events"
            procs = []
            for child_number in range(24):
                env = dict(self.env, PORCH_SESSIONS_MAX_PARALLEL="2", PORCH_SESSIONS_QUEUE_TIMEOUT_S="20")
                procs.append(subprocess.Popen([sys.executable, "-c", code, str(events), str(child_number)],
                                              env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
            for proc in procs:
                stdout, stderr = proc.communicate(timeout=25)
                self.assertEqual(proc.returncode, 0, stdout + stderr)
            rows = [line.split() for line in events.read_text().splitlines()]
            self.assertEqual(len([row for row in rows if row[0] == "enter"]), 24)
            timeline = sorted((int(row[2]), 1 if row[0] == "enter" else -1) for row in rows)
            active = peak = 0
            for _stamp, delta in timeline:
                active += delta
                peak = max(peak, active)
            self.assertEqual(peak, 2)
            self.assertEqual(list((self.root / "cache").glob("sessions-waiter-*.lock")), [])

    def test_sigkill_releases_slot_and_timeout_is_reported(self):
        lock_api = sessions._lock_api()
        if lock_api is None:
            self.skipTest("platform has no advisory lock API")
        code = f'''import sys, time
sys.path.insert(0, {str(LIB)!r})
import sessions
slot = sessions._acquire_scan_slot(1, 10)
print("acquired", flush=True)
time.sleep(30)
'''
        owner = self.child(code, PORCH_SESSIONS_MAX_PARALLEL="1")
        self.assertEqual(owner.stdout.readline().strip(), "acquired")
        owner.kill()
        owner.wait(timeout=3)
        owner.stdout.close()
        owner.stderr.close()
        next_code = f'''import sys
sys.path.insert(0, {str(LIB)!r})
import sessions
sessions._acquire_scan_slot(1, 3)
print("acquired")
'''
        follower = subprocess.run([sys.executable, "-c", next_code], env=dict(self.env, PORCH_SESSIONS_MAX_PARALLEL="1"),
                                  capture_output=True, text=True, timeout=5)
        self.assertEqual(follower.returncode, 0, follower.stderr)
        self.assertIn("acquired", follower.stdout)

        held = self.root / "cache" / "sessions-slot-0.lock"
        held.parent.mkdir(parents=True, exist_ok=True)
        handle = held.open("a+b")
        lock_api[0](handle, True)
        try:
            timeout_code = f'''import sys
sys.path.insert(0, {str(LIB)!r})
import sessions
sessions._acquire_scan_slot(1, 1)
'''
            timeout_proc = subprocess.run([sys.executable, "-c", timeout_code],
                                          env=dict(self.env, PORCH_SESSIONS_MAX_PARALLEL="1"),
                                          capture_output=True, text=True, timeout=4)
        finally:
            lock_api[1](handle)
            handle.close()
        self.assertEqual(timeout_proc.returncode, sessions.EXIT_QUEUE_TIMEOUT)
        self.assertIn("timed out waiting for a scan slot", timeout_proc.stderr)
        self.assertIn("PORCH_SESSIONS_QUEUE_TIMEOUT_S", timeout_proc.stderr)

    def test_queue_zero_and_roots_do_not_wait(self):
        lock_api = sessions._lock_api()
        if lock_api is None:
            self.skipTest("platform has no advisory lock API")
        lock_path = self.root / "cache" / "sessions-slot-0.lock"
        lock_path.parent.mkdir(parents=True)
        handle = lock_path.open("a+b")
        lock_api[0](handle, True)
        try:
            started = time.monotonic()
            roots = subprocess.run([sys.executable, str(LIB / "sessions.py"), "roots", "-a", "claude-code"],
                                   env=dict(self.env, PORCH_SESSIONS_MAX_PARALLEL="2",
                                            PORCH_SESSIONS_QUEUE_TIMEOUT_S="0"),
                                   capture_output=True, text=True, timeout=3)
            elapsed = time.monotonic() - started
            self.assertEqual(roots.returncode, 0, roots.stderr)
            self.assertLess(elapsed, 2)
            disabled = subprocess.run([sys.executable, "-c", f'''import sys; sys.path.insert(0, {str(LIB)!r})
import sessions
print(sessions._acquire_scan_slot(0, 0))'''], env=self.env, capture_output=True, text=True, timeout=3)
            self.assertEqual(disabled.stdout.strip(), "None")
            disabled_cli = subprocess.run([sys.executable, str(LIB / "sessions.py"), "list", "-a", "qwen-code"],
                                           env=dict(self.env, PORCH_SESSIONS_MAX_PARALLEL="0",
                                                    PORCH_SESSIONS_QUEUE_TIMEOUT_S="0"),
                                           capture_output=True, text=True, timeout=3)
            self.assertEqual(disabled_cli.returncode, 0, disabled_cli.stderr)
        finally:
            lock_api[1](handle)
            handle.close()


class CliTests(FixtureMixin, unittest.TestCase):
    """End-to-end through argv parsing using PORCH_HISTORY_ROOT_* overrides."""

    def setUp(self):
        super().setUp()
        self.env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1")

    def run_cli(self, *args, env_extra=None):
        env = dict(self.env)
        env.update(env_extra or {})
        return subprocess.run([sys.executable, str(LIB / "sessions.py"), *args],
                              capture_output=True, text=True, encoding="utf-8", timeout=30, env=env)

    def make_claude(self):
        proj = self.root / "claude" / "-work-repo"
        sid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        write_jsonl(proj / f"{sid}.jsonl", [
            {"type": "summary", "summary": "CLI test", "timestamp": "2026-01-11T00:00:00Z"},
            {"type": "user", "timestamp": "2026-01-11T00:00:01Z", "cwd": "/work/repo",
             "sessionId": sid, "uuid": "u1", "origin": {"kind": "human"},
             "message": {"role": "user", "content": "needle in the haystack"}},
            {"type": "assistant", "timestamp": "2026-01-11T00:00:02Z", "sessionId": sid, "uuid": "a1",
             "message": {"role": "assistant", "model": "claude-opus-5",
                         "content": [{"type": "text", "text": "found it"}]}},
        ])
        return self.root / "claude", sid

    def test_list_streams_first_record_to_pipe_reader(self):
        root = self.root / "claude"
        for index in range(600):
            write_jsonl(root / f"project-{index:04d}" / f"session-{index:04d}.jsonl", [
                {"type": "user", "timestamp": "2026-01-11T00:00:01Z",
                 "sessionId": f"session-{index:04d}", "message": {"role": "user", "content": "record"}},
            ])
        env = dict(self.env, PORCH_HISTORY_ROOT_CLAUDE_CODE=str(root))
        proc = subprocess.Popen([sys.executable, str(LIB / "sessions.py"), "list", "-a", "claude-code"],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
        first_line = proc.stdout.readline()
        self.assertTrue(first_line, "list produced no first record")
        first_record = json.loads(first_line)
        self.assertNotIn("_summary", first_record)
        proc.stdout.close()  # `head -1` closes its pipe after this first line.
        _stdout, stderr = proc.communicate(timeout=10)
        self.assertEqual(proc.returncode, 0, stderr)
        self.assertNotIn("Traceback", stderr)
        self.assertEqual(stderr, "")

    def lines(self, proc):
        return [json.loads(l) for l in proc.stdout.splitlines() if l.strip()]

    def test_list_grep_show_around(self):
        root, sid = self.make_claude()
        env = {"PORCH_HISTORY_ROOT_CLAUDE_CODE": str(root)}

        proc = self.run_cli("list", "-a", "claude-code", env_extra=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        rows = self.lines(proc)
        self.assertEqual(rows[0]["session_id"], f"-work-repo/{sid}")
        self.assertTrue(rows[-1]["_summary"])
        self.assertTrue(rows[-1]["scan_complete"])

        proc = self.run_cli("grep", "-a", "claude-code", "needle", env_extra=env)
        hits = [r for r in self.lines(proc) if not r.get("_summary")]
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["kind"], "prompt")
        self.assertEqual(hits[0]["authorship"], "human")
        self.assertIn("line", hits[0]["locator"])

        proc = self.run_cli("show", f"claude-code:-work-repo/{sid}", env_extra=env)
        rows = self.lines(proc)
        self.assertEqual(rows[0]["_session"]["title"], "CLI test")
        seqs = [r["seq"] for r in rows if "seq" in r]
        target = seqs[0]
        proc = self.run_cli("show", f"claude-code:-work-repo/{sid}",
                            "--around", str(target), "--context", "1", env_extra=env)
        rows = self.lines(proc)
        seqs2 = [r["seq"] for r in rows if "seq" in r]
        self.assertIn(target, seqs2)

    def test_index_find_and_show_at_cli(self):
        root = self.root / 'codex_home' / 'sessions'
        sid = 'fixture-thread'
        write_jsonl(root / f'rollout-2026-01-01T00-00-00-{sid}.jsonl', [
            {'type':'session_meta','payload':{'id':sid}},
            {'type':'event_msg','payload':{'type':'user_message','message':'unique fixture needle'}},
        ])
        env = {'PORCH_HISTORY_ROOT_CODEX':str(root), 'CODEX_HOME':str(root.parent),
               'PORCH_SESSIONS_INDEX_DIR':str(self.root/'index')}
        result = self.run_cli('find','needle','fixture|absent','-a','codex',env_extra=env)
        self.assertEqual(result.returncode,0,result.stderr)
        rows = self.lines(result)
        self.assertEqual(rows[0]['native_id'],sid)
        self.assertEqual(rows[-1]['matched_sessions'],1)
        self.assertEqual(rows[-1]['alternatives'][1][1]['sessions'],0)
        locator = rows[0]['evidence']['needle']['locator']
        shown = self.run_cli('show','--at',locator,env_extra=env)
        self.assertEqual(shown.returncode,0,shown.stderr)
        self.assertTrue(any(r.get('text') == 'unique fixture needle' for r in self.lines(shown)))
        invalid = self.run_cli('find','needle|','-a','codex',env_extra=env)
        self.assertNotEqual(invalid.returncode,0)
        self.assertIn('empty alternative',invalid.stderr)

    def test_codex_uuid_suffix_list_grep_and_show(self):
        home = self.root / 'codex_home'
        root = home / 'sessions'
        sid = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
        suffix = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'
        write_jsonl(root / f'rollout-2026-01-01T00-00-00-{sid}_{suffix}.jsonl', [
            {'type':'session_meta','payload':{'id':sid}},
            {'type':'event_msg','payload':{'type':'user_message','message':'fixture needle'}},
        ])
        env = {'PORCH_HISTORY_ROOT_CODEX':str(root), 'CODEX_HOME':str(home)}
        listed = self.run_cli('list','-a','codex',env_extra=env)
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertEqual([r['session_id'] for r in self.lines(listed) if not r.get('_summary')], [sid])
        grep = self.run_cli('grep','-a','codex','needle',env_extra=env)
        self.assertEqual(grep.returncode, 0, grep.stderr)
        self.assertEqual([r['session_id'] for r in self.lines(grep) if not r.get('_summary')], [sid])
        shown = self.run_cli('show','codex:'+sid,env_extra=env)
        self.assertEqual(shown.returncode, 0, shown.stderr)
        self.assertTrue(any(r.get('text') == 'fixture needle' for r in self.lines(shown)))

    def test_missing_store_reports_not_crash(self):
        proc = self.run_cli("list", "-a", "qwen-code",
                            env_extra={"PORCH_HISTORY_ROOT_QWEN_CODE": str(self.root / "nope")})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        summary = self.lines(proc)[-1]
        self.assertEqual(summary["stores"][0]["status"], "missing")
        self.assertTrue(summary["scan_complete"])

    def test_list_limit_reports_incomplete_scan(self):
        root, _ = self.make_claude()
        write_jsonl(root / "other" / "other.jsonl", [
            {"type": "user", "message": {"role": "user", "content": "another"}},
        ])
        proc = self.run_cli("list", "-a", "claude-code", "--limit", "1",
                            env_extra={"PORCH_HISTORY_ROOT_CLAUDE_CODE": str(root)})
        self.assertFalse(self.lines(proc)[-1]["scan_complete"])

    def test_title_exact_cwd_and_grouped_search(self):
        root, sid = self.make_claude()
        write_jsonl(root / "other" / "other.jsonl", [
            {"type": "user", "cwd": "/work/repo-copy", "message": {"role": "user", "content": "needle"}},
        ])
        env = {"PORCH_HISTORY_ROOT_CLAUDE_CODE": str(root)}
        proc = self.run_cli("list", "-a", "claude-code", "--title", "cli TEST", env_extra=env)
        rows = self.lines(proc)
        self.assertEqual(rows[0]["session_id"], f"-work-repo/{sid}")
        self.assertEqual(len(rows), 2)
        proc = self.run_cli("grep", "-a", "claude-code", "--cwd", "/work/repo", "--cwd-exact",
                            "--group-by-session", "needle", env_extra=env)
        rows = self.lines(proc)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["_session"]["session_id"], f"-work-repo/{sid}")
        self.assertEqual(rows[0]["matches"][0]["text"], "needle in the haystack")
        self.assertTrue(rows[-1]["scan_complete"])
        proc = self.run_cli("grep", "--cwd-exact", "needle", env_extra=env)
        self.assertNotEqual(proc.returncode, 0)

    def test_grep_searches_full_text_and_returns_match_excerpt(self):
        root = self.root / "claude"
        write_jsonl(root / "project" / "long.jsonl", [
            {"type": "user", "timestamp": "2026-01-11T00:00:01Z",
             "sessionId": "long", "message": {"role": "user",
             "content": "x" * 600 + "Needle-123" + "y" * 600}},
        ])
        env = {"PORCH_HISTORY_ROOT_CLAUDE_CODE": str(root)}
        for query in [("needle", "-i"), ("Needle-[0-9]+", "--regex")]:
            proc = self.run_cli("grep", "-a", "claude-code", *query, env_extra=env)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            rows = self.lines(proc)
            self.assertEqual(len(rows), 2)
            self.assertIn("Needle", rows[0]["text"])
            self.assertLessEqual(len(rows[0]["text"]), 400)
            self.assertGreater(rows[0]["text_offset"], 0)
            self.assertTrue(rows[0]["truncated"])
            self.assertTrue(rows[-1]["scan_complete"])
        proc = self.run_cli("grep", "-a", "claude-code", "--max-chars", "0",
                            "Needle", env_extra=env)
        self.assertEqual(len(self.lines(proc)[0]["text"]), 1210)

    def test_grep_case_insensitive_excerpt_uses_original_unicode_offsets(self):
        root = self.root / "claude"
        write_jsonl(root / "p" / "unicode.jsonl", [{"type": "user", "message": {
            "role": "user", "content": "İ" * 600 + "Needle" + "x" * 600}}])
        proc = self.run_cli("grep", "-a", "claude-code", "-i", "--max-chars", "30", "needle",
                            env_extra={"PORCH_HISTORY_ROOT_CLAUDE_CODE": str(root)})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("Needle", self.lines(proc)[0]["text"])

    def test_no_db_or_index_created(self):
        root, sid = self.make_claude()
        env = {"PORCH_HISTORY_ROOT_CLAUDE_CODE": str(root)}
        before = {str(p) for p in self.root.rglob("*")}
        proc = self.run_cli("grep", "-a", "claude-code", "needle", env_extra=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        after = {str(p) for p in self.root.rglob("*")}
        self.assertEqual(before, after)


# --- grep raw prefilter ------------------------------------------------------

# Characters that stress the prefilter's reasoning: JSON escapes, case
# mappings that are not one-to-one, astral characters, separators.
TRICKY_CHARS = (
    "abcdefnrtuxyzKSI0123456789 ,:{}[]./-_+\"\\'\n\t\x1b\x00"
    "ПриветЁёжЖ" "Σσς" "İı\u212a\u212bßẞ" "😀\U00010400\U00010428" "\u2028\u0307ﬃ€"
)


def _escape_everything(obj) -> str:
    """JSON with every string character written as \\uXXXX (uppercase hex, surrogate pairs)."""
    if isinstance(obj, str):
        units = obj.encode("utf-16-be")
        return '"' + "".join("\\u%04X" % int.from_bytes(units[i:i + 2], "big")
                             for i in range(0, len(units), 2)) + '"'
    if isinstance(obj, dict):
        return "{" + ",".join(_escape_everything(k) + ":" + _escape_everything(v) for k, v in obj.items()) + "}"
    if isinstance(obj, list):
        return "[" + ",".join(_escape_everything(v) for v in obj) + "]"
    return json.dumps(obj)


def _escape_style(obj) -> str:
    """ensure_ascii JSON with uppercase hex, \\/ for slashes and \\u00XX instead of short escapes."""
    text = json.dumps(obj, separators=(",", ":"))
    short = {"b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t"}

    def repl(m):
        tok = m.group(1)
        if tok in short:
            return "\\u%04X" % ord(short[tok])
        if tok.startswith("u"):
            return "\\u" + tok[1:].upper()
        return "\\" + tok
    text = re.sub(r'\\(u[0-9a-fA-F]{4}|["\\/bfnrt])', repl, text)
    return text.replace("/", "\\/")  # '/' only occurs inside strings in JSON


ENCODERS = (
    lambda r: json.dumps(r, ensure_ascii=False, separators=(",", ":")),   # Node-like
    lambda r: json.dumps(r),                                               # ensure_ascii
    _escape_style,
    _escape_everything,
)


class GrepPrefilterTests(FixtureMixin, unittest.TestCase):
    def rand_text(self, rng, lo=0, hi=14):
        return "".join(rng.choice(TRICKY_CHARS) for _ in range(rng.randint(lo, hi)))

    def rand_value(self, rng, depth=0):
        roll = rng.random()
        if depth > 1 or roll < 0.45:
            return self.rand_text(rng)
        if roll < 0.55:
            return rng.choice([None, True, False, 0, -3, 2.5, 1e-07, 10 ** 20])
        if roll < 0.8:
            return {self.rand_text(rng, 1, 6): self.rand_value(rng, depth + 1) for _ in range(rng.randint(0, 3))}
        return [self.rand_value(rng, depth + 1) for _ in range(rng.randint(0, 3))]

    def claude_records(self, rng):
        t = self.rand_text
        text_or_odd = lambda: rng.choice([t(rng), t(rng), None, 7, {"k": t(rng)}])
        return [
            {"type": "user", "cwd": "/w", "message": {"role": "user", "content": t(rng)}},
            {"type": "user", "message": {"role": "user", "content": [
                {"type": "text", "text": t(rng)},
                {"type": "tool_result", "tool_use_id": "x", "content": rng.choice([
                    t(rng), [{"type": "text", "text": text_or_odd()}, {"type": "text", "text": text_or_odd()}],
                    self.rand_value(rng)])}]}},
            {"type": "assistant", "message": {"role": "assistant", "model": "m", "content": [
                {"type": "text", "text": t(rng)}, {"type": "thinking", "thinking": t(rng)},
                rng.choice([{"type": "tool_use", "id": "i", "name": t(rng, 1, 6), "input": self.rand_value(rng)},
                            {"type": "tool_use", "id": "i"}]),
                {"type": t(rng, 1, 5), t(rng, 1, 4): self.rand_value(rng)}]}},
            {"type": "summary", "summary": t(rng)},
            {"type": "permission-mode", "permissionMode": t(rng)},
            {"type": t(rng, 1, 6), t(rng, 1, 6): self.rand_value(rng), "n": rng.choice([1, 2.0, None, True])},
        ]

    def codex_records(self, rng):
        t = self.rand_text
        text_or_odd = lambda: rng.choice([t(rng), t(rng), None, 3])
        return [
            {"type": "session_meta", "payload": {"id": "s", "cwd": t(rng), "originator": t(rng)}},
            {"type": "turn_context", "payload": {"turn_id": t(rng), "model": t(rng), "effort": t(rng)}},
            {"type": "response_item", "payload": {"type": "message", "role": rng.choice(["user", "assistant", "developer", "other"]),
                                                  "content": [{"type": "input_text", "text": text_or_odd()},
                                                              {"type": "output_text", "text": text_or_odd()}]}},
            {"type": "response_item", "payload": {"type": "reasoning", "summary": [{"text": t(rng)}]}},
            {"type": "response_item", "payload": {"type": "function_call", "name": t(rng, 1, 6),
                                                  "arguments": json.dumps(self.rand_value(rng), ensure_ascii=False)}},
            {"type": "response_item", "payload": {"type": "custom_tool_call", "name": t(rng, 1, 6), "input": t(rng)}},
            {"type": "response_item", "payload": {"type": "function_call_output", "call_id": "c",
                                                  "output": rng.choice([t(rng), self.rand_value(rng)])}},
            {"type": "response_item", "payload": {"type": t(rng, 1, 5), "x": self.rand_value(rng)}},
            {"type": "event_msg", "payload": {"type": "user_message", "message": t(rng)}},
            {"type": "event_msg", "payload": {"type": "agent_message", "message": t(rng)}},
            {"type": "event_msg", "payload": {"type": t(rng, 1, 5), "v": self.rand_value(rng)}},
            {"type": "compacted", "payload": {"message": t(rng)}},
            {"type": t(rng, 1, 5), "payload": self.rand_value(rng)},
        ]

    def fragments(self, harness, path):
        store = sessions.Store(harness, "sessions", path.parent, "jsonl_dir")
        ref = sessions.SessionRef(harness, "sessions", "s", locator={"file": str(path)})
        reader = sessions.READERS[harness][1]
        return [f["text"] for f in reader(store, ref, 0, {}) if isinstance(f.get("text"), str)]

    def test_no_false_negatives_on_randomized_sessions(self):
        import random
        rng = random.Random(20261114)
        checked = active = 0
        for index in range(160):
            harness = ("claude-code", "codex")[index % 2]
            records = self.claude_records(rng) if harness == "claude-code" else self.codex_records(rng)
            records = rng.sample(records, rng.randint(1, len(records)))
            encode = ENCODERS[(index // 2) % len(ENCODERS)]
            path = self.root / f"s{index}.jsonl"
            path.write_text("".join(encode(r) + "\n" for r in records), encoding="utf-8")
            texts = [t for t in self.fragments(harness, path) if t]
            if not texts:
                continue
            for _ in range(25):
                source = rng.choice(texts)
                start = rng.randrange(len(source))
                needle = source[start:start + rng.randint(1, 10)]
                fold = rng.random() < 0.5
                if fold:
                    needle = "".join(rng.choice((c, c.upper(), c.lower(), c.swapcase())) for c in needle)
                    hit = any(needle.lower() in t.lower() for t in texts)
                else:
                    hit = any(needle in t for t in texts)
                prefilter = sessions.RawPrefilter(needle, ignore_case=fold, regex=False)
                checked += hit
                if hit and prefilter.active:
                    active += 1
                    self.assertTrue(prefilter.may_match(path),
                                    f"false negative: {needle!r} fold={fold} encoder={(index // 2) % len(ENCODERS)} "
                                    f"in {path.read_text(encoding='utf-8')!r}")
        # The check must not be vacuous: most drawn needles are matches the prefilter judged.
        self.assertGreater(checked, 3000)
        self.assertGreater(active, checked // 3)

    def test_skips_files_without_the_pattern_and_keeps_escaped_ones(self):
        path = write_jsonl(self.root / "plain.jsonl", [
            {"type": "user", "message": {"role": "user", "content": "nothing to see " * 1000}}])
        for needle, fold in (("Ошибка", False), ("ошибка", True), ("Needle", True), ("x/y", False)):
            self.assertFalse(sessions.RawPrefilter(needle, ignore_case=fold, regex=False).may_match(path), needle)
        escaped = self.root / "escaped.jsonl"
        escaped.write_text(json.dumps({"type": "user", "message": {"role": "user", "content": "Ошибка x/y"}}) + "\n",
                           encoding="utf-8")
        for needle, fold in (("Ошибка", False), ("ОШИБКА", True), ("x/y", False)):
            self.assertTrue(sessions.RawPrefilter(needle, ignore_case=fold, regex=False).may_match(escaped), needle)
        # A match deep inside a long line is found.
        big = write_jsonl(self.root / "big.jsonl", [
            {"type": "user", "message": {"role": "user", "content": "p" * (2 << 20) + "deep needle"}}])
        self.assertTrue(sessions.RawPrefilter("deep needle", ignore_case=False, regex=False).may_match(big))
        self.assertTrue(sessions.RawPrefilter("DEEP NEEDLE", ignore_case=True, regex=False).may_match(big))

    def test_candidate_lines_across_block_boundaries(self):
        import random
        rng = random.Random(3)
        lines, expected = [], []
        for number in range(1, 4001):
            body = "x" * rng.choice([0, 1, 7, 300, 4095, 4096, 9000])
            if rng.random() < 0.05:
                body = body[:rng.randint(0, len(body))] + "needle" + body
                expected.append(number)
            lines.append(body)
        for ending in ("\n", ""):  # with and without a final newline
            path = self.root / f"lines{len(ending)}.jsonl"
            path.write_text("\n".join(lines) + ending, encoding="utf-8")
            prefilter = sessions.RawPrefilter("needle", ignore_case=False, regex=False)
            with patch.object(sessions, "_PREFILTER_CHUNK", 4096):
                found = list(prefilter._candidate_lines(path))
            self.assertEqual(found, [lines[n - 1].encode() for n in expected], repr(ending))

    def test_bookkeeping_records_do_not_keep_a_session(self):
        path = write_jsonl(self.root / "s.jsonl", [
            {"type": "attachment", "attachment": {"type": "skill_listing", "content": "kernel panic helper"}},
            {"type": "user", "message": {"role": "user", "content": "an unrelated prompt"}}])
        store = sessions.Store("claude-code", "s", self.root, "jsonl_dir")
        ref = sessions.SessionRef("claude-code", "s", "s", locator={"file": str(path)})
        prefilter = sessions.RawPrefilter("kernel panic", ignore_case=False, regex=False)
        reader = lambda records: sessions._claude_record_fragments(store, ref, path, records, 0)
        self.assertTrue(prefilter.may_match(path))
        for scope, expected in (("convo", False), ("system", True), ("all", True)):
            wanted = lambda f: sessions.in_scope(f, scope) and "kernel panic" in (f.get("text") or "")
            self.assertEqual(prefilter.session_may_match(path, reader, wanted), expected, scope)
        missing = self.root / "gone.jsonl"
        self.assertTrue(prefilter.session_may_match(missing, reader, lambda f: False))

    def test_compressed_rollouts_are_judged_and_corrupt_ones_kept(self):
        try:
            from compression import zstd
        except ImportError:
            self.skipTest("Zstandard requires Python 3.14+")
        body = (json.dumps({"type": "event_msg", "payload": {"type": "user_message", "message": "zipped words"}}) + "\n").encode()
        good = self.root / "rollout-a.jsonl.zst"
        good.write_bytes(zstd.compress(body))
        corrupt = self.root / "rollout-b.jsonl.zst"
        corrupt.write_bytes(zstd.compress(body)[:-6])
        self.assertTrue(sessions.RawPrefilter("zipped", ignore_case=False, regex=False).may_match(good))
        self.assertFalse(sessions.RawPrefilter("absent", ignore_case=False, regex=False).may_match(good))
        self.assertTrue(sessions.RawPrefilter("absent", ignore_case=False, regex=False).may_match(corrupt))
        store = sessions.Store("codex", "sessions", self.root, "jsonl_dir")
        ref = sessions.SessionRef("codex", "sessions", "a", locator={"file": str(good)})
        self.assertEqual(sessions._raw_prefilter_path(store, ref), good)

    def test_keeps_files_where_the_match_needs_porchs_own_spelling(self):
        # porch writes the control character as \n or \u001b; this writer used \u000A and \u001B.
        claude = self.root / "escapes.jsonl"
        claude.write_text(_escape_style({"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": "i", "name": "T", "input": "a\nzq\x1bwv"}]}}) + "\n", encoding="utf-8")
        texts = self.fragments("claude-code", claude)
        for needle in ("nzq", "u001bwv", "1bwv", "bwv"):
            self.assertTrue(any(needle in t for t in texts), needle)
            self.assertTrue(sessions.RawPrefilter(needle, ignore_case=False, regex=False).may_match(claude), needle)
        # porch names the input of a custom tool call "arguments"; the session never says so.
        codex = write_jsonl(self.root / "rollout-x.jsonl", [{"type": "response_item", "payload": {
            "type": "custom_tool_call", "name": "apply_patch", "input": "*** Begin Patch"}}])
        self.assertTrue(any("rguments" in t for t in self.fragments("codex", codex)))
        prefilter = sessions.RawPrefilter("rguments", ignore_case=False, regex=False)
        self.assertFalse(prefilter.active and not prefilter.may_match(codex))

    def test_patterns_the_prefilter_cannot_judge_turn_it_off(self):
        for needle, fold in (("a.*b", False), ('say "hi"', False), ("a\\b", False), ("line\nbreak", False),
                             ("true", False), ("NULL", True), ("-1.5e+3", False), (": 5", False),
                             ("name", False), ("ARGUMENT", True), ("nput", False), ("", False)):
            prefilter = sessions.RawPrefilter(needle, ignore_case=fold, regex=needle == "a.*b")
            self.assertFalse(prefilter.active, needle)
            self.assertIn("reason", prefilter.report())
        self.assertTrue(sessions.RawPrefilter("final", ignore_case=False, regex=False).active)

    def test_rollout_index_finds_what_the_per_id_glob_found(self):
        home = self.root / "codex-home"
        names = ["2026/01/rollout-2026-01-01T10-00-00-aaaa-bbbb.jsonl",
                 "2026/01/rollout-2026-01-01T10-00-00-aaaa-bbbb.jsonl.zst",
                 "2026/02/rollout-2026-02-01T10-00-00-cccc.jsonl.zst",
                 "rollout-x-dddd.jsonl", "nested/deep/rollout--eeee.jsonl", "other-ffff.jsonl"]
        for name in names:
            (home / "sessions" / name).parent.mkdir(parents=True, exist_ok=True)
            (home / "sessions" / name).write_text("")
        (home / "archived_sessions").mkdir(parents=True)
        (home / "archived_sessions" / "rollout-2025-cccc.jsonl").write_text("")
        (home / "archived_sessions" / "rollout-2025-gggg.jsonl").write_text("")

        def per_id_glob(thread_id):  # the lookup before the index, kept as the reference
            for base in (home / "sessions", home / "archived_sessions"):
                hits = list(base.rglob(f"rollout-*-{thread_id}.jsonl")) + list(base.rglob(f"rollout-*-{thread_id}.jsonl.zst"))
                if hits:
                    return hits[0]
            return None

        with patch.dict(os.environ, {"CODEX_HOME": str(home)}):
            sessions._ROLLOUT_INDEX.clear()
            for thread_id in ("aaaa-bbbb", "bbbb", "cccc", "dddd", "eeee", "ffff", "gggg", "00-cccc", "zzzz"):
                self.assertEqual(sessions._find_codex_rollout(thread_id), per_id_glob(thread_id), thread_id)
        sessions._ROLLOUT_INDEX.clear()

    def test_case_mappings_stay_in_their_plane(self):
        # RawPrefilter folds case from a table over planes 0 and 1 only.
        for cp in range(0x10000, 0x110000):
            low = chr(cp).lower()
            self.assertTrue(all(ord(c) > 0xFFFF for c in low), hex(cp))
            if cp >= 0x20000:
                self.assertEqual(low, chr(cp), hex(cp))
        for cp in range(0x10000):
            if not 0xD800 <= cp <= 0xDFFF:
                self.assertTrue(all(ord(c) <= 0xFFFF for c in chr(cp).lower()), hex(cp))

    def test_null_text_blocks_are_empty_not_none(self):
        self.assertEqual(sessions._tool_result_text({"content": [{"text": None}, {"text": "a"}, {"text": 5}]}), "\na\n5")
        self.assertEqual(sessions._codex_message_text({"content": [{"type": "input_text", "text": None},
                                                                   {"type": "output_text", "text": "b"}]}), "b")

    def grep(self, env, *argv):
        args = sessions.argparse.Namespace(agent=None, store=None, cwd=None, cwd_exact=False, since=None, until=None,
                                           scope="convo", limit=0, max_chars=400, ignore_case=False, regex=False,
                                           group_by_session=False, pattern=None)
        for key, value in argv:
            setattr(args, key, value)
        out = io.StringIO()
        with patch.dict(os.environ, env), contextlib.redirect_stdout(out):
            sessions.cmd_grep(args)
        # split("\n"), not splitlines(): U+2028 inside a JSON string is no line break.
        return [json.loads(line) for line in out.getvalue().split("\n") if line]

    def test_grep_output_is_the_same_with_the_prefilter_off(self):
        import random
        rng = random.Random(7)
        claude, codex = self.root / "claude", self.root / "codex"
        for index in range(12):
            write_jsonl(claude / f"proj{index % 3}" / f"c{index}.jsonl",
                        [{"type": "custom-title", "customTitle": f"title {index}", "cwd": f"/w/{index % 3}"},
                         *self.claude_records(rng),
                         {"type": "attachment", "attachment": {"type": "skill_listing", "content": "listing token"}},
                         {"type": "user", "cwd": "/w", "message": {"role": "user", "content": f"common token {index}"}}])
            write_jsonl(codex / "2026" / f"rollout-2026-01-01T00-00-0{index % 10}-id{index}.jsonl", self.codex_records(rng))
        env = {"PORCH_HISTORY_ROOT_CLAUDE_CODE": str(claude), "PORCH_HISTORY_ROOT_CODEX": str(codex)}
        real = sessions.RawPrefilter
        for argv in ([("pattern", "common token 1")], [("pattern", "COMMON"), ("ignore_case", True)],
                     [("pattern", "Привет")], [("pattern", "token"), ("group_by_session", True), ("scope", "all")],
                     [("pattern", "common"), ("cwd", "/w/1")], [("pattern", "c"), ("agent", "codex"), ("scope", "all")],
                     [("pattern", "listing token")], [("pattern", "listing token"), ("scope", "all")],
                     [("pattern", "common"), ("since", "2026-01-01")]):
            argv = [("agent", "claude-code,codex"), *argv]
            fast = self.grep(env, *argv)
            with patch.object(sessions, "RawPrefilter", lambda p, **kw: real(p, ignore_case=False, regex=True)):
                slow = self.grep(env, *argv)
            self.assertEqual(fast[:-1], slow[:-1], argv)
            self.assertTrue(fast[-1]["prefilter"]["active"] or argv[-1] == ("scope", "all"))
            for key in ("sessions_scanned", "fragments_emitted", "scan_complete"):
                self.assertEqual(fast[-1][key], slow[-1][key], (argv, key))
        rows = self.grep(env, ("agent", "claude-code"), ("pattern", "common token 4"))
        self.assertEqual(rows[0]["session"]["title"], "title 4")
        self.assertEqual(rows[-1]["prefilter"]["skipped_sessions"], 11)

    def test_cwd_filter_reads_only_heads_and_matches_full_listing(self):
        claude = self.root / "claude"
        for index in range(6):
            write_jsonl(claude / "p" / f"s{index}.jsonl", [
                {"type": "user", "cwd": f"/repo/{index % 2}", "timestamp": f"2026-01-0{index + 1}T00:00:00Z",
                 "message": {"role": "user", "content": "x"}},
                {"type": "ai-title", "aiTitle": f"t{index}"},
                {"type": "assistant", "cwd": "/elsewhere", "message": {"role": "assistant", "model": f"m{index}", "content": []}}])
        store = sessions.Store("claude-code", "s", claude, "jsonl_dir")
        full = [r.as_dict() for r in sessions.claude_iter_sessions(store) if r.cwd == "/repo/1"]
        light = [r.as_dict() for r in sessions.iter_all_sessions(store, "/repo/1", None, None)]
        self.assertEqual(light, full)
        self.assertEqual(len(full), 3)
        exact = [r.as_dict() for r in sessions.iter_all_sessions(store, "/repo/1", None, None, cwd_exact=True)]
        self.assertEqual(exact, full)


class SymlinkTraversalTests(FixtureMixin, unittest.TestCase):
    """Discovery follows directory symlinks once; cycles cannot recur."""

    def setUp(self):
        super().setUp()
        self.env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1")

    def run_cli(self, *args, env_extra=None):
        env = dict(self.env)
        env.update(env_extra or {})
        return subprocess.run([sys.executable, str(LIB / "sessions.py"), *args],
                              capture_output=True, text=True, encoding="utf-8", timeout=30, env=env)

    def make_claude_store(self):
        archive = self.root / "archive"
        proj = archive / "-Users-x-proj"
        sid1 = "11111111-1111-4111-8111-111111111111"
        write_jsonl(proj / f"{sid1}.jsonl", [
            {"type": "user", "timestamp": "2026-01-01T00:00:00Z", "cwd": "/x",
             "sessionId": sid1, "message": {"role": "user", "content": "real session"}},
        ])
        write_jsonl(proj / "subagents" / "agent-a1.jsonl", [
            {"type": "user", "timestamp": "2026-01-01T00:01:00Z",
             "message": {"role": "user", "content": "subagent task"}},
        ])
        # a cycle inside the archive dir: points back at the store root
        os.symlink(self.root / "projects", proj / "loop")
        other = archive / "-Users-y-other"
        sid2 = "22222222-2222-4222-8222-222222222222"
        write_jsonl(other / f"{sid2}.jsonl", [
            {"type": "user", "timestamp": "2026-01-02T00:00:00Z", "cwd": "/y",
             "sessionId": sid2, "message": {"role": "user", "content": "linked file"}},
        ])
        projects = self.root / "projects"
        projects.mkdir()
        os.symlink(proj, projects / "-Users-x-proj")       # project bucket is a dir link
        (projects / "-Users-y-other").mkdir()
        os.symlink(other / f"{sid2}.jsonl",
                   projects / "-Users-y-other" / f"{sid2}.jsonl")  # session file is a file link
        os.symlink(projects, projects / "back")           # link to the store root itself
        return projects, [f"-Users-x-proj/{sid1}",
                          "-Users-x-proj/subagents/agent-a1",
                          f"-Users-y-other/{sid2}"]

    def test_claude_code_store_follows_links_once(self):
        projects, expected = self.make_claude_store()
        store = sessions.Store("claude-code", "projects", projects, "jsonl_dir")
        refs = list(sessions.claude_iter_sessions(store))
        ids = [r.session_id for r in refs]
        self.assertEqual(sorted(ids), sorted(expected))
        self.assertEqual(len(ids), len(set(ids)))  # every session exactly once
        # ids are computed from the walked path under the store root
        self.assertTrue(all(not r.session_id.startswith(str(self.root / "archive")) for r in refs))
        # the same set through the list path (iter_all_sessions)
        listed = [r.session_id for r in sessions.iter_all_sessions(store, None, None, None)]
        self.assertEqual(sorted(listed), sorted(expected))

    def test_claude_code_list_cli_sees_linked_sessions(self):
        projects, expected = self.make_claude_store()
        proc = self.run_cli("list", "-a", "claude-code", "--limit", "0",
                            env_extra={"PORCH_HISTORY_ROOT_CLAUDE_CODE": str(projects)})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        rows = [json.loads(l) for l in proc.stdout.splitlines() if l.strip()]
        ids = [r["session_id"] for r in rows if not r.get("_summary")]
        self.assertEqual(sorted(ids), sorted(expected))
        self.assertTrue(rows[-1]["scan_complete"])

    def make_codex_store(self):
        home = self.root / "codex_home"
        sid = "33333333-3333-4333-8333-333333333333"
        archive_day = self.root / "archive" / "2026" / "11" / "15"
        write_jsonl(archive_day / f"rollout-2026-11-15T00-00-00-{sid}.jsonl", [
            {"timestamp": "2026-11-15T00:00:00Z", "type": "session_meta",
             "payload": {"id": sid, "cwd": "/z"}},
            {"timestamp": "2026-11-15T00:00:01Z", "type": "event_msg",
             "payload": {"type": "user_message", "turn_id": "t", "message": "via day link"}},
        ])
        # a cycle inside the linked day dir: points back at the sessions root
        os.symlink(home / "sessions", archive_day / "back")
        day = home / "sessions" / "2026" / "11" / "15"
        day.parent.mkdir(parents=True)
        os.symlink(archive_day, day)
        return home / "sessions", sid

    def test_codex_store_follows_linked_day_dir(self):
        root, sid = self.make_codex_store()
        store = sessions.Store("codex", "sessions", root, "jsonl_dir")
        refs = list(sessions.codex_iter_sessions(store))
        self.assertEqual([r.session_id for r in refs], [sid])
        stats: dict[str, int] = {}
        out = list(sessions.codex_iter_fragments(store, refs[0], 400, stats))
        self.assertTrue(any(f["kind"] == "prompt" and f["text"] == "via day link" for f in out))


class AllFinalsTests(unittest.TestCase):
    def setUp(self):
        self.prompt = {"kind": "prompt", "origin": "human", "text": "question"}
        self.injected = {"kind": "prompt", "origin": "injected", "text": "notification"}

    def assert_pair(self, turns):
        self.assertEqual(len(turns), 1)
        self.assertEqual([f["text"] for f in turns[0]["finals"]], ["one", "two"])
        self.assertEqual(turns[0]["final"]["text"], "two")

    def test_claude_completed_before_interrupt_and_leading(self):
        def answer(mid, text):
            return {"kind": "assistant", "text": text, "native": {"message_id": mid, "stop_reason": "end_turn"}}
        ref = sessions.SessionRef("claude-code", "s", "id")
        self.assert_pair(sessions._claude_turns(ref, [self.prompt, answer("1", "one"), self.injected, answer("2", "two"), {"kind": "context", "interrupted": True}]))
        turns = sessions._claude_turns(ref, [answer("0", "leading"), self.prompt, answer("1", "one")])
        self.assertEqual(turns[0]["prompts"], [])
        self.assertEqual(turns[0]["finals"][0]["text"], "leading")
        sessions.refine_class(ref, turns)
        self.assertEqual(ref.extra["session_class"], "interactive")

    def test_desktop_code_and_cowork(self):
        code = sessions.SessionRef("claude-desktop", "code-sessions", "id", extra={"surface": "code-tab"})
        def a(mid, text):
            return {"kind": "assistant", "text": text, "native": {"message_id": mid, "stop_reason": "end_turn"}}
        self.assert_pair(sessions._desktop_turns(code, [self.prompt, a("1", "one"), self.injected, a("2", "two")]))
        cowork = sessions.SessionRef("claude-desktop", "cowork", "id", extra={"surface": "cowork"})
        result = {"kind": "boundary", "native_type": "result.success"}
        self.assert_pair(sessions._desktop_turns(cowork, [self.prompt, {"kind": "assistant", "text": "one"}, result, self.injected, {"kind": "assistant", "text": "two"}, result]))

    def test_codex_native_turns_and_copy_dedup(self):
        def final(text, layer="response", phase="final_answer"):
            return {"kind": "assistant", "text": text, "native": {"phase": phase, "layer": layer}}
        start = {"kind": "boundary", "native_type": "event_msg.task_started"}
        stop = {"kind": "boundary", "native_type": "event_msg.task_complete"}
        saved = lambda text: final(text, "task_complete", None)
        ref = sessions.SessionRef("codex", "s", "id")
        turns = sessions._codex_turns(ref, [self.prompt, start, final("one"), stop, saved("one"), self.injected,
                                           start, final("two"), stop, saved("two"),
                                           {"kind": "boundary", "native_type": "event_msg.turn_aborted", "native": {"reason": "interrupted"}}])
        self.assert_pair(turns)
        self.assertTrue(turns[0]["aborted"])
        self.assertEqual(turns[0]["abort_reason"], "interrupted")

    def test_devin_multiple_stops(self):
        def f(kind, node, text, finish=None, origin=None):
            return {"kind": kind, "native_type": "message_nodes." + ("assistant" if kind == "assistant" else "user"),
                    "text": text, "origin": origin, "seq": node,
                    "native": {"node_id": node, "canonical": True, "finish_reason": finish, "has_tool_calls": False}}
        ref = sessions.SessionRef("devin", "s", "id")
        self.assert_pair(sessions._devin_turns(ref, [f("prompt", 1, "q", origin="human"), f("assistant", 2, "one", "stop"), f("prompt", 3, "injected", origin="injected"), f("assistant", 4, "two", "stop")]))

    def test_opencode_v1_and_v2(self):
        ref = sessions.SessionRef("opencode", "s", "id", extra={"oc_source": "v1"})
        def f1(text): return [{"kind": "boundary", "native_type": "part.step-start"}, {"kind": "assistant", "text": text}, {"kind": "boundary", "native_type": "part.step-finish", "status": "stop"}]
        self.assert_pair(sessions.opencode_turns(ref, [self.prompt, *f1("one"), self.injected, *f1("two")]))
        ref.extra["oc_source"] = "v2"
        def f2(mid, text): return {"kind": "assistant", "text": text, "native": {"message_id": mid, "completed": True, "finish": "stop", "after_last_tool": True}}
        self.assert_pair(sessions.opencode_turns(ref, [self.prompt, f2("1", "one"), self.injected, f2("2", "two")]))

    def test_agy_multiple_completed_answers(self):
        def a(text): return {"kind": "assistant", "text": text, "native": {"status": "DONE", "has_tool_calls": False}}
        ref = sessions.SessionRef("agy", "s", "id")
        self.assert_pair(sessions.agy_turns(ref, [self.prompt, a("one"), self.injected, a("two")]))

    def test_porch_and_fallback(self):
        ref = sessions.SessionRef("porch", "s", "id")
        final = {"kind": "assistant", "native_type": "final.txt", "text": "saved"}
        turn = sessions._porch_turns(ref, [self.prompt, self.injected, final])[0]
        self.assertEqual(turn["finals"], [final])
        fallback = sessions.turns_fallback(ref, [self.prompt, {"kind": "assistant", "text": "one"}, self.injected, {"kind": "assistant", "text": "two"}])[0]
        self.assertEqual([f["text"] for f in fallback["finals"]], ["two"])
        self.assertEqual(fallback["final_basis"], "fallback")

    def test_leading_native_answers(self):
        cases = [
            (sessions._claude_turns, sessions.SessionRef("claude-code", "s", "id"),
             [{"kind": "assistant", "text": "leading", "native": {"message_id": "m", "stop_reason": "end_turn"}}]),
            (sessions._codex_turns, sessions.SessionRef("codex", "s", "id"),
             [{"kind": "assistant", "text": "leading", "native": {"phase": "final_answer", "layer": "response"}}]),
            (sessions._desktop_turns, sessions.SessionRef("claude-desktop", "cowork", "id", extra={"surface": "cowork"}),
             [{"kind": "assistant", "text": "leading"}, {"kind": "boundary", "native_type": "result.success"}]),
            (sessions._devin_turns, sessions.SessionRef("devin", "s", "id"),
             [{"kind": "assistant", "text": "leading", "native_type": "message_nodes.assistant", "native": {"node_id": 1, "canonical": True, "finish_reason": "stop", "has_tool_calls": False}}]),
            (sessions.opencode_turns, sessions.SessionRef("opencode", "s", "id", extra={"oc_source": "v2"}),
             [{"kind": "assistant", "text": "leading", "native": {"message_id": "m", "completed": True, "finish": "stop", "after_last_tool": True}}]),
            (sessions.agy_turns, sessions.SessionRef("agy", "s", "id"),
             [{"kind": "assistant", "text": "leading", "native": {"status": "DONE", "has_tool_calls": False}}]),
            (sessions._porch_turns, sessions.SessionRef("porch", "s", "id"),
             [{"kind": "assistant", "native_type": "final.txt", "text": "leading"}]),
        ]
        for rule, ref, records in cases:
            with self.subTest(harness=ref.harness):
                leading = rule(ref, records)[0]
                self.assertEqual(leading["prompts"], [])
                self.assertEqual([f["text"] for f in leading["finals"]], ["leading"])


class TurnContractTests(unittest.TestCase):
    """The shared turn contract: fallback rule and delegated-brief signature."""

    def test_fallback_turns(self):
        def p(text, origin=None, ts=None):
            f = {"kind": "prompt", "text": text, "ts": ts}
            if origin:
                f["origin"] = origin
            return f
        a = lambda text: {"kind": "assistant", "text": text}  # noqa: E731
        frags = [
            p("first", "human"), a("thinking aloud"), {"kind": "tool_call", "text": "x"}, a("answer one"),
            p("<system-reminder>", "injected"), a("after reminder"),
            p("second"), {"kind": "tool_result", "text": "y"},
            p("third", "delegated"), a(""),
        ]
        ref = sessions.SessionRef("grok", "s", "id")
        turns = sessions.turns_fallback(ref, frags)
        self.assertEqual([t["prompts"][0]["text"] for t in turns], ["first", "second", "third"])
        # an injected prompt does not split a turn; the last assistant text wins
        self.assertEqual(turns[0]["final"]["text"], "after reminder")
        self.assertEqual(turns[0]["final_basis"], "fallback")
        self.assertIsNone(turns[1]["final"])
        self.assertFalse(turns[1]["open"])  # followed by another prompt: closed without an answer
        self.assertIsNone(turns[2]["final"])
        self.assertTrue(turns[2]["open"])

    def test_delegated_brief_signature(self):
        prefix = sessions.executor_prefix()
        self.assertTrue(prefix)
        self.assertTrue(sessions.is_delegated_brief("\n" + prefix + "\n\nDo the task."))
        self.assertFalse(sessions.is_delegated_brief("Please do the task. " + prefix))
        self.assertFalse(sessions.is_delegated_brief(None))

    def test_refine_class(self):
        for existing, origin, expected, basis in ((None, "human", "interactive", "genuine prompt"),
                                                   ("unknown", "delegated", "headless", "executor-prefix"),
                                                   ("unknown", "unknown", "unknown", "old"),
                                                   ("subagent", "human", "subagent", "old"),
                                                   ("headless", "human", "headless", "old")):
            ref = sessions.SessionRef("devin", "s", "id", extra={"session_class": existing, "class_basis": "old"})
            sessions.refine_class(ref, [{"prompts": [{"origin": origin}]}])
            self.assertEqual((ref.extra["session_class"], ref.extra["class_basis"]), (expected, basis))
        ref = sessions.SessionRef("devin", "s", "id", extra={"session_class": "unknown"})
        sessions.refine_class(ref, [])
        self.assertEqual(ref.extra["session_class"], "unknown")

    def test_reset_discovery_caches(self):
        caches = (sessions._PORCH_WORKERS, sessions._CODEX_HOME_CACHE, sessions._ROLLOUT_INDEX, sessions._CLAUDE_TRANSCRIPTS)
        for cache in caches:
            cache["test"] = None
        sessions.reset_discovery_caches()
        self.assertTrue(all(not cache for cache in caches))


class AgyTests(FixtureMixin, unittest.TestCase):
    """agy (Antigravity CLI): metadata cache + brain transcripts, file-only."""

    def test_preview_and_porch_worker_class(self):
        root = self.make_root()
        self.write_meta(root, {"preview": self.entry(summary={"Preview": "a title preview"}),
                               "worker": self.entry(summary={"Preview": "worker preview", "AppDataDir": "antigravity"}),
                               "child": self.entry(summary={"Preview": "child"})})
        write_json(root / "brain" / "parent" / ".system_generated" / "subagents" / "child.json", {"conversationId": "child"})
        store = sessions.Store("agy", "cli", root, "agy_root")
        with patch.object(sessions, "porch_workers", return_value={("agy", "worker"), ("agy", "child")}):
            refs = {ref.session_id: ref for ref in sessions.agy_iter_sessions(store)}
        self.assertEqual(refs["preview"].extra["session_class"], "unknown")
        self.assertEqual((refs["worker"].extra["session_class"], refs["worker"].extra["class_basis"]), ("headless", "porch registry"))
        self.assertEqual(refs["child"].extra["session_class"], "subagent")

    def make_root(self):
        root = self.root / "agy"
        (root / "cache").mkdir(parents=True)
        (root / "brain").mkdir()
        (root / "annotations").mkdir()
        (root / "conversations").mkdir()
        return root

    def write_transcript(self, root, cid, records):
        path = root / "brain" / cid / ".system_generated" / "logs" / "transcript_full.jsonl"
        write_jsonl(path, records)
        return path

    def write_meta(self, root, convs):
        write_json(root / "cache" / "conversation_metadata.json", {"conversations": convs})

    def entry(self, **kw):
        base = {"summary": {"ID": "x", "Title": "", "Preview": "", "NumSteps": 0,
                            "UpdatedAt": "2020-01-01T00:00:00Z", "WorkspaceURIs": [],
                            "AppDataDir": "", "AgentName": ""}}
        base["summary"].update(kw.pop("summary", {}))
        base.update(kw)
        return base

    def test_sessions_metadata_brain_edges_and_status(self):
        root = self.make_root()
        cli = "cid-cli-1"
        sub = "cid-sub-1"
        ide = "cid-ide-1"
        gone = "cid-gone-1"
        stale = "cid-stale-1"
        # CLI conversation: metadata + transcript + annotation title override.
        self.write_meta(root, {
            cli: self.entry(summary={"Title": "Meta title", "NumSteps": 2,
                                     "WorkspaceURIs": ["file:///work/repo"],
                                     "Preview": "fix the bug"}),
            sub: self.entry(summary={"Title": "Child", "NumSteps": 1}),
            ide: self.entry(summary={"Title": "IDE row", "AppDataDir": "antigravity"}),
            gone: self.entry(summary={"Title": "No transcript", "NumSteps": 3}),
            stale: self.entry(summary={"Title": "Stale", "NumSteps": 10,
                                       "UpdatedAt": "2099-01-01T00:00:00Z"}),
        })
        self.write_transcript(root, cli, [
            {"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT",
             "status": "DONE", "created_at": "2026-11-10T10:00:00Z",
             "content": "<USER_REQUEST>\nFix the bug\n</USER_REQUEST><ADDITIONAL_METADATA>x</ADDITIONAL_METADATA>"},
            {"step_index": 1, "source": "MODEL", "type": "PLANNER_RESPONSE",
             "status": "DONE", "created_at": "2026-11-10T10:00:01Z",
             "content": "on it", "tool_calls": [{"name": "run_command", "args": {}}]},
        ])
        sub_desc = root / "brain" / cli / ".system_generated" / "subagents" / f"{sub}.json"
        write_json(sub_desc, {"conversationId": sub, "subagentDescriptor": {"typeName": "explore"}})
        self.write_transcript(root, sub, [
            {"step_index": 0, "source": "SYSTEM", "type": "SYSTEM_MESSAGE",
             "status": "DONE", "created_at": "2026-11-10T11:00:00Z",
             "content": f"[Message] sender={cli}/task-2 content=<TASK>do it</TASK></SYSTEM_MESSAGE>"},
        ])
        # IDE row keeps a transcript on disk but stays metadata-only.
        self.write_transcript(root, ide, [
            {"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT",
             "status": "DONE", "created_at": "2026-11-10T09:00:00Z",
             "content": "<USER_REQUEST>ide prompt</USER_REQUEST>"},
        ])
        # Stale: 3 transcript lines < NumSteps 10, UpdatedAt ancient.
        self.write_transcript(root, stale, [
            {"step_index": i, "source": "MODEL", "type": "PLANNER_RESPONSE",
             "status": "DONE", "created_at": "2026-11-10T12:00:00Z", "content": "x"}
            for i in range(3)])
        # Annotation title overrides metadata Title for cli.
        (root / "annotations" / f"{cli}.pbtxt").write_text('title:"Annotated title"\n', encoding="utf-8")
        store = sessions.Store("agy", "cli", root, "agy_root")
        refs = {r.session_id: r for r in sessions.agy_iter_sessions(store)}
        self.assertEqual(set(refs), {cli, sub, ide, gone, stale})
        r = refs[cli]
        self.assertEqual(r.title, "Annotated title")
        self.assertEqual(r.cwd, "/work/repo")
        self.assertEqual(r.extra["surface"], "cli")
        self.assertEqual(r.extra["session_class"], "interactive")
        self.assertEqual(r.extra["native_status"]["NumSteps"], 2)
        self.assertNotIn("content_unavailable", r.extra["native_status"])
        roles = [s["role"] for s in r.extra["sources"]]
        self.assertIn("metadata", roles)
        self.assertIn("content", roles)
        child = refs[sub]
        self.assertEqual(child.parent, cli)
        self.assertEqual(child.extra["session_class"], "subagent")
        self.assertEqual(child.extra["edges"][0]["type"], "subagent")
        self.assertEqual(child.extra["edges"][0]["target_id"], cli)
        self.assertEqual(child.agent, "explore")
        ide_ref = refs[ide]
        self.assertEqual(ide_ref.extra["surface"], "ide")
        self.assertTrue(ide_ref.extra["native_status"]["content_unavailable"])
        gone_ref = refs[gone]
        self.assertEqual(gone_ref.extra["surface"], "cli")
        self.assertTrue(gone_ref.extra["native_status"]["content_unavailable"])
        self.assertEqual(gone_ref.extra["session_class"], "unknown")
        # Discovery staleness is the cheap mtime-vs-UpdatedAt check.
        self.assertTrue(refs[stale].extra["native_status"]["content_stale"])
        self.assertNotIn("content_stale", r.extra["native_status"])
        # Post-read step check: transcript steps vs metadata NumSteps.
        cli_frags = list(sessions.agy_iter_fragments(store, r, 400, {}))
        self.assertFalse(sessions.agy_content_stale(r, cli_frags))
        stale_frags = list(sessions.agy_iter_fragments(store, refs[stale], 400, {}))
        self.assertTrue(sessions.agy_content_stale(refs[stale], stale_frags))
        gone_frags = list(sessions.agy_iter_fragments(store, gone_ref, 400, {}))
        self.assertFalse(sessions.agy_content_stale(gone_ref, gone_frags))
        # IDE rows are metadata-only even with a transcript on disk.
        stats = {}
        frags = list(sessions.agy_iter_fragments(store, ide_ref, 400, stats))
        self.assertEqual(len(frags), 1)
        self.assertEqual(frags[0]["kind"], "metadata")
        self.assertEqual(frags[0]["native_type"], "ide_row")
        # Missing transcript still yields one metadata fragment.
        frags = list(sessions.agy_iter_fragments(store, gone_ref, 400, {}))
        self.assertEqual(len(frags), 1)
        self.assertEqual(frags[0]["basis"], "known conversation without transcript")

    def test_fragments_origins_and_turns(self):
        root = self.make_root()
        cid = "cid-turns"
        self.write_meta(root, {cid: self.entry(summary={"Title": "T", "NumSteps": 6})})
        self.write_transcript(root, cid, [
            {"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT",
             "status": "DONE", "created_at": "2026-11-10T10:00:00Z",
             "content": "<ADDITIONAL_METADATA>meta</ADDITIONAL_METADATA>"
                        "<USER_REQUEST>\nFix the bug please\n</USER_REQUEST>"},
            {"step_index": 1, "source": "SYSTEM", "type": "SYSTEM_MESSAGE",
             "status": "DONE", "created_at": "2026-11-10T10:00:01Z",
             "content": "[Message] sender=parent-1/task-9 content=<TASK>delegated brief</TASK></SYSTEM_MESSAGE>"},
            {"step_index": 2, "source": "SYSTEM", "type": "SYSTEM_MESSAGE",
             "status": "DONE", "created_at": "2026-11-10T10:00:02Z",
             "content": "[Message] no sender here"},
            {"step_index": 3, "source": "MODEL", "type": "PLANNER_RESPONSE",
             "status": "DONE", "created_at": "2026-11-10T10:00:03Z",
             "thinking": "hmm", "content": "Looking into it",
             "tool_calls": [{"name": "run_command", "args": {"CommandLine": "ls"}}]},
            {"step_index": 4, "source": "MODEL", "type": "GENERIC",
             "status": "DONE", "created_at": "2026-11-10T10:00:04Z", "content": "out.txt"},
            {"step_index": 5, "source": "MODEL", "type": "PLANNER_RESPONSE",
             "status": "DONE", "created_at": "2026-11-10T10:00:05Z",
             "content": "Fixed. All done.", "tool_calls": []},
            {"step_index": 6, "source": "USER_EXPLICIT", "type": "USER_INPUT",
             "status": "DONE", "created_at": "2026-11-10T10:05:00Z",
             "content": "<USER_REQUEST>\nOne more thing\n</USER_REQUEST>"},
            {"step_index": 7, "source": "MODEL", "type": "PLANNER_RESPONSE",
             "status": "DONE", "created_at": "2026-11-10T10:05:01Z",
             "content": None, "tool_calls": [{"name": "run_command", "args": {}}]},
        ])
        store = sessions.Store("agy", "cli", root, "agy_root")
        ref = next(r for r in sessions.agy_iter_sessions(store) if r.session_id == cid)
        frags = list(sessions.agy_iter_fragments(store, ref, 400, {}))
        prompts = [f for f in frags if f["kind"] == "prompt"]
        # USER_REQUEST only (metadata block excluded), envelope -> delegated,
        # senderless SYSTEM_MESSAGE stays context.
        self.assertEqual([f["origin"] for f in prompts], ["human", "delegated", "human"])
        self.assertEqual(prompts[0]["origin_basis"], "USER_REQUEST")
        self.assertEqual(prompts[1]["origin_basis"], "system-envelope sender")
        self.assertEqual(prompts[1]["rel"]["sender"], "parent-1")
        self.assertTrue(any(f["kind"] == "context" and "no sender" in str(f.get("text"))
                            for f in frags))
        # Non-request USER_INPUT blocks become hidden context.
        self.assertTrue(any(f["kind"] == "context" and "ADDITIONAL_METADATA" in str(f.get("text"))
                            for f in frags))
        # PLANNER_RESPONSE with tool_calls -> assistant + tool_call fragments,
        # native evidence carries step/status/has_tool_calls.
        asst = [f for f in frags if f["kind"] == "assistant"]
        self.assertEqual(len(asst), 3)
        self.assertEqual(asst[0]["native"]["has_tool_calls"], True)
        self.assertEqual(asst[0]["native"]["step"], 3)
        self.assertTrue(any(f["kind"] == "tool_call" and f["rel"]["tool"] == "run_command"
                            for f in frags))
        self.assertTrue(any(f["kind"] == "tool_result" and f["text"] == "out.txt"
                            for f in frags))
        self.assertTrue(any(f["kind"] == "reasoning" for f in frags))
        turns = sessions.interpret_session(store, ref, frags)
        self.assertEqual(len(turns), 3)
        # Turn 1 is the bare human prompt (the delegated envelope starts turn 2).
        self.assertIsNone(turns[0]["final"])
        self.assertFalse(turns[0]["open"])
        # Turn 2 closes on the DONE no-tool PLANNER_RESPONSE before the next prompt.
        self.assertEqual(turns[1]["final"]["text"], "Fixed. All done.")
        self.assertEqual(turns[1]["final_basis"], "native:PLANNER_RESPONSE")
        self.assertFalse(turns[1]["open"])
        # Turn 3 at EOF: last PLANNER_RESPONSE still has tool_calls -> open,
        # never a final (live transcript tail is not an answer).
        self.assertIsNone(turns[2]["final"])
        self.assertTrue(turns[2]["open"])

    def test_store_discovery_override(self):
        root = self.make_root()
        with patch.dict(os.environ, {"PORCH_HISTORY_ROOT_AGY": str(root)}, clear=True):
            stores = sessions.stores_for("agy")
        self.assertEqual(len(stores), 1)
        self.assertEqual(stores[0].harness, "agy")
        # Parent ~/.gemini override resolves to the antigravity-cli child.
        parent = self.root / ".gemini"
        (parent / "antigravity-cli" / "cache").mkdir(parents=True)
        with patch.dict(os.environ, {"PORCH_HISTORY_ROOT_AGY": str(parent)}, clear=True):
            stores = sessions.stores_for("agy")
        self.assertEqual(stores[0].path, parent / "antigravity-cli")


class OpenCodeV1Tests(FixtureMixin, unittest.TestCase):
    """OpenCode 1.x: file storage + legacy SQLite tables, one V1 shape."""

    def write_file_store(self, root):
        storage = root / "storage"
        ses = "ses_v1file"
        write_json(storage / "session" / "proj" / f"{ses}.json", {
            "id": ses, "version": "1.0.0", "projectID": "proj",
            "directory": "/work/one", "title": "File session",
            "time": {"created": 1760000000000, "updated": 1760000001000}})
        msgs = storage / "message" / ses
        parts = storage / "part"
        # user message: synthetic + ignored + genuine text parts
        write_json(msgs / "m_u1.json", {
            "id": "m_u1", "sessionID": ses, "role": "user",
            "time": {"created": 1760000000100},
            "path": {"cwd": "/work/one"}})
        write_json(parts / "m_u1" / "p_u1a.json", {
            "id": "p_u1a", "messageID": "m_u1", "sessionID": ses, "type": "text",
            "synthetic": True, "text": "<system-reminder>internal</system-reminder>",
            "time": {"start": 1760000000100}})
        write_json(parts / "m_u1" / "p_u1b.json", {
            "id": "p_u1b", "messageID": "m_u1", "sessionID": ses, "type": "text",
            "ignored": True, "text": "ignored note",
            "time": {"start": 1760000000101}})
        write_json(parts / "m_u1" / "p_u1c.json", {
            "id": "p_u1c", "messageID": "m_u1", "sessionID": ses, "type": "text",
            "text": "please fix the flaky test",
            "time": {"start": 1760000000102}})
        # assistant message: step1 tool-calls, step2 stop with answer
        write_json(msgs / "m_a1.json", {
            "id": "m_a1", "sessionID": ses, "role": "assistant",
            "time": {"created": 1760000000200, "completed": 1760000000900},
            "finish": "stop", "modelID": "model-x", "providerID": "prov",
            "path": {"cwd": "/work/one"}})
        pdir = parts / "m_a1"
        write_json(pdir / "p_s1.json", {"id": "p_01", "messageID": "m_a1",
                                       "sessionID": ses, "type": "step-start",
                                       "time": {"start": 1760000000210}})
        write_json(pdir / "p_x1.json", {"id": "p_02", "messageID": "m_a1",
                                       "sessionID": ses, "type": "text",
                                       "text": "before tool note",
                                       "time": {"start": 1760000000211}})
        write_json(pdir / "p_t1.json", {"id": "p_03", "messageID": "m_a1",
                                       "sessionID": ses, "type": "tool",
                                       "tool": "read", "callID": "call_1",
                                       "time": {"start": 1760000000212},
                                       "state": {"status": "completed",
                                                 "input": {"path": "x"},
                                                 "output": "file body"}})
        write_json(pdir / "p_f1.json", {"id": "p_04", "messageID": "m_a1",
                                       "sessionID": ses, "type": "step-finish",
                                       "reason": "tool-calls",
                                       "time": {"start": 1760000000213},
                                       "tokens": {"input": 10, "output": 5}})
        write_json(pdir / "p_s2.json", {"id": "p_05", "messageID": "m_a1",
                                       "sessionID": ses, "type": "step-start",
                                       "time": {"start": 1760000000220}})
        write_json(pdir / "p_t2.json", {"id": "p_06", "messageID": "m_a1",
                                       "sessionID": ses, "type": "tool",
                                       "tool": "bash", "callID": "call_2",
                                       "time": {"start": 1760000000221},
                                       "state": {"status": "completed",
                                                 "input": {"cmd": "ls"},
                                                 "output": "listing"}})
        write_json(pdir / "p_x2.json", {"id": "p_07", "messageID": "m_a1",
                                       "sessionID": ses, "type": "text",
                                       "text": "final answer text",
                                       "time": {"start": 1760000000222}})
        write_json(pdir / "p_f2.json", {"id": "p_08", "messageID": "m_a1",
                                       "sessionID": ses, "type": "step-finish",
                                       "reason": "stop",
                                       "time": {"start": 1760000000223},
                                       "tokens": {"input": 20, "output": 8}})
        # subagent session via parentID + a subtask prompt part
        sub = "ses_v1sub"
        write_json(storage / "session" / "proj" / f"{sub}.json", {
            "id": sub, "version": "1.0.0", "projectID": "proj",
            "directory": "/work/one", "title": "Child",
            "parentID": ses,
            "time": {"created": 1760000001000, "updated": 1760000001100}})
        write_json(storage / "message" / sub / "m_su.json", {
            "id": "m_su", "sessionID": sub, "role": "user",
            "time": {"created": 1760000001050}})
        write_json(parts / "m_su" / "p_st.json", {
            "id": "p_st", "messageID": "m_su", "sessionID": sub,
            "type": "subtask", "agent": "explore", "description": "look around",
            "prompt": "You are the executor of one delegated assignment. Do X.",
            "time": {"start": 1760000001050}})
        return storage

    def test_file_sessions_fragments_and_turns(self):
        storage = self.write_file_store(self.root / "data")
        store = sessions.Store("opencode", "storage", storage, "json_dir")
        refs = {r.session_id: r for r in sessions.opencode_iter_sessions(store)}
        self.assertEqual(set(refs), {"ses_v1file", "ses_v1sub"})
        r = refs["ses_v1file"]
        self.assertEqual(r.extra["oc_source"], "file")
        self.assertEqual(r.extra["surface"], "cli")
        self.assertEqual(r.extra["native_id"], "ses_v1file")
        self.assertEqual(r.extra["session_class"], "interactive")
        sub = refs["ses_v1sub"]
        self.assertEqual(sub.parent, "ses_v1file")
        self.assertEqual(sub.extra["session_class"], "subagent")
        self.assertEqual(sub.extra["edges"][0]["type"], "subagent")
        stats = {}
        frags = list(sessions.opencode_iter_fragments(store, r, 400, stats))
        prompts = [f for f in frags if f["kind"] == "prompt"]
        # synthetic + ignored parts are context, only the genuine text is a prompt.
        self.assertEqual(len(prompts), 1)
        self.assertEqual(prompts[0]["origin"], "human")
        self.assertTrue(any(f["kind"] == "context" and f.get("basis") == "synthetic text part"
                            for f in frags))
        self.assertTrue(any(f["kind"] == "context" and f.get("basis") == "ignored text part"
                            for f in frags))
        # assistant native evidence + step boundaries
        asst = [f for f in frags if f["kind"] == "assistant"]
        self.assertEqual([f["text"] for f in asst], ["before tool note", "final answer text"])
        self.assertTrue(all((f.get("native") or {}).get("message_id") == "m_a1" for f in asst))
        self.assertEqual(asst[0]["native"]["after_last_tool"], False)
        self.assertEqual(asst[1]["native"]["after_last_tool"], True)
        self.assertTrue(any(f["kind"] == "boundary" and f.get("status") == "tool-calls"
                            for f in frags))
        # turns: only step-finish stop/length produces a final; tool-calls does not.
        turns = sessions.interpret_session(store, r, frags)
        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0]["final"]["text"], "final answer text")
        self.assertEqual(turns[0]["final_basis"], "native:step-finish=stop")
        self.assertFalse(turns[0]["open"])
        # subagent: subtask part -> delegated prompt
        sfrags = list(sessions.opencode_iter_fragments(store, sub, 400, {}))
        sprompts = [f for f in sfrags if f["kind"] == "prompt"]
        self.assertEqual(len(sprompts), 1)
        self.assertEqual(sprompts[0]["origin"], "delegated")
        self.assertEqual(sprompts[0]["origin_basis"], "subtask part")

    def test_legacy_sqlite_sessions_and_dedup(self):
        db = self.root / "opencode.db"
        conn = sqlite3.connect(db)
        conn.executescript("""
            CREATE TABLE session_v2 (id TEXT, directory TEXT, title TEXT,
                time_created INTEGER, time_updated INTEGER, parent_id TEXT,
                fork_session_id TEXT, agent TEXT, model TEXT, version TEXT,
                time_idle INTEGER, idle_outcome TEXT, time_busy INTEGER,
                time_archived INTEGER);
            CREATE TABLE session_message (id TEXT, session_id TEXT, type TEXT,
                seq INTEGER, time_created INTEGER, time_updated INTEGER, data TEXT);
            CREATE TABLE session_part (message_id TEXT, session_id TEXT, ordinal INTEGER,
                type TEXT, call_id TEXT, time_created INTEGER, time_updated INTEGER, data TEXT);
            CREATE TABLE session (id TEXT, project_id TEXT, parent_id TEXT, slug TEXT,
                directory TEXT, title TEXT, version TEXT, agent TEXT, model TEXT,
                time_created INTEGER, time_updated INTEGER, time_archived INTEGER);
            CREATE TABLE message (id TEXT, session_id TEXT, time_created INTEGER,
                time_updated INTEGER, data TEXT);
            CREATE TABLE part (id TEXT, message_id TEXT, session_id TEXT,
                time_created INTEGER, time_updated INTEGER, data TEXT);
        """)
        # v2 session that also exists in legacy tables -> emitted once, from v2.
        conn.execute("INSERT INTO session_v2 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     ("ses_both", "/work/v2", "V2 wins", 1, 2, None, None, None,
                      '{"id":"m2"}', "2", None, None, None, None))
        conn.execute("INSERT INTO session VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                     ("ses_both", "p", None, "slug", "/work/legacy", "Legacy dup",
                      "1", None, '{"id":"m1"}', 1, 2, None))
        conn.execute("INSERT INTO session_message VALUES (?,?,?,?,?,?,?)",
                     ("mv2u", "ses_both", "user", 0, 1, 1,
                      json.dumps({"text": "v2 prompt"})))
        # legacy-only session
        conn.execute("INSERT INTO session VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                     ("ses_leg", "p", "ses_both", "slug2", "/work/leg", "Legacy only",
                      "1", None, '{"id":"m1"}', 3, 4, None))
        conn.execute("INSERT INTO message VALUES (?,?,?,?,?)",
                     ("mu1", "ses_leg", 3, 3, json.dumps({
                         "id": "mu1", "sessionID": "ses_leg", "role": "user",
                         "time": {"created": 3}, "path": {"cwd": "/work/leg"}})))
        conn.execute("INSERT INTO part VALUES (?,?,?,?,?,?)",
                     ("pu1", "mu1", "ses_leg", 3, 3, json.dumps({
                         "id": "pu1", "messageID": "mu1", "sessionID": "ses_leg",
                         "type": "text", "text": "legacy prompt",
                         "time": {"start": 3}})))
        conn.execute("INSERT INTO message VALUES (?,?,?,?,?)",
                     ("ma1", "ses_leg", 4, 5, json.dumps({
                         "id": "ma1", "sessionID": "ses_leg", "role": "assistant",
                         "time": {"created": 4, "completed": 5}, "finish": "stop",
                         "modelID": "m", "providerID": "p",
                         "path": {"cwd": "/work/leg"}})))
        conn.execute("INSERT INTO part VALUES (?,?,?,?,?,?)",
                     ("pa1", "ma1", "ses_leg", 4, 5, json.dumps({
                         "id": "pa1", "messageID": "ma1", "sessionID": "ses_leg",
                         "type": "step-start"})))
        conn.execute("INSERT INTO part VALUES (?,?,?,?,?,?)",
                     ("pa2", "ma1", "ses_leg", 4, 5, json.dumps({
                         "id": "pa2", "messageID": "ma1", "sessionID": "ses_leg",
                         "type": "text", "text": "legacy answer",
                         "time": {"start": 4}})))
        conn.execute("INSERT INTO part VALUES (?,?,?,?,?,?)",
                     ("pa3", "ma1", "ses_leg", 4, 5, json.dumps({
                         "id": "pa3", "messageID": "ma1", "sessionID": "ses_leg",
                         "type": "step-finish", "reason": "stop"})))
        conn.commit()
        conn.close()
        store = sessions.Store("opencode", "db", db, "sqlite")
        refs = list(sessions.opencode_iter_sessions(store))
        ids = [r.session_id for r in refs]
        self.assertEqual(ids.count("ses_both"), 1)
        self.assertEqual(ids.count("ses_leg"), 1)
        both = next(r for r in refs if r.session_id == "ses_both")
        self.assertEqual(both.extra["oc_source"], "v2")
        self.assertEqual(both.title, "V2 wins")
        leg = next(r for r in refs if r.session_id == "ses_leg")
        self.assertEqual(leg.extra["oc_source"], "legacy")
        self.assertEqual(leg.parent, "ses_both")
        self.assertEqual(leg.extra["session_class"], "subagent")
        self.assertEqual(leg.extra["edges"][0]["type"], "subagent")
        frags = list(sessions.opencode_iter_fragments(store, leg, 400, {}))
        prompts = [f for f in frags if f["kind"] == "prompt"]
        self.assertEqual(len(prompts), 1)
        self.assertEqual(prompts[0]["origin"], "human")
        self.assertTrue(any(f["kind"] == "assistant" and f["text"] == "legacy answer"
                            for f in frags))
        self.assertTrue(any(f["kind"] == "boundary" and f.get("status") == "stop"
                            for f in frags))
        turns = sessions.interpret_session(store, leg, frags)
        self.assertEqual(turns[0]["final"]["text"], "legacy answer")
        self.assertEqual(turns[0]["final_basis"], "native:step-finish=stop")

    def test_file_store_skips_db_claimed_ids(self):
        # storage + sibling opencode.db: a session present in both is emitted
        # once, from the DB (session_v2 > legacy > file storage).
        data = self.root / "opencode"
        storage = self.write_file_store(data)
        db = data / "opencode.db"
        conn = sqlite3.connect(db)
        conn.executescript("""
            CREATE TABLE session_v2 (id TEXT, directory TEXT, title TEXT,
                time_created INTEGER, time_updated INTEGER, parent_id TEXT,
                fork_session_id TEXT, agent TEXT, model TEXT, version TEXT,
                time_idle INTEGER, idle_outcome TEXT, time_busy INTEGER,
                time_archived INTEGER);
            CREATE TABLE session_message (id TEXT, session_id TEXT, type TEXT,
                seq INTEGER, time_created INTEGER, time_updated INTEGER, data TEXT);
            CREATE TABLE session (id TEXT);
            CREATE TABLE message (id TEXT);
            CREATE TABLE part (id TEXT);
        """)
        conn.execute("INSERT INTO session_v2 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     ("ses_v1file", "/work/db", "DB version", 1, 2, None, None, None,
                      None, "2", None, None, None, None))
        conn.commit()
        conn.close()
        with patch.dict(os.environ, {"XDG_DATA_HOME": str(data.parent)}, clear=True):
            stores = sessions.stores_for("opencode")
        db_refs = [r for s in stores if s.kind == "sqlite"
                   for r in sessions.opencode_iter_sessions(s)]
        file_refs = [r for s in stores if s.kind != "sqlite"
                     for r in sessions.opencode_iter_sessions(s)]
        self.assertEqual([r.session_id for r in db_refs], ["ses_v1file"])
        self.assertEqual([r.session_id for r in file_refs], ["ses_v1sub"])


class OpenCodeV2ContractTests(FixtureMixin, unittest.TestCase):
    """OpenCode 2.x additive classification fields."""

    def test_v2_origins_native_evidence_edges_status(self):
        db = self.root / "opencode.db"
        sqlite3.connect(db).close()
        conn = sqlite3.connect(db)
        conn.executescript("""
            CREATE TABLE session_v2 (id TEXT, directory TEXT, title TEXT,
                time_created INTEGER, time_updated INTEGER, parent_id TEXT,
                fork_session_id TEXT, agent TEXT, model TEXT, version TEXT,
                time_idle INTEGER, idle_outcome TEXT, time_busy INTEGER,
                time_archived INTEGER);
            CREATE TABLE session_message (id TEXT, session_id TEXT, type TEXT,
                seq INTEGER, time_created INTEGER, time_updated INTEGER, data TEXT);
            CREATE TABLE session_part (message_id TEXT, session_id TEXT, ordinal INTEGER,
                type TEXT, call_id TEXT, time_created INTEGER, time_updated INTEGER, data TEXT);
        """)
        conn.execute("INSERT INTO session_v2 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     ("ses_main", "/work", "Main", 1, 9, None, None, None, None,
                      "2", 5000, "completed", None, 8000))
        conn.execute("INSERT INTO session_v2 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     ("ses_child", "/work", "Child", 2, 9, "ses_main", "ses_fork",
                      "build", None, "2", None, None, None, None))
        msgs = [
            ("m_sys", "ses_main", "system", 0, {"text": "rules injected"}),
            ("m_synth", "ses_main", "synthetic", 1, {"text": "<auto>"}),
            ("m_skill", "ses_main", "skill", 2, {"text": "skill body"}),
            ("m_user", "ses_main", "user", 3, {"text": "hello there"}),
            ("m_sub", "ses_main", "user", 4,
             {"text": "You are a subagent spawned by another session. do x"}),
            ("m_exec", "ses_main", "user", 5,
             {"text": sessions.executor_prefix() + "\n\ndo y"}),
            ("m_asst", "ses_main", "assistant", 6,
             {"finish": "tool-calls",
              "time": {"created": 6, "completed": 7},
              "content": [
                  {"type": "text", "text": "before tool"},
                  {"type": "tool", "id": "c1", "name": "read",
                   "state": {"input": {}, "status": "completed", "content": "out"}},
                  {"type": "text", "text": "after tool"}]}),
            ("m_asst2", "ses_main", "assistant", 7,
             {"finish": "stop", "time": {"created": 8, "completed": 9},
              "content": [{"type": "text", "text": "the answer"}]}),
        ]
        for mid, sid, mtype, seq, data in msgs:
            conn.execute("INSERT INTO session_message VALUES (?,?,?,?,?,?,?)",
                         (mid, sid, mtype, seq, seq, seq, json.dumps(data)))
        conn.commit()
        conn.close()
        store = sessions.Store("opencode", "db", db, "sqlite")
        refs = {r.session_id: r for r in sessions.opencode_iter_sessions(store)}
        main, child = refs["ses_main"], refs["ses_child"]
        # additive contract fields
        for r in (main, child):
            self.assertEqual(r.extra["native_id"], r.session_id)
            self.assertEqual(r.extra["surface"], "cli")
            self.assertIn("sources", r.extra)
        self.assertEqual(main.extra["native_status"]["idle_outcome"], "completed")
        self.assertEqual(main.extra["native_status"]["time_idle"], 5000)
        self.assertEqual(main.extra["native_status"]["time_archived"], 8000)
        self.assertEqual(child.extra["session_class"], "subagent")
        self.assertEqual(child.extra["class_basis"], "parent_id")
        edge_types = [e["type"] for e in child.extra["edges"]]
        self.assertEqual(edge_types, ["subagent", "fork"])
        self.assertEqual(child.extra["edges"][0]["target_id"], "ses_main")
        self.assertEqual(child.extra["edges"][1]["target_id"], "ses_fork")
        self.assertEqual(child.parent, "ses_main")
        frags = list(sessions.opencode_iter_fragments(store, main, 400, {}))
        by_text = {f.get("text"): f for f in frags if isinstance(f.get("text"), str)}
        self.assertEqual(by_text["hello there"]["origin"], "human")
        self.assertEqual(by_text["You are a subagent spawned by another session. do x"]["origin"],
                         "delegated")
        self.assertEqual(by_text[sessions.executor_prefix() + "\n\ndo y"]["origin"],
                         "delegated")
        # synthetic/system/skill rows are injected context, not prompts
        for f in frags:
            if f["kind"] == "context":
                self.assertNotEqual(f.get("origin"), "human")
        prompts = [f for f in frags if f["kind"] == "prompt"]
        self.assertEqual(len(prompts), 3)
        # assistant native evidence: finish, completed, after_last_tool
        asst = [f for f in frags if f["kind"] == "assistant"]
        native = {(f.get("native") or {}).get("message_id"): f.get("native") for f in asst}
        self.assertEqual(native["m_asst"]["finish"], "tool-calls")
        self.assertTrue(native["m_asst"]["completed"])
        texts = [f for f in asst if (f.get("native") or {}).get("message_id") == "m_asst"]
        self.assertEqual([f["native"]["after_last_tool"] for f in texts], [False, True])
        self.assertEqual(native["m_asst2"]["finish"], "stop")
        # turn rule: last completed assistant with final finish -> its
        # post-last-tool text is the answer.
        turns = sessions.interpret_session(store, main, frags)
        self.assertEqual(len(turns), 3)
        self.assertEqual(turns[-1]["final"]["text"], "the answer")
        self.assertEqual(turns[-1]["final_basis"], "native:finish=stop")
        self.assertFalse(turns[-1]["open"])


class PerSourceTests(FixtureMixin, unittest.TestCase):
    def test_file_sources_and_builders(self):
        cc = sessions.Store('claude-code', 'projects', self.root / 'cc', 'jsonl_dir')
        path = write_jsonl(cc.path / 'project' / 'thread.jsonl', [{'type':'user','message':{'role':'user','content':'hello'}}])
        before = list(sessions.SOURCES['claude-code'](cc))
        self.assertEqual([(x.key, x.role, x.session_id) for x in before], [((str(path),), 'content', 'thread')])
        listed = next(sessions.claude_iter_sessions(cc)); built = sessions.claude_ref_for_file(cc, path)
        self.assertEqual(listed.as_dict(), built.as_dict())
        self.assertEqual(list(sessions.claude_iter_fragments(cc, listed, 0, {})), list(sessions.claude_iter_fragments(cc, built, 0, {})))
        with path.open('a') as f: f.write('\n')
        appended = next(sessions.SOURCES['claude-code'](cc))
        self.assertNotEqual(before[0].stat, appended.stat)
        path.write_text('rewritten')
        self.assertNotEqual(appended.stat, next(sessions.SOURCES['claude-code'](cc)).stat)
        codex = sessions.Store('codex', 'sessions', self.root / 'cx', 'jsonl_dir')
        rollout = write_jsonl(codex.path / 'rollout-2026-01-01T00-00-00-abc.jsonl', [{'type':'session_meta','timestamp':'2026-01-01T00:00:00Z','payload':{'id':'abc'}}])
        source = next(sessions.SOURCES['codex'](codex))
        self.assertEqual((source.key, source.role, source.session_id), ((str(rollout),), 'content', 'abc'))
        listed = next(sessions.codex_iter_sessions(codex)); built = sessions.codex_ref_for_rollout(codex, rollout, sessions.codex_rollout_shared(codex))
        self.assertEqual(listed.as_dict(), built.as_dict())
        self.assertEqual(list(sessions.codex_iter_fragments(codex, listed, 0, {})), list(sessions.codex_iter_fragments(codex, built, 0, {})))
        desktop = sessions.Store('claude-desktop', 'code-sessions', self.root / 'desktop', 'json_dir')
        manifest = write_json(desktop.path / 'local_123.json', {'sessionId':'local_123','cliSessionId':'abc','title':'a'})
        source = next(sessions.SOURCES['claude-desktop'](desktop))
        self.assertEqual((source.role, source.session_id), ('metadata', None))
        listed = next(sessions.claude_desktop_iter_sessions(desktop)); built = sessions.claude_desktop_ref_for_file(desktop, manifest)
        self.assertEqual(listed.as_dict(), built.as_dict())
        self.assertEqual(list(sessions.claude_desktop_iter_fragments(desktop, listed, 0, {})), list(sessions.claude_desktop_iter_fragments(desktop, built, 0, {})))
        run = sessions.Store('porch', 'runs', self.root / 'runs', 'run_registry')
        directory = run.path / 'run1'; write_json(directory / 'meta.json', {'run_id':'run1','task':'task'}); (directory / 'final.txt').write_text('done')
        listed = next(sessions.porch_iter_sessions(run)); built = sessions.porch_ref_for_run(run, directory)
        self.assertEqual(listed.as_dict(), built.as_dict())
        self.assertEqual(list(sessions.porch_iter_fragments(run, listed, 0, {})), list(sessions.porch_iter_fragments(run, built, 0, {})))
        self.assertEqual({(x.role, x.session_id) for x in sessions.SOURCES['porch'](run)}, {('metadata','run1'),('content','run1')})
        agy = sessions.Store('agy','cli',self.root / 'agy','agy_root')
        write_json(agy.path / 'cache' / 'conversation_metadata.json', {'conversations':{'c1':{'summary':{'Title':'a'}}}})
        listed = next(sessions.agy_iter_sessions(agy)); built = sessions.agy_ref_for_conversation(agy,'c1',sessions.agy_shared(agy))
        self.assertEqual(listed.as_dict(), built.as_dict())
        self.assertEqual(list(sessions.agy_iter_fragments(agy, listed, 0, {})), list(sessions.agy_iter_fragments(agy, built, 0, {})))
        self.assertEqual(next(sessions.SOURCES['agy'](agy)).role, 'metadata')
        storage = self.root / 'oc' / 'storage'; path = write_json(storage / 'session' / 'p' / 's.json', {'id':'s','title':'test'})
        oc = sessions.Store('opencode','storage',storage,'json_dir')
        listed = next(sessions.opencode_iter_sessions(oc)); built = sessions.opencode_file_ref(oc,path,set())
        self.assertEqual(listed.as_dict(), built.as_dict())
        self.assertEqual(list(sessions.opencode_iter_fragments(oc, listed, 0, {})), list(sessions.opencode_iter_fragments(oc, built, 0, {})))
        self.assertEqual(next(sessions.SOURCES['opencode'](oc)).session_id, 's')

    def test_sqlite_builders_and_fingerprints(self):
        db = self.root / 'devin.db'; conn = sqlite3.connect(db)
        conn.executescript('''CREATE TABLE sessions(id TEXT, working_directory TEXT, model TEXT, agent_mode TEXT, created_at TEXT, last_activity_at TEXT, title TEXT, hidden INTEGER, main_chain_id INTEGER);
        CREATE TABLE message_nodes(row_id INTEGER PRIMARY KEY, session_id TEXT, node_id INTEGER, parent_node_id INTEGER, chat_message TEXT, created_at TEXT, metadata TEXT);
        INSERT INTO sessions VALUES('s',NULL,NULL,NULL,NULL,'1','title',0,NULL);''')
        conn.commit(); store = sessions.Store('devin','sessions.db',db,'sqlite')
        first = sessions.session_fingerprints(store,conn); conn.execute("INSERT INTO message_nodes VALUES(1,'s',1,NULL,'{}',NULL,NULL)")
        second = sessions.session_fingerprints(store,conn); self.assertNotEqual(first,second)
        conn.execute('DELETE FROM message_nodes'); self.assertEqual(first,sessions.session_fingerprints(store,conn)); conn.commit()
        self.assertEqual({(x.key[1], x.session_id, x.role) for x in sessions.sqlite_session_sources(store,conn)}, {('sessions','s','metadata'),('message_nodes','s','content')})
        listed = next(sessions.devin_iter_sessions(store)); built = sessions.devin_ref_for_session(store,conn,'s')
        self.assertEqual(listed.as_dict(),built.as_dict())
        self.assertEqual(list(sessions.devin_iter_fragments(store,listed,0,{})),list(sessions.devin_iter_fragments(store,built,0,{},conn)))
        self.assertEqual(conn.execute('SELECT count(*) FROM sessions').fetchone()[0],1); conn.close()
        db = self.root / 'opencode.db'; conn = sqlite3.connect(db)
        conn.executescript('''CREATE TABLE session_v2(id TEXT, title TEXT, time_updated INTEGER, time_busy INTEGER, time_idle INTEGER);
        CREATE TABLE session_message(id TEXT, session_id TEXT, type TEXT, seq INTEGER, time_created INTEGER, time_updated INTEGER, data TEXT);
        CREATE TABLE session_part(message_id TEXT, session_id TEXT, ordinal INTEGER, type TEXT, time_updated INTEGER, data TEXT);
        CREATE TABLE session(id TEXT, title TEXT); CREATE TABLE message(id TEXT, session_id TEXT, time_created INTEGER, data TEXT); CREATE TABLE part(id TEXT, message_id TEXT, session_id TEXT, time_created INTEGER, data TEXT);
        INSERT INTO session_v2 VALUES('v','title',1,NULL,NULL); INSERT INTO session VALUES('l','legacy');''')
        conn.commit(); store = sessions.Store('opencode','db',db,'sqlite')
        first=sessions.session_fingerprints(store,conn); conn.execute("INSERT INTO session_message VALUES('m','v','user',1,1,1,'{\"text\":\"hi\"}')")
        second=sessions.session_fingerprints(store,conn); self.assertNotEqual(first,second)
        conn.execute("INSERT INTO session_part VALUES('m','v',1,'text',2,'{}')"); third=sessions.session_fingerprints(store,conn); self.assertNotEqual(second,third)
        conn.execute('DELETE FROM session_part'); self.assertEqual(second,sessions.session_fingerprints(store,conn))
        conn.execute('DELETE FROM session_message'); self.assertEqual(first,sessions.session_fingerprints(store,conn)); conn.commit()
        self.assertEqual({(x.key[1],x.session_id) for x in sessions.sqlite_session_sources(store,conn)}, {('session_v2','v'),('session_message','v'),('session_part','v'),('session','l'),('message','l'),('part','l')})
        listed={r.session_id:r for r in sessions.opencode_iter_sessions(store)}
        for sid,build in [('v',sessions.opencode_v2_ref),('l',sessions.opencode_legacy_ref)]:
            ref=build(store,conn,sid); self.assertEqual(ref.as_dict(),listed[sid].as_dict())
            self.assertEqual(list(sessions.opencode_iter_fragments(store,listed[sid],0,{})),list(sessions.opencode_iter_fragments(store,ref,0,{},conn)))
        self.assertIsNone(sessions.opencode_legacy_ref(store,conn,'v')); conn.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
