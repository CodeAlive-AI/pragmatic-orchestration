# Session history search and navigation

Read-only access to local coding-agent session histories. `list`, `grep`, `show`,
`turns`, `flags` and `stats` scan the stores directly; `find` and `trace` use the
disposable index below. Raw records carry
native locators (file + line, or db + table + key).

Claude Code prompt fragments may include `origin` and `origin_basis` describing
the record's authorship evidence. Assistant fragments may include `native`
message identifiers and stop reasons. Claude Code session headers include a
native ID, class, lineage edges and source paths; Desktop manifests include
their links and native status facts. An edge points from the session to its
source or parent and includes a locator for its evidence.

Codex session headers include lineage and the selected rollout; interpreted
turns carry abort facts. A resolvable state `rollout_path` takes precedence;
otherwise the newest rollout is selected and multiple revisions are marked
`ambiguous`, with the other paths listed as alternates. Raw listing and grep
continue to read each rollout separately. Turn interpretation prefers explicit
final-answer phases, then the terminal event's saved answer, then unphased
assistant text; commentary never becomes a final answer, and an abort
discards only the unfinished tail of a turn. Every completed answer of a turn
is kept (`finals`), not only the last one.

Supported harnesses: `claude-code`, `codex` (CLI + Desktop), `opencode`,
`agy` (Antigravity CLI),
`grok`, `devin` (CLI/Desktop), `gemini`, `cursor`, `claude-desktop`,
`qwen-code`, `kimi-code`, `omp`, `hermes`, plus `porch` for this skill's own
delegate/review runs.

## Fast search: find and the index

The index contains titles, genuine prompts with their `human`, `delegated` or
`unknown` origin, and every completed answer. It does not index tool output,
reasoning, arbitrary intermediate assistant text, or unfinished answers. Indexed
harnesses are Claude Code, Claude Desktop/Cowork, Codex, Devin, OpenCode, agy,
porch, and Cursor's `chats` and `acp` stores. The Cursor IDE store and the
other remaining harnesses use raw `list`, `grep` and `show` only.

```bash
porch sessions find 'задач' 'найти|нашел|поиск'
porch sessions find 'error' 'build|test' -a codex,claude-code --kind answer --since 2026-01-01
porch sessions find 'review' --origin human --class interactive --cwd my-project
porch sessions show --at '<locator from evidence>'
porch sessions index status
porch sessions index build [--full]
porch sessions index drop
```

Each group is a literal normalized text fragment; groups are ANDed across the
session, while `|` separates alternatives (`\|` is a literal bar). Put groups
that begin with `-` after `--` (`porch sessions find -a codex -- '--continue-run'`). Filters also
include `--until`, `--limit` and `--deadline`; see `find --help`. Evidence includes
an exact `show --at` command. A changed source can be relocated by message ID;
without a stable anchor, search again.

Every `find` first does a stat-first sync and reports freshness and errors in its
`_summary` coverage. Devin in-place edits of old messages that change no
fingerprint field are not detected; `porch sessions index build --full` re-reads
everything. `status` reports the last committed sync without syncing; `build`
has no deadline and shares the raw-scan queue and memory budget; `drop` removes
only the index. Indexes created before sync coverage was recorded show no last
sync time or coverage until the next sync. If incomplete, coverage names the
next command.

On an empty index, the first `find` builds until its deadline (20 seconds by
default) and answers from the partial index with coverage saying so. Run
`porch sessions index build` once before relying on find; a local build took
about 7 minutes for roughly 46 GB of history.

The private, disposable accelerator defaults to
`~/Library/Caches/pragmatic-orchestration/sessions-index/index-v1.sqlite` on
macOS, `$XDG_CACHE_HOME/pragmatic-orchestration/sessions-index/index-v1.sqlite`
on Linux (`~/.cache` when unset), and
`%LOCALAPPDATA%\pragmatic-orchestration\sessions-index\index-v1.sqlite` on
Windows. `PORCH_SESSIONS_INDEX_DIR` overrides its directory; `PORCH_STEER_DIR`
also changes the default to a sibling `sessions-index`. On Unix its directory is
mode 0700 and database mode 0600. A measured local index was about 1.4% of its
source volume; size varies with the stores.

## Trace: lineage and what was left behind

Use `porch sessions find` to get a REF, then run `porch sessions trace REF [REF ...] [--depth N] [--max-nodes N] [--evidence]`. To survey recent unfinished, uncollected or orphaned work, use `porch sessions trace --left-behind [--since 7d] [--cwd DIR] [-a HARNESS]`; the default window is seven days. For example, with a session named `claude-code:fixture`:

```bash
porch sessions find 'fixture'
porch sessions trace claude-code:fixture --evidence
porch sessions trace --left-behind --since 7d --cwd /fixture -a claude-code
```

Trace syncs before reading, with the same 20-second default deadline, busy-lock snapshot and coverage as find. On an empty index the first trace may answer from a partial build; run `porch sessions index build` first, as above. Its single JSON document contains `nodes`, parent-to-child `edges`, `unresolved`, and `_summary` (seeds, coverage, verdict counts and, if truncated, a next command). Default nodes carry compact task and answer snippets, turn state, plan counts, run status and collection, and verdicts with short evidence descriptions. `--evidence` adds plain `show --at` commands where a source can be addressed (otherwise its locator), plus aliases, native status, artifacts, deliveries and full plan items. Use it to inspect or substantiate a verdict; a pointer may require a new search if its source changed.

