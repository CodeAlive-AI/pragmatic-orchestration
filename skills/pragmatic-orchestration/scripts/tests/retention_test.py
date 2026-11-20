#!/usr/bin/env python3
"""Offline tests for `porch prune` (lib/retention.py) and its shell hooks.

Everything happens in temporary directories; no real outputs folder or
registry is read or written.
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
LIB_DIR = TESTS_DIR.parent / "lib"
PORCH = TESTS_DIR.parent / "porch"
sys.path.insert(0, str(LIB_DIR))

from steer.registry import Registry  # noqa: E402

DAY = 86400
HOUR = 3600


def dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


class RetentionCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        self.outputs = self.root / "outputs"
        self.steer = self.root / "steer"
        self.outputs.mkdir()
        self.now = time.time()
        self.gone = dead_pid()

    def tearDown(self):
        self._tmp.cleanup()

    # -- fixtures ---------------------------------------------------------

    def age_tree(self, path: Path, age: float):
        stamp = self.now - age
        for current, dirs, names in os.walk(path):
            for name in names:
                os.utime(os.path.join(current, name), (stamp, stamp))

    def output_run(self, name, *, age, owner="dead", detail=4096, final="the answer"):
        run = self.outputs / name
        (run / "raw").mkdir(parents=True)
        (run / "normalized").mkdir()
        (run / "final").mkdir()
        meta = {"mode": "delegate", "cwd": "/x", "started_at": iso(self.now - age)}
        if owner == "dead":
            meta["owner_pid"] = self.gone
        elif owner == "alive":
            meta["owner_pid"] = os.getpid()
        (run / "meta.json").write_text(json.dumps(meta))
        if detail:
            (run / "raw" / "agent.jsonl").write_bytes(b"r" * detail)
            (run / "normalized" / "agent.jsonl").write_bytes(b"n" * detail)
        if final is not None:
            (run / "final.txt").write_text(final)
            (run / "final" / "agent.txt").write_text(final)
        self.age_tree(run, age)
        return run

    def registry_run(self, name, *, age, status="completed", pid=None, artifacts="", anchor=None):
        reg = Registry(self.steer)
        run_id = reg.create_run(agent_id="codex", backend="codex-cli", model="m", cwd="/x",
                                artifacts_dir=str(artifacts), run_name=name)
        path = reg.runs_dir / run_id
        meta = json.loads((path / "meta.json").read_text())
        meta.update(status=status, pid=self.gone if pid is None else pid,
                    started_at=iso(self.now - age - 60), updated_at=iso(self.now - age))
        if status in ("completed", "failed", "cancelled"):
            meta["finished_at"] = iso(self.now - age)
        if anchor:
            meta["session_handle"] = {"backend": "codex-cli", "thread_id": "t", "anchor_run_id": anchor}
        (path / "meta.json").write_text(json.dumps(meta))
        (path / "final.txt").write_text("registry answer")
        (path / "audit.jsonl").write_text("{}\n" * 50)
        self.age_tree(path, age)
        return run_id, path

    def prune(self, *args, env=None):
        full_env = dict(os.environ, PORCH_OUTPUT_DIR=str(self.outputs), PORCH_STEER_DIR=str(self.steer))
        for name in ("PORCH_RETENTION_DAYS", "PORCH_RETENTION_DETAIL_DAYS", "PORCH_RETENTION_DETAIL_MAX"):
            full_env.pop(name, None)
        full_env.update(env or {})
        return subprocess.run([sys.executable, str(LIB_DIR / "retention.py"), "prune", *args],
                              env=full_env, capture_output=True, text=True, timeout=60)


class OutputsRetentionTest(RetentionCase):
    def test_dry_run_lists_candidates_and_removes_nothing(self):
        old = self.output_run("run-delegate-old-aaaa", age=40 * DAY)
        result = self.prune()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("dry run", result.stdout)
        self.assertIn("run-delegate-old-aaaa", result.stdout)
        self.assertIn("porch prune --apply", result.stdout)
        self.assertTrue((old / "raw" / "agent.jsonl").is_file())

    def test_apply_removes_finished_runs_past_retention_and_keeps_younger_ones(self):
        old = self.output_run("run-delegate-old-aaaa", age=31 * DAY)
        young = self.output_run("run-delegate-young-bbbb", age=29 * DAY, detail=0)
        result = self.prune("--apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(old.exists())
        self.assertEqual((young / "final.txt").read_text(), "the answer")

    def test_streams_go_after_the_detail_age_while_the_answer_stays(self):
        run = self.output_run("run-delegate-mid-cccc", age=4 * DAY)
        fresh = self.output_run("run-delegate-fresh-dddd", age=2 * DAY)
        result = self.prune("--apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((run / "raw").exists())
        self.assertFalse((run / "normalized").exists())
        self.assertEqual((run / "final.txt").read_text(), "the answer")
        self.assertEqual((run / "final" / "agent.txt").read_text(), "the answer")
        self.assertTrue((run / "meta.json").is_file())
        self.assertIn("streams_removed_at", json.loads((run / "retention.json").read_text()))
        self.assertTrue((fresh / "raw" / "agent.jsonl").is_file())

    def test_removing_streams_does_not_restart_the_retention_clock(self):
        run = self.output_run("run-delegate-mid-cccc", age=20 * DAY)
        self.prune("--apply")
        self.assertTrue(run.exists())
        # Ten days later by the policy's clock: the run is 30 days old, not 10.
        result = self.prune("--apply", "--older-than", "20d")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(run.exists())

    def test_a_run_whose_launching_process_is_alive_is_never_pruned(self):
        running = self.output_run("run-delegate-live-eeee", age=90 * DAY, owner="alive")
        finished = self.output_run("run-delegate-done-ffff", age=90 * DAY, owner="dead")
        result = self.prune("--apply", "--older-than", "0", "--detail-older-than", "0")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((running / "raw" / "agent.jsonl").is_file())
        self.assertFalse(finished.exists())

    def test_a_run_without_a_recorded_process_is_kept_while_recently_written(self):
        recent = self.output_run("run-ask-legacy-1111", age=1 * HOUR, owner=None)
        quiet = self.output_run("run-ask-legacy-2222", age=7 * HOUR, owner=None)
        result = self.prune("--apply", "--older-than", "0")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((recent / "raw" / "agent.jsonl").is_file())
        self.assertFalse(quiet.exists())

    def test_size_cap_takes_the_oldest_streams_first_and_spares_the_first_day(self):
        oldest = self.output_run("run-delegate-a-0001", age=50 * HOUR, detail=600_000)
        middle = self.output_run("run-delegate-b-0002", age=30 * HOUR, detail=600_000)
        today = self.output_run("run-delegate-c-0003", age=2 * HOUR, detail=600_000)
        # 3.6 MB of streams against a 2.5 MB cap: one run must give its streams up.
        result = self.prune("--apply", "--max-detail-size", "2500K")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((oldest / "raw").exists())
        self.assertTrue((middle / "raw" / "agent.jsonl").is_file())
        self.assertTrue((today / "raw" / "agent.jsonl").is_file())
        # A cap nothing can satisfy still leaves the run of the last day alone.
        result = self.prune("--apply", "--max-detail-size", "1K")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((middle / "raw").exists())
        self.assertTrue((today / "raw" / "agent.jsonl").is_file())
        self.assertEqual((oldest / "final.txt").read_text(), "the answer")

    def test_zero_cap_disables_the_size_limit(self):
        run = self.output_run("run-delegate-a-0001", age=50 * HOUR, detail=600_000)
        result = self.prune("--apply", "--max-detail-size", "0")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((run / "raw" / "agent.jsonl").is_file())

    def test_only_run_folders_of_the_outputs_dir_are_touched(self):
        other = self.outputs / "notes"
        other.mkdir()
        (other / "keep.txt").write_text("mine")
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "precious.txt").write_text("precious")
        os.symlink(outside, self.outputs / "run-link-9999")
        for path in (other / "keep.txt", outside / "precious.txt"):
            os.utime(path, (self.now - 400 * DAY, self.now - 400 * DAY))
        result = self.prune("--apply", "--older-than", "0")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((other / "keep.txt").read_text(), "mine")
        self.assertEqual((outside / "precious.txt").read_text(), "precious")
        self.assertTrue((self.outputs / "run-link-9999").is_symlink())

    def test_environment_sets_the_policy_and_flags_override_it(self):
        run = self.output_run("run-delegate-mid-cccc", age=10 * DAY)
        self.prune("--apply", env={"PORCH_RETENTION_DAYS": "7"})
        self.assertFalse(run.exists())
        run = self.output_run("run-delegate-mid-dddd", age=10 * DAY)
        self.prune("--apply", "--older-than", "14d", env={"PORCH_RETENTION_DAYS": "7"})
        self.assertTrue(run.exists())

    def test_invalid_policy_is_a_usage_error_that_names_its_source(self):
        result = self.prune("--older-than", "soon")
        self.assertEqual(result.returncode, 5)
        self.assertIn("--older-than", result.stderr)
        result = self.prune(env={"PORCH_RETENTION_DETAIL_MAX": "lots"})
        self.assertEqual(result.returncode, 5)
        self.assertIn("PORCH_RETENTION_DETAIL_MAX", result.stderr)

    def test_json_plan_reports_every_run_with_its_action(self):
        self.output_run("run-delegate-old-aaaa", age=40 * DAY)
        self.output_run("run-delegate-live-eeee", age=40 * DAY, owner="alive")
        plan = json.loads(self.prune("--json").stdout)
        actions = {row["name"]: (row["state"], row["action"]) for row in plan["runs"]}
        self.assertEqual(actions["run-delegate-old-aaaa"], ("finished", "remove"))
        self.assertEqual(actions["run-delegate-live-eeee"], ("running", "keep"))
        self.assertFalse(plan["applied"])


class RegistryRetentionTest(RetentionCase):
    def test_finished_registry_runs_past_retention_are_removed(self):
        _, old = self.registry_run("old", age=31 * DAY)
        _, failed = self.registry_run("failed", age=45 * DAY, status="failed")
        _, young = self.registry_run("young", age=5 * DAY)
        result = self.prune("--apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(old.exists())
        self.assertFalse(failed.exists())
        self.assertEqual((young / "final.txt").read_text(), "registry answer")

    def test_a_running_registry_run_and_its_outputs_are_never_pruned(self):
        outputs = self.output_run("run-delegate-live-eeee", age=90 * DAY, owner="dead")
        _, live = self.registry_run("live", age=90 * DAY, status="running", pid=os.getpid(), artifacts=outputs)
        result = self.prune("--apply", "--older-than", "0", "--detail-older-than", "0")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(live.is_dir())
        self.assertTrue((outputs / "raw" / "agent.jsonl").is_file())

    def test_a_run_whose_supervisor_died_counts_as_finished(self):
        _, stale = self.registry_run("stale", age=40 * DAY, status="running")
        result = self.prune("--apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(stale.exists())

    def test_a_session_chain_is_kept_until_every_run_in_it_is_past_retention(self):
        anchor_id, anchor = self.registry_run("anchor", age=60 * DAY)
        _, follow = self.registry_run("follow", age=5 * DAY, anchor=anchor_id)
        result = self.prune("--apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(anchor.is_dir(), "the anchor holds the session lock of a retained continuation")
        self.assertTrue(follow.is_dir())
        result = self.prune("--apply", "--older-than", "4d")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(anchor.exists())
        self.assertFalse(follow.exists())

    def test_an_unreadable_registry_entry_is_reported_and_left_alone(self):
        _, broken = self.registry_run("broken", age=90 * DAY)
        (broken / "meta.json").write_text("{not json")
        _, old = self.registry_run("old", age=90 * DAY)
        result = self.prune("--apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(broken.is_dir())
        self.assertFalse(old.exists())
        self.assertIn("skipped 1 unreadable", result.stdout)


class ShellHooksTest(RetentionCase):
    def bash(self, script, **env):
        base = {key: value for key, value in os.environ.items()
                if not key.startswith("PORCH_") and key not in ("XDG_STATE_HOME",)}
        base.update(env)
        return subprocess.run(["bash", "-c", script], env=base, capture_output=True, text=True, timeout=60)

    def test_default_outputs_folder_is_durable_user_state_not_the_temp_folder(self):
        script = f'source "{LIB_DIR}/common.sh"; printf %s "$PORCH_OUTPUT_DIR"'
        home = self.root / "home"
        result = self.bash(script, HOME=str(home), TMPDIR=str(self.root / "tmp"))
        self.assertEqual(result.stdout, f"{home}/.local/state/pragmatic-orchestration/outputs", result.stderr)
        result = self.bash(script, HOME=str(home), XDG_STATE_HOME=str(self.root / "state"))
        self.assertEqual(result.stdout, f"{self.root}/state/pragmatic-orchestration/outputs")
        result = self.bash(script, HOME=str(home), PORCH_OUTPUT_DIR="/custom/place")
        self.assertEqual(result.stdout, "/custom/place")

    def test_porch_prune_uses_the_default_outputs_folder(self):
        home = self.root / "home"
        result = subprocess.run([str(PORCH), "prune"], capture_output=True, text=True, timeout=60,
                                env={"PATH": os.environ["PATH"], "HOME": str(home),
                                     "PORCH_STEER_DIR": str(self.steer)})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"{home}/.local/state/pragmatic-orchestration/outputs", result.stdout)

    def init_run(self, **env):
        script = (f'source "{LIB_DIR}/common.sh"; source "{LIB_DIR}/artifacts.sh"; '
                  'artifacts_init_run ask; printf "%s %s" "$PORCH_RUN_DIR" "$$"')
        result = self.bash(script, HOME=str(self.root / "home"), PORCH_OUTPUT_DIR=str(self.outputs),
                           PORCH_STEER_DIR=str(self.steer), **env)
        self.assertEqual(result.returncode, 0, result.stderr)
        run_dir, shell_pid = result.stdout.rsplit(" ", 1)
        return Path(run_dir), int(shell_pid)

    def test_a_new_run_records_its_launching_process(self):
        run_dir, shell_pid = self.init_run(PORCH_RETENTION_AUTO="0")
        self.assertEqual(json.loads((run_dir / "meta.json").read_text())["owner_pid"], shell_pid)

    def wait_for(self, condition, seconds=20):
        deadline = time.time() + seconds
        while time.time() < deadline:
            if condition():
                return True
            time.sleep(0.1)
        return condition()

    def test_creating_a_run_prunes_in_the_background_at_most_once_per_interval(self):
        old = self.output_run("run-delegate-old-aaaa", age=40 * DAY)
        self.init_run()
        self.assertTrue(self.wait_for(lambda: not old.exists()), "the automatic prune did not remove the old run")
        self.assertTrue(self.wait_for(lambda: "porch prune" in (self.outputs / ".retention.log").read_text()))
        # Within the interval a second run does not prune again.
        again = self.output_run("run-delegate-old-bbbb", age=40 * DAY)
        self.init_run()
        time.sleep(1.5)
        self.assertTrue(again.exists())
        self.assertEqual((self.outputs / ".retention.log").read_text().count("porch prune"), 1)

    def test_automatic_prune_can_be_switched_off(self):
        old = self.output_run("run-delegate-old-aaaa", age=40 * DAY)
        self.init_run(PORCH_RETENTION_AUTO="0")
        time.sleep(1.5)
        self.assertTrue(old.exists())
        self.assertFalse((self.outputs / ".retention-stamp").exists())


if __name__ == "__main__":
    unittest.main()
