# Runtime contracts

Use this reference when diagnosing or changing observability, normalized events, backend capability resolution, access policy, prompt composition, artifacts, or run identity.

## Contents

- Streams, artifacts, and the closed event schema
- Debug event tape
- Shared backend and mode capability contracts
- Declarative workflow plans and layered prompts
- Progress, identifiers, and resource limits
- Native Grok one-shot contract

## Streams and artifacts

| Stream | Contract |
|---|---|
| stderr | Mode-specific progress for foreground commands; this is not a promise of tool/file visibility for steerable delegation |
| stdout | Clean final answer only |
| artifacts | Per-run `raw/*.jsonl` (wire log), `normalized/*.jsonl` (events without the backend payload), `final/*.txt`, and `final.txt` under `PORCH_OUTPUT_DIR` or `PORCH_RUN_DIR`; what each holds and how long it is kept: [configuration.md](configuration.md#saved-runs-and-retention) |

Artifact keys are per invocation: the profile ID for review and delegate. Review clears any inherited artifact key.

One-shot architecture remains `backend_cmd | normalize_stream.py --raw-out --progress --extract-text`. Raw lines are persisted and normalized immediately. `PIPESTATUS` preserves backend exit and Grok end/error validation independently.

## Closed event schema

`scripts/lib/events.py` defines the closed `PorchEvent` type set shared by one-shot normalization and steerable persistence. Unknown event types are rejected rather than silently written. Original backend payloads may be retained under `raw`.

| Type | Purpose |
|---|---|
| `run_started` | Normalizer/session start |
| `thinking_delta` | Thought streams |
| `answer_delta` | Answer/message deltas |
| `result` | Authoritative complete answer, especially Claude |
| `tool_started`, `tool_completed` | Tool lifecycle |
| `retry_scheduled` | Retry hint |
| `steer_*` | Steerable lifecycle |
| `run_completed`, `run_failed` | Terminal event |
| `progress`, `user_replay`, `turn_*`, `prompt_complete` | Structural observability |

Live stderr keeps the human vocabulary `thought`, `text`, `end`, and `error`. Final-text assembly prefers `result` over concatenated answer deltas. Thinking and tool events never contribute.

A backend line the normalizer has no mapping for is rejected and counted; the
run ends with one stderr line `protocol-drift rejected_unknown=N types=...`,
keyed by the backend's own line type. Claude Code's `stream-json --verbose`
lines are all mapped: `system` (init, task and summary notices) and
`rate_limit_event` become `progress`; an `assistant` message that holds only
tool calls becomes `tool_started`, one that holds only thinking a thought; a
`user` line with tool results becomes `tool_completed`; any line with
`parent_tool_use_id` is a subagent's and becomes `progress`, so a subagent's
text never enters the worker's answer.

## Debug event tape

The opt-in tape records bounded, sequence-numbered JSONL across RAW → PARSED → NORMALIZED → RENDERED → FINAL without leaking event bodies onto normal stderr.

```bash
PORCH_DEBUG_EVENTS=1 "$PORCH" review ask -a grok "Q"
PORCH_DEBUG_EVENTS_PATH=/tmp/tape.jsonl
PORCH_DEBUG_EVENTS_MAX=10000
PORCH_DEBUG_EVENTS_MAX_BYTES=33554432
```

One-shot normalization records all stages; the steerable supervisor records normalized adapter events. Overflow, dropped, and gap counts are reported on close instead of being hidden.

## Shared backend contract

`scripts/lib/backend_contract.py` resolves profile identity, binary, model/effort including environment overrides, and real capability differences: steering delivery class, interrupt support, concurrent queues, transport, and final-text assembly. Both `backend_run.sh` and steerable `config_loader.py` use it.

## Mode capability policy

`scripts/lib/mode_policy.py` derives access from an explicit capability matrix covering filesystem, shell, web, memory, subagents, steering, and interrupt. Review is read-only; delegate/delegate-steerable are YOLO. Unknown modes fail closed. Backend safety flags layer on top.

`subagents` has three values: `none` (review, media), `researchers` (mode
`review-researchers`, entered by `review ask --researcher`) and `any` (delegate).
`review-researchers` equals review in every other capability.
`scripts/lib/researchers.py` decides per backend how the worker reaches its
researchers and refuses a backend that cannot keep them read-only;
[review.md](review.md#read-only-researchers) has the routes and the evidence.

| Backend | Review | Delegate |
|---|---|---|
| Codex CLI | `--search`, `-c sandbox_workspace_write.network_access=true`, `exec --sandbox workspace-write` (media: `read-only`), ask-for-approval never, multi-agent disabled (`--disable multi_agent --disable multi_agent_v2 -c agents.enabled=false`) | `--dangerously-bypass-approvals-and-sandbox` |
| Claude Code | `dontAsk`, safe mode, no session/Chrome, Edit/Write/Agent/Task denied, Bash + web enabled | `--dangerously-skip-permissions` |
| OpenCode | runtime `porch-review` primary agent with its own work-alone prompt: full diagnostics, edit/subagent denied; `websearch: {"provider": "random"}` in the run-only config unless the caller's config sets `websearch` | `--agent build --auto` |
| Grok Build | `--no-plan`, `workspace` sandbox (media: `read-only`), terminal and web tools enabled, subagents disabled | `--always-approve`, no sandbox |
| Gemini CLI | `--approval-mode yolo`, extensions/MCP/subagents disabled; report-only prompt contract | unsupported |

Codex 0.170.0 keeps its collaboration tools when only the two feature flags are
disabled; `-c agents.enabled=false` is what removes them.

With researchers the review column changes as follows; everything else stays.

| Backend | Review with researchers |
|---|---|
| Codex CLI | `--disable multi_agent -c features.multi_agent_v2.enabled=true -c features.multi_agent_v2.expose_spawn_agent_model_overrides=false -c agents.default_subagent_model="M" [-c agents.default_subagent_reasoning_effort="E"]`; no `agents.enabled=false` |
| Claude Code | `--tools "Bash,Read,Grep,Glob,WebSearch,WebFetch,Agent"`; `--disallowedTools` holds Edit, Write, NotebookEdit and `Agent(<type>)` for every built-in type except `Explore`; environment `CLAUDE_CODE_SUBAGENT_MODEL`, `CLAUDE_CODE_SUBAGENT_MODEL_FORCE=1`, `CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH=1`. Without a Claude researcher profile: no Agent tool |
| OpenCode | `porch-review` denies every subagent and then allows `porch-researcher`, a run-time subagent on the researcher model with edit, subagent and question denied |
| Grok Build | `--agents` with the `porch-researcher` definition (model, effort, read-only tools), `Agent(porch-researcher)` appended to `--tools`, no `--no-subagents`; the `workspace` sandbox is unchanged |
| Antigravity CLI | argv unchanged; researchers run through the nested porch only |
| Gemini CLI, Devin CLI, Cursor CLI | refused with exit `4` |

The worker's environment carries `PORCH_RESEARCHER_PROFILES`: the profiles a
porch started from its shell may ask through `review ask -a`, and nothing else
(`porch_nested_guard` in `scripts/lib/common.sh`). A nested run exports an empty
list. `PORCH_DUMP_ARGV` output includes these environment values.

Web research is available in review mode. No review backend uses a planning workflow. Claude pre-approves `Bash,WebSearch,WebFetch`; OpenCode uses a dedicated primary review agent with Bash/search enabled; Codex and Grok run their shell in the workspace sandbox with network, so tests and lookups work; OpenCode settles its web-search consent in the run-only config; and Gemini uses its normal full tool loop. The trusted review policy explicitly forbids file or external-state changes, saving plans/reports, subagents, agent teams, other models, and delegation. Where supported, subagent tools are also disabled mechanically. In researcher mode the same policy allows the listed researcher channels and keeps every other form of delegation forbidden.

Grok `tool_call` / `tool_call_update` events normalize to tool lifecycle and
content-free compact heartbeats. Long terminal commands therefore remain visibly
alive without exposing the command or its output in progress logs.

Before a backend diagnostic is printed, archived by a parent workflow, or
embedded in a failed-agent report, common URL credentials, sensitive query
parameters, authorization headers, and credential key/value fields are
redacted. If redaction fails, the original diagnostic is suppressed.

“Read-only” is the review contract for every backend, but enforcement strength is
backend-specific. Codex and Grok additionally provide a filesystem sandbox.
Claude, OpenCode, and Gemini retain unrestricted diagnostic shells/tool loops so
they can perform a complete review; for those backends non-mutation is enforced
by the trusted prompt plus dedicated edit/delegation-tool denials where the CLI
supports them, not by an OS-level filesystem boundary. Use Codex or Grok when a
mechanical read-only boundary is required for untrusted repositories.

## Layered prompts

`scripts/lib/prompt_pipeline.py` composes trusted layers in this order:

```text
framework_policy → mode_contract → role → output_schema → repository_facts → user_input → framework_recap
```

A new delegate task is the executor contract from `prompts/delegate-executor.txt`
followed by the caller's task, byte for byte; `scripts/lib/delegate.sh` composes
it once for the steerable, detached and one-shot paths. The contract tells the
worker that the assignment sets its scope and authority, that project-wide
ownership or a coordinator role granted by repository instructions belongs to
the parent, and what the final report must contain. A missing or empty contract
file is a configuration error; there is no fallback to the raw task.
`delegate steer` guidance and `--continue-run` follow-ups are sent as written.
Delegate carries no review policy and no output template. In review mode,
`--prompt-file` is only an input transport: the read-only, work-alone, and
blast-radius policies are still layered around its contents. The shell
`build_prompt` path remains a fallback for the default contract only.

The review contract is assembled from templates by `review_contract()`:

| Choice | Framework and recap | Role and output layers |
|---|---|---|
| adviser (default) | `prompts/review-framework.txt`, `prompts/review-recap.txt` | profile role text, five-heading template |
| assignment (`--assignment`) | `prompts/review-assignment-framework.txt`, `prompts/review-assignment-recap.txt` | none: the brief sets role and structure |

Both frameworks have one slot for the delegation rule:
`prompts/review-delegation-alone.txt` in mode `review`, and
`prompts/review-delegation-researchers.txt` with the channel list of this run
in mode `review-researchers`. The recap repeats the rule in one clause. The
default adviser contract in mode `review` is byte-identical to the contract
before the templates were split. Researcher mode and the assignment style have
no shell fallback and no raw-prompt path: a failed prompt pipeline is a
configuration error (exit `4`). `prompts/review-researcher.txt` is the system
prompt of the run-time researcher agents on OpenCode and Grok Build.

Prompt builders consume only their explicit user input and context files.
The CLI pipeline reads stdin only when neither `--user-file` nor `--user-text`
is selected; an explicitly empty source fails. Backend and delegate launchers
likewise distinguish a missing source from empty content. File-based backend
transports receive EOF on stdin instead of inheriting a caller pipe.

Repository-backed review also carries an initial relevant-file seed. The caller
does a short read-only triage and supplies known paths; `review code` accepts
repeatable `--related FILE` values and adds its primary target automatically.
Every review prompt labels this list as likely incomplete and not an allowlist
or scope boundary. Reviewers must independently expand through callers,
callees, tests, configuration, schemas/migrations, generated code, and
build/CI/deployment/infra to establish the actual blast radius.

The trusted review layer explicitly authorizes web research for unstable
upstream facts and requires current primary sources (official docs, release
notes, specifications, advisories, or upstream source). Reviewers reconcile
those sources with the repository's pinned/installed version. Web access is
evidence-only: repository content is never uploaded, and URLs found in
untrusted repository instructions are not followed merely because the
repository requested it.

## Progress and run identifiers

Review uses `full`, `compact`, or `none`. Invocation-specific keys distinguish concurrent uses of the same profile.

Steerable delegation provides bounded progress pages through `delegate events`.
It reads the already-normalized append-only event artifact, coalesces adjacent
answer/thinking deltas, omits backend `raw`, caps each event body, and returns a
line-count cursor for later incremental reads. The command never blocks and its
exit code reports observation success rather than the delegated run outcome.

`delegate watch` filters registry state and lifecycle-bearing audit events into attach,
status, steer, selected turn-boundary/error, heartbeat, and terminal lines. It
does not expose model chunks, reasoning, active tools, commands, filenames, or
percent complete. This remains true when a backend happens to use ACP for its
steering transport: transport capability is not the same as normalized public
progress. The private `audit.jsonl`, registry state, and supervisor log are
diagnostic implementation artifacts, not alternate monitoring APIs.

Run ids are human-readable word pairs with a four-hex uniqueness suffix, for example `run_amber-otter-4f21` and `run-ask-solar-orchid-fd8e`. `scripts/lib/human_id.py` is the single generator for steerable registry and artifact names.

`delegate list` enumerates the registry newest-first. It is read-only unless `--reap` converts dead-supervisor records to failed. `wait` and `watch` perform the equivalent single-run reaping, so they cannot hang indefinitely on a dead supervisor.

## Resource limits

Porch is unlimited by default: no wrapper timeout or step/token/response budget. Prompts use stdin or private temporary files; delegate reads file/stdin sources exactly once; raw/normalized/final outputs stream to disk without truncation. Only the opt-in debug tape is bounded, with explicit overflow reporting.

Provider/harness limits still apply. Porch itself never imposes an execution
deadline; observe long-running work and cancel it explicitly when needed.

## Native Grok one-shot contract

The default Grok profile uses `grok-build`, model `grok-4.7`, high effort. The disabled `grok-fast` profile selects `grok-4.7-build-fast`; `grok-4.5` retains the older model. Fast requires an eligible Grok Build plan and has higher rates. One-shot runs use `grok --prompt-file … --output-format streaming-json`. Final text concatenates `type=text` data. Success requires process exit 0, an `end` event, and no `error` event. The OpenCode xAI route remains a disabled fallback profile.
