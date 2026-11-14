import datetime
import importlib.util
import json
import os
import pathlib
import plistlib
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock


SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "observe.py"
DISK_MAP = SCRIPT.with_name("disk_map.py")
SPEC = importlib.util.spec_from_file_location("observe", SCRIPT)
observe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(observe)


class ObserveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = pathlib.Path(self.temp.name)
        self.real_home = pathlib.Path.home()
        self.test_home = self.base / "home"
        self.test_home.mkdir()
        self.root = self.base / "root"
        self.root.mkdir()
        self.state = self.base / "state"
        self.config_dir = self.base / "config"
        self.config_dir.mkdir()
        self.config = self.config_dir / "observer.ini"
        self.stock = self.base / "stocks.tsv"
        self.stock.write_text("stock\tpath\tempties\nnothing\t{}\trules\n".format(self.root), encoding="utf-8")
        self.env = os.environ.copy()
        self.env["XDG_STATE_HOME"] = str(self.state)
        self.env["XDG_CONFIG_HOME"] = str(self.config_dir)
        self.env["HOME"] = str(self.test_home)
        self.environ_patch = mock.patch.dict(os.environ, self.env)
        self.environ_patch.start()
        self.state_base_provider = [observe.state_base]

        def guarded_state_base():
            candidate = pathlib.Path(self.state_base_provider[0]()).resolve()
            allowed = self.state.resolve()
            try:
                inside = os.path.commonpath((str(candidate), str(allowed))) == str(allowed)
            except ValueError:
                inside = False
            if not inside:
                raise AssertionError("observe() state_base escaped test temporary folder: {}".format(candidate))
            return str(candidate)

        self.state_guard = mock.patch.object(observe, "state_base", guarded_state_base)
        self.state_guard.start()

    def tearDown(self):
        self.state_guard.stop()
        self.environ_patch.stop()
        self.temp.cleanup()

    def write_config(self, machine="laptop", extra="", notify=""):
        self.config.write_text(
            "[notify]\ncommand = /usr/bin/true\n" + notify + "\n[{}]\nroot = {}\ntarget_free = 100G\n"
            "stocks = {}\n{}\n".format(machine, self.root, self.stock, extra), encoding="utf-8")

    def run_runner(self, *args, env=None, timeout=30):
        return subprocess.run([sys.executable, str(SCRIPT), "--config", str(self.config), *args],
                              env=env or self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True, timeout=timeout)

    def report(self, machine="laptop"):
        return self.state / "stale-data-cleanup" / machine / "reports" / "latest.md"

    def test_real_local_observations_atomic_scan_rotation_and_report(self):
        self.write_config(extra="keep = 1")
        (self.root / "sample").write_bytes(b"test data")
        first = self.run_runner()
        self.assertEqual(first.returncode, 0, first.stderr + first.stdout)
        scans_dir = self.state / "stale-data-cleanup" / "laptop" / "scans"
        scans = list(scans_dir.glob("scan-*.json"))
        self.assertEqual(len(scans), 1)
        first_name = scans[0].name
        self.assertFalse(list(scans_dir.glob("*.tmp")))
        second = self.run_runner()
        self.assertEqual(second.returncode, 0, second.stderr + second.stdout)
        scans = list(scans_dir.glob("scan-*.json"))
        self.assertEqual(len(scans), 1)
        self.assertNotEqual(scans[0].name, first_name)
        text = self.report().read_text(encoding="utf-8")
        self.assertIn("Disk watch: laptop, ", text)
        self.assertIn("Pressure: quiet", text)
        self.assertIn("Volume:", text)
        self.assertEqual(text, (self.report().parent / "report-{}.md".format(datetime.date.today())).read_text(encoding="utf-8"))

    def test_notification_cadence_and_override(self):
        recorder = self.base / "notify.py"
        args_file = self.base / "notification.jsonl"
        recorder.write_text("#!/usr/bin/env python3\nimport json,sys\nopen(%r,'a').write(json.dumps(sys.argv[1:])+'\\n')\n" % str(args_file), encoding="utf-8")
        recorder.chmod(0o755)
        self.write_config(extra="floor_free = 1B", notify="command = {}\ndecide = custom {{machine}} {{pressure}}\n".format(recorder))
        # Use a controlled trend object while retaining real scans and rotation.
        result = {"pressure": "decide", "volume": {"free_bytes": 10, "size_bytes": 100,
                  "days_to_full": None}, "stocks": []}
        with mock.patch.object(observe, "run_command", side_effect=self.controlled_commands(result)):
            self.assertFalse(observe.observe("laptop", {"root": str(self.root), "target_free": "100G",
                              "stocks": str(self.stock), "floor_free": "1B"}, [str(recorder)],
                              {**observe.DEFAULTS, "decide": "custom {machine} {pressure}"}, False, lambda _: None))
            self.assertEqual(args_file.read_text().count("\n"), 1)
            first_day = datetime.date.today()
            for offset, expected_count in ((1, 1), (3, 2)):
                class ShiftedDate:
                    @classmethod
                    def today(cls):
                        return first_day + datetime.timedelta(days=offset)
                with mock.patch.object(observe.datetime, "date", ShiftedDate):
                    with mock.patch.object(observe, "run_command", side_effect=self.controlled_commands(result)):
                        observe.observe("laptop", {"root": str(self.root), "target_free": "100G", "stocks": str(self.stock), "floor_free": "1B"},
                                        [str(recorder)], {**observe.DEFAULTS, "decide": "custom {machine} {pressure}"}, False, lambda _: None)
                self.assertEqual(args_file.read_text().count("\n"), expected_count)
        self.assertIn('"custom laptop decide"', args_file.read_text())

    def controlled_commands(self, result):
        # Keep actual disk_map invocations for scan and rotate.
        def actual(argv, **kwargs):
            if len(argv) < 3 or argv[2] not in ("scan", "rotate", "trend"):
                return original(argv, **kwargs)
            if argv[2] in ("scan", "rotate"):
                return original(argv, **kwargs)
            if "--json" in argv:
                return 0, json.dumps(result).encode(), b"", False
            return 0, b"Trend text output", b"", False
        original = observe.run_command
        return actual

    def scan_result_commands(self, free, total, *, scan_failure=False, trend_failure=True):
        data = {"format": "disk-map/1", "root": str(self.root), "volume": {
            "free_bytes": free, "size_bytes": total}, "tree": {}}
        self.mocked_commands = []

        def call(argv, **kwargs):
            self.mocked_commands.append(argv)
            command = argv[2]
            if command == "scan":
                if scan_failure:
                    return 1, b"", b"stub scan failure", False
                destination = argv[argv.index("-o") + 1]
                pathlib.Path(destination).write_text(json.dumps(data), encoding="utf-8")
                return 0, b"", b"", False
            if command == "rotate":
                return 0, b"", b"", False
            if command == "trend":
                return (1 if trend_failure else 2), b"", b"stub trend unavailable", False
            raise AssertionError("unexpected command: {}".format(argv))
        return call

    def test_fresh_scan_fallback_when_stocks_or_trend_are_unavailable(self):
        notifications = []
        notify_patch = mock.patch.object(observe, "notify", side_effect=lambda *args: notifications.append(args[1]) or True)
        with notify_patch:
            with mock.patch.object(observe, "run_command", side_effect=self.scan_result_commands(5 * 1024**3, 100 * 1024**3)):
                observe.observe("below-floor", {"root": str(self.root), "target_free": "20G", "floor_free": "10G",
                                "stocks": str(self.base / "missing.tsv")}, [], observe.DEFAULTS, False, lambda _: None)
            self.assertFalse(any(command[2] == "trend" for command in self.mocked_commands))
            floor_report = self.state / "stale-data-cleanup" / "below-floor" / "reports" / "latest.md"
            floor_text = floor_report.read_text()
            self.assertIn("decide — BELOW FLOOR", floor_text)
            self.assertIn("trend unavailable", floor_text)
            self.assertIn("pressure is from the fresh scan alone", floor_text)
            self.assertEqual(notifications[-1], observe.DEFAULTS["floor"])

            with mock.patch.object(observe, "run_command", side_effect=self.scan_result_commands(15 * 1024**3, 100 * 1024**3)):
                reduced = observe.observe("under-target", {"root": str(self.root), "target_free": "20G", "floor_free": "10G",
                                          "stocks": str(self.base / "missing.tsv")}, [], observe.DEFAULTS, False, lambda _: None)
            self.assertFalse(reduced, "a missing stocks file is a reduced mode, not a failed observation")
            target_report = self.state / "stale-data-cleanup" / "under-target" / "reports" / "latest.md"
            self.assertIn("Disk watch: under-target, ", target_report.read_text())
            self.assertIn("decide.", target_report.read_text())
            self.assertEqual(notifications[-1], observe.DEFAULTS["decide"])
            self.assertFalse(any(command[2] == "trend" for command in self.mocked_commands))

            with mock.patch.object(observe, "run_command", side_effect=self.scan_result_commands(15 * 1024**3, 100 * 1024**3)):
                trend_failed = observe.observe("trend-failed", {"root": str(self.root), "target_free": "20G", "floor_free": "10G",
                                               "stocks": str(self.stock)}, [], observe.DEFAULTS, False, lambda _: None)
            self.assertTrue(trend_failed, "a trend that fails with a stocks file present is a failure")
            trend_report = self.state / "stale-data-cleanup" / "trend-failed" / "reports" / "latest.md"
            self.assertIn("trend failed", trend_report.read_text())
            self.assertIn("pressure is from the fresh scan alone", trend_report.read_text())
            self.assertEqual(notifications[-1], observe.DEFAULTS["decide"])

    def test_failed_fresh_scan_cannot_use_prior_pressure_or_floor(self):
        commands = []
        notices = []
        scan_count = [0]
        good_scan = {"format": "disk-map/1", "root": str(self.root), "volume": {
            "free_bytes": 5 * 1024**3, "size_bytes": 100 * 1024**3}, "tree": {}}
        latest_trend = {"pressure": "decide", "volume": {"free_bytes": 5 * 1024**3,
                        "size_bytes": 100 * 1024**3, "days_to_full": None}, "stocks": []}
        old_trend = {"pressure": "now", "volume": {"free_bytes": 1 * 1024**3,
                      "size_bytes": 100 * 1024**3, "days_to_full": 0.2}, "stocks": []}

        def controlled(argv, **kwargs):
            commands.append(argv)
            if argv[2] == "scan":
                scan_count[0] += 1
                if scan_count[0] > 1:
                    return 1, b"", b"offline", False
                pathlib.Path(argv[argv.index("-o") + 1]).write_text(json.dumps(good_scan), encoding="utf-8")
                return 0, b"", b"", False
            if argv[2] == "rotate":
                return 0, b"", b"", False
            if argv[2] == "trend":
                value = latest_trend if scan_count[0] == 1 else old_trend
                if "--json" in argv:
                    return 0, json.dumps(value).encode(), b"", False
                return 0, "Pressure: {}\n".format(value["pressure"]).encode(), b"", False
            raise AssertionError("unexpected command: {}".format(argv))

        config = {"root": str(self.root), "target_free": "20G", "floor_free": "10G", "stocks": str(self.stock)}
        with mock.patch.object(observe, "run_command", side_effect=controlled):
            with mock.patch.object(observe, "notify", side_effect=lambda *args: notices.append(args[1]) or True):
                self.assertFalse(observe.observe("laptop", config, [], observe.DEFAULTS, False, lambda _: None))
                self.assertEqual(notices, [observe.DEFAULTS["floor"]])
                self.assertTrue(observe.observe("laptop", config, [], observe.DEFAULTS, False, lambda _: None))
                failed_report = self.report().read_text()
                self.assertIn("Disk watch: laptop, ", failed_report)
                self.assertIn("scan failed.", failed_report)
                self.assertIn("Trend from earlier scans:", failed_report)
                state_path = self.state / "stale-data-cleanup" / "laptop" / ".observer-state.json"
                self.assertEqual(json.loads(state_path.read_text())["previous_pressure"], "decide")
                self.assertEqual(notices, [observe.DEFAULTS["floor"]])
                self.assertTrue(observe.observe("laptop", config, [], observe.DEFAULTS, False, lambda _: None))
        self.assertEqual(notices, [observe.DEFAULTS["floor"], observe.DEFAULTS["scan_failed"]])
        self.assertIn("scan failed.", self.report().read_text())

    def test_decimal_gigabyte_message_placeholders(self):
        values = {"free_gb": observe.decimal_gb(118_600_000_000),
                  "total_gb": observe.decimal_gb(500_000_000_000)}
        self.assertEqual(values, {"free_gb": "118.6", "total_gb": "500.0"})
        self.assertEqual(observe.decimal_gb(None), "unknown")
        calls = []
        with mock.patch.object(observe, "run_command", side_effect=lambda argv, **kwargs: calls.append(argv) or (0, b"", b"", False)):
            self.assertTrue(observe.notify(["stub"], "Free {free_gb} of {total_gb} GB", values, lambda _: None))
        self.assertEqual(calls[0][-1], "Free 118.6 of 500.0 GB")

    def test_main_logs_machine_exception_and_continues(self):
        self.config.write_text(
            "[notify]\ncommand = /usr/bin/true\n[first]\nroot = {}\ntarget_free = 100G\nstocks = {}\n"
            "[second]\nroot = {}\ntarget_free = 100G\nstocks = {}\n".format(self.root, self.stock, self.root, self.stock),
            encoding="utf-8")
        visited = []

        def fail_first(name, *args):
            visited.append(name)
            if name == "first":
                raise OSError("report folder unwritable")
            return False

        with mock.patch.object(observe, "observe", side_effect=fail_first):
            code = observe.main(["--config", str(self.config)])
        self.assertEqual(code, 2)
        self.assertEqual(visited, ["first", "second"])
        log = (self.state / "stale-data-cleanup" / "observer.log").read_text()
        self.assertIn("first: unexpected error: OSError: report folder unwritable", log)

    def test_state_guard_stops_observation_before_real_state_write(self):
        real_state = self.real_home / ".local" / "state" / "stale-data-cleanup"
        self.state_base_provider[0] = lambda: str(real_state)
        with self.assertRaisesRegex(AssertionError, "escaped test temporary folder"):
            observe.observe("laptop", {"root": str(self.root), "target_free": "100G"}, [], observe.DEFAULTS, True, lambda _: None)

    def test_upkeep_threshold_guidance_is_general(self):
        upkeep = SCRIPT.parents[1] / "references" / "upkeep.md"
        text = upkeep.read_text(encoding="utf-8")
        self.assertIn("`target_free`", text)
        self.assertIn("`floor_free`", text)
        self.assertIn("100G` means 100 GiB", text)
        self.assertIn("2, 5, 8", text)
        self.assertNotIn("The Mac target is 100 GB", text)

    def test_launchd_template_keeps_stable_launcher_arguments_separate(self):
        template = SCRIPT.parents[1] / "assets" / "observer-launchd.plist"
        text = template.read_text(encoding="utf-8")
        self.assertIn("stable launcher path", text)
        values = {
            "__LABEL__": "org.example.disk-watch",
            "__INTERPRETER_COMMAND__": "/opt/homebrew/bin/uv",
            "__RUNNER_PATH__": "/tmp/observe.py",
            "__CONFIG_PATH__": "/tmp/observer.ini",
            "__HOUR__": "3",
            "__MINUTE__": "15",
            "__LOG_FOLDER__": "/tmp/logs",
        }
        for key, value in values.items():
            text = text.replace(key, value)
        parsed = plistlib.loads(text.encode("utf-8"))
        self.assertEqual(parsed["ProgramArguments"][:5], ["/opt/homebrew/bin/uv", "run", "--no-project", "python", "/tmp/observe.py"])
        self.assertFalse(parsed["RunAtLoad"])

    def test_notification_timeout_kills_process_group(self):
        stub = self.base / "slow_notify.py"
        pid_file = self.base / "notify.pid"
        stub.write_text("#!/usr/bin/env python3\nimport os,time\nopen(%r,'w').write(str(os.getpid()))\ntime.sleep(20)\n" % str(pid_file), encoding="utf-8")
        stub.chmod(0o755)
        messages = []
        with mock.patch.object(observe, "NOTIFY_TIMEOUT_SECONDS", 1.0):
            sent = observe.notify([sys.executable, str(stub)], "test {machine}", {"machine": "laptop"}, messages.append)
        self.assertFalse(sent)
        self.assertIn("timed out", messages[-1])
        with self.assertRaises(ProcessLookupError):
            os.kill(int(pid_file.read_text()), 0)

    def test_scan_failure_message_repeats_on_failures_two_five_and_eight(self):
        captured = []
        with mock.patch.object(observe, "notify", side_effect=lambda *args: captured.append(args[1]) or True):
            for _ in range(8):
                with mock.patch.object(observe, "run_command", side_effect=self.scan_result_commands(0, 1, scan_failure=True)):
                    observe.observe("unreachable", {"root": str(self.root), "target_free": "100G",
                                    "stocks": str(self.base / "missing.tsv")}, [], observe.DEFAULTS, False, lambda _: None)
        self.assertEqual(captured, [observe.DEFAULTS["scan_failed"]] * 3)

    def test_notification_failure_has_one_could_not_do_section(self):
        result = {"pressure": "now", "volume": {"free_bytes": 5, "size_bytes": 100, "days_to_full": 1}, "stocks": []}
        def controlled(argv, **kwargs):
            if argv[2] == "scan":
                destination = argv[argv.index("-o") + 1]
                pathlib.Path(destination).write_text(json.dumps({"format": "disk-map/1", "root": str(self.root),
                                         "volume": {"free_bytes": 5, "size_bytes": 100}, "tree": {}}), encoding="utf-8")
                return 0, b"", b"", False
            if argv[2] == "rotate":
                return 0, b"", b"", False
            if "--json" in argv:
                return 0, json.dumps(result).encode(), b"", False
            return 0, b"Pressure: now\n", b"", False
        with mock.patch.object(observe, "run_command", side_effect=controlled):
            with mock.patch.object(observe, "notify", return_value=False):
                failed = observe.observe("notify-fails", {"root": str(self.root), "target_free": "100G",
                                       "stocks": str(self.stock)}, [], observe.DEFAULTS, False, lambda _: None)
        self.assertTrue(failed)
        report = (self.state / "stale-data-cleanup" / "notify-fails" / "reports" / "latest.md").read_text()
        self.assertEqual(report.count("Could not do"), 1)
        self.assertIn("notification command failed", report)

    def test_quiet_now_and_floor_notify_each_run_and_no_notify(self):
        recorder = self.base / "notify.py"
        args_file = self.base / "notify-args"
        recorder.write_text("#!/usr/bin/env python3\nimport sys\nopen(%r,'a').write(sys.argv[-1]+'\\n')\n" % str(args_file), encoding="utf-8")
        recorder.chmod(0o755)
        machine = {"root": str(self.root), "target_free": "100G", "stocks": str(self.stock)}
        def run(pressure, floor=None, no_notify=False):
            value = {"pressure": pressure, "volume": {"free_bytes": 10, "size_bytes": 100, "days_to_full": 0.5}, "stocks": []}
            with mock.patch.object(observe, "run_command", side_effect=self.controlled_commands(value)):
                observe.observe("laptop", {**machine, **({"floor_free": floor} if floor else {})}, [str(recorder)], observe.DEFAULTS, no_notify, lambda _: None)
        run("quiet")
        self.assertFalse(args_file.exists())
        run("now")
        run("now")
        run("quiet", floor="100G")
        run("quiet", floor="100G")
        run("decide", floor="100G", no_notify=True)
        self.assertEqual(args_file.read_text().count("\n"), 4)
        self.assertIn("BELOW FLOOR", self.report().read_text())

    def test_notification_is_one_literal_argument(self):
        recorder = self.base / "notify.py"
        args_file = self.base / "argv.json"
        recorder.write_text("#!/usr/bin/env python3\nimport json,sys\nopen(%r,'w').write(json.dumps(sys.argv[1:]))\n" % str(args_file), encoding="utf-8")
        recorder.chmod(0o755)
        message = 'quote " and $(touch SHOULD_NOT_EXIST)'
        machine = {"root": str(self.root), "target_free": "100G", "stocks": str(self.stock)}
        value = {"pressure": "now", "volume": {"free_bytes": 5, "size_bytes": 100, "days_to_full": 1}, "stocks": []}
        with mock.patch.object(observe, "run_command", side_effect=self.controlled_commands(value)):
            observe.observe("laptop", machine, [str(recorder), "fixed"], {**observe.DEFAULTS, "now": message}, False, lambda _: None)
        self.assertEqual(json.loads(args_file.read_text()), ["fixed", message])
        self.assertFalse((self.base / "SHOULD_NOT_EXIST").exists())

    def make_ssh(self, mode="success"):
        bindir = self.base / "bin"
        bindir.mkdir(exist_ok=True)
        fake = bindir / "ssh"
        marker = self.base / "ssh.pid"
        fake.write_text("""#!/usr/bin/env python3
import os,subprocess,sys,time
mode = os.environ.get('FAKE_SSH_MODE','success')
if mode == 'fail': sys.exit(255)
if mode == 'empty': sys.exit(0)
if mode == 'sleep':
    open(os.environ['FAKE_SSH_PID'],'w').write(str(os.getpid()))
    time.sleep(20)
    sys.exit(0)
cmd = sys.argv[-1]
p = subprocess.run(['/bin/sh','-c',cmd], input=sys.stdin.buffer.read())
sys.exit(p.returncode)
""", encoding="utf-8")
        fake.chmod(0o755)
        return str(bindir), marker

    def remote_config(self, extra="", timeout="45"):
        self.write_config(extra="ssh = stub-host\nscan_timeout_minutes = {}\n{}".format(timeout, extra))

    def test_remote_ssh_success_unreachable_empty_and_two_failure_message(self):
        bindir, _ = self.make_ssh()
        notify_file = self.base / "remote-notify.txt"
        notify = self.base / "notify.py"
        notify.write_text("#!/usr/bin/env python3\nimport sys\nopen(%r,'a').write(sys.argv[-1]+'\\n')\n" % str(notify_file), encoding="utf-8")
        notify.chmod(0o755)
        env = dict(self.env, PATH=bindir + os.pathsep + self.env.get("PATH", ""), FAKE_SSH_MODE="success")
        self.remote_config()
        self.config.write_text(self.config.read_text().replace("command = /usr/bin/true", "command = {}".format(notify)), encoding="utf-8")
        success = self.run_runner(env=env)
        self.assertEqual(success.returncode, 0, success.stdout + success.stderr)
        env["FAKE_SSH_MODE"] = "fail"
        first = self.run_runner(env=env)
        second = self.run_runner(env=env)
        self.assertEqual(first.returncode, 2)
        self.assertEqual(second.returncode, 2)
        self.assertIn("remote scan failed", self.report().read_text())
        self.assertIn("Disk observation is still failing", notify_file.read_text())
        self.assertEqual(notify_file.read_text().count("Disk observation is still failing"), 1)
        env["FAKE_SSH_MODE"] = "empty"
        empty = self.run_runner(env=env)
        self.assertEqual(empty.returncode, 2)
        self.assertIn("empty scan output", self.report().read_text())

    def test_remote_timeout_terminates_process_group(self):
        bindir, marker = self.make_ssh()
        self.remote_config(timeout="0.01")
        env = dict(self.env, PATH=bindir + os.pathsep + self.env.get("PATH", ""), FAKE_SSH_MODE="sleep", FAKE_SSH_PID=str(marker))
        result = self.run_runner(env=env, timeout=10)
        self.assertEqual(result.returncode, 2)
        self.assertTrue(marker.exists())
        pid = int(marker.read_text())
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)
        self.assertIn("timed out", self.report().read_text())

    def test_lock_skips_second_run_and_releases_when_first_is_killed(self):
        bindir, marker = self.make_ssh()
        self.remote_config(timeout="1")
        env = dict(self.env, PATH=bindir + os.pathsep + self.env.get("PATH", ""), FAKE_SSH_MODE="sleep", FAKE_SSH_PID=str(marker))
        first = subprocess.Popen([sys.executable, str(SCRIPT), "--config", str(self.config)], env=env,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        limit = time.monotonic() + 5
        while not marker.exists() and time.monotonic() < limit:
            time.sleep(0.02)
        self.assertTrue(marker.exists())
        second = self.run_runner(env=env)
        self.assertEqual(second.returncode, 0)
        self.assertIn("observer already running; skipped", second.stdout)
        first.kill()
        first.wait(timeout=5)
        first.communicate()
        try:
            os.kill(int(marker.read_text()), 9)
        except ProcessLookupError:
            pass
        next_env = dict(env, FAKE_SSH_MODE="success")
        next_run = self.run_runner(env=next_env)
        self.assertEqual(next_run.returncode, 0, next_run.stdout + next_run.stderr)

    def test_config_errors_name_key_and_dry_run_writes_nothing(self):
        self.write_config(extra="unknown = x")
        bad = self.run_runner()
        self.assertEqual(bad.returncode, 3)
        self.assertIn("unknown", bad.stderr)
        self.write_config()
        self.config.write_text(self.config.read_text().replace("target_free = 100G", "target_free = nonsense"), encoding="utf-8")
        bad_size = self.run_runner()
        self.assertEqual(bad_size.returncode, 3)
        self.assertIn("target_free", bad_size.stderr)
        self.write_config(extra="ssh = -oProxyCommand=unsafe")
        bad_ssh = self.run_runner()
        self.assertEqual(bad_ssh.returncode, 3)
        self.assertIn("ssh", bad_ssh.stderr)
        self.config.write_text("[notify]\ncommand = /usr/bin/true\n", encoding="utf-8")
        missing = self.run_runner()
        self.assertEqual(missing.returncode, 3)
        self.assertIn("machine section", missing.stderr)
        self.write_config()
        dry = self.run_runner("--dry-run")
        self.assertEqual(dry.returncode, 0, dry.stderr)
        self.assertIn("# laptop", dry.stdout)
        self.assertIn("trend", dry.stdout)
        self.assertFalse(self.state.exists())

    def test_machine_selection_requires_a_section(self):
        self.write_config()
        missing = self.run_runner("--machine", "missing")
        self.assertEqual(missing.returncode, 3)
        self.assertIn("[missing]", missing.stderr)


if __name__ == "__main__":
    unittest.main()
