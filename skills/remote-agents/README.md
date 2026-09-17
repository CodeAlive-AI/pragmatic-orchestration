# remote-agents

Operate dedicated remote Linux/Windows hosts that run coding agents — securely,
observably, and with nothing left behind.

The skill owns the **host layer**: provisioning and hardening, SSH-over-SSM
transport with no public ingress, a WireGuard/SMB work bridge, a headless
Windows desktop for GUI QA, storage audits, and cleanup. Agents themselves run
through **`porch`** (the sibling `pragmatic-orchestration` skill) on the host.

## What it covers

- **Provisioning/hardening** — no-ingress security groups, SSM-only access,
  least-privilege controller IAM, signature-verified toolchain bootstrap,
  `apply-hardening.ps1`/`audit-hardening.ps1`, a Terraform module for the
  Linux variant.
- **Bridge** — SSH-over-SSM control channel; optional WireGuard + SMB mount of
  the remote work root with a dedicated credential and an optional
  password-free tunnel service (launchd on macOS, systemd on Linux; on
  Windows the WireGuard tunnel service manages itself).
- **porch on the host** — install, authenticate provider CLIs with device
  auth, `delegate --detach` over SSH, supervise via `events`/`wait`, `quota`.
- **Windows desktop** — headless FreeRDP session through an SSM loopback
  tunnel, saved OS credential (Keychain / Credential Manager /
  `passwordCommand`), pinned RDP certificate, read-only token observer,
  interactive-session probes.
- **Visual QA** — bounded scheduled-task worker (`Start-VisualQa.ps1` +
  `run-visual-qa.py`), external pywinauto/UIA MCP driver, PNG evidence via
  `fetch-qa-screenshots.py`.
- **Managed workers** — the durable daemon plane for fleets that must survive
  disconnects (documented pattern; daemon package itself is separate).
- **Codex Remote** — phone-visible TUI threads via `--remote unix://` with a
  registration gate before leaving one unattended.

Runs from macOS, Linux, or Windows 11 (Git Bash) dev machines — see
[references/local-platforms.md](references/local-platforms.md).

## Setup

```bash
cd skills/remote-agents
cp config.example.json config.json   # fill in your hosts
scripts/lib/config.py hosts          # sanity check
scripts/host.sh status
```

Full contract and routing: [SKILL.md](SKILL.md) · per-topic depth in
[references/](references/).