| Source | Lineage evidence |
|---|---|
| Claude Code | `subagent` from transcript paths |
| Claude Desktop Code tab | `spawned` / `fork` / `continuation` from `spawnedFrom`, `forkedFromSessionId`, `priorCliSessionIds` |
| Codex | `subagent` from thread-spawn metadata or state edges; `fork` from `forked_from_id` |
| OpenCode 1.x/2.x | `subagent` from `parentID` / `parent_id`; native fork metadata where present |
| agy | `subagent` from brain descriptors |
| porch | `caller_run` via launcher-key aliases; `worker_session` via backend and native id; `parent_run` and `continuation` from run metadata |

Aliases merge identities; they are not edges. A missing target appears in `unresolved` with its coverage-based reason; trace does not guess a link.

For a run with final text, **collection** is true for a delivery record at or after finish, or an attributed, successful caller `wait` (not quiet), `result`, final.txt read or foreground result after finish. A final.txt read is a Read tool or a shell reader (`cat`, `head`, `tail`, `sed`, `less`, `more`, `bat`, `awk`, `jq`, or Python/Node with `open`/`readFile` and a literal path in the same command segment); path checks and listings only count as mentions. Output supplies run targets only from porch progress or detached-launch `run_id=` fields or, when JSON was requested (or is the command’s format), a JSON `run_id`; answer prose is not parsed as JSON evidence. Collection is false only when the resolved caller and its fork/continuation descendants have complete content and store walks, are inactive for 30 minutes, have no observations of the run other than its detached launch, and have no unattributed read or wait after finish. Otherwise it is unknown; without final text it is null. **Unfinished** marks a nonterminal, failed or cancelled run, an idle open or aborted last turn, native error/interruption flags, or incomplete plan items. An open turn active within 30 minutes is **active** instead. **Uncollected** means collection is false. **Orphaned** is true when a recorded parent/caller is absent from a completely walked store, or a positively identified delegated worker has no porch run reference and the porch registry was completely walked. A subagent whose parent was not recorded, an incomplete walk, or a run without a recorded launcher is unknown; other headless sessions have no orphan verdict. Each verdict gives its reason and evidence. Offset-less stored timestamps are interpreted as UTC for verdict comparisons.

Caller-side collection observations come only from Claude Code and Codex transcripts. Delivery records exist only for deliveries made with porch 5.9.0 or later; older-run collection is heuristic, and dynamic commands without an attributable run stay unknown. Desktop `wrap_up` closes only the in-progress plan item. Status texts are not searchable with find.

## Command surface

```bash
porch sessions roots  [-a h1,h2] [--store SUBSTR]
porch sessions list   [-a h1,h2] [--store S] [--cwd S] [--cwd-exact] [--title S] [--since T] [--until T] [--limit N]
porch sessions grep   [-a h1,h2] [--scope S] [-i] [--regex] [--group-by-session] [--limit N] PATTERN
porch sessions show   <harness:session-id> [--scope S] [--limit N]
porch sessions show   <harness:session-id> --around SEQ [--context N]
porch sessions show   --file PATH          # sniff a single file/db
```

For the sessions of the Claude Desktop app and the Codex app — with their
app-visible titles, archive state, and actions to start, open and archive them —
use `porch desktop` ([desktop-apps.md](desktop-apps.md)).

`history` is an alias of `sessions`. Raw scan output is JSONL on stdout: session
headers (`_session`), fragments, and a final `_summary` record; `find` is JSONL
and `trace` emits one JSON document. Diagnostics go
to stderr; exit codes are 0 ok / 2 partial / 3 hard failure. See the
[compatibility matrix](session-compatibility.md) for per-format evidence.

Each process has a 1536 MiB memory budget and content scans share two advisory
lock slots per user, bounding concurrent scans to about 3 GiB total. Set
`PORCH_SESSIONS_MEMORY_LIMIT_MB=0` to disable the per-call budget;
`PORCH_SESSIONS_MAX_PARALLEL=0` disables the queue. `PORCH_SESSIONS_QUEUE_TIMEOUT_S`
sets the default 600-second wait. Exit 6 means the memory budget stopped the
scan; exit 7 means it timed out waiting for a slot. `roots` reports when the
current platform cannot provide either safeguard. A budget stop can leave
partial output on stdout; exit 6 and the absence of a final `_summary` record
mean the output above is incomplete.

## Platform defaults

All commands run on macOS, Linux, and Windows. On Windows use the sibling
`porch.cmd` launcher (sessions/quota run on native Python; review/delegate
forward to bash — Git Bash, MSYS2, or WSL required for those). The reader
itself is platform-neutral; only the *default store roots* differ:

