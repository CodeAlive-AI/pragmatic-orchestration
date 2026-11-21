# Proposals: independent designs, comparison, synthesis, check

Stages 3 and 4 in detail.

## Contents

- [The package](#the-package)
- [Independence](#independence)
- [Launching architects](#launching-architects)
- [What a proposal contains](#what-a-proposal-contains)
- [Research by the architects](#research-by-the-architects)
- [The follow-up round](#the-follow-up-round)
- [Architects that work alone](#architects-that-work-alone)
- [A guarantee by mechanism](#a-guarantee-by-mechanism)
- [Comparing](#comparing)
- [Experiments](#experiments)
- [Choosing and synthesising](#choosing-and-synthesising)
- [The check](#the-check)
- [Handling objections](#handling-objections)
- [When a worker fails](#when-a-worker-fails)

## The package

Every architect gets one frozen version of the same package:

- the original request word for word, and every clarification in force;
- the requirements, the musts, and the quality scenarios;
- a short map of the system: flows, boundaries, owners of data, consumers;
- the decisive evidence with locators;
- the disputed premises and the known gaps;
- pointers to the primary sources.

The package is a file in the work folder with a version in its name, and its
text is pasted into every brief: a worker may be unable to read a path outside
the repository. It is not the only permitted truth: each architect has read
access to the repository, must name the premises that could change its design,
and may check or dispute them.

## Independence

- **The same brief for all, and no assigned position.** Architects are strong
  models chosen for their own judgment: each designs freely, bound by the
  requirements and by what the system is. Telling one of them to argue for a
  given kind of solution trades an independent design for a role, and the
  differences between proposals then come from the briefs instead of the
  models. The simple pole stays in the comparison another way: every brief
  asks for the simplest admissible alternative and the exact reason against
  it, and the smallest change to the current system is the baseline you
  compare against.
- Before the first proposal no architect sees another's design, a ranking, or
  the answer you prefer. Shared facts are allowed; a hint at the conclusion is
  not.
- Inherited context counts. A new `porch` run starts a fresh session, clean of
  your notes; a forked subagent that has read them is not. If independence
  cannot be ensured, say so in the journal and do not count matching
  conclusions as independent confirmation.
- Different model families reduce shared mistakes and do not exclude them.
- Architects read the same repository instructions and may all inherit one
  local habit. A choice they share because the project's conventions suggest it
  counts as one opinion, not three.
- A profile can carry its own role text (see the profile in the
  configuration). `--assignment` leaves it out, so the brief alone sets the
  architect's role. A review run without that flag gets it: a profile tuned
  for code audit then answers a design assignment as an auditor.
- Do not write your own design before theirs are in. Reading three designs
  after committing to a fourth turns comparison into defence.

## Launching architects

Fill [architect-brief.md](../assets/architect-brief.md) once and give every
architect the same text. It holds the working root, the read-only limit, the
package, the research budget, the design criteria and the sections of the
answer. `PORCH_PATH` in it is the absolute path of `porch`.

An architect is a read-only review worker that may start read-only researchers
(`--researcher`) and answers in the sections of the brief (`--assignment`):

```bash
"$PORCH" --list-agents
cd /absolute/path/of/the/repository/to/read
git status --porcelain=v1 > "$WORK/status-before.txt"      # jj: jj status
PORCH_RUN_DIR="$WORK/run-architect-A" "$PORCH" review ask -a <exact-id> \
  --researcher <cheap-id> --assignment --progress compact \
  --prompt-file "$WORK/brief.md" \
  > "$WORK/proposal-A.md" 2> "$WORK/progress-A.log"; echo "exit=$?"
```

- One call per architect, each in the background, all started before any is
  read. The call ends when the answer is complete: exit `0` with the proposal
  on stdout, `3` a backend failure or an empty answer (a failed run can still
  leave text in the output file), `4` a refused or misconfigured request.
- **Which researcher profiles.** Name one cheap profile of the architect's own
  backend: on Codex, Claude Code, OpenCode and Grok Build its model runs the
  harness's read-only subagent. Claude Code, OpenCode and Antigravity
  architects also reach profiles of any backend through a nested
  `porch review ask`, so more `--researcher` flags are useful there, and for
  Antigravity that is the only route; Codex and Grok Build architects reach
  their own backend only. The contract around the brief lists the channels
  this run really has, `porch` prints a note for a profile it cannot reach,
  and it refuses the run when it can reach none. Routes per backend:
  `references/review.md` of `pragmatic-orchestration`.
- **There is no run id.** `PORCH_RUN_DIR` names the run folder: `meta.json` in
  it holds the start time and the process id, `normalized/` the events, and
  the final answer is saved there as well as in the output file. Write the
  profile, the brief, the run folder and the output file into the journal
  before launching. Starting a second copy of a frontier-model run because the
  first was forgotten is the costly mistake here.
- Set reasoning effort through the profile or a supported override (see
  `configuration.md` of `pragmatic-orchestration`), not with prose in the
  brief. A profile left at low effort answers from the brief without opening
  the code and without using its researchers.
- Within the first minutes read the progress log and the events in the run
  folder: is it reading the code (tool calls appear), and is it designing or
  reviewing the brief? A review run cannot be steered: stop a run that went
  wrong and start a new call with the correction in the brief.

### The fallback: `delegate`

`porch` refuses `--researcher` (exit `4`, before anything starts) for a backend
that cannot keep researchers read-only: today Gemini CLI, Devin CLI and Cursor
CLI. Run such an architect through `delegate`, or let it
[work alone](#architects-that-work-alone). Choose `delegate` also when you need
what a review run lacks: a run id with events and a steer, or a Codex session
that a follow-up can continue.

```bash
RUN_A=$("$PORCH" delegate -a <exact-id> --detach --name design-a --prompt-file "$WORK/brief.md")
"$PORCH" delegate wait "$RUN_A" --timeout 2400 > "$WORK/proposal-A.md"; echo "exit=$?"
```

- Run each `wait` in the background; exit `124` means the run is still going,
  so wait again and do not read the file as a proposal yet. Exit `0` is
  completed, `74` completed without answer text, anything else a failure.
- For a Codex profile add `--persist-session`, so that a follow-up can continue
  the same session.
- Write the run id into the journal as soon as `delegate` prints it. After a
  compaction, `"$PORCH" delegate list --active` shows what is still running.
- Watch with `"$PORCH" delegate events "$RUN_A" --max-events 50`, read the
  effort from `"$PORCH" delegate status "$RUN_A" --json`, and correct once with
  a self-contained `"$PORCH" delegate steer "$RUN_A" --mode auto --prompt-file
  FILE`. Here nothing but the brief limits what the architect starts.

The two routes side by side:

| | `review ask --researcher --assignment` | `delegate`: the fallback |
| --- | --- | --- |
| Writes | Forbidden by contract; Codex and Grok also run in a read-only sandbox; Claude loses its editing tools; elsewhere a shell remains | Possible: full access, no sandbox. Only the brief forbids them |
| Starting other agents | Only read-only researchers, through the channels the contract lists; other agent tools are switched off where the backend can | Whatever the assignment asks for |
| Researcher models | The named profiles: own backend as a subagent, others through a nested `porch` where the shell allows | Whatever the brief names |
| Run id, events, wait, steer | No: a run folder, a progress log and an output file | Yes |
| Role and answer format | Set by the brief | The executor contract, then the brief |
| After completion | One-shot | A Codex run with `--persist-session` can be continued; others cannot |
| Skills | Off in Claude; uncertain elsewhere | Whatever the harness has installed |

On every route compare the repository with the snapshot when each run ends.
Only the Codex and Grok sandboxes make a write impossible; a Claude, OpenCode
or Antigravity review worker keeps a shell, and a `delegate` run has full
access.

```bash
git status --porcelain=v1 | diff "$WORK/status-before.txt" -
```

Any difference goes to the user with the name of the run. Do not revert it and
do not accept it on your own: with several architects in one checkout you may
not know whose it is.

Check each result when it arrives. The exit code first, then the content: is
it a whole design on the given requirements? Does **Deviations** say what the
status comparison shows? A proposal that answers a different question goes
back once with a precise correction.

## What a proposal contains

- the chosen model of the domain, the data, the state and the algorithm;
- boundaries of responsibility, owners of data, the source of truth, contracts
  between parts;
- the mechanism behind each must and invariant, as a property card where a
  promise is at risk;
- effects, order of operations, partial failures, unknown outcomes, recovery;
- why existing mechanisms are reused, why each new abstraction exists, why
  duplication is kept where it is;
- the path from the current system: compatibility, migration, the order of
  steps;
- the simplest admissible alternative it rejected, the trade-offs, residual
  risks, the assumptions the design rests on;
- the research it did, with every fact the package lacks;
- the checks and early experiments it needs, and the conditions under which
  the design must change.

The smallest admissible change to the existing system is the baseline of every
comparison. "Change nothing" is a candidate only when it really answers the
task. Weak alternatives invented for the count are noise.

## Research by the architects

An architect reads the code itself and may start read-only researchers on
cheap models for wide searches: its harness's own read-only subagent on the
model of a `--researcher` profile, or a nested `porch review ask` on such a
profile. In a `delegate` run the brief names the profiles instead. Three
reasons to let it:

- a fact is needed in the middle of a design, and a request to you would cost
  a second run of a frontier model;
- wide searching on a cheap model keeps the architect's context for the design;
- architects looking from different sides find dependencies the first research
  missed, which is half of what this stage is for.

What keeps it in hand:

- **A budget in the brief:** how many researcher runs in total and at once, and
  on which profiles. Size it to the task; three to six runs is a usual start.
  It is part of the limit on the whole tree, and a number in a prompt limits
  nothing by itself: look at the events of a long run.
- **Only researchers.** An architect starts no implementer, no second architect
  and no review of its own design. With `--researcher` the contract says so
  and `porch` cuts the tools down; in a `delegate` run only the brief does.
- **A researcher's answer is a lead.** The architect rereads at the source
  whatever its design depends on.
- **The research is reported:** each question, who answered, what came back,
  what the architect checked, and every fact the package lacks, with a locator.
- **You verify and share.** A fact one architect found is unknown to the others.
  Reread the ones that decide something, write them into `RESEARCH.md`, and
  send them to all (next section). Designs that rest on different facts are
  not yet comparable.

What an architect cannot reach comes back as a request to you: a closed source,
a decision of the user, an experiment that writes.

```text
Unknown     what is not known
Decides     which design decision depends on it
Look        where the answer is likely to be found
Enough      what would count as a sufficient answer
Limit       how deep to go
```

Merge duplicate requests across architects, answer them, and check the answers
that decide something. Before an extra search, name the possible answers and
how each would change the decision. If no realistic answer changes the choice,
the guarantee or the next check, postpone it.

## The follow-up round

A material new fact goes into `RESEARCH.md` first, the package gets a new
version, and the fact goes to **every** architect whose proposal is still being
compared, in the same words: the neutral fact, its source, its limits. Never
another architect's design, and never which architect found it. If no new fact
could change a design, skip the round.

How it reaches an architect depends on its run:

- **A review run, and every `delegate` run but the one below,** cannot be
  reopened. Start a new one, launched the same way, whose prompt holds
  everything again: the original brief with the package updated and every
  clarification in force, so that a must missed the first time can still be
  found; the architect's own first proposal, verbatim; the new facts, marked as
  new; and the request.
- **A completed Codex `delegate` run started with `--persist-session`** keeps
  its memory. Continue it: `"$PORCH" delegate -a <same-id> --continue-run
  "$RUN_A" --detach --name design-a-2 --prompt-file FILE`, where the file holds
  the new facts, what changed in the package, and the request below.

The request is the same in both cases: "Give your complete design again,
updated for these facts, and list what changed and why; or say why it stands
unchanged." A whole document is needed, because the comparison and the checker
work on whole designs.

One follow-up round is the default. A second is justified by a fact that changes
admissibility. Compare proposals on agreed inputs; a local update does not
restart the whole process.

## Architects that work alone

A profile that can neither start read-only researchers nor be delegated to
(Gemini CLI is review-only) still serves as an architect, alone:

```bash
cd /absolute/path/of/the/repository/to/read
PORCH_RUN_DIR="$WORK/run-architect-A" "$PORCH" review ask -a <exact-id> --assignment \
  --progress compact --prompt-file "$WORK/brief.md" \
  > "$WORK/proposal-A.md" 2> "$WORK/progress-A.log"; echo "exit=$?"
```

The brief stays the same: its research section tells an architect whose
contract forbids every subagent to start nobody and to return what it lacks as
research requests. You answer the requests with your researchers, and the
follow-up round carries the answers. Say so in the comparison: this architect
designed with less research.

## A guarantee by mechanism

When the user or the project requires that nothing can be written by mechanism,
choose architects on backends with a sandbox: Codex CLI and Grok Build. Their
researchers are the harness's own subagents inside the same read-only sandbox,
so the whole tree is covered, and no nested `porch` runs. On Claude Code,
OpenCode and Antigravity the same flags switch the writing tools off and leave
a shell, held by the contract. Never use `delegate` under this requirement. If
it leaves fewer model families than the route calls for, tell the user.

## Comparing

Normalise the proposals first: the same requirements, inputs, load and
transition conditions. Make differing assumptions explicit before comparing
anything else.

**Musts first.** For each proposal and each must, one of three:

| State | Meaning |
| --- | --- |
| Justified | A mechanism is given, and the reasoning or evidence for it holds |
| Violated | A concrete sequence of events breaks it |
| Unverified | It rests on a premise nobody has checked |

A justified argument, the result of an experiment and a check of finished code
are different kinds of evidence; say which one you have. A violated must removes
the proposal unless the violation can be repaired without changing its base.

**Then the trade-offs**, on the same scenarios:

- clarity of the model: the number of concepts needed and of exceptions;
- ownership of state, cohesion, consistency boundaries, what callers must know;
- the next plausible changes, how local their effect is, the cost of removing
  or replacing the solution;
- failure, diagnosis, recovery and the real reach of a failure;
- implementation, integration, migration, operation, upkeep;
- residual uncertainty, and how far the choice depends on disputed premises.

Write a table of arguments and consequences. A sum of scores hides the one
difference that matters. Remove the clearly dominated proposals, then explain
the preference among the rest. When the choice is close, test whether it flips
under a plausible change in the estimates; if it does, the deciding fact is
what to find out next.

Put the smallest admissible change to the current system into the table as a
row of its own, built from the alternatives the architects rejected and from
your own reading. If every proposal is heavier than that row, each must show
what the extra machinery buys: the outcome it prevents or the change it makes
cheaper.

Judge elegance by the method: few independent concepts, special cases that
disappear, a change path that is easy. The neatest document is not the neatest
design.

## Experiments

When the choice hangs on a premise nobody has checked, run the smallest
permitted experiment: a reproduction, a profile, a check of an API version's
behaviour, a rehearsal of a migration, a prototype of one joint, a controlled
order of events. State beforehand the question, the possible conclusions and
the limits of the evidence.

One experiment deserves to be tried early: a throwaway attempt at the direct
change. Make it naively in an isolated copy, let the compiler and the tests
list what breaks, keep the list, discard the diff. Where the build is fast,
those minutes find dependencies that no amount of reading does, and the list
goes to every architect as a fact. An experiment that writes anything runs through an
executor in an isolated workspace, under the user's rules for such work, with
its writes, outside calls, cost and cleanup in the brief. Verify the minimum; do
not replace design with a full implementation of every candidate.

## Choosing and synthesising

Prefer one coherent proposal with targeted improvements. Synthesise only when
the gain can be named.

Before combining, fix the base: the source of truth, ownership of data, the
state model, consistency boundaries, retry semantics, errors and recovery. For
each borrowed part, check that its premises are compatible with that base.

Combinations that break quietly:

- delivery with retries, and a handler that is not idempotent;
- an asynchronous write, and a promise of immediate consistency;
- a new data format, and old readers;
- an optimistic update, and cancellation;
- two owners of one invariant.

A substantial synthesis is a new candidate. Nobody has designed it as a whole
yet, including you.

## The check

Give the chosen design to a checker that neither wrote it nor made the
synthesis, with [checker-brief.md](../assets/checker-brief.md): the design, the
request and clarifications, the requirements, `RESEARCH.md`, the versions, and
what the synthesis changed. The checker judges the whole design on the scenarios
that matter, with extra attention to the changed joints. A local check of a
borrowed fragment is not enough when the shared invariant has changed.

The checker works alone and read-only through plain `review ask`, without
`--researcher` and `--assignment`: the role of an independent adviser and the
five answer headings are set by `porch`, and its brief already maps the report
onto them. Launch it like an architect, with its own `PORCH_RUN_DIR`, output
file and progress log.

The checker returns confirmed violations, material hypotheses and non-blocking
suggestions separately, each objection as: the requirement or invariant, the
source, a realistic sequence of events, the consequence, a way to resolve it.
No quota of findings. An empty report is not proof that nothing is wrong; it is
one reader who found nothing in the stated area.

Use another model family than the author of the base where the configuration
allows it.

## Handling objections

Each objection gets exactly one outcome, recorded with its reason:

| Outcome | When |
| --- | --- |
| Fix | A confirmed violation |
| Test | A material hypothesis that an experiment or a reading can settle |
| Accept | A real trade-off that is admissible; say why |
| Reject | Refuted, duplicate or immaterial; say why |

A counterexample that depends on a disputed assumption becomes a check of that
assumption. New matters of taste do not open another round. If several fixes do
not resolve one problem, the premise under it is wrong: go back to the model
instead of improving the wording.

Apply `review-acceptance.md` of `pragmatic-orchestration` when judging findings.

## When a worker fails

| Symptom | Likely cause | Response |
| --- | --- | --- |
| A review of the brief instead of a design | The brief read as a request for an opinion, or `--assignment` was left out | One correction naming the deliverable: a new call (a steer while a `delegate` run is going) |
| A design for an easier problem | A must was dropped silently | Return it with the must quoted; do not compare it as is |
| All proposals look alike | The package hinted at an answer, or the task has one sound design | Reread the package for hints; if clean, record that the choice is narrow |
| Confident claims about code without locators | The architect did not read the source | Ask for locators; verify the ones that decide |
| The proposal says the repository could not be read, or the events show no tool calls | Effort too low, or the model answered from the brief | Check the effort; one correction saying it can and must read the working root, then the complete design again |
| The answer stops mid-way or is empty | Backend limit or failure | Recover what was produced, then rerun once; a different profile if it repeats |
| Proposals rest on different facts | Each architect researched on its own | Verify the facts, send them to all; compare after the follow-up |
| The repository differs from the snapshot after a run | An architect wrote despite the brief | Show the user the difference and the run; neither revert nor accept it yourself |
| An architect starts more researchers than its budget | The limit was read as advice: `porch` does not count the runs | Stop the run and start it again with the budget restated; a `delegate` run gets one steer first and is cancelled if it continues |
| `porch` exits `4` on `--researcher` | The backend cannot keep researchers read-only, or no named profile is reachable from it | Name a profile of the architect's own backend; otherwise the fallback or an architect that works alone |
