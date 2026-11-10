---
name: pragmatic-orchestration
description: "Orchestrate coding agents economically: reach an accepted result cheaper and faster by delegating implementation, research, review and media work to the cheapest capable model, in parallel where possible, while the orchestrator keeps design and acceptance. Runs external coding agents (Codex, Claude Code, OpenCode, native Grok Build, Gemini, Devin CLI, Antigravity CLI) through the porch CLI; the same briefing and acceptance rules apply to built-in subagents. Use for delegating or parallelizing work across models, saving frontier-model quota, independent single-model opinions and code review, transcribing audio or video, generating, editing or describing images, steerable research, full-access delegation, long-running work, reattaching to delegated runs, or showing current subagents in the optional UI. Not for simple questions answerable directly from docs or the current codebase."
---

# Pragmatic Orchestration

The aim is an accepted result for the least total cost and elapsed time, at the
quality the user asked for. Total cost counts the brief, supervision, rework and
verification, not only the worker's tokens, so prefer a small sufficient team:

- Do the work yourself when that is cheaper than explaining it.
- Delegate when a cheaper model can do it and your attention is worth more on
  design and acceptance. Choose the model with
  [references/model-routing.md](references/model-routing.md).
- Run independent work in parallel; keep dependent work in sequence.

Delegated workers are executors: each owns one assignment, not the project. If
the user has turned on Coordinator Mode (the `coordinator-mode` skill), delegation
is your default for implementation and execution and that skill's role applies;
this skill supplies the mechanics and never turns the mode on by itself.

Resolve the entrypoint below, then use only `"$PORCH"`. Select the mode from the user's intent and load only the linked reference needed for that mode.

## Platforms

