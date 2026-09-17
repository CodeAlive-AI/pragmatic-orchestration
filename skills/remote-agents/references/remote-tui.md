# Codex Remote control on an agent host

Use this only for interactive Codex threads that must appear in a paired
Remote client such as the ChatGPT phone app. Managed/batch work continues
through porch or the worker daemon and is not registered as a Remote thread.

## Invariants

- Phone visibility belongs to the paired app-server environment, not to tmux
  and not to files under `~/.codex/sessions`.
- `codex` and `codex exec` start independent local cores — invalid launch
  paths when phone visibility is required.
- The TUI must use the same managed Codex binary as the app-server daemon and
  connect with `--remote unix://` (or the deployment-approved endpoint).
- Keep the app-server on an owner-only Unix socket or loopback. Do not add
  public ingress, copy OAuth state, or print stored tokens.

## Setup and pairing

Verify the live CLI contract before relying on flags:

```bash
codex --version
codex remote-control --help
codex login status
codex remote-control start --json
```

If login is absent, run `codex login --device-auth` in a TTY and let the user
complete the device flow. Pair the phone with `codex remote-control pair`;
treat the code as short-lived and do not persist it. Record the non-secret
`environmentId` and require `status=connected`.

## Launch a phone-visible TUI

```bash
codex remote-control start --json
tmux new-session -d -s SESSION_NAME \
  '/absolute/path/to/managed/codex --remote unix:// -C /absolute/cwd'
tmux attach -t SESSION_NAME
```

Resolve any hook-review screen interactively before assuming an initial
prompt ran. Pass ordinary TUI options after `--remote unix://`.

## Registration gate

Before leaving a thread unattended, prove all of:

1. the app-server reports `connected` and the expected `environmentId`;
2. the TUI process command line contains `--remote unix://`;
3. `codex --remote unix:// resume` lists the new thread;
4. the paired client lists the same thread;
5. a bounded smoke prompt completes through it without a
   `thread ... not found` error.

Tmux presence proves only terminal persistence; a local rollout file proves
only local persistence. Neither proves app-server registration.

## Lifecycle and recovery

- Distinguish an active model turn, an idle registered thread, an idle TUI,
  and a dead shell. Only observed user/model activity is workload.
- Before host shutdown, inspect both worker daemons and Codex Remote
  processes — a worker's safe-stop guard cannot see TUI activity.
- After host restart, verify the remote-control service and relaunch the TUI
  with `--remote unix://`; do not resume with plain `codex resume`.
- A thread created by plain `codex` cannot be assumed remotely visible
  retroactively; start a registered replacement and carry over only
  user-approved context.
