# Desktop app sessions: Claude Desktop and the Codex app

`porch desktop` works with the sessions of two desktop apps on macOS:

- the Code tab of **Claude Desktop** (`com.anthropic.claudefordesktop`), whose session ids look like `local_<uuid>`;
- the **Codex app** (`com.openai.codex`), whose thread ids are UUIDs. The app ships inside `ChatGPT.app`.

Use it to:

- find, search and read sessions (read-only);
- start a new task for a project;
- open a session in its app;
- archive or unarchive a session.

Inside a Claude Desktop session, its own session tools (list, search, archive, unarchive, start)
are simpler. `porch desktop` is for every other agent, and for anything involving the Codex app.

Run `porch desktop <command> -h` for the full flags. `list`, `search` and `read` print JSONL with a
final `_summary`. Actions print one JSON object. A refusal prints `{"ok": false, "error": <kind>,
"message", "next"}` to stderr; `next` is the command or step to try.

| Exit | Meaning |
|---|---|
| 0 | done |
| 2 | partial or unverified |
| 3 | failed or not found |
| 4 | missing tool or permission |
| 5 | usage error or ambiguous target |
| 6 | the user must act or agree first |

## Find, search, read (read-only, any platform)

```bash
porch desktop list   [--app claude|codex|all] [--project S] [--title S] [--state active|archived|any] [--since T] [--limit N]
porch desktop search PATTERN [same filters] [-i] [--regex] [--scope convo|prompts|all|...] [--per-session N]
porch desktop read   <claude:local_…|codex:<id>|id prefix|exact title> [--tail N | --around SEQ [--context N] | --all]
```

**`list`** prints the newest sessions first. Each record has:

- `app`, `id`, `title`, `project`, `cwd` and `archived`;
- `updated` and `created`;
- Claude only: `model`, `permission_mode` and `turns`;
- Codex only: `source`, `pinned` and `first_message`;
- `url`, the app's own deep link;
- `read`, the next command to run.

**Defaults:**

- Only active sessions are listed.
- Codex threads come only from the app: `source` is `vscode`, the name the app records. Pass
  `--source cli|exec|any` for other surfaces.
- Codex subagent threads are hidden unless you pass `--include-subagents`.
- `--project` ignores case and punctuation, so `garden-planner` matches "Garden Planner".
- Claude's `project` is the root folder (`originCwd`), even when the session runs in a worktree.

**`search`** checks the title and the transcript. It prints one record per matching session, with
up to `--per-session` excerpts and a `read … --around SEQ` hint. Narrow it with `--since`,
`--project` or `--app` first: transcripts of long sessions are large.

**`read`** prints the last 20 conversation fragments by default (`--tail`). It accepts:

- a window around a search hit: `--around SEQ --context N`;
- everything: `--all`;
- other fragment kinds: `--scope all`;
- raw-source pointers: `--locators`.

**Where the data comes from** (the same readers as `porch sessions`, see
[session-history.md](session-history.md)):

| App | Metadata | Transcript |
|---|---|---|
| Claude | `~/Library/Application Support/Claude/claude-code-sessions/**/local_*.json` | the linked Claude Code transcript in `~/.claude/projects` |
| Codex | `~/.codex/state_N.sqlite`, table `threads`; the app-visible title is the `name` column | the rollout file in `~/.codex/sessions` or `~/.codex/archived_sessions` |

## Act in the apps (macOS)

```bash
porch desktop projects [--app claude|codex|all]
porch desktop start    --app claude|codex --project NAME (--prompt TEXT | --prompt-file FILE|-) [--no-send]
porch desktop start    --app codex --path /abs/folder --prompt-file -      # folder not in the sidebar
porch desktop open     <session> [--focus]
porch desktop archive  <session> [--dry-run]
porch desktop unarchive codex:<id>
```

**Requirements:**

- `agent-desktop` (`npm install -g agent-desktop`).
- Accessibility permission for the app that runs you. `agent-desktop permissions --guide` walks
  the user through it.
- The target app running with its main window open. The window may be behind others.
- For `start` in Claude, the sidebar grouped by project, because `start` uses the per-project
  "New session in …" buttons.

**How the actions run:**

