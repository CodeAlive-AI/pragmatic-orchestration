"""OpenCode 2 steerable backend: event-stream recovery and web-search consent (fake server only)."""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from test_steer_e2e import PORCH, env_base, extract_run_id
from steer.adapters.opencode import OpenCodeAdapter


class OpenCodeResilienceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="porch-oc-resilience-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cwd = self.root / "cwd"
        self.cwd.mkdir()
        (self.root / "registry").mkdir()
        self.env = env_base(self.root / "registry", self.root / "artifacts")
        self.env["PORCH_FAKE_STEER_SLOW"] = "0.1"
        self.env["PORCH_SAVE_OUTPUTS"] = "0"
        self.form_log = self.root / "forms.jsonl"
        self.env["PORCH_FAKE_OC_FORM_LOG"] = str(self.form_log)

    def delegate(self, *extra, agent="opencode", timeout=90, **env):
        run_env = dict(self.env, **env)
        proc = subprocess.run([str(PORCH), "delegate", "-a", agent, "TASK"], cwd=self.cwd, env=run_env,
                              text=True, input="", capture_output=True, timeout=timeout)
        run_id = extract_run_id(proc.stderr)
        events = []
        if run_id:
            shown = subprocess.run([str(PORCH), "delegate", "events", run_id, "--cursor", "0", "--max-events", "500"],
                                   cwd=self.cwd, env=run_env, text=True, capture_output=True)
            events = json.loads(shown.stdout)["events"]
        return proc, events

    def progress(self, events):
        return [e["data"] for e in events if e["type"] == "progress"]

    def answers(self):
        if not self.form_log.exists():
            return []
        return [json.loads(x) for x in self.form_log.read_text().splitlines() if "answer" in x]

    # R1: event-stream recovery -------------------------------------------------

    def test_a_stream_drop_mid_run_recovers_without_duplicates_or_losses(self):
        reference, reference_events = self.delegate(PORCH_FAKE_OC_SCRIPT="plain")
        self.assertEqual(reference.returncode, 0, reference.stderr)
        dropped, events = self.delegate(PORCH_FAKE_OC_SCRIPT="drop")
        self.assertEqual(dropped.returncode, 0, dropped.stderr)
        self.assertEqual(dropped.stdout, reference.stdout)
        self.assertEqual(dropped.stdout.strip(), "Alpha Beta Gamma")
        text = [e["data"] for e in events if e["type"] == "answer_delta"]
        self.assertEqual("".join(text), "Alpha Beta Gamma")
        self.assertEqual("".join(text), "".join(e["data"] for e in reference_events if e["type"] == "answer_delta"))
        tools = [(e["type"], json.loads(e["data"])["call_id"]) for e in events if e["type"].startswith("tool_")]
        self.assertEqual(tools, [("tool_started", "call-1"), ("tool_completed", "call-1")])
        self.assertEqual([p for p in self.progress(events) if p.startswith("stream_reconnected")], ["stream_reconnected attempt=1"])
        self.assertIn("progress stream_reconnected attempt=1", dropped.stderr)
        self.assertEqual(events[-1]["type"], "run_completed")

    def test_a_reconnect_never_resends_the_prompt(self):
        log = self.root / "prompts.jsonl"
        proc, _ = self.delegate(PORCH_FAKE_OC_SCRIPT="drop", PORCH_FAKE_OC_PROMPT_LOG=str(log))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(len(log.read_text().splitlines()), 1)

    def test_f_completion_missed_during_the_outage_is_recovered_from_history(self):
        # The stream stays down for the rest of the run, so only history shows the outcome.
        proc, events = self.delegate(PORCH_FAKE_OC_SCRIPT="drop", PORCH_FAKE_OC_GAP="0")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "Alpha Beta Gamma")
        text = "".join(e["data"] for e in events if e["type"] == "answer_delta")
        self.assertEqual(text, "Alpha Beta Gamma")
        self.assertEqual(len([e for e in events if e["type"] == "tool_completed"]), 1)

    def test_b_server_gone_fails_explicitly_with_attempt_count(self):
        started = time.monotonic()
        proc, events = self.delegate(PORCH_FAKE_OC_SCRIPT="drop-gone")
        self.assertNotEqual(proc.returncode, 0)
        self.assertLess(time.monotonic() - started, 60)
        failed = [e for e in events if e["type"] == "run_failed"]
        self.assertEqual(len(failed), 1, events)
        self.assertIn("could not be re-established after 3 reconnect attempt(s)", failed[0]["data"])
        self.assertIn("server process exited", failed[0]["data"])
        self.assertFalse([e for e in events if e["type"] == "run_completed" and e["data"] == "completed"])
        self.assertEqual(self.progress(events).count("stream_reconnected attempt=1"), 0)

    def test_c_repeated_drops_past_the_limit_fail_explicitly(self):
        proc, events = self.delegate(PORCH_FAKE_OC_SCRIPT="hang", PORCH_FAKE_OC_DROP_ALWAYS="1")
        self.assertNotEqual(proc.returncode, 0)
        failed = [e for e in events if e["type"] == "run_failed"]
        self.assertEqual(len(failed), 1, events)
        self.assertIn("after 3 reconnect attempt(s)", failed[0]["data"])
        reconnects = [p for p in self.progress(events) if p.startswith("stream_reconnected")]
        self.assertEqual(reconnects, [f"stream_reconnected attempt={n}" for n in (1, 2, 3)])

    def test_recovery_gives_up_at_the_wall_clock_cap(self):
        a = OpenCodeAdapter(binary="fake", model="p/m", cwd=str(self.root), artifacts_dir=str(self.root), agent_id="oc")
        (self.root / "raw").mkdir(exist_ok=True)
        a.base_url = "http://127.0.0.1:1"
        a._connected_once = True
        a.session_id = "ses"
        with patch.object(a, "_read_stream", side_effect=RuntimeError("down")), \
                patch.object(OpenCodeAdapter, "_RECONNECT_BACKOFF", (0.0, 0.0, 0.0)), \
                patch.object(OpenCodeAdapter, "_RECONNECT_TOTAL_SECONDS", 0.0):
            a._sse_loop()
        self.assertTrue(a.is_done())
        self.assertIn("after 0 reconnect attempt(s): down", a._error)

    def test_stream_drop_before_first_connect_fails_as_before(self):
        a = OpenCodeAdapter(binary="fake", model="p/m", cwd=str(self.root), artifacts_dir=str(self.root), agent_id="oc")
        (self.root / "raw").mkdir(exist_ok=True)
        a.base_url = "http://127.0.0.1:1"
        with patch.object(a, "_read_stream", side_effect=RuntimeError("refused")):
            a._sse_loop()
        self.assertEqual(a._error, "OpenCode 2 event stream failed: refused")

    # R2: web-search consent ----------------------------------------------------

    def config_log(self):
        return [json.loads(x)["config_websearch"] for x in self.form_log.read_text().splitlines() if "config_websearch" in x]

    def test_d_default_allows_web_search_through_server_config(self):
        proc, events = self.delegate(PORCH_FAKE_OC_FORM="websearch")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.progress(events)[0], "websearch_consent_allowed via=config")
        self.assertEqual(self.config_log(), [{"provider": "random"}])
        self.assertEqual(self.answers(), [])  # the server never asked, and Porch never answers

    def test_d_decline_profile_turns_web_search_off_through_server_config(self):
        proc, events = self.delegate(agent="opencode-web-search-decline", PORCH_FAKE_OC_FORM="websearch")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.progress(events)[0], "websearch_consent_declined via=config")
        self.assertEqual(self.config_log(), [False])
        self.assertEqual(self.answers(), [])  # the server never asked, and Porch never answers

    def test_d_default_never_answers_a_consent_form_that_appears_anyway(self):
        proc, events = self.delegate(PORCH_FAKE_OC_FORM="websearch", PORCH_FAKE_OC_FORM_FORCE="1")
        self.assertNotEqual(proc.returncode, 0)
        failed = [e for e in events if e["type"] == "run_failed"]
        self.assertEqual(len(failed), 1, events)
        self.assertTrue(failed[0]["data"].startswith("input_required: OpenCode asked for web-search provider consent"), failed[0]["data"])
        self.assertIn("web_search='allow'", failed[0]["data"])
        self.assertEqual(self.answers(), [])

    def test_d_existing_config_content_is_kept_and_extended(self):
        proc, _ = self.delegate(PORCH_FAKE_OC_FORM="websearch", OPENCODE_CONFIG_CONTENT='{"theme": "x"}')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.config_log(), [{"provider": "random"}])

    def test_d_caller_websearch_setting_wins_and_is_not_overwritten(self):
        proc, events = self.delegate(PORCH_FAKE_OC_FORM="websearch", OPENCODE_CONFIG_CONTENT='{"websearch": {"provider": "exa"}}')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.config_log(), [{"provider": "exa"}])
        self.assertFalse([p for p in self.progress(events) if p.startswith("websearch_consent")])

    def test_d_unusable_config_content_fails_fast_with_the_cause_when_asked(self):
        proc, events = self.delegate(PORCH_FAKE_OC_FORM="websearch", OPENCODE_CONFIG_CONTENT="// jsonc")
        self.assertNotEqual(proc.returncode, 0)
        failed = [e for e in events if e["type"] == "run_failed"]
        self.assertIn("not applied", failed[0]["data"])
        self.assertEqual(self.answers(), [])

    def test_d_opt_in_profile_enables_a_provider_through_server_config_only(self):
        proc, events = self.delegate(agent="opencode-web-search-allow", PORCH_FAKE_OC_FORM="websearch")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.config_log(), [{"provider": "random"}])
        self.assertEqual(self.answers(), [])
        progress = self.progress(events)
        self.assertEqual(progress[0], "websearch_consent_allowed via=config")
        self.assertFalse([p for p in progress if p.startswith("websearch_consent_declined")])

    def test_d_opt_in_profile_still_never_answers_a_form(self):
        proc, events = self.delegate(agent="opencode-web-search-allow", PORCH_FAKE_OC_FORM="websearch", PORCH_FAKE_OC_FORM_FORCE="1")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("web_search='allow'", [e for e in events if e["type"] == "run_failed"][0]["data"])
        self.assertEqual(self.answers(), [])

    def test_d_invalid_web_search_value_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "web_search must be"):
            OpenCodeAdapter(binary="fake", model="p/m", cwd=str(self.root), artifacts_dir=str(self.root), web_search="maybe")

    def test_d_one_shot_server_environment_is_unchanged(self):
        a = OpenCodeAdapter(binary="fake", model="p/m", cwd=str(self.root), artifacts_dir=str(self.root))
        with patch.dict(os.environ, clear=False):
            os.environ.pop("OPENCODE_CONFIG_CONTENT", None)
            self.assertNotIn("OPENCODE_CONFIG_CONTENT", a._child_env())

    # R2/e: everything else still fails fast -------------------------------------

    def test_e_other_form_fails_fast(self):
        for kind in ("other", "lookalike", "odd-fields"):
            with self.subTest(kind=kind):
                proc, events = self.delegate(PORCH_FAKE_OC_FORM=kind, PORCH_FAKE_OC_FORM_FORCE="1")
                self.assertNotEqual(proc.returncode, 0)
                failed = [e for e in events if e["type"] == "run_failed"]
                self.assertEqual(len(failed), 1)
                self.assertTrue(failed[0]["data"].startswith("input_required:"), failed[0]["data"])
                self.assertEqual(self.answers(), [])

    def test_e_permission_request_still_fails_fast(self):
        proc, events = self.delegate(PORCH_FAKE_OC_PERMISSION="1")
        self.assertNotEqual(proc.returncode, 0)
        failed = [e for e in events if e["type"] == "run_failed"]
        self.assertTrue(failed and failed[0]["data"].startswith("permission_required:"), events)


if __name__ == "__main__":
    unittest.main()
