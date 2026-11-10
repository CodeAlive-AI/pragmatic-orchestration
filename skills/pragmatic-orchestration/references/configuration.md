# Configuration and execution

## Prerequisites

- Python 3
- At least one supported harness: Codex CLI, OpenCode 2.2.6 or newer in the 2.x series, Claude Code, or native Grok Build
- Gemini CLI is optional and review-only
- Devin CLI is optional and opt-in: `devin` v3000.x on PATH, authenticated via `devin auth login`
- Antigravity CLI is optional and opt-in: `agy` 1.5.4 or newer (ACP and stream-json input) on PATH, signed in through an interactive `agy` login or `agy acp --login`

Authentication stays with each harness.

## Agent profiles

Profiles live in `config.json`; set `PORCH_CONFIG` to use another file.
The local file replaces the shipped example rather than merging with it, so its
models and enabled profiles may differ from the defaults described below.

| Field | Purpose |
|---|---|
| `enabled` | Legacy profile flag; review and delegate select an exact profile explicitly |
| `backend` | `codex-cli`, `claude-code`, `opencode`, `grok-build`, `gemini-cli`, `devin-cli`, or `antigravity-cli` |
| `model` | Backend model id |
| `role` | Analyst, lateral, or specialist role |
| `effort` | Backend reasoning effort/variant |
| `label` | Human-readable output label |
| `review_instructions` | Optional profile-specific instructions for non-raw review prompts |
| `supports_delegate` | Optional; set false for review-only profiles |

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

The enabled `codex` profile uses `gpt-6.1-sol` at `high` effort, and the enabled
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
`low`, `medium`, `high`, `xhigh`, and `max`; it does not accept `none`. The
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

The disabled-by-default `devin` profile uses Devin CLI's `swe-2-flash-high` model (SWE-2 Flash;
`swe-2-flash-medium`, `swe-2-flash-max` and the `swe-2-*` family also work —
see `devin models`);
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
| `PORCH_OUTPUT_DIR`, `PORCH_RUN_DIR` | Artifact locations |
| `PORCH_SAVE_OUTPUTS` | Disable ordinary archival with `0`; steerable service artifacts remain |
| `PORCH_STEER_DIR` | Steerable registry; reuse the same value for every control command |
| `PORCH_DEBUG_EVENTS*` | Opt-in bounded event tape and its path/record/byte limits |
| `GEMINI_API_KEY` | Gemini CLI authentication when required |

Porch imposes no execution deadline in any mode. Provider context/output
limits and managed harness settings still apply.

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
