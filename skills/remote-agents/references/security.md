# Security model for unrestricted remote workers

## What full-access means

A remote agent runs as the VM/host user with sandbox disabled and approvals
auto-accepted. It can read/write everything that account touches, execute
commands, use network egress, and invoke any credential in its environment.
**The containment boundary is the VM/account, not the prompt.**

## Required infrastructure controls

- Dedicated instance (or at least a dedicated OS account) per trust domain.
- Minimum IAM role — `AmazonSSMManagedInstanceCore` only unless the workload
  needs narrowly scoped extras.
- No production cloud credentials, signing keys, password stores, or
  unrelated repositories on the host.
- Private subnet / no-ingress security group; SSH-over-SSM only. The single
  permitted exception is the WireGuard UDP port for the work bridge.
- Egress restricted to what the work needs (model APIs, package registries,
  git hosts, DNS, NTP).
- Encrypted disk, snapshots/backups, repository remotes for durable work.
- Rotate/rebuild worker instances rather than treating them as trusted pets.
- Review provider data controls before placing private source on the host.

## Control-plane protection

- The manager reaches the host only through authenticated OpenSSH-over-SSM.
- No TCP control endpoint; a daemon relay binds a 0600 Unix socket.
- State/config dirs 0700; state/token files 0600.
- Callback/viewer tokens are per-run, random, and never returned through
  manager-visible channels.
- Worker-facing callback surfaces are never registered on the manager side.
- Subscription tokens stay in the official CLI's storage on the host.

These do not defend against another process running as the same OS user —
separate accounts/VMs for mutually untrusted workloads.

## Prompt injection and untrusted content

Repository files can contain instructions aimed at coding agents. Treat
worker conclusions as untrusted evidence: independently verify diffs,
commands, tests, and external actions. Agreement between two models is not
validation.

## Destructive external actions

"Request approval before irreversible actions" in a worker prompt is a
coordination convention, not enforcement. Enforcement is IAM scope, network
policy, protected branches, deployment approvals, and separate credentials.

## Secrets

Never pass secrets in task text, prompts, or callback payloads. Keep
production `.env`, SSH private keys, browser profiles, and cloud admin
credentials off the host. Log redaction cannot guarantee a tool never read a
secret. `.bridge-state` (wg private key, SMB credential) lives only in the
gitignored skill dir, mode 0700.
