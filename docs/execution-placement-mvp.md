# Execution placement MVP

Status: incremental implementation authorized by the owner on 2026-10-29.
The existing-host slice is implemented; the native AgentCore guest passed a
bounded qualification trial. Automatic cloud provisioning remains planned.
See the implementation checkpoint and execution-placement-verification.md for evidence.
Updated: 2026-10-30.

All execution environments use the existing shared development Docker services
by default. Task-specific image exceptions need durable ownership and verified
disposal, independently of the agent VM. See
[shared development resources](shared-development-resources.md) for the owner's
decision, the small fleet-store extension and remaining acceptance work. Native
ARM64 AgentCore access to shared Docker passed an operator-controlled smoke;
automatic enrollment and resource disposal remain planned.

## Initial plan saved before updating the clone

Keep Codex Desktop and Claude Desktop Code as the user's interfaces. Extend Porch rather than introduce another mandatory UI. Retain two skills: pragmatic-orchestration owns agent execution and remote-agents owns execution environments.

Expected workload is roughly ten concurrent development tasks, potentially spawning subagents and heavy browser checks; this is a workload assumption, not a concurrency limit. Use subscription-authenticated native agent CLIs only. Preserve steering, interruption and conversation continuation through the originating chat.

Use the Mac, an existing development VM, and on-demand cloud execution. AgentCore is preferred but not mandatory; an ordinary EC2 instance booted from a prepared image is also a candidate. A session-start hook provides advisory placement information. Actual dispatch refreshes state and atomically reserves placement. Never migrate running tasks automatically or retry uncertain launches blindly.

Prepare dependencies, browsers, skills and instructions in versioned environments. Keep credentials outside images. Shared agent memory is deferred; retain task-specific history and files. Aim to retain completed environments for about a day, with preservation before deletion; exact lifecycle policy remains to be decided.

First qualify Claude interruption/continuation, then existing-host remote execution, then cloud provisioning. Research backend-independent boundaries covering existing hosts, stopped hosts, image-created VMs, AgentCore, Docker Sandboxes, Daytona and Vercel Sandbox. Implement only the smallest useful provider set.

## Reassessment after repository refresh

`git fetch origin` followed by `git merge --ff-only origin/main` left main at
`cc829d908822978fe7fce4670967525ea3a9a267` (already current). This document was first
saved before that refresh and then expanded against this revision. No runtime,
installed skill interface, cloud resource or credential was changed.

The following is a proposed design, not an implementation claim. User requirements
above are settled; provider selection, capacity calibration and interface names
below remain design choices. Ten tasks is an expected workload, not a new cap.

## Decision and comparison basis (FPF)

The result to obtain is one coding task launched on suitable compute, with its
files, events and conversation controllable from its originating desktop chat.
Compare complete configurations that deliver this result, including provisioning,
authentication, browser tooling, recovery and ongoing maintenance. An agent UI,
a VM provider and a scheduler are different components, not interchangeable products.

| Complete approach | What it adds | Principal unresolved cost | Disposition |
| --- | --- | --- | --- |
| Porch + existing hosts + one on-demand provider | Placement and transport around existing agent adapters | Shared reservations, cloud login and remote control qualification | Preferred MVP |
| Another orchestration product used headlessly + provider + Porch/UI bridge | Potentially supplied session management | Must prove two native subscriptions, bidirectional control and load-based placement through original chats; another lifecycle owner | Reconsider only with evidence it removes more code than it adds |
| General distributed workflow scheduler + worker fleet | Durable workflows and scaling mechanisms | Still needs CLI session control, environment assembly and subscription handling | Defer; no demonstrated need at this scale |

Omnigent/Paseo remain possible integration components, not an additional required
UI or control plane in this MVP. No claim of unsupported features is made here;
a new candidate must demonstrate the same end-to-end result before replacing Porch.
No numerical rankings, throughput or cost forecasts are justified by the current evidence.

The first decision-changing experiments are native subscription login in disposable
compute, reliable Claude interruption/continuation, and reconnecting to a detached
runner. A failure in any of these matters more than a provider's provisioning API.

## Two independent axes, with explicit compatibility

1. **Agent backend:** Codex CLI, Claude Code, later other Porch adapters. Owns model
   selection, native session identifiers, turn events and interrupt semantics.
