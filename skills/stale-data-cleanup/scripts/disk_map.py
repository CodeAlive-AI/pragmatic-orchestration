#!/usr/bin/env python3
"""Scan allocated disk space once, then query, compare, and reconcile the saved map.

This tool gives cleanup agents one reusable view of a volume instead of repeated
disk walks. It records allocated bytes with hardlinked files counted once. A
folder's size is an upper bound on what removing it frees because copy-on-write
clones can share blocks.

Typical commands:
  disk_map.py scan /System/Volumes/Data -o scan.json
  ssh HOST python3 - scan / < disk_map.py > remote.json
  disk_map.py query scan.json /Users --depth 2
  disk_map.py diff before.json after.json
  disk_map.py reconcile before.json after.json --manifest removed.tsv

Exit codes: 0 means success; 1 means a queried path is absent from the scan or
reconcile found unexplained decreases; 2 means a usage error or unreadable input.

Removal manifests are tab-separated files with a path column. When a status or
result column is present, only rows starting with done count. A bytes or
size_bytes column supplies the removed size when its value is a non-negative
integer; non-absolute paths are ignored.
"""

import argparse
import csv
import datetime as _dt
import errno
import heapq
import json
import os
import socket
import stat
import sys
import tempfile
import time


FORMAT = "disk-map/1"
SAMPLE_LIMIT = 200


def allocated(st):
    return getattr(st, "st_blocks", None) * 512 if getattr(st, "st_blocks", None) is not None else st.st_size


def norm(path):
    return os.path.normpath(os.path.abspath(os.path.expanduser(path)))


def under(path, parent):
    try:
        return os.path.commonpath((path, parent)) == parent
    except ValueError:
        return False


def map_data_path(path, root):
    if root == "/System/Volumes/Data" and os.path.isabs(path) and not under(path, root):
        return os.path.join(root, path.lstrip("/"))
    return path


