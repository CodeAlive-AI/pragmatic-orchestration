# Does this design hold? Where does it fail?

<!-- Coordinator: fill every CAPITALISED placeholder. The checker must not be
     the author of the chosen design nor of the synthesis. Give it the design
     and the requirements, not the verdict you hope for. The brief is a
     question to an independent adviser, because review mode fixes the role and
     the five answer headings. Delete this comment. -->

## The question

A design is about to become a specification. Your independent check is needed:
does it keep every requirement below, and by which concrete sequence of events
would it fail?

The answer wanted is objections with concrete counterexamples. There is no
expected number of findings. If you find nothing in an area, say which area and
by which means you looked: that is one reader who found nothing, not proof.
Read and analyse only: change nothing and start no other agents.

## Inputs

- The design under check follows at the end of this text, in full.
- The task as the user stated it:

```text
VERBATIM_REQUEST
```

- Clarifications in force: CLARIFICATIONS.
- Musts, invariants and scenarios: REQUIREMENTS.
- Research at commit COMMIT, pasted below the design. It may be incomplete;
  check in the sources whatever your objection depends on.
- What the synthesis changed compared with the original proposals:
  SYNTHESIS_CHANGES (model, contracts, borrowed parts), or "no synthesis".

<initial_relevant_files completeness="likely-partial">
  <file path="PATH">WHY_RELEVANT</file>
</initial_relevant_files>
<context_seed_note>
This list is likely incomplete and is not an allowlist or scope boundary.
Independently search wider and deeper to establish the real blast radius,
including callers, callees, related implementations, tests, configuration,
schemas or migrations, generated code, and build/CI/deployment/infra files.
</context_seed_note>

## What to check

Judge the whole design on the scenarios that matter, with extra attention to
the joints the synthesis changed:

- each must: is there a mechanism, and does it hold?
- compatibility of premises between the parts of the design;
- ownership of state: one writer per fact, and what the others see;
- dangerous orders of events: a second actor, a duplicate, a crash between two
  steps, a delay past a timeout, an old version taking part, a slow dependency;
- outside effects and the unknown outcome: operation identity, repeat, lookup
  of the real result;
- migration and coexisting versions: half-migrated data, work in flight, what a
  rollback really restores;
- the real strength of each planned check: which defect it would catch, what it
  leaves unproven;
- what the design adds that no must or confirmed change needs, and whether a
  substantially simpler design would be admissible.

A mechanism's name is not an argument. For "idempotent", "transactional",
"queued", "locked" ask which conflicting history is forbidden, where the
decision becomes fixed, and what happens after the executor stops.

## An objection

```text
Violates    the requirement or invariant
Source      where the relevant code, contract or statement is (locator)
Sequence    the initial state and the 3-4 events
Outcome     what goes wrong, for whom
Resolve     the smallest change that would close it, or the fact to check
```

If the sequence depends on an assumption you could not confirm, say which one:
it becomes a question to check, not a confirmed violation.

## How to answer

Keep the five headings you were asked to use, like this:

- **Assessment:** is the design admissible as written? One paragraph.
- **Key Findings:** confirmed violations, numbered, each in the format above.
- **Blind Spots:** material hypotheses that need a check, in the same format;
  then the area you analysed and the limits of your conclusion.
- **Alternatives:** non-blocking suggestions, kept apart from defects. Matters
  of taste may be omitted.
- **Recommendation:** what must change before implementation, in order; your
  confidence; what would change your mind.

Answer in LANGUAGE; keep identifiers and file names as they are.

## The design under check

```text
DESIGN_TEXT (the whole design, pasted; never a path)
```

## Research

```text
PACKAGE_TEXT (sections 1-8 of RESEARCH.md, pasted; never a path)
```
