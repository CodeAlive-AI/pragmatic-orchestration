# Native ARM AgentCore trial

Status: native guest/tool/browser checks passed; all 18 cleanup checks passed.
Prepared and approved 2026-10-29.

**Owner's approval, 2026-10-29:** "да", answering the request to create a separate
native ARM AgentCore stand, privately push the image, run one m7g.xlarge for up to
two hours within a USD 10 AWS budget, then remove the stand. No model calls or
ARAI production changes were included. This approves those experiment controls;
it introduces no limit on the eventual user's task concurrency or runtime.

## Question this trial resolves

Does the prepared Porch ARM64 image, particularly Chromium, work on native
AgentCore Instances? The x86 dev VM built and booted the image, but the browser
failed under QEMU. Native ARM evidence changes the next implementation decision;
another emulation retry does not.

## Approved scope

- Push the already built qualification image to a new **private** ECR repository
  dedicated to Porch; do not publish a Git branch/release or alter ARAI images.
- Create a separate AgentCore runtime and capacity provider using Linux ARM64 on
  `m7g.xlarge`, a separate encrypted 20 GiB work volume, required narrowly scoped
  IAM roles, and the network/logging resources needed by this isolated stand.
  Existing ARAI production resources and its subscription proxy are out of scope.
- Run one synthetic native ARM session at a time for this experiment, with an
  experiment window of at most two hours and an owner-approved AWS spending budget
  of **up to USD 10**. These are approved experiment controls, not new limits on
  the eventual user's task parallelism or runtime.
- Use the existing authorized operator identity with short-lived role sessions.
  No new IAM users or long-lived keys, no credential copies to the guest.
- Run HTTP/boot identity, Codex/Python/Node version and native Chromium checks.
  The browser uses a static local data URL. No model requests, subscription login,
  customer files or production API are part of this trial.
- Export the small synthetic results and resource inventory, then stop/remove
  only this stand's managed session, volumes and other temporary resources.
  Preserve the code/image recipe and verification record. Do not destroy source
  data or change production retention settings.

The budget is a requested authorization ceiling, **not a price estimate**. No
startup, throughput or browser success estimate is made. If a necessary step
cannot fit the approved scope/budget, stop before that new commitment and explain.

## Prepared artifact

The local qualification tag is recorded in `agentcore-image-verification.md` with
its manifest digests, exact inspected tool versions and failed browser evidence.
The code under `skills/pragmatic-orchestration/scripts/lib/agentcore` and image
recipe are independent of ARAI's application services. Native AWS acceptance of
the corrected guest, browser and writable work volume passed; the complete cloud
dispatcher is still unimplemented. The exact outcomes and failed earlier variants
are in [the verification record](benchmarks/2026-10-29-agentcore-native-trial/README.md).

This approval authorized this bounded infrastructure experiment and its
private image push. It did not authorize general automatic cloud overflow,
permanent infrastructure, model API billing, credential inspection/copying,
production changes or Git publication.

## Acceptance and next decision

The final native image passed the expected DOM, HTTP/session identity,
subscription-readiness-false and writable-volume checks. Independent passive
verification at 2026-10-29 15:43 UTC passed all 18 cleanup checks: no owned live
compute, volumes, interfaces, runtime/capacity/identity records, network, IAM
roles/profile, repository, log groups, autoscaling group or launch template
remained. See `cleanup-verification.json` in the verification record. Exact billed
cost is not yet available; the approved budget is not a bill estimate.

Pass only after a native ARM guest returns the expected browser DOM, reports
honest subscription readiness false, preserves session/boot identity, and can be
observed/retired without leaving owned compute or volumes behind. Record actual
image digest, timestamps, platform, outcomes and remaining resources. A version
string alone is not a browser pass. An unavailable inventory is not absence proof.

After this experiment, finish passive cloud inventory and lifecycle binding,
repository/result exchange and supported unattended Codex subscription bootstrap.
Only then qualify the full VM-busy → AgentCore dispatch flow with Sol/high and
Terra/xhigh. Failure leads to a documented image/platform fix or a provider
comparison, never an automatic hidden switch to another backend.
