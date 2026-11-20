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
        # In-process tests never read the real disk's free space: a failed scan
        # measures it through volume_now, which each test sets when it matters.
        self.volume = [(None, None, "not measured in this test")]
        self.volume_patch = mock.patch.object(observe, "volume_now", side_effect=lambda config: self.volume[0])
        self.volume_patch.start()
        self.real_volume_now = self.volume_patch.temp_original

    def tearDown(self):
        self.volume_patch.stop()
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
        # The pressure level depends on the real disk the test runs on.
        self.assertRegex(text, r"Pressure: (quiet|decide)")
        self.assertIn("Volume:", text)
        self.assertRegex(text, r"Observer: scan took \d+\.\d min")
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
        # Background priority starved the scan while the user was away; the
        # template must not bring it back.
        for key in ("ProcessType", "LowPriorityIO", "Nice"):
            self.assertNotIn(key, parsed)

    def failing_scan_commands(self, *, timed_out=True, last_free=None, days_ago=2.0):
        """Scan fails; trend answers from one earlier scan when last_free is given."""
        stamp = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days_ago)).isoformat()
        trend = {"pressure": "quiet", "volume": {"free_bytes": last_free, "size_bytes": 100 * 1024**3,
                 "days_to_full": None}, "stocks": [],
                 "scans": [{"file": "scan-old.json", "scanned_at": stamp, "free_bytes": last_free}]}

        def call(argv, **kwargs):
            command = argv[2]
            if command == "scan":
                return (-15, b"", b"", True) if timed_out else (1, b"", b"offline", False)
            if command == "rotate":
                return 0, b"", b"", False
            if command == "trend":
                if last_free is None:
                    return 1, b"", b"no scans", False
                if "--json" in argv:
                    return 0, json.dumps(trend).encode(), b"", False
                return 0, b"Volume: old trend text\n", b"", False
            raise AssertionError("unexpected command: {}".format(argv))
        return call

    def observe_failed(self, config, notices, log=None, **commands):
        with mock.patch.object(observe, "run_command", side_effect=self.failing_scan_commands(**commands)):
            with mock.patch.object(observe, "notify", side_effect=lambda *args: notices.append(args[1]) or True):
                return observe.observe("laptop", dict(config, root=str(self.root), stocks=str(self.stock)),
                                       [], observe.DEFAULTS, False, log or (lambda _: None))

    def test_timed_out_scan_reports_failure_and_free_space_and_alerts_at_once(self):
        gib = 1024**3
        self.volume[0] = (16 * gib, 100 * gib, None)
        notices, log = [], []
        failed = self.observe_failed({"target_free": "60G", "floor_free": "30G", "scan_timeout_minutes": "45"},
                                     notices, log.append, last_free=56 * gib, days_ago=2.0)
        self.assertTrue(failed)
        report = self.report().read_text()
        self.assertIn("scan failed \u2014 BELOW FLOOR.", report.splitlines()[0])
        self.assertIn("Scan failed: scan timed out after 45.0 minutes.", report)
        self.assertIn("Free space now (df): 16.0 GiB of 100.0 GiB, below the floor (30G)", report)
        self.assertRegex(report, r"Since the last saved scan \(.* UTC, 2\.0 days ago\): -40\.0 GiB, "
                                 r"20\.0 GiB/day filling; full in 0\.8 days at that rate")
        self.assertIn("Trend from earlier scans:", report)
        # The very first failed run already raises the floor alert.
        self.assertEqual(notices, [observe.DEFAULTS["floor"]])
        self.assertTrue(any("scan timed out after 45.0 minutes" in line and "free now 16.0 GiB of 100.0 GiB" in line
                            for line in log), log)

    def test_failed_scan_alerts_on_a_fast_drop_that_is_still_above_the_target(self):
        gib = 1024**3
        self.volume[0] = (70 * gib, 100 * gib, None)
        notices = []
        self.observe_failed({"target_free": "60G"}, notices, last_free=90 * gib, days_ago=1.0)
        # 20 GiB/day leaves 3.5 days: inside the four-day horizon.
        self.assertEqual(notices, [observe.DEFAULTS["now"]])
        self.assertIn("full in 3.5 days at that rate", self.report().read_text())

    def test_failed_scan_below_target_notifies_decide_once_then_only_the_failing_observer(self):
        gib = 1024**3
        self.volume[0] = (50 * gib, 100 * gib, None)
        notices = []
        config = {"target_free": "60G"}
        self.observe_failed(config, notices, timed_out=False)
        self.assertEqual(notices, [observe.DEFAULTS["decide"]])
        self.observe_failed(config, notices, timed_out=False)
        self.assertEqual(notices, [observe.DEFAULTS["decide"], observe.DEFAULTS["scan_failed"]])
        self.assertIn("Free space now (df): 50.0 GiB of 100.0 GiB, below the target (60G)", self.report().read_text())

    def test_failed_scan_with_enough_free_space_sends_no_space_alert(self):
        gib = 1024**3
        self.volume[0] = (80 * gib, 100 * gib, None)
        notices = []
        self.observe_failed({"target_free": "60G", "floor_free": "30G"}, notices, last_free=81 * gib, days_ago=3.0)
        self.assertEqual(notices, [])
        report = self.report().read_text()
        self.assertIn("scan failed.", report.splitlines()[0])
        self.assertIn("Free space now (df): 80.0 GiB of 100.0 GiB, above the target (60G)", report)

    def test_failed_scan_says_so_when_free_space_cannot_be_measured(self):
        self.volume[0] = (None, None, "df failed (ssh exit 255): no route")
        notices = []
        self.observe_failed({"target_free": "60G", "floor_free": "30G"}, notices, timed_out=False)
        report = self.report().read_text()
        self.assertIn("Free space now: unknown (df failed (ssh exit 255): no route)", report)
        self.assertIn("- free space check failed: df failed (ssh exit 255): no route", report)
        self.assertEqual(notices, [])

    def test_real_timeout_writes_a_report_with_the_free_space_statvfs_reports(self):
        self.write_config(extra="scan_timeout_minutes = 0.00001\nfloor_free = 1K")
        (self.root / "sample").write_bytes(b"test data")
        result = self.run_runner("--no-notify")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        report = self.report().read_text()
        self.assertIn("scan failed.", report.splitlines()[0])
        self.assertIn("Scan failed: scan timed out after 1e-05 minutes.", report)
        vfs = os.statvfs(self.root)
        total = observe.pretty_bytes(vfs.f_blocks * vfs.f_frsize)
        self.assertRegex(report, r"Free space now \(df\): [0-9.]+ [KMGT]iB of {}, ".format(total.replace(".", r"\.")))
        self.assertIn("free now ", result.stdout)

    def test_volume_now_reads_the_local_volume_and_parses_remote_df(self):
        free, total, error = self.real_volume_now({"root": str(self.root)})
        vfs = os.statvfs(self.root)
        self.assertIsNone(error)
        self.assertEqual(total, vfs.f_blocks * vfs.f_frsize)
        self.assertLess(abs(free - vfs.f_bavail * vfs.f_frsize), 1024**3)
        df = (b"Filesystem     1024-blocks      Used Available Capacity Mounted on\n"
              b"/dev/sda1        206292968 150000000  45787560      77% /\n")
        seen = []
        with mock.patch.object(observe, "run_command", side_effect=lambda argv, **kw: seen.append(argv) or (0, df, b"", False)):
            self.assertEqual(self.real_volume_now({"root": "/srv data", "ssh": "build-host"}),
                             (45787560 * 1024, 206292968 * 1024, None))
        self.assertEqual(seen[0][-2:], ["build-host", "df -Pk -- '/srv data'"])
        with mock.patch.object(observe, "run_command", return_value=(255, b"", b"no route", False)):
            self.assertEqual(self.real_volume_now({"root": "/", "ssh": "h"}),
                             (None, None, "df failed (ssh exit 255): no route"))
        with mock.patch.object(observe, "run_command", return_value=(0, b"garbage\n", b"", False)):
            self.assertIn("unexpected df output", self.real_volume_now({"root": "/", "ssh": "h"})[2])
        with mock.patch.object(observe, "run_command", return_value=(-9, b"", b"", True)):
            self.assertIn("df timed out", self.real_volume_now({"root": "/", "ssh": "h"})[2])

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
import json,os,subprocess,sys,time
args_path = os.environ.get('FAKE_SSH_ARGS_FILE')
if args_path:
    with open(args_path,'w') as stream: json.dump(sys.argv[1:],stream)
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
        ssh_args_file = self.base / "ssh-args.json"
        notify_file = self.base / "remote-notify.txt"
        notify = self.base / "notify.py"
        notify.write_text("#!/usr/bin/env python3\nimport sys\nopen(%r,'a').write(sys.argv[-1]+'\\n')\n" % str(notify_file), encoding="utf-8")
        notify.chmod(0o755)
        env = dict(self.env, PATH=bindir + os.pathsep + self.env.get("PATH", ""), FAKE_SSH_MODE="success",
                   FAKE_SSH_ARGS_FILE=str(ssh_args_file))
        self.remote_config()
        self.config.write_text(self.config.read_text().replace("command = /usr/bin/true", "command = {}".format(notify)), encoding="utf-8")
        success = self.run_runner(env=env)
        self.assertEqual(success.returncode, 0, success.stdout + success.stderr)
        ssh_args = json.loads(ssh_args_file.read_text())
        self.assertIn(["-o", "ControlMaster=no"], [ssh_args[index:index + 2] for index in range(len(ssh_args) - 1)])
        self.assertIn(["-o", "ControlPath=none"], [ssh_args[index:index + 2] for index in range(len(ssh_args) - 1)])
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

    def test_remote_command_bounds_scan_with_best_effort_priority(self):
        command = observe.remote_command("/srv", 45)
        self.assertIn("timeout --kill-after=30 2640", command)
        self.assertIn("ionice -c2 -n7", command)
        self.assertIn("nice -n 10", command)
        self.assertIn("command -v timeout", command)
        self.assertIn("command -v ionice", command)
        self.assertIn("command -v nice", command)
        self.assertIn("timeout --kill-after=30 60", observe.remote_command("/srv", 0.01))

    def test_remote_timeout_permission_error_kills_client_and_reports_timeout(self):
        bindir, marker = self.make_ssh()
        self.remote_config(timeout="0.01")
        env = dict(self.env, PATH=bindir + os.pathsep + self.env.get("PATH", ""), FAKE_SSH_MODE="sleep", FAKE_SSH_PID=str(marker))
        config = {"root": str(self.root), "ssh": "stub-host", "target_free": "100G",
                  "stocks": str(self.base / "missing.tsv"), "scan_timeout_minutes": "0.01"}
        log = []
        with mock.patch.dict(os.environ, env):
            with mock.patch.object(observe.os, "killpg", side_effect=PermissionError("not permitted")):
                failed = observe.observe("laptop", config, [], observe.DEFAULTS, True, log.append)
        self.assertTrue(failed)
        self.assertTrue(marker.exists())
        pid = int(marker.read_text())
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)
        report = self.report().read_text()
        self.assertIn("scan timed out after 0.01 minutes", report)
        self.assertNotIn("remote scan could not start", report)
        self.assertTrue(any("scan timed out after 0.01 minutes" in line for line in log))

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
