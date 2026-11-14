---
name: stale-data-cleanup
description: "Find and remove stale data on a developer's machines by evidence, with verdicts instead of a checklist for the owner: finished agent workspaces and git worktrees, old task checkouts, build and run output, package and model caches, container images, on a laptop and on remote dev hosts. Use when a disk is full or keeps filling, or the user asks to free space, clean up after coding agents, close old workspaces or worktrees, audit what takes the space, or work out what is obsolete (\"disk is full\", \"free up space\", \"clean up my machine\", \"what can I delete\", \"диск забит\", \"почисти диск\", \"разбери старые воркспейсы\"), or to watch what grows between cleanups (\"keep the disk under control\"). A cleanup runs as a coordinated multi-agent job in Coordinator Mode; the observer is one read-only session. Not for refactoring or dead code, not for cloud storage or database retention, not for an emergency that must free space within minutes, and never for a delegated worker."
---

# Stale Data Cleanup

A full disk on a machine where many agents work is rarely old junk. It is
finished work nobody closed, caches, and a remainder only the owner can judge.
The job is to tell the three apart **by evidence**, remove the first two without
troubling the owner, and bring them a short list for the third.

The owner asked because they are at a loss. A list of two hundred checkboxes
hands the problem back. **They want verdicts.** Every candidate ends in one of
three states: REMOVED, KEPT with a one-line reason, or ASKED, and ASKED is rare.

## The role

- **Run this in Coordinator Mode.** Load `coordinator-mode` and enter it as that
  skill describes (executors, journal, capsule); it loads
  `pragmatic-orchestration` for the mechanics. Invoking this skill **is** the
  explicit request that mode asks for: tell the user in one line that this runs
  as a multi-agent job, and keep the mode on until they end it. After a
  compaction reread this file and the journal first.
- **You are the strongest model available.** Wrong verdict rules destroy data,
  so rules, acceptance and decisions stay with you.
- **Researchers and executors are cheaper models**, several in parallel (up to
  five per model unless the user says otherwise). Launch researchers again, with
  narrower questions, until every sizeable candidate is clear.
- **Evidence comes from the machine, not from guesses:** the filesystem,
  processes, version control, task records, project documents and agent session
  histories (`porch sessions` in this pack).
- **Decide the scale after the map** (step 2). When everything fits a few zones
  you can audit yourself within the hour, work alone: no team, but the same
  rules, the same gate and manifest, and a journal.
