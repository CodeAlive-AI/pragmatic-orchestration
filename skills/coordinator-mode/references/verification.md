# Designing verification

You decide what must be proven and what counts as proof. Executors write the
tests, run them and operate the application. You then read the tests and the
evidence and decide whether the proof holds.

This split matters because agents left alone produce tests in bulk: one per
function, assertions that restate the implementation, mocks that verify
themselves. Such tests pass, cost time on every run, and catch nothing. Current
models also add tests unprompted, so a brief that only says "cover it with tests"
gets too many of the wrong kind. Name the checks you want.

## Contents

- [Start from behavior and risk](#start-from-behavior-and-risk)
- [Pick the level](#pick-the-level)
- [A check must be able to fail](#a-check-must-be-able-to-fail)
- [Keep the suite small](#keep-the-suite-small)
- [Put the plan in the brief](#put-the-plan-in-the-brief)
- [Reading the evidence](#reading-the-evidence)
- [Examples](#examples)

## Start from behavior and risk

For each substantial piece of work, write down what must be true when it is done,
which invariants must survive, and how it could plausibly fail at the boundaries
it touches. First look at what the existing checks already prove, then add the
least that covers the real gaps.

A small change needs one sentence. A larger one gets a table in the journal:

| Requirement or invariant | Plausible failure | Check and level | Evidence expected |
| --- | --- | --- | --- |
| A paid order is never charged twice | A retried webhook creates a second charge | Integration: replay the same webhook against the real handler and database | One charge row; second call returns the first result |

Take expected values from the requirement, the domain rule, an external contract
or an independent reference. An expected value computed by repeating the
implementation's algorithm proves nothing. Change the plan when requirements or
risks change, not to fit a failing implementation.

## Pick the level

Choose the cheapest level that can observe the failure you fear.

| Level | Use it for | Avoid it for |
| --- | --- | --- |
| Unit or property | Dense business rules, algorithms, state machines, boundary values, combinations of inputs, corner cases | Glue code, wiring, anything where the test would mostly be mocks |
| Integration | The seams: persistence and transactions, serialization, authorization, queues and retries, wiring of real dependencies. Most feature work is verified best here | Exhaustive input combinations (push those down to unit level) |
| Contract | Consumers and providers that change independently: schemas, public responses, version transitions | Code owned and released as one unit |
| End-to-end | A few critical user journeys, usually the happy path, where only the whole system shows the failure | Variants and edge cases, anything a narrower test already proves |
| Other evidence | Rendered UI states, a manual scenario that can be repeated, a benchmark, a migration rehearsal, a runtime observation | Replacing an automated check that would be cheap to keep |

End-to-end tests are slow, expensive and brittle. Keep them few, make each cover a
different journey, and do not let two of them fail for the same defect. Add a
failure path at this level only when it matters to the user and nothing narrower
can show it: a declined payment, a cancelled upload, a recovery after restart.

Test the boundary at risk for real. Mock what is unrelated; mocking the thing
under test verifies the mock. There are no quotas: no pyramid percentages, no
test per function, no new end-to-end test for every change. The project's
required gates still apply.

## A check must be able to fail

For every check you plan, answer two questions:

1. Which plausible defect makes it fail?
2. Would a different, equally valid implementation still pass?

If the first has no answer, the check is decoration. If the second answer is no,
the check pins the implementation and will be rewritten at every refactor.

Checks that fail these questions: assertions that an object was constructed, that
a mock returned what it was told to return, that internal calls happened in a
certain order; snapshots of implementation detail; a success message taken as
proof that data changed. Assert on outputs, stored state and effects the user or
another system can observe.

For a bug fix, ask for a regression test that fails on the old behavior and passes
on the new one, when that comparison is feasible. If the old behavior cannot be
reproduced, record that and accept another observation instead of rebuilding
history.

## Keep the suite small

- Prefer extending an existing test to adding a new file.
- Ask executors to remove or merge tests that their change made redundant.
- Coverage numbers locate gaps; they do not prove correctness and are not a target.
- An executor may correct a test that contradicts the agreed requirement. It may
  not weaken an assertion, skip a test, or refresh a snapshot only to get a pass;
  that comes back to you as a question.

## Put the plan in the brief

Give the executor the scenarios and the evidence, and leave the mechanics to it:

```text
Verification plan
- Integration: replaying the same payment webhook twice leaves one charge row
  and returns the first result both times (real handler, real test database).
- Unit: rounding of partial refunds at 0.005 boundaries, expected values from
  the finance rules in docs/refunds.md, not from the code.
- Existing checkout end-to-end test must still pass; add no new end-to-end test.
These are the checks wanted. If you see a risk they miss, say so in the report
instead of adding tests for it.
Report: test names, results, the revision they ran on, anything skipped and why.
```

Doubt about the intended behavior comes back to you. It does not become a
rewritten expected value.

## Reading the evidence

- Read the test code in the diff, not only the result. Check it against the two
  questions above.
- Evidence belongs to a revision and an environment. Reuse it while it still
  applies; ask for a rerun after changes that invalidate it, including a merge
  with parallel work.
- Keep passed, failed, skipped and blocked apart. A check that could not run is
  not a pass.
- A flaky failure that passes on retry is not fixed. Have it investigated.
- For high-risk logic, ask an executor to break the code on purpose and confirm
  that the test fails. One such probe says more than twenty extra assertions.
- Use an independent reviewer when the consequences, coupling or uncertainty
  justify it. Give it the requirements and the raw artifacts, not the
  implementer's confidence, and ask specific questions.

Accept when the required gates and the planned checks have real evidence and no
concrete concern is left. Verification grows only for a new failure, a relevant
change, or an uncertainty that is still open.

## Examples

- Discount rounding: unit or property tests at monetary boundaries, expectations
  from the pricing rules.
- Tenant isolation: integration through real authorization, request handling and
  storage, one allowed and one denied access.
- Renamed field in a public response: a contract test with the old consumer
  against the new provider.
- Duplicate payment webhook: integration at the real seam with retries; the one
  existing checkout journey end to end.
- UI spacing: rendered states at the affected sizes; no new unit-test framework.
- Data migration: representative data, invariants preserved, the approved
  rollback path rehearsed.
- Internal refactor: existing behavioral and integration checks; new tests only
  for a gap that was found, not for every rewritten function.
