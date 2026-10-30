# Shared Docker and disposable task resources

Status: owner decision and implementation plan, 2026-10-30. Service access and
manual resource cleanup passed a native ARM64 AgentCore smoke. Automatic resource
registration/disposal below is planned, not implemented by that smoke.

**Owner's decision, 2026-10-30:** every task on every agent instance uses the
existing shared development Docker host and reuses its development services and
databases by default. A task may build its own image on that host when the shared
environment is unsuitable, provided the orchestrator records and disposes it.
No new concurrency, retention or task-duration limit is introduced.

## Default and exception

Discover configured shared services first, including sleeping profiles. An agent's
separate VM does not imply another Docker daemon or database server. Run
Docker-backed commands in its dedicated checkout on the shared host through the
host's existing heavy-job admission. Remote bind paths and loopback service ports
belong to that host. SSH execution is the first supported bridge; a remote Docker
endpoint alone does not supply filesystem or application-port routing.

Reuse a shared database service. When tests mutate data, use a task-specific
logical database/schema or fixture namespace as needed. Do not reset another
task's data. A different database version, extension, image dependency or test
requiring destructive exclusive state can justify a task-owned exception. Record
the reason; ordinary task images on the shared host need no additional approval.

Distinguish borrowed shared services/base images, private resources created for a
task, and reusable provider infrastructure. A task disposer removes only private
task resources. Compute and Docker resource ownership are separate: deleting an
agent VM does not stop its remote container.

Subagents working inside one task borrow that task's infrastructure; they cannot
dispose the parent's resources. A separately delegated fleet request records its
own resources and parent request. Resource cleanup follows these recorded owners,
independently of the selected model, agent CLI or compute provider.

## Small implementation boundary

Keep this in the existing fleet lifecycle and the existing two skills. Extend
`skills/pragmatic-orchestration/scripts/lib/fleet/store.py` with one resource
table linked to the existing request. Keep the ledger outside disposable
instances. Placement still chooses compute; a small Docker adapter creates,
inspects and removes recorded resources on the configured host. No second
scheduler, workflow engine or plugin-discovery mechanism is needed.

| Recorded fact | Purpose |
| --- | --- |
| Existing task/request and run | Associate the resource with its owner |
| Resource host and kind | Reconcile after the agent VM disappears |
| Exact resource identity | Image ID/digest and tag; container, volume/network ID; test namespace |
| Borrowed or created ownership | Keep shared resources out of deletion targets |
| Reason and dependencies | Explain the exception and cleanup order |
| Creation operation and observed result | Recover an interrupted build/create without duplicating it |
| Cleanup observation and error | Separate execution outcome from verified disposal |

Start with one coordinator and one task owner per private resource. Write creation
intent and a unique operation label before Docker I/O; label resources with the
task/run/operation and record their actual IDs. After a lost reply, discover that
operation's resources instead of repeating it blindly. Until implemented, retain
the same manifest explicitly on the controller. The smoke's manual manifest is
evidence for the mechanism, not an automatic janitor.

## Disposal

Use the [existing lifecycle contract](execution-lifecycle.md), with durable
reconciliation after success, cancellation, failed startup or controller loss:

1. Save wanted source, results and data outside the resources to be removed.
2. Stop and confirm owned remote jobs/containers; release their ports.
3. Remove disposable test volumes, networks and logical namespaces.
4. Remove private image tags/images after checking exact ownership and confirming
   that other tasks do not reference them.
5. Verify absence; retain uncertain observations and errors for reconciliation.

Repeated disposal of an absent owned resource succeeds. A changed tag, foreign
reference, unavailable inventory or failed removal stays unresolved. Do not force
removal based on a name/prefix, age or dead parent process. Release confirmed-idle
compute independently of pending storage cleanup, retaining the resource record
and original task result.

Shared services, base images, database data and common BuildKit cache are not task
cleanup targets. Record private build contexts and a builder created solely for
the task; do not globally prune Docker or a shared builder. If another task adopts
a private image, explicitly transfer its lifecycle to shared ownership before
retiring its creator. Matching content digests do not establish ownership.

## Acceptance before automatic operation

- Two tasks reuse one database without starting two database servers.
- A justified image exception records and removes its container/image/data.
- Cancellation or loss of the agent VM still cleans its owned remote resources.
- A crash between create and recording the result recovers by operation label.
- Repeated disposal is safe; other tasks and shared services survive.
- Changed tags, foreign references and failed inventory/removal stay observable.

Two capless ARM clients and a native ARM64 AgentCore instance passed pinned SSH,
source-hash transfer and remote Docker/HTTP checks. Owned fixtures, volumes, ports,
private staging copies and ephemeral nodes were removed; the native stand passed
all 18 cloud resource absence checks. Automatic disposal, detached-heavy-job
cancellation, real browser E2E through the bridge, restart reconciliation and
concurrent-controller ownership remain unqualified.

Host identities, keys and bootstrap profiles are private configuration; they do
not belong in images, this repository or task source/result bundles.
