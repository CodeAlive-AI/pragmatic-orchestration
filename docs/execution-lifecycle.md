# Stopping tasks and cleaning execution environments

Status: MVP lifecycle contract and qualification gaps, reviewed 2026-10-29.
The AgentCore experiment's removal passed 18 checks. General unattended cleanup
is not implemented or qualified by that experiment. This document introduces no
automatic retention cutoff, task timeout or spending limit.

## Four distinct actions

| Action | Intended result | What remains |
| --- | --- | --- |
| Interrupt/steer | End the current turn and accept replacement guidance | Native conversation, task and environment |
| Cancel task | Stop that task and its owned descendants; preserve partial work/history | Existing host; owned environment until results are preserved and its chosen disposition runs |
| Stop environment | Quiesce all work assigned to an exclusively owned environment and stop its compute | Persistent data and a recoverable task record; storage charges can remain |
| Destroy environment | Remove the explicitly owned disposable resources after preservation | Verified results/history outside the deleted environment and the lifecycle receipt |

A closed originating chat, disconnected SSH client or closed Mac lid is none of
these actions. Detached VM/cloud work continues. Work started on the Mac stays
there unless the owner explicitly requests a handoff.

## Ownership and the cleanup boundary

- **Existing Mac/shared VM:** stop only the task's recorded process/job boundary
  and owned descendants. Never stop the machine, prune shared Docker resources,
  remove another checkout or delete native login state.
- **Owned on-demand environment:** its exact provider/session binding is the
  removal target. A task cannot delete an environment with another active lease.
- **Reusable provider infrastructure:** runtime definitions, capacity pools,
  shared network/NAT, ECR images and IAM roles normally outlive a task. A task
  janitor must not copy the experiment's whole-stand teardown. Remove the pool
  only through its separate ownership/decommission operation.

Record the task, environment reference, owner, frozen connection binding and
created resource identifiers before side effects. A name/prefix alone is not
deletion authority. After a create timeout, discover that operation's exact
resources rather than allocate a replacement or delete a broad project prefix.

## The normal completion/cancellation path

1. Persist the desired action against the original task/environment operation.
   Repeated cancel/cleanup commands join that operation.
2. Stop new task input and new descendants. Ask the native backend to interrupt
   and stop the owned execution boundary, including its task-owned nested Porch
   runs and browser/test processes. Identify descendants through actual recorded
   ownership; unrelated user chats/runs are outside the cancellation tree.
3. Verify quiescence through the execution boundary and fresh observation. An
   interrupt acknowledgement, a dead parent PID, terminal registry metadata or
   an API 200/202 does not establish that writers are gone. Forced termination
   of this task's owned boundary is part of cancellation when needed; do not
   signal an unrelated/reused PID or the caller's process group.
4. Preserve final or partial changes, required untracked inputs/artifacts, native
   session reference and task/event history. Verify a manifest/hash receipt at
   durable storage outside the environment being deleted. A commit or archive
   existing only on its disposable disk is not preservation. Exclude native auth
   homes, credentials and undeclared private files.
5. Apply the recorded environment disposition. Compute may be stopped after
   quiescence while retaining unexported persistent data; destructive removal
   waits for the preservation receipt. Failed export retains the data and reports
   why cleanup is pending. Cancellation does not discard partial work.
6. Verify the provider's resource state and only then close the cleanup record.
   Release the workspace/capacity allocation after task quiescence is known;
   track result preservation and remaining storage cleanup separately. Do not
   retain a compute slot solely because archival is still pending once compute
   absence is confirmed, and do not erase the unresolved environment record.

Task outcome and cleanup outcome are separate facts. Keep the existing task
result plus a small durable cleanup record: requested disposition, last completed
step, `pending/confirmed/unknown/blocked`, result receipt, observed resource state
and last error. This is a record and a reconciliation loop, not a workflow DSL.

## Failure, restart and unattended cleanup

Persist intent before each provider mutation and its observation afterward.
Timeout/5xx means unknown until reconciled against the original resource. A
not-found control-plane record is insufficient if compute/volumes remain.
An expired heartbeat is never proof of inactivity. Unknown ownership or state
retains the claim/data; it does not trigger a replacement or destructive cleanup.

One deterministic reconciler resumes unfinished operations after controller or
agent failure. `finally`, task completion callbacks and the originating Mac chat
are not the only cleanup mechanisms. Reconciliation uses passive inventory,
does not invoke a stopped guest just to inspect it, and never asks an LLM to
decide whether deletion is safe. A deliberate export/recovery wake is a separate
recorded operation, not a status probe.

