# Stop/cleanup design audit

2026-10-29. This probe **reproduced a defect**, not a passing cancellation test.
The observed baseline is Porch `821eb2e` on macOS. No production fix, new cloud
resource, model request or container was part of this audit.

The exact sequence used by Codex cancellation is:

1. Create an isolated process group with an RPC-like parent and a descendant
   that ignores SIGTERM. Wait until the descendant installs its signal handler.
2. Call real `JsonRpcProcess.terminate()`; it terminates and reaps the parent.
3. Call real `kill_process_group(parent_pid)`; resolving the departed parent
   fails, and the helper returns without stopping the live descendant.
4. Observe the descendant's non-zombie process state. `result.json` records
   `leader_reaped: true` and `descendant_survived: true`.
5. In `finally`, send SIGKILL only to the fresh group created by this probe and
   wait for its parent. The probe explicitly refuses the caller's process group.

`reproduce.py` preserves the small diagnostic. It prints its current source
commit and observations; running it later does not overwrite the historical
result or imply that future code has the same defect. Use a managed Python:

```sh
uv run --no-project docs/benchmarks/2026-10-29-stop-cleanup-audit/reproduce.py
```

The diagnostic proves the parent-reaping gap. It does not qualify a cgroup/job
boundary, detached nested Porch workers, SIGKILL survival, Windows cancellation
or full VM/cloud cleanup. Existing process-tree tests only assert descendant
absence on Windows; their POSIX pass is not evidence those cases are covered.

The next implementation must preserve/control the actual execution boundary
before parent reaping, account for escaped/owned child jobs, surface cleanup
errors, and decouple terminal task outcome from proven workspace quiescence.
The provider-neutral obligations and acceptance cases are in
[`execution-lifecycle.md`](../../execution-lifecycle.md).
