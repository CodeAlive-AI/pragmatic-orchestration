"""Per-run isolation of cursor-agent's mutable config directory.

cursor-agent persists ``model.modelId`` into ``<config>/cli-config.json``
whenever ``--model`` is passed -- even when the run fails. Porch therefore
spawns every cursor-agent process with ``CURSOR_CONFIG_DIR`` pointing at an
isolated, per-run directory seeded from the user's real config:

- ``cli-config.json`` is *copied* (writable), never linked: ``--model`` and any
  other config writes land on the copy and die with the run.
- Every other top-level entry (``chats/``, ``acp-sessions/``, ``mcp.json``,
  ``skills*``, ...) is symlinked, so chat history still lands in the real
  location and the user's MCP servers and skills stay visible to the run.
- Login credentials are not in ``cli-config.json`` (cursor-agent keeps them in
  the OS credential store), so the copy preserves authentication.

The helper is shared by the one-shot path (``backend_run.sh``, which calls the
CLI form below) and the steerable adapter (``steer/adapters/cursor.py``).
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
from pathlib import Path

ISOLATED_PREFIX = "porch-cursor-config."


def source_config_dir(env: dict | None = None) -> Path:
    """The real config root cursor-agent would use without our override.

    Mirrors the CLI: explicit CURSOR_CONFIG_DIR wins, then
    $XDG_CONFIG_HOME/cursor, then ~/.cursor.
    """
    env = os.environ if env is None else env
    override = (env.get("CURSOR_CONFIG_DIR") or "").strip()
    if override:
        return Path(override).expanduser()
    xdg = (env.get("XDG_CONFIG_HOME") or "").strip()
    if xdg:
        return Path(xdg).expanduser() / "cursor"
    return Path.home() / ".cursor"


def prepare(root: "str | Path | None" = None, env: dict | None = None) -> tuple[str, list[str]]:
    """Create the isolated config dir; return (absolute path, link warnings).

    ``root`` names the directory to fill (created if missing); when None a
    fresh ``tempfile.mkdtemp`` is used. Entries that cannot be symlinked are
    reported in ``warnings`` -- isolation still holds (the real cli-config.json
    is never exposed), those entries just fall back to a fresh per-run copy.
    """
    src = source_config_dir(env)
    if root is None:
        root_path = Path(tempfile.mkdtemp(prefix=ISOLATED_PREFIX))
    else:
        root_path = Path(root)
        root_path.mkdir(parents=True, exist_ok=True)
    warnings: list[str] = []
    if src.is_dir():
        for entry in sorted(src.iterdir()):
            target = root_path / entry.name
            if target.exists() or target.is_symlink():
                continue
            if entry.name == "cli-config.json":
                shutil.copy2(entry, target)
            else:
                try:
                    os.symlink(entry, target)
                except OSError:
                    warnings.append(entry.name)
    return str(root_path.resolve()), warnings


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", default="",
                    help="Directory to fill (default: fresh dir under $TMPDIR)")
    args = ap.parse_args(argv)
    path, warnings = prepare(args.root or None)
    for name in warnings:
        print(f"cursor_config: entry not linked into isolated config: {name}",
              file=sys.stderr)
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
