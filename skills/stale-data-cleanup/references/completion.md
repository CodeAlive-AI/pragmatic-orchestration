# Finishing: when to stop and what to hand over

## Contents

- [When the work is done](#when-the-work-is-done)
- [When to stop early](#when-to-stop-early)
- [Final checks](#final-checks)
- [The report](#the-report)
- [Causes and follow-ups](#causes-and-follow-ups)
- [After the report](#after-the-report)

## When the work is done

Done is not "the disk has room". It is all of:

1. every zone has a verdict for each candidate above the size floor: removed,
   kept with a reason, or asked;
2. every execution track is accepted ([method.md](method.md), section 8);
3. the control scan reconciles with the manifests;
4. the space target is met (the owner's, else 20% of the volume), **or**
   everything still large is on the kept or asked list with its reason;
5. the journal holds the final state, and no worker of yours is running;
6. the report is delivered and the closing questions are asked.

## When to stop early

- **The target is met and the rest is small.** Do not remove things for sport.
  Past the target, continue only with what is provably finished (tidiness), not
  with borderline data.
- **A pass yields little.** When a narrow second pass removes one object in
  ten, that zone is finished; what remains is unfinished work for its owners.
  A rough rule: stop a zone when another pass would free less than a tenth of
  what the last one did, or less than about half a percent of the disk.
- **Everything left needs the owner.** Stop and ask.
- **The evidence source is exhausted.** No new fact in a pass: the open
  candidates become KEEP.
- **Risk rises.** Workers disagree with reality, another actor is changing the
  same area, a volume is unstable: stop removals, keep the audit, report.

Never stop with a worker still running, a manifest unread, or a track
unaccepted. If you must hand over, say exactly which.

## Final checks

- Space per volume, same metric as the baseline, with the time. If another
  writer moved it meanwhile, give both figures.
- `disk_map.py reconcile` exits 0, or each unexplained row is explained in the
  report.
- Live projects still resolve their dependencies; no dangling worktree or
  workspace registrations; services up.
- Your own leftovers are gone: worker scratch files, temporary lists, helper
  scripts. Manifests, reports and the journal stay.
- An earlier summary or plan you gave the owner is now stale: mark it as
  superseded or replace it.
- Worker deviations that the owner should know are collected.

## The report

Write it as verdicts, in the language the owner writes in, short enough to read
in a minute. Lead with the result. Details live in the manifests. Drop a
heading that has nothing under it.

```markdown
Cleanup is done: FREE_NOW free on MACHINE instead of FREE_BEFORE.

| | Before | Now |
| --- | --- | --- |
| MACHINE (one row per volume), used / free | … | … |

## Removed
- KIND: COUNT objects, about SIZE (one line of what and why it was safe).

## Deliberately kept
- THING (SIZE): the reason in one line.

## Unfinished work, left where it is
- WHERE: WHAT it holds, WHOSE it is (or "nobody claims it"), the ONE action
  that would finish it.

## Yours to decide
| Thing | Size | Recommendation |

## What I could not see
- SIZE on VOLUME that the scan could not read, and what would open it.

## Why the disk filled
- CAUSE and the one change that stops it.

## To be open about
- Anything removed from a personal folder; anything a worker did outside its
  brief; anything another session changed meanwhile.
```

Rules for the report:

- State nominal sizes as approximate; the before and after table is the truth.
  When another actor freed or filled space meanwhile, say how much of the
  difference is not yours, or give the figure right after your cleanup and the
  figure now.
- Say what you could not see (the blind spot) as a number.
- Disclose what was removed from personal folders, even three installers, and
  anything a worker did outside its brief.
- "Kept" must explain the large things the owner will notice are still there.
- Do not list every path. Give the location of the manifests.
- No praise of the work, no hedging about verified facts.

## Causes and follow-ups

A cleanup that names no cause will be needed again in a month. For the largest
areas ask what keeps creating them and which single change stops it. Common
causes and their fixes:

| Cause | Fix |
| --- | --- |
| A pipeline makes a full checkout, images and a task record per run and never closes them | A closing step at the end of the run, on success and on failure; a documented way to close a dead run's record |
| Every task workspace carries its own dependency folder | Share one store (links or clones), install on demand, or close workspaces when their task lands |
| Scratch and local artifacts have a retention rule that nothing enforces | A sweep keyed on the project's own rule; scratch outside the repository |
| Non-interactive agent jobs write full session histories | Run such jobs without session persistence; set the tool's retention |
| Package caches grow without bound | The tool's prune in a periodic idle window |
| A container build cache has no ceiling | A size limit on the builder below the free space |
| Many full iterations of one deliverable are kept | Keep the final one; drop intermediates when it is packaged |
| Applications download media automatically | Turn auto-download off, set a limit in the application |

Offer the fix as a separate follow-up task with a complete brief (it is code or
configuration work with its own review), through the harness's way of proposing
a task. Do not fold it into the cleanup, and do not start it unasked.

Offer the observer for the time between cleanups ([upkeep.md](upkeep.md)): write
the machine's stocks file from the zones of this cleanup and take the first scan
now, so the next look starts from a baseline. A schedule for it is installed
only on the owner's explicit yes.

Alerting on free space within hours is a different job; mention it once if the
owner has none.

## After the report

1. Ask the closing batch ([owner-decisions.md](owner-decisions.md)).
2. Act on each answer at once, with the same gate and manifest; measure; amend
   the report in place.
3. Write the final state into the journal: figures, what stays open and why,
   that no worker runs.
4. Coordinator Mode stays on until the owner turns it off. Tell them which
   workers, if any, are still running.
