# AgentCore guest image qualification, 2026-10-29

Implemented locally, not deployed to AWS:

- A standard-library HTTP service on port 8080, with AgentCore `/ping` and
  `/invocations`, calls the existing Porch fleet CLI. It does not introduce
  another agent protocol or task scheduler.
- Ready distinguishes HTTP/runner readiness from native subscription login.
  New task input is refused when native Codex subscription authentication is
  unavailable. The current image profile supports Codex only.
- A typed channel checks protocol/session/boot identity and never silently
  replays an unknown outcome. Session identity persists in the work volume;
  process boot identity changes on restart.
- Six loopback HTTP/channel tests pass: real detached supervision with a fake
  backend/auth status, idempotent launch, reconnect/result collection, missing
  subscription, wrong session, changed boot and no implicit retry.

The ARM64 image was built on the existing x86 dev VM using its existing remote
Docker context and emulation, under `dev-heavy` and `user.slice`. No Docker engine
was started on the Mac. No image was pushed and no AWS runtime was provisioned.

Qualification image manifest-list digest:
`sha256:201e5c16cdd05e22a706018e07e835ad2ae74a5d7106825036a3d78c3188a799`.
ARM64 image manifest:
`sha256:9c53645fc0aca6af9016877970721819df28401d9e8c66f69df398f3cae70664`.
These identify the local qualification build, not an ECR or published artifact.

Inspected tools in the image:

| Tool | Observed version |
| --- | --- |
| Python | 3.14.10 |
| Node | 24.22.2 |
| npm | 11.19.1 |
| Codex CLI | 0.165.0 |
| Chromium | 156.0.8078.47, Debian bookworm |

Codex assets are fetched from the official 0.165.0 release and checked against
GitHub's SHA-256 asset digests. Base-image tags resolve during the build; its
provenance is retained with the build metadata. This is a qualification recipe;
a release must pin the accepted base-image digests and complete dependency pins.

A container with no network and an empty private work volume answered `/ping`
with `Healthy` and `ready` with `runner_ready: true, subscription_ready: false`.
No model was contacted. This verifies the actual container entrypoint, not only
an in-process HTTP fixture. Its qualification container was removed.

The first version-inspection attempt raced the image-tag replacement during a
rebuild and saw Docker content-not-found; it was repeated after the final build
completed. Tool version checks and HTTP checks then passed against the final tag.

**Browser qualification failed.** The real ARM64 Chromium headless process under
QEMU could not launch its GPU subprocess (`error_code=1002`, `GPU process isn't
usable`). It produced no DOM. The crashed test container did not exit promptly
and was explicitly removed after matching its exact image and timeout/Chromium
command. No other container was stopped. This is neither a passing browser smoke
nor proof that native ARM has the same problem; the cause is not established.
No alternate platform or weakened browser configuration was substituted.

Next live qualification needs a separate native ARM AgentCore stand. Before
turning on automatic overflow it also needs controller lifecycle/passive inventory,
repository and result exchange, and supported unattended subscription bootstrap.
The image has no baked-in native auth and does not reuse ARAI production services.