| harness | macOS / Linux | Windows |
|---|---|---|
| `claude-code` | `~/.claude/projects` | `%USERPROFILE%\.claude\projects` |
| `codex` | `~/.codex` (or `$CODEX_HOME`) | `%USERPROFILE%\.codex` (or `%CODEX_HOME%`) |
| `opencode` | `~/.local/share/opencode` | `%LOCALAPPDATA%\opencode` |
| `agy` | `~/.gemini/antigravity-cli` | `%USERPROFILE%\.gemini\antigravity-cli` |
| `grok` | `~/.grok/sessions` | `%USERPROFILE%\.grok\sessions` |
| `devin` | `~/.local/share/devin/cli` | `%APPDATA%\devin\cli` and `%LOCALAPPDATA%\devin\cli` (both checked) |
| `gemini` | `~/.gemini` | `%USERPROFILE%\.gemini` |
| `claude-desktop` | `~/Library/Application Support/Claude` | `%APPDATA%\Claude` |
| `cursor` | `~/Library/Application Support/Cursor/User` | `%APPDATA%\Cursor\User` |
| `qwen-code` | `~/.qwen/projects` | `%USERPROFILE%\.qwen\projects` |
| `kimi-code` | `~/.kimi-code` (or `$KIMI_CODE_HOME`) | `%USERPROFILE%\.kimi-code` (or `%KIMI_CODE_HOME%`) |
| `omp` | `~/.omp/agent/sessions` | `%USERPROFILE%\.omp\agent\sessions` |
| `porch` | `~/Library/Caches/pragmatic-orchestration/steer` (macOS), `$XDG_CACHE_HOME/pragmatic-orchestration/steer` (Linux) | `%LOCALAPPDATA%\pragmatic-orchestration\steer` |

Hermes uses `~/.hermes/state.db` (`%USERPROFILE%` on Windows). Cursor also
checks `~/.cursor/acp-sessions` and `~/.cursor/chats` on every platform,
including Windows (`%USERPROFILE%\.cursor\…`, not AppData).

`PORCH_HISTORY_ROOT_<HARNESS>` overrides any default (uppercase, `-`→`_`),
`PORCH_STEER_DIR` overrides the porch root. `PORCH_HISTORY_ROOT_CURSOR`
replaces every Cursor store. Point it at the chats directory
(`<32-hex workspace hash>/<chat-uuid>/meta.json`), at the acp-sessions
directory (`<uuid>/`), or at the Cursor config root (`~/.cursor` or a
`CURSOR_CONFIG_DIR` directory): a root containing `chats/` or
`acp-sessions/` children is expanded into both stores. A root whose
children are 32-hex hashes of chat directories is the CLI `chats` store. A
one-level `<uuid>/` root stays the ACP store. A non-empty override matching
none of these layouts is reported `unavailable` with an explicit message
naming the expected shapes instead of silently listing nothing. On Linux
`claude-desktop` and the Cursor IDE root fall back to `~/.config/…`. SQLite
stores open read-only via a `file:` URI with `mode=ro` + `PRAGMA query_only=ON`;
drive-letter paths (`C:\…`) are URI-escaped internally — pass them as normal
paths.

## The navigation algorithm

Follow these steps in order. Each step narrows before the next one reads more
data — do not grep before you know which sessions exist.

1. **Discover stores** — `sessions roots`. Each store reports `status`
   (`ok`/`missing`/`empty`/`unavailable`) and `evidence` (`verified` =
   confirmed against a live install, `spec` = format documented but only
   fixture-verified, `guess` = best effort). Note which harnesses are absent;
   absence is a fact about the machine, not an error.
2. **List candidate sessions** — `sessions list`. Metadata scan (Cursor
   sessions also read each `store.db` header for title and created):
   session id, title, cwd, created/updated, models. Filter with `--cwd`,
   `--since/--until`, `-a`, `--store`. Session ids are stable addresses —
   keep them verbatim for `show`. `--since` drops a session with no timestamp
   at all, and drops one whose latest known timestamp (created or updated) is
   before the bound. `--until` drops a session only when every known timestamp
   is after the bound; a session with no timestamp still passes an
   `--until`-only filter. A session created before the window and updated
   inside it still matches `--since`.
3. **Search text** — `sessions grep PATTERN`. Runs over decoded fragment text
   (not raw bytes), honors the same filters. Each hit is a fragment with
   `kind`, `authorship`, `ts`, `model`, `seq`, and `locator`. Default
   `--scope convo` emits only `prompt` + `assistant` fragments; widen with
   `--scope all|tools|system|reasoning|prompts`.
   Matching scans the full fragment; `--max-chars` limits only the emitted
   excerpt around the first match. `text_offset` is the excerpt's character
   offset in the full fragment. Use `--max-chars 0` to emit the full text.
   `list --title` filters recorded titles case-insensitively. For `list` or
   `grep`, combine `--cwd` and `--cwd-exact` to distinguish similarly named
   project paths. `grep --group-by-session` emits one object per source
   session with a `matches` array; `--limit` still counts matching fragments.
   For a literal pattern, grep first reads each claude-code and codex
   session file (and the transcript behind a claude-desktop code session) as raw bytes (`.jsonl.zst` decompressed). Only lines that
   hold the pattern (or a JSON escape of one of its characters) are parsed,
   and only a session where one of them yields a match in scope is parsed in
   full; the rest are skipped. A word that every session carries in a
   bookkeeping record, such as the skill listing, therefore costs a byte
   scan plus a parse of those lines. The output is the same as without that
   step.
   `--regex`, a pattern with a quote, backslash or control character, one
   made only of JSON syntax (`true`, `: 5`) or a fragment of the keys porch
   adds to tool calls (`name`, `input`, `arguments`) parse every session;
   `_summary.prefilter` says which applied and why. `--cwd` reads only the
   head of a claude-code session to learn its cwd, and title, dates and
   models are read only for sessions that have a match.
