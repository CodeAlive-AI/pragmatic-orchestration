# Cancellation repair and cleanup evidence

2026-10-30 (owner's Asia/Makassar date). No models, cloud provisioning or containers.

The original `2026-10-29-stop-cleanup-audit/reproduce.py` reported a surviving
descendant on baseline `821eb2e`. With the repaired working-tree source on parent
`05a86a4`, the same real-process probe reports `leader_reaped: true` and
`descendant_survived: false` on macOS. The source parent is not presented as the
fix commit; the repair is the commit containing this record.

RPC now signals the owned process group while its leader is identifiable. The
helper can reap its Popen leader while observing the group, rather than infer
group death from parent death. Resistant members receive SIGKILL. Errors no
longer silently become successful RPC closure. The caller's process group is
explicitly protected. Existing grace durations are reused.

Six new tests passed on macOS and Linux: real SIGTERM-resistant descendant
cancellation, duplicate cancellation, unrelated-process survival, refusal to
signal the caller, retryable RPC cleanup errors, supervisor cleanup error/
idempotency and fleet observation of pending/failed cleanup (some assertions
share one test). No fake model was used in the process regression.

The Linux VM additionally ran the existing fleet, HTTP guest, platform and
steering E2E suites. Results are in `results.json`; native Windows cases remain
skipped on Linux. The full unrelated review/session-history suite was not rerun.

Supervisor cleanup is separate metadata from task outcome. A terminal run whose
adapter cleanup is pending/failed is not spare fleet capacity. This is an adapter
receipt; process-group escape, detached nested jobs, already-departed backend
leaders, durable environment cleanup and actual cloud stop remain unqualified.
No permanent cloud enrollment or automatic data deletion was enabled.