def scan(root, min_bytes, top_n, excludes, cross_filesystems):
    root = norm(root)
    root_stat = os.stat(root, follow_symlinks=False)
    if not stat.S_ISDIR(root_stat.st_mode):
        raise ValueError("scan root must be a directory")
    root_dev = root_stat.st_dev
    excluded = sorted(norm(p) for p in excludes)
    errors = 0
    samples = []
    hardlink_dups = 0
    dirs = 0
    total_files = 0
    other_fs_paths = []
    seen_inodes = set()
    # Min-heap of (allocated size, insertion id, file record).
    largest = []
    seq = 0

    def err(path, exc):
        nonlocal errors
        errors += 1
        if len(samples) < SAMPLE_LIMIT:
            samples.append({"path": path, "errno": errno.errorcode.get(getattr(exc, "errno", None), "UNKNOWN")})

    def frame(path, name, st):
        nonlocal dirs
        dirs += 1
        return {"path": path, "name": name, "st": st, "it": None, "bytes": 0,
                "files": 0, "newest": 0, "own_files_bytes": 0,
                "small_count": 0, "small_bytes": 0, "children": [], "errors": 0}

    root_frame = frame(root, root, root_stat)
    stack = [root_frame]
    root_node = None
    while stack:
        f = stack[-1]
        if f["it"] is None:
            try:
                f["it"] = os.scandir(f["path"])
            except OSError as exc:
                err(f["path"], exc)
                f["errors"] += 1
                f["it"] = iter(())
        try:
            entry = next(f["it"])
        except StopIteration:
            try:
                f["it"].close()
            except (AttributeError, OSError):
                pass
            stack.pop()
            node = {"name": f["name"], "bytes": f["bytes"], "files": f["files"],
                    "newest": f["newest"], "own_files_bytes": f["own_files_bytes"],
                    "small_dirs": {"count": f["small_count"], "bytes": f["small_bytes"]},
                    "children": sorted(f["children"], key=lambda x: (-x["bytes"], x["name"]))}
            if f["errors"]:
                node["errors"] = f["errors"]
            if stack:
                parent = stack[-1]
                parent["bytes"] += f["bytes"]
                parent["files"] += f["files"]
                parent["newest"] = max(parent["newest"], f["newest"])
                parent["errors"] += f["errors"]
                if f["bytes"] < min_bytes:
                    parent["small_count"] += 1
                    parent["small_bytes"] += f["bytes"]
                else:
                    parent["children"].append(node)
            else:
                root_node = node
            continue
        except OSError as exc:
            err(f["path"], exc)
            f["errors"] += 1
            try:
                f["it"].close()
            except (AttributeError, OSError):
                pass
            f["it"] = iter(())
            continue

        path = os.path.join(f["path"], entry.name)
        try:
            st = entry.stat(follow_symlinks=False)
        except OSError as exc:
            err(path, exc)
            f["errors"] += 1
            continue
        mode = st.st_mode
        if stat.S_ISDIR(mode):
            if path in excluded:
                continue
            if not cross_filesystems and st.st_dev != root_dev:
                other_fs_paths.append(path)
                continue
            stack.append(frame(path, entry.name, st))
        elif stat.S_ISREG(mode):
            size = allocated(st)
            key = (st.st_dev, st.st_ino)
            if st.st_nlink > 1:
                if key in seen_inodes:
                    hardlink_dups += 1
                    continue
                seen_inodes.add(key)
            f["bytes"] += size
            f["own_files_bytes"] += size
            f["files"] += 1
            f["newest"] = max(f["newest"], int(st.st_mtime))
            total_files += 1
            seq += 1
            rec = {"path": path, "bytes": size, "mtime": int(st.st_mtime)}
            if top_n > 0:
                item = (size, seq, rec)
                if len(largest) < top_n:
                    heapq.heappush(largest, item)
                elif item[0] > largest[0][0]:
                    heapq.heapreplace(largest, item)
        else:
            # Symlinks and other special entries occupy space, but are not regular files.
            f["bytes"] += allocated(st)

    excluded_found = []
    for p in excluded:
        # Report requested excludes only when they lie inside the root.
        if under(p, root):
            excluded_found.append(p)
    # Directories on another filesystem are identified without following them.
    other_filesystems = sorted(other_fs_paths)
    top_files = [x[2] for x in sorted(largest, key=lambda x: (-x[0], x[2]["path"]))]
    scanned_at = _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat()
    return {"format": FORMAT, "root": root, "host": socket.gethostname(), "scanned_at": scanned_at,
            "metric": "allocated", "min_node_bytes": min_bytes,
            "total_bytes": root_node["bytes"], "files": total_files, "dirs": dirs,
            "errors": errors, "error_samples": samples, "partial": bool(errors),
            "hardlink_dups": hardlink_dups, "excluded": excluded_found,
            "other_filesystems": other_filesystems, "top_files": top_files, "tree": root_node}


def load_scan(path):
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    if data.get("format") != FORMAT or not isinstance(data.get("tree"), dict):
        raise ValueError("not a disk-map/1 scan")
    return data


def path_index(data):
    root = data["root"]
    out = {}
    stack = [(data["tree"], root)]
    while stack:
        node, path = stack.pop()
        out[path] = node
        for child in node.get("children", []):
            stack.append((child, os.path.join(path, child["name"])))
    return out


def human(n):
    units = ("B", "KiB", "MiB", "GiB", "TiB", "PiB")
    x = float(n)
    i = 0
    while x >= 1024 and i < len(units) - 1:
        x /= 1024
        i += 1
    return "{} {}".format(int(x) if i == 0 else "{:.1f}".format(x), units[i])


def signed_human(n):
    return ("+" if n > 0 else "-" if n < 0 else "") + human(abs(n))


