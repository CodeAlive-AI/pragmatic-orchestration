# Pragmatic Orchestration — Development Guide

Single-skill repo. `skills/pragmatic-orchestration/` follows the canonical Agent Skills layout, read by both `npx skills add` (the cross-agent CLI) and Claude Code's plugin install (`/plugin marketplace add` + `/plugin install …@pragmatic-orchestration`). The plugin sees the repo root as itself via `source: "./"` in `marketplace.json`.

## Git publishing defaults

When the user asks to commit or push without naming a target branch, commit and
push directly to `main`. If the current branch is `main`, stay on it; do not
create a feature branch by default. Use another branch only when the user
explicitly requests one.

## Naming

- Skill name: `pragmatic-orchestration` (SKILL.md frontmatter, directory, state dirs).
- CLI name: `porch` (`scripts/porch`, `scripts/porch.cmd`).
- Environment variables: `PORCH_*` prefix.
- Historical name: `agents-consilium` / `consilium` — do not reintroduce.

## Testing

Offline suite with fake backends — no network, no real provider CLIs:

```bash
cd skills/pragmatic-orchestration
bash scripts/tests/run.sh          # full harness (shell + python)
python3 scripts/tests/platform_test.py -v   # native-Python platform checks
```

Opt-in real-backend smoke tests: `scripts/tests/steer/smoke_real.sh` — see
[references/testing.md](skills/pragmatic-orchestration/references/testing.md).

## Versioning and releases

Follow semver. Skill/interface changes (`skills/**`, `.claude-plugin/*`) require
a version bump in BOTH `.claude-plugin/plugin.json` and
`.claude-plugin/marketplace.json` (keep them in sync), then:

```bash
git tag -a vX.Y.Z -m "Version X.Y.Z" && git push origin vX.Y.Z
gh release create vX.Y.Z --title "vX.Y.Z — Title" --notes "Release notes"
```

Docs-only changes (README/AGENTS) do not need a release.

## Configuration

`skills/pragmatic-orchestration/config.json` is user-local and gitignored — the
loader falls back to `config.example.json`. Never commit a personal
`config.json`; update `config.example.json` when the schema or roster changes.