- They press the app's own controls through the accessibility API. No pointer moves and no window
  comes forward, so the app the user is working in keeps focus. Every result reports `focus_kept`.
- Each action is verified in the app's store, not by the click result. `archive` waits until
  `isArchived` (Claude) or `threads.archived` (Codex) flips. `start` returns the new session's `id`.

**The user's screen is theirs.**

- `start` and `open` refuse with exit 6 (`app_in_use`) when the target app is frontmost.
- `archive` refuses when the session is the one on screen in the frontmost app.
- Ask the user, then add `--allow-frontmost`.

**A sent prompt starts an agent run** with whatever permission mode the composer shows. That is
often `Bypass permissions` in Claude and `Full access` in Codex. Use `--no-send` when the user
should press Send themselves.

| Action | Claude Desktop | Codex app |
|---|---|---|
| start | Sidebar "New session in <project>" → checks `Folder:`/`Environment:` → `set-value` prompt → Send | Sidebar "Start new chat in <project>" → checks `Change project:` → `set-value` → Send |
| open | Clicks the session's sidebar row, expanding its project group if needed (and collapsing it back if that did not help). `--focus` uses `claude://claude.ai/epitaxy/<id>` | Clicks the sidebar row. `--focus` uses `codex://threads/<id>` |
| archive | Row menu "More options for <title>" → Archive | The row's own "Archive chat" button while the app runs; `codex app-server` `thread/archive` when the app is closed |
| unarchive | Not available from outside the app: use Claude Desktop's own tool, or the app's archived list | `codex app-server` `thread/unarchive`; a running app may show the thread only after its sidebar refreshes |

### Deep links

Deep links bring the app forward. The script hands focus back to the previous app at once, but
the user sees a switch. Neither app has a parameter that sends the prompt automatically.

- `codex://threads/new?path=<abs>&prompt=<text>` opens a new thread for that folder with the
  prompt prefilled. Other parameters: `projectId`, `mode`, `originUrl`. `start --path` then
  presses Send headless.
- `claude://code/new?folder=<abs>&q=<text>` (`prompt` is an alias of `q`) prefills the Code tab.
  Claude shows **"Trust this workspace?" every time**, even for trusted folders. `start --path`
  stops there with exit 6. Never confirm that dialog for the user.
- Other links: `claude://code/continue?session=last`, `claude://code/needs-input`,
  `codex://threads/<id>`.

### Behaviour found in testing (2026-11-13)

Tested with agent-desktop 1.3.0, Claude 5.11492 and ChatGPT/Codex 26.1106.

- **Missing click actions.** Chromium can drop to a reduced accessibility mode. Elements keep
  their names but lose their click actions, so clicks fail with `POLICY_DENIED … supported_action`.
  agent-desktop restores the full mode only when the tree is empty. `porch desktop` sets
  `AXManualAccessibility` on the app before acting; it is the switch assistive tools use and
  changes nothing visible. The ChatGPT/Codex process does not support that attribute; there, a
  restart of the app restored the actions.
- **Claude's editor ignores headless `type`.** The text never reaches the app state. `set-value`
  works in both apps.
- **Keys don't work in the background.** `press` is refused when the window is not frontmost, and
  `--app Claude` is ambiguous because many processes are named `claude`. Use buttons instead.
- **Refs are per snapshot.** Use a ref right after the `find` that produced it. Claude's Send
  button is missing from `snapshot -i --compact`; `find --name Send --exact` returns it.
- **The Codex context menu is unsafe.** Right-clicking a Codex row opens a native menu whose title
  is the window's thread, so it is unclear which thread it targets. The row's own "Archive chat"
  button is unambiguous.
- **Codex app-server and the running app.** `thread/archive` from a separate app-server is
  refused for a thread the running app holds (`already has an active writer`).
- **Hidden Codex threads.** Older Codex threads sit in collapsed project groups, which don't expand
  through accessibility. Open the thread with `--focus` first; it then appears under Recents.
- **Stale Claude groups.** A Claude group can point at a deleted folder: the composer shows
  `Folder: Choose folder…`. `start` refuses to send there.
- **Overlay windows.** The ChatGPT app has extra "Computer Use" overlay windows. Window selection
  skips them and takes the largest main window.
