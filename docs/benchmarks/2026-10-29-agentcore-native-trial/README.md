# Native ARM AgentCore guest qualification

2026-10-29, eu-central-1. Native guest checks and all 18 independent cleanup
checks passed; results are recorded in `results.json`. This is a substrate/runner trial, not a complete
automatic cloud-placement or subscription-authentication qualification.

**Owner's approval, 2026-10-29:** "да", approving a separate stand, private ECR
push, one m7g.xlarge at a time, up to two hours and a USD 10 AWS budget, followed
by stand removal. No model calls or ARAI production changes were included.
These experiment controls do not set the eventual user's task limits.

## What passed

- Linux ARM64 on native m7g.xlarge; process UID 1000.
- Python 3.14.10, Node 24.22.2, Codex CLI 0.165.0 and Chromium 156.0.8078.47.
- Real HTTP `ready` through AgentCore, with `runner_ready: true` and honest
  `subscription_ready: false`. A repeated invocation kept the same session and
  boot identity.
- A synthetic checkpoint was written and read at `/mnt/work/task` as UID 1000.
  This proves writable EBS in this running session; stop/resume recovery was not tested.
- Native Chromium returned the expected DOM and exit 0. The exact command uses
  `--headless --no-sandbox --disable-gpu --disable-dev-shm-usage` and a static data
  URL. DBus diagnostics remain in the raw frames. This is a browser-start smoke,
  not a project Playwright suite or a concurrency/performance benchmark.
- Eight offline guest/channel tests, including actual loopback chunked HTTP,
  extensions/trailers, incomplete framing, identity guards and fake-agent control.

Final image manifest:
`sha256:3b1ef8d0fede93853c0c5412ac9b4eb30784a3b119b92155d93e7930594cf0c0`.
The parent resolved to the previously qualified ARM64 image's index
`sha256:201e5c16cdd05e22a706018e07e835ad2ae74a5d7106825036a3d78c3188a799`.
`results.json` records source hashes and each variant's distinct outcome.

## Failures and changes

The initial overlay booted as root and tried runtime ownership/UID changes.
Instances strips the necessary Linux capabilities; changing cwd alone did not
fix startup. Prior ARAI native evidence identified the same mismatch. The corrected
profile sets ownership during build and starts directly as `USER 1000:1000`.
The corrected HTTP process also passed on the dev VM with capabilities dropped
and `no-new-privileges` enabled. The numeric-root diagnostic v3 was built/pushed
but superseded before invocation; it is not counted as native evidence.
`root-bootstrap-regression.txt` records the original bootstrap's EPERM failure
under those restrictions and the corrected profile's successful HTTP result.

Variant v4 then booted and ran the browser, but `ready` returned HTTP 400. The
guest required Content-Length while AWS used chunked framing. A failing loopback
test reproduced the problem; commit `e31e789` fixes decoding before JSON parsing.
Variant v5 contains that exact server source and passed native invocation as well
as the browser and volume checks. An earlier variant's success is never attributed
to the final image without rerunning its relevant checks.

`InvokeAgentRuntimeCommand` was used only for deterministic synthetic checks on
an explicitly active session. An earlier call against the failed startup timed
out and a replacement EC2 instance appeared under the same session. Command
invocation therefore cannot serve as passive inventory. Use EC2 inspection with
`IncludeManagedResources=True` and the exact capacity/session binding.

The final `StopRuntimeSession` call returned `InternalServerException`; its
outcome remained unknown. No blind stop retry was made. Since all stand data was
synthetic and exported, cleanup uses `DeleteCapacityProviderSession`, which
deprovisions the whole approved stand session. Stop/resume and a guaranteed
interruption deadline remain unqualified.

Final passive verification at 15:43 UTC passed 18 checks covering live compute,
managed and previously observed volumes, interfaces, runtime/capacity/workload
identity, VPC, NAT/EIP, ECR, logs, IAM roles/profile, autoscaling group and launch
templates. No runtime invocation was used to establish resource absence.

## Reproduction boundary

`Dockerfile`, `entrypoint.sh` and `fleet.json` are the final trial overlay. The
single-host capacity value in `fleet.json` is the approved experiment fixture.
Its named `porch` build context must point to this revision's skill directory;
only the changed server module is copied over the qualified parent. Verify the
parent's resolved digest before building. Build on the authorized remote Docker
host with its admission and cgroup policy, for example:

```sh
docker build --platform linux/arm64 --provenance=false \
  --build-context porch=/path/to/skills/pragmatic-orchestration \
  --cgroup-parent=user.slice -t porch-native-trial:verified \
  docs/benchmarks/2026-10-29-agentcore-native-trial
```

The image contains no credentials or customer inputs. Real AWS identities,
resource ids and the disposable operator harness stay in the local experiment
ledger outside git; do not replay it as a deployment tool. Public evidence here
contains only synthetic command frames, hashes, timestamps and cleanup checks.

## Remaining work

Automatic provisioning, durable passive inventory/lifecycle binding,
repository/result exchange and supported unattended native Codex subscription
bootstrap are separate work. Sol/high (`codex`) and Terra/xhigh
(`codex-gpt-6.1-terra`) remain the chosen delegate profiles; neither made a model
turn in this trial. Prepared project dependencies/skills and real browser suites
also need qualification before automatic placement.

Exact billed AWS cost is not yet available. USD 10 was the approved budget, not
an estimate or a claim about the final bill.

Sources checked 2026-10-29: [Instances setup and deletion](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-instances-get-started-cli.html),
[Instances roles](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-instances-security.html)
and [command execution](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-execute-command.html).
