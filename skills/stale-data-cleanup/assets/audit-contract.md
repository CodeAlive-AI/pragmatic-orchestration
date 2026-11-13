# Audit contract (read-only)

<!-- Coordinator: fill every CAPITALISED placeholder, then append one zone section
     per researcher (scope paths, what is special there, which dry-run commands are
     allowed, the two report paths). Delete this comment. -->

## Context and intent

MACHINE (disk size, free space now, operating system). The owner's complaint, in
their words: "OWNER_WORDS". Several researchers audit separate zones in parallel;
a coordinator turns the reports into verdicts and has them carried out.
**Nothing is deleted in this phase.**

Your report decides what is removed without the owner looking at each item. A
wrong "safe to remove" can cost work that exists nowhere else; a missed large
item leaves the disk full. The target is TARGET.

The deliverable is an assessment, not an implementation.

## Limits (the runtime gives you full access; these are your limits)

- **Read-only.** Do not delete, move, rename, truncate, compress, chmod or modify
  anything. Do not run a cleanup, prune, gc, uninstall or cache-clean command of
  any tool (`rm`, `mv`, `brew cleanup`, `npm cache clean`, `pnpm store prune`,
  `uv cache prune|clean`, `docker ... prune|rm`, `git gc|prune|fetch|worktree prune`,
  `jj abandon|workspace forget`, and the like). A dry-run variant is allowed only
  where your zone section names it.
- **The only files you may create** are your two report files. No temporary files
  elsewhere; use pipes.
- No `sudo`. Do not launch GUI applications or a container engine. Install
  nothing. Upload nothing. **No credentialed network calls**, not even read-only
  ones (cloud CLIs, object stores, registries, `gh`), unless your zone section
  names one. A web lookup is allowed only to identify an unfamiliar folder or
  bundle id; never put a user, project or customer name into a query.
- **Privacy: metadata only** (names, sizes, timestamps, counts). Do not read the
  contents of `.env*`, credentials, keys, tokens, shell startup files, keychains,
  `~/.ssh`, `~/.aws`, `~/.gnupg`, chat or session transcripts, databases, mail,
  messenger data or the owner's documents. Reading a manifest to identify
  ownership (package.json, Info.plist, a version file, README, AGENTS.md) is
  fine. The one exception for transcripts: when your zone section allows it, a
  bounded search of agent session history for a candidate's path with
  SESSION_TOOLS, reading only the fragments around a hit.
- **Version control without writing.** git: read-only commands with
  `--no-optional-locks` (`status`, `log`, `rev-parse`, `rev-list`, `worktree list`,
  `for-each-ref`, `stash list`). jj: every command carries
  `--ignore-working-copy`; a plain jj command snapshots the working copy, which
  is a write.
- **Be gentle on the machine.** Other researchers run at the same time. Never
  `du` or `find` the whole disk or the whole home directory. Start from the
  shared scan; run targeted `stat`, `find` or `du` only inside your zone, one at
  a time, no background loops.
- **Stay in your zone.** If you notice something large outside it, add one line
  under "Outside my zone".
- Do not start subagents and do not delegate.
- **Other actors are live right now:** OTHER_ACTORS (sessions, scheduled jobs,
  their areas). Their directories are not stale, whatever their age. If the
  state of your zone changes while you work, record it under "Concurrent
  changes" and classify what exists now, not what the scan shows.

## Shared inventory (use it before touching the disk)

A metadata scan of VOLUME was taken at SCAN_TIME. Query it:

```bash
SCAN_QUERY_COMMAND <ABSOLUTE_DIR> [--depth N] [--min-mb N] [--top N]
```

It gives each folder's size and the age of its newest file. Sizes are allocated
bytes and therefore an **upper bound** on what removal frees: copy-on-write
clones and hardlinks share blocks, sparse files exist. Where sharing is likely
(dependency folders, copied checkouts, package stores) say so and mark the
estimate "upper bound".

## What counts as evidence

Give concrete evidence for every candidate, never an impression, and never age
alone:

- the newest modification time inside it and when it was created;
- whether the owning tool is still installed, its current version, and whether a
  newer version of the same thing sits next to the candidate;
- whether anything live refers to it: a running process (`pgrep -fl`, a process
  whose working directory is inside it), a symlink, a config entry, a
  version-control registration, a lock file, a service definition;
- whether it is regenerable, by which command, from where, at what cost;
- whether it is the **only copy** of something: search the project's documents
  for the path and for statements such as "kept locally" or "not uploaded";
- for a checkout: version-control state. Uncommitted work, or commits that exist
  nowhere else, are **not** stale, however old;
- for task or run output: the state of its task record, quoted.

PROTECTED_LIST (the categories or the file that lists what must never be offered
as removable).

## Classes

- **A**: regenerable, or provably obsolete with the proof stated.
- **B**: probably stale, but a fact is missing or the owner must decide. Say
  exactly which fact, or which decision.
- **C**: keep: live data, protected, or in active use. List every C item of
  1 GB or more with a one-line reason, so the owner sees why large things stay.
- **U**: could not determine. Say what evidence is missing and where it might be
  found.

Conversation and session history, messenger data, documents, customer files,
credentials, backups and anything on the protected list are never class A.

## Deliverable

1. **Report** at REPORT_MD, in English:
   - `Summary`: zone total from the scan; reclaimable size per class; the ten
     largest candidates.
   - `Findings`: one row per finding of FLOOR or more (smaller ones may be
     grouped): path | size | class | what it is | evidence (the command you ran
     and its key output) | what breaks and the cost to regenerate | proper
     removal (the owning tool's own command where one exists; proposed, never
     run) | confidence.
   - `Growth cause`: for the three largest items, which process keeps creating
     them and which single change would stop the regrowth. A hypothesis is
     acceptable when labelled as one.
   - `Coverage`: what you did not examine and why (permissions included).
   - `Outside my zone`. `Concurrent changes`.
   - `Deviations`: anything unexpected, any limit you could not keep, any
     command that turned out to write. "No deviations." if there were none.
2. **Items** at ITEMS_JSON: a JSON array, one object per class A, B or U finding
   (one per independently decidable thing: one per checkout, never one per zone):

   ```json
   {"id": "zN-short-slug", "path": "/abs/path", "size_bytes": 0,
    "newest_days": 45, "class": "A",
    "kind": "checkout|build-output|dependency-folder|cache|run-output|logs|installer|old-version|container|model|app-data|history|personal|other",
    "evidence": "one line", "removal": "owning tool's command, or `path`",
    "regenerate": "command and cost, or `not regenerable`",
    "needs": "for B and U: the missing fact or the owner's decision; else null",
    "confidence": "high|medium|low"}
   ```

Write the report after your first findings and keep it updated, so partial work
survives an interruption. Work largest-first and finish the zone.

Final answer, at most 12 lines: totals per class, the five largest candidates,
blockers. The two files are the deliverable.
