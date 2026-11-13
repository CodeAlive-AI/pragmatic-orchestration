# Method: from a full disk to accepted removals

## Contents

- [1. Baseline and map](#1-baseline-and-map)
- [2. Zones and the read-only audit](#2-zones-and-the-read-only-audit)
- [3. Accepting the audit](#3-accepting-the-audit)
- [4. Research until it is clear](#4-research-until-it-is-clear)
- [5. Verdict rules](#5-verdict-rules)
- [6. Execution](#6-execution)
- [7. Supervision and worker failures](#7-supervision-and-worker-failures)
- [8. Acceptance](#8-acceptance)

`SKILL_DIR` below is the directory of this skill. Run the scripts with the
Python launcher the user's instructions prescribe, else `python3`.

Default thresholds, to adjust to the disk and the owner's target:

| Threshold | Default |
| --- | --- |
| Free-space target when the owner names none | 20% of the volume |
| Re-measure and re-verify an audit item yourself | above 1% of the disk |
| Spend another research pass on an unclear item | above 0.1% of the disk |
| Size floor for removing data (not caches) | 50 MB; below it KEEP |
| Idle time: checkout in a registered repository / separate clone / project for its dependency folders | 24 h / 72 h / 3 days |
| Idle time: finished run output, scratch entries | 7 days |
| One history or network lookup by a worker | 60 seconds |
| A tool's prune waiting on a lock | 1 minute, then skip |

## 1. Baseline and map

Do this yourself; it is cheap and every later decision rests on it.

1. **Measure** each volume before anything else and write the numbers, with the
   time, into the journal. Use a metric that follows removals: on Linux
   `df -B1`; on APFS the data volume's "Volume Used Space"
   (`diskutil info /System/Volumes/Data`), because the container's free space
   also moves with swap and snapshots.
2. **Take one scan per volume or host** and let everyone query it. Ten workers
   each running `du` over the disk is slow, hurts the machine and gives ten
   inconsistent pictures.

   ```bash
   python3 SKILL_DIR/scripts/disk_map.py scan /System/Volumes/Data -o mac.json   # macOS
   python3 SKILL_DIR/scripts/disk_map.py scan / -o host.json                      # Linux
   ssh HOST python3 - scan / < SKILL_DIR/scripts/disk_map.py > remote.json        # remote host
   python3 SKILL_DIR/scripts/disk_map.py query mac.json /Users/me --depth 2 --min-mb 1000
   ```

   The scan gives every folder its allocated size and the age of its newest
   file, which is the first staleness signal for free. On macOS the root is the
   data volume (`scan /` is refused: the root shows the same folders twice);
   `query` accepts the short form of a path (`/Users/me`). Other filesystems
   below the root are listed, not entered: scan each volume separately. A
   faster scanner from a platform skill is fine as long as it yields a
   queryable tree.
3. **Find the blind spot.** Used space minus the scan total is what the scan
   could not read: privacy-protected folders, other users, snapshots. Report it
   as a number; do not guess what is in it.
4. **Find the other actors now**, before any worker starts: agent sessions
   (processes whose working directory is in a candidate area, entries of the
   last hours in a repository's operation log or reflog, lock files), scheduled
   jobs and reapers, the owner's other terminals. Write them into the journal;
   they go into every brief as "live right now".
5. **Check access** you will need (remote host reachable, cloud login valid).
   Restoring network or credentials is the owner's act: ask once, and keep the
   zones that do not need it moving.
6. Write a ten-line picture for yourself: the largest areas and how fresh each
   is. Fresh and large usually means live parallel work, not junk.
7. **Decide the scale.** A few zones you can audit yourself within the hour:
   work alone, with the same rules, gate, manifest and a journal. Otherwise a
   team, as below.

Practical points:

- **Working files** (scans, reports, manifests, lists) live in the journal's
  folder, outside every project; for a machine-wide job the journal's project
  is the home directory. A scan of a large disk is tens of megabytes.
- **A disk that is nearly out of room** (under about 2% or 5 GB free) cannot
  host scans and workers safely. Free the zero-risk tier first: caches through
  their own commands, old tool versions. Then start the audit.
- **Who works where:** `lsof -a -d cwd -Fpn` on macOS and the `/proc/*/cwd`
  links on Linux give each process's working directory cheaply; your harness's
  session list and `porch delegate list --active` name the agent runs.
- **An existing workspace tool** (a survey or "finish" command named in the
  repository's instructions) is the first thing to ask about its checkouts.

## 2. Zones and the read-only audit

Cut the disk into zones by **who owns the data and what kind it is**, with
exclusive path prefixes. A typical laptop needs six to ten zones, a remote host
two:

| Zone | Typical content |
| --- | --- |
| Task checkouts | agent workspaces, git worktrees, per-run clones of the main repository |
| Inside the repository | untracked scratch, local artifacts, build output, tool state under `.git` |
| Other projects | idle checkouts, their dependency folders, benchmark and research clones |
| AI-tool state | session history, tool caches, worktrees and old versions the tools keep |
| Package caches and toolchains | package stores, SDKs, simulators, browsers for automation, models |
| Applications and system areas | apps, updater leftovers, logs, diagnostics |
| Application data | per-app data and caches under the user library |
| Personal content | downloads, documents, recordings, cloud-synced folders |
| Remote: containers | images, build cache, volumes |
| Remote: filesystem | task checkouts, run output, artifacts, backups, home caches |

Give each researcher `assets/audit-contract.md` filled in, plus its zone section.
Researchers are read-only, work from the shared scan, and deliver a report with
evidence and a machine-readable item list. Give version-control-heavy zones to
the more careful model. Check each worker's understanding in its first minute.

## 3. Accepting the audit

Read the reports as claims. Check these yourself:

- **Sizes.** Re-measure every item above about 1% of the disk.
- **Evidence.** Re-run the evidence command of the largest "safe" items. For
  checkouts make your own independent version-control classification and compare
  it with the worker's.
- **Protected categories.** Search all "safe" items for history, messenger
  data, credentials, backups, customer or personal files.
- **Coverage.** Sum of the zones against used space; any folder of 1 GB or more
  that no zone covered. Audits miss large items; executors find some later, and
  that is expected.
- **Shared blocks.** On copy-on-write filesystems the scan overstates what
  removal frees. Measure the real gain of the first removals of a kind (volume
  used before and after, divided by the nominal size) and apply that factor to
  the rest; label estimates "upper bound". A dependency folder that is a clone
  of the package store frees almost nothing by itself.
- **Deviations.** Workers with full access sometimes act: a credentialed cloud
  listing, a temporary file, a version-control command that takes a lock. Read
  the `Deviations` sections and disclose what happened to the owner in the
  final report.
- **Surprises in free space.** If free space moves while only read-only workers
  run, establish the cause from the operation log and the process tree before
  suspecting your workers: another session of the owner may be cleaning or
  creating things at the same time. Rescan and `diff`.

## 4. Research until it is clear

After each pass sort the candidates into decided and unclear. A candidate is
**clear** when its verdict rests on evidence you could show the owner. For the
unclear ones above the size floor, launch another, narrower pass on cheaper
models, in parallel:

- one question per pass ("is this the only copy?", "which session created these
  and how did it end?", "is every commit of these checkouts on the remote?");
- the candidates listed by exact path;
- the place to look named in the brief: which repository, which remote, which
  record, which session store.

Stop the loop for a candidate when it has a verdict, or when a pass brought no
new evidence: then it is KEEP ("not provable"), or ASK if it is large and only
the owner can know. One to three passes are normal. Do not spend a pass on
small things: below about 0.1% of the disk the verdict is KEEP unless a tool's
own command regenerates it.

When a rule turned out unprovable as written, the proof often exists somewhere
the worker was not told to look (the canonical repository on another machine,
the clone's own remote, an object store). Find it yourself, state in the next
brief exactly what you verified and when, and relaunch a pass that only
re-checks the facts that can change (process, recency, clean working copy).

## 5. Verdict rules

Turn the findings into numbered rules per track. A rule names:

- the kind of object;
- the conditions that must **all** hold, each with the command that shows it,
  checked immediately before removal;
- **where the proof lives**;
- the removal command (the owning tool's, or the gate);
- the allowed path prefixes;
- what a failed condition means: KEEP, with the fact that blocked it.

Add to every contract: where a case fits no rule, the verdict is KEEP; executors
neither invent nor stretch rules. Test each rule against one real object before
launch: a rule that cannot be proven yields zero removals and a lost half hour.

**Gate by reversibility.** Give each track one of two gates:

| Track holds | Gate | Why |
| --- | --- | --- |
| regenerable things (caches through their tool, build output, dependency folders, old versions) and finished checkouts whose work is preserved elsewhere | `ACT`: the executor writes its verdict table, then acts | a mistake costs a rebuild or a re-clone |
| data (run output, experiment traces, exports, scratch that documents may cite, anything that could be an only copy) | `TABLE-THEN-STOP`: the executor writes the table and **ends its run**; you read every row above the size floor, run the only-copy test yourself ([evidence.md](evidence.md)), then have an exact list executed (a follow-up to that executor, a new one, or yourself) | a mistake is unrecoverable, and executors reliably mistake "results are committed" for "the raw data is redundant" |

Set a **size floor for data** (for example 50 MB): below it the gain is not
worth any risk, so the verdict is KEEP.

## 6. Execution

- One executor per track, tracks with **exclusive prefixes**, each with
  `assets/cleanup-contract.md` filled in plus its rules and gate. Two writers
  never share a prefix. If tracks depend on each other (a handoff folder may go
  only after its workspace is closed), order them or plan a second pass.
- **Order:** regenerable first (it frees room at no risk), then finished work,
  then data, then the owner's answers. Decisions about personal folders come
  last, because other verdicts may rely on originals there.
- **The owning tool first.** Registered worktrees and workspaces are closed with
  their version-control command; caches with their tool's prune command while
  the tool is idle; container objects by exact name; SDK and simulator
  components through their manager. A system path is not untouchable when its
  manager offers a removal command, and not removable by `rm` when it does.
  Read what the tool's command also deletes: a "finish and remove" helper may
  take dependency folders with it.
- **Plain paths go through the gate:**

  ```bash
  python3 SKILL_DIR/scripts/apply_removals.py list.tsv \
    --allow-prefix /abs/area --manifest track-manifest.tsv            # dry run
  python3 SKILL_DIR/scripts/apply_removals.py list.tsv \
    --allow-prefix /abs/area --manifest track-manifest.tsv --apply
  ```

  The list is tab-separated with the columns `path`, `rule`, `evidence` and
  optionally `min_age_days` and `allow`. To remove a directory, allow its
  parent; the gate never removes a prefix itself. It re-checks containment,
  protected names, mount points, version-control state (git and jj, nested
  repositories included), key-like files, recency (nothing changed in the last
  day, unless the row sets `min_age_days`) and processes working in the tree.
  Every row ends as `would-remove` or `done`, `kept: REASON`, `refused: REASON`
  or `error: REASON`, and with `--apply` all of them go into the manifest. A
  refusal is a finding, never something to work around: either the thing stays,
  or you lift that one check for that one row (`allow` set to `vcs` or
  `key-files`, `min_age_days` set to `0`) with the proof in the evidence
  column. Dependency folders (`node_modules`, `.venv` and the like) are not
  inspected inside; the rule for them is about their project.
- **Your own share.** Applying a list you verified is mechanical and may be done
  by you when delegating it would cost more than doing it; note it in the
  journal.
- Measure before and after each track.

## 7. Supervision and worker failures

Check understanding in the first minute, then every five to ten minutes look at
the manifest and the verdict table, not at the worker's prose. A worker is
stalled when its files have not changed for ten minutes while its status says
running. Stopping and steering your own workers and their child processes is
always yours to do (`porch delegate steer`, `porch delegate cancel`); the limit
on stopping processes protects everyone else's.

| What happens | What to do |
| --- | --- |
| A worker hangs in an unbounded search (session history, `du` of a busy tree) | Stop that child process, steer: bound every lookup (filters, 60 seconds), and say what the verdict is when the lookup finds nothing |
| A worker stalls after writing its table (context compaction, a failed hook) | **Cancel it first**, so a revived worker cannot execute a table you are about to correct; then review the table and have the corrected list executed |
| A worker removes nothing because the rule cannot be proven | It did right. Fix the rule or supply the proof ([section 4](#4-research-until-it-is-clear)) and relaunch a narrow pass |
| A worker wants to remove data because "the results are committed" | That is what `TABLE-THEN-STOP` is for; run the only-copy test |
| A worker stretches a rule (removes archives the rule did not name) | Read its deviation journal; accept or restore the rule in the next brief; disclose |
| The gate or a worker's own guard refuses an object | Fail closed. Resolve by naming the exact object, never by widening a prefix |
| A report file stays a stub for a long time | Steer: write the report now, finish in ten minutes |
| Free space moves in ways the manifests do not explain | Another actor is writing or cleaning; trust manifests and scans, not `df` deltas |
| A worker reasons for many minutes and writes nothing | Interrupt once ("write the file now"); if nothing appears in a few minutes, cancel, confirm it stopped, and give the assignment to another model |
| A worker used credentials or the network outside its brief | Find out exactly what it reached and whether anything changed; tighten the contract; tell the owner what was accessed. Rotating a secret is theirs to decide and only matters if a value was exposed |

**When time is short** (the owner needs room within the hour): do the
regenerable tier and the checkouts already proven finished, through the same
commands and the same gate; skip nothing inside the gate. Postpone the research
passes, the control scan and the full report, say so, and finish them after.

## 8. Acceptance

A track is accepted when:

1. every manifest row lies inside the track's prefixes, has a rule and evidence,
   and ends in `done` or a recorded reason;
2. a **second scan reconciles**: every decrease is explained by a removal on
   record;

   ```bash
   python3 SKILL_DIR/scripts/disk_map.py scan ROOT -o after.json
   python3 SKILL_DIR/scripts/disk_map.py reconcile before.json after.json \
     --manifest track1.tsv --manifest track2.tsv --explain /path/pruned/by/its/tool
   ```

   An unexplained decrease is a defect to investigate before you report. What
   another actor removed is explained by naming that actor in the report, and
   its share of the gain is not yours to claim;
3. what remains still works: dependency links of live projects after a package
   store prune, no dangling entries in `git worktree list` or
   `jj workspace list`, services up;
4. for removed checkouts, the commits are reachable where the rule said;
5. space is measured again, and differences from the manifests' totals are
   explained (shared blocks, concurrent writers). Manifest sizes are nominal.
