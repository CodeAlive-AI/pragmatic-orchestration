# Cleanup contract (execution)

<!-- Coordinator: fill every CAPITALISED placeholder, then append the track
     section: scope, the numbered verdict rules, the allowed removal prefixes,
     the gate (ACT or TABLE-THEN-STOP), and the three file paths. Delete this
     comment. -->

## Context and intent

MACHINES have been short of disk space. A read-only audit on AUDIT_DATE
classified the data; its reports are in AUDIT_DIR (`*.md` with evidence,
`*.items.json` with path, size and class per finding).

The owner asked for decisions, not a list to tick: "OWNER_WORDS". So this phase
is **execution**. The coordinator has already taken the decisions that needed
judgment; they are the verdict rules in your track section. You apply them and
act. Do not hand choices back to the owner. Where a case does not fit a rule,
the verdict is KEEP: keep it, say why in one line, and move on. Do not invent a
rule and do not stretch one.

## Standing limits

- **Lossless or regenerable only.** Remove a thing only when its rule is met and
  you verified the evidence yourself immediately before removing it. The audit
  is hours old and other agents are working on these machines: paths vanish, new
  ones appear, states change.
- **Never remove:** conversation or session history; customer files;
  credentials, keys, `.env*`; databases and container volumes; backups; anything
  on PROTECTED_LIST; anything a running process has open or works in; anything
  modified in the last 24 hours, unless your track's rule says otherwise.
- **Other writers are live:** OTHER_ACTORS. Never land, park, merge, abandon,
  rebase or edit another writer's change, except the exact objects your track
  names. Never push. Never undo or restore repository operations.
- **The owning tool first.** A registered worktree or workspace is closed with
  its version-control command; a cache is pruned with its tool's own command and
  only while no process of that tool runs; a container object is removed by its
  exact name. Never a global prune on a shared host, never a volume.
- **Plain paths go through the gate.** Remove plain files and directories only
  with `APPLY_REMOVALS_COMMAND LIST.tsv --allow-prefix ... --manifest MANIFEST`
  (dry run first, then `--apply`). The list names absolute literal paths, each
  with its rule id and one line of evidence. No globs, no `find -delete`, no
  `rm` written by hand. A refusal by the gate is a result: record it and keep
  the thing.
- **Manifest first.** MANIFEST is one tab-separated file for your track with the
  header `time`, `action`, `path`, `bytes`, `files`, `rule`, `evidence`,
  `status`. The gate creates it and writes its own rows. A removal done through
  a tool's own command is recorded by you in the same file: one row before the
  command with the status `started` (local time, the command as the action, the
  absolute path or the object's name, bytes, `0` when the file count is unknown,
  rule id, one line of evidence), and one row after it with `done` or
  `error: REASON`. If your first removal is a tool's command, create the file
  with exactly that header line first. A removal with no manifest row is a
  defect.
- **Table before action.** Write the complete verdict table to REPORT before the
  first removal. If your track's gate is `TABLE-THEN-STOP`, stop there: the
  coordinator reads the table and sends you the list to execute.
- No `sudo` unless your track allows it. Install nothing. No paid calls. Do not
  start subagents and do not delegate. Do not stop or restart any process or
  service to release a lock: skip the item and report it.
- **Session histories**, where a rule says "confirm with session history": use
  the read-only session tools SESSION_TOOLS. Bound every lookup: filter by
  project and date, give it at most 60 seconds, never search the whole store.
  After five minutes without a result, decide by version-control evidence, or
  KEEP. Cite the session id and the one fact you relied on; quote nothing else.
- **Deviation journal.** Create DEVIATIONS. Record every unexpected finding and
  every departure from the plan as it occurs: expected versus observed, the file
  or command that shows it, what you did or propose, remaining risk. The journal
  does not authorize out-of-scope changes. If nothing deviated, write
  "No deviations."
- **Measure.** Record free space before your first removal and after your last,
  with MEASURE_COMMAND.

## Report

REPORT, in English:

1. `Verdicts`: every object of your track with its verdict REMOVED, KEPT or
   REFUSED (a tool or the gate refused), the rule id, one line of evidence, bytes.
2. `Space`: free space before and after.
3. `Unfinished work left in place`: each kept object that holds unfinished work:
   what it is, whose it is (session id when known), and the one action that
   would finish it.
4. `Not done`: anything a rule called for that you could not do, and why.

Final answer, at most 12 lines: counts per verdict, bytes freed, blockers.
