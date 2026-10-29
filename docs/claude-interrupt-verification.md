# Claude interrupt handover: 2026-10-29

Implementation adds live `cancel_and_send` to the existing stream-json adapter.
It waits for native turn initialization before sending interrupt, and for both a
correlated control acknowledgement and old-turn result before sending replacement
input. These barriers are independent of response order. Duplicate result UUIDs
are ignored, and old partial text does not become the replacement's answer.

Eleven deterministic protocol tests cover success, both event orders, an early
request, acknowledgement without a result, unrelated acknowledgements, explicit
rejection, result-before-start, repeated interruption, overlapping/queued input
and unexpected native queue ownership. The fake-CLI end-to-end test exercises the
public mailbox and supervisor path rather than only the adapter methods.

A native protocol probe and a separate public Porch smoke used the existing
Claude Code 2.1.302 subscription login. They ran only in isolated scratch
directories. The smoke interrupted `touch started; sleep 30; touch late` after
`started` existed, sent a replacement instruction and received exactly
`PORCH_CLAUDE_INTERRUPT_OK` from the same native session. The Porch run completed,
its steer used `cancel_and_send`, and `late` remained absent after the original
30-second deadline. No repository or external service was changed by the model.
No separate model API was configured.

The native receipt was `control_response/success` with `still_queued: []`; the
old turn ended as `error_during_execution` with no result text. The next turn
emitted a fresh `system/init` and succeeded. These observations justify preserving
the process across the interrupted result, not treating that result as task failure.

`--bare` did not use the available subscription login in the first probe. A
second probe initially supplied an invalid empty MCP configuration. These setup
attempts performed no model task. The successful probes used the normal native
binary with an explicit empty `mcpServers` record and no auto-discovered setting
sources, leaving authentication under the native CLI's control. No credential
file or keychain value was inspected or copied.

Limits of this evidence: no native Claude on the dev VM was installed, no durable
Claude resume after process exit was tested, and no cloud authentication was
qualified. Known unresolved native queues remain visible; no accepted instruction
is silently cleared or automatically replayed. Native interruption cannot undo
side effects that already happened.

The full offline suite initially found two stale expectations: agent-facing fleet
documentation violated the existing resolved-entrypoint wording check, and the
shared capability test still asserted that Claude rejects interruption. Both were
updated. It also exposed an unrelated 1.5-second OpenCode wall-clock assertion on
the shared VM. That assertion is removed: its existing five-second subprocess
timeout still detects the fake's 30-second hang, and exact terminal output is
still asserted. No production timeout or agent execution behavior was changed.

At the owner's subsequent request, remote execution stays Codex-only. Claude
2.1.302 is installed on the dev VM but not authenticated or enrolled in the fleet.
The native CLI installer verified its downloaded binary and created the user's
launcher under `/home/ubuntu/.local/bin`; shell startup files were not edited.
Native uninstall is removal of that launcher and its version directory, preserving
any user-owned configuration. No API billing or model call was made on that VM.

The complete offline suite passed on the dev VM: **1035 passed, 0 failed**.
The subsequent EOF-race correction has its own process-exit regression: an exit
with code zero before handover completes cannot report replacement-task success.
