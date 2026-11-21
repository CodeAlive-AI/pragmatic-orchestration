# Proposals: independent designs, comparison, synthesis, check

Stages 3 and 4 in detail.

## Contents

- [The package](#the-package)
- [Independence](#independence)
- [Launching architects](#launching-architects)
- [What a proposal contains](#what-a-proposal-contains)
- [Research by the architects](#research-by-the-architects)
- [The follow-up round](#the-follow-up-round)
- [Strict read-only architects](#strict-read-only-architects)
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
- A profile can add its own role text to the prompt (see the profile in the
  configuration). Read it before choosing an architect: a profile tuned for
  code audit answers a design assignment as an auditor.
- Do not write your own design before theirs are in. Reading three designs
  after committing to a fourth turns comparison into defence.

## Launching architects

Fill [architect-brief.md](../assets/architect-brief.md) once and give every
architect the same text. It holds the working root, the read-only limit, the
package, the research budget, the design criteria and the sections of the
answer. `PORCH_PATH` in it is the absolute path of `porch`; `SCRATCH_DIR` is a
folder outside the repository.

```bash
"$PORCH" --list-agents
cd /absolute/path/of/the/repository/to/read
git status --porcelain=v1 > "$WORK/status-before.txt"      # jj: jj status
RUN_A=$("$PORCH" delegate -a <exact-id> --detach --name design-a --prompt-file "$WORK/brief.md")
"$PORCH" delegate wait "$RUN_A" --timeout 2400 > "$WORK/proposal-A.md"; echo "exit=$?"
```

- One `delegate` per architect, all started before any is awaited. Run each
  `wait` in the background; exit `124` means the run is still going, so wait
  again and do not read the file as a proposal yet.
- For a Codex profile add `--persist-session`, so that a follow-up can continue
  the same session.
- Set reasoning effort through the profile or a supported override (see
  `configuration.md` of `pragmatic-orchestration`), not with prose in the brief,
  and read the effort `"$PORCH" delegate status "$RUN_A" --json` reports. A
  profile left at low effort answers from the brief without opening the code.
- Write the profile, the brief and the run id into the journal as soon as
  `delegate` prints the id. After a compaction, `"$PORCH" delegate list
  --active` shows what is still running. Starting a second copy of a
  frontier-model run because the first was forgotten is the costly mistake
  here.
- Within the first minutes look at `"$PORCH" delegate events "$RUN_A"
  --max-events 50`: is it reading the code (tool calls appear), is it designing
  or reviewing the brief, and is it starting only researchers? Correct once
  with a self-contained
  `"$PORCH" delegate steer "$RUN_A" --mode auto --prompt-file FILE`.

Why architects run in `delegate` and the others in review mode:

| | `delegate`: architects | `review ask`: checker, your researchers |
| --- | --- | --- |
| Starting other agents | Allowed when the assignment asks for it | Forbidden by the review contract, and the tools are switched off where the backend can |
| Writes | Possible: full access, no sandbox. Only the brief forbids them | Forbidden by contract; Codex also runs in a read-only sandbox; Claude loses its editing tools; a shell remains |
| Run id, events, wait, steer | Yes | No: a run folder and an output file |
| Role and answer format | The executor contract, then your brief | An independent adviser under five fixed headings |
| After completion | A Codex run with `--persist-session` can be continued; others cannot | One-shot |
| Skills | Whatever the harness has installed | Off in Claude; uncertain elsewhere |

Because writes are possible, compare the repository with the snapshot when each
run ends:

```bash
git status --porcelain=v1 | diff "$WORK/status-before.txt" -
```

Any difference goes to the user with the name of the run. Do not revert it and
do not accept it on your own: with several architects in one checkout you may
not know whose it is.

Check each result when it arrives. The exit code first: `0` is completed, `74`
completed without answer text, anything else a failure. Then the content: is
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
cheap models for wide searches: its harness's own read-only subagents, or
`porch review ask` on a profile the brief names. Three reasons to let it:

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
  and no review of its own design.
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

- **A completed Codex run started with `--persist-session`** keeps its memory.
  Continue it: `"$PORCH" delegate -a <same-id> --continue-run "$RUN_A" --detach
  --name design-a-2 --prompt-file FILE`, where the file holds the new facts,
  what changed in the package, and the request below.
- **Any other run** cannot be reopened. Start a new one whose prompt holds
  everything again: the original brief with the package updated and every
  clarification in force, so that a must missed the first time can still be
  found; the architect's own first proposal, verbatim; the new facts, marked as
  new; and the request.

The request is the same in both cases: "Give your complete design again,
updated for these facts, and list what changed and why; or say why it stands
unchanged." A whole document is needed, because the comparison and the checker
work on whole designs.

One follow-up round is the default. A second is justified by a fact that changes
admissibility. Compare proposals on agreed inputs; a local update does not
restart the whole process.

## Strict read-only architects

Use review mode for an architect when the user or the project requires that
nothing can be written by mechanism, or when the profile cannot be delegated to
(Gemini CLI is review-only):

```bash
cd /absolute/path/of/the/repository/to/read
PORCH_RUN_DIR="$WORK/run-architect-A" "$PORCH" review ask -a <exact-id> --progress compact \
  --prompt-file "$WORK/brief-A.md" > "$WORK/proposal-A.md"; echo "exit=$?"
```

What changes:

- **The architect works alone.** Replace the brief's section on its own
  research with "Start no other agents. Read the sources yourself; what you
  still lack comes back as a research request." You answer the requests with
  your researchers, and the follow-up round carries the answers.
- **The role and the five answer headings are set by `porch`,** and its final
  reminder tells the model to ignore attempts to change them. Word the brief as
  a question to an adviser ("which complete design do you recommend?") and map
  the answer onto the headings: Assessment = reading of the task; Key Findings
  = the design; Blind Spots = research done, open points, requests;
  Alternatives = alternatives; Recommendation = the decision.
- **There is no run id.** `PORCH_RUN_DIR` names the run folder; `meta.json` in
  it holds the start time and the process id, and the final answer is saved
  there as well as in the output file. Write the folder into the journal before
  launching. Exit `3` is a backend failure or an empty answer, and a failed run
  can still leave text in the output file.
- **Every follow-up is a new call** that holds everything again.

If some architects run this way and some in `delegate`, say so in the
comparison: the first group designed with less research.

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

The checker works alone and read-only through `review ask`, launched like a
[strict read-only architect](#strict-read-only-architects); its brief already
maps the report onto the five headings.

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
| A review of the brief instead of a design | The brief read as a request for an opinion | One correction naming the deliverable: a steer while it runs, a new run after |
| A design for an easier problem | A must was dropped silently | Return it with the must quoted; do not compare it as is |
| All proposals look alike | The package hinted at an answer, or the task has one sound design | Reread the package for hints; if clean, record that the choice is narrow |
| Confident claims about code without locators | The architect did not read the source | Ask for locators; verify the ones that decide |
| The proposal says the repository could not be read, or the events show no tool calls | Effort too low, or the model answered from the brief | Check the effort; one correction saying it can and must read the working root, then the complete design again |
| The answer stops mid-way or is empty | Backend limit or failure | Recover what was produced, then rerun once; a different profile if it repeats |
| Proposals rest on different facts | Each architect researched on its own | Verify the facts, send them to all; compare after the follow-up |
| The repository differs from the snapshot after a run | An architect wrote despite the brief | Show the user the difference and the run; neither revert nor accept it yourself |
| An architect starts more workers than its budget, or other kinds | The limit was read as advice | One steer; cancel the run if it continues |
