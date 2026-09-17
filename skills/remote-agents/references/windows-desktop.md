# Native headless Windows desktop from macOS

## Architecture

One native `sfreerdp` process holds the interactive desktop through the SSM
loopback RDP tunnel (`desktop-tunnel` → `127.0.0.1:<localPort>` → host 3389).
QA workers and the UI driver run inside Windows. There is no Docker, Xvfb,
VNC, extra Windows account, RDS role, autologon, or lock-policy change. (This
is a technical description, not a licensing determination.)

The optional read-only observer is a localhost HTTP server on Windows
(`observer.py`, binds 127.0.0.1:`<viewerRemotePort>`), forwarded over SSH to
Mac loopback `:<viewerLocalPort>`. It captures ~1 fps only while a page is
open; page and frames require a fresh random token; it has no input or
execution endpoints and expires after one hour. Use the QA driver's exact
PNGs — not observer JPEGs — for pixel-level evidence. Do not open a competing
RDP client during a UI run.

## Workflow

```bash
scripts/host.sh status
scripts/host.sh start                 # only if stopped
scripts/host.sh desktop-start         # tunnel + sfreerdp + interactive probe
scripts/host.sh desktop-status
scripts/host.sh desktop-probe         # fresh probe; view the PNG before trusting it
scripts/host.sh desktop-viewer        # prints the observation URL
scripts/host.sh desktop-viewer-stop   # leaves RDP running
scripts/host.sh desktop-stop          # disconnects; does NOT log off Windows
scripts/host.sh stop                  # only when nobody else uses the host
```

The Mac needs FreeRDP's `sfreerdp` (Homebrew `freerdp`) and Python 3. Do not
install XQuartz/x11vnc for this workflow.

`desktop-start` reuses an owned live connection and always runs a fresh probe.
The probe (`desktop/probe.py`) runs via a temporary interactive scheduled task
and checks input desktop `Default`, nonzero session id, and a usable
screenshot. `desktop-status` distinguishes transport from GUI readiness — a
live RDP process does not prove an unlocked desktop.

## Credential handling

`desktop.py` resolves only the configured Windows App bookmark
(`desktop.rdpBookmark`) at `127.0.0.1:<localPort>` for `desktop.windowsUser`,
reads its Keychain item once per start, and pipes it to sfreerdp through
stdin in memory — never argv, env, logs, or a file. The RDP certificate
SHA256 is fetched over authenticated SSH and pinned (`/cert:fingerprint:`);
NLA stays on. If the exact item is missing or auth fails, report the error —
do not switch to manual entry or another credential. macOS may prompt to
approve Keychain access.

## Lifecycle and failure handling

- One UI worker per desktop; extra observers do not create isolated slots.
- No automatic login retry. Auth failure is recorded in the runtime log and
  survives stop; only after the operator verifies the credential may
  `desktop-clear-auth` acknowledge it and permit one new start.
- No mouse jiggler or auto-unlock. A lock means stop UI work and consider a
  controlled reconnect. Viewer closed ≠ Windows locked ≠ RDP disconnected —
  each needs its own evidence.
- Runtime state (pid+identity records, logs, captures) lives under
  `$TMPDIR/remote-agents-desktop-<host>/` (0700). Cleanup verifies pid, start
  time, and command before killing. Operations serialize on a file lock.
- Helper scripts deploy to `<workRoot>/<helperDir>` on demand; probe tasks are
  unregistered after use.
- Viewer/probe SSH connections disable multiplexing — shared ControlMaster
  sockets intermittently dropped commands on the reference deployment.
- Observation URLs are bearer tokens; do not publish or commit them.
- The desktop depends on the Mac's SSM session; survival through Mac
  sleep/reboot/network loss is not promised.
