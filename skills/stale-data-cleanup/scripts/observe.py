#!/usr/bin/env python3
"""Run one scheduled, read-only disk observation for configured machines."""

import argparse
import configparser
import datetime
import fcntl
import json
import math
import os
import re
import shlex
import signal
import string
import subprocess
import sys
import time


HERE = os.path.dirname(os.path.abspath(__file__))
DISK_MAP = os.path.join(HERE, "disk_map.py")
MACHINE_KEYS = {"root", "ssh", "target_free", "floor_free", "stocks",
                "scan_timeout_minutes", "keep"}
NOTIFY_KEYS = {"command", "decide", "now", "floor", "scan_failed"}
SIZE_RE = re.compile(r"(?:[0-9]+(?:\.[0-9]+)?[KMGTPE]?(?:i?B)?|[0-9]+(?:\.[0-9]+)?%)\Z", re.I)
NOTIFY_TIMEOUT_SECONDS = 60
# A failed scan still reports free space; the check must not outlast the run.
DF_TIMEOUT_SECONDS = 60
# Same horizon as disk_map.py trend: "now" means full within this many days.
HORIZON_DAYS = 4
DEFAULTS = {
    "decide": "Disk space on {machine} is below target: {free} free of {total}; target {target}, pressure {pressure}. Report: {report_path}",
    "now": "Disk space on {machine} needs attention now: {free} free of {total}; pressure {pressure}, {days_until_full} until full. Report: {report_path}",
    "floor": "URGENT: {machine} is below its free-space floor: {free} free of {total}; floor {floor}. Report: {report_path}",
    "scan_failed": "Disk observation is still failing for {machine}: {pressure}. Report: {report_path}",
}


class ConfigError(ValueError):
    pass


def fail_config(message):
    raise ConfigError(message)


def parse_config(path):
    parser = configparser.ConfigParser(interpolation=None, strict=True)
    parser.optionxform = str
    try:
        with open(path, encoding="utf-8") as stream:
            parser.read_file(stream)
    except (OSError, configparser.Error) as exc:
        fail_config("configuration file {}: {}".format(path, exc))
    if not parser.sections():
        fail_config("missing machine section (example: [laptop])")
    for section in parser.sections():
        allowed = NOTIFY_KEYS if section == "notify" else MACHINE_KEYS
        for key in parser[section]:
            if key not in allowed:
                fail_config("unknown key {!r} in [{}]; valid example: root = /path/to/scan".format(key, section))
    if "notify" not in parser:
        fail_config("missing [notify] section; valid example: command = /usr/bin/logger")
    if not any(section != "notify" for section in parser.sections()):
        fail_config("missing machine section; valid example: [laptop]")
    notify = dict(parser["notify"])
    try:
        command = shlex.split(notify.get("command", ""), posix=True)
    except ValueError as exc:
        fail_config("invalid notify command; valid example: command = /usr/bin/logger -t disk-watch ({})".format(exc))
    if not command:
        fail_config("missing key 'command' in [notify]; valid example: command = /usr/bin/logger -t disk-watch")
    templates = dict(DEFAULTS)
    fields = {"machine", "free", "total", "free_gb", "total_gb", "target", "floor",
              "pressure", "days_until_full", "report_path"}
    for key in DEFAULTS:
        if key in notify:
            templates[key] = notify[key]
        try:
            found = {name for _, name, _, _ in string.Formatter().parse(templates[key]) if name}
        except ValueError as exc:
            fail_config("invalid key {!r} in [notify]: {}; use named placeholders such as {{machine}}".format(key, exc))
        unknown = found - fields
        if unknown:
            fail_config("invalid key {!r} in [notify]: unknown placeholder {}; valid example: {{machine}}".format(key, sorted(unknown)[0]))
    machines = {}
    for name in parser.sections():
        if name == "notify":
            continue
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name) or name in (".", ".."):
            fail_config("invalid machine section {!r}; use a simple name such as [laptop]".format(name))
        values = dict(parser[name])
        for needed in ("root", "target_free"):
            if not values.get(needed):
                example = "/srv" if needed == "root" else "100G"
                fail_config("missing key {!r} in [{}]; valid example: {} = {}".format(needed, name, needed, example))
        if not os.path.isabs(os.path.expanduser(values["root"])):
            fail_config("invalid key 'root' in [{}]: use an absolute path such as /srv".format(name))
        if values.get("ssh", "").startswith("-"):
            fail_config("invalid key 'ssh' in [{}]: a host alias cannot begin with '-'; use ssh = build-host".format(name))
        for key in ("target_free", "floor_free"):
            if key in values and not SIZE_RE.fullmatch(values[key]):
                fail_config("invalid key {!r} in [{}]: {!r}; use 100G or 20%".format(key, name, values[key]))
        for key, example in (("keep", "8"), ("scan_timeout_minutes", "45")):
            if key in values:
                try:
                    number = float(values[key]) if key == "scan_timeout_minutes" else int(values[key])
                except ValueError:
                    fail_config("invalid key {!r} in [{}]: use {}".format(key, name, example))
                if not math.isfinite(number) or number <= 0 or (key == "keep" and number < 1):
                    qualifier = "positive integer" if key == "keep" else "positive number"
                    fail_config("invalid key {!r} in [{}]: use a {} such as {}".format(key, name, qualifier, example))
        machines[name] = values
    return machines, command, templates


