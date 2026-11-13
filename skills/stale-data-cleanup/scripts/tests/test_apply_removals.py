import csv
import importlib.util
import io
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock


SCRIPT = os.path.join(os.path.dirname(__file__), "..", "apply_removals.py")
HEADER = "time\taction\tpath\tbytes\tfiles\trule\tevidence\tstatus"


class ApplyRemovalsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temp.name)
        self.area = self.root / "area"
        self.area.mkdir()
        self.manifest = self.root / "manifest.tsv"
        self.list_path = self.root / "list.tsv"

    def tearDown(self):
        self.temp.cleanup()

    def write_list(self, rows, columns=("path", "rule", "evidence", "min_age_days", "allow")):
        with self.list_path.open("w", encoding="utf-8", newline="") as f:
            writer = csv.writer(f, delimiter="\t", lineterminator="\n")
            writer.writerow(columns)
            for row in rows:
                writer.writerow([row.get(c, "") for c in columns])

    def invoke(self, rows=None, *, apply=False, args=None, env=None, columns=None, prefix=None):
        if rows is not None:
            self.write_list(rows, columns or ("path", "rule", "evidence", "min_age_days", "allow"))
        cmd = [sys.executable, SCRIPT, str(self.list_path), "--allow-prefix", str(prefix or self.area),
               "--manifest", str(self.manifest)]
        if args is None:
            cmd.extend(("--min-age-days", "0"))
        if apply:
            cmd.append("--apply")
        cmd.extend(args or ())
        return subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              env=dict(os.environ, **(env or {})), check=False)

    def row(self, path, **extra):
        return {"path": str(path), "rule": "stale", "evidence": "verified"} | extra

    def statuses(self, result):
        return [line.split("\t", 1)[0] for line in result.stdout.splitlines()[:-1]]

    def test_dry_run_and_apply_manifest_size(self):
        target = self.area / "payload"
        target.mkdir()
        (target / "one.txt").write_text("hello")
        dry = self.invoke([self.row(target)])
        self.assertEqual(dry.returncode, 0, dry.stderr)
        self.assertIn("would-remove", dry.stdout)
        self.assertTrue((target / "one.txt").exists())
        self.assertFalse(self.manifest.exists())
        applied = self.invoke([self.row(target)], apply=True)
        self.assertEqual(applied.returncode, 0, applied.stderr)
        self.assertFalse(target.exists())
        lines = self.manifest.read_text().splitlines()
        self.assertEqual(lines[0], HEADER)
        fields = next(csv.reader([lines[1]], delimiter="\t"))
        self.assertEqual(fields[1], "remove")
        self.assertGreater(int(fields[3]), 0)
        self.assertEqual(fields[4], "1")
        self.assertEqual(fields[-1], "done")

    def test_row_validation_and_prefix_rules(self):
        sibling = self.root / "area-extra"
        sibling.mkdir()
        tests = [
            (self.row(self.area / "x", rule=""), "refused: no rule"),
            (self.row(self.area / "x", evidence=""), "refused: no evidence"),
            (self.row("relative"), "refused: bad path"),
            (self.row(str(self.area / ".." / "outside")), "refused: bad path"),
            (self.row(self.root / "outside"), "refused: outside allowed prefixes"),
            (self.row(sibling), "refused: outside allowed prefixes"),
            (self.row(self.area), "refused: is an allowed prefix"),
        ]
        for row, expected in tests:
            with self.subTest(expected=expected):
                result = self.invoke([row])
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(expected, result.stdout)

    def test_symlinked_parent_outside_prefix_refused(self):
        outside = self.root / "outside"
        outside.mkdir()
        data = outside / "valuable"
        data.write_text("safe")
        link = self.area / "portal"
        link.symlink_to(outside, target_is_directory=True)
        result = self.invoke([self.row(link / "valuable")])
        self.assertIn("refused: outside allowed prefixes", result.stdout)
        self.assertEqual(data.read_text(), "safe")

    def test_var_and_private_prefix_spellings(self):
        target = self.area / "payload"
        target.mkdir()
        (target / "file").write_text("x")
        lexical_area = str(self.area)
        real_area = os.path.realpath(lexical_area)
        # macOS commonly exposes temporary paths through both /var and /private/var.
        result = self.invoke([self.row(target)], prefix=real_area)
        self.assertIn("would-remove", result.stdout)
        real_target = os.path.join(real_area, "payload")
        result = self.invoke([self.row(real_target)], prefix=lexical_area)
        self.assertIn("would-remove", result.stdout)

    def test_symlink_targets_are_not_followed(self):
        outside = self.root / "outside-data"
        outside.write_text("keep")
        link = self.area / "link"
        link.symlink_to(outside)
        result = self.invoke([self.row(link)], apply=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(link.exists())
        self.assertTrue(outside.exists())
        tree = self.area / "tree"
        tree.mkdir()
        (tree / "outside-link").symlink_to(outside)
        result = self.invoke([self.row(tree)], apply=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(tree.exists())
        self.assertTrue(outside.exists())

    def test_protected_paths(self):
        home = self.root / "home"
        (home / "Documents").mkdir(parents=True)
        ssh = self.area / ".ssh" / "thing"
        env_file = self.area / ".env.local"
        env_file.write_text("secret")
        rows = [self.row(home), self.row(home / "Documents"), self.row(ssh), self.row(env_file)]
        result = self.invoke(rows, env={"HOME": str(home)}, prefix=self.root)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.statuses(result), ["refused: protected"] * 4)

    def git(self, cwd, *args, check=True):
        p = subprocess.run(["git", "-C", str(cwd), *args], stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, text=True, check=False)
        if check and p.returncode:
            self.fail("git failed: " + p.stderr)
        return p

    def make_remote_clone(self):
        suffix = str(len(list(self.root.glob("remote-*.git"))))
        bare = self.root / ("remote-" + suffix + ".git")
        source = self.root / ("source-" + suffix)
        clone = self.area / "clone"
        subprocess.run(["git", "init", "--bare", str(bare)], check=True, stdout=subprocess.DEVNULL)
        source.mkdir()
        self.git(source, "init")
        self.git(source, "config", "user.email", "test@example.invalid")
        self.git(source, "config", "user.name", "Test")
        (source / "file.txt").write_text("base\n")
        self.git(source, "add", "file.txt")
        self.git(source, "commit", "-m", "base")
        self.git(source, "branch", "-M", "main")
        self.git(source, "remote", "add", "origin", str(bare))
        self.git(source, "push", "-u", "origin", "main")
        subprocess.run(["git", "--git-dir", str(bare), "symbolic-ref", "HEAD", "refs/heads/main"], check=True)
        subprocess.run(["git", "clone", str(bare), str(clone)], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.git(clone, "config", "user.email", "test@example.invalid")
        self.git(clone, "config", "user.name", "Test")
        return clone

    def test_git_clean_and_dirty_conditions(self):
        clean = self.make_remote_clone()
        result = self.invoke([self.row(clean)], apply=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(clean.exists())
        for kind, expected in (("untracked", "uncommitted changes"), ("modified", "uncommitted changes"),
                               ("stash", "stash entries"), ("unpushed", "commits on no remote")):
            with self.subTest(kind=kind):
                clone = self.make_remote_clone()
                if kind == "untracked":
                    (clone / "extra").write_text("x")
                elif kind == "modified":
                    (clone / "file.txt").write_text("changed\n")
                elif kind == "stash":
                    (clone / "file.txt").write_text("stashed\n")
                    self.git(clone, "stash", "push", "-m", "saved")
                else:
                    (clone / "new.txt").write_text("new\n")
                    self.git(clone, "add", "new.txt")
                    self.git(clone, "commit", "-m", "local")
                result = self.invoke([self.row(clone)])
                self.assertIn(expected, result.stdout)
                self.assertTrue(clone.exists())
                override = self.invoke([self.row(clone, allow="vcs")], apply=True)
                self.assertEqual(override.returncode, 0, override.stderr)
                self.assertFalse(clone.exists())

    def test_linked_worktree_and_nested_dirty_repository(self):
        clone = self.make_remote_clone()
        linked = self.area / "linked"
        self.git(clone, "worktree", "add", "-b", "linked", str(linked))
        result = self.invoke([self.row(linked)])
        self.assertIn("linked git worktree", result.stdout)
        nested = self.area / "parent"
        nested.mkdir()
        repo = nested / "nested"
        repo.mkdir()
        self.git(repo, "init")
        self.git(repo, "config", "user.email", "test@example.invalid")
        self.git(repo, "config", "user.name", "Test")
        (repo / "dirty").write_text("x")
        result = self.invoke([self.row(nested)])
        self.assertIn("uncommitted changes", result.stdout)

    def test_key_file_and_override(self):
        target = self.area / "tree"
        target.mkdir()
        (target / "private.pem").write_text("x")
        result = self.invoke([self.row(target)])
        self.assertIn("kept: contains key-like files (1)", result.stdout)
        result = self.invoke([self.row(target, allow="key-files")], apply=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(target.exists())

    def test_age_rules_and_ignore(self):
        target = self.area / "fresh"
        target.mkdir()
        (target / "recent").write_text("x")
        result = self.invoke([self.row(target)], args=())
        self.assertIn("kept: modified", result.stdout)
        self.assertIn("set min_age_days=0 in that row", result.stdout)
        removed = self.invoke([self.row(target, min_age_days="0")], args=(), apply=True)
        self.assertIn("done", removed.stdout)
        self.assertFalse(target.exists())
        target.mkdir()
        (target / "recent").write_text("x")
        self.assertIn("kept: modified", self.invoke([self.row(target)], args=("--min-age-days", "1")).stdout)
        module_tree = self.area / "modules"
        (module_tree / "node_modules").mkdir(parents=True)
        (module_tree / "node_modules" / "recent").write_text("x")
        self.assertIn("would-remove", self.invoke([self.row(module_tree)], args=("--min-age-days", "1")).stdout)
        self.assertIn("would-remove", self.invoke([self.row(target, min_age_days="0")], args=("--min-age-days", "1")).stdout)
        ignored = self.area / "ignored"
        (ignored / "vendor").mkdir(parents=True)
        (ignored / "vendor" / "recent").write_text("x")
        self.assertIn("would-remove", self.invoke([self.row(ignored)], args=("--min-age-days", "1", "--dependency-dir", "vendor")).stdout)

    def test_nested_jj_repository_and_dependency_ignore(self):
        colocated = self.area / "colocated"
        (colocated / ".jj").mkdir(parents=True)
        (colocated / ".git").write_text("gitdir: absent\n")
        result = self.invoke([self.row(colocated)], args=())
        self.assertIn("refused: jj repository (verify with jj, then allow=vcs)", result.stdout)

        target = self.area / "parent"
        nested = target / "one" / "two"
        (nested / ".jj").mkdir(parents=True)
        result = self.invoke([self.row(target)], args=())
        self.assertIn("refused: jj repository at one/two (verify with jj, then allow=vcs)", result.stdout)
        passed = self.invoke([self.row(target, allow="vcs")], args=(), apply=True)
        self.assertEqual(passed.returncode, 0, passed.stderr)
        self.assertFalse(target.exists())
        self.assertIn("[allow=vcs]", self.manifest.read_text())

        ignored = self.area / "dependency-tree"
        (ignored / "node_modules" / "nested" / ".jj").mkdir(parents=True)
        result = self.invoke([self.row(ignored)], args=())
        self.assertIn("would-remove", result.stdout)

    @unittest.skipUnless(sys.platform.startswith("linux") or shutil.which("lsof"), "requires /proc or lsof")
    def test_process_check_and_override(self):
        target = self.area / ".venv"
        target.mkdir()
        proc = subprocess.Popen(["sleep", "30"], cwd=target)
        try:
            time.sleep(0.2)
            result = self.invoke([self.row(target)])
            self.assertIn("kept: in use by pid", result.stdout)
            result = self.invoke([self.row(target)], apply=True, args=("--no-process-check",))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(target.exists())
        finally:
            proc.terminate()
            proc.wait(timeout=5)

    def test_manifest_append_sanitization_and_skips(self):
        target = self.area / "missing"
        first = self.invoke([self.row(target)], apply=True)
        self.assertEqual(first.returncode, 0, first.stderr)
        second = self.invoke([self.row(self.area)], apply=True)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(self.manifest.read_text().splitlines().count(HEADER), 1)
        self.assertEqual(len(self.manifest.read_text().splitlines()), 3)
        bad_path = str(self.area / "absent") + "\tsecond"
        self.list_path.write_text("path\trule\tevidence\n%s\tstale\tproof\n" % bad_path, encoding="utf-8")
        third = self.invoke(apply=True)
        self.assertEqual(third.returncode, 0, third.stderr)
        self.assertIn(str(self.area / "absent") + " second", self.manifest.read_text())
        self.assertIn("kept: missing", self.manifest.read_text())

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "read-only parent is not restrictive as root")
    def test_failed_removal_records_error_and_exit_one(self):
        parent = self.area / "locked"
        parent.mkdir()
        target = parent / "file"
        target.write_text("x")
        parent.chmod(0o555)
        try:
            result = self.invoke([self.row(target)], apply=True)
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertIn("error:", self.manifest.read_text())
        finally:
            parent.chmod(0o755)

    def test_missing_path_and_bad_header(self):
        result = self.invoke([self.row(self.area / "missing")], apply=True)
        self.assertEqual(result.returncode, 0)
        self.assertIn("kept: missing", result.stdout)
        self.manifest.write_text("wrong\theader\n")
        result = self.invoke([self.row(self.area / "missing")], apply=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn("header mismatch", result.stderr)
        result = self.invoke([self.row(self.area / "missing")], columns=("rule", "evidence"))
        self.assertEqual(result.returncode, 2)

    def test_json_output(self):
        target = self.area / "json"
        target.mkdir()
        result = self.invoke([self.row(target)], args=("--json",))
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual(data["mode"], "dry-run")
        self.assertEqual(data["removed"], 1)
        self.assertEqual(data["skipped"], 0)
        self.assertEqual(data["rows"][0]["status"], "would-remove")
        self.assertTrue(target.exists())
        self.assertFalse(self.manifest.exists())

    def test_dependency_directories_count_but_skip_inspections(self):
        target = self.area / "project"
        dep = target / "node_modules"
        dep.mkdir(parents=True)
        (dep / "secret.pem").write_text("secret")
        (dep / "fresh").write_text("recent")
        result = self.invoke([self.row(target)], args=("--min-age-days", "1"))
        self.assertIn("would-remove", result.stdout)
        result = self.invoke([self.row(target)], apply=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        fields = self.manifest.read_text().splitlines()[1].split("\t")
        self.assertGreater(int(fields[3]), 0)
        self.assertEqual(fields[4], "2")

        env_target = self.area / ".venv"
        env_target.mkdir()
        (env_target / "cacert.pem").write_text("certificate")
        result = self.invoke([self.row(env_target)])
        self.assertIn("would-remove", result.stdout)

    def test_protected_check_resolves_symlinked_parent(self):
        home = self.root / "home"
        ssh = home / ".ssh"
        ssh.mkdir(parents=True)
        target = ssh / "authorized_keys"
        target.write_text("keep")
        work = home / "work"
        work.mkdir()
        (work / "link").symlink_to(ssh, target_is_directory=True)
        result = self.invoke([self.row(work / "link" / "authorized_keys")], env={"HOME": str(home)}, prefix=home)
        self.assertIn("refused: protected", result.stdout)
        self.assertEqual(target.read_text(), "keep")

    def test_env_examples_and_protected_directories_as_keys(self):
        example_target = self.area / ".env.example"
        example_target.write_text("placeholder")
        self.assertIn("would-remove", self.invoke([self.row(example_target)]).stdout)
        target = self.area / "tree"
        target.mkdir()
        for name in (".env.example", ".env.sample", ".env.template", ".env.dist", ".env.defaults"):
            (target / name).write_text("placeholder")
        result = self.invoke([self.row(target)])
        self.assertIn("would-remove", result.stdout)
        secret_dir = target / ".ssh"
        secret_dir.mkdir()
        result = self.invoke([self.row(target)])
        self.assertIn("kept: contains key-like files (1), e.g. .ssh", result.stdout)
        result = self.invoke([self.row(target, allow="key-files")])
        self.assertIn("would-remove", result.stdout)

    def test_plain_tsv_quotes_bad_path_and_help(self):
        named = self.area / 'a"quoted-name'
        named.mkdir()
        (named / "file").write_text("x")
        self.list_path.write_text('path\trule\tevidence\n%s\t"rule"\t"proof"\n' % named, encoding="utf-8")
        result = self.invoke()
        self.assertIn("would-remove", result.stdout)
        result = self.invoke(apply=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        manifest_fields = self.manifest.read_text().splitlines()[1].split("\t")
        self.assertEqual(len(manifest_fields), 8)
        self.assertEqual(manifest_fields[5], '"rule"')
        self.assertEqual(manifest_fields[6], '"proof"')
        bad_path = str(self.area / "bad") + "\tsecond"
        self.list_path.write_text("path\trule\tevidence\n%s\trule\tproof\n" % bad_path, encoding="utf-8")
        result = self.invoke()
        self.assertIn("refused: bad path", result.stdout)
        help_result = subprocess.run([sys.executable, SCRIPT, "--help"], text=True, stdout=subprocess.PIPE)
        self.assertIn("Without --apply this is a dry run", help_result.stdout)
        self.assertIn("--dependency-dir NAME", help_result.stdout)
        self.assertIn("Columns: path, rule, evidence (required)", help_result.stdout)
        self.assertIn("default 1 day, 0 disables", help_result.stdout)

    def test_process_listing_is_shared_across_rows(self):
        targets = [self.area / ("target-%d" % i) for i in range(3)]
        for target in targets:
            target.mkdir()
        self.write_list([self.row(target) for target in targets], columns=("path", "rule", "evidence"))
        spec = importlib.util.spec_from_file_location("apply_removals_under_test", SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        args = [str(self.list_path), "--allow-prefix", str(self.area), "--manifest", str(self.manifest)]
        with mock.patch.object(module, "collect_process_cwds", return_value=([], True)) as listing:
            with unittest.mock.patch("sys.stdout", new=io.StringIO()):
                self.assertEqual(module.main(args), 0)
        listing.assert_called_once()


if __name__ == "__main__":
    unittest.main()
