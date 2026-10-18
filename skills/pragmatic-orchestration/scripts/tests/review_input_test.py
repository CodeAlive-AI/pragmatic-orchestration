"""Offline regressions for review ask prompt and stdin transport."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import tempfile
import unittest
from pathlib import Path


TESTS = Path(__file__).resolve().parent
PORCH = TESTS.parent / "porch"


class ReviewInputTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="porch-review-input-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.log = self.root / "argv.jsonl"
        self.prompt_file = self.root / "question.md"
        self.prompt_file.write_text("QUESTION_FROM_FILE", encoding="utf-8")
        self.env = {key: value for key, value in os.environ.items()
                    if not key.startswith(("PORCH_", "CODEX_"))}
        self.env.update(
            PORCH_CONFIG=str(TESTS / "fixtures" / "test-config.json"),
            PORCH_BIN_CODEX=str(TESTS / "fakes" / "fake-codex"),
            PORCH_FAKE_ARGV_LOG=str(self.log),
            PORCH_RUN_DIR=str(self.root / "run"),
            PORCH_OUTPUT_DIR=str(self.root / "archive"),
            PORCH_SUPPRESS_SHELL_WARN="1",
        )

    def ask(self, *args, input=None, expected=0):
        with (self.root / "stdout").open("w+") as out, \
                (self.root / "stderr").open("w+") as err:
            process = subprocess.Popen(
                [str(PORCH), "review", "ask", "--progress", "none",
                 "-a", "codex", *args],
                stdin=subprocess.PIPE, stdout=out, stderr=err,
                text=True, encoding="utf-8", env=self.env,
                start_new_session=True,
            )
            try:
                if input is None:
                    # Keep the write end open: communicate() would send EOF
                    # and conceal the inherited-stdin deadlock.
                    process.wait(timeout=10)
                else:
                    process.communicate(input, timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
                self.fail("review ask waited for EOF on unused stdin")
            finally:
                process.stdin.close()
            out.seek(0)
            err.seek(0)
            stdout, stderr = out.read(), err.read()
        self.assertEqual(process.returncode, expected, stderr)
        if expected == 0:
            self.assertIn("FAKE_CODEX_OK", stdout)
            rows = [json.loads(line) for line in self.log.read_text().splitlines()]
            self.assertEqual(len(rows), 1)
            return rows[0]["stdin"]
        self.assertFalse(self.log.exists(), "invalid input launched a backend")
        return stderr

    def test_positional_prompt_does_not_wait_for_stdin_eof(self):
        self.assertIn("POSITIONAL_QUESTION", self.ask("POSITIONAL_QUESTION"))

    def test_prompt_file_does_not_wait_for_stdin_eof(self):
        self.assertIn("QUESTION_FROM_FILE", self.ask("--prompt-file", str(self.prompt_file)))

    def test_explicit_blank_prompt_does_not_read_stdin(self):
        self.assertIn("No prompt provided", self.ask(" \t", expected=5))

    def test_blank_prompt_file_does_not_read_stdin(self):
        self.prompt_file.write_text(" \n\t", encoding="utf-8")
        self.assertIn("No prompt provided", self.ask("--prompt-file", str(self.prompt_file), expected=5))

    def test_stdin_supplies_prompt_when_no_explicit_prompt(self):
        prompt = self.ask(input="STDIN_QUESTION\nSECOND_LINE\n")
        self.assertIn("STDIN_QUESTION\nSECOND_LINE", prompt)
        self.assertNotIn("--- Input ---", prompt)

    def test_context_stdin_appends_to_positional_prompt(self):
        prompt = self.ask("--context-stdin", "QUESTION", input="CONTEXT\nSECOND_LINE\n")
        self.assertIn("QUESTION\n\n--- Input ---\nCONTEXT\nSECOND_LINE", prompt)

    def test_context_stdin_appends_to_prompt_file(self):
        prompt = self.ask("--context-stdin", "--prompt-file", str(self.prompt_file), input="CONTEXT")
        self.assertIn("QUESTION_FROM_FILE\n\n--- Input ---\nCONTEXT", prompt)

    def test_context_stdin_requires_explicit_prompt_before_reading(self):
        self.assertIn("--context-stdin requires", self.ask("--context-stdin", expected=5))

    def test_context_stdin_rejects_empty_input(self):
        self.assertIn("--context-stdin requires", self.ask("--context-stdin", "QUESTION", input=" \n", expected=5))

    def test_unused_stdin_is_not_added_to_explicit_prompt(self):
        prompt = self.ask("QUESTION", input="UNREQUESTED_CONTEXT")
        self.assertIn("QUESTION", prompt)
        self.assertNotIn("UNREQUESTED_CONTEXT", prompt)


if __name__ == "__main__":
    unittest.main()
