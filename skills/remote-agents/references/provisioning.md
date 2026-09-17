# Provisioning and hardening

Provision a dedicated VM per trust domain. The pattern below is AWS/SSM-based
and battle-tested; other providers work if the same invariants hold (no public
ingress for control, authenticated transport, encrypted disk).

## Cloud shape

- EC2 in a private or default subnet; **security group with no inbound rules**
  (one UDP port only if the WireGuard work bridge is used).
- No public IP for pure SSM hosts; an ephemeral public IP is acceptable when
  the bridge needs a WireGuard endpoint — never add TCP ingress for it.
- Instance profile containing **only** `AmazonSSMManagedInstanceCore`, unless
  the workload itself needs narrowly scoped extra permissions.
- Encrypted gp3 root volume sized for builds (150 GiB is a tested floor).
- Termination protection on; the root volume deletes with the instance, so say
  that explicitly before any user-requested destruction.
- `references/terraform/` is a working module for the Linux variant
  (no-ingress SG, role, key pair, cloud-init bootstrap). Adapt for Windows
  with a Server base AMI; hardening below applies after first boot.

## Controller IAM (least privilege)

The controlling identity should be able to: describe this one instance,
start/stop/reboot it, open SSM SSH and port-forwarding sessions to it, and
send/cancel standard commands. It must **not** terminate instances or change
IAM/networking. `examples/controller-policy.example.json` is a template — fill
in your account/region/instance.

## SSH-over-SSM

See `bridge.md`. Invariants: dedicated ed25519 key per host, `IdentitiesOnly`,
`ControlMaster auto` + `ControlPersist 10m`, `ProxyCommand` runs
`AWS-StartSSHSession`. No inbound TCP 22 anywhere.

## Windows provisioning

1. Boot Windows Server (Full Base tested) with the instance profile above.
2. Enable OpenSSH Server, install the host's public key into
   `C:\ProgramData\ssh\administrators_authorized_keys`, set pwsh 7 as the
   OpenSSH `DefaultShell` once installed (an open ControlMaster keeps the old
   shell until it expires; SSM `AWS-RunPowerShellScript` stays on 5.1).
3. Run `scripts/bootstrap-windows.ps1` over SSH or SSM send-command:
   signature/checksum-verified installers for Visual Studio workloads
   (`-VsWorkloads`/`-VsComponents`), Git, Python, pwsh 7, 7-Zip; sets
   `NUGET_PACKAGES`/`DOTNET_CLI_HOME` under the work root so the 32-bit SSM
   profile and 64-bit MSBuild share caches; writes `<workRoot>/toolchain.json`
   and `bootstrap.log`.
4. Run `scripts/apply-hardening.ps1`: RDP enabled but reachable only through
   `127.0.0.1` (SSM loopback), WinRM/RemoteRegistry disabled, firewall blocks
   inbound by default, SMB1 off (LanmanServer disabled unless `-AllowWorkShare`),
   pubkey-only SSHD, locked-down `administrators_authorized_keys` ACL, password
   policy, process-creation/logon auditing.
5. Verify with `scripts/audit-hardening.ps1` (JSON report) — review it after
   any networking change. A service listening on `0.0.0.0:3389` is not
   exposure by itself; Windows Firewall and the security group both enforce
   the boundary.
6. Create the work-root layout (`work-storage.md`) and deploy
   `scripts/Start-Interactive.ps1`, `scripts/Start-VisualQa.ps1`,
   `scripts/run-visual-qa.py`, `scripts/desktop/{probe,observer}.py` to
   `<workRoot>/<helperDir>` (desktop.py deploys helpers on demand).
7. `repair-visual-studio-path.ps1` removes a VS instance registered at the
   broken `C:\Program` path if a bootstrap went wrong; `install-7zip.ps1` is a
   standalone checksummed installer.

## Linux provisioning

- Ubuntu LTS (the terraform module selects Canonical 24.04 amd64).
- `AmazonSSMManagedInstanceCore`, no SG ingress, no public IP.
- `ssh_public_key` installed for SSH-over-SSM; user `ubuntu` or a dedicated
  agent account.
- `loginctl enable-linger` + a systemd **user** service for anything that must
  survive SSH disconnects (durable worker daemons, porch runs).
- Toolchain via cloud-init or your repo's own scripts; keep agent CLI auth
  interactive (`<cli> login --device-auth` through `ssh -t`) — never copy local
  credential files.

## Acceptance checklist

- `host.sh status` shows `running` + SSM `connected`.
- `sshd -t` clean; `audit-hardening.ps1` report matches intent.
- `toolchain.json` written; work-root layout exists and `host.sh audit` clean.
- A bounded smoke prompt on the host completes and its artifacts verify.
