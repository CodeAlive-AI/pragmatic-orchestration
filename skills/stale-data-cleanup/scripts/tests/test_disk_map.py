import json
import contextlib
import importlib.util
import io
import os
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock


SCRIPT = os.path.join(os.path.dirname(__file__), "..", "disk_map.py")


def size(path):
    st = os.lstat(path)
    return getattr(st, "st_blocks", None) * 512 if getattr(st, "st_blocks", None) is not None else st.st_size


def node(name, bytes_, files=0, own=0, children=None, small=None, newest=0):
    return {"name": name, "bytes": bytes_, "files": files, "newest": newest,
            "own_files_bytes": own, "small_dirs": small or {"count": 0, "bytes": 0},
            "children": children or []}


def document(root, tree):
    return {"format": "disk-map/1", "root": root, "host": "test", "scanned_at": "2026-01-01T00:00:00+00:00",
            "metric": "allocated", "min_node_bytes": 0, "total_bytes": tree["bytes"],
            "files": tree["files"], "dirs": 1, "errors": 0, "error_samples": [],
            "partial": False, "hardlink_dups": 0, "excluded": [], "other_filesystems": [],
            "top_files": [], "tree": tree}


class DiskMapTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = self.tmp.name
        self.root = os.path.join(self.base, "root")
        os.mkdir(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, *args, stdin=None, cwd=None):
        return subprocess.run([sys.executable, SCRIPT, *map(str, args)], input=stdin,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=cwd)

    def scan(self, *args):
        p = self.run_cli("scan", self.root, "--min-node-mb", "0", *args)
        self.assertEqual(p.returncode, 0, p.stderr)
        return json.loads(p.stdout)

    def save(self, path, data):
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        return path

    def test_totals_and_directory_metrics(self):
        a = os.path.join(self.root, "a")
        b = os.path.join(self.root, "a", "b")
        os.makedirs(b)
        f1 = os.path.join(self.root, "root-file")
        f2 = os.path.join(a, "one")
        f3 = os.path.join(b, "two")
        for path, content in ((f1, b"root"), (f2, b"a" * 8192), (f3, b"b" * 4096)):
            with open(path, "wb") as fh:
                fh.write(content)
        os.utime(f1, (100, 100))
        os.utime(f2, (200, 200))
        os.utime(f3, (300, 300))
        result = self.scan()
        self.assertEqual(result["total_bytes"], sum(size(p) for p in (f1, f2, f3)))
        self.assertEqual(result["files"], 3)
        self.assertEqual(result["tree"]["own_files_bytes"], size(f1))
        self.assertEqual(result["tree"]["newest"], 300)
        na = result["tree"]["children"][0]
        self.assertEqual(na["name"], "a")
        self.assertEqual(na["bytes"], size(f2) + size(f3))
        self.assertEqual(na["files"], 2)
        self.assertEqual(na["newest"], 300)
        self.assertEqual(na["own_files_bytes"], size(f2))
        self.assertEqual(na["children"][0]["own_files_bytes"], size(f3))

    def test_hardlink_counted_once(self):
        f = os.path.join(self.root, "first")
        with open(f, "wb") as fh:
            fh.write(b"data" * 512)
        os.link(f, os.path.join(self.root, "second"))
        result = self.scan()
        self.assertEqual(result["files"], 1)
        self.assertEqual(result["total_bytes"], size(f))
        self.assertEqual(result["hardlink_dups"], 1)

    def test_symlink_does_not_enter_outside_directory(self):
        outside = os.path.join(self.base, "outside")
        os.mkdir(outside)
        with open(os.path.join(outside, "large"), "wb") as fh:
            fh.write(b"x" * 100000)
        os.symlink(outside, os.path.join(self.root, "link"))
        result = self.scan()
        self.assertEqual(result["total_bytes"], size(os.path.join(self.root, "link")))
        self.assertEqual(result["files"], 0)
        self.assertEqual(result["tree"]["children"], [])

    def test_exclude_skips_and_lists_directory(self):
        excluded = os.path.join(self.root, "skip")
        os.mkdir(excluded)
        with open(os.path.join(excluded, "f"), "wb") as fh:
            fh.write(b"x" * 4096)
        result = self.scan("--exclude", excluded)
        self.assertIn(excluded, result["excluded"])
        self.assertEqual(result["files"], 0)
        self.assertEqual(result["tree"]["children"], [])

    @unittest.skipIf(getattr(os, "geteuid", lambda: 1)() == 0, "root can read chmod 000 directories")
    def test_unreadable_directory_is_partial(self):
        bad = os.path.join(self.root, "bad")
        os.mkdir(bad)
        os.chmod(bad, 0)
        try:
            p = self.run_cli("scan", self.root, "--min-node-mb", "0")
        finally:
            os.chmod(bad, 0o700)
        data = json.loads(p.stdout)
        self.assertEqual(p.returncode, 0)
        self.assertTrue(data["partial"])
        self.assertGreaterEqual(data["errors"], 1)
        self.assertEqual(data["tree"]["errors"], data["errors"])

    def test_compaction_preserves_bytes(self):
        small = os.path.join(self.root, "small")
        os.mkdir(small)
        f = os.path.join(small, "f")
        with open(f, "wb") as fh:
            fh.write(b"x")
        result = self.scan("--min-node-mb", "1")
        self.assertEqual(result["tree"]["bytes"], size(f))
        self.assertEqual(result["tree"]["small_dirs"], {"count": 1, "bytes": size(f)})
        self.assertEqual(result["tree"]["children"], [])

    @unittest.skipUnless(hasattr(os, "fsencode"), "requires byte paths for invalid UTF-8")
    def test_invalid_utf8_name_survives_scan_and_query(self):
        # A folder whose name is not valid UTF-8 (possible on Linux filesystems).
        raw_dir = os.fsencode(self.root) + b"/bad-\xff"
        try:
            os.mkdir(raw_dir)
            fd = os.open(raw_dir + b"/file", os.O_CREAT | os.O_WRONLY, 0o600)
            os.write(fd, b"abc")
            os.close(fd)
        except OSError as exc:
            self.skipTest("filesystem refuses invalid UTF-8 name: {}".format(exc))
        data = self.scan()
        self.assertIn("\\udcff", json.dumps(data, ensure_ascii=True))
        scanfile = self.save(os.path.join(self.base, "scan.json"), data)
        q = self.run_cli("query", scanfile, self.root, "--json")
        self.assertEqual(q.returncode, 0, q.stderr)
        self.assertIn("\\udcff", q.stdout)
        # The text listing prints the name as the bytes it has instead of failing.
        text = subprocess.run([sys.executable, SCRIPT, "query", scanfile, self.root],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(text.returncode, 0, text.stderr)
        self.assertIn(b"bad-\xff", text.stdout)
        # A manifest that names the folder explains its removal.
        after = json.loads(json.dumps(data))
        after["tree"]["children"] = []
        after["tree"]["bytes"] = after["tree"]["own_files_bytes"]
        after["total_bytes"] = after["tree"]["bytes"]
        afterfile = self.save(os.path.join(self.base, "after.json"), after)
        manifest = os.path.join(self.base, "raw.tsv")
        with open(manifest, "wb") as fh:
            fh.write(b"path\tstatus\n" + raw_dir + b"\tdone\n")
        r = subprocess.run([sys.executable, SCRIPT, "reconcile", scanfile, afterfile, "--manifest", manifest,
                            "--min-mb", "0.000001"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        empty = os.path.join(self.base, "none.tsv")
        with open(empty, "w", encoding="utf-8") as fh:
            fh.write("path\tstatus\n")
        r = subprocess.run([sys.executable, SCRIPT, "reconcile", scanfile, afterfile, "--manifest", empty,
                            "--min-mb", "0.000001"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn(b"UNEXPLAINED", r.stdout)

    def test_query_depth_minimum_folded_and_files_here(self):
        big = os.path.join(self.root, "big")
        small = os.path.join(self.root, "small")
        os.mkdir(big)
        os.mkdir(small)
        own = os.path.join(self.root, "own")
        with open(own, "wb") as fh:
            fh.write(b"o" * 4096)
        f = os.path.join(big, "large")
        with open(f, "wb") as fh:
            fh.write(b"b" * (2 * 1024 * 1024))
        sf = os.path.join(small, "tiny")
        with open(sf, "wb") as fh:
            fh.write(b"s")
        data = self.scan("--min-node-mb", "1")
        scanfile = self.save(os.path.join(self.base, "scan.json"), data)
        q = self.run_cli("query", scanfile, self.root, "--depth", "1", "--min-mb", "0", "--json")
        rows = json.loads(q.stdout)
        self.assertIn("files_here", [r["type"] for r in rows])
        self.assertIn("small_dirs", [r["type"] for r in rows])
        self.assertEqual(sum(r["type"] == "directory" for r in rows), 2)
        qmin = self.run_cli("query", scanfile, self.root, "--min-mb", "5", "--json")
        self.assertEqual([r["path"] for r in json.loads(qmin.stdout)], [self.root])
        missing = self.run_cli("query", scanfile, os.path.join(self.root, "small", "x"))
        self.assertEqual(missing.returncode, 1)
        self.assertIn(self.root, missing.stderr)

    def test_query_mac_data_volume_path_mapping(self):
        root = "/System/Volumes/Data"
        users = node("Users", 10, children=[node("x", 10)])
        apps = node("Applications", 10, children=[node("x", 10)])
        doc = document(root, node(root, 20, children=[users, apps]))
        scanfile = self.save(os.path.join(self.base, "manual.json"), doc)
        q = self.run_cli("query", scanfile, "/Users/x", "--json")
        self.assertEqual(q.returncode, 0, q.stderr)
        self.assertEqual(json.loads(q.stdout)[0]["path"], "/System/Volumes/Data/Users/x")
        q_apps = self.run_cli("query", scanfile, "/Applications/x", "--json")
        self.assertEqual(q_apps.returncode, 0, q_apps.stderr)
        self.assertEqual(json.loads(q_apps.stdout)[0]["path"], "/System/Volumes/Data/Applications/x")

    def test_reconcile_direct_loss_not_hidden_by_child_growth(self):
        root = self.root
        before = node(root, 600, own=500, children=[node("grow", 100)])
        after = node(root, 600, own=100, children=[node("grow", 500)])
        fa = self.save(os.path.join(self.base, "before.json"), document(root, before))
        fb = self.save(os.path.join(self.base, "after.json"), document(root, after))
        manifest = os.path.join(self.base, "empty.tsv")
        with open(manifest, "w", encoding="utf-8") as fh:
            fh.write("path\n")
        p = self.run_cli("reconcile", fa, fb, "--manifest", manifest, "--min-mb", "0.0001")
        self.assertEqual(p.returncode, 1)
        self.assertIn("UNEXPLAINED (files or small folders directly here)", p.stdout)
        self.assertIn(root, p.stdout)
        explained = self.run_cli("reconcile", fa, fb, "--manifest", manifest, "--explain", root,
                                 "--min-mb", "0.0001")
        self.assertEqual(explained.returncode, 0, explained.stdout)

    def test_query_text_and_json_row_context(self):
        root = self.root
        tree = node(root, 30, own=10, newest=100, small={"count": 2, "bytes": 5},
                    children=[node("child", 15, files=1, newest=200)])
        scanfile = self.save(os.path.join(self.base, "manual-rows.json"), document(root, tree))
        q = self.run_cli("query", scanfile, root, "--depth", "1")
        self.assertEqual(q.returncode, 0, q.stderr)
        self.assertEqual(q.stdout.splitlines()[0].split(), ["size", "newest", "files", "path"])
        self.assertIn(root + "/(files directly here)", q.stdout)
        self.assertIn(root + "/(2 smaller folders)", q.stdout)
        rows = json.loads(self.run_cli("query", scanfile, root, "--depth", "1", "--json").stdout)
        self.assertEqual(rows[0]["depth"], 0)
        pseudo = [r for r in rows if r["type"] != "directory"]
        self.assertTrue(all(r["parent"] == root and r["depth"] == 1 for r in pseudo))
        self.assertTrue(all(r["path"].startswith(root + "/") for r in pseudo))

    def test_diff_gone_new_shrink_and_root_refusal(self):
        root = self.root
        a = document(root, node(root, 1000, children=[node("gone", 400), node("stay", 600)]))
        b = document(root, node(root, 800, children=[node("new", 300), node("stay", 500)]))
        fa = self.save(os.path.join(self.base, "a.json"), a)
        fb = self.save(os.path.join(self.base, "b.json"), b)
        p = self.run_cli("diff", fa, fb, "--min-mb", "0", "--limit", "20")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("GONE", p.stdout)
        self.assertIn("NEW", p.stdout)
        self.assertIn("stay", p.stdout)
        self.assertIn("change  flag  path", p.stdout)
        self.assertLess(p.stdout.index("Totals:"), p.stdout.index("change  flag  path"))
        self.assertIn("-200 B", p.stdout)
        b["root"] = "/elsewhere"
        fc = self.save(os.path.join(self.base, "c.json"), b)
        refused = self.run_cli("diff", fa, fc)
        self.assertEqual(refused.returncode, 2)

    def test_reconcile_explained_unexplained_residual_manifest_status_and_explain(self):
        root = self.root
        child_a = node("child", 60)
        parent_a = node(root, 100, children=[child_a], own=40)
        child_b = node("child", 0)
        parent_b = node(root, 20, children=[child_b], own=20)
        fa = self.save(os.path.join(self.base, "before.json"), document(root, parent_a))
        fb = self.save(os.path.join(self.base, "after.json"), document(root, parent_b))
        manifest = os.path.join(self.base, "manifest.tsv")
        with open(manifest, "w", encoding="utf-8") as fh:
            fh.write("path\tstatus\n")
            fh.write(root + "/child\tdone removed\n")
            fh.write(root + "\tplanned\n")
        p = self.run_cli("reconcile", fa, fb, "--manifest", manifest, "--min-mb", "0.00001")
        self.assertEqual(p.returncode, 1)
        self.assertIn("UNEXPLAINED", p.stdout)
        self.assertIn(root, p.stdout)
        explained = self.run_cli("reconcile", fa, fb, "--manifest", manifest, "--explain", root, "--min-mb", "0.00001")
        self.assertEqual(explained.returncode, 0, explained.stdout)

    def test_reconcile_fully_explained_and_removed_directory(self):
        root = self.root
        a = document(root, node(root, 100, children=[node("gone", 100)]))
        b = document(root, node(root, 100, children=[node("new", 100)]))
        fa = self.save(os.path.join(self.base, "before.json"), a)
        fb = self.save(os.path.join(self.base, "after.json"), b)
        manifest = os.path.join(self.base, "done.tsv")
        with open(manifest, "w", encoding="utf-8") as fh:
            fh.write("path_or_object\tresult\n" + root + "/gone\tdone\n")
        p = self.run_cli("reconcile", fa, fb, "--manifest", manifest, "--min-mb", "0.00001")
        self.assertEqual(p.returncode, 0, p.stdout)
        empty_manifest = os.path.join(self.base, "empty.tsv")
        with open(empty_manifest, "w", encoding="utf-8") as fh:
            fh.write("path\n")
        unexplained = self.run_cli("reconcile", fa, fb, "--manifest", empty_manifest, "--min-mb", "0.00001")
        self.assertEqual(unexplained.returncode, 1)
        self.assertIn(root + "/gone", unexplained.stdout)

    def test_reconcile_manifest_file_size_credits_direct_loss(self):
        mib = 1024 * 1024
        root = self.root
        removed = os.path.join(root, "big-installer.dmg")

        def run(before_bytes, after_bytes, manifest_size, top_files=None, explain=False):
            before = document(root, node(root, before_bytes, own=before_bytes))
            after = document(root, node(root, after_bytes, own=after_bytes))
            before["top_files"] = top_files or []
            fa = self.save(os.path.join(self.base, "credit-before.json"), before)
            fb = self.save(os.path.join(self.base, "credit-after.json"), after)
            manifest = os.path.join(self.base, "credit.tsv")
            with open(manifest, "w", encoding="utf-8") as fh:
                fh.write("time\taction\tpath\tbytes\tstatus\n")
                status = "skip" if explain else "done"
                fh.write("now\tremove\t{}\t{}\t{}\n".format(removed, manifest_size, status))
            args = ["reconcile", fa, fb, "--manifest", manifest, "--min-mb", "0.0001"]
            if explain:
                args = ["reconcile", fa, fb, "--manifest", manifest,
                        "--explain", removed, "--min-mb", "0.0001"]
            return self.run_cli(*args)

        fully_explained = run(4 * mib, 0, 4 * mib)
        self.assertEqual(fully_explained.returncode, 0, fully_explained.stdout)

        partial = run(10 * mib, 0, 4 * mib)
        self.assertEqual(partial.returncode, 1)
        self.assertIn("6.0 MiB", partial.stdout)

        top_file = run(10 * mib, 0, "", top_files=[{"path": removed, "bytes": 4 * mib}])
        self.assertEqual(top_file.returncode, 1)
        self.assertIn("6.0 MiB", top_file.stdout)

        no_size = run(4 * mib, 0, "", explain=True)
        self.assertEqual(no_size.returncode, 0, no_size.stdout)

    def test_reconcile_explanation_above_and_deep_below(self):
        root = self.root
        deep_a = node("a", 300, children=[node("b", 300, children=[node("c", 200), node("d", 100)])])
        deep_b = node("a", 0, children=[node("b", 0)])
        fa = self.save(os.path.join(self.base, "before.json"), document(root, node(root, 300, children=[deep_a])))
        fb = self.save(os.path.join(self.base, "after.json"), document(root, node(root, 0, children=[deep_b])))
        manifest = os.path.join(self.base, "deep.tsv")
        with open(manifest, "w", encoding="utf-8") as fh:
            fh.write("path\tstatus\n" + root + "/a/b/c\tdone\n")
        # Only c is explained: its sibling d is reported, the ancestors are not.
        p = self.run_cli("reconcile", fa, fb, "--manifest", manifest, "--min-mb", "0.00001")
        self.assertEqual(p.returncode, 1, p.stdout)
        self.assertIn(root + "/a/b/d", p.stdout)
        self.assertEqual(p.stdout.count("UNEXPLAINED"), 1, p.stdout)
        # An explanation that names a parent of the scan root explains everything.
        above = self.run_cli("reconcile", fa, fb, "--manifest", manifest, "--explain", os.path.dirname(root),
                             "--min-mb", "0.00001")
        self.assertEqual(above.returncode, 0, above.stdout)

    def test_runs_program_from_stdin(self):
        with open(SCRIPT, "rb") as fh:
            source = fh.read()
        p = subprocess.run([sys.executable, "-", "scan", self.root], input=source,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(p.returncode, 0, p.stderr.decode())
        self.assertEqual(json.loads(p.stdout)["root"], self.root)

    def test_scan_root_refused_on_macos_in_process(self):
        spec = importlib.util.spec_from_file_location("disk_map_under_test", SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(module.sys, "platform", "darwin"), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = module.main(["scan", "/"])
        self.assertEqual(code, 2)
        self.assertEqual(out.getvalue(), "")
        self.assertIn("error: on macOS scan the data volume, not /: disk_map.py scan /System/Volumes/Data", err.getvalue())

    def test_query_missing_path_explains_possibilities(self):
        folded = node(self.root, 10, small={"count": 3, "bytes": 9})
        data = document(self.root, folded)
        data["min_node_bytes"] = 1048576
        scanfile = self.save(os.path.join(self.base, "folded.json"), data)
        p = self.run_cli("query", scanfile, os.path.join(self.root, "missing"))
        self.assertEqual(p.returncode, 1)
        self.assertIn("does not exist, is a file, or is smaller than the node floor (1 MiB)", p.stderr)
        self.assertIn('"(3 smaller folders)"', p.stderr)
        self.assertIn(self.root, p.stderr)
        data["tree"]["small_dirs"] = {"count": 0, "bytes": 0}
        scanfile = self.save(os.path.join(self.base, "unfolded.json"), data)
        p = self.run_cli("query", scanfile, os.path.join(self.root, "missing"))
        self.assertIn("has no folded folders, so no such folder existed at scan time", p.stderr)

    def test_scan_summary_reports_counts_on_stderr(self):
        p = self.run_cli("scan", self.root, "--min-node-mb", "0")
        self.assertEqual(p.returncode, 0)
        self.assertTrue(json.loads(p.stdout)["root"] == self.root)
        self.assertIn("scanned {}: 0 B, 0 files, 1 folders, 0 unreadable".format(self.root), p.stderr)

    def test_help_has_workflow_and_option_descriptions(self):
        outputs = {}
        for command in (None, "scan", "query", "diff", "reconcile", "trend", "rotate"):
            args = ["--help"] if command is None else [command, "--help"]
            p = self.run_cli(*args)
            self.assertEqual(p.returncode, 0, p.stderr)
            outputs[command] = p.stdout
        for phrase in ("/System/Volumes/Data", "ssh HOST python3 - scan /", "allocated"):
            self.assertIn(phrase, outputs[None])
        self.assertIn("--manifest", outputs["reconcile"])
        combined = "\n".join(outputs.values())
        for phrase in ("MiB", "absolute removed path", "default:"):
            self.assertIn(phrase, combined)

    def test_relative_manifest_paths_do_not_explain_and_relative_explain_fails(self):
        root = self.base
        os.mkdir(os.path.join(root, "gone"))
        before = node(root, 100, children=[node("gone", 100), node("grow", 0)])
        after = node(root, 100, children=[node("grow", 100)])
        fa = self.save(os.path.join(self.base, "rel-before.json"), document(root, before))
        fb = self.save(os.path.join(self.base, "rel-after.json"), document(root, after))
        manifest = os.path.join(self.base, "relative.tsv")
        with open(manifest, "w", encoding="utf-8") as fh:
            fh.write("path\tstatus\ngone\tdone\n")
        p = self.run_cli("reconcile", fa, fb, "--manifest", manifest, "--min-mb", "0.00001", cwd=root)
        self.assertEqual(p.returncode, 1)
        self.assertIn(root + "/gone", p.stdout)
        p = self.run_cli("reconcile", fa, fb, "--manifest", manifest, "--explain", "gone", cwd=root)
        self.assertEqual(p.returncode, 2)
        self.assertIn("error: --explain needs an absolute path: gone", p.stderr)

    def stock_file(self, body):
        path = os.path.join(self.base, "stocks.tsv")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(body)
        return path

    def set_scan_time_volume(self, data, at, free, size=1000000):
        data["scanned_at"] = at
        data["volume"] = {"size_bytes": size, "free_bytes": free}
        return data

    def test_scan_has_volume_and_stock_partition_patterns(self):
        dirs = ["outer", "outer/nested", "outer/a/node_modules", "outer/a/b/node_modules",
                "outside/node_modules", "carved", "carved/node_modules", "repeats",
                "repeats/node_modules", "repeats/node_modules/deeper/node_modules"]
        for rel in dirs:
            os.makedirs(os.path.join(self.root, rel))
            with open(os.path.join(self.root, rel, "f"), "wb") as fh:
                fh.write(rel.encode() * 16)
        data = self.scan("--min-node-mb", "0")
        self.assertGreaterEqual(data["volume"]["size_bytes"], data["volume"]["free_bytes"])
        self.assertGreaterEqual(data["volume"]["free_bytes"], 0)
        stocks = self.stock_file("stock\tpath\tempties\n"
                                 "work\t{}/outer\trules\n"
                                 "nested\t{}/outer/nested\towner\n"
                                 "above\t{}/**/node_modules\tnobody\n"
                                 "carved\t{}/carved\trules\n"
                                 "carved-modules\t{}/carved/**/node_modules\tnobody\n"
                                 "repeated\t{}/repeats/**/node_modules\towner\n".format(
                                     self.root, self.root, self.root, self.root, self.root, self.root))
        scanfile = self.save(os.path.join(self.base, "tree.json"), data)
        p = self.run_cli("trend", scanfile, "--stocks", stocks, "--json")
        self.assertEqual(p.returncode, 0, p.stderr)
        result = json.loads(p.stdout)
        self.assertEqual(sum(row["bytes"] for row in result["stocks"]) + result["outside"]["bytes"], data["total_bytes"])
        rows = {row["stock"]: row["bytes"] for row in result["stocks"]}
        def queried(path):
            query = self.run_cli("query", scanfile, path, "--json")
            self.assertEqual(query.returncode, 0, query.stderr)
            return json.loads(query.stdout)[0]["bytes"]
        self.assertEqual(rows["work"], queried(self.root + "/outer") - queried(self.root + "/outer/nested"))
        self.assertEqual(rows["nested"], queried(self.root + "/outer/nested"))
        self.assertEqual(rows["above"], queried(self.root + "/outside/node_modules"))
        self.assertEqual(rows["carved"], queried(self.root + "/carved") - queried(self.root + "/carved/node_modules"))
        self.assertEqual(rows["carved-modules"], queried(self.root + "/carved/node_modules"))
        self.assertEqual(rows["repeated"], queried(self.root + "/repeats/node_modules"))

    def test_trend_rates_forecasts_pressure_and_target_units(self):
        root = self.root
        first = document(root, node(root, 1000, own=1000))
        second = document(root, node(root, 1300, own=1300))
        self.set_scan_time_volume(first, "2026-01-01T00:00:00+00:00", 500, 1000)
        self.set_scan_time_volume(second, "2026-01-03T00:00:00+00:00", 100, 1000)
        a = self.save(os.path.join(self.base, "a.json"), first)
        b = self.save(os.path.join(self.base, "b.json"), second)
        stocks = self.stock_file("stock\tpath\tempties\nroot\t{}\trules\n".format(root))
        p = self.run_cli("trend", a, b, "--stocks", stocks, "--target-free", "20%", "--horizon-days", "0.5", "--json")
        self.assertEqual(p.returncode, 0, p.stderr)
        result = json.loads(p.stdout)
        self.assertEqual(result["stocks"][0]["per_day_last"], 150)
        self.assertEqual(result["volume"]["fill_per_day_last"], 200)
        self.assertEqual(result["volume"]["days_to_full"], .5)
        self.assertEqual(result["volume"]["target_free_bytes"], 200)
        self.assertIsNone(result["volume"]["days_to_target"])
        self.assertEqual(result["pressure"], "now")
        second["volume"]["free_bytes"] = 400
        self.save(b, second)
        p = self.run_cli("trend", a, b, "--stocks", stocks, "--target-free", "20%", "--horizon-days", "1", "--json")
        result = json.loads(p.stdout)
        self.assertEqual(result["volume"]["days_to_target"], 4)
        self.assertEqual(result["pressure"], "quiet")
        second["volume"]["free_bytes"] = 150
        self.save(b, second)
        p = self.run_cli("trend", a, b, "--stocks", stocks, "--target-free", "20%", "--horizon-days", "0", "--json")
        self.assertEqual(json.loads(p.stdout)["pressure"], "decide")
        second.pop("volume")
        self.save(b, second)
        p = self.run_cli("trend", a, b, "--stocks", stocks, "--target-free", "20%", "--json")
        self.assertEqual(json.loads(p.stdout)["pressure"], "unknown")
        second["volume"] = {"size_bytes": 1000, "free_bytes": 500}
        self.save(b, second)
        first.pop("volume")
        self.save(a, first)
        p = self.run_cli("trend", a, b, "--stocks", stocks, "--json")
        result = json.loads(p.stdout)
        self.assertEqual(result["volume"]["free_bytes"], 500)
        self.assertIsNone(result["volume"]["fill_per_day_last"])
        for target, expected in (("10G", 10 * 1024**3), ("500M", 500 * 1024**2), ("20%", 200)):
            p = self.run_cli("trend", a, b, "--stocks", stocks, "--target-free", target, "--json")
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertEqual(json.loads(p.stdout)["volume"]["target_free_bytes"], expected)
        self.assertEqual(self.run_cli("trend", a, b, "--stocks", stocks, "--target-free", "banana").returncode, 2)

    def test_trend_short_interval_is_called_rough(self):
        root = self.root
        first = document(root, node(root, 1000, own=1000))
        second = document(root, node(root, 1100, own=1100))
        self.set_scan_time_volume(first, "2026-01-01T00:00:00+00:00", 500, 1000)
        self.set_scan_time_volume(second, "2026-01-01T00:09:00+00:00", 490, 1000)
        a = self.save(os.path.join(self.base, "short-a.json"), first)
        b = self.save(os.path.join(self.base, "short-b.json"), second)
        stocks = self.stock_file("stock\tpath\tempties\nroot\t{}\trules\n".format(root))
        p = self.run_cli("trend", a, b, "--stocks", stocks)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("last interval 9 minutes", p.stdout)
        self.assertIn("are rough", p.stderr)
        p = self.run_cli("trend", a, b, "--stocks", stocks, "--json")
        self.assertTrue(any("are rough" in w for w in json.loads(p.stdout)["warnings"]))
        # Two days apart: a measured rate, no warning, the span in days.
        second["scanned_at"] = "2026-01-03T00:00:00+00:00"
        self.save(b, second)
        p = self.run_cli("trend", a, b, "--stocks", stocks)
        self.assertIn("last interval 2.0 days", p.stdout)
        self.assertNotIn("are rough", p.stderr)

    def test_trend_directory_skips_bad_files_missing_paths_and_one_scan(self):
        data = document(self.root, node(self.root, 20, own=20))
        self.set_scan_time_volume(data, "2026-01-01T00:00:00+00:00", 500, 1000)
        one = self.save(os.path.join(self.base, "one.json"), data)
        scan_dir = os.path.join(self.base, "inputs")
        os.mkdir(scan_dir)
        for name, content in (("broken.json", "{"), ("not-scan.json", '{"format":"other"}')):
            with open(os.path.join(scan_dir, name), "w", encoding="utf-8") as fh:
                fh.write(content)
        stocks = self.stock_file("stock\tpath\tempties\nmissing\t{}/absent\towner\n".format(self.root))
        p = self.run_cli("trend", one, "--stocks", stocks, "--json")
        self.assertEqual(p.returncode, 0, p.stderr)
        result = json.loads(p.stdout)
        self.assertIsNone(result["stocks"][0]["per_day_last"])
        self.assertIn("one scan: no rates yet", self.run_cli("trend", one, "--stocks", stocks).stdout)
        p = self.run_cli("trend", scan_dir, "--stocks", stocks, "--json")
        self.assertEqual(p.returncode, 2)  # no usable scans remain
        bad = self.save(os.path.join(self.base, "bad-explicit.json"), {"format": "no"})
        p = self.run_cli("trend", bad, "--stocks", stocks)
        self.assertEqual(p.returncode, 2)
        self.assertIn("cannot load scan", p.stderr)
        # A directory mixed with a valid scan reports each skipped input in JSON and stderr.
        with open(os.path.join(scan_dir, "valid.json"), "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        p = self.run_cli("trend", scan_dir, "--stocks", stocks, "--json")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(len(json.loads(p.stdout)["warnings"]), 3)
        self.assertIn("stock path is in no scan", p.stderr)

    def test_trend_data_volume_mapping_and_unseen_arithmetic(self):
        root = "/System/Volumes/Data"
        a = document(root, node(root, 1000, children=[node("Users", 1000, children=[node("x", 1000)])]))
        b = document(root, node(root, 1200, children=[node("Users", 1200, children=[node("x", 1200)])]))
        self.set_scan_time_volume(a, "2026-01-01T00:00:00+00:00", 9000, 10000)
        self.set_scan_time_volume(b, "2026-01-03T00:00:00+00:00", 8000, 10000)
        fa, fb = self.save(os.path.join(self.base, "data-a.json"), a), self.save(os.path.join(self.base, "data-b.json"), b)
        stocks = self.stock_file("stock\tpath\tempties\nuser\t/Users/x\trules\n")
        p = self.run_cli("trend", fa, fb, "--stocks", stocks, "--json")
        self.assertEqual(p.returncode, 0, p.stderr)
        result = json.loads(p.stdout)
        self.assertEqual(result["stocks"][0]["bytes"], 1200)
        self.assertEqual(result["unseen"]["change_last"], 800)  # 1000 free loss less 200 scanned growth
        b["volume"]["free_bytes"] = 9500
        self.save(fb, b)
        p = self.run_cli("trend", fa, fb, "--stocks", stocks, "--json")
        self.assertEqual(json.loads(p.stdout)["unseen"]["change_last"], -700)

    def test_trend_refuses_bad_stock_rows_and_different_roots(self):
        data = document(self.root, node(self.root, 1))
        first = self.save(os.path.join(self.base, "one.json"), data)
        invalid = ["stock\tpath\n", "stock\tempties\n", "path\tempties\n",
                   "stock\tpath\tempties\nx\trelative\trules\n",
                   "stock\tpath\tempties\nx\t{}\tinvalid\n".format(self.root),
                   "stock\tpath\tempties\na\t{}\trules\nb\t{}\towner\n".format(self.root, self.root + "/.") ,
                   "stock\tpath\tempties\n\t{}\trules\n".format(self.root),
                   "stock\tpath\tempties\nx\t\trules\n",
                   "stock\tpath\tempties\nx\t{}\t\n".format(self.root),
                   "stock\tpath\tempties\nx\t{}/a/**/b/**/c\trules\n".format(self.root)]
        for content in invalid:
            stocks = self.stock_file(content)
            p = self.run_cli("trend", first, "--stocks", stocks)
            self.assertEqual(p.returncode, 2, content)
            self.assertRegex(p.stderr, r"line [123]")
        other = document(self.root + "-other", node(self.root + "-other", 1))
        second = self.save(os.path.join(self.base, "other.json"), other)
        stocks = self.stock_file("stock\tpath\tempties\nx\t{}\trules\n".format(self.root))
        p = self.run_cli("trend", first, second, "--stocks", stocks)
        self.assertEqual(p.returncode, 2)
        self.assertIn(self.root, p.stderr)
        self.assertIn(other["root"], p.stderr)

    def test_trend_refuses_empties_conflict_and_warns_for_missing_pattern(self):
        data = document(self.root, node(self.root, 1))
        scanfile = self.save(os.path.join(self.base, "one.json"), data)
        stocks = self.stock_file("stock\tpath\tempties\n"
                                 "same\t{}/one\trules\n"
                                 "same\t{}/two\towner\n".format(self.root, self.root))
        refused = self.run_cli("trend", scanfile, "--stocks", stocks)
        self.assertEqual(refused.returncode, 2)
        self.assertIn("stocks file line 3: stock \"same\" is listed with empties=rules above and empties=owner here", refused.stderr)
        stocks = self.stock_file("stock\tpath\tempties\nmissing\t{}/**/node_modules\towner\n".format(self.root))
        warned = self.run_cli("trend", scanfile, "--stocks", stocks, "--json")
        self.assertEqual(warned.returncode, 0, warned.stderr)
        result = json.loads(warned.stdout)
        expected = "{}/**/node_modules".format(self.root)
        message = "stock pattern matches nothing in any scan: {} (check the spelling)".format(expected)
        self.assertIn(message, warned.stderr)
        self.assertIn(message, result["warnings"])
        self.assertEqual(result["stocks"][0]["missing_paths"], [expected])

    def test_growth_outside_reports_deep_folder_and_rotate(self):
        oldtree = node(self.root, 100, children=[node("ancestor", 100, children=[node("growing", 100)])])
        newtree = node(self.root, 400, children=[node("ancestor", 400, children=[node("growing", 400)])])
        a, b = document(self.root, oldtree), document(self.root, newtree)
        self.set_scan_time_volume(a, "2026-01-01T00:00:00+00:00", 900, 1000)
        self.set_scan_time_volume(b, "2026-01-02T00:00:00+00:00", 800, 1000)
        fa, fb = self.save(os.path.join(self.base, "growth-a.json"), a), self.save(os.path.join(self.base, "growth-b.json"), b)
        stocks = self.stock_file("stock\tpath\tempties\nx\t{}/not-here\towner\n".format(self.root))
        p = self.run_cli("trend", fa, fb, "--stocks", stocks, "--min-mb", "0.000001", "--json")
        paths = [r["path"] for r in json.loads(p.stdout)["growth_outside"]]
        self.assertIn(self.root + "/ancestor/growing", paths)
        self.assertNotIn(self.root + "/ancestor", paths)
        self.assertNotIn(self.root, paths)
        scans = os.path.join(self.base, "rotate")
        os.mkdir(scans)
        for name, when in (("old.json", "2026-01-01"), ("new.json", "2026-01-03"), ("mid.json", "2026-01-02")):
            with open(os.path.join(scans, name), "w", encoding="utf-8") as fh:
                fh.write('{{"format":"disk-map/1","scanned_at":"{}"}}'.format(when))
        with open(os.path.join(scans, "other.json"), "w", encoding="utf-8") as fh:
            fh.write("not a scan")
        sub = os.path.join(scans, "sub")
        os.mkdir(sub)
        with open(os.path.join(sub, "nested.json"), "w", encoding="utf-8") as fh:
            fh.write("nested")
        p = self.run_cli("rotate", scans, "--keep", "2", "--dry-run")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("would remove " + os.path.join(scans, "old.json"), p.stdout)
        self.assertTrue(os.path.exists(os.path.join(scans, "old.json")))
        p = self.run_cli("rotate", scans, "--keep", "2")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertFalse(os.path.exists(os.path.join(scans, "old.json")))
        self.assertTrue(os.path.exists(os.path.join(scans, "other.json")))
        self.assertTrue(os.path.exists(os.path.join(sub, "nested.json")))
        self.assertEqual(self.run_cli("rotate", scans, "--keep", "0").returncode, 2)

    def test_growth_outside_lists_child_residuals_or_parent(self):
        mib = 1024 * 1024
        stockfile = self.stock_file("stock\tpath\tempties\nx\t{}/not-here\towner\n".format(self.root))

        def pair(old_tree, new_tree, stem):
            old = document(self.root, old_tree)
            new = document(self.root, new_tree)
            self.set_scan_time_volume(old, "2026-01-01T00:00:00+00:00", 900, 1000)
            self.set_scan_time_volume(new, "2026-01-02T00:00:00+00:00", 800, 1000)
            before = self.save(os.path.join(self.base, stem + "-old.json"), old)
            after = self.save(os.path.join(self.base, stem + "-new.json"), new)
            result = self.run_cli("trend", before, after, "--stocks", stockfile,
                                  "--min-mb", "1", "--json")
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)["growth_outside"]

        children = pair(
            node(self.root, 2 * mib, children=[node("parent", 2 * mib, children=[node("a", mib), node("b", mib)])]),
            node(self.root, 4 * mib, children=[node("parent", 4 * mib, children=[node("a", 2 * mib), node("b", 2 * mib)])]),
            "two-children")
        child_paths = {row["path"] for row in children}
        self.assertEqual(child_paths, {self.root + "/parent/a", self.root + "/parent/b"})
        self.assertEqual(sum(row["change_last"] for row in children), 2 * mib)

        parent = pair(
            node(self.root, 0, children=[node("parent", 0, children=[node("a", 0), node("b", 0), node("c", 0), node("d", 0)])]),
            node(self.root, 1200 * 1024, children=[node("parent", 1200 * 1024, children=[
                node("a", 300 * 1024), node("b", 300 * 1024), node("c", 300 * 1024), node("d", 300 * 1024)])]),
            "many-small")
        self.assertEqual(parent, [{"path": self.root + "/parent", "change_last": 1200 * 1024}])

    def trend_pair(self, first, second, stocks):
        """Save two scans and run trend in both text and JSON form."""
        fa = self.save(os.path.join(self.base, "trend-a.json"), first)
        fb = self.save(os.path.join(self.base, "trend-b.json"), second)
        text = self.run_cli("trend", fa, fb, "--stocks", stocks)
        self.assertEqual(text.returncode, 0, text.stderr)
        js = self.run_cli("trend", fa, fb, "--stocks", stocks, "--json")
        self.assertEqual(js.returncode, 0, js.stderr)
        return text.stdout, json.loads(js.stdout)

    @unittest.skipIf(getattr(os, "geteuid", lambda: 1)() == 0, "root can read chmod 000 directories")
    def test_trend_unreadable_and_partial_stocks_are_not_zero(self):
        locked = os.path.join(self.root, "locked")
        partial = os.path.join(self.root, "partial")
        hidden = os.path.join(partial, "hidden")
        open_ = os.path.join(self.root, "open")
        for path in (locked, hidden, open_):
            os.makedirs(path)
        for path in (os.path.join(partial, "data"), os.path.join(open_, "data")):
            with open(path, "wb") as fh:
                fh.write(b"x" * 4096)
        os.chmod(hidden, 0)
        os.chmod(locked, 0)
        try:
            data = self.scan("--min-node-mb", "0")
        finally:
            os.chmod(hidden, 0o700)
            os.chmod(locked, 0o700)
        later = json.loads(json.dumps(data))
        later["scanned_at"] = "2026-01-03T00:00:00+00:00"
        self.set_scan_time_volume(data, "2026-01-01T00:00:00+00:00", 900, 1000)
        self.set_scan_time_volume(later, "2026-01-03T00:00:00+00:00", 800, 1000)
        stocks = self.stock_file("stock\tpath\tempties\n"
                                 "locked stock\t{}\towner\n"
                                 "partial stock\t{}\towner\n"
                                 "open stock\t{}\trules\n".format(locked, partial, open_))
        text, result = self.trend_pair(data, later, stocks)
        rows = {row["stock"]: row for row in result["stocks"]}
        # Unreadable: no size, no change, explicit status; never a bare 0 B.
        locked_row = rows["locked stock"]
        self.assertEqual(locked_row["status"], "unreadable")
        self.assertIsNone(locked_row["bytes"])
        self.assertIsNone(locked_row["change_last"])
        self.assertIsNone(locked_row["per_day_last"])
        self.assertIsNone(locked_row["per_day_window"])
        self.assertGreaterEqual(locked_row["unreadable_entries"], 1)
        self.assertTrue(all(s["status"] == "unreadable" for s in locked_row["scans"]))
        # Partial: the measured size stays but is flagged as a lower bound.
        partial_row = rows["partial stock"]
        self.assertEqual(partial_row["status"], "partial")
        self.assertGreater(partial_row["bytes"], 0)
        self.assertEqual(partial_row["unreadable_entries"], 1)
        self.assertEqual(partial_row["change_last"], 0)  # partial keeps its change
        # Measured: unchanged behaviour.
        open_row = rows["open stock"]
        self.assertEqual(open_row["status"], "measured")
        self.assertGreater(open_row["bytes"], 0)
        self.assertEqual(open_row["unreadable_entries"], 0)
        self.assertEqual(open_row["change_last"], 0)
        # Text output: dashes for the unreadable stock, never 0 B; partial annotated.
        self.assertIn("locked stock (unreadable)", text)
        self.assertIn("—", text)
        for line in text.splitlines():
            if "not seen by the scan" in line:
                continue
            if "locked stock" in line:
                cells = line.split()
                self.assertEqual(cells[:4], ["—"] * 4)
            if "partial stock" in line:
                self.assertIn("1 unreadable entry", line)
                self.assertNotIn("—", line.split("partial stock")[0])
            if "open stock" in line:
                self.assertNotIn("unreadable", line)
        # The not-seen line names the stocks that can hide space there.
        unseen = result["unseen"]
        self.assertIn("locked stock", unseen["includes"]["unreadable"])
        self.assertIn("partial stock", unseen["includes"]["partial"])
        self.assertIn("includes unreadable: locked stock", text)
        self.assertIn("includes partial: partial stock", text)
        locked_lines = [line for line in text.splitlines() if "locked stock" in line and "not seen" not in line]
        self.assertEqual(len(locked_lines), 1)
        self.assertNotIn("0 B", locked_lines[0])

    def test_trend_unreadable_stock_detected_from_error_samples(self):
        # Scans written before erroring folders stayed in the tree only record
        # them in error_samples; trend must still not report them as 0 B.
        root = self.root
        first_tree = node(root, 200, own=50, small={"count": 1, "bytes": 50},
                          children=[node("partialx", 50, own=50), node("open", 50, own=50)])
        first_tree["children"][0]["errors"] = 1
        second_tree = node(root, 200, own=50, small={"count": 1, "bytes": 50},
                           children=[node("partialx", 50, own=50), node("open", 50, own=50)])
        second_tree["children"][0]["errors"] = 1
        first = document(root, first_tree)
        second = document(root, second_tree)
        self.set_scan_time_volume(first, "2026-01-01T00:00:00+00:00", 900, 1000)
        self.set_scan_time_volume(second, "2026-01-03T00:00:00+00:00", 800, 1000)
        for data in (first, second):
            data["error_samples"] = [
                {"path": os.path.join(root, "ghost"), "errno": "EACCES"},
                {"path": os.path.join(root, "partialx", "inner"), "errno": "EACCES"},
            ]
        stocks = self.stock_file("stock\tpath\tempties\n"
                                 "ghost\t{}\towner\n"
                                 "partial old\t{}\towner\n"
                                 "open\t{}\trules\n"
                                 "tiny\t{}\towner\n".format(
                                     os.path.join(root, "ghost"), os.path.join(root, "partialx"),
                                     os.path.join(root, "open"), os.path.join(root, "tiny")))
        text, result = self.trend_pair(first, second, stocks)
        rows = {row["stock"]: row for row in result["stocks"]}
        self.assertEqual(rows["ghost"]["status"], "unreadable")
        self.assertIsNone(rows["ghost"]["bytes"])
        self.assertIsNone(rows["ghost"]["change_last"])
        # partialx has 1 tree error; the sample under it is already tree-attributed.
        self.assertEqual(rows["partial old"]["status"], "partial")
        self.assertEqual(rows["partial old"]["bytes"], 50)
        self.assertEqual(rows["partial old"]["unreadable_entries"], 1)
        self.assertEqual(rows["open"]["status"], "measured")
        # A stock absent with no errors stays a measured 0 B with the missing-path warning.
        self.assertEqual(rows["tiny"]["status"], "measured")
        self.assertEqual(rows["tiny"]["bytes"], 0)
        self.assertIn("stock path is in no scan", text + "\n".join(result["warnings"]))
        self.assertNotIn("ghost", "\n".join(w for w in result["warnings"] if "in no scan" in w))


if __name__ == "__main__":
    unittest.main()
