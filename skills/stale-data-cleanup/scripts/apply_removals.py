#!/usr/bin/env python3
"""Guarded removal step for stale-data-cleanup.

Removes a path only when its row names a rule and evidence, the path lies inside
an allowed prefix, and the re-checks pass (protected names, version-control
state, key-like files, recency, live processes). Writes a manifest that
`disk_map.py reconcile` reads. Dry run unless --apply. Standard library only.
"""

import argparse
import datetime
import fnmatch
import json
import os
import shutil
import subprocess
import sys
import time


MANIFEST_HEADER = ["time", "action", "path", "bytes", "files", "rule", "evidence", "status"]
PROTECTED_COMPONENTS = {".ssh", ".gnupg", ".aws", ".azure", ".kube", ".password-store", "Keychains"}
HOME_CHILDREN = {"Documents", "Desktop", "Downloads", "Pictures", "Movies", "Music", "Library", "Applications", "Public", ".config", ".local"}
DEPENDENCY_DEFAULT = {"node_modules", ".venv", "venv", "site-packages", "__pycache__"}
ENV_EXAMPLES = {".env.example", ".env.sample", ".env.template", ".env.dist", ".env.defaults"}


def components(path):
    return [part for part in path.split(os.sep) if part]


def inside(path, prefix, strict=False):
    p, q = components(path), components(prefix)
    return len(p) > len(q) and p[:len(q)] == q if strict else len(p) >= len(q) and p[:len(q)] == q


def key_like(name):
    return (name == ".env" or (name.startswith(".env.") and name not in ENV_EXAMPLES) or
            any(fnmatch.fnmatchcase(name, pat) for pat in ("*.pem", "*.key", "*.p12", "*.pfx", "*.kdbx", "id_rsa*", "id_ed25519*")))


def human(n):
    value = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return "%g %s" % (value, unit) if unit == "B" else "%.1f %s" % (value, unit)
        value /= 1024


def parse_list(path):
    try:
        with open(path, "r", encoding="utf-8", newline="") as f:
            lines = [line for line in f if line.strip() and not line.lstrip().startswith("#")]
    except (OSError, UnicodeError) as e:
        raise ValueError("cannot read list: %s" % e)
    if not lines:
        raise ValueError("list has no header")
    try:
        headers = lines[0].rstrip("\r\n").split("\t")
        rows = []
        for line in lines[1:]:
            fields = line.rstrip("\r\n").split("\t")
            if len(fields) > len(headers):
                if headers and headers[0] == "path":
                    extra = len(fields) - len(headers)
                    fields = ["\t".join(fields[:extra + 1])] + fields[extra + 1:]
                else:
                    raise ValueError("list row has extra columns")
            fields += [""] * (len(headers) - len(fields))
            rows.append(dict(zip(headers, fields)))
    except (ValueError, UnicodeError) as e:
        raise ValueError("invalid list: %s" % e)
    if not {"path", "rule", "evidence"}.issubset(set(headers)):
        raise ValueError("list requires path, rule, and evidence columns")
    return rows