- **An observation is not a cleanup** ([Between cleanups](#between-cleanups)):
  one read-only session, no team, no Coordinator Mode.

## Who decides what

| Decide and act | Keep and list | Ask the owner |
| --- | --- | --- |
| Caches, through their tool's own command | Anything a process works in or runs from | Personal content: documents, downloads, recordings, messenger data, cloud-synced folders, the trash |
| Build output and dependency folders of idle projects | Checkouts with uncommitted work or commits that exist nowhere else | Only copies: raw experiment data, exports, backups, customer files |
| Old versions beside a current one; installers; exact duplicates | Session history, memory and settings of agents and applications | Data that waits for a product decision |
| Finished checkouts: work preserved elsewhere, task closed, idle, unoccupied | Records and data in a state that waits for a person | Discarding unfinished work nobody claims |
| Output of tasks whose record is final | Whatever no rule covers, and whatever cannot be proven | Anything that needs privileges, stops a process, or publishes; their own databases and volumes |

Full lists and the question patterns: [owner-decisions.md](references/owner-decisions.md).

## How the work goes

1. **Mandate.** Infer it from the request and the user's standing instructions;
   ask only what is missing, in **one interactive batch** (the harness's
   question tool, recommendation first): how far you may go without asking
   again, which machines, how much free space is enough, what is off limits.
   "Sort it out", "clean it up", "delete what is obsolete" already answer the
   first; "let's look", "what takes the space" do not.
2. **Baseline and map.** Measure each volume. Take **one scan per volume** with
   `scripts/disk_map.py` and let every worker query it. Where scans of earlier
   runs exist, `disk_map.py trend` over them shows what grew since: start
   there. Name the blind spot
   (used space the scan could not read). Find the other actors on the machine
   now: live agent sessions, scheduled jobs. Check the access you will need. If
   the disk is nearly out of room, free the zero-risk tier first (caches through
   their own commands) so the work itself has space.
3. **Audit, read-only, in parallel.** Cut zones by owner and kind with exclusive
   prefixes; give each researcher [assets/audit-contract.md](assets/audit-contract.md)
   filled in. They return evidence and a class per finding.
4. **Accept the audit and research until clear.** Re-measure the large items,
   re-run the evidence of the big "safe" ones, correct sizes for shared blocks.
   For what stays unclear, launch narrower passes: one question, exact paths,
   the place to look. Stop when a pass brings nothing new: that item is KEEP, or
   ASK when it is large.
5. **Write verdict rules** per track: conditions that must all hold, checked
   right before removal, and **where the proof lives**. Where no rule fits, the
   verdict is KEEP. Gate each track by reversibility: regenerable things and
   provably finished work, the executor acts; **data, the executor writes its
   table and stops** until you have run the only-copy test.
6. **Execute.** One executor per track, exclusive prefixes,
   [assets/cleanup-contract.md](assets/cleanup-contract.md) filled in. The owning
   tool's command first; plain paths only through `scripts/apply_removals.py`,
   which re-checks and writes the manifest. Regenerable first, personal last.
7. **Supervise and accept.** First-minute check of each worker, then manifests
   and tables, not prose. Accept on evidence: manifests inside their prefixes,
   a second scan that `disk_map.py reconcile` explains completely, live projects
   still working, space measured again.
8. **Report and close.** Verdicts in the owner's language, one screen. Then the
   closing questions, interactive, at most four at a time, largest first; the
   cause of the growth; the fix offered as a separate task; and the observer
   offered for the time between cleanups.

Tell the owner in one line when a phase changes; an hour of silence on a full
disk reads as nothing happening.

The steps in detail, with commands and the table of worker failures:
[method.md](references/method.md).

## Evidence

- **Age alone proves nothing.** Fresh and large is usually live parallel work;
  old and small can be the only copy of an experiment.
- **Work may go when four things hold:** it is preserved elsewhere, its task is
  closed, it is idle, and nobody is in it. Uncommitted work is never stale.
- **Data may go when it is regenerable** (by which command, from where) **or a
  proven duplicate.** Before removing anything else, answer "where else does
  this exist?". "The results are committed" does not make raw data redundant;
  documents that cite a path as evidence, or say it exists only here, keep it.
- **Sources, cheapest first:** filesystem, processes, version control, the
  owning tool's verdict, task records, project documents, session histories,
  remote existence (only with authorization), the owner.
- **A proof may live somewhere the worker was not told to look.** Find it and
  put it in the brief. No proof means KEEP.
- **Sizes are upper bounds.** Clones, hardlinks and image layers share blocks;
  the volume's used space is the truth.

Per kind of object, with commands for git, jj, plain copies, task records and
containers: [evidence.md](references/evidence.md).

## Standing limits

**The owner's alone.** A general mandate ("delete everything stale", "don't ask
me again", an order given in a hurry) never covers these. Only the owner's
explicit answer about the named thing does; then you carry it out, or they do
where privileges or a running session are involved:

- everything in the "ask the owner" column, their credentials included;
- stopping or restarting a process, service or engine to free a lock;
- landing, parking, pushing or discarding unfinished work, also work their own
  agents left behind.

**Nobody's to waive.** Whoever asks, however urgent:

- other people's data and shared resources: no global prune on a shared host,
  nothing that belongs to someone else's task;
- removal by age or name pattern alone: every object needs its own evidence or
  the owner's answer naming it;
- a force flag or a wider prefix to get past a refusal (a tool, the gate, a
  guard) whose reason you have not read and resolved for that object;
- reading secret values: judge by names, structure and metadata.

The user's standing instructions and the project's own rules bind you as well.
Where they are stricter than this skill, they win; nothing looser lifts the
second list. Workers get no credentialed network access, do not delegate, and
do not inherit this role.

## When signals conflict

The signal that protects unrecoverable work or data wins for now; then find a
third source; if none settles it, KEEP and name the open fact. The cases that
come up most:

- old, but a process runs from it: live;
- "removable" by a tool, but the working copy has unrecorded edits: keep;
- the branch looks merged, but commits are on no remote: the commits decide;
- a record says "active", nothing has moved for days: a dead record, decide by
  the rest;
- a record or a document says someone is waiting or this is the only copy: keep
  at any age;
- free space moved and no manifest explains it: another actor, not your workers.

The full playbook and the corner cases: [conflicts.md](references/conflicts.md).

## Finishing

Done means: every zone has verdicts, every track is accepted, the control scan
reconciles, no worker runs, the journal holds the final state, the report is
delivered and the closing questions are asked. Stop earlier when the target is
met and the rest is small, when a pass yields little, or when everything left
needs the owner. Do not remove things for sport.

The report leads with the result and reads in a minute: before and after;
removed, by kind; deliberately kept and why; unfinished work left to its owners;
yours to decide, with a recommendation; why the disk filled and the one change
that stops it. Stop conditions, the template and follow-ups:
[completion.md](references/completion.md).

## Between cleanups

A cleanup empties the disk once; what fills it again is usually not what was
removed. The **observer** is a read-only run every couple of days: one scan,
then `disk_map.py trend` against the machine's list of **stocks**, the places
where things accumulate, each marked with who can empty it (`rules`, `owner`,
`nobody`). It reports the free space, the fill rate, the days until the volume
is full and a pressure level. It removes nothing.

- **At the end of a cleanup** write the stocks file from the zones, take the
  first scan and offer the observer. A schedule is standing configuration:
  install one only on the owner's explicit yes.
- **Stocks only the owner can empty** (session history, data awaiting a
  decision, abandoned unfinished work) are what fills a disk after a good
  cleanup. No sweep helps there; the work is a cheap, timely decision for the
  owner.
- **Growth outside the listed stocks** means a new stock has appeared: find out
  what it is and propose a row.

File format, reading the result, the report and scheduling:
[upkeep.md](references/upkeep.md).

## Bundled tools

Standard-library Python, macOS and Linux. `SKILL_DIR` is this file's directory;
use the Python launcher the user's instructions prescribe, else `python3`.

```bash
python3 SKILL_DIR/scripts/disk_map.py scan ROOT -o scan.json          # macOS: /System/Volumes/Data; remote: ssh HOST python3 - scan / < disk_map.py
python3 SKILL_DIR/scripts/disk_map.py query scan.json DIR --depth 2   # size and age of the newest file per folder
python3 SKILL_DIR/scripts/disk_map.py diff before.json after.json
python3 SKILL_DIR/scripts/disk_map.py reconcile before.json after.json --manifest M.tsv
python3 SKILL_DIR/scripts/apply_removals.py LIST.tsv --allow-prefix DIR --manifest M.tsv [--apply]
python3 SKILL_DIR/scripts/disk_map.py trend SCANS_DIR --stocks stocks.tsv --target-free 100G   # growth per stock, days until full
python3 SKILL_DIR/scripts/disk_map.py rotate SCANS_DIR --keep 8                                # drop the oldest scans
uv run --no-project python SKILL_DIR/scripts/observe.py --config observer.ini                # scheduled multi-machine observer
```

`apply_removals.py` is a dry run without `--apply`. It refuses or keeps paths
outside the allowed prefixes, protected names, checkouts with uncommitted or
unpushed work, trees with key-like files, anything changed in the last day and
directories a process works in; a row lifts one check only by naming it. Both
tools explain themselves with `--help`.

## References

| File | Read when |
| --- | --- |
| [method.md](references/method.md) | starting; cutting zones; writing rules; a worker stalls; accepting |
| [evidence.md](references/evidence.md) | deciding whether a checkout, a cache, data or run output may go |
| [conflicts.md](references/conflicts.md) | signals, rules, numbers or workers disagree; odd cases |
| [owner-decisions.md](references/owner-decisions.md) | about to ask the owner anything, or unsure whether to |
| [completion.md](references/completion.md) | deciding to stop; final checks; writing the report; follow-ups |
| [platforms.md](references/platforms.md) | a specific tool's cache, an AI tool's files, a filesystem that will not free space |
| [upkeep.md](references/upkeep.md) | setting up or running the observer; reading what grew since the last scan |
| [observer.ini](assets/observer.ini), [observer-launchd.plist](assets/observer-launchd.plist), [observe.py](scripts/observe.py) | script-only scheduled observation and macOS launchd template |
| [assets/audit-contract.md](assets/audit-contract.md), [assets/cleanup-contract.md](assets/cleanup-contract.md) | briefing researchers and executors |
| [assets/stocks.tsv](assets/stocks.tsv), [assets/observer-prompt.md](assets/observer-prompt.md) | starting a machine's stocks file; a scheduled observation |