def state_base():
    state_home = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return os.path.join(state_home,
                        "stale-data-cleanup")


def run_command(argv, *, stdin=None, timeout=None, cwd=None):
    proc = subprocess.Popen(argv, stdin=stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            cwd=cwd, start_new_session=True)
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        signal_process(proc, signal.SIGTERM)
        try:
            out, err = proc.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            signal_process(proc, signal.SIGKILL)
            out, err = proc.communicate()
        return proc.returncode, out, err, True
    return proc.returncode, out, err, False


def signal_process(proc, signum):
    try:
        os.killpg(proc.pid, signum)
    except OSError:
        try:
            proc.kill()
        except OSError:
            pass


def remote_command(root, scan_timeout_minutes):
    remote_seconds = max(60, int(float(scan_timeout_minutes) * 60 - 60))
    scan = "python3 - scan {}".format(shlex.quote(root))
    return ("T=''; if command -v timeout >/dev/null 2>&1; then T='timeout --kill-after=30 {}'; fi; "
            "I=''; if command -v ionice >/dev/null 2>&1; then I='ionice -c2 -n7'; fi; "
            "N=''; if command -v nice >/dev/null 2>&1; then N='nice -n 10'; fi; "
            "exec $T $I $N {}".format(remote_seconds, scan))


def volume_now(config):
    """Free and total bytes of the watched volume, measured without a scan.

    Returns (free, total, error). Locally this is statvfs, the call `df` makes
    and the one disk_map.py records in a scan; remotely it is `df -Pk` over ssh.
    """
    root = config["root"]
    if not config.get("ssh"):
        try:
            vfs = os.statvfs(os.path.expanduser(root))
        except OSError as exc:
            return None, None, str(exc)
        return vfs.f_bavail * vfs.f_frsize, vfs.f_blocks * vfs.f_frsize, None
    argv = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", "-o", "ControlMaster=no",
            "-o", "ControlPath=none", config["ssh"], "df -Pk -- {}".format(shlex.quote(root))]
    try:
        code, out, err, timed_out = run_command(argv, timeout=DF_TIMEOUT_SECONDS)
    except OSError as exc:
        return None, None, str(exc)
    if timed_out:
        return None, None, "df timed out after {} seconds".format(DF_TIMEOUT_SECONDS)
    if code:
        return None, None, "df failed (ssh exit {}): {}".format(code, err.decode("utf-8", "replace").strip())
    lines = out.decode("utf-8", "replace").strip().splitlines()
    fields = lines[-1].split() if len(lines) >= 2 else []
    try:
        return int(fields[3]) * 1024, int(fields[1]) * 1024, None
    except (IndexError, ValueError):
        return None, None, "unexpected df output: {!r}".format(lines[-1] if lines else "")


def last_scan_point(result):
    """(free_bytes, scanned_at as aware datetime) of the newest saved scan, if any."""
    for item in reversed((result or {}).get("scans") or []):
        free, stamp = item.get("free_bytes"), item.get("scanned_at")
        if free is None or not stamp:
            continue
        try:
            when = datetime.datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
        except ValueError:
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=datetime.timezone.utc)
        return free, when
    return None


