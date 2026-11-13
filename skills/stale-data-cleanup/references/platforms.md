# Platforms and tools: where the space goes and how to remove it properly

## Contents

- [How to use this file](#how-to-use-this-file)
- [Package, build and model caches](#package-build-and-model-caches)
- [What AI coding tools leave on disk](#what-ai-coding-tools-leave-on-disk)
- [Filesystem traps](#filesystem-traps)
- [The most common wrong moves](#the-most-common-wrong-moves)

## How to use this file

One rule covers every row: **inspect with the tool's list or dry-run command,
then remove with the tool's own cleanup command, while no process of that tool
runs.** Never `rm -rf` inside a store a tool manages. Tools change: paths and
flags below were checked in late 2026; run `TOOL --help` before relying on a
flag, and ask the tool for its path (`pnpm store path`, `uv cache dir`,
`brew --cache`, `go env GOCACHE`) instead of assuming it.

## Package, build and model caches

| Ecosystem | Where | Inspect | Remove | Traps |
| --- | --- | --- | --- | --- |
| npm | `~/.npm/_cacache`, `~/.npm/_npx` | `npm cache verify`, `npm cache npx ls` | `npm cache clean --force`; `npm cache npx rm` | Tool servers started through `npx` **execute from `_npx`**: not while any is running. `_cacache` is only a download cache and may be cleaned meanwhile |
| pnpm | `pnpm store path` | `pnpm store status` | `pnpm store prune` | Removes what no registered project references; projects link or clone from the store, so a project's own dependency folder frees little. Check live projects' links afterwards. Never `prune --force` |
| Yarn | `yarn cache dir`; Berry: `yarn config get cacheFolder` | — | `yarn cache clean` (`--all` in Berry) | A project's `.yarn/cache` may be committed on purpose: `git ls-files .yarn/cache` first |
| Bun | `bun pm cache` | `bun pm cache rm --dry-run` | `bun pm cache rm` | Projects using the global store need `bun install` again |
| pip | `pip cache dir` | `pip cache info` | `pip cache purge` | Virtual environments are not in this cache |
| uv | `uv cache dir` | `uv cache size` | `uv cache prune` (unused entries); `uv cache clean` (everything) | Waits for other uv processes; `--force` ignores the lock: never while `uvx` tool servers run from it. Installed tools and interpreters (`uv tool list`, `uv python list`) are software, not cache |
| Poetry, conda | `poetry config cache-dir`; `conda info` | `poetry cache list`; `conda clean --all --dry-run` | `poetry cache clear --all NAME`; `conda clean --all` | Environments are often hand-built and not reproducible: ask before removing one |
| Cargo | `~/.cargo/registry`, `~/.cargo/git`; per-project `target/` | `cargo clean --dry-run` | `cargo clean` in the project | Recent Cargo collects its home cache by itself. `~/.cargo/bin` and credentials are not cache |
| Go | `go env GOMODCACHE GOCACHE` | — | `go clean -cache`, `go clean -modcache` | The module cache is read-only on purpose: plain `rm` fails halfway |
| Maven, Gradle | `~/.m2/repository`; `~/.gradle/caches`, `wrapper/dists` | sizes | Gradle cleans itself; stop daemons (`./gradlew --stop`) before anything manual | `~/.m2` also holds artifacts installed locally that exist on no remote, and `settings.xml` |
| NuGet | `dotnet nuget locals all --list` | same | `dotnet nuget locals http-cache --clear` (or `global-packages`, `all`) | Not while an IDE or a build has the solution open |
| Homebrew | `brew --cache` | `brew cleanup -n` | `brew cleanup` | `brew autoremove` can take a formula the owner relies on |
| Playwright, Puppeteer | `ms-playwright` under the user cache; `~/.cache/puppeteer` | list the builds | `npx playwright uninstall` | Builds are shared by projects; a browser or daemon may be running from one. Remove only builds no installed version refers to |
| Terraform, OpenTofu | per-directory `.terraform/`; optional plugin cache directory | sizes | remove `.terraform/`; `init` restores it | Never the state file or the lock file. With a plugin cache, `.terraform` holds links into it. A `terraform.tfstate` **inside** `.terraform` is normally only a backend pointer: confirm by structure, not by reading values |
| Xcode | `~/Library/Developer/Xcode/DerivedData`, `Archives`, `* DeviceSupport`; simulator devices and runtimes | `xcrun simctl list devices`, `xcrun simctl runtime list` | `xcrun simctl delete unavailable`; `xcrun simctl runtime delete ID` (has `--dry-run`) | Runtimes sit under a system path and are removed through `simctl`, not `rm`. Archives hold shipped debug symbols: data. Simulator devices hold app data |
| Android | SDK directory, `~/.android/avd` | `sdkmanager --list_installed`, `avdmanager list avd` | `sdkmanager --uninstall ...`, `avdmanager delete avd -n NAME` | Emulator disks hold app data |
| Hugging Face | `~/.cache/huggingface/hub` | `hf cache --help`, then its list or scan command | its `rm` / `prune` / `delete` command | Blobs are shared between repositories: never delete them by hand. Gated models need a token to return |
| Ollama, LM Studio | `~/.ollama/models` (a service user's home on Linux); `~/.lmstudio/models` | `ollama ls`, `ollama ps` | `ollama rm MODEL`; the application's model manager | Tags share layers; a loaded model is in use. Large downloads: ask before removing a model the owner chose |
| Docker and compatible engines | the engine's data directory; on macOS and Windows a virtual disk | `docker system df -v` | by exact name: `docker image rm REPO:TAG`, `docker rm NAME`; build cache: `docker buildx prune --filter until=...` | Volumes are data. `prune` semantics differ between engines and versions (some remove named volumes). On a shared host never a prune of any kind: exact objects of finished tasks only. Never touch the data directory by hand |
| Local clusters | `~/.minikube`; kind nodes are containers | `minikube profile list`, `kind get clusters` | `minikube delete`, `kind delete cluster` | Volumes inside the cluster vanish with it |

## What AI coding tools leave on disk

Classify every directory of an agent tool before judging it:

| Class | Examples | Verdict |
| --- | --- | --- |
| **History** | session transcripts and rollouts, prompt history, per-workspace chat stores, checkpoints, generated images and attachments | The owner's. Never removed on evidence. Usually recent: measure how much is older than two months before even raising it |
| **State** | settings, credentials, memory, plugins, skills, marker files of running sessions | Never removed |
| **Cache** | logs, debug output, paste caches, extension package caches, browser profile caches | Regenerable, with the tool closed |
| **Worktree** | checkouts the tool made for a task (inside the repository or under the tool's home) | A checkout like any other: [evidence.md](evidence.md). The tool may hold a lock while an agent runs; a lock alone does not prove a live process, and its absence does not prove a dead one |
| **Runtime** | old versions beside the current one, downloaded browsers, a sandbox VM image | Old versions may go; the current and the only one stay. A VM image returns as a multi-gigabyte download and may carry session data beside it |

Things to know:

- Several tools have their **own retention**: a cleanup period for transcripts,
  a session retention setting, a cap on the number of managed worktrees. The
  right fix for growing history is that setting, set by the owner, not deletion
  by you. Non-interactive jobs can usually run without persisting a session.
- Tools **tidy their own worktrees**: on exit when clean, on archive, after a
  merged pull request, or past a count. Archiving a session in a desktop
  application can discard uncommitted changes of its worktree. Commit or park
  before anyone archives.
- Editor forks keep chats in a per-workspace and a global database that can
  reach tens of gigabytes. Deleting the database loses history and has broken
  the application; use the application's own compaction command, and treat the
  database as history.
- An orphaned per-workspace store (its folder is gone) is still history.
- Caches from which helpers execute (`_npx`, the uv tool and environment
  directories, automation browsers) are in use while any agent tool is open.
- A tool's files inside a repository (chat history files, tag caches) are
  usually ignored by version control: removing the checkout removes them.

## Filesystem traps

**APFS (macOS).**

- *Clones* share blocks. Summed sizes overstate; removing one copy frees only
  what it does not share. Measure by the volume: `diskutil info /System/Volumes/Data`
  ("Volume Used Space"), before and after.
- *Container against volume.* Free space belongs to the container, which the
  system, swap and update volumes also use. Follow the data volume's used space.
- *Local snapshots* keep deleted blocks alive: `tmutil listlocalsnapshots /`.
  They expire by themselves; thinning them is the owner's call.
- *Purgeable space* is released lazily; figures can lag for minutes after a
  large removal.
- *Sparse files* (virtual disks) report their maximum size to `ls`; trust
  allocated sizes.
- *Privacy protection.* Without full disk access a terminal cannot read the
  trash, mail, messages, browser data, photo libraries or other applications'
  containers. The scan undercounts silently; count its errors and report the
  gap. Granting access is the owner's act.

**Linux.**

- Reflinks (btrfs, XFS) and snapshots (btrfs, ZFS, LVM) behave like clones and
  local snapshots: `btrfs subvolume list -s /`, `zfs list -t snapshot`. Remove
  snapshots only through the tool that made them.
- *Deleted but open* files keep their space until the process closes them:
  `lsof +L1`.
- "No space left" with free space means inodes: `df -i`.
- ext4 keeps a reserve for root; `df` shows less available than free.
- Logs: `journalctl --disk-usage`, then `--vacuum-size` or `--vacuum-time`.
  Core dumps: `coredumpctl list`. Old package revisions: `snap list --all`,
  `flatpak uninstall --unused`. These need privileges: the owner's act.
- A container engine's storage directory is an index of layers; deleting inside
  it corrupts the engine.

**Virtual disks.** The disk image of a desktop container engine and the virtual
disk of a Linux subsystem on Windows grow and do **not shrink** when their
contents are removed. Pruning inside frees space inside. Reclaiming the host
file is a separate, documented procedure of that product; lowering a size limit
in settings can wipe the whole image. Never move or delete the file by hand.

**Cloud-synced folders.** Online-only placeholders report a full logical size
and occupy almost nothing. Reading metadata is harmless; **reading content**
(hashing, `grep`, indexing) downloads the files. Deleting a file there deletes
it in the cloud and on every device. To free local space use the provider's own
"remove download" or "online-only" action. Leave these folders out of content
scans, and treat them as personal content.

**Trash.** Moving to the trash on the same volume frees nothing until it is
emptied; each volume has its own.

**Mounts and links.** A tree may contain a mount point, a bind mount or a
network share: removing the tree would remove another filesystem's files. Check
`mount` or `findmnt`; stay on one filesystem (`find -x`, `rm --one-file-system`
where it exists; the bundled gate refuses a tree that crosses one). External and
network volumes are out of scope unless named, and a path on an unmounted volume
is not "already gone".

**Windows.** The component store, the hibernation file, the page file and the
recycle bin are managed only through the system's own tools. A developer cleanup
touches the caches above and the subsystem and container data through their own
commands.

## The most common wrong moves

1. Removing a checkout because its branch "looked merged". Prove it by commits
   and content.
2. Answering a refusal with `--force` or `rm -rf`. The refusal is the safety net.
3. Deleting session history to save space.
4. Removing a cache a running tool server executes from.
5. `prune` with volumes on a machine that has databases; a global prune on a
   shared host.
6. Hand-deleting inside a managed store: an engine's layer directory, a package
   store, shared model blobs.
7. Garbage collection "now" (`git gc --prune=now`, `jj util gc --expire now`,
   expiring reflogs) in a repository other agents are using: it ends recovery
   and can race with their writes.
8. Trusting a clean status while ignored files hold the unique things.
9. Removing a directory tree through a mount point or a symlink.
10. Deleting files in a cloud-synced folder instead of evicting them.
11. Trusting summed sizes instead of the volume's used space.
12. Reporting a small total from a scan that could not read half the home
    directory.
13. Treating everything named "cache" as regenerable: build archives with
    shipped symbols, locally installed artifacts, a committed offline cache,
    simulator and emulator data, state files.
14. Letting an application "tidy" a worktree that holds uncommitted work.
