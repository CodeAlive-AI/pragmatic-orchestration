# The coordinator's journal

One Markdown file that holds everything a coordinator must not lose: the goal,
the decisions, the verification plan, who is working on what, and the next
action. It exists because context is compacted and sessions end; whatever is only
in the conversation will eventually be gone.

## Contents

- [When to keep one](#when-to-keep-one)
- [Where it lives](#where-it-lives)
- [What it holds](#what-it-holds)
- [When to write](#when-to-write)
- [Keeping it small over weeks](#keeping-it-small-over-weeks)
- [Coming back after compaction](#coming-back-after-compaction)

## When to keep one

Keep a journal when you are given an epic, a specification, or any task that will
run longer than one context window. A short coordination answer needs none.
Keep one journal per epic and start a new one, linked from the old, when the epic
changes.

You are its only writer. Workers return evidence in their reports and write their
own artifacts where you tell them; they do not edit the journal.

## Where it lives

**Default: outside the project**, so that progress notes never land in the
repository by accident and no worker edits them:

```bash
python3 SKILL_DIR/scripts/coordinator_mode.py on --project "$PWD" --task checkout-redesign
```

This creates `~/.local/state/coordinator-mode/journals/<project>-<hash>/<date>-<task>.md`
from a template, records the session and prints the capsule. `COORDINATOR_MODE_HOME`
moves the state root. Without Python, create the file by hand in the same place.

**In the plan or specification file, only when the user asks** ("write progress
into the plan"). Then:

```bash
python3 SKILL_DIR/scripts/coordinator_mode.py on --journal /absolute/project/PLAN.md
```

The script never modifies an existing file. Add a `## Coordinator progress`
section at the end yourself, put the capsule at its top, and keep all progress
inside that section. The requirements above it stay as the user wrote them;
progress notes do not rewrite requirements. Do not move a journal from one place
to the other unless the user asks.

Use the exact path from the capsule. Never adopt "the latest journal" of a project
or of another session.

## What it holds

The template has these sections. Write facts and reasons, not a diary.

| Section | Content |
| --- | --- |
| Capsule (top) | the five capsule lines, the project root, the start date |
| Goal and constraints | what the user wants, in their terms; limits, budgets, deadlines; the source plan or specification |
| Decisions and approvals | each decision with its reason; what the user approved and within which scope |
| Invariants | what must stay true across all assignments |
| Verification plan | the table from [verification.md](verification.md): requirement, plausible failure, check and level, evidence |
| Work items | each assignment: owner, workspace, dependencies, status |
| Workers | run ids or agent handles, model and effort, launch time, last checked, event cursor, pending guidance |
| Accepted results and evidence | what was accepted, on which revision, with which checks; consciously accepted limits |
| Open questions and blockers | what waits on the user or on something external |
| Next action | one concrete step that a reader with no context could take |
| Log | one line per significant event, newest last |

Link bulky artifacts instead of pasting them. Keep secrets out.

## When to write

- before a substantial dispatch (the brief's essentials and the worker handle);
- after accepting or rejecting a result;
- when a decision, an approval or a blocker changes;
- before a long wait, and before ending a turn;
- whenever the context is getting full. There may be no warning before a
  compaction, so do not save the update for later.

Always leave `Next action` current. It is the first thing you will need.

## Keeping it small over weeks

A journal that grows without limit stops being read. Rewrite the state sections
in place so they describe the present; only `Log` is append-only. Move finished
work items to a short "Done" list with a link to their evidence. When the log
gets long, summarize the old part into a few lines and move the original lines to
an archive file beside the journal.

## Coming back after compaction

1. Read the skill, then the whole journal.
2. Compare `Workers` with what is actually running (`porch delegate list --active`,
   or the harness's own view of its subagents). Collect finished results before
   launching anything; a worker you cannot find is unknown, not failed.
3. Check the working copies named in `Work items` for uncommitted or partial work.
4. Take `Next action`. If it no longer makes sense, the journal was stale: fix
   the journal first, then act.

When handing the role to another session, give it the capsule; everything else it
needs must already be in the journal.