2. **Environment provider:** existing host, stopped VM, image-created VM, AgentCore,
   Docker Sandbox, Daytona or Vercel Sandbox. Owns compute lifecycle and access.

The placement core knows requirements and observations, not vendor APIs. It must
not contain branches such as “if Claude on EC2”. A qualified pairing is the
intersection of agent capabilities, environment capabilities, installed profile
and verified subscription authentication. Independence does not mean every pairing works.

```text
Codex / Claude Desktop originating chat
                    |
          Porch dispatch and control
                    |
       placement decision + reservation
                    |
       environment acquisition and access
                    |
      detached Porch runner in environment
                    |
         existing native agent adapter
```

Keep the existing two skills. `pragmatic-orchestration` explains task delegation,
placement and steering. `remote-agents` explains host onboarding, recipes and
provider operations. A third scheduler skill would duplicate the user workflow.
The scheduler must be executable code; prose in a skill cannot enforce reservations.

## Small executable boundary (Code That Fits in Your Head)

Use a functional core and an imperative shell. The complete dispatch flow should
be readable in order: inspect, decide, reserve, acquire, launch, record. Avoid a
workflow DSL, plugin discovery framework, event bus, generic dependency container
or universal VM class.

Reuse these existing foundations rather than creating a second agent protocol:

- `skills/pragmatic-orchestration/scripts/lib/backend_contract.py`: backend identities
  and capability truth.
- `scripts/lib/steer/adapters/base.py` under that skill: adapter lifecycle,
  `DeliveryClass`, events and steering results.
- Existing steer supervisor, registry, mailbox and event journals: runner lifecycle
  and conversation control; extend their actual boundaries after a focused review.
- `skills/remote-agents/references/porch-remote.md` and `managed-workers.md`:
  existing SSH/detached-runner patterns; not evidence of a finished fleet scheduler.

Proposed module responsibilities inside the orchestration runtime (names provisional):

| Module | Responsibility | Must not own |
| --- | --- | --- |
| `placement.py` | Pure `choose(request, observations, reservations, now)` returning a decision and reasons | SSH, SDK calls, reading clocks or credentials |
| `dispatch.py` | Sequential workflow and reconciliation after ambiguous outcomes | Agent-specific stream parsing |
| `placement_store.py` | Atomic reservations, idempotency keys and environment associations | Agent transcript as a second source of truth |
| `environments/base.py` | Small records and acquisition/inspection/release protocol | Every vendor feature as an optional method |
| `environments/existing_host.py` and first cloud adapter | Concrete lifecycle and access bindings | Placement policy |
| Existing runner/control modules | Launch/status/events/steer/interrupt/cancel for a run | Cloud selection |

Start with simple records: `TaskRequest`, `EnvironmentObservation`, `PlacementDecision`
and `EnvironmentLease`. Add fields only when a real caller needs them. A lease is
permission to use an environment, not the environment itself.

Provider operations are `inspect`, idempotent `acquire(request, operation_id)`, and
`release(lease, disposition)`, with explicit outcomes. An acquisition yields an
opaque environment reference and runner connection. Dispositions distinguish
release allocation, stop owned compute, and destroy owned compute. Existing shared
machines are never implicitly stopped or deleted. Optional provider operations
stay in provider code until a second implementation needs the same abstraction.

The runner connection exposes launch, inspect, events-after-cursor and control;
start with local and SSH transports. A future provider may tunnel these through
its exec API or an authenticated service endpoint. Do not require SSH on AgentCore
or sandboxes. SDK transport differences must not leak into agent adapters.

Runtime modules must ship with Porch or be explicit optional integrations. Installing
only `pragmatic-orchestration` must not depend on an assumed sibling skill directory.
`remote-agents` can document/configure providers without becoming an implicit runtime
package dependency. Keep provider registration explicit at the composition root.

## Placement, concurrency and failures

The explicit [stop and cleanup contract](execution-lifecycle.md) defines task
cancellation, compute stop, result preservation and destructive removal. Its
current-code gaps and failure/restart scenarios must be resolved before automatic
cloud enrollment. The successful disposable stand teardown is narrower evidence.

Use one authoritative placement store on the existing VM, reached through a small
Porch command/service over the existing authenticated transport. SQLite on that
machine is sufficient initially; never share a SQLite file over a network mount.
Multiple chats must claim through this single writer. The runner remains authoritative
for execution state; the placement store owns allocations and references only.