4. **Read around a hit** — `sessions show <harness:id> --around SEQ --context N`.
   `seq` is the fragment's native sequence position within its session;
   `--context` emits N *emitted* fragments on each side — counted after
   `--scope` filtering, so a scope-filtered window may span more seqs than
   2N+1. `flags` evidence carries `seq` directly — feed it to `--around`.
5. **Open the raw record** — every fragment's `locator` points at the source:
   `{"file": ..., "line": N}` for JSONL stores, `{"db": ..., "table": ...,
   "key"/"row_id": ...}` for SQLite, `{"dir": ...}` for directory stores.
   When normalized classification is in doubt, read the raw record — it is
   the ground truth.
6. **Check coverage** — the `_summary` record reports `sessions_scanned`,
   `fragments_emitted`, `malformed_records`, `truncated_tail_files`,
   `unreadable_files`, `scan_complete`. If `scan_complete` is false or
   malformed counts are high, say so — an empty result under an incomplete
   scan means "not found in the readable part", not "does not exist".
   For grep, `prefilter.skipped_sessions` counts sessions ruled out from raw
   bytes; malformed and truncated counts cover the parsed sessions only.

### Finding "what the human actually asked"

```bash
porch sessions grep --scope prompts --since 2026-01-01 "deploy"
```

`--scope prompts` emits only `kind=prompt` fragments. Within that set,
`authorship=human` marks genuine user input; `authorship=agent` marks
orchestrator-injected user-role records (task notifications, IDE context
blocks, `<system-reminder>` wrappers, environment bootstrap). The `basis`
field explains why the classifier decided as it did — quote it when the
distinction matters.

### Following branches, forks, and subagents

- `rel` on a fragment carries native link ids: `parent`, `uuid`, `sessionId`,
  `parent_node_id`, `parent_thread_id`, `subagent` — whatever the harness
  recorded. Collect these from `show` output to walk a tree.
- Session-level `parent` on `sessions list` output marks subagent/sidechain
  sessions where the harness records the linkage (opencode `parent_id`,
  claude `subagents/` transcripts, codex `thread_spawn_edges` +
  `source.subagent.thread_spawn`, claude-desktop `priorCliSessionIds` /
  `cliSessionId`).
- Forks and rewinds appear as repeated `sessionId`/`message_id` at different
  sequence positions (devin node forest, claude `parentUuid` chains). Treat
  the file order (`seq`) as the canonical timeline; reconstruct trees only
  when `rel` fields support it.

## Normalized fragment schema

Every emitted fragment:

| field | meaning |
|---|---|
| `harness`, `store`, `session_id` | provenance |
| `seq` | native sequence position (line order or row order) |
| `kind` | `prompt` / `assistant` / `reasoning` / `tool_call` / `tool_result` / `context` / `permission` / `compaction` / `boundary` / `metadata` / `unknown` |
| `authorship` | `human` / `agent` / `system` / `unknown` (omitted when `unknown`) |
| `native_type` | the record/part type as the harness named it |
| `locator` | `file`+`line`, `db`+`table`+`key`/`row_id`, or `dir` |
| `text` | decoded text, capped at `--max-chars` (`truncated: true` when capped) |
| `ts` | record timestamp, ISO-8601 |
| `model` | model id when the record carries one |
| `cwd` | session working directory when known |
| `visible` | false for internal/scratchpad records |
| `basis` | why the classifier chose `kind`/`authorship` |
| `rel` | native relationship ids (see above) |
| `status` | record status when meaningful (e.g. `stop`, `error`) |
| `origin`, `origin_basis` | prompt provenance: `human` / `delegated` / `injected` / `unknown` + why |
| `native` | harness-native evidence dict (e.g. agy `step`/`status`/`has_tool_calls`, opencode `finish`/`completed`/`after_last_tool`) |

`kind` and `authorship` are deliberately separate axes: a `kind=prompt`
record is user-*shaped*, while `authorship` says who actually wrote it.
Never infer authorship from `kind` alone — check `authorship` and `basis`.

## Per-harness store map and format recipes

### claude-code — `~/.claude/projects/<encoded-cwd>/**/*.jsonl` (+ `~/.claude/history.jsonl`)

- One JSON object per line. `type` ∈ `user`, `assistant`, `system`,
  `summary`, `file-history-snapshot`, …
- Genuine prompts: `type=user` with string `message.content`, or content
  blocks that are not `tool_result`. `origin.kind` may name the producer
  (`human`, `task-notification`, …); `isMeta=true` and `isSidechain=true`
  mark technical/subagent records — classify them `system`/technical, keep
  `authorship=agent` for orchestrator-injected user records.
- Command and reminder wrappers classify a text block as context only when
  they begin it; quoted wrappers inside a prompt remain prompt text. As a
  result, grep may now show such quoted text under `prompt` instead of `context`.
- `parentUuid`/`uuid` chain gives conversation order; `sessionId` groups
  records; `subagents/` subdirectories hold sidechain transcripts whose
  `agentId`/`promptId` identify the subagent.
- Session id used by `show`: the file path relative to the project dir
  without `.jsonl` (e.g. `-Users-rodio/1f0e382c-…`); a bare uuid also
  resolves if unique.
- `history.jsonl` is a flat prompt log (`display` text + `project`), useful
  for prompt search without per-session context.

### codex — layered: canonical rollouts + state index + App/Desktop catalog

Codex stores the same threads in several layers with different authority.
`roots` shows them as separate stores; do not merge them mentally.

