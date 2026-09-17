# Pragmatic Orchestration — Development Guide

Multi-skill repo shipped as one plugin. `skills/<name>/` follows the canonical Agent Skills layout, read by both `npx skills add` (the cross-agent CLI) and Claude Code's plugin install (`/plugin marketplace add` + `/plugin install …@pragmatic-orchestration`). The plugin sees the repo root as itself via `source: "./"` in `marketplace.json`. Current skills: `pragmatic-orchestration` (the `porch` orchestration CLI) and `remote-agents` (dedicated remote Linux/Windows agent hosts).

## Git publishing defaults

When the user asks to commit or push without naming a target branch, commit and
push directly to `main`. If the current branch is `main`, stay on it; do not
create a feature branch by default. Use another branch only when the user
explicitly requests one.

## Naming

- Skill names: `pragmatic-orchestration`, `remote-agents` (SKILL.md frontmatter, directory, state dirs).
- CLI name: `porch` (`scripts/porch`, `scripts/porch.cmd`); remote-agents has no CLI of its own, its entry point is `scripts/host.sh`.
- Environment variables: `PORCH_*` for orchestration; `REMOTE_AGENTS_*` for the host skill.
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

`skills/*/config.json` is user-local and gitignored — `pragmatic-orchestration`'s
loader falls back to `config.example.json`, while `remote-agents` requires a real
`config.json` (its example holds placeholders). `remote-agents` also keeps local
bridge secrets/state under `skills/remote-agents/.bridge-state/` (gitignored) —
never commit credentials, private keys, or real infrastructure identifiers there
or anywhere in the repo. Update `config.example.json` when a schema changes.
