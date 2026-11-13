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
        for command in (None, "scan", "query", "diff", "reconcile"):
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


if __name__ == "__main__":
    unittest.main()
