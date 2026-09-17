---
name: remote-agents
description: Operate dedicated remote Linux/Windows hosts that run coding agents. Use for provisioning and hardening an agent VM, SSH-over-SSM access with no public ingress, host power lifecycle, the WireGuard/SMB work bridge, running agents remotely through porch, a headless Windows desktop with read-only observation, bounded visual QA, durable managed-worker daemons, phone-visible Codex Remote threads, storage audits, and cleanup evidence. Not for running agents on the local machine — use pragmatic-orchestration directly for that.
---

# Remote Agents

You are the local manager of dedicated remote hosts on which coding agents run.
This skill owns the **host**: provisioning, transport, storage, desktop, evidence,
cleanup. Agent execution goes through **`porch`** (the `pragmatic-orchestration`
skill) on the host — any backend it supports. Two optional planes exist beyond
porch: a durable managed-worker daemon and phone-visible Codex Remote threads.
Never present one plane as another.

## Start here

All lifecycle commands run through `scripts/host.sh` from this skill directory;
host identity comes from `config.json` (copy `config.example.json`, or set
`REMOTE_AGENTS_CONFIG`). Select a host with `--host <id>`,
`REMOTE_AGENTS_HOST`, or `defaultHost`.

```bash
./scripts/host.sh status          # instance state + SSM reachability
./scripts/host.sh start|stop|restart
./scripts/host.sh ssh             # interactive SSH-over-SSM
./scripts/host.sh run '<cmd>'     # one-shot remote command
./scripts/host.sh audit           # work-root layout audit (Windows)
./scripts/host.sh desktop-start|desktop-status|desktop-probe|desktop-stop  # Windows GUI
./scripts/host.sh desktop-viewer|desktop-viewer-stop|desktop-clear-auth
./scripts/host.sh desktop-tunnel|desktop-open                              # manual Windows App path
./scripts/work-bridge.py connect|status|mount|unmount|disconnect           # SMB work mount
```

SSH travels inside AWS Systems Manager Session Manager; the only permitted
inbound rule is the WireGuard UDP port for the work bridge. Never open TCP 22,
3389, or 445 to the internet.

## Route by task

| Need | Path |
|---|---|
| Provision/harden a new host | [provisioning.md](references/provisioning.md) (+ `references/terraform/`, `examples/`) |
| Configure host identity | [configuration.md](references/configuration.md) |
| SSH config, file transfer, SMB mount | [bridge.md](references/bridge.md) |
| Run agents on the host | [porch-remote.md](references/porch-remote.md) |
| Linux host operations | [linux-host.md](references/linux-host.md) |
| Windows host inventory/recovery | [windows-host.md](references/windows-host.md) |
| Headless interactive desktop | [windows-desktop.md](references/windows-desktop.md) |
| GUI/visual QA runs | [visual-qa.md](references/visual-qa.md) |
| Work-root layout and audit | [work-storage.md](references/work-storage.md) |
| Durable worker fleet | [managed-workers.md](references/managed-workers.md) |
| Phone-visible Codex threads | [remote-tui.md](references/remote-tui.md) |
| Threat model | [security.md](references/security.md) |
| Failures | [troubleshooting.md](references/troubleshooting.md) |

## Operating contract

Always:

1. Resolve the host (`config.py resolve`) before acting; never guess ids.
2. `host.sh status` first; start a stopped host and wait for SSM.
3. Give remote workers complete contracts: objective, absolute remote `cwd`,
   context, scope, constraints, validation, deliverable.
4. Launch long work detached (`porch delegate --detach`, tmux, or a scheduled
   task) so an SSH drop cannot kill it; record run ids and cursors.
5. Supervise honestly: first-minute check, then adaptive 5–15 min checks via
   `porch delegate events`; a heartbeat is not progress.
6. Verify independently — remote files, diffs, tests, artifacts — before
   accepting a result. A worker's report is a claim, not evidence.
7. Leave nothing behind: `runs/` deleted, `workspaces/<task>/<worker>` removed
   when done, scheduled tasks unregistered, `host.sh audit` clean, bridge
   unmounted before `stop`.

## Security invariants

- No public ingress for control; SSM only. Bridge = one UDP port + tunnel-scoped SMB.
- Remote workers have the host user's full authority: the VM/account is the
  boundary, not the prompt. Dedicated instance per trust domain.
- Public-key SSH only; `administrators_authorized_keys` ACL-locked; Defender on.
- Secrets never in prompts, bundles, or the repo; `.bridge-state/` stays local
  (0700) and is never committed.
- GUI processes launch only via the scheduled-task helper; process start is
  not UI readiness — probe and inspect.
- Details and rationale: [security.md](references/security.md).

## Scripts map

`host.sh` lifecycle/transport · `lib/config.py` host resolution ·
`desktop.py` + `desktop/{probe,observer}.py` headless desktop ·
`work-bridge.py` / `work-bridge.command` / `install-work-bridge-service.py`
SMB bridge · `Start-Interactive.ps1` GUI launcher · `Start-VisualQa.ps1` +
`run-visual-qa.py` bounded QA worker · `verify-qa-vision.py` +
`validate_qa_vision.py` driver smoke test · `fetch-qa-screenshots.py` evidence
· `bootstrap-windows.ps1` toolchain · `apply-hardening.ps1` /
`audit-hardening.ps1` baseline + report · `Test-WorkStorage.ps1` layout audit
· `install-7zip.ps1`, `repair-visual-studio-path.ps1` recovery helpers ·
`auth-grok.sh` device auth.
