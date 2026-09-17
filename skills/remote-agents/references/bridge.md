# Bridge: secure dev↔host connectivity

Two layers, strictly separated:

1. **Control plane** — OpenSSH over AWS SSM (`AWS-StartSSHSession` via
   `ProxyCommand`). No public TCP ingress anywhere.
2. **Data plane (optional, Windows)** — a dedicated WireGuard tunnel carrying
   an SMB mount of the remote work root. Scoped to two tunnel IPs only.

## SSH-over-SSM (control)

`examples/ssh-config-ssm` is the template:

```sshconfig
Host agent-linux
  HostName i-0123456789abcdef0
  User ubuntu
  IdentityFile ~/.ssh/agent-linux_ed25519
  IdentitiesOnly yes
  ProxyCommand sh -c "aws ssm start-session --target %h --document-name AWS-StartSSHSession --parameters 'portNumber=%p' --region us-east-1"
  ServerAliveInterval 30
  ServerAliveCountMax 3
  ControlMaster auto
  ControlPath ~/.ssh/cm-%C
  ControlPersist 10m
```

One dedicated key per host. `ControlMaster` amortizes SSM session setup across
many short SSH calls. `scp`/`ssh` through the alias cover file transfer; prefer
archives + sha256 verification for payloads, never mirror `.git`/`.jj` or
overwrite another worker's tree, and never run a `--delete` sync against the
host. For a durable private git channel, `git bundle` files transferred over
the alias are explicit and auditable.

## WireGuard + SMB work bridge (Windows)

Purpose: mount the remote work root on the Mac for file inspection and direct
artifact reads, without exposing SMB to the internet.

Architecture (all values configurable via `bridge.*`):

- WireGuard tunnel on a dedicated /32 pair (`<subnetPrefix>.1` Windows,
  `.2` Mac), UDP `<listenPort>` as the **only** security-group ingress.
- Windows exports the work root as SMB share `<shareName>` bound to the tunnel
  IP, served to a **dedicated local SMB account** (not Administrator), SMB2/3
  with encryption required. Admin shares stay unreachable — the firewall rule
  allows 445 only on the tunnel local address.
- Mac mounts `//<account>@<subnetPrefix>.1/<shareName>` at
  `<mountRoot>/<host-id>` (default `~/.remote-agents/<id>`).

### State and secrets

`.bridge-state/` inside the skill dir (gitignored) holds the wg config
(`<host>.conf` — private key inside), `smb.json` (the dedicated account
credential consumed only by this helper), and `system-service.json` when the
launchd service is installed. Mode 0700/0600. Never commit, print, or transfer
it; it is the only place these credentials exist.

### Commands

```bash
scripts/work-bridge.py connect      # resolve public endpoint, up tunnel, mount
scripts/work-bridge.py mount        # mount only (tunnel already up)
scripts/work-bridge.py status       # smb reachability + mount state
scripts/work-bridge.py unmount
scripts/work-bridge.py disconnect   # umount + tunnel down
scripts/work-bridge.py path         # print the mount path
```

`work-bridge.command` is a double-clickable wrapper for interactive use.
`install-work-bridge-service.py` (run once with sudo) installs a fixed
launchd service — pass host selection explicitly since plain `sudo` drops
the user environment:

```bash
sudo REMOTE_AGENTS_HOST=<id> python3 scripts/install-work-bridge-service.py
```

The service is fixed: pinned binaries copied into `appSupportDir`, a locked
`wg.conf`, an `endpoint` file as the *only* user-writable input (validated as
IPv4, never executed), and a sudoers rule allowing only
`launchctl kickstart -k`/`kill SIGTERM` on the service label — so routine
connect/disconnect never prompts for a password. It refuses unexpected tunnel
configs and never touches other VPNs.

The Mac mount must be a real empty local directory outside any repository;
the helper refuses mounts over nonempty or symlinked paths.

### Rules

- The bridge is for the work root only — do not share profile dirs, credential
  stores, or other drives.
- Endpoint is re-resolved from AWS on every `connect` (public IP changes on
  stop/start); a stale endpoint is rewritten, never appended.
- `unmount` before `host.sh stop`; a mounted share on a stopped host wedges
  Finder and IO.