| store | path | authority |
|---|---|---|
| `sessions`, `archived` | `~/.codex/{sessions,archived_sessions}/YYYY/MM/DD/rollout-*.jsonl[.zst]` | **canonical conversation body** |
| `state:state_N.sqlite` | `~/.codex/state_*.sqlite` | canonical thread index (all surfaces: CLI, VS Code, exec) |
| `catalog` | `~/.codex/sqlite/codex-dev.db` → `local_thread_catalog` | Desktop/sidebar catalog — includes `chatgpt` cloud threads with **no local transcript** |
| `summaries` | `~/.codex/sqlite/codex-thread-summaries-dev.db` → `thread_turn_summaries` | app-generated summaries, not messages |
| `history` | `~/.codex/history.jsonl` | flat prompt log `{session_id, ts, text}` |

- `threads` (state index): `rollout_path` links the index row to the
  canonical rollout file — `show` on a `state:*` session reads through to
  the rollout and emits real fragments. `first_user_message`, `preview`,
  `git_branch`, `archived`, `tokens_used`, `model`, `reasoning_effort`,
  `approval_mode`/`sandbox_policy` are index-level metadata.
- **Subagent lineage**: `threads.source` may be a JSON object
  `{"subagent": {"thread_spawn": {"parent_thread_id", "depth",
  "agent_nickname", "agent_path"}}}`; `thread_spawn_edges` maps
  parent→child. Both are surfaced as `parent`/`agent` on the session.
- `catalog` rows carry `source_kind` (`vscode`/`cli`/`exec`/`chatgpt`/
  subagent JSON), `host_id`, `missing_candidate`. ChatGPT-cloud and stale
  rows have **no local rollout** — `show` emits an `index_row` metadata
  fragment (`basis: "no local rollout file"`) plus the catalog/thread
  summary if one exists; it never fabricates a conversation.
- Rollout JSONL record types: `session_meta` (cwd, originator, cli_version,
  model_provider), `turn_context` (per-turn model/effort/approval policy),
  `response_item` (typed payload: message/reasoning/function_call/…),
  `event_msg` (user-facing events), `token_count` (cumulative usage — never
  sum across records), `compacted`/`turn_aborted` markers.
- Compressed Codex rollouts require Python 3.14+ (stdlib Zstandard). With an
  older runtime or a corrupt archive the reader reports an incomplete scan.
- **Genuine prompts come from `event_msg` with `user_message` payload**, or
  `response_item` `message` with `role=user` whose text is not bootstrap
  material. `response_item` user-role messages containing
  `<environment_context>`, AGENTS.md content, or tool schemas are bootstrap
  context — `kind=context`, `authorship=system`.
- Real-world files can contain corrupted interior lines (truncated records,
  binary spills) — counted as `malformed_records`, never silently skipped.
- `history` sessions are grouped prompt-log entries (`native_type
  history.entry`, `authorship=unknown`): useful for prompt search, not a
  transcript.

### hermes — `~/.hermes/state.db`

- Reads current `sessions` and `messages` SQLite tables read-only; legacy JSON
  exports are not included. Source configuration and system prompts are not read.
  User-role authorship is unknown because the record does not establish whether
  the text came from a person or an integration. Fixture-verified.

### opencode — `~/.local/share/opencode/` (2.x SQLite + 1.x file storage)

Three generations are read, in precedence order `session_v2` > legacy tables
> file storage; each session id is emitted once from its best source.

- **2.x** — `opencode.db` (or `opencode-*.db`, `$OPENCODE_DB`,
  `$XDG_DATA_HOME/opencode`): `session_v2`, `session_message`,
  `session_part` tables, read-only. `session_message.type` ∈
  `user`/`assistant`/`shell`/`synthetic`/`system`/`skill`; assistant parts may
  be inline `content[]` or `session_part` rows (stored rows first, then
  inline). `user` text starting with the subagent-spawn prefix or the porch
  executor prefix classifies `delegated`; `synthetic`/`system`/`skill` rows
  are `injected` context. `parent_id`/`fork_session_id` give `subagent`/`fork`
  edges; `time_idle`/`idle_outcome`/`time_busy`/`time_archived` land in
  `native_status`. Final answer = last completed assistant message
  (`time.completed`) whose `finish` is `stop`/`length`/`content-filter`,
  taking the text after its last tool part.
- **1.x legacy tables** — same DB's `session`, `message`, `part` tables
  (`data` JSON columns, same shapes as file storage). Read only for sessions
  absent from `session_v2`.
- **1.x file storage** — `storage/session/<project>/<sid>.json`,
  `storage/message/<sid>/*.json`, `storage/part/<mid>/*.json` under the
  OpenCode data dir (kind `json_dir`, store name `storage`). Read only for
  ids not claimed by any sibling DB. V1 part types: `text` (user text parts
  are prompts unless `synthetic`/`ignored`; `subtask` parts carry delegated
  briefs), `reasoning`, `tool` (`state.status`/`input`/`output`),
  `step-start`/`step-finish` (`reason` ∈ `stop`/`length`/`tool-calls`/…),
  `file`, `agent`, `compaction`, `retry`, `patch`. Final = text parts of the
  last step whose `step-finish.reason` is `stop` or `length`.
- In-memory databases (`OPENCODE_DB=:memory:`) have no disk history.

### agy — `~/.gemini/antigravity-cli/` (Antigravity CLI)