Collect running Porch tasks, reservations, available memory, CPU pressure, free disk,
browser workload and environment readiness. Running-task count alone is insufficient:
a waiting agent can be cheap while one browser/build job occupies the machine.
Observe non-Porch load through host metrics; do not claim to identify all its tasks.
Use measured resource profiles and owner-agreed capacity settings; this document
sets no new numeric concurrency, timeout, spending or resource limit.

Proposed selection: explicit user target first; otherwise prefer the existing dev
VM for portable development/browser work, keep Mac-specific work on the Mac, and
use eligible local capacity according to configured preference. If suitable existing
capacity is busy, acquire the configured cloud environment immediately. This ordering
is a proposal, not permission to run local Docker/builds against existing host policy.
A task requiring macOS cannot silently be sent to Linux.

A start-session hook only publishes a compact timestamped advisory snapshot. It
cannot reserve resources for a task not yet known. The reliable enforcement point
is Porch dispatch immediately before launch, including new tasks in old chats.
Hook installation and supported lifecycle events must be verified separately in
each desktop host; the workflow must remain usable without a hook. Initial ordinary
chat work is not automatically intercepted: the skill needs an explicit instruction
to route eligible substantial work through Porch.

Reserve against a fresh snapshot in one transaction, before provisioning. Use a
client-generated operation identifier throughout acquisition and launch. Repeated
requests with the same identifier inspect the original operation rather than start
a second worker. Do not promise exactly-once execution across provider APIs.

Expired heartbeat means unknown, not idle. Reconcile provider state and runner
state before reclaiming a reservation; a lease timer alone does not prove an agent
has stopped. Lost launch responses require lookup by operation/run identifier.
A known pre-launch failure may release a claim. An uncertain launch may not be
blindly retried on another host. Report provisioning/authentication failures clearly;
capacity overflow is the requested policy, not a fallback for correctness failures.

If the coordinator is unavailable, existing detached workers continue. Automatic
new placement is unavailable and is reported as such; do not start a rival coordinator
or silently bypass reservations. An explicitly local workflow remains a separate
user choice. No high availability is proposed for the MVP.

Native subagents stay inside the parent environment initially, and host measurements
include their resource use. Do not recursively redispatch them as new fleet tasks.
A separately delegated Porch task receives a parent task reference and its own
reservation. Unknown resource demand is recorded honestly, then refined by measurements.

## Environment and task data

Define a versioned logical environment profile: OS/architecture, CLI versions,
language runtimes, browser binaries/dependencies, skills and project instructions.
Resolve it into provider-specific artifacts (AMI, container/template or snapshot).
One logical profile is not one portable binary image. Verify readiness before a
new environment becomes eligible, including browser launch and runner protocol.

Transfer a declared repository revision and, when requested, an explicit patch of
uncommitted work. Never assume a remote bind mount points to Mac files. Use one
isolated checkout/workspace per writing task and retain result commits/patches,
logs, native session references and required artifacts independently of compute.
Do not silently omit untracked inputs or export ignored secrets.

Subscription login is a separate environment qualification step. Use the native
agent's supported authentication; no model API fallback and no credentials baked
into images. A newly created machine requiring manual login on every task fails
the desired unattended overflow workflow until a supported solution is qualified.
Do not infer token portability from the ability to copy a filesystem. Model account
quotas remain shared even when compute expands.

Retain task history separately from shared agent memory. Memory integration remains
out of scope. About one day of environment retention is a desired user experience,
not permission to terminate active tasks after 24 hours or erase their results.
Cleanup applies only to owned idle resources after verified result preservation;
exact retention and spending settings need a concrete operational decision.

## Provider assessment: evidence checked 2026-10-29

