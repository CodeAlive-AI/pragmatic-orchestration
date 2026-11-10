#!/usr/bin/env python3
"""Offline tests for `porch media` with fake agy, codex, claude and ffmpeg."""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import tempfile
import unittest

TESTS = pathlib.Path(__file__).resolve().parent
FAKES = TESTS / "fakes"
PORCH = TESTS.parent / "porch"
CONFIG = TESTS / "fixtures" / "test-config.json"
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 16


class MediaTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="porch-media-test-")
        self.tmp = pathlib.Path(self._tmp.name)
        self.cwd = self.tmp / "cwd"
        self.cwd.mkdir()
        self.log = self.tmp / "argv.jsonl"
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith(("PORCH_", "CODEX_", "AGY_", "CLAUDE_"))}
        self.env.update({
            "PORCH_CONFIG": str(CONFIG),
            "PORCH_BIN_AGY": str(FAKES / "fake-agy"),
            "PORCH_BIN_CODEX": str(FAKES / "fake-codex"),
            "PORCH_BIN_CLAUDE": str(FAKES / "fake-claude"),
            "PORCH_BIN_FFMPEG": str(FAKES / "fake-ffmpeg"),
            "PORCH_BIN_FFPROBE": str(FAKES / "fake-ffprobe"),
            "PORCH_AGY_BRAIN_DIR": str(self.tmp / "brain"),
            "CODEX_HOME": str(self.tmp / "codex-home"),
            "PORCH_FAKE_ARGV_LOG": str(self.log),
            "PORCH_OUTPUT_DIR": str(self.tmp / "outputs"),
        })

    def tearDown(self):
        self._tmp.cleanup()

    def porch(self, *args, **env):
        return subprocess.run([str(PORCH), "media", *args, "--progress", "none"]
                              if args and args[0] != "capabilities" else
                              [str(PORCH), "media", *args],
                              cwd=self.cwd, env={**self.env, **env}, text=True,
                              capture_output=True, stdin=subprocess.DEVNULL, timeout=120)

    def rows(self):
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines() if line.strip()]

    def image(self, name="in.png"):
        path = self.cwd / name
        path.write_bytes(PNG)
        return name

    # ---- capabilities --------------------------------------------------

    def test_capabilities_lists_capable_profiles(self):
        r = self.porch("capabilities", "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        table = json.loads(r.stdout)
        self.assertEqual(table["transcribe"], ["agy"])
        self.assertIn("claude-code", table["image.describe"])
        self.assertIn("codex", table["image.generate"])
        self.assertEqual(sorted(table["image.animate"])[:2], ["agy", "codex"])
        self.assertNotIn("claude-code", table["image.generate"])
        self.assertNotIn("devin", table["image.describe"])

    # ---- transcription -------------------------------------------------

    def test_transcribe_chunks_and_offsets_srt(self):
        (self.cwd / "talk.m4a").write_text("audio")
        r = self.porch("transcribe", "-a", "agy", "--format", "srt", "--speakers",
                       "--segment-minutes", "15", "talk.m4a", PORCH_FAKE_MEDIA_SECONDS="2000")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Speaker 1: FAKE_SPEECH chunk-000.mp3 one", r.stdout)
        self.assertIn("00:15:00,500 --> 00:15:02,000\nSpeaker 1: FAKE_SPEECH chunk-001.mp3 one",
                      r.stdout)
        self.assertIn("00:30:00,500 --> 00:30:02,000", r.stdout)
        rows = self.rows()
        ffmpeg = [x for x in rows if x.get("bin") == "ffmpeg" and "-segment_time" in x["argv"]][0]
        argv = ffmpeg["argv"]
        self.assertEqual(argv[argv.index("-segment_time") + 1], "900")
        for flag in ("-vn", "libmp3lame"):
            self.assertIn(flag, argv)
        launches = [x for x in rows if x.get("transport") == "headless"]
        self.assertEqual(len(launches), 3)
        for row in launches:
            deny = row["deny"]
            for rule in ("write_file(*)", "command(*)", "read_url(*)"):
                self.assertIn(rule, deny)
            self.assertIn("--json-schema", row["argv"])
            self.assertNotEqual(pathlib.Path(row["cwd"]).resolve(), self.cwd.resolve())
        self.assertEqual(list(self.cwd.iterdir()), [self.cwd / "talk.m4a"])

    def test_transcribe_renders_text_vtt_and_json(self):
        (self.cwd / "clip.mp4").write_text("video")
        text = self.porch("transcribe", "-a", "agy", "clip.mp4")
        self.assertEqual(text.returncode, 0, text.stderr)
        self.assertEqual(text.stdout,
                         "FAKE_SPEECH chunk-000.mp3 one FAKE_SPEECH chunk-000.mp3 two\n")
        vtt = self.porch("transcribe", "-a", "agy", "--format", "vtt", "clip.mp4")
        self.assertTrue(vtt.stdout.startswith("WEBVTT\n\n00:00:00.500 --> 00:00:02.000\n"))
        out = self.cwd / "t" / "clip.json"
        js = self.porch("transcribe", "-a", "agy", "--format", "json", "--out", str(out),
                        "--language", "en", "clip.mp4")
        self.assertEqual(js.returncode, 0, js.stderr)
        self.assertEqual(js.stdout, "")
        data = json.loads(out.read_text())
        self.assertEqual(data["agent"], "agy")
        self.assertEqual(data["language"], "en")
        self.assertEqual(len(data["segments"]), 2)
        self.assertNotIn("speaker", data["segments"][0])

    def test_transcribe_uses_wav_without_mp3_encoder(self):
        (self.cwd / "a.flac").write_text("audio")
        r = self.porch("transcribe", "-a", "agy", "a.flac", PORCH_FAKE_FFMPEG_NO_MP3="1")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("chunk-000.wav", r.stdout)

    def test_transcribe_failures_are_explicit(self):
        (self.cwd / "a.wav").write_text("audio")
        deaf = self.porch("transcribe", "-a", "agy", "a.wav", PORCH_FAKE_AGY_MEDIA="deaf")
        self.assertEqual(deaf.returncode, 1)
        self.assertIn("could not transcribe it: binary file type is not supported", deaf.stderr)
        silent = self.porch("transcribe", "-a", "agy", "a.wav", PORCH_FAKE_AGY_MEDIA="silent")
        self.assertEqual(silent.returncode, 66)
        bad = self.porch("transcribe", "-a", "agy", "a.wav", PORCH_FAKE_FFMPEG_FAIL="1")
        self.assertEqual(bad.returncode, 1)
        self.assertIn("ffmpeg could not extract audio", bad.stderr)
        missing = self.porch("transcribe", "-a", "agy", "a.wav",
                             PORCH_BIN_FFMPEG=str(self.tmp / "no-ffmpeg"))
        self.assertEqual(missing.returncode, 4)
        self.assertIn("ffmpeg is required", missing.stderr)
        nofile = self.porch("transcribe", "-a", "agy", "nope.wav")
        self.assertEqual(nofile.returncode, 5)

    def test_unsupported_profile_names_capable_ones(self):
        (self.cwd / "a.wav").write_text("audio")
        r = self.porch("transcribe", "-a", "codex", "a.wav")
        self.assertEqual(r.returncode, 4)
        self.assertIn("does not support media transcribe; profiles that can: agy", r.stderr)
        self.assertIn("porch media transcribe -a agy", r.stderr)
        r = self.porch("image", "generate", "-a", "claude-code", "a cat")
        self.assertEqual(r.returncode, 4)
        r = self.porch("transcribe", "-a", "no-such-profile", "a.wav")
        self.assertEqual(r.returncode, 4)

    # ---- images ----------------------------------------------------------

    def test_agy_generate_collects_session_images(self):
        r = self.porch("image", "generate", "-a", "agy", "--count", "2", "--aspect", "16:9",
                       "--out-dir", "out", "A red apple on white")
        self.assertEqual(r.returncode, 0, r.stderr)
        manifest = json.loads(r.stdout)
        self.assertEqual((manifest["requested"], manifest["produced"]), (2, 2))
        names = sorted(pathlib.Path(i["path"]).name for i in manifest["images"])
        self.assertEqual(names, ["a-red-apple-on-white-1.jpg", "a-red-apple-on-white-2.jpg"])
        for item in manifest["images"]:
            self.assertTrue(pathlib.Path(item["path"]).read_bytes().startswith(b"\xff\xd8\xff"))
            self.assertNotIn(".system_generated", item["source"])
        prompt = [x for x in self.rows() if x.get("event") == "stdin_prompt"][0]["text"]
        self.assertIn("call it exactly 2 times", prompt)
        self.assertIn("Aspect ratio: 16:9.", prompt)
        again = self.porch("image", "generate", "-a", "agy", "--out-dir", "out",
                           "A red apple on white")
        self.assertEqual(json.loads(again.stdout)["images"][0]["path"],
                         str((self.cwd / "out" / "a-red-apple-on-white-3.jpg").resolve()))

    def test_partial_and_missing_images(self):
        short = self.porch("image", "generate", "-a", "agy", "--count", "3", "x",
                           PORCH_FAKE_AGY_MEDIA="short")
        self.assertEqual(short.returncode, 2, short.stderr)
        self.assertEqual(json.loads(short.stdout)["produced"], 2)
        none = self.porch("image", "generate", "-a", "codex", "x", PORCH_FAKE_CODEX_MEDIA="none")
        self.assertEqual(none.returncode, 66)
        self.assertIn("produced no image", none.stderr)
        self.assertEqual(json.loads(none.stdout)["produced"], 0)

    def test_codex_edit_attaches_staged_images(self):
        first, second = self.image("a.png"), self.image("b.png")
        r = self.porch("image", "edit", "-a", "codex", "--input", first, "--input", second,
                       "--name", "combo", "--out-dir", "edits", "merge them")
        self.assertEqual(r.returncode, 0, r.stderr)
        manifest = json.loads(r.stdout)
        self.assertEqual(manifest["agent_note"], "FAKE_CODEX_IMAGES count=1 attached=2")
        self.assertTrue((self.cwd / "edits" / "combo-1.png").is_file())
        codex = [x for x in self.rows() if x.get("bin") == "codex"][0]
        images = [a for a in codex["argv"] if a.startswith("--image=")]
        self.assertEqual([pathlib.Path(a.split("=", 1)[1]).name for a in images],
                         ["image-1.png", "image-2.png"])
        self.assertIn("--sandbox", codex["argv"])
        self.assertIn("read-only", codex["argv"])
        self.assertNotIn("--search", codex["argv"])

    def test_agy_edit_limits_and_input_validation(self):
        imgs = [self.image(f"i{n}.png") for n in range(4)]
        too_many = self.porch("image", "edit", "-a", "agy", *sum((["--input", i] for i in imgs), []),
                              "combine")
        self.assertEqual(too_many.returncode, 5)
        self.assertIn("at most 3 input images", too_many.stderr)
        (self.cwd / "notes.txt").write_text("hello")
        bad = self.porch("image", "edit", "-a", "agy", "--input", "notes.txt", "x")
        self.assertEqual(bad.returncode, 5)
        none = self.porch("image", "edit", "-a", "agy", "x")
        self.assertEqual(none.returncode, 5)
        ok = self.porch("image", "edit", "-a", "agy", "--input", imgs[0], "--input", imgs[1],
                        "make it blue")
        self.assertEqual(ok.returncode, 0, ok.stderr)
        self.assertIn("refs=2", json.loads(ok.stdout)["agent_note"])
        badaspect = self.porch("image", "generate", "-a", "agy", "--aspect", "wide", "x")
        self.assertEqual(badaspect.returncode, 5)
        nocount = self.porch("image", "generate", "-a", "agy", "--count", "9", "x")
        self.assertEqual(nocount.returncode, 5)

    def test_sheet_grid(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("porch_media", TESTS.parent / "lib" / "media.py")
        media = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(media)
        self.assertEqual({n: media.sheet_grid(n) for n in (2, 3, 5, 6, 7, 8, 9, 12, 16)},
                         {2: (2, 1), 3: (3, 1), 5: (3, 2), 6: (3, 2), 7: (3, 3), 8: (4, 2),
                          9: (3, 3), 12: (4, 3), 16: (4, 4)})

    def test_codex_animate_assembles_gif_from_spritesheet(self):
        r = self.porch("image", "animate", "-a", "codex", "--frames", "8", "--frame-ms", "150",
                       "--width", "256", "--name", "ball", "--out-dir", "anim",
                       "a bouncing ball")
        self.assertEqual(r.returncode, 0, r.stderr)
        manifest = json.loads(r.stdout)
        self.assertEqual((manifest["frames"], manifest["frame_ms"], manifest["grid"]),
                         (8, 150, "4x2"))
        gif, sheet = (pathlib.Path(i["path"]) for i in manifest["images"])
        self.assertEqual((gif.name, sheet.name), ("ball-1.gif", "ball-sheet-1.png"))
        self.assertTrue(gif.read_bytes().startswith(b"GIF89a"))
        prompt = [x for x in self.rows() if x.get("bin") == "codex"][0]["stdin"]
        self.assertIn("4 columns × 2 rows", prompt)
        self.assertIn("Aspect ratio: 2:1.", prompt)
        self.assertIn("Animation: a bouncing ball", prompt)
        ffmpeg = [x for x in self.rows() if x.get("bin") == "ffmpeg" and "-final_delay" in x["argv"]][0]
        argv = ffmpeg["argv"]
        graph = argv[argv.index("-vf") + 1]
        for part in ("format=rgb24", "untile=4x2", "trim=end_frame=8", "setpts=N*0.15/TB",
                     "scale=256:-2", "paletteuse"):
            self.assertIn(part, graph)
        self.assertEqual(argv[argv.index("-final_delay") + 1], "15")
        self.assertEqual(argv[argv.index("-loop") + 1], "0")

    def test_agy_animate_with_reference_and_validation(self):
        img = self.image()
        r = self.porch("image", "animate", "-a", "agy", "--frames", "7", "--input", img,
                       "a waving flag")
        self.assertEqual(r.returncode, 0, r.stderr)
        prompt = [x for x in self.rows() if x.get("event") == "stdin_prompt"][0]["text"]
        self.assertIn("Leave the last 2 cells plain empty background.", prompt)
        self.assertIn("./image-1.png", prompt)
        for args, code in ((["--frames", "1"], 5), (["--frames", "17"], 5),
                           (["--frame-ms", "155"], 5), (["--width", "4"], 5)):
            bad = self.porch("image", "animate", "-a", "agy", *args, "x")
            self.assertEqual(bad.returncode, code, (args, bad.stderr))
        claude = self.porch("image", "animate", "-a", "claude-code", "x")
        self.assertEqual(claude.returncode, 4)
        none = self.porch("image", "animate", "-a", "codex", "x", PORCH_FAKE_CODEX_MEDIA="none")
        self.assertEqual(none.returncode, 66)
        launches = len([x for x in self.rows() if x.get("bin") == "codex"])
        noff = self.porch("image", "animate", "-a", "codex", "x",
                          PORCH_BIN_FFMPEG=str(self.tmp / "missing"))
        self.assertEqual(noff.returncode, 4)
        self.assertEqual(len([x for x in self.rows() if x.get("bin") == "codex"]), launches,
                         "a missing ffmpeg must fail before spending a generation")

    def test_describe_backends(self):
        img = self.image()
        agy = self.porch("image", "describe", "-a", "agy", "--input", img)
        self.assertEqual(agy.returncode, 0, agy.stderr)
        self.assertEqual(agy.stdout, "FAKE_AGY_DESCRIPTION image-1.png\n")
        codex = self.porch("image", "describe", "-a", "codex", "--input", img, "What is it?")
        self.assertEqual(codex.stdout, "FAKE_CODEX_DESCRIPTION\n")
        claude = self.porch("image", "describe", "-a", "claude-code", "--input", img)
        self.assertEqual(claude.returncode, 0, claude.stderr)
        row = [x for x in self.rows() if x.get("bin") == "claude"][-1]
        argv = row["argv"]
        self.assertEqual(argv[argv.index("--allowedTools") + 1], "Read")
        self.assertIn("Bash", argv[argv.index("--disallowedTools") + 1])


if __name__ == "__main__":
    unittest.main()