def load_average_text():
    try:
        return "{:.2f}/{:.2f}/{:.2f}".format(*os.getloadavg())
    except (OSError, AttributeError):
        return None


def pretty_bytes(value):
    if value is None:
        return "unknown"
    value = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB", "PiB"):
        if abs(value) < 1024 or unit == "PiB":
            return "{} {}".format(int(value) if unit == "B" else "{:.1f}".format(value), unit)
        value /= 1024


def decimal_gb(value):
    return "unknown" if value is None else "{:.1f}".format(value / 1_000_000_000)


def size_bytes(value, volume_size):
    if value.endswith("%"):
        return volume_size * float(value[:-1]) / 100
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)([KMGTPE]?)(?:i?B)?", value, re.I)
    if not match:
        return None
    power = "KMGTPE".find(match.group(2).upper()) + 1 if match.group(2) else 0
    return float(match.group(1)) * (1024 ** power)


def notify(command, template, values, log):
    message = template.format(**values).replace("\n", " ").replace("\r", " ")
    try:
        code, _out, err, timed_out = run_command(command + [message], timeout=NOTIFY_TIMEOUT_SECONDS)
    except OSError as exc:
        log("notify command could not start: {}".format(exc))
        return False
    if timed_out:
        log("notify command timed out after {} seconds; process group terminated".format(NOTIFY_TIMEOUT_SECONDS))
        return False
    if code:
        log("notify command failed with exit {}: {}".format(code, err.decode("utf-8", "replace").strip()))
        return False
    log("notification sent")
    return True


