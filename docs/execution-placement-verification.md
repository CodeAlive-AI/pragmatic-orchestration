# Existing-host fleet slice: verification, 2026-10-29

This verifies the first implementation, not the full multi-provider MVP.

- Six fleet test cases passed on macOS and Linux: real SQLite concurrent claims,
  request idempotency/conflicts, unknown launch retention, positive terminal release,
  weighted accounting, workspace exclusivity, unavailable observations, explicit-only
  Mac routing, SSH argument quoting, real detached supervisor with a fake backend,
  routed completion and recovery of a lost launch receipt.
- The existing orchestration regression suite passed: 56 tests on the dev VM.
- A separate real SSH smoke launched a fake Codex worker on the dev VM, closed the
  launching SSH connection, and retrieved completion through a subsequent connection.
- A separate native subscription smoke ran Codex CLI 0.165.0 (login status: ChatGPT),
  profile `codex`, GPT-6.1 Sol/high, in an isolated directory. It created
  `porch-smoke.txt` containing `PORCH_NATIVE_SMOKE_OK` plus newline. Independent
  shell inspection verified those bytes, and routed status reported completion.
  No other repository was exposed as its working directory. No API billing route
  was configured by this change. This smoke did not test live steering/interrupt.
- Skills Manager structural review reported zero errors for both existing skills.
  Its existing footprint/style advisories are outside this small routing edit;
  no third skill or duplicate installation was introduced.
- Shell syntax, JSON parsing through CLI tests, and whitespace checks passed.

The initial Linux fixture was missing Porch's prompts directory; copying the
complete source skill fixed fixture setup. A subsequent macOS recovery test
caught a canonical-path mismatch (`/var` versus `/private/var`). Runner metadata
now preserves the submitted workspace identity independently from its resolved cwd.
These were fixed before delivery; the regression covers the lost receipt path.

No cloud resources, production services, global hooks or shared host installation
were created. The dev VM contains only a dedicated task copy and isolated test
state. Its installed Codex was used unchanged; Claude was absent from the inspected
PATH. Native Claude, AgentCore, image/browser qualification, default fleet enrolment
and full workload/concurrency measurement remain unverified.

Full repository suites were not repeated: the focused fleet tests and existing
supervisor/orchestration regressions cover the changed runtime boundary. No browser
or build workload was introduced by this step.