def collect_process_cwds():
    processes = []
    if os.path.isdir("/proc"):
        try:
            pids = os.listdir("/proc")
        except OSError:
            return None, False
        for pid in pids:
            if not pid.isdigit():
                continue
            try:
                cwd = os.path.realpath(os.readlink("/proc/%s/cwd" % pid))
                try:
                    with open("/proc/%s/comm" % pid, encoding="utf-8") as f:
                        command = f.read().strip()
                except OSError:
                    command = "unknown"
                processes.append((int(pid), command, cwd))
            except OSError:
                continue
        return processes, True
    try:
        result = subprocess.run(["lsof", "-a", "-d", "cwd", "-Fpcn"], stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, text=True, timeout=20, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None, False
    pid, command = None, "unknown"
    for line in result.stdout.splitlines():
        if line.startswith("p"):
            try:
                pid = int(line[1:])
            except ValueError:
                pid = None
        elif line.startswith("c"):
            command = line[1:] or "unknown"
        elif line.startswith("n"):
            processes.append((pid, command, os.path.realpath(line[1:])))
    return processes, True


def process_check(target, processes):
    real_target = os.path.realpath(target)
    for pid, command, cwd in processes or ():
        if pid is not None and (cwd == real_target or inside(cwd, real_target)):
            return (pid, command)
    return None


def walk_target(target, ignore_names, min_age):
    """One no-follow walk supplying filesystem, VCS, key, age, size and count facts."""
    try:
        root_stat = os.lstat(target)
    except OSError as e:
        return {"error": str(e)}
    result = {"mount": None, "repos": [], "jj_repos": [], "keys": [], "fresh": None, "bytes": 0, "files": 0}
    if not os.path.isdir(target) or os.path.islink(target):
        result["bytes"] = getattr(root_stat, "st_blocks", 0) * 512 or root_stat.st_size
        result["files"] = 1
        if stat_is_regular(root_stat.st_mode) and min_age > 0 and root_stat.st_mtime > time.time() - min_age * 86400:
            result["fresh"] = (time.time() - root_stat.st_mtime, os.path.basename(target))
        return result
    root_dev = root_stat.st_dev
    for current, dirs, files in os.walk(target, topdown=True, followlinks=False):
        rel_dir = os.path.relpath(current, target)
        if rel_dir == ".":
            rel_dir = ""
        git_entry = os.path.join(current, ".git")
        jj_entry = os.path.join(current, ".jj")
        in_ignored = any(part in ignore_names for part in components(rel_dir))
        if not in_ignored and (os.path.isdir(git_entry) or os.path.isfile(git_entry)):
            result["repos"].append(current)
        if not in_ignored and os.path.isdir(jj_entry):
            result["jj_repos"].append(current)
        for name in list(dirs) + files:
            full = os.path.join(current, name)
            rel = os.path.relpath(full, target)
            try:
                st = os.lstat(full)
            except OSError:
                continue
            if st.st_dev != root_dev and result["mount"] is None:
                result["mount"] = rel
            result["bytes"] += getattr(st, "st_blocks", 0) * 512 or st.st_size
            if not stat_is_dir(st.st_mode) or os.path.islink(full):
                result["files"] += 1
            parent_ignored = in_ignored
            entry_ignored = parent_ignored or name in ignore_names
            if not entry_ignored and (key_like(name) or (stat_is_dir(st.st_mode) and name in PROTECTED_COMPONENTS)):
                result["keys"].append(rel)
            if not entry_ignored and min_age > 0 and stat_is_regular(st.st_mode) and st.st_mtime > time.time() - min_age * 86400 and result["fresh"] is None:
                result["fresh"] = (time.time() - st.st_mtime, rel)
    result["repos"] = list(dict.fromkeys(result["repos"]))
    result["jj_repos"] = list(dict.fromkeys(result["jj_repos"]))
    return result


def stat_is_dir(mode):
    import stat
    return stat.S_ISDIR(mode)


def stat_is_regular(mode):
    import stat
    return stat.S_ISREG(mode)


def git_refusal(repos):
    if len(repos) > 50:
        return "refused: too many nested repositories"
    for repo in repos:
        git_path = os.path.join(repo, ".git")
        if repo == repos[0] and os.path.isfile(git_path) and not os.path.isdir(git_path):
            return 'refused: linked git worktree (use "git worktree remove")'
        env = dict(os.environ, GIT_OPTIONAL_LOCKS="0")
        commands = [(["git", "-C", repo, "status", "--porcelain", "--untracked-files=normal"], "status"),
                    (["git", "-C", repo, "stash", "list"], "stash"),
                    (["git", "-C", repo, "rev-list", "--count", "HEAD", "--branches", "--not", "--remotes"], "commits")]
        outputs = []
        for cmd, kind in commands:
            try:
                p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env, check=False)
            except OSError:
                return "refused: cannot verify git state"
            if p.returncode and kind == "commits":
                head = subprocess.run(["git", "-C", repo, "rev-parse", "--verify", "HEAD"],
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env, check=False)
                if head.returncode:
                    outputs.append("")
                    continue
            if p.returncode:
                return "refused: cannot verify git state"
            outputs.append(p.stdout.strip())
        if outputs[0]:
            count = len([x for x in outputs[0].splitlines() if x])
            return "refused: uncommitted changes in %s (%d paths)" % (repo, count)
        if outputs[1]:
            return "refused: stash entries in %s" % repo
        try:
            count = int(outputs[2] or "0")
        except ValueError:
            return "refused: cannot verify git state"
        if count > 0:
            return "refused: %d commits on no remote in %s" % (count, repo)
    return None


