# Developer-machine OS support

`remote-agents` runs on macOS, Linux, and Windows 11 developer machines. All
platform knowledge lives in `scripts/lib/devlocal.py`; the rest of the skill is
OS-agnostic. `scripts/lib/devlocal.DEV_OS` is `macos`, `linux`, or `windows`.

## Prerequisites per dev OS

| | macOS | Linux | Windows 11 |
|---|---|---|---|
| Shell | any | any | **Git Bash** (or WSL) — `host.sh` is bash |
| Python | `python3` | `python3` | `python` (host.sh auto-detects `python3`→`python`→`py`) |
| AWS CLI + session-manager-plugin | yes | yes | yes |
| ssh/scp | built in | built in | OpenSSH Client (Windows feature) |
| WireGuard | brew `wireguard-tools` (`wg`, `wireguard-go`) | `wireguard-tools` (`wg-quick`) | WireGuard for Windows (`wireguard.exe`) |
| RDP holder | `sfreerdp` (brew freerdp) | `sfreerdp3`/`sdl-freerdp`/`xfreerdp` | `wfreerdp` |
| Interactive RDP | Windows App / MS Remote Desktop | remmina/xfreerdp/krdc — or `desktop.openCommand` | `mstsc` / Windows App |
| SMB mount | `mount_smbfs` (no sudo) | `sudo mount -t cifs` | `net use \\ip\share` (UNC, no mountpoint) |

## Headless RDP session holder

`desktop-start` launches a FreeRDP client that holds the remote console
session for QA. Per-OS details:

- **macOS** — `sfreerdp`, truly headless, no window.
- **Linux** — SDL/X11 client with `SDL_VIDEODRIVER=dummy` set automatically;
  no window appears even though the client is an SDL app.
- **Windows** — `wfreerdp` opens a real window holding the session. Minimize
  it; do not close it during UI runs. Set `desktop.clientBinary` if wfreerdp
  is not on PATH.

## Saved RDP credential

`desktop-start` reads the operator-authorized saved credential — never argv,
env, logs, or files:

- **macOS** — the Windows App bookmark (`desktop.rdpBookmark`, address
  `127.0.0.1:<localPort>`) → its Keychain item. macOS may prompt once.
- **Windows** — Credential Manager generic credential `TERMSRV/<addr>`,
  exactly what mstsc/Windows App save when you tick "remember me". Override
  the target with `desktop.credTarget` if your client stores it differently.
- **Linux** — no standard store; set `desktop.passwordCommand`, e.g.
  `secret-tool lookup service remote-agents host myhost` or
  `pass show remote-agents/myhost`.
- Any OS escape hatches: `desktop.passwordCommand` (shell command printing
  the password) or `desktop.passwordEnv` (environment variable name).

## Work bridge per OS

- **macOS** — `sudo wg-quick up` per connect, or the installed launchd
  service (passwordless restart via sudoers).
- **Linux** — same `wg-quick` path; the installer writes a systemd oneshot
  unit (`bridge.serviceName`, default `remote-agents-work-bridge-<id>`) plus
  sudoers for `systemctl restart|stop`. SMB mount needs sudo for
  `mount -t cifs`/`umount` (credential passed via a 0600 file, not argv).
- **Windows** — no fixed service: `connect` renders the conf and runs
  `wireguard /installtunnelservice` elevated (one UAC prompt per connect).
  The share attaches via `cmdkey` + `net use`; `disconnect` removes the
  `cmdkey` credential only if the bridge created it (marker in `.bridge-state`).

## Known Windows dev-machine constraints

- `host.sh` needs Git Bash; under plain PowerShell call
  `python scripts/desktop.py …` and `python scripts/work-bridge.py …`
  directly — they do not depend on bash except `desktop.py`'s internal
  `desktop-tunnel` invocation, which resolves `bash` (Git for Windows puts
  it on PATH).
- The interactive-viewer URL, probe PNGs, and scheduled-task flow are
  unchanged — they run on the *remote* host.
- `nc` is not required anywhere; port checks use raw sockets.
