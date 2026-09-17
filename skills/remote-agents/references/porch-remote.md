# porch on the remote host

`porch` (the `pragmatic-orchestration` skill's CLI) is the primary way to run
coding agents on a remote host: same binary, same delegate/steer/wait contract,
just executed over SSH. The remote host runs real provider CLIs; porch is a
dependency-free POSIX wrapper, so installation is a file copy.

## Install

```bash
scp -r skills/pragmatic-orchestration <alias>:/tmp/porch
ssh <alias> 'mkdir -p ~/.local/share/porch && cp -r /tmp/porch/* ~/.local/share/porch/ \
             && ln -sf ~/.local/share/porch/scripts/porch ~/.local/bin/porch && rm -rf /tmp/porch'
ssh <alias> 'porch --version'
```

Copy `config.example.json` to `~/.local/share/porch/config.json` (or set
`PORCH_CONFIG`) and keep only backends actually installed on that host —
`PORCH_BIN_CODEX`/`PORCH_BIN_GROK`/etc. point at the remote CLI paths.

## Agent CLIs on the host

Install each provider CLI with its official installer, then authenticate
interactively on the host: `ssh -t <alias> '<cli> login --device-auth'`
(`scripts/auth-grok.sh` wraps the Grok variant). Auth belongs to the remote
account profile — never copy local credential files or tokens to the host.

## Delegate remotely

```bash
ssh <alias> 'cd <workRoot>/workspaces/<task> && porch delegate -a codex --detach "Task…"'
ssh <alias> 'porch delegate list --active'
ssh <alias> 'porch delegate events run_<id> --max-events 50'
ssh <alias> 'porch delegate steer run_<id> --mode auto "Prefer approach B"'
ssh <alias> 'porch delegate wait run_<id> --timeout 300 --json'
ssh <alias> 'porch quota all'
```

- Always `--detach` (or wrap in tmux/a scheduled task) — an SSH drop must not
  kill the worker. `--detach` prints `run_id=…`; record it immediately.
- Give the worker a complete contract: objective, absolute remote `cwd`,
  context, allowed scope, constraints, validation commands, deliverables. The
  worker has full remote-user authority; "read-only" is an instruction, not a
  sandbox — verify the repo state yourself afterwards.
- Supervise: check within the first minute, then every 5–15 min via `events`;
  a heartbeat is not progress. Bounded `wait`; `steer` to redirect; `cancel`
  to stop.
- On Windows hosts run porch under pwsh via `porch.cmd`; paths are Windows
  paths (`C:\Work\workspaces\...`).

## Honesty and evidence

`porch events`/`wait` output is the worker's own report — not independent
evidence. Verify claims on the host: diff the workspace, run the tests again
yourself, fetch artifacts (scp/`fetch-qa-screenshots.py`). Close or cancel
finished/abandoned runs; do not leave detached supervisors idling.

## Quota

`ssh <alias> 'porch quota all'` reads provider-side remaining quota where the
CLI exposes it (Codex `account/rateLimits/read`, Grok `/usage`). Do not scrape
terminals for quota — use the structured command.
