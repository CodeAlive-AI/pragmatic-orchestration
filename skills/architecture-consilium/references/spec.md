# The specification, the status and the handover

Stage 5 in detail.

## Contents

- [What the specification is for](#what-the-specification-is-for)
- [Sections](#sections)
- [Rules for the content](#rules-for-the-content)
- [Changes while writing](#changes-while-writing)
- [When the design is done](#when-the-design-is-done)
- [Choosing the status](#choosing-the-status)
- [The final report](#the-final-report)
- [Handing over to implementation](#handing-over-to-implementation)

## What the specification is for

`SPEC.md` ties the result to the decisions and the checks. Its reader is an
implementer, human or agent, who was not part of the design and must be able to
build the change, know which decisions are closed, and know when to come back.

It matches the design that was checked. It is not a transcript of the process:
the proposals, the comparison table and the objections stay in the work folder
and are linked, not copied.

## Sections

Start from [spec-template.md](../assets/spec-template.md) and write each
section at the depth the task needs:

1. **Goal, scope, versions.** The goal, what is out of scope, the starting
   versions, the user's decisions in force.
2. **Requirements and invariants**, each with an acceptance criterion.
3. **The chosen design.** The model, data, state, algorithms, owners and
   contracts.
4. **The main flow and the failures that matter.** Concurrency, retries,
   unknown outcomes, cancellation, recovery, with a property card per promise
   at risk.
5. **Why this design.** The real alternatives and the trade-offs accepted.
6. **Implementation plan.** Coherent steps with dependencies, borders of
   responsibility and a verifiable result per step.
7. **Transition.** Compatibility of versions, migration and backfill, order of
   switching on, how an error is detected, rollback or fix-forward.
8. **Verification.** The plan, and separately the experiments actually run.
9. **Assumptions, limits, open unknowns, conditions for revisiting.**

## Rules for the content

- **Separate preparation from the change of behaviour** where that lowers the
  risk. The preparation may be empty. The plan does not grow by neighbouring
  improvements that do not serve the task.
- **Every step is safe to stop after** and has a result that can be observed.
- **Where it can be done, the first step fixes the invariants in something that
  fails loudly:** a type, a schema constraint, a test. A border that exists only
  as a sentence in the specification is the first thing a cheaper implementer
  crosses half-way through.
- **For each check that matters** say what it confirms, which plausible defect
  it would reject, and what it does not prove. Tie a result that was really
  obtained to the state of the code, the configuration, the data and the
  conditions. A reused result is valid only while its inputs and contract are
  unchanged; unknown dependencies are not assumed stable.
- **A change of a test, a reference value, a threshold or an acceptance
  condition needs a reason of its own.** Say what passes now and why that meets
  the requirement. A fix of the implementation and a weakening of the check must
  never justify each other.
- **Judge rollback by the properties restored**, not by the existence of a
  revert or a feature flag. For an irreversible transformation of data, give the
  real repair strategy. Promise no recovery for which the data or the mechanism
  is not provided.
- **Close the architectural decisions and leave local detail** to the
  implementer inside the agreed borders. Do not fix every class and line in
  advance.
- **Name the conditions of return:** a change of behaviour, contract, invariant,
  state model, migration or material risk sends the implementer back with the
  fact, its consequences and a proposed change.
- **Keep kinds of statement apart:** requirement, decision, assumption, planned
  check, check performed. "Verified" appears only next to what was run.

## Changes while writing

Writing the specification often exposes a gap. If a material premise, a
contract, an invariant, the state model or the transition from the current
system changes while writing, check the consequences before handing over, and
repeat only the affected check. A change to the base of a design that was
already checked makes it a new candidate.

## When the design is done

- Material requirements are covered by mechanisms and acceptance criteria.
- Important premises have sufficient grounds; no unresolved contradiction could
  change the choice or make it inadmissible.
- The candidates were compared under the same material conditions; the choice
  and its trade-offs can be explained.
- The final design is consistent in state, contracts, effects and the transition
  from the current system.
- What remains unknown may be found out during implementation without a hidden
  change of architecture; the way, the moment and the consequences are named.
- The implementer knows the next step, the borders of its own decisions and the
  conditions of return.

## Choosing the status

| Status | Choose when | The report must name |
| --- | --- | --- |
| `READY_FOR_IMPLEMENTATION` | Every item of the list above holds | The next step and who may take it |
| `NEEDS_EVIDENCE` | A specific fact or experiment could change admissibility or the choice | The fact, how to obtain it, what each result would mean |
| `NEEDS_USER_DECISION` | A specific fork belongs to the user | The options, the recommendation, the consequences |
| `DRAFT` | A stage the chosen route requires was not passed | What is missing |

Rules:

- Judge against the route chosen at the start. The checked-design route
  requires the design by the method and its one independent check. The full
  workflow requires checked research, independent designs, a comparison on the
  same scenarios and the check of the final design. A route is not downgraded
  afterwards to make the status fit.
- Running out of budget, time or patience never raises a status. A good-looking
  document with an unchecked synthesis is `DRAFT`.
- One open must with status "unverified" is enough for `NEEDS_EVIDENCE`.
- `READY_FOR_IMPLEMENTATION` means the design is justified by analysis. It is
  not evidence about code that does not exist yet, and it authorises neither
  implementation nor release.

## The final report

Lead with the result, in terms the user can judge without reading code:

- the status and what it means for the next step;
- the decision in a few sentences, and the main reason;
- the alternatives rejected and what was given up;
- what was really checked, and by which means;
- what was not verified, and which assumptions the design rests on;
- questions that are the user's, with a recommendation each;
- the paths of `SPEC.md` and `RESEARCH.md`, and the next permitted step.

Say how the work was done in one or two lines: which models designed, which
checked, how many rounds. Report cost or savings only when measured. Coordinator
Mode stays on until the user ends it: say so, and name any worker still
running.

## Handing over to implementation

Hand the work to an executor only when the user's request already includes
implementation. Otherwise deliver the specification and name the next step.

When it does, the executor's brief follows `coordinator-mode` and
`pragmatic-orchestration`, and adds:

- `SPEC.md`, `RESEARCH.md`, the starting versions and the user's clarifications
  as inputs;
- the first step: confirm that the decisive premises and the starting state
  still hold;
- the musts, as links to the requirements and invariants of the specification;
- the steps and their acceptance;
- the conditions of return listed above;
- the report: actual changes, checks really run, limits, deviations from the
  specification, what remains. A planned check is never reported as run.