def query_rows(data, requested, depth, min_bytes, top):
    root = data["root"]
    realpath = map_data_path(norm(requested), root)
    if not under(realpath, root):
        return None, "outside scan root: {}".format(requested)
    idx = path_index(data)
    if realpath not in idx:
        ancestor = max((p for p in idx if under(realpath, p)), key=len, default=root)
        folded = idx[ancestor].get("small_dirs", {})
        if folded.get("count", 0):
            mib = data.get("min_node_bytes", 0) / (1024 * 1024)
            mib_text = ("{:g}".format(mib))
            return None, ("{} is not in the scan: it does not exist, is a file, or is smaller than the node floor "
                          "({} MiB) and counted in \"({} smaller folders)\" under {}"
                          .format(requested, mib_text, folded["count"], ancestor))
        return None, "{} is not in the scan: {} has no folded folders, so no such folder existed at scan time".format(requested, ancestor)
    root_node = idx[realpath]
    rows = []
    now = int(time.time())

    def add(kind, node, path, level, size=None, files=None, err_count=0, parent=None):
        bytes_ = node.get("bytes", 0) if size is None else size
        if kind != "directory" and bytes_ == 0:
            return
        newest = node.get("newest", 0)
        rows.append({"type": kind, "bytes": bytes_, "size": human(bytes_),
                     "age_days": None if kind != "directory" or not newest else max(0, (now - newest) // 86400),
                     "files": node.get("files", 0) if files is None else files,
                     "path": path,
                     "depth": level,
                     "errors": err_count or node.get("errors", 0)})
        if parent is not None:
            rows[-1]["parent"] = parent

    stack = [(root_node, realpath, 0)]
    while stack:
        node, path, level = stack.pop()
        add("directory", node, path, level)
        if level >= depth:
            continue
        candidates = [c for c in node.get("children", []) if c.get("bytes", 0) >= min_bytes]
        candidates.sort(key=lambda c: (-c["bytes"], c["name"]))
        for child in reversed(candidates[:max(0, top)]):
            stack.append((child, os.path.join(path, child["name"]), level + 1))
        if node.get("small_dirs", {}).get("count", 0):
            sd = node["small_dirs"]
            if sd.get("bytes", 0) >= min_bytes:
                label = "({} smaller folders)".format(sd["count"])
                add("small_dirs", node, os.path.join(path, label), level + 1,
                    sd["bytes"], 0, parent=path)
        own = node.get("own_files_bytes", 0)
        if own >= min_bytes:
            add("files_here", node, os.path.join(path, "(files directly here)"), level + 1,
                own, 0, parent=path)
    return rows, None


def diff_maps(a, b):
    if a["root"] != b["root"]:
        raise ValueError("scans have different roots: {} and {}".format(a["root"], b["root"]))
    return path_index(a), path_index(b)


def read_manifests(paths):
    out = {}
    for filename in paths:
        with open(filename, "r", encoding="utf-8-sig", errors="surrogateescape", newline="") as fh:
            reader = csv.DictReader(fh, delimiter="\t")
            if not reader.fieldnames:
                raise ValueError("manifest has no header: {}".format(filename))
            names = {n.strip().lower(): n for n in reader.fieldnames if n}
            path_col = names.get("path") or names.get("path_or_object")
            if not path_col:
                raise ValueError("manifest needs a path or path_or_object column: {}".format(filename))
            status_col = names.get("status") or names.get("result")
            size_col = names.get("bytes") or names.get("size_bytes")
            for row in reader:
                value = (row.get(path_col) or "").strip()
                status = (row.get(status_col) or "").strip().lower() if status_col else "done"
                if value and status.startswith("done"):
                    expanded = os.path.expanduser(value)
                    if os.path.isabs(expanded):
                        size = None
                        if size_col:
                            raw_size = (row.get(size_col) or "").strip()
                            try:
                                parsed_size = int(raw_size)
                                if parsed_size >= 0:
                                    size = parsed_size
                            except ValueError:
                                pass
                        key = norm(expanded)
                        # Prefer a recorded size if another manifest explains the same path.
                        if key not in out or (out[key] is None and size is not None):
                            out[key] = size
    return out


def reconcile(a, b, explained, min_bytes, explained_sizes=None):
    ia, ib = diff_maps(a, b)
    root = a["root"]
    explained_sizes = {map_data_path(norm(path), root): size
                       for path, size in (explained_sizes or {}).items()}
    explained = set(map_data_path(norm(x), root) for x in explained)
    # Lookups instead of a scan of every explanation per node: a whole-disk map
    # has tens of thousands of nodes and a cleanup has hundreds of removals.
    above = set()        # every strict ancestor of an explained path
    by_parent = {}       # parent folder -> explained paths directly in it
    for item in explained:
        by_parent.setdefault(node_path(item, root), []).append(item)
        parent = os.path.dirname(item)
        while parent not in above:
            above.add(parent)
            parent = os.path.dirname(parent)
    unexplained = []
    visited = set()
    # The walk goes top-down and never enters an explained folder, so a visited
    # node lies under an explanation only when it is one, or when the scan root does.
    stack = [] if any(under(root, e) for e in explained) else [(root, a["tree"])]
    while stack:
        path, node_a = stack.pop()
        if path in visited:
            continue
        visited.add(path)
        old = node_a.get("bytes", 0)
        new = ib.get(path, {}).get("bytes", 0)
        decrease = old - new
        if path in explained:
            continue
        below = path in above
        if decrease >= min_bytes:
            if not below:
                unexplained.append((decrease, path, "UNEXPLAINED"))
                continue
        # Direct files and compacted folders belong to this node, so sibling
        # growth cannot cancel their loss as it can in a subtree residual.
        node_b = ib.get(path, {})
        direct = (node_a.get("own_files_bytes", 0) - node_b.get("own_files_bytes", 0))
        direct += (node_a.get("small_dirs", {}).get("bytes", 0)
                   - node_b.get("small_dirs", {}).get("bytes", 0))
        credit = 0
        unknown_direct = False
        for explained_path in by_parent.get(path, ()):
            if explained_path in ia:
                continue
            recorded = explained_sizes.get(explained_path)
            if recorded is None:
                recorded = next((entry.get("bytes") for entry in a.get("top_files", [])
                                 if entry.get("path") == explained_path), None)
            if recorded is None:
                unknown_direct = True
            else:
                credit += recorded
        direct = 0 if unknown_direct else max(0, direct - credit)
        if direct >= min_bytes:
            unexplained.append((direct, path, "UNEXPLAINED (files or small folders directly here)"))
        for child in reversed(node_a.get("children", [])):
            stack.append((os.path.join(path, child["name"]), child))
    # Growth is informative, using all paths emitted by either scan.
    growth = []
    for path in set(ia) | set(ib):
        d = ib.get(path, {}).get("bytes", 0) - ia.get(path, {}).get("bytes", 0)
        if d >= min_bytes:
            growth.append((d, path))
    return sorted(unexplained, key=lambda x: (-x[0], x[1])), sorted(growth, key=lambda x: (-x[0], x[1]))


def node_path(path, root):
    """Return the parent of an explained path, preserving filesystem roots."""
    return os.path.dirname(path) or root


def build_parser():
    formatter = argparse.RawDescriptionHelpFormatter
    parser = argparse.ArgumentParser(description=__doc__.strip(), formatter_class=formatter)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("scan", help="scan one directory tree",
                       description="Scan a directory tree and save its allocated-space map.\n\n"
                                   "Example: disk_map.py scan /System/Volumes/Data -o scan.json",
                       formatter_class=formatter)
    p.add_argument("root", help="directory to scan; on macOS use /System/Volumes/Data, not /")
    p.add_argument("-o", "--out", help="write JSON atomically to this file; default: stdout")
    p.add_argument("--exclude", action="append", default=[], help="absolute directory to skip; may be repeated")
    p.add_argument("--min-node-mb", type=float, default=1, help="fold child folders smaller than this many MiB; default: 1")
    p.add_argument("--top-files", type=int, default=100, help="number of largest files to record; default: 100")
    p.add_argument("--cross-filesystems", action="store_true", help="enter directories on other filesystems")
    p = sub.add_parser("query", help="query a saved scan",
                       description="List folder sizes and file summaries from a saved scan.\n\n"
                                   "Example: disk_map.py query scan.json /Users --depth 2",
                       formatter_class=formatter)
    p.add_argument("scan", help="disk-map/1 JSON scan file")
    p.add_argument("path", nargs="?", help="absolute folder path; default: scan root; /Applications/X maps under /System/Volumes/Data")
    p.add_argument("--depth", type=int, default=1, help="directory levels below PATH; default: 1")
    p.add_argument("--min-mb", type=float, default=0, help="hide rows smaller than this many MiB; default: 0")
    p.add_argument("--top", type=int, default=30, help="largest child folders per directory; default: 30")
    p.add_argument("--json", action="store_true", help="print rows as a JSON array without a text header")
    p = sub.add_parser("diff", help="compare saved scans",
                       description="Compare folder sizes between two scans of the same root.\n\n"
                                   "Example: disk_map.py diff before.json after.json",
                       formatter_class=formatter)
    p.add_argument("a", help="first disk-map/1 JSON scan")
    p.add_argument("b", help="second disk-map/1 JSON scan")
    p.add_argument("--min-mb", type=float, default=100, help="minimum absolute change in MiB to print; default: 100")
    p.add_argument("--limit", type=int, default=500, help="maximum change rows to print; default: 500")
    p = sub.add_parser("reconcile", help="check significant decreases against removal records",
                       description="Check significant decreases against completed removal records.\n\n"
                                   "Example: disk_map.py reconcile before.json after.json\n"
                                   "  --manifest removed.tsv",
                       formatter_class=formatter)
    p.add_argument("a", help="before disk-map/1 JSON scan")
    p.add_argument("b", help="after disk-map/1 JSON scan")
    p.add_argument("--manifest", action="append", required=True,
                   help="TSV with path/path_or_object; status/result rows need done; bytes optional; repeatable")
    p.add_argument("--explain", action="append", default=[],
                   help="absolute removed path to explain; size unknown; repeatable")
    p.add_argument("--min-mb", type=float, default=100, help="minimum decrease in MiB to reconcile; default: 100")
    return parser


def atomic_json(path, data):
    directory = os.path.dirname(os.path.abspath(path))
    fd, temp = tempfile.mkstemp(prefix=".disk-map-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=True, separators=(",", ":"))
            fh.write("\n")
        os.replace(temp, path)
    except BaseException:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise


def main(argv=None):
    # A file name is bytes. One that is not valid UTF-8 must print as the bytes
    # it has, not stop the listing.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="surrogateescape")
    args = build_parser().parse_args(argv)
    try:
        if args.command == "scan":
            if args.min_node_mb < 0 or args.top_files < 0:
                raise ValueError("size and top-file limits must be non-negative")
            if sys.platform == "darwin" and norm(args.root) == "/":
                print("error: on macOS scan the data volume, not /: disk_map.py scan /System/Volumes/Data", file=sys.stderr)
                return 2
            result = scan(args.root, int(args.min_node_mb * 1024 * 1024), args.top_files, args.exclude, args.cross_filesystems)
            if args.out:
                atomic_json(args.out, result)
            else:
                json.dump(result, sys.stdout, ensure_ascii=True, separators=(",", ":"))
                sys.stdout.write("\n")
            summary = "scanned {}: {}, {} files, {} folders, {} unreadable".format(
                result["root"], human(result["total_bytes"]), result["files"], result["dirs"], result["errors"])
            other_count = len(result["other_filesystems"])
            if other_count:
                summary += ", {} folders on other filesystems not entered".format(other_count)
            print(summary, file=sys.stderr)
            if result["errors"]:
                print("the scan is partial: sizes are a lower bound; error_samples lists the first paths", file=sys.stderr)
            if other_count:
                print("pass --cross-filesystems to enter them, or scan each one separately", file=sys.stderr)
            return 0
        if args.command == "query":
            if args.depth < 0 or args.top < 0 or args.min_mb < 0:
                raise ValueError("depth, top and minimum size must be non-negative")
            data = load_scan(args.scan)
            rows, problem = query_rows(data, args.path or data["root"], args.depth, int(args.min_mb * 1024 * 1024), args.top)
            if problem:
                print(problem, file=sys.stderr)
                return 1
            if args.json:
                json.dump(rows, sys.stdout, ensure_ascii=True)
                sys.stdout.write("\n")
            else:
                print("{:>10}  {:>6}  {:>8}  {}".format("size", "newest", "files", "path"))
                for r in rows:
                    age = "-" if r["age_days"] is None or r["type"] != "directory" else "{}d".format(r["age_days"])
                    marker = " [errors]" if r["errors"] else ""
                    files = "-" if r["type"] != "directory" else str(r["files"])
                    print("{:>10}  {:>6}  {:>8}  {}{}".format(r["size"], age, files, r["path"], marker))
            return 0
        if args.command == "diff":
            if args.min_mb < 0 or args.limit < 0:
                raise ValueError("minimum size and limit must be non-negative")
            a, b = load_scan(args.a), load_scan(args.b)
            ia, ib = diff_maps(a, b)
            threshold = int(args.min_mb * 1024 * 1024)
            total_delta = b["total_bytes"] - a["total_bytes"]
            print("Totals: {} -> {} ({}) [{} bytes]. A folder marked GONE may only have shrunk below the node floor.".format(
                human(a["total_bytes"]), human(b["total_bytes"]), signed_human(total_delta), total_delta))
            print("{:>12}  {:4}  {}".format("change", "flag", "path"))
            changes = []
            for path in set(ia) | set(ib):
                av = ia.get(path, {}).get("bytes", 0)
                bv = ib.get(path, {}).get("bytes", 0)
                delta = bv - av
                if abs(delta) >= threshold:
                    flag = "GONE" if path in ia and path not in ib else "NEW" if path in ib and path not in ia else ""
                    changes.append((delta, flag, path))
            changes.sort(key=lambda x: (0 if x[0] < 0 else 1, abs(x[0]) * -1, x[2]))
            for delta, flag, path in changes[:args.limit]:
                print("{:>12}  {:4}  {}".format(signed_human(delta), flag, path))
            return 0
        if args.command == "reconcile":
            if args.min_mb < 0:
                raise ValueError("minimum size must be non-negative")
            a, b = load_scan(args.a), load_scan(args.b)
            for value in args.explain:
                if not os.path.isabs(os.path.expanduser(value)):
                    raise ValueError("--explain needs an absolute path: {}".format(value))
            manifest_sizes = read_manifests(args.manifest)
            explanations = list(manifest_sizes) + [norm(os.path.expanduser(x)) for x in args.explain]
            threshold = int(args.min_mb * 1024 * 1024)
            unexplained, growth = reconcile(a, b, explanations, threshold, manifest_sizes)
            for size, path, label in unexplained:
                print("{} {:>12}  {}".format(label, human(size), path))
            if growth:
                print("Growth:")
                for size, path in growth:
                    print("+{:>11}  {}".format(human(size), path))
            if unexplained:
                print("VERDICT: {} unexplained decrease(s)".format(len(unexplained)))
                return 1
            print("VERDICT: all significant decreases are explained")
            return 0
    except (OSError, ValueError, json.JSONDecodeError, csv.Error) as exc:
        print("error: {}".format(exc), file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    sys.exit(main())
