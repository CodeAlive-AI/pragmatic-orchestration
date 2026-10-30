"""Offline Go catalog coverage and launch contracts; no provider inference."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

TESTS = Path(__file__).resolve().parent
SCRIPTS = TESTS.parent
SKILL = SCRIPTS.parent
sys.path.insert(0, str(SCRIPTS / "lib"))
from steer.adapters.opencode import OpenCodeAdapter
from steer.config_loader import agent_settings

CATALOG = json.loads((TESTS / "fixtures/opencode-go-models.json").read_text())
CONFIG = json.loads((SKILL / "config.example.json").read_text())


class OpenCodeGoTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="porch-go-catalog-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.env = dict(os.environ, PORCH_CONFIG=str(SKILL / "config.example.json"),
                        PORCH_BIN_OPENCODE=str(TESTS / "fakes/fake-opencode"),
                        PORCH_OUTPUT_DIR=str(self.root / "outputs"),
                        PORCH_STEER_DIR=str(self.root / "registry"),
                        PORCH_SAVE_OUTPUTS="0", PORCH_SUPPRESS_SHELL_WARN="1")
        for key in ("OPENCODE_MODEL", "OPENCODE_EFFORT", "PORCH_RUN_DIR",
                    "PORCH_ARTIFACT_KEY", "PORCH_DUMP_ARGV"):
            self.env.pop(key, None)

    def run_command(self, argv, env=None):
        result = subprocess.run(argv, cwd=self.root, env=env or self.env,
                                input="", capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def test_current_provider_catalog_has_explicit_profiles(self):
        for model in CATALOG["models"]:
            with self.subTest(model=model["id"]):
                profile = CONFIG["agents"]["opencode-go-" + model["id"]]
                self.assertEqual(profile["backend"], "opencode")
                self.assertEqual(profile["model"], "opencode-go/" + model["id"])
                # The oracle is a captured provider/CLI catalog, not the config.
                expected = model["variants"][-1] if model["variants"] else "none"
                self.assertEqual(profile["effort"], expected)
                self.assertEqual(profile["enabled"], model["id"] == "kimi-k3")

    def test_review_and_delegate_argv_match_catalog(self):
        for model in CATALOG["models"]:
            profile_id = "opencode-go-" + model["id"]
            for mode in ("review", "delegate"):
                with self.subTest(model=model["id"], mode=mode):
                    output = self.root / "argv.json"
                    self.run_command([str(SCRIPTS / "lib/backend_run.sh"), "--mode", mode,
                                      "--agent-id", profile_id, "--raw", "CATALOG_CHECK"],
                                     dict(self.env, PORCH_DUMP_ARGV=str(output)))
                    argv = json.loads(output.read_text())["argv"]
                    self.assertEqual(argv[argv.index("-m") + 1], "opencode-go/" + model["id"])
                    if model["variants"]:
                        self.assertEqual(argv[argv.index("--variant") + 1], model["variants"][-1])
                    else:
                        self.assertNotIn("--variant", argv)

    def test_steerable_payload_matches_catalog(self):
        with patch.dict(os.environ, self.env, clear=True):
            for model in CATALOG["models"]:
                with self.subTest(model=model["id"]):
                    profile_id = "opencode-go-" + model["id"]
                    agent, backend, resolved_model, binary = agent_settings(profile_id)
                    self.assertEqual(backend, "opencode")
                    adapter = OpenCodeAdapter(binary=binary, model=resolved_model,
                                              effort=agent["effort"], cwd=str(self.root),
                                              artifacts_dir=str(self.root))
                    adapter.session_id = "catalog-session"
                    with patch.object(adapter, "_http_json", return_value=None) as request:
                        adapter._prompt_async("CATALOG_CHECK", "catalog-client")
                    payload = request.call_args.kwargs["body"]
                    self.assertEqual(payload["model"], {"providerID": "opencode-go",
                                                       "modelID": model["id"]})
                    if model["variants"]:
                        self.assertEqual(payload["variant"], model["variants"][-1])
                    else:
                        self.assertNotIn("variant", payload)

    def test_cli_selects_new_and_deepseek_profiles(self):
        listing = self.run_command([str(SCRIPTS / "porch"), "--list-agents"]).stdout
        for model in CATALOG["models"]:
            with self.subTest(model=model["id"]):
                self.assertIn('id="opencode-go-' + model["id"] + '"', listing)
        for model_id in ("deepseek-v4.1-flash", "glm-5.3-flash", "gpt-6-luna",
                         "minimax-m3", "mimo-v2.6-pro"):
            for mode in ("review", "delegate"):
                with self.subTest(model=model_id, mode=mode):
                    log = self.root / "native.jsonl"
                    log.unlink(missing_ok=True)
                    command = (["review", "ask", "--progress", "none"] if mode == "review"
                               else ["delegate", "--one-shot"])
                    result = self.run_command([str(SCRIPTS / "porch"), *command, "-a",
                                               "opencode-go-" + model_id, "CATALOG_CHECK"],
                                              dict(self.env, PORCH_FAKE_ARGV_LOG=str(log)))
                    self.assertIn("FAKE_OPENCODE_OK", result.stdout)
                    argv = json.loads(log.read_text().splitlines()[0])["argv"]
                    self.assertEqual(argv[argv.index("-m") + 1], "opencode-go/" + model_id)

    def test_new_profile_keeps_explicit_effort_override(self):
        for effort, expected in (("low", "low"), ("none", "")):
            with self.subTest(effort=effort), patch.dict(
                    os.environ, dict(self.env, OPENCODE_EFFORT=effort), clear=True):
                agent, _, model, _ = agent_settings("opencode-go-glm-5.3-flash")
                self.assertEqual(model, "opencode-go/glm-5.3-flash")
                self.assertEqual(agent["effort"], expected)


if __name__ == "__main__":
    unittest.main()
