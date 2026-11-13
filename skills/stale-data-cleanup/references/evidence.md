# Evidence: what proves that something may go

## Contents

- [Sources, from cheap to costly](#sources-from-cheap-to-costly)
- [Finished work: checkouts](#finished-work-checkouts)
- [Regenerable things](#regenerable-things)
- [Data: the only-copy test](#data-the-only-copy-test)
- [Task and run output](#task-and-run-output)
- [Containers and virtual machines](#containers-and-virtual-machines)
- [Agent session histories](#agent-session-histories)

Age alone proves nothing. A month-old folder can be the only copy of an
experiment; a folder from this morning can be a finished lane nobody closed.

## Sources, from cheap to costly

| Source | Tells you | How far to trust it |
| --- | --- | --- |
| Filesystem metadata: size, newest file, creation time | where to look first | Weak alone. Copies preserve modification times; tools touch files |
| Liveness: a process working in it or holding files, a lock, a service definition, fresh log lines | that it is in use | Strong for KEEP. Its absence is weak: an agent session can sit idle for hours. Re-check immediately before removal; a transient process is normal on a shared host |
| Version control: status, commits that exist nowhere else, merged state, registrations, the operation log or reflog | whether work is preserved and who did what when | Strong |
| The owning tool's own verdict (a workspace manager's survey, a package manager's dry run, a simulator manager's "deletable") | removable or not, with a reason | Strong as a list of reasons. Read each blocker: some are accidents or guards, not work |
| Task records, trackers, CI | whether the task ended | Medium. Records go stale ("active" forever). A state that names a human decision is strong for KEEP |
| Project documents and rules | which paths are caches, which are stores of evidence, what must be kept | Strong when explicit |
| Agent session histories | which session made the thing and how it ended | Medium, slow. Bound every lookup |
| Remote existence: a pushed branch, a pull request's state, an object-store listing | that a copy exists elsewhere | Strong, but it is a credentialed network call: only after the owner said yes to it ([owner-decisions.md](owner-decisions.md), "Proof that needs the network"), and done by you, not by a worker. Prefer calls that write nothing locally: `git ls-remote origin` lists the remote's refs without fetching |
| The owner | everything else | Ask last, in a batch |

When two sources disagree, see [conflicts.md](conflicts.md).

## Finished work: checkouts

A checkout (a clone, a git worktree, a jj workspace, an agent's task workspace)
may go when **all four** hold:

1. **Preserved elsewhere.** Every change it holds exists outside it.
2. **Closed.** Its task ended: merged, landed, parked on a named remote ref, or
   explicitly abandoned by its owner.
3. **Idle.** Nothing outside dependency folders changed recently (24 hours for a
   checkout in a registered repository, 72 hours for a separate clone).
4. **Unoccupied.** No process works in it.

### Git

Read-only, each with `--no-optional-locks` (or `GIT_OPTIONAL_LOCKS=0`) so the
check itself takes no lock in a repository another agent is using:

```bash
G="git --no-optional-locks -C D"
$G rev-parse --git-dir --git-common-dir                   # they differ: a linked worktree, closed with `git worktree remove`
$G status --porcelain --untracked-files=all               # empty: nothing uncommitted or untracked
$G status --porcelain --ignored --untracked-files=all     # read the ignored names (`!!`): all regenerable?
$G stash list                                             # note: the stash belongs to the repository, not to one worktree
$G log --branches --not --remotes --oneline               # empty: no branch commit that is on no remote
$G log HEAD --not --branches --remotes --oneline          # empty: a detached HEAD holds nothing unique
$G merge-base --is-ancestor HEAD origin/main              # exit 0: contained in the default branch
git --no-optional-locks -C REPO worktree list --porcelain # registrations; `prunable` marks a missing directory, `locked` a claimed one
```

Traps:

- **Ignored files are invisible to a clean status** and are where unique things
  hide: `.env`, a local database, rendered media, a token file, an agent's chat
  history file. Read the ignored list; anything that is not a known regenerable
  name makes the checkout KEEP.
- **Remote-tracking refs are a memory of the last fetch.** A branch deleted on
  the server after its merge still looks "on a remote" locally, and a fetch is a
  write and a network call, so in an audit it needs the zone to allow it. When
  in doubt use the pull request.
- **Squash and rebase merges.** The branch's commits are then not ancestors of
  the default branch, and `git branch --merged` says "not merged" for work that
  is in fact upstream (and without a named ref it compares against the current
  HEAD, not the default branch). Check by content:
  - `$G cherry -v origin/main BRANCH` marks commits whose patch is upstream with
    `-`. It matches one commit to one commit, so a squash of several shows `+`;
  - compare the files the branch touched:
    `$G diff --quiet origin/main BRANCH -- $($G diff --name-only origin/main...BRANCH)`
    exits 0 when the default branch now holds the same content. When the default
    branch has moved on in those files the comparison is inconclusive;
  - with authorization, the hosting record settles it:
    `gh pr list --state all --head BRANCH --json state,mergedAt,headRefOid`. A
    merged pull request whose `headRefOid` equals the local tip means nothing
    local is newer than what was merged. A newer local tip is unique work.
- **A branch that "looks merged" by name or by pull request title proves
  nothing.** Only commits and content do.
- **Remove a registered worktree with `git worktree remove PATH`**, never with
  `rm`: plain removal leaves a stale registration and the branch still counted
  as checked out. It refuses a tree with changes or submodules; that refusal is
  the check, so never add `--force` to pass it. It does not delete the branch.
  `git worktree prune` only clears registrations whose directory is gone.
- **Removing a worktree removes its own HEAD reflog.** A commit made on a
  detached HEAD there is then found only by `git fsck` until garbage collection
  takes it. Anything unique gets a branch first.
- A repository with **no remote at all** has every commit as its only copy. A
  shallow clone cannot prove ancestry.
- Submodules and Git LFS are separate stores: check each submodule as its own
  repository. Never run garbage collection with "prune now" or expire reflogs in
  a repository other agents are using.

### Jujutsu

```bash
jj --ignore-working-copy -R REPO workspace list
jj --ignore-working-copy -R REPO log -r '(::NAME@ ~ ::trunk()) ~ empty()'             # non-empty changes not in trunk
jj --ignore-working-copy -R REPO log -r '(::NAME@ ~ ::remote_bookmarks()) ~ empty()'  # ... and on no remote
jj --ignore-working-copy -R REPO log -r '(mutable() ~ ::remote_bookmarks()) ~ empty()' # everything unpublished, any workspace
jj --ignore-working-copy -R REPO op log --limit 30                                     # who did what, when
```

- A plain `jj` command snapshots the working copy, which is a write. In the
  audit use `--ignore-working-copy`; it then cannot see edits not yet
  snapshotted, so report those as unknown. In execution a plain `jj status`
  inside the candidate is the right check: the snapshot makes stray edits
  visible.
- Close a workspace with the repository's own helper when it has one, otherwise
  `jj workspace forget NAME` and then remove the directory (recent versions also
  have `jj workspace remove`, which snapshots first and deletes ignored files
  with the directory). A snapshot that no bookmark names is easy to lose: put a
  bookmark on work that must stay.
- Do not undo or restore operations in a repository several workspaces share:
  it rewinds everyone. Do not abandon operation history or collect garbage with
  an immediate expiry there either: the operation log is the recovery path.
- A workspace manager may report "blocked" or "active" for a reason that is not
  work: a guard for recent activity triggered by another session's rebase, a
  stray commit holding one accidental screenshot, unsaved paths that are all
  build output. Establish the reason with exact commands, resolve that one
  blocker by name, and never use a force flag to skip the reading.
- A **stray commit** may be abandoned (`jj abandon REV`) only when all of this
  is shown: no description, no bookmark, no children, no workspace's working
  copy on it, and its whole content is a tool's by-product (a screenshot, a
  log) that no document cites. It is the single commit the rule names. Anything
  authored is unfinished work, whoever made it.

### Per-run clones

Pipelines that qualify a change often make a full separate clone per run and
never close it. Such a clone authors nothing but synthetic commits (an export of
a source commit, an empty "outputs" commit). It may go when its working copy is
clean, it is idle and unoccupied, and the source commit exists in the canonical
repository or on the remote: check the object where it actually lives
(`git cat-file -t SHA` in the canonical clone prints `commit`). The clone's own
store cannot prove this, and neither can a repository that never fetched it.
One modified test file in an otherwise dead clone makes it KEEP. The gate
refuses such a clone, because its export commit is on no remote: give that row
`allow=vcs` and write the proof into its evidence column. That is what the
override is for; it is not a way around a dirty working copy, which you have
just checked.

### A plain copy without version control

It cannot show its own cleanliness. Use the time of the copy:

- take the **change time** (ctime, not mtime: copies preserve mtime) of the
  oldest top-level source file as the copy time;
- list source files changed more than a few minutes after it, skipping generated
  folders (dependencies, build output, test reports, caches);
- no such file, and the task shown closed by its record or by the fate of the
  same task elsewhere: it may go. Source files edited after the copy: KEEP, and
  name them. No edits but nothing that shows the task closed: KEEP, or ASK when
  it is large.

Limit: a later recursive `chown` or `chmod` resets every change time; then the
method is silent and the verdict is KEEP.

### Unfinished work

A checkout with uncommitted changes, with commits that exist nowhere else, or
with a live process is **not stale, whatever its age**. Do not commit, land,
park or discard it for its owner. List it under "unfinished work left in
place": what it holds, whose it is (session id when known), and the one action
that would finish it.

## Regenerable things

- **Caches that a tool owns** go through that tool's own command, while no
  process of the tool runs. A cache locked by a running tool is skipped and
  reported for an idle window; do not stop the tool. Directories from which
  long-lived helpers execute (package-runner caches used by tool servers) are
  in use even when nothing wrote to them for weeks. The table of tools and
  commands is in [platforms.md](platforms.md).
- **Build output and dependency folders** of a project: no process uses the
  project, it runs no service (check service definitions, not just processes),
  and nothing outside those folders changed for days. Remove; do not reinstall.
  Uncommitted work in the project does not protect its dependency folder, but
  it tells you someone may be back: let the idle time decide.
- **Old versions beside a current one:** establish "current" at that moment from
  the symlink, the config or the running process. Never remove the current or
  the only version.
- **A cache the project documents as refillable** from a named store ("losing
  this costs downloads, never data") may go. If the store cannot be checked and
  the item is small, keep it.
- **Tools reinstallable by one command** and unused for months: no binary on
  `PATH` resolves into it, no config refers to it, no process uses it. A
  component other tools build on (compilers, command-line developer tools, the
  container CLI's backing application) stays.
- **Installers and exact duplicates:** an installer of an installed application;
  a duplicate proven by equal hashes, keeping the copy in the more deliberate
  place (documents over downloads, a project over a temporary folder). These
  may go even from a personal folder such as downloads; say so in the report.
  Everything else in a personal folder is the owner's. Removal is permanent:
  the trash on the same volume frees nothing.

## Data: the only-copy test

Before removing anything that is not regenerable, answer in writing: **where
else does this exist?**

1. Search the project's documents and rules for the path itself and for
   statements about it: "kept locally", "only here", "exists only", "not
   uploaded", "not in" the bucket, and their equivalents in the owner's
   language. A hit means KEEP that path.
2. Look for a rule that makes a folder the store of evidence or of results
   worth citing. Such a folder is kept whole, however old.
3. Check whether documents **cite the path as the place of evidence** for a
   claim. Cited entries stay; uncited siblings of the same age may go.
4. "The results are committed" does not mean "the raw data is redundant". Raw
   traces let results be re-scored; they are data.
5. A mirror in a remote store counts only when a document states the upload and
   nothing says it failed, or the listing was verified with authorization.
6. A copy of a file whose original exists elsewhere: prove it by hash, and
   record where the original is. If the original sits in a personal folder, say
   so in the closing question about that folder.
7. Does a tool read old entries again? Read the tool's source before calling its
   run history disposable.

Never removed on evidence alone: backups and database dumps, customer and user
files, live credentials. Do not read key material to judge it. Judge by
structure and metadata: a state file that is only a pointer to a remote backend
(inspect key names and the resource count, not values).

**Key-like files inside a candidate** (`*.pem`, `*.key`, `.env`) keep the whole
folder by default. They stop blocking only when you can show, without opening
key material, that they are dead test fixtures: the certificates beside them
(`openssl x509 -noout -subject -issuer -enddate`) are self-signed, named for a
test and expired, and the folder is a finished probe. Then the folder is judged
by the ordinary data rules and removed with `allow=key-files`. If that cannot
be shown, KEEP.

### Scratch areas

A project may declare a folder disposable ("scratch", "tmp", a session
scratchpad) in its own rules. That declaration is the proof for its entries,
with three conditions per entry: old enough, cited by no committed document,
read by no code. Remove entry by entry, never the folder, and keep cited
entries together with the neighbours they refer to. A folder that merely looks
like scratch, with no rule behind it, is data: run the whole test above.

## Task and run output

- The output of a task may go when its record shows a **final** state, nothing
  in it changed for a week, and no checkout of the task remains. Keep the small
  top-level receipts and reports; remove the bulk beneath.
- Use a whitelist of final states (landed, delivered, completed, closed,
  passed, cancelled). "Verified", "ready" or "published" are not final.
- **Nested fields overrule the headline state:** a record whose state looks
  final but which names a retained cache inside the output folder, pending
  cleanup, a running sub-stage or a foreign writer is not final.
- A record stuck at "prepared" or "active" for days, with no process, is a dead
  record: decide by the other evidence. Do not edit or delete records unless the
  project documents how; report records that now point at nothing.
- States that wait for a person (awaiting, paused, parked, retained, pending a
  decision) are KEEP at any age, the whole output, not only the part a field
  points at. List each with its size and the decision it waits for.
- Several complete iterations of one deliverable: keep the delivered one and
  remove the rest only when the record or the session history identifies the
  delivered one with certainty. Otherwise keep all and ask.

## Containers and virtual machines

- Remove an image only by its exact repository and tag, when its tag carries the
  id of a task whose checkout you removed under a rule (or whose record is
  final) and no container, running or stopped, uses it.
- Volumes are data. Never remove one on evidence alone.
- On a shared host: no `prune` of any kind, no deletion by age or name pattern.
  The build cache is governed by the owner's policy; report its size.
- Sizes of images share layers; the sum overstates the gain.
- The command-line client may be a link into the desktop application: the
  application then stays even if the engine is never started.
- Never clean an engine's storage directory by hand.

## Agent session histories

They answer two questions nothing else can: which session created this, and how
did that session end ("delivered", "parked", "kept locally on purpose"). With
this pack:

```bash
porch sessions roots
porch sessions list --cwd /abs/project --since 2026-01-01 --limit 50
porch sessions grep --cwd /abs/project --group-by-session 'name-of-the-folder'
porch sessions show HARNESS:SESSION_ID --around SEQ --context 3
```

Searching is reading the owner's conversations: do it yourself or give it to one
researcher whose zone section allows it, look for the candidate's path or task
name, and read only the fragments around a hit. Always narrow first (project,
dates, harness) and give each lookup a time limit; a search over a whole history
store can run for many minutes. Session history is
a cross-check and a way to find an owner, rarely the deciding proof: if it is
slow or silent, decide by version control, or KEEP. In reports cite the session
id and the one fact relied on; quote nothing else. Session history itself is
never a cleanup candidate.
