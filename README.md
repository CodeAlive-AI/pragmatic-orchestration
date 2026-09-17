# pragmatic-orchestration

Use other coding agents as subagents — even when your primary agent does not support their models.

`pragmatic-orchestration` is a cross-agent skill (agentskills.io layout) that lets any coding agent which can run skills call a different coding-agent CLI to research, review, or implement work — via the `porch` CLI:

- **review** — independent opinions or multi-depth code review from several model families at once (read-only).
- **delegate** — hand a task to one exact agent: steerable by default, detachable, with `wait` / `watch` / `events` / `list` to supervise long-running work (full-access runtime).
- **sessions** — search and navigate local coding-agent session histories.
- **quota** — read remaining Codex / Grok subscription quota.

Supported worker harnesses: Codex CLI, Claude Code, OpenCode, Grok Build, Devin CLI, and Gemini CLI (review only).

The repo also ships **`remote-agents`**: a companion skill for operating dedicated remote Linux/Windows hosts that run these agents — secure VM/OS provisioning, SSH-over-SSM access with no public ingress, the WireGuard/SMB work bridge, `porch` on the remote host, a headless Windows desktop, bounded visual QA, durable worker daemons, and Codex Remote threads. See [skills/remote-agents/SKILL.md](skills/remote-agents/SKILL.md).

Full documentation: [skills/pragmatic-orchestration/README.md](skills/pragmatic-orchestration/README.md) and [SKILL.md](skills/pragmatic-orchestration/SKILL.md).

## Install

Any agent supporting the Agent Skills standard:

```bash
npx skills add CodeAlive-AI/pragmatic-orchestration@pragmatic-orchestration -g -y
```

As a Claude Code plugin:

```bash
claude plugin marketplace add CodeAlive-AI/pragmatic-orchestration
claude plugin install pragmatic-orchestration@pragmatic-orchestration
```

You also need Python 3 and at least one supported coding-agent CLI installed and authenticated.

Configuration: copy `skills/pragmatic-orchestration/config.example.json` to `config.json` in the same directory (gitignored, per-user) or point `PORCH_CONFIG` at your own file. Without `config.json`, the shipped example defaults are used. `remote-agents` follows the same pattern (`REMOTE_AGENTS_CONFIG` / `config.json`).

## Platform support

Linux and macOS are covered by the full offline regression suite (957 checks). Windows 11 with Git Bash/MSYS2 and native Python 3.11+ has experimental, community-supported compatibility — `porch.cmd` runs `sessions` and `quota` natively; `review` and `delegate` run from Git Bash. CI covers ubuntu/macos/windows.

## Repository layout

```
pragmatic-orchestration/
├── .claude-plugin/            ← single-plugin marketplace (source: "./")
├── skills/
│   ├── pragmatic-orchestration/
│   │   ├── SKILL.md           ← agent-facing instructions
│   │   ├── README.md          ← human-facing docs
│   │   ├── scripts/porch      ← the CLI (+ porch.cmd for Windows)
│   │   ├── references/        ← per-mode deep docs
│   │   ├── prompts/           ← review/specialist prompt templates
│   │   └── config.example.json
│   └── remote-agents/         ← dedicated remote agent hosts (Linux/Windows)
│       ├── SKILL.md           ← agent-facing instructions
│       ├── scripts/           ← host.sh, desktop.py, work-bridge.py, Windows helpers
│       ├── references/        ← provisioning, bridge, porch-remote, security…
│       └── config.example.json
└── .github/workflows/         ← offline regression CI
```

## History

Extracted from [CodeAlive-AI/ai-driven-development](https://github.com/CodeAlive-AI/ai-driven-development) (`skills/agents-consilium`) via `git filter-repo`; commit history preserved (SHAs rewritten). Earlier pre-consolidation history lives in the archived `CodeAlive-AI/awesome-agent-skills` repository.

## License

MIT — see [LICENSE](LICENSE).
