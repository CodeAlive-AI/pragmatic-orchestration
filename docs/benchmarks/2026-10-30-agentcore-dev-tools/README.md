# AgentCore development tools — 2026-10-30

The real ARAI task needed SSH/source transfer and reported an optional native
dependency build failure because the image lacked a compiler. Add OpenSSH
client, rsync and build-essential to the existing guest image. No server,
Docker daemon, network policy, identity or credential is configured by this.

The ARM image built on the shared development VM. Under emulation, UID 1000,
all capabilities dropped, no network and `user.slice`, the actual image ran
Codex and its code-mode host, SSH/rsync/make, and compiled and executed C and
C++ programs. The disposable smoke container was removed. This does not
qualify Chromium under emulation, native cloud execution of the new image,
the project's better-sqlite3 dependency, or the requested bridge itself.

`results.json` binds the source hash, image index and observed results;
`smoke.log` retains the actual output. The version consistency check and
`git diff --check` passed. Runner scripts were unchanged, so the unrelated
full offline orchestration suite was not repeated. No image/release was
published; the local shared-VM image is retained for the bridge qualification.