For AgentCore, plan a small AWS-side scheduled cleanup worker with its own
restricted role and a durable provider operation/result journal. It must work
when the Mac is closed and Mac-issued temporary credentials have expired. The
single Porch coordinator still owns placement; this worker completes only
already authorized provider cleanup and discovers that provider's incomplete
creates. Its concrete journal/permissions and independent recovery rehearsal are
part of the next cloud slice, not authorized or deployed by the earlier trial.
Other providers can implement the same obligation in their own control plane.

Use the owner's desired roughly one-day availability as a design input, not an
approved 24-hour deletion timer. Deploy a numeric idle/retention/spending policy
only after its concrete effects are agreed. Active work and unexported data are
never deleted by a generic TTL. Show meaningful cleanup failures and retained
resources in the originating task; keep exact infrastructure ids in private logs.

## AgentCore-specific obligations

`StopRuntimeSession` stops one runtime, not the entire session/instance, and
retains persistent EBS. Idle/max-lifetime shutdown also retains persistent EBS.
`DeleteCapacityProviderSession` removes the entire session, including compute,
interfaces and persistent volumes. `DeleteCapacityProvider` can remove all its
sessions/data, so it is never a per-task shortcut on a shared pool.

Use `IncludeManagedResources=True` and the exact capacity/session binding for
passive inspection. Preserve session ids durably. Check root/work volumes and
interfaces as well as runtime/provider records; an empty default EC2 listing is
not an absence receipt. Managed EBS cannot be backed up with an ordinary snapshot;
export through the guest while it is available. If export is unavailable, keep
the persistent data and mark cleanup blocked rather than assume it is backed up.

The disposable trial exported synthetic results, deleted its sessions and pool,
then checked the whole stand. Its StopRuntimeSession 500 stayed unknown; the
separately authorized destructive session removal was verified independently.
That result does not establish a reliable resumable stop or task cancellation.

## Current code gaps and acceptance

**2026-10-30 implementation update:** the controlled-cancellation parent-reaping
defect below is fixed. RPC termination signals/observes its process group before
waiting for the leader; observation continues after the leader exits and escalates
to SIGKILL for resistant remaining members. Permission failures are observable,
and signalling the caller's group is refused. Supervisor adapter cleanup now has
pending/completed/failed metadata and an audit error rather than a swallowed
exception. Fleet keeps terminal tasks with incomplete cleanup allocated.
See [verification](benchmarks/2026-10-30-cancellation-fix/README.md).

This is the qualified inherited-process-group slice. A backend that has already
exited before cancellation, nested Porch jobs in separate sessions, and processes
that escape the group still need a durable host job boundary and reconciliation.
Adapter cleanup completion is not a claim of complete environment destruction.
The cloud lifecycle/export/independent cleanup obligations below remain open.

At `821eb2e`, supervisor terminal status is written before its final adapter
cleanup; `_cleanup_adapter` suppresses exceptions. Fleet terminal observations
can therefore release capacity without independent process-quiescence evidence.
`JsonRpcProcess.terminate()` waits/reaps the RPC parent before the Codex adapter
calls `kill_process_group(pid)`. That helper can no longer resolve its group.
A synthetic macOS probe reproduced a live SIGTERM-resistant descendant after
this exact sequence; it then removed its own isolated test group. This is an
observed defect, not a claim that cancellation is fully qualified.
The reproducer and observation are preserved in
[`2026-10-29-stop-cleanup-audit`](benchmarks/2026-10-29-stop-cleanup-audit/README.md).

Before automatic cloud placement, implement and qualify:

| Scenario | Required evidence |
| --- | --- |
| Parent exits before descendants; browser ignores graceful stop | Owned writers are gone, independent caller/unrelated job survives |
| Nested Porch worker detaches into a separate group | Recorded descendants stop; independently owned tasks continue |
| Cleanup throws after task result is written | Result survives; cleanup error stays visible and allocation is not falsely idle |
| Cancel races launch or a duplicate cancel arrives | One operation, no replacement worker, eventual reconciled outcome |
| Stop/delete returns 5xx or response is lost | Same binding inspected; no blind destructive replay or premature release |
| Controller dies after create/export/delete side effect | Durable journal resumes at the observed step without duplicating work |
| Mac closes and temporary credentials expire | Provider cleanup resumes independently with authorized renewable identity |
| Result export fails or worker is unreachable | Partial work/storage retained, cleanup reason visible |
| Compute disappears but a work volume/interface remains | Compute allocation can close; residual resource remains on cleanup ledger |
| New lease arrives during cleanup; shared VM/pool is in use | No stop/delete of another task's or shared resources |
| Normal task and full pool decommission | Different resource sets removed; fresh passive absence receipts |

Sources rechecked 2026-10-29: [AWS session stop/deletion](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-instances-get-started-cli.html)
and [Instances data/export lifecycle](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-instances-data-management.html).
