# Conflicts and corner cases

## Contents

- [The principle](#the-principle)
- [When signals disagree](#when-signals-disagree)
- [When rules disagree](#when-rules-disagree)
- [When other actors are on the machine](#when-other-actors-are-on-the-machine)
- [When the numbers disagree](#when-the-numbers-disagree)
- [When a worker and the coordinator disagree](#when-a-worker-and-the-coordinator-disagree)
- [Corner cases worth knowing](#corner-cases-worth-knowing)

## The principle

Removal is the only step that cannot be taken back, and a full disk is never as
expensive as lost work. So:

1. When two signals disagree, the one that protects unrecoverable work or data
   wins **for now**.
2. Then look for a third source that settles it ([evidence.md](evidence.md)).
   Do not split the difference and do not let the larger size decide.
3. If nothing settles it, the verdict is KEEP with the open fact named; or ASK
   when it is large and only the owner can know.
4. A refusal by a tool, a gate or a guard is information. Read the reason. Never
   answer it with a force flag or a wider prefix.

## When signals disagree

| One signal says | Another says | Resolution |
| --- | --- | --- |
| Old: nothing changed for weeks | A process works in it, or it holds a lock, or a service runs from it | Live. KEEP. Helpers started long ago still execute from caches nobody wrote to |
| Fresh: changed in the last hours | Version control shows everything landed | Probably a guard tripped by someone else's rebase or a tool touching files. Read what changed; if it is only tool noise, the version-control proof decides |
| The workspace tool says "removable" | The working copy has edits not yet recorded | KEEP. Take the snapshot or status inside the candidate right before removal |
| The workspace tool says "blocked" | The blocker is one stray commit or only build output | Resolve that exact blocker by name with the proof written down, then let the tool close the workspace normally |
| The branch looks merged (name, pull request title, `branch --merged`) | Commits exist that are on no remote | The commits decide. KEEP until they are shown upstream by content |
| `branch --merged` says not merged | The default branch holds the same content (squash or rebase merge) | Content decides: patch equivalence, or the pull request's merged head equal to the local tip |
| The task record says "active" | No process, nothing changed for days, the commit is on the remote | A dead record. Decide by the rest; report the record as dangling |
| The task record's state looks final | A nested field names a retained cache, pending cleanup or a running stage | Not final. KEEP |
| The record says awaiting, paused, parked or retained | It is months old | KEEP. Age does not end a wait for a person. List it with the decision it waits for |
| A committed document reports the results | A document says the raw data exists only here | Only copy. KEEP the named path |
| The project calls it a cache of a remote store | The store cannot be checked | Explicit "costs downloads, never data" plus recent use of the store: remove. Otherwise keep small items, ask about large ones |
| A file matches a key or credential pattern | It is an expired self-signed test certificate | Judge by certificate metadata without reading key material. If that cannot be shown, KEEP |
| It sits under a system path | Its manager lists it as deletable | Use the manager's command. Neither "system path, untouchable" nor `rm` |
| An application looks unused | Other tools resolve into it (a CLI linked into the app bundle, compilers) | KEEP |
| An exact duplicate | The "original" lives in a personal folder the owner may empty | Remove the copy, record the dependency, mention it when asking about that folder |
| Six complete iterations of one deliverable | Nothing identifies the delivered one | KEEP all and ASK which was delivered |

## When rules disagree

- **The owner's mandate against a project rule.** "Delete everything stale" does
  not lift a repository's own rule such as "data is not destroyed without a
  backup or the owner's word" or "pushing is deploying". The stricter rule holds
  inside that project; the mandate covers what is regenerable or provably
  preserved.
- **The mandate against a protected category.** It stays protected when the
  owner asks in general terms or in a hurry. If they name one of their own
  protected things, say once what is lost, and act on their explicit answer
  about that thing. Other people's data is not theirs to release
  ([owner-decisions.md](owner-decisions.md), "Never the owner's to give").
- **"Finish everything unfinished" against "do not touch other writers' work".**
  Finishing means closing what is provably done and listing the rest with its
  one finishing action and owner. It never means landing, parking or discarding
  someone else's change.
- **A host's policy against efficiency.** "No global prune", "no deletion by age
  or prefix", "shared services are not cleanup targets": these bound every rule
  on that host. Work object by object through the owning task.
- **The audit against the rule.** The audit suggested removal, the execution
  rule says keep (a recency window, a missing proof). The rule wins; note the
  difference in the report.
- **The skill against the user's standing instructions.** Read them before
  writing rules. Where they are stricter, or prescribe how things are done
  here (which version-control tool, which Python launcher, where builds run),
  they win. A looser instruction does not lift the limits no one can waive.
- **Stopping processes.** The limit protects other people's and other tools'
  processes. Your own workers, their child processes and your own waiting
  commands you stop whenever they hang.

## When other actors are on the machine

- **Another session is cleaning or creating things right now.** Free space
  jumps, directories vanish or appear. Establish who from the operation log, the
  reflog and the process tree, write it in the journal, and do not double-handle:
  leave that area to the other session, tell the owner in one line, rescan.
- **A live agent run sits in a candidate.** Its checkout, its run output and its
  host-side counterpart are all live. Name them in every brief as untouchable.
- **A worker of yours and another writer overlap.** Stop your worker, inspect
  what it did, then decide. Never "race to finish".
- **A cache is locked by a running tool.** If the tool's prune waits on a lock
  for more than a minute, stop the prune (not the holder), skip the cache and
  report it for an idle window. Do not stop servers, daemons or the container
  engine to get a lock.
- **Tools tidy by themselves.** Some agent applications remove their own old
  worktrees or sessions on archive or by count. A candidate that vanished may
  have been theirs to remove; record it under "Concurrent changes".
- **A scheduled reaper exists.** Learn its rules before you write yours; do not
  remove what it tracks behind its back.
- **Read-only checks can still disturb.** A plain status command refreshes an
  index or snapshots a working copy while an agent is committing there. Use the
  non-locking forms in audits.

## When the numbers disagree

- **The disk freed less than the manifests total.** Shared blocks: clones,
  hardlinks, image layers. Manifest sizes are nominal; the volume's used space
  is the truth. A dependency folder cloned from a package store frees almost
  nothing until the store itself is pruned.
- **The disk freed more than the manifests total.** Someone else removed things.
  Reconcile with a scan before claiming the gain.
- **Free space fell during the cleanup.** A live build or another task is
  writing. Report both figures (after the cleanup, and now) and say why.
- **Free space did not move at all.** Snapshots hold the blocks, the files are
  deleted but still open, the trash was not emptied, or the virtual disk of a
  container engine or subsystem does not shrink by itself.
  See [platforms.md](platforms.md).
- **The scan and `du` disagree.** Allocated against logical size, sparse files,
  or the tree changed in between. Re-measure the single item.
- **Used space exceeds the scan total.** That difference is the blind spot.
  State it as a number.

## When a worker and the coordinator disagree

- **The worker says REMOVE, you are not sure.** For data, the gate is
  `TABLE-THEN-STOP`: nothing runs until you have read the rows. Run the
  only-copy test yourself on every row above the size floor.
- **The worker says KEEP by a strict reading, you see it is safe.** Change the
  rule in writing (a steer or a new brief) with the evidence; do not tell the
  worker to "use judgment".
- **The worker did something the brief did not allow.** Record it, check the
  effect, tighten the contract, disclose it to the owner. Harmless is not the
  same as authorized.
- **A stalled worker holds a table you now disagree with.** Cancel it before
  anything else.
- **Two workers report different facts about one object.** Measure it yourself;
  on a busy host both may have been right at different minutes.
- **The worker's proof and yours cover different things.** Your proof that the
  commits exist elsewhere does not cover a file edited in the working copy.
  Keep the worker's re-checks even when you hand it a proof.

## Corner cases worth knowing

- **Fresh and large is usually live parallel work.** Thirty task workspaces from
  this week are thirty tasks, some finished and unclosed. The fix is closing the
  finished ones and the pipeline that never closes them, not an age cut-off.
- **Scratch folders are cited as evidence.** Documents point at scratch paths.
  Split entries into cited (keep) and uncited (remove).
- **Ignored files** are outside version control's view and are where secrets,
  local databases and rendered media sit.
- **An external or network volume appears or disappears mid-run.** A checkout
  whose parent repository lives on it changes state. Re-check when it mounts;
  never treat "path missing" on an unmounted volume as "already removed".
- **A symlinked directory inside a tree** leads outside the allowed area.
  Removal must not follow it; the gate does not.
- **A path equal to an allowed prefix** cannot be removed through the gate.
  Allow its parent and name the object exactly.
- **A tool's "finish" command removes more than you named** (dependency folders,
  caches). Read its help before putting it in a rule.
- **Records are left dangling** when a checkout is removed and nothing documents
  how to close its record. Report them; propose the missing procedure.
- **The audit is hours old.** Paths vanish and states change. Every executor
  re-verifies immediately before each removal; a missing path is "kept:
  missing", not an error.
- **Compaction of your own context.** Decisions not yet in the journal are lost.
  Write verdict rules and worker ids down before launching anything.
- **A second pass often yields little.** When a narrow pass removes one object
  out of a dozen, the zone is done: the rest is unfinished work for its owners.
- **Cleaning the cleanup.** Workers leave scratch files, helper scripts and
  temporary lists. Remove your own leftovers; keep manifests, reports and the
  journal.