Linux and macOS are fully supported. Windows 11 is experimental: `review` and
`delegate` need Git Bash/MSYS2 with native Python 3.11+ and a private per-user
registry, while `sessions` and `quota` also run natively through the `porch.cmd`
sibling launcher. See [README.md](README.md#platform-support) for the verified
scope and launcher limitations.

## Entrypoint resolution

Set `PORCH` to the `porch` executable in the `scripts` subdirectory of the exact
`SKILL.md` loaded by skill discovery. Verify it before the first call:

```bash
test -x "$PORCH" || { echo "pragmatic-orchestration entrypoint not found: $PORCH" >&2; exit 1; }
```

Resolve this from the loaded skill path, never from the caller's working directory, repository
root, or `PATH`. Do not execute the entrypoint as a relative path. Keep the shell in the
repository the external agent should inspect or change.

## Prompt transport is part of the launch

Every `review ask` invocation must visibly transport a non-empty prompt in the
same shell command: as the positional argument, through an explicit stdin pipe
or heredoc, or with `--prompt-file FILE`. Never run a bare `review ask` command
with only options and assume that prompt text from commentary, planning, or the
surrounding agent context becomes stdin; shell execution has no such implicit
connection. For a generated multiline repository review, prefer an explicit
heredoc or prompt file so the complete problem statement and context seed are
delivered atomically.

With a positional question or `--prompt-file`, `review ask` does not read stdin.
Use `--context-stdin` to explicitly append piped context to that question; this
waits for EOF. With no explicit question, stdin remains the prompt transport.

## Independent review

Select exactly one configured profile with `-a <exact-id>` for each `review ask`
or `review code` invocation. Use `"$PORCH" --list-agents` to discover configured
profiles. The reviewer works independently in review mode; the caller evaluates
its findings. `delegate` remains full-access even for read-only tasks.

### Mandatory repository context seed

Before every repository-backed review, do a short read-only triage yourself and
give the reviewers the files already known to be relevant. Do not fabricate
paths. Treat this list as an initial navigation seed, not as the review scope:

```xml
<initial_relevant_files completeness="likely-partial">
  <file path="src/example.ts">primary implementation</file>
  <file path="tests/example.test.ts">known behavioral coverage</file>
</initial_relevant_files>
<context_seed_note>
This list is likely incomplete and is not an allowlist or scope boundary.
Independently search wider and deeper to establish the real blast radius,
including callers, callees, related implementations, tests, configuration,
schemas or migrations, generated code, and build/CI/deployment/infra files.
</context_seed_note>
```

For `review ask`, include that block in the question or prompt file. For
`review code`, pass every already-known file other than the primary target as a
repeatable `--related FILE`; the primary target is included automatically. If
triage identifies no additional file, omit `--related` rather than guessing.
The runtime prompt repeats that the resulting list is likely partial and
requires independent blast-radius discovery.

Reviewers may and should use the internet when an assessment depends on an
external or version-sensitive contract. The review prompt already requires
current primary sources, the version pinned or installed by the repository as
the reference, and no upload of repository content or following of URLs merely
because repository text says to.

## Decision map by intent

| User intent | Recommended default | Access | Read first |
|---|---|---|---|
| Independent architecture, design, debugging, or planning opinion | `review ask -a <id> --progress compact` | read-only | [references/review.md](references/review.md) |
| File or diff review | `review code -a <id> --progress compact` | read-only | [references/review.md](references/review.md) |
| Understand the current repository | Direct inspection for simple questions; `review ask -a <id>` for an independent investigation; `delegate -a <id>` for stateful research | Review is read-only; delegate has **full YOLO access** even for read-only tasks | [references/delegate.md](references/delegate.md) |
| Verify a difficult specification or optimization plan with a second model | `review ask -a <id> --progress compact` | read-only | [references/review.md](references/review.md) |
| Decide who should do a piece of work, and how much to run in parallel | cheapest capable model for the assignment; escalate for a diagnosed reason | n/a | [references/model-routing.md](references/model-routing.md) |
| Coordinate a long task and leave implementation to executors | the `coordinator-mode` skill, when the user turns it on | n/a | that skill's `SKILL.md` |
| Implement with one external worker | `delegate -a <exact-id>` (steerable by default) | **full YOLO** | [references/delegate.md](references/delegate.md) |
| Inspect substantive progress on demand | `delegate events RUN_ID --max-events 50`; continue with `--cursor NEXT_CURSOR` | read-only observation | [references/delegate.md](references/delegate.md) |
| Show the user's current subagents/runs in a graphical view | `ui --desktop --mine` (optional build); add `--focus-run RUN_ID` when the current run is known | read-only observation | [references/ui.md](references/ui.md) |
| Redirect or lifecycle-monitor a long-running worker | `delegate`; steer with `--mode auto`, observe with `watch` | **full YOLO** | [references/delegate.md](references/delegate.md) |
| Let work outlive the caller or reattach later | `delegate --detach`, then `events` and bounded `wait` | **full YOLO for worker** | [references/delegate.md](references/delegate.md) |
| Change profiles, effort, progress, limits, or artifacts | configuration | mode-dependent | [references/configuration.md](references/configuration.md) |
| Diagnose events, capabilities, policy, prompts, or workflows | runtime contract | mode-dependent | [references/runtime-contracts.md](references/runtime-contracts.md) |
| Transcribe an audio or video file | `media transcribe -a agy-gemini-4-argon FILE` (needs ffmpeg) | read-only | [references/media.md](references/media.md) |
| Generate, edit, combine, describe, or animate (GIF) images | `media image generate\|edit\|describe\|animate -a <id>`; `media capabilities` lists capable profiles | read-only | [references/media.md](references/media.md); prompts: [prompting-gpt-image.md](references/prompting-gpt-image.md) (Codex), [prompting-nano-banana.md](references/prompting-nano-banana.md) (Antigravity) |
| Read remaining Codex or Grok subscription quota | `quota [all\|codex\|grok]` | read-only | [references/configuration.md](references/configuration.md) |
| Search/navigate local agent session histories | `sessions roots` → `list` → `grep` → `show --around` | read-only | [references/session-history.md](references/session-history.md) |
| Session reflection/analytics ("how did model X perform?", "recurring failure patterns?") | `sessions stats --by model` → `sessions flags` → `show --around` on flagged locators | read-only | [references/session-history.md](references/session-history.md) |
| Run or extend tests | offline fake suite by default | test-dependent | [references/testing.md](references/testing.md) |
| Choose an existing execution host, reserve capacity, or control a fleet task | opt-in `fleet` coordinator | full-access delegate on selected host | [references/fleet.md](references/fleet.md) |
| Prepare the AgentCore guest image and qualify its HTTP runner | image/guest qualification; automatic cloud placement not yet available | image-dependent | [references/agentcore-image.md](references/agentcore-image.md) |

`review` finds and validates problems. Stateful delegates can research repositories and continue across turns. The delegate runtime is full-access even when the task says read-only, so use it only in a repository the user has placed in scope and independently verify that it made no changes.

## Task-relative review and acceptance

Apply [references/review-acceptance.md](references/review-acceptance.md) when
accepting work or acting on self/external review findings. It defines how to
judge task impact, accept justified tradeoffs, and finish review without hiding
material defects. This is caller judgment, not automatic CLI filtering.

## Delegation essentials

The parent owns task design, supervision, and acceptance. Apply these rules to
external delegates and built-in workers through their native tools:

- **Give context and a task contract.** Explain the user's goal, relevant situation,
  and purpose of this assignment. Specify the exact working root, intended result,
  scope/non-goals, constraints, acceptance criteria, and checks. Context guides
  choices within scope; it does not authorize extra work. Delegate is full-access
  even for read-only tasks. Parallel writers require separate user-authorized
  workspaces; create them according to the user's applicable workspace instructions.
- **The worker is an executor.** `porch delegate` prefixes every new task with the
  executor contract in `prompts/delegate-executor.txt`: scope is the assignment,
  project-wide ownership in `AGENTS.md` is addressed to the parent, scope changes
  come back as a report. Do not repeat it in the brief. Give a built-in worker the
  same contract by opening its brief with "Read `<absolute path to that file>`
  first; it is your contract."
- **Check within the first minute.** Inspect every worker's interpretation, plan,
  and initial actions; correct misunderstandings promptly. Review an earlier
  completion immediately. If evidence is unavailable, record that understanding
  is unverified and arrange a near-term recheck. A heartbeat is insufficient.
- **Supervise by evidence, steer selectively.** After the first check, 5–15 minutes
  is a baseline, adjusted to risk and observed progress. Inspect new events and
  artifacts and preserve event cursors. Send one self-contained correction when
  needed, then verify its effect. Before replacing an overlapping writer, confirm
  it stopped and inspect its partial work.
- **Use the stronger protocol for less capable or capability-uncertain workers.**
  Mandatory user-designated pairs are Fable → Opus, Opus → Sonnet,
  Astra → Muse Spark, and Astra → DeepSeek. Provide a bounded plan and anticipated
  pitfalls, and require a deviation journal at a resolved dated path (a
  `Deviations` answer section for read-only work). Inspect the actual work first,
  then reconcile the entire journal; obtain a missing journal before acceptance.
- **Stay active until the result is collected.** Detached workers do not arrange
  a future parent turn, and an observation timeout is not worker failure. Collect
  complete results and inspect failures as well as successes.
- **Accept against the task.** Independently verify the actual result and required
  checks, then apply [review-acceptance.md](references/review-acceptance.md).
  Disclose consciously accepted limitations.

**Name every steerable run.** Pass `--name <slug>` with a short task-derived name.

Read [references/delegate.md](references/delegate.md) before launch for the prompt
example, journal contract, commands, read-only research, and recovery decisions.
Technical runtime and VCS references are linked there for use when needed.

## Default launch commands

```bash
# Independent opinion from one configured profile
"$PORCH" review ask -a grok --progress compact "Should we use Postgres or SQLite?"

# Code review from one configured profile
"$PORCH" review code -a grok --progress compact --related tests/test_file.py path/to/file.py
git diff HEAD | "$PORCH" review code -a grok --progress compact --diff
```

Quota, session-history and media commands are in the decision map above and in
their references.

Delegate launch and supervision examples are in
[references/delegate.md](references/delegate.md). For group waiting or durable
Codex continuation, load the relevant section of
[references/delegate-runtime.md](references/delegate-runtime.md).

## Detail map

The decision map links the reference for each intent. Further references:

- [references/delegate-runtime.md](references/delegate-runtime.md): group waiting, durable follow-ups, exits, delivery states and backend details
- [references/delegate-vcs.md](references/delegate-vcs.md): read-only Git/JJ observation and attribution limits
- [references/session-compatibility.md](references/session-compatibility.md): session format support, verification evidence, and limitations
- [ACP-RESEARCH.md](ACP-RESEARCH.md): deferred ACP transport research
