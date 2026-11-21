# Specification: TASK_TITLE

<!-- Write each section at the depth the task needs; delete what does not apply
     instead of filling it with ritual text. Keep kinds of statement apart:
     requirement, decision, assumption, planned check, check performed. -->

**Status:** DRAFT | NEEDS_EVIDENCE | NEEDS_USER_DECISION | READY_FOR_IMPLEMENTATION
**Date:** DATE · **Research:** RESEARCH_PATH (package vN)

A status of READY_FOR_IMPLEMENTATION means the design is justified by analysis.
It is not a verified implementation and does not authorise implementation or
release.

## 1. Goal, scope, versions

- Goal: who gets which result, under which conditions.
- Out of scope: …
- Starting versions: repository, commit, uncommitted state; dependencies that
  matter.
- Decisions of the user in force: …

## 2. Requirements and invariants

| ID | Requirement or invariant | Must or preference | Acceptance criterion |
| --- | --- | --- | --- |
| R1 | | | |

## 3. The design

Decision: "Need X under C. Must keep I. Chose A over B because Y, accepting Z.
Verify by P. Revisit when R."

- Model: the concepts, the data, the state and its allowed transitions, the
  algorithm.
- Owners: who owns each mutable fact; the source of truth.
- Boundaries and contracts: what each part promises and what a caller must know.
- What is reused; why each new abstraction exists; where duplication is kept on
  purpose.

## 4. Main flow and failures

The main flow, step by step, marking each effect that outlives its step.

One card per promise at risk:

```text
Property        the observable promise and the outcome that must not happen
Mechanism       who controls the state and which transitions are allowed
Assumptions     dependency contracts; the failure, time and concurrency model
Counterexample  the 3-4 events that would break it, and what stops them
Limits          the nearest realistic failure the mechanism does not stop, and who accepted it
Evidence        what is checked, on which code or model, and what that leaves uncovered
Recovery        what is true after partial execution, a crash, a version change
```

Cover what applies: concurrency, retries and duplicates, the unknown outcome,
cancellation, overload, recovery.

## 5. Why this design

| Alternative | What it gains | What it gives up | Why not chosen |
| --- | --- | --- | --- |
| The smallest change to the current system | | | |
| | | | |

Trade-offs accepted, and who pays each.

## 6. Implementation plan

| Step | Kind (preparation or behaviour) | Depends on | Border of responsibility | Verifiable result |
| --- | --- | --- | --- | --- |
| 1 | | | | |

Every step leaves the system working and is safe to stop after.

Decisions left to the implementer: …
Conditions of return to the coordinator: a change of behaviour, contract,
invariant, state model, migration or material risk.

## 7. Transition

- Coexisting versions: what old code reads and writes next to new code.
- Data: migration and backfill, repeatable and resumable; half-migrated state.
- Work in flight: how operations started under the old rules finish.
- Order of switching on, and how it is enforced.
- How an error is detected after switching on.
- Per step: what can be undone and how; the point of no return; the repair
  strategy beyond it.

## 8. Verification

Planned:

| Check | Confirms | Would reject which defect | Does not prove |
| --- | --- | --- | --- |
| | | | |

Performed during design (experiments really run):

| Experiment | Command or procedure | On which revision and data | Result | Limits |
| --- | --- | --- | --- | --- |
| | | | | |

## 9. Assumptions, limits, unknowns, revisiting

| ID | Assumption or unknown | If false | How and when it is checked | Blocks |
| --- | --- | --- | --- | --- |
| | | | | nothing / the status / a step |

Revisit the design when: …

## Process record

Architects: … (model, researcher runs) · Checker: … · Rounds: … · Objections: N raised,
fixed F, tested T, accepted A, rejected J (details: link).