- Sessions = union of `cache/conversation_metadata.json` `conversations`,
  `brain/<cid>/` dirs, and `conversations/<cid>.db` names (the `.db` files
  are **never opened** — read-only SQLite opens create `-wal`/`-shm`
  sidecars in the user's store).
- Content: `brain/<cid>/.system_generated/logs/transcript_full.jsonl`
  (falls back to `transcript.jsonl`), records `{source, type, status,
  step_index, created_at, content, thinking, tool_calls, model}`.
  `USER_EXPLICIT/USER_INPUT` → prompt from inside `<USER_REQUEST>` only
  (other blocks become hidden context); `SYSTEM/SYSTEM_MESSAGE` with a
  `[Message] … sender=<parent>[/task-N]` envelope → `delegated` prompt;
  other `SYSTEM_*` → injected context; `MODEL/PLANNER_RESPONSE` →
  assistant (+`thinking` reasoning, +`tool_calls`); `MODEL/GENERIC` →
  tool_result. Every fragment carries `native.step` = `step_index`.
- Titles: `annotations/<cid>.pbtxt` `title:"…"` overrides metadata `Title`;
  cwd from `WorkspaceURIs` `file://` URIs. Subagent edges from
  `brain/<parent>/.system_generated/subagents/<child>.json` descriptors or
  the SYSTEM_MESSAGE sender fallback.
- `surface`: `cli` for CLI rows, `ide` for metadata rows with
  `AppDataDir == "antigravity"` (metadata-only: one `ide_row` fragment,
  `content_unavailable` always). `native_status`: `NumSteps`,
  `content_unavailable` (no transcript / IDE row), `content_stale`
  (transcript mtime older than `UpdatedAt` by >60 s). The NumSteps-vs-
  transcript check runs post-read via `agy_content_stale(ref, fragments)`.
- Turn rule: a turn's final is its LAST assistant fragment only when that
  PLANNER_RESPONSE has non-empty content, no `tool_calls`, `status=DONE`;
  a live transcript tail (tool calls pending, thinking-only, trailing
  prompt) leaves the last turn open.

### grok — `~/.grok/sessions/<encoded-cwd>/<session-id>/`

- `summary.json`: session id, cwd, created/updated, generated title, model,
  reasoning effort, agent name, message counts.
- `chat_history.jsonl`: typed records — `system`, `user`, `reasoning`,
  `assistant`, `tool_result`, `backend_tool_call`. A `user` record whose
  text starts with `<system-reminder>` is technical context, not a prompt.
- `prompt_history.jsonl` is the clean prompt-only stream; `events.jsonl`,
  `updates.jsonl`, `terminal/` are auxiliary.

### devin — `~/.local/share/devin/cli/sessions.db` + `~/.local/share/devin/cli/transcripts/`

- `sessions` table: id (slug like `mature-band`), title, working_directory,
  model, agent_mode (`normal`/`accept-edits`/`bypass`), hidden flag,
  created/last_activity, `main_chain_id`.
- `message_nodes` table: a **node forest** — `node_id`/`parent_node_id`
  edges encode branches and rewinds; the same `message_id` may appear at
  several nodes. `role` ∈ `user`/`assistant`/`system`/`tool`; `seq` output
  order is node order.
- `transcripts/<id>.json`: ACP-style `{session_id, steps[]}` exports when
  present.
- Desktop/session UI state under `~/Library/Application Support/Devin` and
  `~/.devin` is not a transcript source.

### gemini — `~/.gemini/tmp/<project-hash>/chats/session-*.json`, `~/.gemini/projects.json`

- Whole-session JSON: `sessionId`, `messages[]` with `type`
  (`user`/`gemini`/`tool`/`info`), `model`, timestamps. `projects.json`
  maps project hash → cwd.

### cursor — `~/Library/Application Support/Cursor/User/{globalStorage,workspaceStorage/*/}state.vscdb`

- CLI chats under `~/.cursor/chats/<32-hex workspace hash>/<uuid>/` (store
  `chats`, session id `cursor-cli:<uuid>`) on every platform. `meta.json`
  supplies cwd and created/updated (`createdAtMs` / `updatedAtMs`). `store.db`
  has `blobs` and `meta`; meta key `0` is hex-encoded JSON with `agentId` and
  `latestRootBlobId`. The root blob's field 1 lists JSON message blobs. The
  prompt is the inner text of `<user_query>`, or plain user text with no
  wrapper. A user `content` given as a list of parts is joined first: text
  parts (and bare strings) are concatenated with `\n`, then `<user_query>`
  is extracted from the joined text — `json.dumps` would escape the wrapped
  newlines. System text, `<user_info>` and reminder wrappers are hidden
  context. Assistant text comes from JSON `type=text` parts. Reasoning text
  is read from the turn-step protobuf (root field 8) only when the latest
  assistant message's JSON reasoning text is empty; an inline assistant step
  that repeats the JSON answer is not emitted again. Tool calls are the JSON
  `tool-call` parts (projected as `tool_call` fragments with name and args)
  and `role=tool` messages' `tool-result` parts (projected as `tool_result`
  fragments), both visible under `--scope tools`. A turn-step tool record
  (root field 8, call id at protobuf field 57) whose id never appeared in
  JSON still counts as `unsupported_fragments`. A chat with only
  `meta.json` is listed empty. One unreadable chat does not hide the others;
  the store stays `ok` while any chat lists. `sessions find` indexes this
  store one unit per chat directory. Live-checked read-only
  against Cursor CLI 2026.11.19-f005e9d on 2026-11-19. Harness evidence
  stays `spec` because the IDE store is still fixture-only.
