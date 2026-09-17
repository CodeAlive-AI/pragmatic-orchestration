# Troubleshooting

Verify layers in order; fix the lowest broken one.

## 1. Config / selection

```bash
scripts/lib/config.py resolve            # which host would be used?
scripts/lib/config.py hosts              # defined hosts
scripts/lib/config.py config-path        # which file is live?
```

`no config at ...` → copy `config.example.json` to `config.json` and fill it.
`unknown host` → the id isn't in `hosts`; check spelling/`defaultHost`.

## 2. Transport

```bash
ssh -o BatchMode=yes <alias> 'true'                       # SSH-over-SSM
scripts/host.sh status                                    # EC2 + SSM agent
aws --profile <p> --region <r> ec2 describe-instances ... # power state
```

- SSM `NotConnected`: instance stopped, agent down, or role missing
  `AmazonSSMManagedInstanceCore`. `host.sh start`, then `wait` on SSM.
- SSH hangs on `ProxyCommand`: Session Manager plugin/AWS CLI auth — test
  `aws ssm start-session --target <id>` directly.
- Stale ControlMaster: `ssh -O exit <alias>` or remove `~/.ssh/cm-*`.

## 3. Host health (Windows)

```bash
ssh <alias> 'powershell -NoProfile -Command "Get-Service sshd"'
```

SSH dead but SSM alive → `ssm send-command` recovery (`windows-host.md`).
SSM dead → check instance state, wait for boot. Never open 22/3389 to the
internet as a fix.

## 4. Desktop

`desktop-status` distinguishes transport (`transport_running`) from GUI
readiness. Sequence:

1. `tunnel_listening` false → `host.sh desktop-tunnel` died; check the runtime
   log in `$TMPDIR/remote-agents-desktop-<host>/`.
2. `auth_error` set → credential rejected; verify the saved bookmark
   credential, then `desktop-clear-auth` once verified.
3. `connection_failed` → inspect `rdp.log` before `stop`/`start`; no blind
   retry.
4. Probe fails → no unlocked interactive session (locked, signed out, or
   session 0). The fix is session state, not more probes.
5. Observer 403 → wrong/expired token; 503 → desktop not ready; nothing →
   check the remote scheduled task and the viewer tunnel.

## 5. Work bridge

- `work-bridge.py status`: `smb_reachable` false → tunnel down or endpoint
  stale (public IP changes on stop/start — `connect` re-resolves).
- Mount fails after tunnel up → SMB credential in `.bridge-state/smb.json`,
  share name, or the tunnel-scoped 445 rule.
- Service mode: check `/var/log/<label>.log` and that the `endpoint` file
  holds a bare IPv4 address.
- `wg-quick` up fails → another utun holds the name; `disconnect` first.

## 6. Workers (porch / daemon)

```bash
ssh <alias> 'porch delegate list --active'
ssh <alias> 'systemctl --user status <daemon> --no-pager'
```

- `porch` not found → not installed on the host (`porch-remote.md`).
- Detached run lost its id → `delegate list --active`; journal under porch's
  state dir.
- Daemon unreachable → `systemctl --user restart <daemon>`; journalctl for
  the cause. Live sessions become `interrupted` — say so, don't claim
  continuity.
- Auth failures on the host → `ssh -t <alias> '<cli> login --device-auth'`
  as the same account that runs the workload.

## 7. Storage audit

`host.sh audit` findings map to `work-storage.md`: unknown root entries,
stale `runs/`, forbidden paths, orphan scheduled tasks. Clean the specific
items; never bulk-delete the work root.