def remove_path(path):
    errors = []
    if os.path.islink(path) or not os.path.isdir(path):
        try:
            os.unlink(path)
        except OSError as e:
            errors.append(str(e))
    else:
        def onerror(func, name, exc_info):
            errors.append(str(exc_info[1]))
        try:
            if sys.version_info >= (3, 12):
                shutil.rmtree(path, onexc=lambda func, name, exc: errors.append(str(exc)))
            else:
                shutil.rmtree(path, onerror=onerror)
        except OSError as e:
            errors.append(str(e))
    left = 0
    if os.path.lexists(path):
        if os.path.isdir(path) and not os.path.islink(path):
            for _, dirs, files in os.walk(path, followlinks=False):
                left += len(dirs) + len(files)
            left += 1
        else:
            left = 1
        if not errors:
            errors.append("path still exists after removal")
    return errors, left


def clean_field(value):
    return str(value or "").replace("\t", " ").replace("\r", " ").replace("\n", " ")


def main(argv=None):
    parser = argparse.ArgumentParser(
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Remove listed paths only after re-checking each one, and record every decision.\n"
                    "Without --apply this is a dry run: it prints what it would do and writes nothing.",
        epilog="Example:\n"
               "  apply_removals.py list.tsv --allow-prefix ~/work --manifest removals.tsv          # dry run\n"
               "  apply_removals.py list.tsv --allow-prefix ~/work --manifest removals.tsv --apply\n\n"
               "The list is tab-separated with a header. Columns: path, rule, evidence (required);\n"
               "min_age_days (keep the target if a file in it is newer; default 1 day, 0 disables);\n"
               "allow (comma-separated per-row overrides: vcs, key-files).\n"
               "To remove a directory, allow its parent: an allowed prefix itself is never removed.\n"
               "A refused or kept row is a normal outcome (exit 0); exit 1 means a removal failed.")
    parser.add_argument("list", help="UTF-8 tab-separated candidate list")
    parser.add_argument("--allow-prefix", action="append", required=True, help="allowed parent directory (repeatable)")
    parser.add_argument("--manifest", required=True, help="TSV record written when --apply is used")
    parser.add_argument("--apply", action="store_true", help="remove rows that pass every check")
    parser.add_argument("--min-age-days", type=float, default=1, help="keep targets with files newer than this age (default: 1 day)")
    parser.add_argument("--dependency-dir", action="append", default=[], metavar="NAME", help="directory name whose contents are excluded from inspections (repeatable)")
    parser.add_argument("--no-process-check", action="store_true", help="skip checks for processes working in a target")
    parser.add_argument("--json", action="store_true", help="print results as JSON")
    args = parser.parse_args(argv)
    try:
        rows = parse_list(args.list)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 2
    if args.min_age_days < 0:
        print("min-age-days must be non-negative", file=sys.stderr)
        return 2
    prefixes = [os.path.normpath(os.path.abspath(os.path.expanduser(x))) for x in args.allow_prefix]
    prefix_reals = [os.path.realpath(x) for x in prefixes]
    manifest = None
    if args.apply:
        try:
            exists = os.path.exists(args.manifest)
            if exists:
                with open(args.manifest, "r", encoding="utf-8", newline="") as mf:
                    header = next(mf, "").rstrip("\r\n").split("\t")
                if header != MANIFEST_HEADER:
                    print("manifest header mismatch", file=sys.stderr)
                    return 2
            else:
                with open(args.manifest, "w", encoding="utf-8", newline="") as mf:
                    mf.write("\t".join(MANIFEST_HEADER) + "\n")
            manifest = open(args.manifest, "r+", encoding="utf-8", newline="")
        except OSError as e:
            print("cannot open manifest: %s" % e, file=sys.stderr)
            return 2
    results = []
    process_cache = [None, 0.0, True, False]
    for row in rows:
        raw_path = row.get("path") or ""
        rule = clean_field(row.get("rule"))
        evidence = clean_field(row.get("evidence"))
        allow = {x.strip() for x in (row.get("allow") or "").split(",") if x.strip()}
        status, target = None, None
        facts = {}
        used_overrides = set()
        size, count = 0, 0
        if not rule:
            status = "refused: no rule"
        elif not evidence:
            status = "refused: no evidence"
        elif not raw_path or "\x00" in raw_path or "\t" in raw_path or "\n" in raw_path or "\r" in raw_path or not os.path.isabs(raw_path) or ".." in components(raw_path):
            status = "refused: bad path"
        else:
            target = os.path.normpath(raw_path)
            real_parent = os.path.realpath(os.path.dirname(target))
            real_candidate = os.path.join(real_parent, os.path.basename(target))
            is_prefix = any(target == p or real_candidate == rp or
                            (os.path.isdir(target) and os.path.realpath(target) == rp)
                            for p, rp in zip(prefixes, prefix_reals))
            # Resolve the parent, but deliberately preserve the final component so a
            # symlink target is still checked and removed as a link.
            allowed = any(inside(real_candidate, rp, strict=True) for rp in prefix_reals)
            if is_prefix:
                status = "refused: is an allowed prefix"
            elif not allowed:
                status = "refused: outside allowed prefixes"
            else:
                home = os.path.normpath(os.path.expanduser("~"))
                comps = components(target)
                homecomps = components(home)
                is_home_ancestor = len(comps) <= len(homecomps) and homecomps[:len(comps)] == comps
                direct_child = len(comps) == len(homecomps) + 1 and comps[:len(homecomps)] == homecomps and comps[-1] in HOME_CHILDREN
                real_comps = components(real_candidate)
                real_homecomps = components(os.path.normpath(os.path.realpath(home)))
                real_home_ancestor = len(real_comps) <= len(real_homecomps) and real_homecomps[:len(real_comps)] == real_comps
                real_direct_child = len(real_comps) == len(real_homecomps) + 1 and real_comps[:len(real_homecomps)] == real_homecomps and real_comps[-1] in HOME_CHILDREN
                protected_for = lambda path_comps: any(c in PROTECTED_COMPONENTS for c in path_comps)
                protected = (target == os.path.sep or is_home_ancestor or direct_child or
                             real_home_ancestor or real_direct_child or protected_for(comps) or protected_for(real_comps) or
                             key_like(os.path.basename(target)))
                if protected:
                    status = "refused: protected"
                elif not os.path.lexists(target):
                    status = "kept: missing"
                elif os.path.ismount(target):
                    status = "refused: mount point"
                else:
                    try:
                        age = float(row["min_age_days"]) if row.get("min_age_days", "") != "" else args.min_age_days
                        if age < 0:
                            raise ValueError()
                    except (ValueError, TypeError):
                        status = "refused: bad min_age_days"
                        age = 0
                    dependency_dirs = DEPENDENCY_DEFAULT | set(args.dependency_dir)
                    skip_inspections = os.path.basename(target) in dependency_dirs
                    facts = walk_target(target, dependency_dirs, age) if status is None else {}
                    if status is None and facts.get("error"):
                        status = "refused: cannot inspect path"
                    if status is None and facts.get("mount"):
                        status = "refused: contains another filesystem at %s" % facts["mount"]
                    if status is None:
                        size, count = facts["bytes"], facts["files"]
                    if status is None and os.path.isdir(target) and not os.path.islink(target) and not skip_inspections:
                        vcs_relevant = bool(facts["repos"] or facts["jj_repos"] or
                                            os.path.isfile(os.path.join(target, ".git")))
                        if "vcs" in allow and vcs_relevant:
                            used_overrides.add("vcs")
                        jj_repo = next((repo for repo in facts["jj_repos"]), None)
                        if jj_repo is not None and "vcs" not in allow:
                            if jj_repo == target:
                                status = "refused: jj repository (verify with jj, then allow=vcs)"
                            else:
                                rel = os.path.relpath(jj_repo, target)
                                status = "refused: jj repository at %s (verify with jj, then allow=vcs)" % rel
                        elif os.path.isfile(os.path.join(target, ".git")) and not os.path.isdir(os.path.join(target, ".git")) and "vcs" not in allow:
                            status = 'refused: linked git worktree (use "git worktree remove")'
                        elif "vcs" not in allow:
                            git_repos = [repo for repo in facts["repos"] if repo not in facts["jj_repos"]]
                            status = git_refusal(git_repos)
                    if status is None and os.path.isdir(target) and not skip_inspections and facts.get("keys") and "key-files" not in allow:
                        status = "kept: contains key-like files (%d), e.g. %s" % (len(facts["keys"]), facts["keys"][0])
                    elif status is None and os.path.isdir(target) and not skip_inspections and facts.get("keys") and "key-files" in allow:
                        used_overrides.add("key-files")
                    if status is None and not skip_inspections and facts.get("fresh"):
                        hours, rel = facts["fresh"]
                        status = ("kept: modified %d h ago: %s "
                                  "(if the rule allows fresh data, set min_age_days=0 in that row)") % (int(hours // 3600), rel)
                    if status is None and not args.no_process_check:
                        now = time.monotonic()
                        if not process_cache[3] or now - process_cache[1] > 5:
                            process_cache[0], process_cache[2] = collect_process_cwds()
                            process_cache[1] = time.monotonic()
                            process_cache[3] = True
                        found = process_check(target, process_cache[0])
                        available = process_cache[2]
                        if found:
                            status = "kept: in use by pid %d (%s)" % found
                        elif not available:
                            evidence = clean_field(evidence + " [process-check unavailable]")
        if status is None:
            status = "would-remove" if not args.apply else "done"
        passed = status in ("would-remove", "done")
        action = "remove" if passed else "skip"
        used = sorted(used_overrides)
        row_evidence = evidence + ((" [allow=" + ",".join(used) + "]") if used else "")
        result = {"path": target or raw_path, "rule": rule, "evidence": row_evidence, "status": status,
                  "bytes": size, "files": count, "action": action}
        if args.apply:
            timestamp = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
            fields = [timestamp, action, result["path"], size, count, rule, row_evidence,
                      "started" if passed else status]
            manifest.seek(0, os.SEEK_END)
            start = manifest.tell()
            manifest.write("\t".join(clean_field(field) for field in fields) + "\n")
            manifest.flush()
            os.fsync(manifest.fileno())
            if passed:
                errors, left = remove_path(target)
                if errors or left:
                    status = "error: %s; %d entries left" % ((errors[0] if errors else "path remains"), left)
                    result["status"] = status
                    manifest.seek(start)
                    manifest.truncate()
                    manifest.write("\t".join(clean_field(field) for field in fields[:-1] + [status]) + "\n")
                    manifest.flush()
                    os.fsync(manifest.fileno())
                else:
                    result["status"] = "done"
                    manifest.seek(start)
                    manifest.truncate()
                    manifest.write("\t".join(clean_field(field) for field in fields[:-1] + ["done"]) + "\n")
                    manifest.flush()
                    os.fsync(manifest.fileno())
                result["removed_bytes"] = size if result["status"] == "done" else 0
            else:
                result["removed_bytes"] = 0
        else:
            result["removed_bytes"] = size if passed else 0
        results.append(result)
    if manifest:
        manifest.close()
    errors = sum(1 for r in results if r["status"].startswith("error:"))
    skipped = sum(1 for r in results if not r["action"] == "remove")
    removed_bytes = sum(r.get("removed_bytes", 0) for r in results)
    if args.json:
        print(json.dumps({"mode": "apply" if args.apply else "dry-run", "rows": results,
                          "removed": sum(1 for r in results if r["status"] == "done" or (not args.apply and r["status"] == "would-remove")),
                          "skipped": skipped, "errors": errors, "bytes_removed": removed_bytes}, ensure_ascii=False))
    else:
        for r in results:
            print("%s\t%s\t%s" % (r["status"], human(r["bytes"]), r["path"]))
        outcomes = {}
        for r in results:
            label = "error" if r["status"].startswith("error:") else (
                "refused" if r["status"].startswith("refused:") else
                "kept" if r["status"].startswith("kept:") else r["status"])
            outcomes[label] = outcomes.get(label, 0) + 1
        counts = ", ".join("%s=%d" % (name, outcomes[name]) for name in sorted(outcomes)) or "none"
        print("summary: %d rows (%s), %d skipped, %s bytes %s" %
              (len(results), counts, skipped, human(removed_bytes), "removed" if args.apply else "would be removed"))
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