def observe(name, config, command, templates, no_notify, log):
    state = os.path.join(state_base(), name)
    scans = os.path.join(state, "scans")
    reports = os.path.join(state, "reports")
    os.makedirs(scans, exist_ok=True)
    os.makedirs(reports, exist_ok=True)
    today = datetime.date.today().isoformat()
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    final_scan = os.path.join(scans, "scan-{}.json".format(stamp))
    temp_scan = final_scan + ".tmp"
    report_path = os.path.join(reports, "report-{}.md".format(today))
    errors = []
    scan_ok = False
    scan_data = None
    timeout_minutes = float(config.get("scan_timeout_minutes", "45"))
    scan_started = time.monotonic()
    argv = [sys.executable, DISK_MAP, "scan", config["root"]]
    if config.get("ssh"):
        argv = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15",
                "-o", "ControlMaster=no", "-o", "ControlPath=none", config["ssh"],
                remote_command(config["root"], timeout_minutes)]
        try:
            script = open(DISK_MAP, "rb")
        except OSError as exc:
            errors.append("remote scan could not start: {}".format(exc))
            script = None
        if script is not None:
            try:
                output = open(temp_scan, "wb")
            except OSError as exc:
                script.close()
                errors.append("remote scan could not start: {}".format(exc))
                output = None
            if output is not None:
                try:
                    try:
                        proc = subprocess.Popen(argv, stdin=script, stdout=output, stderr=subprocess.PIPE,
                                                start_new_session=True)
                    except OSError as exc:
                        errors.append("remote scan could not start: {}".format(exc))
                        proc = None
                    if proc is not None:
                        try:
                            _, stderr = proc.communicate(timeout=timeout_minutes * 60)
                            timed_out = False
                        except subprocess.TimeoutExpired:
                            signal_process(proc, signal.SIGTERM)
                            try:
                                _, stderr = proc.communicate(timeout=2)
                            except subprocess.TimeoutExpired:
                                signal_process(proc, signal.SIGKILL)
                                _, stderr = proc.communicate()
                            timed_out = True
                        if timed_out:
                            errors.append("scan timed out after {} minutes".format(timeout_minutes))
                        elif proc.returncode:
                            errors.append("remote scan failed (ssh exit {}): {}".format(
                                proc.returncode, stderr.decode("utf-8", "replace").strip()))
                        else:
                            scan_ok = True
                finally:
                    script.close()
                    output.close()
    else:
        local_argv = argv + ["-o", temp_scan]
        code, _out, err, timed_out = run_command(local_argv, timeout=timeout_minutes * 60)
        if timed_out:
            errors.append("scan timed out after {} minutes".format(timeout_minutes))
        elif code:
            errors.append("scan failed (exit {}): {}".format(code, err.decode("utf-8", "replace").strip()))
        else:
            scan_ok = True
    if scan_ok:
        try:
            if os.path.getsize(temp_scan) == 0:
                raise ValueError("empty scan output")
            with open(temp_scan, "r", encoding="utf-8") as stream:
                scan_data = json.load(stream)
            if not isinstance(scan_data, dict) or scan_data.get("format") != "disk-map/1":
                raise ValueError("not disk-map/1 JSON")
            os.replace(temp_scan, final_scan)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            errors.append("scan output was invalid: {}".format(exc))
            scan_ok = False
    try:
        os.unlink(temp_scan)
    except FileNotFoundError:
        pass
    scan_minutes = (time.monotonic() - scan_started) / 60
    scan_note = "scan {} {:.1f} min".format("took" if scan_ok else "ran", scan_minutes)
    if not config.get("ssh") and load_average_text():
        scan_note += ", load average {}".format(load_average_text())
    # A failed scan must not make a fast drop silent: measure free space directly.
    df_error = None
    if not scan_ok:
        df_free, df_total, df_error = volume_now(config)
        if df_error:
            errors.append("free space check failed: {}".format(df_error))

    rotate_argv = [sys.executable, DISK_MAP, "rotate", scans, "--keep", config.get("keep", "8")]
    code, out, err, _ = run_command(rotate_argv)
    if code:
        errors.append("rotate failed (exit {}): {}".format(code, err.decode("utf-8", "replace").strip()))

    stocks = config.get("stocks", os.path.join(state, "stocks.tsv"))
    missing_stocks = not os.path.isfile(stocks)
    result = None
    trend_text = ""
    if missing_stocks:
        errors.append("trend unavailable: disk_map.py requires a stocks file; create {}".format(stocks))
    else:
        trend_argv = [sys.executable, DISK_MAP, "trend", scans, "--stocks", stocks,
                      "--target-free", config["target_free"], "--json"]
        code, out, err, _ = run_command(trend_argv)
        if code:
            errors.append("trend failed (exit {}): {}".format(code, err.decode("utf-8", "replace").strip()))
        else:
            try:
                result = json.loads(out.decode("utf-8"))
                trend_text = json.dumps(result, ensure_ascii=False, indent=2)
                # The JSON result drives policy; keep the human-facing trend output
                # intact in the report rather than synthesizing a second format.
                plain_argv = [part for part in trend_argv if part != "--json"]
                plain_code, plain_out, plain_err, _ = run_command(plain_argv)
                if plain_code:
                    errors.append("trend text output failed (exit {}): {}".format(
                        plain_code, plain_err.decode("utf-8", "replace").strip()))
                else:
                    trend_text = plain_out.decode("utf-8", "replace").rstrip()
            except (ValueError, UnicodeDecodeError) as exc:
                errors.append("trend returned invalid JSON: {}".format(exc))

    scan_volume = (scan_data or {}).get("volume") or {}
    failed_space = None
    failed_days_to_full = None
    failed_lines = []
    if not scan_ok:
        pressure = "scan failed"
        free = None if df_error else df_free
        total = None if df_error else df_total
        failed_lines.append("Scan failed: {}. The numbers in this block are measured now.".format(
            next((item for item in errors if item.startswith(("scan ", "remote scan "))), "see below")))
        if free is None:
            failed_lines.append("Free space now: unknown ({})".format(df_error))
        else:
            target_bytes = size_bytes(config["target_free"], total)
            floor_bytes = size_bytes(config["floor_free"], total) if config.get("floor_free") else None
            if floor_bytes is not None and free < floor_bytes:
                level = "below the floor ({})".format(config["floor_free"])
            elif target_bytes is not None and free < target_bytes:
                level = "below the target ({})".format(config["target_free"])
            else:
                level = "above the target ({})".format(config["target_free"])
            failed_lines.append("Free space now (df): {} of {}, {}".format(pretty_bytes(free), pretty_bytes(total), level))
            if target_bytes is not None and free < target_bytes:
                failed_space = "decide"
            point = last_scan_point(result)
            if point:
                days = (datetime.datetime.now(datetime.timezone.utc) - point[1]).total_seconds() / 86400
                change = free - point[0]
                line = "Since the last saved scan ({}, {:.1f} days ago): {}{}".format(
                    point[1].strftime("%Y-%m-%d %H:%M UTC"), days, "+" if change >= 0 else "-", pretty_bytes(abs(change)))
                if days > 0 and change < 0:
                    rate = -change / days
                    failed_days_to_full = free / rate
                    line += ", {}/day filling; full in {:.1f} days at that rate".format(pretty_bytes(rate), failed_days_to_full)
                    if failed_days_to_full <= HORIZON_DAYS:
                        failed_space = "now"
                failed_lines.append(line)
        if result:
            trend_text = "Trend from earlier scans:\n\n" + trend_text
    elif result:
        pressure = result.get("pressure", "unknown")
        free = result.get("volume", {}).get("free_bytes")
        total = result.get("volume", {}).get("size_bytes")
    elif scan_volume.get("free_bytes") is not None and scan_volume.get("size_bytes") is not None:
        free = scan_volume["free_bytes"]
        total = scan_volume["size_bytes"]
        target_bytes = size_bytes(config["target_free"], total)
        pressure = "decide" if target_bytes is not None and free < target_bytes else "quiet"
        errors.append("trend unavailable; pressure is from the fresh scan alone (now requires trend history)")
    else:
        pressure = "scan failed" if not scan_ok else "unknown"
        free = None
        total = None
    floor = config.get("floor_free")
    below_floor = False
    if free is not None and floor and total is not None:
        floor_bytes = size_bytes(floor, total)
        below_floor = floor_bytes is not None and free < floor_bytes
    header_pressure = pressure + (" — BELOW FLOOR" if below_floor else "")

    memory_path = os.path.join(state, ".observer-state.json")
    try:
        with open(memory_path, encoding="utf-8") as stream:
            memory = json.load(stream)
    except (OSError, ValueError):
        memory = {}
    previous_streak = int(memory.get("failure_streak", 0))
    failed = not scan_ok
    failure_streak = previous_streak + 1 if failed else 0
    memory["failure_streak"] = failure_streak
    message_values = {
        "machine": name, "free": pretty_bytes(free), "total": pretty_bytes(total),
        "free_gb": decimal_gb(free), "total_gb": decimal_gb(total),
        "target": config["target_free"], "floor": floor or "unset", "pressure": header_pressure,
        "days_until_full": ("{:.1f} days".format(result["volume"]["days_to_full"])
                            if scan_ok and result and result["volume"].get("days_to_full") is not None
                            else "{:.1f} days".format(failed_days_to_full) if failed_days_to_full is not None
                            else "unknown"),
        "report_path": report_path,
    }
    # Space alerts and the failing-observer alert are separate signals: a failed
    # scan still raises floor/now/decide from the free space measured just now.
    template_keys = []
    if not no_notify:
        space = failed_space if failed else pressure
        if below_floor:
            template_keys.append("floor")
        elif space == "now":
            template_keys.append("now")
        elif space == "decide":
            day = datetime.date.today().toordinal()
            last = memory.get("last_decide_day")
            if memory.get("previous_pressure") != "decide" or last is None or day - int(last) >= 3:
                template_keys.append("decide")
                memory["last_decide_day"] = day
        if failed and failure_streak >= 2 and (failure_streak - 2) % 3 == 0:
            template_keys.append("scan_failed")
    if scan_ok and pressure in ("quiet", "decide", "now"):
        memory["previous_pressure"] = pressure
    elif failed and free is not None:
        # Free space was measured, so the repeat interval of "decide" applies.
        memory["previous_pressure"] = failed_space or "quiet"
    notify_failed = False
    for template_key in template_keys:
        if not notify(command, templates[template_key], message_values, log):
            notify_failed = True
    if notify_failed:
        errors.append("notification command failed")
    body = "Disk watch: {}, {}. {}.\n\n".format(name, today, header_pressure)
    if failed_lines:
        body += "\n".join(failed_lines) + "\n\n"
    body += trend_text or "Trend output unavailable."
    body += "\n\nObserver: {}".format(scan_note)
    if errors:
        body += "\n\nCould not do\n" + "\n".join("- " + item for item in errors)
    for path in (report_path, os.path.join(reports, "latest.md")):
        temp = path + ".tmp"
        with open(temp, "w", encoding="utf-8") as stream:
            stream.write(body + "\n")
        os.replace(temp, path)
    temp_mem = memory_path + ".tmp"
    with open(temp_mem, "w", encoding="utf-8") as stream:
        json.dump(memory, stream)
    os.replace(temp_mem, memory_path)
    summary = "; ".join(errors) if errors else "observed; pressure {}".format(pressure)
    if failed and free is not None:
        summary += "; free now {} of {}".format(pretty_bytes(free), pretty_bytes(total))
    log("{}: {}; {}".format(name, summary, scan_note))
    # A missing stocks file is a documented reduced mode, not a failed observation:
    # neither of the two lines it adds to the report counts as a failure.
    reduced_mode = ("trend unavailable: disk_map.py requires a stocks file",
                    "trend unavailable; pressure is from the fresh scan alone")
    other_failures = [e for e in errors if not (missing_stocks and e.startswith(reduced_mode))]
    return bool(failed or other_failures or notify_failed)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    config_home = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    parser.add_argument("--config", default=os.path.join(config_home,
                                                           "stale-data-cleanup", "observer.ini"))
    parser.add_argument("--machine")
    parser.add_argument("--no-notify", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        machines, command, templates = parse_config(args.config)
        if args.machine:
            if args.machine not in machines:
                fail_config("missing machine section [{}]; valid example: [laptop]".format(args.machine))
            selected = {args.machine: machines[args.machine]}
        else:
            selected = machines
    except ConfigError as exc:
        print("configuration error: {}".format(exc), file=sys.stderr)
        return 3
    if args.dry_run:
        for name, config in selected.items():
            root = config["root"]
            scans = os.path.join(state_base(), name, "scans")
            out = os.path.join(scans, "scan-DATE.json.tmp")
            if config.get("ssh"):
                scan_line = "ssh -o BatchMode=yes -o ConnectTimeout=15 -o ControlMaster=no -o ControlPath=none {} {} < {} > {}".format(
                    shlex.quote(config["ssh"]), shlex.quote(remote_command(root, config.get("scan_timeout_minutes", "45"))),
                    shlex.quote(DISK_MAP), shlex.quote(out))
            else:
                scan_line = "{} {} scan {} -o {}".format(shlex.quote(sys.executable), shlex.quote(DISK_MAP),
                                                         shlex.quote(root), shlex.quote(out))
            stocks = config.get("stocks", os.path.join(state_base(), name, "stocks.tsv"))
            if os.path.isfile(stocks):
                trend = "{} {} trend {} --stocks {} --target-free {} --json".format(
                    shlex.quote(sys.executable), shlex.quote(DISK_MAP), shlex.quote(scans),
                    shlex.quote(stocks), shlex.quote(config["target_free"]))
            else:
                trend = "# trend skipped: stocks file missing ({})".format(shlex.quote(stocks))
            print("# {}\n{}\n{} {} rotate {} --keep {}\n{}".format(
                name, scan_line, shlex.quote(sys.executable), shlex.quote(DISK_MAP), shlex.quote(scans),
                shlex.quote(config.get("keep", "8")), trend))
        return 0
    base = state_base()
    os.makedirs(base, exist_ok=True)
    log_path = os.path.join(base, "observer.log")
    def log(message):
        line = "{} {}\n".format(datetime.datetime.now().isoformat(timespec="seconds"), message)
        with open(log_path, "a", encoding="utf-8") as stream:
            stream.write(line)
        print(line, end="")
    lock_path = os.path.join(base, "observer.lock")
    lock = open(lock_path, "a+")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        print("observer already running; skipped")
        return 0
    failures = False
    try:
        for name, config in selected.items():
            try:
                if observe(name, config, command, templates, args.no_notify, log):
                    failures = True
            except Exception as exc:
                failures = True
                log("{}: unexpected error: {}: {}".format(name, type(exc).__name__, exc))
        return 2 if failures else 0
    finally:
        lock.close()


if __name__ == "__main__":
    sys.exit(main())