- ACP sessions under `~/.cursor/acp-sessions/<uuid>/` (store `acp`, session id
  `cursor-acp:<uuid>`) use the same `{meta.json,store.db}` layout minus the
  workspace-hash level and the exact same graph — one shared decoder serves
  both stores; only the directory layout, session-id prefix and error labels
  differ. An ACP prompt may have no `<user_query>` wrapper: the turn's user
  step (turn field 1) carries the bare prompt text and promotes the JSON user
  record that only classified as wrapper context. Cancelled turns keep what
  Cursor recorded — partial assistant text, the tool call, and the recorded
  tool result such as "Shell command was interrupted" — nothing is invented.
  A session dir holding only `meta.json` (aborted before the first turn
  persisted) lists empty. One unreadable session does not hide the rest: it
  counts in `unreadable_dirs`/`unreadable` while the store stays `ok`.
  `sessions find` indexes both stores one unit per session directory, skipping
  `store.db-wal/-shm/-journal` churn. Live-checked read-only against Cursor
  CLI 2026.11.19-f005e9d on 2026-11-19 — six sessions covering plain and
  wrapped prompts, assistant text, reasoning, shell tool calls/results, a
  cancelled turn and a mid-turn steer. Harness evidence stays `spec` because
  the IDE store is still fixture-only.
- VS Code KV sqlite (`cursorDiskKV` table). Sessions are `composerData:<id>`
  keys: `name` (title), `createdAt`, `lastUpdatedAt`, `usageData` (per-model
  token counters).
- Message bodies are under `bubbleId:<composer>:<msg>` keys in the same db.
- Workspace path: `workspaceIdentifier.uri.fsPath` in `composerHeaders`
  when present, else `workspace.json` next to the workspace `state.vscdb`.

### claude-desktop — two layers: `cowork` + `code-sessions`

`~/Library/Application Support/Claude/` holds two unrelated session trees.

**`cowork`** — `local-agent-mode-sessions/<org>/<user>/local_<id>{.json,/}`:

- `local_<id>.json` is the session manifest: `title`, `cwd`, `model`,
  `effort`, `permissionMode`, `isArchived`, `createdAt`/`lastActivityAt`,
  and **`initialMessage` — the user's first message, always emitted as a
  `prompt` fragment with `authorship=human`**.
- `local_<id>/audit.jsonl` is the canonical Cowork record: append-only,
  HMAC-chained (`.audit-key` sibling — **never read it**). Record types:
  `user`/`assistant` messages (same taxonomy as claude-code), `system`
  `init`/`permission_request`/`permission_response` (→ `metadata`/
  `permission`), `result` (turn boundary with usage → `boundary`),
  `rate_limit_event` (→ `metadata`). `parent_tool_use_id` marks subagent
  records → `authorship=agent`.
- The audit log **re-emits the triggering user message after each `init`
  boundary** — duplicates across turns are the format's own bookkeeping,
  not reader noise.
- `local_<id>/.claude/projects/**` are inner claude-code subprocess
  transcripts — signposted as `inner_transcript` metadata, never scanned
  twice (they are not under `~/.claude/projects`).

**`code-sessions`** — `claude-code-sessions/**/local_<id>.json`:

- Desktop metadata for the embedded code tab: `sessionId`, **`cliSessionId`
  → the link to the canonical transcript** at
  `~/.claude/projects/<encoded-cwd>/<cliSessionId>.jsonl`, plus
  `priorCliSessionIds` (session continuation chain), `title`,
  `permissionMode`, `enabledMcpTools`, `completedTurns`, `isArchived`.
- `show` emits the manifest as `metadata`, then delegates to the linked
  claude-code transcript (fragments keep `harness=claude-desktop` and carry
  `rel.cli_session_id`). Missing transcript → explicit `cli_transcript`
  metadata fragment (`basis: "cliSessionId has no transcript"`), never
  silent.

### qwen-code — `~/.qwen/projects/<encoded-cwd>/chats/*.jsonl` (+ `subagents/`)

- Claude-code-like JSONL; content arrives as `message.parts[]` with typed
  parts including `functionResponse` (tool results — classify
  `tool_result`, not prompt). `subagents/<sid>/agent-*.jsonl` hold sidechain
  transcripts.

### kimi-code — `~/.kimi-code/sessions/<encoded-cwd>/<session-id>/` + `session_index.jsonl`

- Per-session directory; records typed `turn.prompt`, `turn.steer`,
  `context.append_loop_event`, etc. `session_index.jsonl` is a fast
  title/time index.

### omp — `~/.omp/agent/sessions/**/*.jsonl`

- Generic JSONL transcripts; conservative fallback classification applies.

### porch — `$PORCH_STEER_DIR/runs/<run-id>/` (default `~/Library/Caches/pragmatic-orchestration/steer/runs/`)

- `meta.json`: agent_id, backend, model, effort, cwd, status, timestamps,
  `native_session` linkage (e.g. codex thread id for `--persist-session`
  runs — joinable to `codex:` sessions).
- `events.jsonl`: normalized event stream emitted by the run; raw provider
  output under `raw/`.

## Turn analytics: `turns` / `flags` / `stats`

