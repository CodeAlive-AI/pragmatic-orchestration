# Configuration and execution

## Prerequisites

- Python 3
- At least one supported harness: Codex CLI, OpenCode 2.2.6 or newer in the 2.x series, Claude Code, or native Grok Build
- Gemini CLI is optional and review-only
- Devin CLI is optional and opt-in: `devin` v3000.x on PATH, authenticated via `devin auth login`
- Antigravity CLI is optional and opt-in: `agy` 1.5.4 or newer (ACP and stream-json input) on PATH, signed in through an interactive `agy` login or `agy acp --login`
- Cursor CLI is optional and opt-in: `cursor-agent` on PATH, signed in. Do not call the bare name `agent` — where Grok Build is installed, that name is Grok Build. An empty model uses the account default.

Authentication stays with each harness.

## Agent profiles

Profiles live in `config.json`; set `PORCH_CONFIG` to use another file.
The local file replaces the shipped example rather than merging with it, so its
models and enabled profiles may differ from the defaults described below.

| Field | Purpose |
|---|---|
| `enabled` | Legacy profile flag; review and delegate select an exact profile explicitly |
| `backend` | `codex-cli`, `claude-code`, `opencode`, `grok-build`, `gemini-cli`, `devin-cli`, `antigravity-cli`, or `cursor-cli` |
| `model` | Backend model id |
| `role` | Analyst, lateral, or specialist role |
| `effort` | Backend reasoning effort/variant |
| `label` | Human-readable output label |
| `review_instructions` | Optional profile-specific instructions for non-raw review prompts |
| `supports_delegate` | Optional; set false for review-only profiles |
| `web_search` | Optional, OpenCode profiles only: `"allow"` (default) or `"decline"`; see [Web search in OpenCode delegate runs](#web-search-in-opencode-delegate-runs) |

### Web search in OpenCode delegate runs

OpenCode 2 asks the user to consent to its web-search provider the first time
a worker searches. A delegate run has nobody to answer, so Porch settles it
before launch, by profile, through the private server's inline configuration
(`OPENCODE_CONFIG_CONTENT`). That configuration lives only in the run's server
process: Porch never writes to your OpenCode settings and never answers the
consent form, because the answer is stored in OpenCode's own state. Review runs
(`review ask`, `review code`) settle it the same way from the mode: web search is
on, and a `websearch` key already in your `OPENCODE_CONFIG_CONTENT` wins.

- Default (`"web_search": "allow"`, or the key absent): the server starts
  with `websearch: {"provider": "random"}`, the same value the consent form's
  "allow" stores, so a provider is used for this run only. The run emits the
  progress event `websearch_consent_allowed via=config` at the start. Workers
  are expected to check current primary sources, so web search is on by
  default.
- Opt-out (`"web_search": "decline"`): the server starts with
  `websearch: false`, so the worker continues without web search, and the run
  emits `websearch_consent_declined via=config`. Set it on a profile in your
  own `config.json` when its searches must not reach a third-party provider:

```json
"opencode-go-deepseek-v4-pro-offline": {
  "enabled": false,
  "backend": "opencode",
  "model": "opencode-go/deepseek-v4-pro",
  "effort": "max",
  "role": "analyst",
  "web_search": "decline",
  "label": "DeepSeek V4 Pro without web search"
}
```

Every OpenCode form or permission request still fails the run with
`input_required` or `permission_required`. If the web-search consent form
appears despite the configuration (recognised by its structured identifiers:
`metadata.kind` equal to `websearch.provider` and a single `choice` field
offering `allow` and `disable`, never by wording), the run fails with an
`input_required` message that names the cause. An inline
`OPENCODE_CONFIG_CONTENT` that already sets `websearch` is kept as is, and
content that is not a JSON object is not modified; the message says so. Other
values of `web_search` are rejected. The legacy `--one-shot` path is unchanged.

List effective profiles with:

```bash
"$PORCH" --list-agents
```

Non-empty environment values override profile model/effort for one invocation:

- `CODEX_MODEL`, `CODEX_EFFORT`
- `CLAUDE_MODEL`, `CLAUDE_EFFORT`
- `OPENCODE_MODEL`, `OPENCODE_EFFORT` (`none` omits the variant)
- `GROK_MODEL`, `GROK_EFFORT`
- `GEMINI_MODEL`
- `DEVIN_MODEL`; `PORCH_BIN_DEVIN` overrides the `devin` binary
- `AGY_MODEL`, `AGY_EFFORT`; `PORCH_BIN_AGY` overrides the `agy` binary
- `CURSOR_MODEL`; `PORCH_BIN_CURSOR` overrides the `cursor-agent` binary. An empty model omits `--model` so the account default is used.

The enabled `codex` profile uses `gpt-6.2-sol` at `high` effort, and the enabled
`codex-gpt-6-luna` profile uses `gpt-6-luna` at `low` effort.
The opt-in `codex-gpt-6.1-sol` profile selects the same GPT-6.1 Sol model and
`high` effort explicitly in review and delegate. If you maintain a user-local
`config.json` or `PORCH_CONFIG`, copy this profile from `config.example.json`
to make the new id available there; user configurations are not merged with
the shipped example. The model id is documented in the
[official Codex model guide](https://developers.openai.com/codex/models#gpt-6.1-sol).
The opt-in `codex-gpt-6.1-terra` profile uses `gpt-6.1-terra` at `xhigh`
effort for review and delegate. `CODEX_EFFORT` overrides this default.
It requires Codex CLI 0.159.0 or later; see the
[official OpenAI model guide](https://learn.chatgpt.com/docs/models).
Copy this profile into existing user-local configs to make the new id available.
The opt-in `codex-gpt-6-astra` profile retains `gpt-6-astra` at `high` effort. Astra accepts
`low`, `medium`, `high`, `xhigh`, and `max`; it does not accept `none`.
The opt-in `codex-gpt-6.2-sol` and `codex-gpt-6.2-astra` profiles use
`gpt-6.2-sol` and `gpt-6.2-astra` at `high` effort for review and delegate
(both ids and the `low`/`medium`/`high`/`xhigh`/`max`/`ultra` levels verified
with `codex debug models` on Codex CLI 0.169.0). `CODEX_EFFORT` overrides the
effort. `codex-gpt-6.2-sol` selects the same model as the default `codex`; `codex-gpt-6.2-astra` is disabled by default. The default `codex` moved from GPT-6.1 Sol to GPT-6.2 Sol in 5.15.0 (`codex-gpt-6.1-sol` keeps the old one). Copy them into existing user-local configs to make the new ids available. The
default Claude Code profile, `claude-opus`, uses `opus` at
`medium` effort. `claude-code` is a disabled alias with the same model and
effort. The opt-in `claude-fable` and `claude-sonnet` profiles use `fable` and
`sonnet` at `high` effort; `claude-haiku` uses `haiku` at `medium` effort.
These [Claude Code aliases](https://code.claude.com/docs/en/model-config#model-aliases)
can resolve to different versions depending on provider, CLI version, and
configuration. Fable 5.2 requires Claude Code 2.1.321 or later. Existing
user-local configurations are not merged with the example: update their model
fields and add `claude-haiku` to adopt these profiles.
The opt-in `opencode-go-gemini-4-argon-contributor` profile uses
`opencode-go/gemini-4-argon-contributor` with `high` effort for review and
delegate. Select it with `-a opencode-go-gemini-4-argon-contributor`.
The captured catalog exposes `low`, `medium`, and `high` variants; `OPENCODE_EFFORT`
overrides the default. Copy this profile into an existing user-local config
to use the new id. OpenCode Go lists this as Gemini 4 Argon **Contributor**:
prompts and completions may be used to train Google models, and it is not ZDR.
See the [Go catalog and privacy terms](https://dev.opencode.ai/v2/docs/console/go/).

The [current OpenCode Go profiles](opencode-go.md) cover all 28 tool-capable
models verified on 2026-10-30, including DeepSeek V4.1 Flash, DeepSeek V4 Pro,
GLM 5.3/Flash, Qwen 3.8 Max/Flash, Kimi K3.1 Pro and MiniMax M3.1 Flash.
Use `-a opencode-go-<model-id>` for an exact model. The new profiles are opt-in;
copy them from `config.example.json` when maintaining an existing local config.

The disabled-by-default `opencode-go-muse-spark-1.3-contributor` and
`opencode-go-deepseek-v4.1-flash` profiles use the OpenCode Go catalog at
their maximum effort (`xhigh` for Muse and `max` for DeepSeek). Select either
explicitly with `-a`; keeping them disabled avoids changing the default review
pool. Muse supports `minimal`, `low`, `medium`, `high`, and `xhigh`; DeepSeek
supports `low`, `high`, and `max`. OpenCode Go's
privacy terms state that Muse Spark Contributor prompts and completions may be
used for model training, the service is not zero-data-retention, and availability
is region-limited. Check the current OpenCode Go terms before reviewing sensitive
repositories because catalog and privacy terms can change.

Devin's default model is SWE-2.1 (`devin`); the SWE-2 and SWE-2 Flash profiles (`devin-swe-2`, `devin-swe-2-flash-max`) remain selectable for the older generation. SWE-2.1 is free on the owner's paid plan through 2026-12-08 and discounted through 2027-01-30; SWE-2 and SWE-2 Flash stay free beyond that. The `devin-gemini-4-*` profiles are labelled "only when Devin is named" and are chosen only when the user asks for that model through Devin; otherwise Gemini 4 runs through Antigravity ([routing](model-routing.md#harness-for-a-named-model)).

The disabled-by-default `devin` profile uses Devin CLI's `swe-2-1-xhigh` model (SWE-2.1
at xhigh effort; the family also has `swe-2-1-low`, `swe-2-1-medium`, `swe-2-1`,
`swe-2-1-high` and `swe-2-1-max`, plus the `-lightning`-suffixed Ultrafast tier
served about ten times faster — see `devin models`). The opt-in
`devin-swe-2-1-ultrafast` profile selects `swe-2-1-xhigh-lightning` and is chosen
only when the user names Ultrafast; standard SWE-2.1 generates at roughly a tenth
of Ultrafast's speed, so Ultrafast suits deadline-sensitive work while the promo
lasts. The older `swe-2-flash-*` and `swe-2-*` families also work.
Devin versions effort into the model id, so there is no effort override. Both
one-shot and steerable paths run over `devin acp` (JSON-RPC 2.0 NDJSON on
stdio) — print mode `devin -p` is not used because a denied or
confirmation-required tool call silently cancels the session without final
text. Review runs `devin acp --agent-type review`, an agent with no write/edit
tools; yolo delegate runs the default agent type under `session/set_mode
bypass`. The helper strips inherited `ACP_BACKEND` from the child environment.

The opt-in `devin-gemini-4-argon` profile selects Gemini 4 Argon with `high`
effort for review and delegate through the same ACP paths. Its exact Devin model
id is `gemini-4-argon-high`; do not use the OpenCode Go Contributor id here.
Verified with `devin models list` on CLI 3000.11.92, which also lists
`gemini-4-argon-low` and `gemini-4-argon-medium`. Use `DEVIN_MODEL` with an
exact model id to override it; there is no separate Devin effort flag.
Copy this profile into existing user-local configs to select the new id.

The opt-in `agy-gemini-4-argon` and `agy-gemini-3.8-flash` profiles run Google's
Antigravity CLI with the `gemini-4-argon` and `gemini-3.8-flash` model families
at `high` effort. Antigravity keeps model and thinking level separate: one-shot
runs pass `--model FAMILY --effort LEVEL`, and steerable runs set the `model`
and `thought_level` session options over ACP. `agy models` lists the suffixed
ids (`gemini-4-argon-low|medium|high`, `gemini-3.8-flash-low|medium|high`), and
a suffixed id also works as `model` with an empty `effort`. Gemini 4 Argon needs
a paid Google AI plan; Gemini 3.8 Flash is on every plan. An unknown model or
thinking level fails the launch instead of falling back to the account default.

The opt-in `agy-gemini-4-hydrogen*` profiles select Gemini 4 Hydrogen, one
profile per thinking level: `agy-gemini-4-hydrogen` (`high`),
`agy-gemini-4-hydrogen-xhigh`, `-medium` and `-low`. `agy models` lists
`gemini-4-hydrogen-low|medium|high|xhigh` (verified on agy 1.5.10); `--effort`
accepts `low|medium|high|xhigh|max`, so `xhigh` is Hydrogen's top level.

The opt-in `agy-gemini-4-neon*` profiles select Gemini 4 Neon the same way:
`agy-gemini-4-neon` (`high`), `agy-gemini-4-neon-xhigh`, `-medium` and `-low`.
`agy models` lists `gemini-4-neon-low|medium|high|xhigh` (verified on agy 1.5.12).

The opt-in `agy-gemini-4-carbon*` profiles target Gemini 4 Carbon, announced
on 2026-10-28 ([Google](https://blog.google/innovation-and-ai/models-and-research/gemini-models/introducing-gemini-4-carbon/))
and so far available only to selected cyber defenders through Google's
Fairwind Program: `agy-gemini-4-carbon` (`high`), `-medium` and `-low`
(Carbon has no `xhigh`). Google has published no API id; `gemini-4-carbon` is
the id in the model list of Antigravity's own ACP server builds and matches the
Argon scheme. Until an account has access, agy rejects it (agy 1.5.10 does not
list Carbon) — "not recognized as a known model" (reported as "--effort is not
supported" while effort is set) or ACP `Unknown model ID` — and the run fails
instead of falling back. Once access arrives, check `agy models`: if Carbon is
listed only with suffixed ids, set `model` to that exact id and `effort` to an
empty string (or use `AGY_MODEL`/`AGY_EFFORT`).

The opt-in `devin-gemini-4-hydrogen` and `devin-gemini-4-carbon` profiles use
Devin's `gemini-4-hydrogen-high` and `gemini-4-carbon-high` ids. Verified with
`devin models list` on CLI 3000.13.4, which also lists
`gemini-4-hydrogen-low|medium|xhigh` and `gemini-4-carbon-low|medium`. As with
Argon, effort is part of the id: pick another level with `DEVIN_MODEL`.

- Review and `--one-shot` delegate use headless stream-json in both directions
  (`agy --input-format stream-json --output-format stream-json`): the prompt is
  one stdin message, never argv, and only a terminal `result` with status
  `SUCCESS` counts as an answer. `agy` can exit 0 after an interrupted or failed
  turn, so `INTERRUPTED`, `ERROR`, any `finished_reason`, or a missing result
  fails the run.
- Review adds session-only settings (`--settings`, never saved) with deny rules
  for `write_file(*)`, `mcp(*)` and `execute_url(*)` (plus `read_url(*)` when a
  mode forbids web). Antigravity evaluates Deny before Ask and Allow, so these
  rules also beat allow rules in the user's own `settings.json`; a write attempt
  ends as a failed tool step "permission check failed … Matches user-configured
  deny rule". The shell stays available and is trusted to the report-only
  prompt contract, as with the other backends. Plan mode is not used: it is a
  planning workflow, not a permission boundary.
- Every Antigravity run passes `--dangerously-skip-permissions`; headless mode
  would otherwise soft-deny each tool call that needs approval and still exit 0.
  Explicit deny rules, including the user's own, still apply.

The disabled-by-default `cursor` profile runs Cursor CLI with an empty model
(`label`: "Cursor CLI (account default model)"), so `--model` is omitted and
the account default (Auto) is used. The optional disabled `cursor-grok-4.7`
profile sets `model` to `grok-4.7` and passes `--model`. Copy either profile
from `config.example.json` into a user-local `config.json`; that file replaces
the example and is not merged. `CURSOR_MODEL` overrides the profile model for
one invocation. An empty value does not force a model.

The binary is `cursor-agent`. Do not call bare `agent`: on a machine that also
has Grok Build, `agent` is that CLI.

The prompt is written to the child's stdin, not passed as a positional
argument. Review adds `--mode plan`, which blocks edit and shell writes in
print mode. `--mode ask` is not the review guard: on the Free tier it is
refused (`ActionRequiredError: Model unavailable on Free`, or a connection
reset with no stdout). `--sandbox enabled` does not block writes. Delegate
adds `--force` and does not pass `--mode plan`.

On the Free tier, any explicit `--model`, including `grok-4.7`, is refused
even when the error text says Free includes Grok 4.7. Leave the profile model
empty to use Auto; `cursor-grok-4.7` therefore requires a paid Cursor plan.
Bare `--model` also rewrites `model.modelId` in `~/.cursor/cli-config.json` —
it persists the choice even when the run fails. To keep that side effect off
the user's config, every run (one-shot, review, delegate, and each steered
`--resume` process) sets `CURSOR_CONFIG_DIR` to a per-run temporary directory
seeded by `scripts/lib/cursor_config.py`: `cli-config.json` is copied so auth
and model writes land on the copy, while `chats/`, `acp-sessions/` and other
entries are symlinked so sessions stay discoverable. The temp dir is removed
when the run ends; `~/.cursor/cli-config.json` is never written.

Steering is a new `cursor-agent --resume <session_id>` process per queued
turn; interrupt is not supported. If a turn fails, the stderr reason (for
example `ActionRequiredError: Model unavailable on Free …`) is appended to
the run's error and queued steers are acknowledged as failed with
`turn_failed` rather than dropped. A result with `is_error` or a subtype
other than `success` fails the run even when the process exits 0. The
`result` string is the answer; the stream's complete assistant message is
deduplicated against the deltas already shown, so progress text is emitted
once.

Verified against Cursor CLI 2026.11.19-f005e9d on the Free tier (2026-11-19).

## Shell-safe prompts

Prefer `--prompt-file`, stdin, or a single-quoted heredoc for prompts containing backticks, `$`, `!`, or quotes. Double-quoted positional prompts are shell-expanded and may accidentally execute substitutions or leave a backend waiting on stdin.

```bash
"$PORCH" review ask -a grok --prompt-file prompt.md
"$PORCH" review ask -a grok "$(cat <<'EOF'
Explain `foo` and $PATH handling.
EOF
)"
```

The shell-interpolation warning applies only to positional prompts that still contain backticks or `$()`; file/stdin content is not warned.

Prompt sources are selected explicitly, independently of their contents. An
empty or whitespace-only positional prompt or prompt file is a usage error;
it never switches to inherited stdin. Do not combine a positional prompt with
`--prompt-file`. Stdin supplies the prompt only when neither source is selected.
This applies to review, delegate, and steering guidance. File-based backend
transports receive closed stdin; stream-based transports receive the staged
prompt file. The internal prompt builders do not append ambient stdin.
File paths are read literally, including a file named `-`; file-reading tools
must not reinterpret such a path as an instruction to read stdin.

## Environment variables

| Variable | Purpose |
|---|---|
| `PORCH_CONFIG` | Alternate profile configuration |
| `PORCH_PROGRESS` | Review progress: `full`, `compact`, or `none` |
| `PORCH_OUTPUT_DIR`, `PORCH_RUN_DIR` | Artifact locations; `PORCH_OUTPUT_DIR` defaults to `${XDG_STATE_HOME:-~/.local/state}/pragmatic-orchestration/outputs` |
| `PORCH_SAVE_OUTPUTS` | Disable ordinary archival with `0`; steerable service artifacts remain |
| `PORCH_STEER_DIR` | Steerable registry; reuse the same value for every control command |
| `PORCH_RETENTION_DAYS`, `PORCH_RETENTION_DETAIL_DAYS`, `PORCH_RETENTION_DETAIL_MAX`, `PORCH_RETENTION_AUTO` | Retention of saved runs, see below |
| `PORCH_STREAM_FULL` | `1` saves the complete, uncompacted event streams of a run (debugging) |
| `PORCH_DEBUG_EVENTS*` | Opt-in bounded event tape and its path/record/byte limits |
| `GEMINI_API_KEY` | Gemini CLI authentication when required |

Porch imposes no execution deadline in any mode. Provider context/output
limits and managed harness settings still apply.

## Saved runs and retention

Every run saves its answer and event streams in its own folder
`run-<mode>-<name>` under `PORCH_OUTPUT_DIR`. The default folder is
`${XDG_STATE_HOME:-~/.local/state}/pragmatic-orchestration/outputs` (on Windows
`%LOCALAPPDATA%\pragmatic-orchestration\outputs`). It is not the system
temporary folder: the OS empties that at every boot and purges it after a few
idle days, and a saved answer can be the only copy of finished work. A caller
that sets `PORCH_RUN_DIR` owns that folder; retention never touches it.

Nothing else bounds a durable folder, so porch does:

| What | Kept | Setting |
|---|---|---|
| A finished run: its folder and its registry entry (`final.txt`, `meta.json`, audit) | 30 days | `PORCH_RETENTION_DAYS`, `--older-than` |
| The event streams of a finished run (`raw/`, `normalized/`) | 3 days | `PORCH_RETENTION_DETAIL_DAYS`, `--detail-older-than` |
| All event streams of finished runs together | 2 GiB, oldest removed first, never within a run's first 24 hours; `0` switches the limit off | `PORCH_RETENTION_DETAIL_MAX`, `--max-detail-size` |

A run that is still going is never pruned: one the registry reports as running,
one whose launching process is alive, or one written to in the last six hours
when no process was recorded. A registry entry goes only together with the
whole session chain it belongs to (`--continue-run`), and only when every run
of the chain is past retention. After its streams are removed, a run still
answers `delegate status`, `wait` and `list`, and `porch sessions` still shows
its task and final answer; `delegate events` has no events left for it.

```bash
porch prune                          # list what would be pruned; removes nothing
porch prune --apply                  # prune it
porch prune --older-than 14d --detail-older-than 1d --max-detail-size 500M
porch prune --all --json             # every run with its verdict
```

Ages take `h`, `d` or `w` (a bare number is days); sizes take `K`, `M`, `G`.
Exit codes: 0 done, 1 some entries could not be removed, 5 invalid setting.
Porch also starts `porch prune --apply` in the background when a run starts,
at most once in six hours (`<outputs>/.retention.log` has its output);
`PORCH_RETENTION_AUTO=0` switches that off. The other outputs folder of an
older version is pruned by naming it:
`PORCH_OUTPUT_DIR="${TMPDIR%/}/pragmatic-orchestration-outputs" porch prune`.

### What the event streams hold

- `normalized/<agent>.jsonl` is what `delegate events`, the UI and
  `porch sessions` read: one record per event with its type, text and tool
  name. Content events (thinking, answer, progress, tools, turns) do not carry
  the backend's payload; lifecycle events porch makes itself (run start and
  end, steer states) keep their small details.
- `raw/<agent>.jsonl` is the wire log, the complete record of what the backend
  sent and what porch sent to it. Media modes read it. For Codex, the repeated
  `turn/diff/updated` messages (the whole turn diff again after every edit) are
  collapsed to the last one per turn.
- A persistent Codex run (`--persist-session`, `--continue-run`) is also a
  native Codex session holding the full history. Once that session file is on
  disk and the run ended without a backend error, the wire log is replaced by
  `raw/<agent>.native.json`, which names the session:
  `porch sessions show codex:<native_session_id>`. An ephemeral run has no
  native session and keeps its wire log.
- The registry entry of a steerable run holds a private `audit.jsonl` and, for
  a detached run, `supervisor.log`. They record turns, tools, steers, errors
  and the end of the run one by one. The streamed pieces of answer, thinking
  and protocol chatter are only counted there: the next audit record carries
  the counts in its `stream` field (progress by label), and the detached log
  gets one `stream text=… thought=… progress=…` line at most once a minute.
  After the event streams are pruned, those counts, the task and the final
  answer are what is left of a run's course. A foreground run still prints a
  progress line per piece on stderr.
- `PORCH_STREAM_FULL=1` on a run saves everything as before: the payload inside
  every normalized record, every repeated diff, the wire log of a persistent
  run, and an audit record and a detached log line for every streamed piece.

# Quota inspection

Use the read-only quota command to make scheduling decisions without starting a
review or delegate turn:

```bash
"$PORCH" quota [all|codex|grok]
```

The output is a versioned JSON envelope. Codex is queried through
`account/rateLimits/read` on `codex app-server`; this does not consume an
available reset credit. Grok is queried through the official `/usage` command
in a temporary tmux session on `grok-aws`. Override the safe SSH alias with
`PORCH_GROK_QUOTA_SSH_HOST` and the absolute remote working directory with
`PORCH_GROK_QUOTA_CWD`.

When both providers are requested, one failure does not discard the successful
result. Exit `0` means all requested providers succeeded, `2` means partial
success, and `3` means all requested providers failed.

Current model IDs: `gpt-6.1-sol`, [GPT-6 Luna](https://openai.com/index/introducing-gpt-6-sol-and-luna/) and [Grok 4.7](https://docs.x.ai/developers/models/grok-4.7). The default native `grok` profile uses `grok-4.7` at `high` effort. The opt-in `grok-fast` profile uses the Grok Build catalog id `grok-4.7-build-fast`, not a public xAI API id. Grok 4.7 Fast uses faster infrastructure at higher rates and is not available on the free tier; see the [Grok 4.7 guide](https://docs.x.ai/developers/grok-4-7). The opt-in `grok-4.5` profile preserves the old model. Existing user-local `config.json` files and environment overrides retain precedence; update them explicitly to adopt these defaults.
