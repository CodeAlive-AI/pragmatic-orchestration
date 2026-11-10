# Pragmatic Orchestration — Development Guide

Multi-skill repo shipped as one plugin. `skills/<name>/` follows the canonical Agent Skills layout, read by both `npx skills add` (the cross-agent CLI) and Claude Code's plugin install (`/plugin marketplace add` + `/plugin install …@pragmatic-orchestration`). The plugin sees the repo root as itself via `source: "./"` in `marketplace.json`. Current skills: `pragmatic-orchestration` (the `porch` orchestration CLI), `coordinator-mode` (the long-lived coordinating role, its journal and compaction recovery) and `remote-agents` (dedicated remote Linux/Windows agent hosts).

## Roles in the shipped skills

Two roles run through the skills and must stay distinct in every text and prompt:

- **Coordinator** (orchestrator, parent): the session that works with the user. It owns design, verification strategy and acceptance, and in Coordinator Mode leaves implementation to executors by default. The mode has no hard prohibitions; it relies on the coordinator's judgment.
- **Executor** (worker, delegate): owns one assignment. `porch delegate` prefixes every new task with `skills/pragmatic-orchestration/prompts/delegate-executor.txt`. A passage in any `AGENTS.md` that grants ownership of the whole codebase is addressed to the coordinator, and the contract says so.

When editing either skill, keep the executor contract short (it is paid for on every delegation) and keep `coordinator-mode/SKILL.md` under 5,000 tokens: Claude Code restores an invoked skill after compaction only up to that size.

## Git publishing defaults

When the user asks to commit or push without naming a target branch, commit and
push directly to `main`. If the current branch is `main`, stay on it; do not
create a feature branch by default. Use another branch only when the user
explicitly requests one.

## Naming

- Skill names: `pragmatic-orchestration`, `coordinator-mode`, `remote-agents` (SKILL.md frontmatter, directory, state dirs).
- CLI name: `porch` (`scripts/porch`, `scripts/porch.cmd`); remote-agents has no CLI of its own, its entry point is `scripts/host.sh`; coordinator-mode has one helper, `scripts/coordinator_mode.py`.
- Environment variables: `PORCH_*` for orchestration; `REMOTE_AGENTS_*` for the host skill; `COORDINATOR_MODE_HOME` for the coordinator's state root (default `~/.local/state/coordinator-mode`).
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

The coordinator helper has its own standard-library tests:

```bash
python3 skills/coordinator-mode/scripts/tests/test_coordinator_mode.py -v
```

## Versioning and releases

Owner's decision, 2026-10-30: use stable semver releases (for example, `2.1.0`).
Do not add beta or other prerelease suffixes unless the owner explicitly requests
a prerelease.

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
