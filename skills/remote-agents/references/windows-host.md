# Windows host operations

A Windows agent host is a dedicated Windows Server VM: SSH-over-SSM for
control, an optional loopback RDP desktop for GUI work, an optional
WireGuard/SMB bridge for file access. All identifiers live in `config.json`.

## Host inventory (fill in per deployment)

| Item | Where |
|---|---|
| Region / AZ, instance id, type | `config.json` `aws.*` |
| OS baseline | Windows Server Full Base (2025 tested) |
| Root disk | encrypted gp3, delete-on-termination |
| Security group | no inbound rules; UDP `<listenPort>` only if bridge used |
| Instance role | `AmazonSSMManagedInstanceCore` only |
| Controller identity | least-privilege IAM user/role (see `examples/controller-policy.example.json`) |
| SSH alias | `sshAlias` in config |
| Work root | `workRoot` (e.g. `C:\Work`) — closed layout, `work-storage.md` |

## Health and access

```bash
scripts/host.sh status
ssh -o BatchMode=yes <alias> 'hostname; whoami; Get-Service sshd'
ssh <alias> '& "$env:WINDIR\System32\OpenSSH\sshd.exe" -t; exit $LASTEXITCODE'
```

Expected: EC2 `running`, SSM `connected`, `sshd` running, `sshd -t` exit 0.

## Recovery through SSM

Use only when SSH is unavailable:

```bash
aws --profile <profile> --region <region> ssm send-command \
  --instance-ids <id> \
  --document-name AWS-RunPowerShellScript \
  --parameters 'commands=["Get-Service sshd","Restart-Service sshd","& $env:WINDIR\\System32\\OpenSSH\\sshd.exe -t"]'
```

Retrieve output via `ssm get-command-invocation` with the returned command id.
If SSM itself is offline, check instance state and wait for boot — never open
SSH/RDP to the internet as a "fix".

## Toolchain

`bootstrap-windows.ps1` installs and verifies VS workloads, Git, Python 3.13,
pwsh 7, 7-Zip — all signature/checksum-verified — and writes
`<workRoot>/toolchain.json` + `bootstrap.log`. Set pwsh 7 as the OpenSSH
`DefaultShell` (`HKLM:\SOFTWARE\OpenSSH`) so `ssh` and `host.sh run` execute in
pwsh; SSM `AWS-RunPowerShellScript` remains 5.1. `NUGET_PACKAGES` and
`DOTNET_CLI_HOME` must be machine variables under the work root so the 32-bit
SSM profile and 64-bit MSBuild share caches.

## RDP security invariants

NLA required, TLS security layer, high encryption, the built-in Remote Desktop
firewall group disabled, and exactly one loopback rule allowing
`RemoteAddress=127.0.0.1` on 3389. Group ingress permits only the bridge UDP
port — never TCP 3389/445/22. `audit-hardening.ps1` reports all of it; a
listener on `0.0.0.0:3389` is contained by both firewall layers, verify both
after any change.

## Interactive vs headless boundary

GUI automation and the app under test must share one logged-in interactive
desktop session. Anything launched from a plain SSH shell is headless — use
`Start-Interactive.ps1` (Task Scheduler, `Interactive` logon) for GUI
processes, and verify `pid`+`session_id` survive after the launcher exits.

## Cost control and destruction

Stop when unused; resize only after checking current prices/quota (burstable
classes exhaust credits under sustained builds). Termination protection stays
on; before destruction identify the exact instance+volume, preserve work, and
state that the root volume deletes with it.
