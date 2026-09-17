# Linux host operations

A Linux agent host is a dedicated VM reached only through SSH-over-SSM. Long
work runs under the host's own supervision, never under your SSH session.

## Baseline

- Ubuntu LTS, `AmazonSSMManagedInstanceCore`, no SG ingress, no public IP
  (see `provisioning.md`; `references/terraform/` is a working module).
- Agent CLIs installed and authenticated as the host user (device auth over
  `ssh -t`); `porch` installed per `porch-remote.md`.
- `loginctl enable-linger <user>` so systemd **user** services and tmux survive
  logout.

## Durability ladder

| Need | Mechanism |
|---|---|
| Command outlives the SSH call | `porch delegate --detach` (own session leader, `wait` reattaches) |
| Interactive/long-lived process | tmux session on the host (`tmux new -d -s name 'cmd'`) |
| Must survive reboots | systemd user unit (`~/.config/systemd/user/`), `systemctl --user enable` |
| Fleet of durable workers | optional managed-worker daemon — see `managed-workers.md` |

Verify a detached thing the same way regardless of launcher: process alive
(`systemctl --user status` / `tmux ls`), journal or event log progressing,
expected output files appearing.

## Recovery order

1. `ssh <alias> 'true'` — transport.
2. `host.sh status` — instance + SSM agent.
3. `systemctl --user status <unit>` / `tmux ls` — workload supervision.
4. `journalctl --user -u <unit> -n 200 --no-pager` — cause.
5. Only then restart/relaunch. A daemon/VM restart makes live sessions
   `interrupted`; recover state from the journal and start a replacement —
   never claim continuity you did not verify.

## Cost control

Stop the host when no agent uses it; EBS keeps billing, compute does not.
`host.sh stop` after confirming no active runs (`porch delegate list --active`,
tmux, systemd user services). Enable termination protection; never terminate
on a broad tag match.