For reflection workloads ("how did model X do?", "what keeps failing in my
orchestration?") navigate **turns**, not raw fragments. A turn follows the
TraceLab definition: it starts at the *triggering* user message
(`kind=prompt` + `authorship` ∈ `human`|`unknown`) and ends at the last agent
output before the next triggering message. Adjacent prompts with no agent
output between them merge into one request (multi-message input and
store-internal mirrors like codex `event_msg.user_message` ↔
`response_item.user`).

```bash
porch sessions turns -a codex --since 2026-02-01   # one row per turn
porch sessions flags -a claude-code --kind retry_loop
porch sessions stats --by model                    # grouped aggregates
```

Each turn row carries: `ts_start/ts_end/duration_s`, `models`, `user_msgs`,
`tool_calls`, `tool_errors`, `retries`, `assistant_msgs`, `reasoning`,
`compactions`, `permissions`, `interrupted`, `has_final`, `usage`, and
`first_locator`/`last_locator` for `show --around` drill-down.

`flags` emits deterministic, evidence-linked detections — **never verdicts**:

| pattern | detection rule |
|---|---|
| `retry_loop` | ≥3 identical `tool_call`s (same tool+input) across consecutive *steps* — calls fanned out from one raw record (same `locator`) count as one parallel step |
| `search_loop` | ≥5 consecutive search/read-type steps without a mutation (same step-collapsing) |
| `edit_without_read` | Write/Edit/apply_patch on a path with no earlier Read in the session |
| `correction` | triggering prompt matching lexical correction markers ("that's not", "revert", "не так", …) after a completed turn |
| `correction_burst` | ≥3 corrections in one session |
| `abandoned` | session ends on an unanswered human prompt after earlier agent activity |
| `permission_friction` | ≥3 permission events in one session |
| `context_pressure` | ≥1 compaction event |
| `interrupted` | ≥1 interrupt/abort record (`turn_aborted`, `[Request interrupted…]`) |
| `error_burst` | ≥3 error fragments within a 10-fragment window |
| `failed_run` | terminal boundary `status=failed/error` (porch runs) |

Every flag carries `evidence: [{seq, locator}, …]` + `basis` describing the
rule — feed `seq` straight to `sessions show <harness:id> --around SEQ` to
verify before quoting it in an insight. These are heuristics: a `retry_loop` may be a deliberate
poll; `correction` markers may be false positives on idiomatic text.

`stats` groups by `--by model|harness|cwd|day`: sessions, turns, tool
calls/errors + error rate, compactions, interruptions, median turn duration,
token usage, flags per pattern, plus `_top_flagged` sessions for triage.

Usage semantics: claude/gemini/opencode emit per-request usage (summed);
codex `token_count` is a cumulative snapshot (max taken, never summed).
Grok `tool_result` carries no error field — `tool_errors=0` there means
"not recorded", not "no errors".

**Dedup rule** (stated in every `_summary`): layered stores of one
conversation collapse — first canonical layer wins (rollout/transcript over
index/catalog), claude-desktop code-sessions key on `cliSessionId`, porch
runs dedupe by `native_session`.

**Synthesis recipe**: `stats` → find outlier groups → `flags` → collect
patterns with locators → `show --around` on the worst sessions → write the
top insights yourself with evidence links. The tool prepares evidence;
the calling agent owns the narrative.

## Classification contract

| `kind` | assigned to |
|---|---|
| `prompt` | user-role content (human or orchestrator-injected — see `authorship`) |
| `assistant` | model-visible reply text |
| `reasoning` | model thinking / chain-of-thought blocks |
| `tool_call` | tool/function invocations |
| `tool_result` | tool/function outputs, including function responses |
| `context` | system prompts, bootstrap/environment blocks, reminders |
| `permission` | permission-mode changes, auto-approve, guardian assessments |
| `compaction` | history compaction records |
| `boundary` | turn/step boundaries, task lifecycle events |
| `metadata` | titles, bookkeeping, session_meta, usage snapshots |
| `unknown` | unrecognized but preserved records |

| `authorship` | assigned to |
|---|---|
| `human` | genuine user keystrokes/pastes |
| `agent` | orchestrator-injected user-role content (task notifications, IDE context, sidechain prompts) and model output |
| `system` | harness bootstrap, system prompts, reminders, tool-produced output |
| `unknown` | insufficient evidence |

Rules:

- Authorship is never guessed from `kind`. When uncertain, `authorship` is
  omitted or `unknown`, and `basis` records the deciding evidence.
- Technical content is never discarded — it is classified
  `context`/`tool_call`/`tool_result`/`metadata` with `visible=false` where
  appropriate and excluded only by scope filters.
- Malformed lines (undecodable JSON, binary spills, truncated tails) are
  counted in `_summary.malformed_records` / `truncated_tail_files`, not
  dropped silently.
- Usage/token records are cumulative snapshots — report latest value, never
  sum.

## Safety contract

- Pure read: sqlite opened `mode=ro` (never `immutable=1` on live dbs — WAL
  state matters), files only opened for reading, nothing written anywhere.
- Store discovery follows directory symlinks (each real directory once, so
  loops cannot recur); session ids stay relative to the store root, and a
  store reporting `unreadable_dirs` skipped part of its tree.
- `sessions` never prints record content beyond `--max-chars` per fragment;
  use `show --file`/`--around` deliberately rather than bulk-dumping whole
  histories — sessions may contain secrets pasted by the user.
- Do not pipe `sessions` output into files committed to a repository.
