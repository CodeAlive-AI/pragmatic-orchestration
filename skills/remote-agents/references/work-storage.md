# Work-root storage contract

The remote work root (`workRoot` in config, e.g. `C:\Work` or
`/home/<user>/agent-work`) has a **closed layout**: agents and operators never
create files directly in its root, and every artifact has a declared home.

## Layout

```
<workRoot>/
  workspaces/<task>/<worker>/   # per-task working checkouts; delete when landed/parked/abandoned
  runs/<yyyyMMdd>-<task>/       # one-shot output (logs, results); delete at session end
  artifacts/<topic>-<date>/     # evidence the user explicitly asked to keep
  state/                        # durable host state (tokens, keys, service state)
  desktop-helpers/              # probe/observer deployment (desktop.py)
  Temp/                         # scratch
  bootstrap.log, toolchain.json # provisioning records
```

Extend the allowlist in `Test-WorkStorage.ps1` (`-AllowedDirectories`,
`-AllowedFiles`) for project-specific roots rather than loosening the rule.

## Rules

- One-shot scripts, logs, screenshots → `runs/<yyyyMMdd>-<task>` or system
  temp; never the root and never the desktop.
- Kept evidence → `artifacts/<topic>-<date>` only when the user asked for it.
- Scheduled tasks and helper processes are registered, used, and removed —
  unregister yours when done.
- Secrets (`.bridge-state`, tokens, smb credentials) never live in the work
  root except the state dir where a tool explicitly puts them.

## Audit

`scripts/host.sh audit` streams `Test-WorkStorage.ps1` to the host: unknown
root entries, runs older than `-StaleRunHours` (default 24), forbidden paths,
and scheduled tasks whose target no longer exists. A clean audit is necessary
but not sufficient — it does not prove you cleaned up; the layout contract
does.

## Session-end checklist

1. Remove your `runs/` entries and finished `workspaces/<task>/<worker>` dirs.
2. Unregister your scheduled tasks (`Get-ScheduledTask | ? TaskName -like '<prefix>*'`).
3. `host.sh audit` clean; investigate leftovers rather than ignoring them.
4. `work-bridge.py unmount` before stopping the host.