| Provider | Relevant evidence | What still needs proof for Porch | MVP position |
| --- | --- | --- | --- |
| Existing Mac + Linux VM | Current Porch host and detached-runner patterns | Shared claims, reconnect, full two-agent lifecycle | First vertical slice |
| Start a stopped VM | Reuses persisted disk; lifecycle differs from creating a new machine | Native login after restart, host readiness, correct provider API | Small follow-up to existing host binding |
| New EC2 from prepared image | Fits ordinary host/runner model; existing host tooling provides a starting point | Bootstrap/authentication, browser profile, idempotent provisioning and cleanup | Baseline cloud proposal |
| AgentCore Instances | Managed EC2 capacity with longer sessions and persistent storage [1] | Runtime integration, long-lived bidirectional control, native subscription auth | Qualify against EC2 baseline before choosing first cloud provider |
| Docker Sandboxes | Local and cloud modes now documented separately [2] | Host compatibility for local mode; cloud auth/config/transport differences | Future adapter; not equivalent to ordinary Docker containers |
| Daytona | Snapshot-based environments; v2 CLI explicitly labelled Alpha [3] | Pin stable API/version, stop semantics, subscriptions and detached control | Future adapter; do not build MVP on unqualified alpha interface |
| Vercel Sandbox | Current guide documents persistent filesystem and separate session lifetime [4] | Actual account/runtime support, CLI control and auth; distinguish disk resume from process resume | Future adapter; do not reject on obsolete ephemeral-only assumptions |

[1] [AWS AgentCore Instances](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-instances-how-it-works.html).
Instances and microVM compute have different lifetime/architecture/storage contracts.
The documentation currently lists up to 14-day Instances sessions versus 8-hour
microVM sessions. This is vendor capability, not proof that Porch works there.

