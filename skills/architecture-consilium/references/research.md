# Research: fixing the task and gathering context

Stages 1 and 2 in detail: what to record before any design, how to cut research
into questions, how claims are written down, and how to tell that the context is
sufficient.

## Contents

- [Before research](#before-research)
- [The first look](#the-first-look)
- [Cutting questions](#cutting-questions)
- [Who researches what](#who-researches-what)
- [Launching researchers](#launching-researchers)
- [Recording claims](#recording-claims)
- [Checking the research yourself](#checking-the-research-yourself)
- [When sources disagree](#when-sources-disagree)
- [When research is enough](#when-research-is-enough)

## Before research

Establish, from the context at hand and with questions only where a real fork is
open:

- the original request, the useful result expected, and decisions the user has
  already made;
- the repositories and sources in scope, working directories, versions of code,
  dependencies and significant configuration;
- musts, compatibility and operational constraints, preferences, assumptions;
- the models and tools available, and the real limits on parallelism, time,
  spend and access;
- the scope of the result: design only, which isolated experiments are allowed,
  and how the work is handed to implementation.

For code, record the commit and whether uncommitted changes exist. A commit does
not describe uncommitted code, configuration or outside services. With several
repositories, record each one that matters.

Restate the task in a few lines: who gets which result, under which conditions,
what has to change, what must still hold. A technical means the user named is
kept when it is a stated constraint; otherwise its fitness may be examined.
Behaviour of the current code is not automatically a requirement: the task may
be to fix exactly that. An old discussion explains where a decision came from;
whether it still applies has to be checked.

Express each quality that matters as a scenario: the event, the conditions, the
observable response, the allowed bound. Typical ones for a complex change: a
response lost after a write, a failing dependency, competing operations, a
stated load, a changed business rule, old and new versions running together.
Bounds come from requirements or measurements. When one is unknown, say so and
find out only the values the choice depends on.

## The first look

Before cutting questions, read the structure of the project and the main paths
of the behaviour in question yourself, enough to divide the research well and
to choose the scale. Starting paths are navigation hints, not the border of the
analysis.

## Cutting questions

One researcher, one coherent question. Useful shapes:

- Where does the flow start, who calls it, and who consumes its result?
- Where does the state live, and which other paths can change it?
- Where is the invariant enforced, and can that boundary be bypassed?
- Which errors, cancellations, retries and asynchronous transitions change the
  outcome?
- Which existing capability could be reused, and does its meaning really match?
- What do data schemas, client versions, deployment and operations constrain?
- Why was a disputed decision made, and does its reason still hold?

Follow both the grounds of the change and its consequences: from callers to
state and effects, then to consumers, error handling and migration. Take dynamic
registration, configuration, generated code, background jobs and outside
contracts into account where they could change a conclusion. A full graph of
the project is not a deliverable.

Sources beyond the code are opened for a named unknown:

| Source | For |
| --- | --- |
| Version history | Why a piece of code is as it is; what changed together |
| Issue tracker, chats, agent session history (`porch sessions`) | An intention or constraint that is written nowhere in the code |
| Vendor documentation | The behaviour of the version actually in use |
| The web | A current, version-sensitive outside contract |

A source that cannot be reached is recorded as unreachable. Lack of access does
not confirm that a constraint is absent.

Where the build and the tests are fast, a throwaway attempt at the direct
change in an isolated copy is research too: what breaks is a list of
dependencies. See the experiments section of
[proposals.md](proposals.md#experiments).

Researchers make no credentialed calls. A closed source (a private tracker, a
chat, a paid documentation portal) is read by you through access you already
have; the researcher gets the excerpt that bears on its question, or its brief
names the one read-only source it may open.

## Who researches what

| Work | Worker |
| --- | --- |
| Navigation, extracting evidence, finding usages, bounded local checks | A small, fast model |
| Subtle semantics, a contradiction, a guarantee across systems | A strong model on a bounded question, or a small experiment |

The price and the name of a model do not establish that it fits. The mapping
comes from the owner's instructions and `"$PORCH" --list-agents`.

## Launching researchers

Fill [researcher-brief.md](../assets/researcher-brief.md) for each. A brief holds
the question, why it matters, the exact environment and versions, navigation
hints, the traps, the limit of work and the report expected. Do not ask for a
hidden chain of reasoning.

Run them read-only and in parallel. Prefer the harness's built-in read-only
subagents on a cheap model: a narrow question does not need the advisory
wrapper that `review ask` puts around every answer. Use `review ask` when the
harness has no such subagents or another model family is wanted:

```bash
cd /absolute/path/of/the/repository/to/read
PORCH_RUN_DIR="$WORK/run-research-Q1" "$PORCH" review ask -a <cheap-profile> --progress compact \
  --prompt-file "$WORK/research-Q1.md" > "$WORK/answer-Q1.md"; echo "exit=$?"
```

in the background, one call per researcher. `WORK` is the absolute path of the
work folder: the command runs from the repository, and a relative file name
would be created there. `review ask` wraps the answer in its own five headings;
the brief says where the findings go. Up to five researchers per model at once
unless the user says otherwise. Check the exit code and that the first answer
understood the question before trusting a batch cut the same way.

Note each researcher in the journal before launching it. A `review ask` call
cannot be watched or awaited through `porch`; its state is its run folder and
its output file. A silent worker has not necessarily failed: read what it
already produced before starting it again.

## Recording claims

`RESEARCH.md` keeps three levels: a short core for the decision; an index of
evidence with one line of explanation each; raw fragments and long results by
link. Keep access to the grounds without making every participant read the
whole corpus.

A claim that matters is one line:

```text
ID | claim | kind | source and locator | version or date | check status | meaning for the decision | limits
```

**Kind** says what was established: user requirement, observed behaviour,
content of the code, history of a decision, inference, assumption.
**Check status** says on what ground: source read, observation made, inference
justified, not checked, contradictory, superseded. These are two dimensions; do
not fold them into one vague "verified".

- For code give the path, the symbol and a locator; for an outside source the
  link, the date and the version it applies to.
- A claim that a check was run keeps the command or procedure, the inputs, the
  result and its limits. Reading a test is not running it.
- An empty search is written as "not found in this area by these means", never
  as "does not exist".
- For every relevant place, say why it may matter. A list of paths without
  reasons cannot be checked or pruned.

## Checking the research yourself

Reread at the primary source the claims that decide the choice, touch a must,
are costly if wrong, or contradict other findings. Check minor facts in
proportion to their weight; do not repeat the whole research in sequence.

Then check for omission separately. Confirming the files on the list does not
show that the list is complete. For each decisive premise, look for the
realistic way it fails:

- another writer of the same state;
- another entry point into the same flow;
- a bypass of the adapter or the boundary;
- an outside consumer of the data or the contract;
- a special launch mode: a migration, a script, a job, a test harness;
- a version that disagrees: an old client, a stale worker, a cached copy.

Typical misses of cheap researchers: a dynamic call found by no text search, a
second implementation behind a flag, generated code, configuration that changes
the path, a consumer in another repository.

## When sources disagree

If a contradiction that matters cannot be resolved by reading, choose the
smallest check, or ask whoever owns the requirement. Do not rank sources
mechanically: the code describes what is implemented, the user defines what is
required, a historical record may explain why. A gap between them is itself a
subject for a decision.

Questions to the user are for what the code cannot decide: allowed degradation,
delay, loss, manual recovery, a change of scope. Do not pass the search for a
technical fact to the user.

## When research is enough

The first research stage is sufficient when the task, the boundaries touched,
the main guarantees and the remaining unknowns are clear enough to form
meaningful candidates. Do not wait for full knowledge of the project: the
architects research further on their own, and that is expected.
