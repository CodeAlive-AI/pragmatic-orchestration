# Delegate card

Enough for one worker on one task, external (`porch delegate`) or built-in. For
steering, recovery, groups, durable Codex follow-ups, read-only research and
handoffs read [delegate.md](delegate.md) and [delegate-runtime.md](delegate-runtime.md).

## Brief

```text
Original task (verbatim, or read FILE): "..."
Context and intent (orchestrator's): goal, why this assignment exists.
Working root: /exact/assigned/repository
Deliverable: the result, assessment or implementation.
Scope: ... Not in scope: ...
Invariants: ...
Acceptance criteria: ...
Verification: named checks at named levels; report any risk they miss.
Suggestions (unverified, replace if they fail a requirement): ...
Report: changed files, checks and results, deviations, open issues.
```

Quote, never paraphrase; give requirements, not untested design; point to files
instead of retelling them. `porch delegate` prepends the executor contract. A
built-in worker's brief opens with "Read `<absolute path to prompts/delegate-executor.txt>`
first; it is your contract."

## Less capable workers

For Fable → Opus, Opus → Sonnet, Astra → Muse Spark, Astra → DeepSeek, and
workers of uncertain capability, also add a bounded plan and named pitfalls with
how to handle each, and this block. JOURNAL_PATH is the literal
`docs/tmp/{yyyy.MM.dd}_{task-name}_deviations.md` (placeholders filled; in
Coordinator Mode a separate absolute file outside the repository); read-only
work uses a `Deviations` answer section instead
([details](delegate.md#delegating-to-a-less-capable-model)):

```text
Create JOURNAL_PATH (create docs/tmp if needed). Record every unexpected finding
and plan deviation as it occurs: expected versus observed behavior, file/command
evidence, why a change was needed, action taken or proposed, and remaining risks
or blockers. Keep resolved entries and their resolution. The journal does not
authorize out-of-scope changes. If nothing deviated, record "No deviations."
Return the journal path, changed files, checks and results, and unresolved issues.
```

## Launch

A built-in worker: start it with the harness's own subagent tool (in Claude Code
the Agent tool, choosing the model), brief as above, then collect its result.
An external worker, from the assigned root; name every run:

```bash
RUN_ID=$("$PORCH" delegate -a <id> --detach --name <slug> --prompt-file task.md)
```

Early check, only for long, risky or hard-to-undo work and less capable workers:
within a minute or two run `"$PORCH" delegate events "$RUN_ID" --max-events 50`,
keep `next_cursor`, and correct with one self-contained
`"$PORCH" delegate steer "$RUN_ID" --mode auto --prompt-file correction.md`.
For a built-in worker, inspect its journal and working-tree changes instead.
A short task is reviewed when it completes.

## Wait

- Claude Code: run `"$PORCH" delegate wait "$RUN_ID" --timeout N --json` in the
  background (`run_in_background`); no `sleep`, no polling.
- Choose N from the next check you need and, for a large context, the cache TTL:
  1 hour, one wait up to about 50 minutes; 5 minutes, suggest the user switch to
  1 hour, or wake about every 4 minutes.
- A built-in worker in Claude Code notifies you when it finishes; do not poll it.
- Codex: keep the turn active: `wait` with a deadline of 300-900 seconds; if it
  yields a session handle, resume it with the native wait (at most 60 s each).
- Exit 124 means still running, not failure. Parse JSON from stdout only.

## Accept

Check against the original task, not the worker's tests. Run the required checks
yourself or independently. For subtle requirements use a reviewer from another
model family ([model-routing.md](model-routing.md)). Reconcile the whole journal.
Apply [review-acceptance.md](review-acceptance.md).

## If stuck

See [changing approach](delegate.md#changing-approach-and-preserving-work) and
[exits and recovery](delegate-runtime.md#wait-and-watch-exits).