[2] [Docker local/cloud comparison](https://docs.docker.com/ai/sandboxes/cloud/local-vs-cloud/)
and [local architecture](https://docs.docker.com/ai/sandboxes/architecture/).
Cloud has its own resources, secrets and configuration restrictions. Filesystem
moves do not transfer running processes. Local mode consumes host resources;
cloud mode uses managed compute. Neither is assumed enabled on the user's hosts.

[3] [Daytona stable documentation entry](https://www.daytona.io/docs/en/) and
[v2 CLI reference](https://www.daytona.io/docs/en/v2/cli/sandboxes-and-resources/).
The latter is useful architectural evidence, not a stable integration commitment.

[4] [Vercel duration and persistence guide](https://vercel.com/kb/guide/vercel-sandbox-duration-and-persistence).
It describes filesystem snapshots on stop/resume and plan-dependent session
lengths. A restored filesystem does not establish live native-agent continuation.
Validate the actual SDK and account rather than hard-code documentation defaults.

## Claude interrupt: close the adapter gap first

At the inspected repository revision, `steer/adapters/claude.py` explicitly rejects
interrupt mode and treats an authoritative result as completing the adapter.
This is a Porch implementation limitation, not evidence Claude cannot interrupt.
The [official SDK query implementation](https://github.com/anthropics/claude-agent-sdk-python/blob/main/src/claude_agent_sdk/_internal/query.py)
provides the control-request interrupt protocol. Installed Claude CLI observed in
this investigation: 2.1.302; recheck the version when implementation starts.

Plan the state change before adding a control message: distinguish active turn,
interrupted turn, conversation still open and process exited. Correlate control
requests, acknowledgements and turn outcomes. An acknowledgement is not proof that
the tool or turn stopped. Do not treat an interrupted result as successful task
completion or replay a side-effecting tool call to recover a missing result.

Test interrupt during generation, during a tool, immediately after prompt input,
and between turns; then send a new instruction in the same conversation. Include
repeated interruption, missing/delayed result, process exit and reconnect. The current
fake-Claude tests need protocol scenarios rather than merely accepting a new flag.
A real native-subscription smoke is necessary before advertising support; none was
run for this planning document. Durable Claude continuation after process restart
is a separate capability from interrupting a live turn.

## Delivery sequence and acceptance evidence

1. **Prove agent control locally.** Focused adapter/state-machine tests and an
   authorized subscription smoke for Codex and Claude. Preserve honest capability
   reporting when a backend cannot supply a requested operation.
2. **Complete one existing-host slice.** Originating chat delegates to the VM,
   receives progress, steers/interrupts, disconnects/reconnects and collects results.
   Two concurrent clients cannot claim the same final available allocation. No
   cloud provisioning is needed for this step.
3. **Add environment profile and one cloud provider.** Compare a small AgentCore
   qualification with the EC2 baseline. Choose one based on native auth, control,
   browser execution and recovery evidence. Busy existing capacity launches cloud
   without a queue; no duplicate task after an uncertain response.
4. **Add advisory desktop integration.** Update the two existing skills and optional
   supported hooks. User sees placement reason and execution state in the original
   chat. Do not create a new mandatory dashboard.
5. **Exercise representative concurrency and cleanup.** Mixed browser/development
   jobs around the expected ten-task workload, nested subagents, host loss,
   coordinator restart, provider failure and result-preservation failure. Record
   measured pressure; use it to propose capacity settings rather than invent them.

Under the owner's latest scope, the MVP is complete only when Codex can perform
the agreed control flow on existing hosts and the chosen cloud path, including
qualified stopping/cleanup, subscription billing and
recoverable task history. Steps 1–2 alone are an intermediate delivery, not the
full requested result. Claude remote enrollment is deferred by the owner; its
adapter remains available for later qualification. Other providers are enabled individually after the same
contract tests; there is no requirement to implement all providers now.

Offline tests should focus on meaningful decisions and failures: concurrent claim,
idempotency, stale evidence, wrong OS/profile, unknown launch result, detached
runner recovery, interrupt/continuation and safe ownership-aware release. Avoid
mock-only evidence for native auth, provider persistence or browser behavior.

## Deferred decisions and explicit exclusions

Before implementation, settle the first cloud provider with the qualification above,
the actual resource profiles, supported login/bootstrap procedure, result archive
location and idle-resource policy. Provider pricing/account availability must be
checked then; this document makes no infrastructure purchase or cost promise.

No automatic live migration, shared agent memory, generalized workflow engine,
distributed consensus, speculative plugin marketplace, spot interruption handling,
universal image format or full multi-provider implementation belongs in this MVP.
Manual transfer is a distinct checkpoint/export/resume operation to design later;
it must never masquerade as moving a running process.

## ARAI implementation evidence and revised cloud priority

Added 2026-10-29 after the owner pointed to the existing ARAI implementation.
This section supersedes the earlier EC2-first baseline preference: **qualify
AgentCore Instances first**, using the existing ARAI design as concrete evidence.
Ordinary EC2 remains a comparison option if the small Porch integration cannot
meet its requirements; do not implement both provisioners preemptively.

Scope of evidence: inspected local source and dated checked-in experiment/activation
records in the sibling `arai` repository. No production inspection, cloud operation,
credential read, build or model call was performed. Historical receipts establish
what was tested then, not the current health of production or Porch compatibility.

### What exists and what it proves

| ARAI source or record (repository-relative) | Observed implementation/evidence | Implication for Porch |
| --- | --- | --- |
| `app/deploy/terraform/aws-agentcore/agentcore.tf` | AgentCore Instances capacity provider; Linux ARM64 compute; container referenced by ECR digest; separate work volume mounted into the guest | Image-based deployment and persistent work storage are implemented, not hypothetical |
| `app/deploy/terraform/aws-agentcore/image/Dockerfile` | Native dependency parent plus application/skills overlay; Python AgentCore adapter entrypoint; `/work` links to the mounted volume | Reuse the image assembly pattern, but build a separate general coding profile |
| `agent/sandbox-agent/adapter/agentcore_adapter.py` | `/ping` and `/invocations`; wake/launch/control forwarding to a guest runner; boot identity and busy health states | Implement a thin Porch adapter around its runner, without requiring SSH |
| `agent/runtime/src/provider/boundary.ts` | Separate lifecycle, runner transport and object exchange; unavailable observations and uncertain outcomes remain explicit | Adopt these three responsibilities instead of an oversized universal environment object |
| `agent/runtime/src/provider/agentcore/runner-channel.ts` | Runner commands and event retrieval carried over AgentCore invocation envelopes, with operation and boot identity | Interactive supervision can use short requests to a detached runner; no permanent client connection required |
| `agent/runtime/src/provider/agentcore/model-proxy.ts` | Trusted host owns Codex subscription OAuth and forwards authorized guest model requests | Existing subscription success depends on a separate authentication component, not on AgentCore itself |
| `docs/benchmarks/2026-10-19-agentcore-w3/model/README.md` | Recorded successful subscription task, question/answer, output retrieval and checkpoint restore preserving the native Codex session UUID; also reports a hard-stop usage accounting defect | Strong prior evidence for Codex; not proof of Claude support or identical Porch steering semantics |
| `docs/runbooks/agentcore-activation.md` and `docs/benchmarks/2026-10-19-agentcore-w5-activation/README.md` | Activation status and production rollout record | Prefer dated activation evidence over older pre-activation paragraphs still retained in the runbook |

These are source references for maintainers with the sibling checkout; Porch must
not import ARAI application code at runtime. Keep private deployment identifiers,
account details and operational credentials out of the generic Porch configuration.

### Revised provider boundary

Keep the placement core and existing agent adapters unchanged in responsibility.
Refine the environment integration into three small seams, following the proven
ARAI split:

1. **Lifecycle:** acquire, passively observe, stop/release/remove with explicit
   ownership and confirmed/requested/unknown outcomes.
2. **Runner transport:** launch, inspect, fetch events, steer, interrupt and cancel
   through local execution, SSH or AgentCore invocation.
3. **File exchange:** stage repository inputs and retrieve verified results using
   local files, remote transfer or object storage. Large files do not belong in
   command envelopes.

Do not implement these as three new frameworks. Initially these can be small
protocols/functions composed by one provider binding. Add code only for existing
hosts and AgentCore. Keep the public CLI independent of this internal split.

Crucial lesson from ARAI: **an AgentCore invocation can start or wake compute**.
Therefore fleet status and cleanup checks must use passive provider inventory,
not a guest health invocation disguised as a read. Distinguish passive environment
observation from active runner inspection. A changed boot identity invalidates
assumptions about the old process even if the persistent work volume is intact.

### Minimal AgentCore vertical slice for Porch

- Prepare a separate Porch runtime/image and resource namespace, reusing build
  techniques and qualified dependencies rather than the production engineering
  image, production work volumes or ARAI task services.
- Put Porch, the selected native CLIs, common skills, language dependencies and a
  browser smoke in the profile. Check ARM64 dependency compatibility; supporting
  an image does not prove all project tools run on it.
- Have the existing placement coordinator invoke the Porch guest adapter through
  the provider binding. The guest starts a detached Porch supervisor and returns
  stable run and boot references; subsequent calls exchange control and events.
- Stage a revision/patch/input manifest through explicit object exchange, and
  preserve results outside the disposable environment before releasing it.
- Verify the existing Codex authentication approach for personal Porch use as a
  distinct integration. Review current provider-supported authentication and
  applicable terms before reusing that approach; historical technical success
  alone establishes neither portability nor authorization. Prefer supported native
  login where it supplies unattended operation. Do not copy production credentials
  or introduce an API-billed substitute.
- Qualify Claude authentication and interrupt independently. ARAI's Codex proxy
  cannot be presumed usable for Claude. This is now the largest remaining gap
  in the two-agent AgentCore plan, alongside the general coding/browser profile.

The ARAI controller, application Task schema, billing settlement, publication,
customer isolation and full trusted-service topology are not the Porch MVP. Reuse
behavioral lessons and focused protocol code only after checking dependencies and
licenses; do not vendor the whole runtime. Preserve essential behavior: durable
operation identity before side effects, reconcile ambiguous starts, recognize new
boots, distinguish requested from observed stop, and export before deletion.

### Startup and retention implications

ARAI's `docs/benchmarks/2026-10-24-agentcore-image-coldstart/README.md` records
63 isolated starts. The production-shaped gzip image on baseline disk had a
124-second median (9 samples); the same workload with zstd and faster root disk
had a 79-second median (6 samples). These are historical ARAI measurements, **not
Porch startup estimates**. A coding image with browsers needs its own measurement.
Do not copy faster disk settings or add a warm pool without a workload/cost decision.

“Immediately launch cloud when busy” means begin acquisition immediately, not promise
an instantly ready agent. Report provisioning as a real state in the originating
chat. A stopped volume, restored filesystem and resumed native conversation remain
three different things. ARAI's archive workflow also demonstrates that recovery
can add delay; Porch must avoid serializing unrelated dispatch behind a long export.

### What changed in the next implementation step

After the local Claude control qualification, use the existing VM slice to establish
the common runner/control contract. Then implement the **AgentCore** slice against
that same contract; no broad provider bake-off is needed first. The qualification
is now narrowly targeted at Porch-native control, subscription authentication for
both CLIs, general coding/browser dependencies, file exchange and reliable cleanup.
Do not repeat the already documented investigation into whether AgentCore can
launch a prepared image or carry a remote runner protocol.

## Implementation checkpoint: existing-host slice, 2026-10-29

Owner approved implementation and incremental evaluation. Owner's decision,
2026-10-29: automatic preference is VM then cloud; Mac only by explicit selection
("Да: VM, затем облако; Mac явно"). The first code slice provides opt-in `porch
fleet` commands; details and its exact experimental scope are in
`skills/pragmatic-orchestration/references/fleet.md`.

Implemented: local/SSH observation, pure selection in configured host order,
single-coordinator SQLite claims, weighted task allocations, same-workspace
reservation, detached launch through existing delegate, original-host controls,
idempotent request replay and lost-receipt reconciliation from runner metadata.
A changed host binding or unavailable observation fails explicitly. No model API
or cloud credential integration was added. No automatic default was enabled.

Not yet implemented: AgentCore provider/image, project staging/result archival,
Claude interrupt and durable continuation, subscription readiness qualification,
resource profile calibration, hook installation and unattended cleanup. The
existing-host slice is not the complete MVP. Uncertain launches without a matching
runner receipt retain their reservation and require investigation; they are never
automatically retried or expired.

The dev VM has Codex on PATH but neither Porch nor Claude on PATH at inspection.
A task-local Porch copy was used for unpaid fake-agent verification; no shared
installation or production service was changed. Its existing host-level admission
policy still governs heavy work. Numeric capacities in test/example configurations
are test fixtures, not approved operating limits for the user's real tasks.

## Implementation checkpoint: Claude live handover, 2026-10-29

The Claude adapter now implements native live interrupt-and-replacement without
ending the conversation. The native CLI subscription smoke and public Porch smoke
passed on version 2.1.302; details are in `claude-interrupt-verification.md`.
This supersedes the earlier statement that Porch rejects every Claude interrupt.
Queued-input/overlapping handover cases remain explicit unsupported transitions;
no queue is dropped and no failed instruction is replayed. Durable Claude resume
across process exit and cloud subscription readiness are still separate work.

## Owner's current execution preferences, 2026-10-29

Owner's decision: remote VM/cloud execution uses Codex only for now; do not finish
Claude authentication or enroll it in remote placement. Claude CLI 2.1.302 was
installed in the dev VM user's native directory but remains unauthenticated and
inactive. No Mac credential was copied. Its already implemented interrupt support
is retained for future use.

Owner's final profile correction: "Делегатор по дефолту GPT 6.1 Sol high и 6.1 Terra xhigh".
The standard delegation choices are `codex` (GPT-6.1 Sol/high) and
`codex-gpt-6.1-terra` (GPT-6.1 Terra/xhigh). This supersedes the intermediate
Sol/xhigh request. One delegated task still selects one exact profile; this does
not authorize duplicating every task across both profiles. The shipped profiles
already have these model/effort settings; fleet examples now enroll only these
Codex choices.

## Implementation checkpoint: AgentCore guest qualification, 2026-10-29

Added the guest half of an AgentCore provider: a small HTTP facade around the
existing fleet CLI, a typed channel with session/boot checks, and an ARM64 image
recipe. It reuses the existing task registry, reservation/idempotency semantics
and agent adapters. It creates no AWS resources and does not enroll a provisionable
cloud pool in the scheduler yet. See `references/agentcore-image.md` in the Porch
skill for its exact interface and qualification boundary.

The image built on the existing dev VM; its HTTP health/readiness, Codex binary,
Python, Node and Chromium installation were inspected. Six additional loopback
protocol tests passed with real Porch supervision and fake agent/auth boundaries.
No credential was included in the image; it correctly reports subscription
readiness false and refuses a launch until native subscription login is ready.

The ARM64 Chromium headless test failed under QEMU on the x86 dev VM, reporting
GPU subprocess launch failure. Browser execution is therefore unqualified. Do not
hide it by changing flags or calling a different platform's result an ARM pass.
A native ARM cloud trial is the next decision-changing experiment. Cloud lifecycle,
passive inventory, object exchange and unattended Codex subscription bootstrap are
still required before VM-busy → automatic AgentCore overflow can be advertised.

## Implementation checkpoint: native ARM trial, 2026-10-29

The owner approved the separate disposable stand described in
`agentcore-native-trial.md`: "да" to the private ECR push, one m7g.xlarge at a
time, two-hour experiment window and USD 10 AWS budget, followed by removal.
There were no model calls or production changes. This is authorization for the
experiment, not a new production runtime/concurrency limit or permanent pool.

Native ARM Chromium returned the expected DOM with exit 0. The corrected guest
also passed AWS HTTP readiness, stable session/boot identity and a UID-1000
work-volume write/read, while reporting subscription readiness false. This
supersedes the earlier lack of native browser evidence; the QEMU failure remains
a failed emulation result. It does not qualify a real project's browser suite.

Two concrete integration defects were resolved: runtime root ownership/UID
changes fail on capability-free Instances, so ownership belongs in the build;
AWS chunked request framing needs explicit decoding at the HTTP boundary. Eight
guest/channel tests passed. The native trial's final overlay, source hashes,
failed variants and cleanup verification live in
`docs/benchmarks/2026-10-29-agentcore-native-trial/`.

The final StopRuntimeSession attempt returned an internal service error. Its
outcome was treated as unknown, never as confirmation of a stop. The synthetic
stand was retired through DeleteCapacityProviderSession and passive resource
inspection. Stop/resume behavior and an interruption deadline remain unqualified.

The stop/cleanup audit subsequently reproduced a descendant surviving RPC parent
reaping. Close the cancellation boundary and cleanup-evidence gaps in
`execution-lifecycle.md` before the next live cloud slice. Then qualify
supported unattended native subscription bootstrap in a
separate trusted configuration; reuse the ARAI broker design only after examining
its source, without its production credentials or services. Then connect passive
inventory, idempotent acquisition/release and repository/result exchange through
one small provider adapter. Keep AWS SDK/lifecycle code outside placement and
agent adapters, and keep the existing originating chats and two-skill boundary.
Automatic enrollment still needs its own concrete operational configuration;
the disposable trial does not establish it.

## Implementation checkpoint: cancellation and cleanup evidence, 2026-10-30

The reproduced controlled-cancellation defect is fixed: RPC group termination
precedes parent reaping, continues observing the group after the leader exits,
and kills remaining resistant members. The original macOS reproducer now reports
no surviving descendant. New regressions run on macOS and Linux; unrelated jobs
and the caller's group remain protected. Cleanup failures are visible/retryable.

The existing-host MVP now distinguishes task result from adapter cleanup. Each
new run starts with pending cleanup; completion/failure is recorded separately,
and fleet does not reuse its allocation while cleanup is pending or failed.
This advances the coordinator without adding a provider framework or daemon.

Remaining work before automatic cloud enrollment: host-level ownership of nested
detached jobs, already-exited-backend recovery, durable provider cleanup/result
exchange, and unattended native subscription bootstrap. No new cloud resources
or model calls were used for this slice. Verification is recorded under
`docs/benchmarks/2026-10-30-cancellation-fix/`.

## Implementation checkpoint: real development task, 2026-10-30

At the owner's request, a native ARM AgentCore worker using the existing Codex
subscription and GPT-6.1 Terra / medium completed a real ARAI formatter task.
Its original-policy regression failed and the correction passed; selected HTTP
tests (52), the quick app check (880), lint and three TypeScript projects passed.
A real Chromium landing lane had 20 passes and one Kazakh 320px reflow failure;
full Docker-dependent App E2E remains unqualified. ARAI retains the detailed
record at `docs/benchmarks/2026-10-30-agentcore-development-smoke/` in its repo.

The trial found and repaired the missing Codex code-mode companion and idle
bootstrap reporting; see the [image qualification](benchmarks/2026-10-30-agentcore-code-host/README.md).
The owner authorized preserving the native login profile and installation ID
for reuse. They are protected outside source/images and were exported before
the trial session was deleted. Automatic profile seeding and concurrent token
refresh are still separate work; this does not establish a permanent login pool.

All 20 passive final cleanup checks passed at 2026-10-30 04:25 UTC, including
both trial runtimes/identities, compute, work volumes, networking and roles.
No trial resources remain and no production deployment or Git publication ran.

The next infrastructure slice is a private Tailscale/SSH bridge to the shared
Docker VM, with exact revision transfer and whole Docker-backed test lanes in
isolated server checkouts. The owner confirmed tailnet administrator access;
service SSH authorization and enrollment setup are pending. Docker API access
alone does not make server bind paths and localhost ports available to a guest.
Keep provider lifecycle, source exchange and auth outside the placement core.
