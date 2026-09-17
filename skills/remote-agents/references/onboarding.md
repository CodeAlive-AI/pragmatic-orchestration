# Onboarding a remote agent host

End-to-end runbook: from zero to a verified remote agent host. Each phase has a
**done-when** gate — do not move on with a red phase. `scripts/onboard.py check`
mirrors these phases and prints PASS/FAIL/WARN/SKIP per item; run it after each
phase and at the end.

```bash
scripts/onboard.py [--host <id>] check
```

## Phase 0 — Decide and record

Decide: host OS (`linux`|`windows`), whether the host gets a desktop plane
(Windows GUI QA) and/or a bridge plane (SMB mount), subnet prefix for the
bridge, local ports for RDP/observer.

Create `config.json` (copy `config.example.json`) with the host entry — see
[configuration.md](configuration.md). Nothing else needs the file until the
host exists.

## Phase 1 — Dev machine

Per-OS prerequisites are listed in [local-platforms.md](local-platforms.md):
AWS CLI + session-manager-plugin, OpenSSH client, Python, WireGuard tools, a
FreeRDP client (Windows hosts), bash (Windows devs: Git Bash).

**Done-when:** `onboard.py check` shows no FAIL under `dev.*` except the
bridge/credential rows that depend on later phases.

## Phase 2 — Provision the VM

[provisioning.md](provisioning.md) — or `references/terraform/` for the Linux
shape. Invariants: security group with **no TCP ingress** (only the bridge UDP
port when bridging), SSM instance role, encrypted volume, termination
protection.

Then configure the SSH-over-SSM alias: `examples/ssh-config-ssm` — one
dedicated ed25519 key per host; install the public key on the host
(administrators_authorized_keys on Windows — see provisioning.md).

**Done-when:** `host.sh status` shows running + SSM connected, and
`ssh <alias> true` answers.

## Phase 3 — Host OS baseline

Windows: `bootstrap-windows.ps1` (toolchain), `apply-hardening.ps1` then
`audit-hardening.ps1` clean, work-root layout per
[work-storage.md](work-storage.md), pwsh 7 as OpenSSH `DefaultShell`.
Linux: packages (`git`, `tmux`, `python3`), dedicated agent user, work-root
layout, sshd hardening.

**Done-when:** `onboard.py check` passes `host.workRoot`, `host.layout`,
`host.sshd`, `host.pwsh-shell`.

## Phase 4 — Bridge (Windows hosts with `bridge` config)

1. Dev: `onboard.py init-keys` — writes `.bridge-state/<id>.conf` (private key,
   mode 600) and prints the dev public key.
2. Host (elevated, over SSH or console): `scripts/setup-host-bridge.ps1`
   with `-PeerPublicKey` — creates the `WireGuardTunnel$` service, a dedicated
   non-admin SMB account with a random password, the encrypted share, and the
   two scoped firewall rules. Copy it over with `scp` first.
3. Dev: save the printed `smbJson` as `.bridge-state/smb.json` (0600).
4. Dev: `onboard.py set-peer <hostPublicKey>` (printed by the ps1).
5. Optional: `sudo REMOTE_AGENTS_HOST=<id> python3 scripts/install-work-bridge-service.py`
   for the fixed passwordless service (macOS/Linux only — see
   [bridge.md](bridge.md)).

**Done-when:** `work-bridge.py connect` mounts/attaches and `status` reports
`smb_reachable: true`. On Windows the access path is the UNC
`\\<subnet>.1\<share>`; on macOS/Linux the mount directory.

## Phase 5 — Desktop (Windows hosts with `desktop` config)

1. Save the RDP credential once, manually: Windows App bookmark on macOS,
   `TERMSRV/<addr>` via mstsc "remember me" on Windows, or set
   `desktop.passwordCommand` on Linux — see
   [local-platforms.md](local-platforms.md).
2. Ensure `desktop.remotePython` points at a `pythonw.exe` with Pillow on the
   host.
3. `host.sh desktop-start` then `desktop-probe`; inspect the returned PNG.

**Done-when:** probe reports `ready: true` and the PNG shows a real desktop.

## Phase 6 — Agent execution

[porch-remote.md](porch-remote.md): install `porch` on the host, authenticate
the provider CLIs you will use (`auth-grok.sh` shows the device-auth pattern;
same flow for other CLIs), then smoke a detached run:

```bash
ssh <alias> 'porch delegate --detach --prompt "echo ok" --path <workRoot>/runs/smoke'
ssh <alias> 'porch delegate events <id>'   # or wait/list
```

Optional planes: managed-worker daemon ([managed-workers.md](managed-workers.md)),
Codex Remote ([remote-tui.md](remote-tui.md)).

**Done-when:** `onboard.py check` passes `host.porch`, `host.agents`, and a
detached run completes with `porch delegate events` showing a terminal state.

## Phase 7 — Acceptance evidence

Before declaring the host ready, collect into a dated `artifacts/` dir on the
host (or locally):

- `onboard.py check` full output with zero FAIL.
- A desktop probe PNG (desktop hosts).
- A completed `porch delegate` smoke run id + `events` tail.
- `work-bridge.py status` output showing mounted/reachable (bridge hosts).
- `audit-hardening.ps1` output (Windows) or equivalent baseline notes.

Then follow the session-end checklist in [work-storage.md](work-storage.md).
