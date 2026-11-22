#!/bin/bash
#
# Unified backend runner for pragmatic-orchestration v5.
#
# Usage:
#   backend_run.sh --mode review|review-researchers|delegate --agent-id <id> [options] ["prompt"]
#   backend_run.sh --mode review --agent-id codex --prompt-file path.txt
#   echo "prompt" | backend_run.sh --mode review --agent-id codex
#
# Options:
#   --mode review|delegate  Required. review = read-only; delegate = full YOLO.
#                            review-researchers = review whose worker may start
#                            read-only researchers; needs --researchers.
#   --researchers <ids>      Comma-separated researcher profile ids
#                            (mode review-researchers only).
#   --style adviser|assignment
#                            Review prompt style. adviser (default): adviser
#                            role and five headings. assignment: the caller's
#                            brief sets the role and the answer structure.
#   --agent-id <id>          Required. Exact config agent id.
#   --role <role>            Override role from config.
#   --prompt-file <path>     Raw prompt from file (implies PORCH_RAW_PROMPT=1).
#   --raw                    Send prompt without principles/role/template wrap.
#   --no-wrap                Alias for --raw.
#   -h, --help
#
# Env overrides (tests use these to inject fake CLIs):
#   PORCH_BIN_CODEX / PORCH_BIN_CLAUDE / PORCH_BIN_OPENCODE
#   PORCH_BIN_GEMINI / PORCH_BIN_GROK / PORCH_BIN_DEVIN
#   PORCH_DUMP_ARGV=<path>  — if set, write the exact argv array as JSONL and exit 0
#                                 without executing (used by argv safety tests).
#
# Observability:
#   progress → stderr live while the model is still running (not post-hoc)
#   final answer text → stdout only
#   artifacts under $PORCH_RUN_DIR when archival enabled
#
# Streaming architecture (structured backends):
#   prompt_file -> backend_cmd 2>stderr_file | normalize_stream.py --raw-out --progress --extract-text
#     • each raw stdout line is persisted immediately (--raw-out, flushed)
#     • each event is normalized and written immediately (stdout → NORM_STREAM)
#     • compact semantic progress reaches stderr before process completion
#   PIPESTATUS preserves backend exit/signal and normalizer validation
#   independently of pipefail rightmost-status semantics.
#
set -euo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "$LIB_DIR/common.sh"
# shellcheck source=config.sh
source "$LIB_DIR/config.sh"
# shellcheck source=progress.sh
source "$LIB_DIR/progress.sh"
# shellcheck source=artifacts.sh
source "$LIB_DIR/artifacts.sh"

MODE=""
AGENT_ID=""
ROLE_OVERRIDE=""
PROMPT=""
PROMPT_FILE=""
PROMPT_SOURCE=""   # positional | file | stdin — shell-interpolation warn only for positional
RAW_MODE=0
DEBUG_EVENTS=""
RESEARCHERS=""
REVIEW_STYLE="adviser"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --mode)        MODE="${2:-}"; shift 2 ;;
        --agent-id)    AGENT_ID="${2:-}"; shift 2 ;;
        --role)        ROLE_OVERRIDE="${2:-}"; shift 2 ;;
        --prompt-file)
            [[ $# -ge 2 && -n "$2" ]] || { echo "Error: --prompt-file requires a path" >&2; exit $EXIT_USAGE; }
            PROMPT_FILE="$2"; PROMPT_SOURCE="file"; shift 2 ;;
        --raw|--no-wrap) RAW_MODE=1; shift ;;
        --researchers)
            [[ $# -ge 2 && -n "$2" ]] || { echo "Error: --researchers requires a comma-separated list of profile ids" >&2; exit $EXIT_USAGE; }
            RESEARCHERS="$2"; shift 2 ;;
        --style)
            [[ $# -ge 2 && -n "$2" ]] || { echo "Error: --style requires adviser or assignment" >&2; exit $EXIT_USAGE; }
            REVIEW_STYLE="$2"; shift 2 ;;
        --debug-events)
            if [[ -n "${2:-}" && "${2:-}" != -* ]]; then
                DEBUG_EVENTS="$2"; shift 2
            else
                DEBUG_EVENTS="__default__"; shift
            fi
            ;;
        --debug-events=*) DEBUG_EVENTS="${1#--debug-events=}"; shift ;;
        -h|--help)     sed -n '2,40p' "$0"; exit $EXIT_OK ;;
        --)            shift; PROMPT="${1:-}"; PROMPT_SOURCE="positional"; break ;;
        -*)            echo "Error: unknown flag: $1" >&2; exit $EXIT_USAGE ;;
        *)             PROMPT="$1"; PROMPT_SOURCE="positional"; shift; break ;;
    esac
done

[[ -n "$MODE" ]] || { echo "Error: --mode required (review|delegate)" >&2; exit $EXIT_USAGE; }
# Explicit mode→capability matrix (fail closed for unknown modes).
# access_class is the coarse readonly|yolo switch; full matrix fields
# (filesystem/shell/web/memory/subagents) also drive backend-specific argv
# where the backend can enforce them.
MODE_CAPS_JSON="$(
    python3 "$LIB_DIR/mode_policy.py" "$MODE" --json 2>/dev/null
)" || {
    echo "Error: --mode must be a known Porch mode (review|delegate|…); got: $MODE" >&2
    exit $EXIT_USAGE
}
ACCESS_POLICY="$(
    MODE_CAPS_JSON="$MODE_CAPS_JSON" python3 -c 'import json,os; print(json.loads(os.environ["MODE_CAPS_JSON"])["access_class"])'
)"
MODE_CAP_WEB="$(
    MODE_CAPS_JSON="$MODE_CAPS_JSON" python3 -c 'import json,os; print("true" if json.loads(os.environ["MODE_CAPS_JSON"]).get("web") else "false")'
)"
MODE_CAP_MEMORY="$(
    MODE_CAPS_JSON="$MODE_CAPS_JSON" python3 -c 'import json,os; print("true" if json.loads(os.environ["MODE_CAPS_JSON"]).get("memory") else "false")'
)"
# none | researchers | any
MODE_CAP_SUBAGENTS="$(
    MODE_CAPS_JSON="$MODE_CAPS_JSON" python3 -c 'import json,os; print(json.loads(os.environ["MODE_CAPS_JSON"]).get("subagents") or "none")'
)"
MODE_CAP_SHELL="$(
    MODE_CAPS_JSON="$MODE_CAPS_JSON" python3 -c 'import json,os; print("true" if json.loads(os.environ["MODE_CAPS_JSON"]).get("shell") else "false")'
)"
MODE_CAP_FILESYSTEM="$(
    MODE_CAPS_JSON="$MODE_CAPS_JSON" python3 -c 'import json,os; print(json.loads(os.environ["MODE_CAPS_JSON"]).get("filesystem") or "read")'
)"
[[ -n "$AGENT_ID" ]] || { echo "Error: --agent-id required" >&2; exit $EXIT_USAGE; }

# The review family shares the layered prompt and the read-only argv.
IS_REVIEW=0
case "$MODE" in review|review-researchers) IS_REVIEW=1 ;; esac
case "$REVIEW_STYLE" in
    adviser) ;;
    assignment)
        [[ "$IS_REVIEW" -eq 1 ]] || { echo "Error: --style assignment applies to review modes only" >&2; exit $EXIT_USAGE; }
        ;;
    *) echo "Error: --style must be adviser or assignment (got: $REVIEW_STYLE)" >&2; exit $EXIT_USAGE ;;
esac
# Researcher mode is never entered by accident and never without researchers.
if [[ "$MODE" == "review-researchers" && -z "$RESEARCHERS" ]]; then
    echo "Error: --mode review-researchers requires --researchers <profile ids>" >&2
    exit $EXIT_USAGE
fi
if [[ -n "$RESEARCHERS" && "$MODE" != "review-researchers" ]]; then
    echo "Error: --researchers requires --mode review-researchers" >&2
    exit $EXIT_USAGE
fi

config_validate || exit $EXIT_CONFIG_ERROR

# Shared backend contract: identity, model/effort, binary, capabilities.
# Falls back to config.sh fields if the resolver fails (should not happen).
_RESOLVE_ARGS=(resolve "$AGENT_ID" --mode "$MODE")
if [[ "$MODE" == "delegate" ]]; then
    _RESOLVE_ARGS+=(--require-delegate)
fi
RESOLVED_JSON="$(
    python3 "$LIB_DIR/backend_contract.py" "${_RESOLVE_ARGS[@]}" 2>/dev/null
)" || RESOLVED_JSON=""
unset _RESOLVE_ARGS

if [[ -n "$RESOLVED_JSON" ]]; then
    eval "$(
        RESOLVED_JSON="$RESOLVED_JSON" python3 - <<'PY'
import json, os, shlex, sys
# Native Windows Python must not add CR characters to shell assignments.
sys.stdout.reconfigure(newline='\n')
d = json.loads(os.environ["RESOLVED_JSON"])
def emit(k, v):
    print(f"{k}={shlex.quote('' if v is None else str(v))}")
emit("BACKEND", d.get("backend"))
emit("MODEL", d.get("model"))
emit("EFFORT", d.get("effort"))
emit("LABEL", d.get("label") or d.get("agent_id"))
emit("ROLE_ID", d.get("role") or "analyst")
emit("BIN", d.get("binary"))
emit("REVIEW_INSTRUCTIONS", d.get("review_instructions") or "")
emit("ACCESS_POLICY", d.get("access_class") or "")
PY
    )"
    ROLE_ID="${ROLE_OVERRIDE:-$ROLE_ID}"
    ROLE_ID="${ROLE_ID:-analyst}"
    # Profile review instructions are role text; an assignment sets its own role.
    if [[ "$IS_REVIEW" -ne 1 || "$REVIEW_STYLE" == "assignment" ]]; then
        REVIEW_INSTRUCTIONS=""
    fi
else
    BACKEND="$(config_get_field "$AGENT_ID" backend)" || exit $EXIT_CONFIG_ERROR
    MODEL="$(config_get_field "$AGENT_ID" model)"
    LABEL="$(config_get_field "$AGENT_ID" label)"; LABEL="${LABEL:-$AGENT_ID}"
    EFFORT="$(config_get_field "$AGENT_ID" effort)"
    ROLE_ID="${ROLE_OVERRIDE:-$(config_get_field "$AGENT_ID" role)}"
    ROLE_ID="${ROLE_ID:-analyst}"
    REVIEW_INSTRUCTIONS=""
    if [[ "$IS_REVIEW" -eq 1 && "$REVIEW_STYLE" != "assignment" ]]; then
        REVIEW_INSTRUCTIONS="$(config_get_field "$AGENT_ID" review_instructions)"
    fi
    case "$BACKEND" in
        codex-cli)
            MODEL="${CODEX_MODEL:-$MODEL}"
            EFFORT="${CODEX_EFFORT:-$EFFORT}"
            ;;
        claude-code)
            MODEL="${CLAUDE_MODEL:-$MODEL}"
            EFFORT="${CLAUDE_EFFORT:-$EFFORT}"
            ;;
        opencode)
            MODEL="${OPENCODE_MODEL:-$MODEL}"
            EFFORT="${OPENCODE_EFFORT:-$EFFORT}"
            ;;
        grok-build)
            MODEL="${GROK_MODEL:-$MODEL}"
            EFFORT="${GROK_EFFORT:-$EFFORT}"
            ;;
        gemini-cli)
            MODEL="${GEMINI_MODEL:-$MODEL}"
            ;;
        devin-cli)
            MODEL="${DEVIN_MODEL:-$MODEL}"
            ;;
        antigravity-cli)
            MODEL="${AGY_MODEL:-$MODEL}"
            EFFORT="${AGY_EFFORT:-$EFFORT}"
            ;;
        cursor-cli)
            # Empty stays empty: an explicit model is refused on the Free tier,
            # and the account default (Auto) is selected by omitting --model.
            MODEL="${CURSOR_MODEL:-$MODEL}"
            ;;
    esac
    BIN=""
fi

if [[ "$MODE" == "delegate" ]]; then
    if [[ "$BACKEND" == "gemini-cli" ]]; then
        echo "Error: agent '$AGENT_ID' backend gemini-cli is review-only; cannot delegate" >&2
        exit $EXIT_CONFIG_ERROR
    fi
    supports="$(config_get_field "$AGENT_ID" supports_delegate 2>/dev/null || true)"
    if [[ "$supports" == "false" || "$supports" == "0" ]]; then
        echo "Error: agent '$AGENT_ID' has supports_delegate=false" >&2
        exit $EXIT_CONFIG_ERROR
    fi
fi

# Researcher mode: decide how this backend reaches the named researcher
# profiles, or refuse. lib/researchers.py is the single source for the routes
# and for the channel list the worker's contract carries.
RESEARCH_NATIVE_PROFILE=""
RESEARCH_NATIVE_MODEL=""
RESEARCH_NATIVE_EFFORT=""
RESEARCH_PORCH_PROFILES=""
RESEARCH_UNREACHABLE=""
RESEARCH_CHANNELS=""
RESEARCH_CLAUDE_DENIED=""
if [[ "$MODE" == "review-researchers" ]]; then
    if [[ "$RAW_MODE" -eq 1 || -n "$PROMPT_FILE" || -n "${PORCH_RAW_PROMPT:-}" ]]; then
        echo "Error: researcher mode always carries the review contract; a raw prompt is not allowed" >&2
        exit $EXIT_USAGE
    fi
    PORCH_SELF="$(cd "$LIB_DIR/.." && pwd)/porch"
    RESEARCH_PLAN_JSON="$(
        python3 "$LIB_DIR/researchers.py" plan "$AGENT_ID" --researcher "$RESEARCHERS" --porch "$PORCH_SELF"
    )" || exit $EXIT_CONFIG_ERROR
    eval "$(
        RESEARCH_PLAN_JSON="$RESEARCH_PLAN_JSON" PYTHONPATH="$LIB_DIR${PYTHONPATH:+:$PYTHONPATH}" python3 - <<'PY'
import json, os, shlex, sys
from researchers import CLAUDE_DENIED_AGENT_TYPES
sys.stdout.reconfigure(newline='\n')
d = json.loads(os.environ["RESEARCH_PLAN_JSON"])
def emit(k, v):
    print(f"{k}={shlex.quote(str(v))}")
emit("RESEARCH_NATIVE_PROFILE", d["native_profile"])
emit("RESEARCH_NATIVE_MODEL", d["native_model"])
emit("RESEARCH_NATIVE_EFFORT", d["native_effort"])
emit("RESEARCH_PORCH_PROFILES", ",".join(d["porch_profiles"]))
emit("RESEARCH_UNREACHABLE", ",".join(d["unreachable"]))
emit("RESEARCH_CHANNELS", "\n".join(d["channels"]))
emit("RESEARCH_CLAUDE_DENIED", ",".join(f"Agent({t})" for t in CLAUDE_DENIED_AGENT_TYPES))
PY
    )"
    unset RESEARCH_PLAN_JSON
    progress_info "researchers" "agent=$AGENT_ID native=${RESEARCH_NATIVE_PROFILE:-none} porch=${RESEARCH_PORCH_PROFILES:-none}"
    if [[ -n "$RESEARCH_UNREACHABLE" ]]; then
        # A narrower menu, not a weaker guarantee; still said aloud at any progress style.
        echo "[porch] note: researcher profiles this backend cannot reach: $RESEARCH_UNREACHABLE (backend=$BACKEND)" >&2
    fi
fi
# Porch started from this worker's shell is limited by this variable: only
# `review ask -a` a listed profile. A run that itself began in such a shell
# (a researcher) passes an empty list on: its shell starts no porch at all.
if [[ "$MODE" == "review-researchers" ]]; then
    export PORCH_RESEARCHER_PROFILES="$RESEARCH_PORCH_PROFILES"
elif [[ -n "${PORCH_RESEARCHER_PROFILES+x}" ]]; then
    export PORCH_RESEARCHER_PROFILES=""
fi

if [[ -n "$PROMPT_FILE" ]]; then
    [[ "$PROMPT_SOURCE" != "positional" ]] || { echo "Error: cannot combine --prompt-file with a positional prompt" >&2; exit $EXIT_USAGE; }
    [[ -f "$PROMPT_FILE" ]] || { echo "Error: prompt file not found: $PROMPT_FILE" >&2; exit $EXIT_USAGE; }
    PROMPT="$(cat < "$PROMPT_FILE")"
    RAW_MODE=1
    PROMPT_SOURCE="file"
fi

if [[ -z "$PROMPT_SOURCE" && ! -t 0 ]]; then
    PROMPT="$(cat)"
    PROMPT_SOURCE="stdin"
fi
[[ "$PROMPT" =~ [^[:space:]] ]] || { echo "Error: no prompt provided" >&2; exit $EXIT_USAGE; }

# Only warn for unsafe positional-prompt usage. File/stdin content often
# legitimately contains backticks / $(...) from code samples.
if [[ "$PROMPT_SOURCE" == "positional" ]]; then
    warn_shell_special_in_prompt "$PROMPT"
fi

# Drop any inherited FULL_PROMPT *before* assigning. In bash, assigning to a
# name that arrived exported keeps the export attribute — which would put the
# full prompt body back into the child environment and hit ARG_MAX/E2BIG.
unset FULL_PROMPT 2>/dev/null || true

# Stage prompt bodies on disk before the pipeline — never put large text in env.
_PROMPT_STAGE_DIR="$(mktemp -d "${TMPDIR:-/tmp}/porch-prompt.XXXXXX")"
_PROMPT_USER_FILE="$_PROMPT_STAGE_DIR/user.txt"
_PROMPT_ROLE_FILE="$_PROMPT_STAGE_DIR/role.txt"
printf '%s' "$PROMPT" > "$_PROMPT_USER_FILE"

if [[ "$RAW_MODE" -eq 1 || -n "${PORCH_RAW_PROMPT:-}" ]]; then
    export PORCH_RAW_PROMPT=1
    FULL_PROMPT="$(
        python3 "$LIB_DIR/prompt_pipeline.py" --mode raw --raw --user-file "$_PROMPT_USER_FILE" 2>/dev/null \
        || cat "$_PROMPT_USER_FILE"
    )"
else
    if ! ROLE_PROMPT="$(get_role_prompt "$ROLE_ID")"; then
        rm -rf "$_PROMPT_STAGE_DIR"
        echo "Error: unknown role '$ROLE_ID' for agent $AGENT_ID" >&2
        exit $EXIT_CONFIG_ERROR
    fi
    printf '%s' "$ROLE_PROMPT" > "$_PROMPT_ROLE_FILE"
    if [[ -n "$REVIEW_INSTRUCTIONS" ]]; then
        {
            printf '%s' "$ROLE_PROMPT"
            printf '\n\nMODEL-SPECIFIC REVIEW INSTRUCTIONS:\n%s' "$REVIEW_INSTRUCTIONS"
        } > "$_PROMPT_ROLE_FILE"
    fi
    SKIP_TPL_ARGS=()
    if [[ -n "${PORCH_SKIP_OUTPUT_TEMPLATE:-}" ]]; then
        SKIP_TPL_ARGS+=(--skip-output-template)
    fi
    PIPE_MODE="$MODE"
    if [[ -n "${PORCH_SKIP_OUTPUT_TEMPLATE:-}" && "$MODE" == "review" ]]; then
        PIPE_MODE="review-code"
    fi
    CONTRACT_ARGS=()
    if [[ "$IS_REVIEW" -eq 1 ]]; then
        CONTRACT_ARGS+=(--style "$REVIEW_STYLE")
    fi
    if [[ -n "$RESEARCH_CHANNELS" ]]; then
        printf '%s\n' "$RESEARCH_CHANNELS" > "$_PROMPT_STAGE_DIR/researcher-channels.txt"
        CONTRACT_ARGS+=(--researcher-channels-file "$_PROMPT_STAGE_DIR/researcher-channels.txt")
    fi
    FULL_PROMPT="$(
        python3 "$LIB_DIR/prompt_pipeline.py" \
            --mode "$PIPE_MODE" \
            --role-file "$_PROMPT_ROLE_FILE" \
            --user-file "$_PROMPT_USER_FILE" \
            ${SKIP_TPL_ARGS[@]+"${SKIP_TPL_ARGS[@]}"} \
            ${CONTRACT_ARGS[@]+"${CONTRACT_ARGS[@]}"} \
            2>/dev/null
    )" || FULL_PROMPT=""
    if [[ -z "$FULL_PROMPT" && ( "$MODE" == "review-researchers" || "$REVIEW_STYLE" != "adviser" ) ]]; then
        # The shell fallback knows only the default contract (an adviser who
        # works alone). Sending that instead would change what the worker may do.
        rm -rf "$_PROMPT_STAGE_DIR"
        echo "Error: cannot build the review contract for mode=$MODE style=$REVIEW_STYLE" >&2
        exit $EXIT_CONFIG_ERROR
    fi
    if [[ -z "$FULL_PROMPT" ]]; then
        if [[ -n "$REVIEW_INSTRUCTIONS" ]]; then
            ROLE_PROMPT+=$'\n\nMODEL-SPECIFIC REVIEW INSTRUCTIONS:\n'
            ROLE_PROMPT+="$REVIEW_INSTRUCTIONS"
        fi
        FULL_PROMPT="$(build_prompt "$ROLE_PROMPT" "$PROMPT" </dev/null)"
    fi
fi
rm -rf "$_PROMPT_STAGE_DIR"
unset _PROMPT_STAGE_DIR _PROMPT_USER_FILE _PROMPT_ROLE_FILE
# NEVER export FULL_PROMPT — large prompts exceed ARG_MAX when copied into the
# environment (execve counts env + argv). Prompt bodies travel via temp file /
# stdin only.

# Resolve CLI binary if shared contract did not supply one.
bin_for() {
    case "$1" in
        codex-cli)   echo "${PORCH_BIN_CODEX:-codex}" ;;
        claude-code) echo "${PORCH_BIN_CLAUDE:-claude}" ;;
        opencode)    echo "${PORCH_BIN_OPENCODE:-opencode}" ;;
        gemini-cli)  echo "${PORCH_BIN_GEMINI:-gemini}" ;;
        grok-build)  echo "${PORCH_BIN_GROK:-grok}" ;;
        devin-cli)   echo "${PORCH_BIN_DEVIN:-devin}" ;;
        antigravity-cli) echo "${PORCH_BIN_AGY:-agy}" ;;
        *)           echo "" ;;
    esac
}

if [[ -z "${BIN:-}" ]]; then
    BIN="$(bin_for "$BACKEND")"
fi
if [[ -z "$BIN" ]]; then
    echo "Error: unknown backend '$BACKEND' for agent $AGENT_ID" >&2
    exit $EXIT_CONFIG_ERROR
fi
bin_usable() {
    command -v "$1" &>/dev/null || [[ -x "$1" ]]
}
if ! bin_usable "$BIN"; then
    # Native Windows Python can resolve an npm .CMD shim that MSYS bash cannot
    # execute. Use its extensionless sibling, keeping the resolved directory
    # (and any explicit binary override) authoritative.
    _bin_fallback=""
    if [[ "$BIN" == *[/\\]* && "$BIN" == *.[cC][mM][dD] ]]; then
        _bin_fallback="${BIN%.*}"
        _bin_fallback="${_bin_fallback//\\//}"
        if command -v cygpath &>/dev/null; then
            _bin_fallback="$(cygpath -u "$_bin_fallback" 2>/dev/null)" || _bin_fallback=""
        fi
    fi
    if [[ -n "$_bin_fallback" && -x "$_bin_fallback" ]]; then
        BIN="$_bin_fallback"
    else
        echo "Error: backend CLI not found: $BIN (backend=$BACKEND)" >&2
        exit $EXIT_CONFIG_ERROR
    fi
    unset _bin_fallback
fi

# Codex model alias + default efforts are already applied by backend_contract
# when resolve succeeded; keep shell fallbacks for the legacy path.
if [[ "$BACKEND" == "codex-cli" && "$MODEL" == "gpt-5.6" ]]; then
    MODEL="gpt-5.6-sol"
fi
case "$BACKEND" in
    codex-cli|claude-code|grok-build) EFFORT="${EFFORT:-high}" ;;
    opencode)
        if [[ "$EFFORT" == "none" ]]; then EFFORT=""; fi
        ;;
esac

# Opt-in debug event tape (CLI or PORCH_DEBUG_EVENTS=1).
if [[ -n "$DEBUG_EVENTS" ]]; then
    if [[ "$DEBUG_EVENTS" == "__default__" ]]; then
        export PORCH_DEBUG_EVENTS=1
    else
        export PORCH_DEBUG_EVENTS=1
        export PORCH_DEBUG_EVENTS_PATH="$DEBUG_EVENTS"
    fi
fi

# Progress identity = the invocation, not just the agent. Fan-out layers run the
# same agent in several roles/stages concurrently; without the key their live
# lines are indistinguishable. Artifact paths reuse the same value below.
PROGRESS_ID="${PORCH_ARTIFACT_KEY:-$AGENT_ID}"
progress_agent_start "$PROGRESS_ID" "$BACKEND" "$MODE" "$MODEL" "$EFFORT"

# Ensure run dir + artifact subdirs exist (honors pre-set PORCH_RUN_DIR)
if [[ "${PORCH_SAVE_OUTPUTS:-1}" != "0" ]]; then
    if [[ -z "${PORCH_RUN_DIR:-}" ]]; then
        artifacts_init_run "$MODE"
    else
        mkdir -p "$PORCH_RUN_DIR/raw" "$PORCH_RUN_DIR/normalized" "$PORCH_RUN_DIR/final"
        export PORCH_RUN_DIR
    fi
fi
# Artifact key identifies this invocation. Fan-out callers set an explicit
# PORCH_ARTIFACT_KEY (e.g. "codex.security", "discovery-small.0.x.analyst",
# "judge.primary.claude-code"). Ordinary ask/delegate leave it unset → agent id.
ARTIFACT_KEY="$PROGRESS_ID"
artifacts_paths_for "$ARTIFACT_KEY"

# Build argv into array CMD
CMD=()
PROMPT_VIA_FILE=0
PROMPT_FILE_PATH=""
RUNTIME_SETTINGS_FILE=""

build_cmd_codex() {
    local approval_sandbox
    CMD=("$BIN")
    # Top-level -a is ask-for-approval
    if [[ "$ACCESS_POLICY" == "readonly" ]]; then
        if [[ "$MODE_CAP_SUBAGENTS" == "researchers" ]]; then
            # Sub-agents inherit the parent turn's sandbox and approval policy
            # (verified on 0.170.0), so they share the reviewer's workspace
            # sandbox and report-only contract. The default sub-agent model is
            # the researcher profile's,
            # and the spawn tool's own model parameters are hidden, so the
            # worker cannot pick a costlier one.
            CMD+=(--disable multi_agent
                  -c features.multi_agent_v2.enabled=true
                  -c features.multi_agent_v2.expose_spawn_agent_model_overrides=false
                  -c "agents.default_subagent_model=\"$RESEARCH_NATIVE_MODEL\"")
            if [[ -n "$RESEARCH_NATIVE_EFFORT" ]]; then
                CMD+=(-c "agents.default_subagent_reasoning_effort=\"$RESEARCH_NATIVE_EFFORT\"")
            fi
        else
            CMD+=(--disable multi_agent --disable multi_agent_v2)
            if [[ "$MODE_CAP_SUBAGENTS" == "none" ]]; then
                # The two feature flags alone do not switch sub-agents off:
                # Codex 0.170 then takes the multi-agent version from the model
                # catalog and still offers the collaboration tools.
                CMD+=(-c agents.enabled=false)
            fi
        fi
        if [[ "${MODE_CAP_WEB:-true}" == "true" || "${MODE_CAP_WEB:-1}" == "1" ]]; then
            CMD+=(--search)
        fi
        CMD+=(-a never)
        if [[ -n "$EFFORT" ]]; then
            CMD+=(-c "model_reasoning_effort=\"$EFFORT\"")
        fi
        # A review shell is a working shell: tests, builds and package or
        # documentation lookups need temp, the working folder and the network.
        # workspace-write keeps writes to the working folder and temp, and
        # the report-only prompt contract keeps the reviewer from changing
        # them, as on the backends without a kernel sandbox. Media has no
        # shell and stays read-only.
        local codex_sandbox="read-only"
        if [[ "${MODE_CAP_SHELL:-true}" == "true" ]]; then
            codex_sandbox="workspace-write"
            CMD+=(-c sandbox_workspace_write.network_access=true)
        fi
        CMD+=(exec --model "$MODEL" --sandbox "$codex_sandbox" --skip-git-repo-check --ephemeral)
        if [[ -n "${PORCH_CODEX_NO_MCP:-}" ]]; then
            CMD+=(--ignore-user-config)
        fi
        # Media mode attaches staged images to the initial prompt. The
        # --image=PATH form matters: a bare -i/--image is variadic and would
        # swallow the following stdin marker.
        if [[ "$MODE" == "media" && -n "${PORCH_MEDIA_IMAGES:-}" ]]; then
            local image
            while IFS= read -r image; do
                [[ -n "$image" ]] && CMD+=("--image=$image")
            done <<< "$PORCH_MEDIA_IMAGES"
        fi
        # Structured events for observability; final text via -o
        CMD+=(--json)
    else
        # delegate YOLO: full bypass, no sandbox
        if [[ -n "$EFFORT" ]]; then
            CMD+=(-c "model_reasoning_effort=\"$EFFORT\"")
        fi
        CMD+=(exec --model "$MODEL" --dangerously-bypass-approvals-and-sandbox --skip-git-repo-check)
        CMD+=(--json)
    fi
    # Prompt + last message file filled at run time via placeholders handled below
}

build_cmd_claude() {
    # -p enables headless print mode; the complete prompt is read from stdin.
    # Review uses Claude's normal tool loop, not its plan workflow.
    CMD=("$BIN")
    if [[ "$ACCESS_POLICY" == "readonly" ]]; then
        # `plan` is the wrong workflow for an independent review: Claude Code
        # deliberately creates a plan artifact under ~/.claude/plans. dontAsk
        # stays non-interactive without activating that workflow. Dedicated
        # edit tools remain unavailable; Bash is trusted to follow the review
        # prompt's report-only contract so git/rg/test diagnostics still work.
        CMD+=(--permission-mode dontAsk)
        # Preserve the full review loop while disabling project/user
        # customizations, hooks, MCP, custom agents, browser integration, and
        # on-disk session history. Unlike --bare, safe-mode does not force an
        # API-key-only authentication path.
        CMD+=(--safe-mode --no-session-persistence --no-chrome)
        if [[ "${MODE_CAP_SHELL:-true}" != "true" ]]; then
            # Media mode: the Read tool sees staged files; no shell at all.
            CMD+=(--disallowedTools "Edit,Write,NotebookEdit,Agent,Task,Bash")
            CMD+=(--allowedTools "Read")
        elif [[ "$MODE_CAP_SUBAGENTS" == "researchers" ]]; then
            # --tools is an allowlist of built-in tools: it removes Workflow,
            # the schedulers and every other way to start work elsewhere.
            # With a Claude researcher profile the Agent tool stays, cut down
            # to the read-only Explore type by denying the other built-in
            # types (custom agents do not load under --safe-mode). The
            # session's Edit/Write denial covers subagents too; their model is
            # forced, and they cannot spawn further (depth 1).
            local claude_tools="Bash,Read,Grep,Glob" claude_allowed="Bash"
            local claude_denied="Edit,Write,NotebookEdit"
            if [[ "${MODE_CAP_WEB:-true}" == "true" || "${MODE_CAP_WEB:-1}" == "1" ]]; then
                claude_tools+=",WebSearch,WebFetch"
                claude_allowed+=",WebSearch,WebFetch"
            fi
            if [[ -n "$RESEARCH_NATIVE_PROFILE" ]]; then
                claude_tools+=",Agent"
                claude_denied+=",$RESEARCH_CLAUDE_DENIED"
                export CLAUDE_CODE_SUBAGENT_MODEL="$RESEARCH_NATIVE_MODEL"
                export CLAUDE_CODE_SUBAGENT_MODEL_FORCE=1
                export CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH=1
            else
                claude_denied+=",Agent,Task"
            fi
            CMD+=(--tools "$claude_tools")
            CMD+=(--disallowedTools "$claude_denied")
            CMD+=(--allowedTools "$claude_allowed")
        else
            CMD+=(--disallowedTools "Edit,Write,NotebookEdit,Agent,Task")
            if [[ "${MODE_CAP_WEB:-true}" == "true" || "${MODE_CAP_WEB:-1}" == "1" ]]; then
                CMD+=(--allowedTools "Bash,WebSearch,WebFetch")
            else
                CMD+=(--allowedTools "Bash")
            fi
        fi
    else
        CMD+=(--dangerously-skip-permissions)
    fi
    CMD+=(--model "$MODEL")
    if [[ -n "$EFFORT" ]]; then
        CMD+=(--effort "$EFFORT")
    fi
    # Prefer stream-json for observability when not dumping argv only
    if [[ -z "${PORCH_DUMP_ARGV:-}" ]]; then
        CMD+=(--output-format stream-json --verbose)
    else
        CMD+=(--output-format text)
    fi
    CMD+=(-p)
}

build_cmd_opencode() {
    CMD=("$BIN" run --standalone)
    if [[ "$ACCESS_POLICY" == "readonly" ]]; then
        # The built-in plan agent is a planning workflow with its own plan-file
        # permissions. Review needs the full analysis/tool loop instead. Define
        # a primary review agent at runtime: full shell/search, no edits or task
        # delegation. The trusted prompt carries the report-only contract.
        # In researcher mode the review agent may start one subagent, the
        # read-only "porch-researcher" defined here with the researcher
        # profile's model; a subagent uses its own permissions, not its
        # parent's, so it carries its own edit and subagent denials.
        local oc_researcher_model=""
        if [[ "$MODE_CAP_SUBAGENTS" == "researchers" && -n "$RESEARCH_NATIVE_PROFILE" ]]; then
            oc_researcher_model="$RESEARCH_NATIVE_MODEL"
            if [[ -n "$RESEARCH_NATIVE_EFFORT" ]]; then
                oc_researcher_model+="#$RESEARCH_NATIVE_EFFORT"
            fi
        fi
        OPENCODE_CONFIG_CONTENT="$(
            PORCH_OC_SUBAGENTS="$MODE_CAP_SUBAGENTS" \
            PORCH_OC_WEB="${MODE_CAP_WEB:-true}" \
            PORCH_OC_RESEARCHER_MODEL="$oc_researcher_model" \
            PORCH_OC_RESEARCHER_PROMPT="$LIB_DIR/../../prompts/review-researcher.txt" \
            PYTHONPATH="$LIB_DIR${PYTHONPATH:+:$PYTHONPATH}" python3 -c '
import json, os, sys
from researchers import RESEARCHER_AGENT
try:
    config = json.loads(os.environ.get("OPENCODE_CONFIG_CONTENT") or "{}")
    if not isinstance(config, dict) or not isinstance(config.get("agents", {}), dict):
        raise ValueError("configuration and agents must be objects")
    researchers = os.environ.get("PORCH_OC_SUBAGENTS") == "researchers"
    researcher_model = os.environ.get("PORCH_OC_RESEARCHER_MODEL") or ""
    permissions = [{"action": "edit", "resource": "*", "effect": "deny"},
                   {"action": "subagent", "resource": "*", "effect": "deny"}]
    if researchers:
        delegation = ("Start no agent except the read-only researchers your instructions allow; "
                      "never use other task delegation, other models, or external workers.")
    else:
        delegation = "Work alone: never use task delegation, subagents, other models, or external workers."
    # OpenCode 2 asks for consent to a web-search provider on the first
    # search, and `opencode run` cancels that form and fails the run. Settle
    # it in this run-only configuration: the provider OpenCode picks when
    # the mode allows web, search off otherwise. A caller setting wins.
    if "websearch" not in config:
        config["websearch"] = {"provider": "random"} if os.environ.get("PORCH_OC_WEB") == "true" else False
    agents = config.setdefault("agents", {})
    if researcher_model:
        # The last matching rule wins: only the researcher may be started.
        permissions.append({"action": "subagent", "resource": RESEARCHER_AGENT, "effect": "allow"})
        with open(os.environ["PORCH_OC_RESEARCHER_PROMPT"], encoding="utf-8") as handle:
            researcher_prompt = handle.read()
        agents[RESEARCHER_AGENT] = {
            "description": "Read-only researcher for one bounded fact-finding question",
            "system": researcher_prompt,
            "mode": "subagent",
            "model": researcher_model,
            "permissions": [{"action": "edit", "resource": "*", "effect": "deny"},
                            {"action": "subagent", "resource": "*", "effect": "deny"},
                            {"action": "question", "resource": "*", "effect": "deny"}],
        }
    agents["porch-review"] = {
        "description": "Independent read-only review performed without delegation",
        "system": "Review independently and read-only. " + delegation + " Use shell, search, and read tools to inspect any repository files needed for the real blast radius. Return only the report; never modify files or external state.",
        "mode": "primary",
        "permissions": permissions,
    }
    print(json.dumps(config))
except (ValueError, TypeError, OSError):
    print("Invalid OPENCODE_CONFIG_CONTENT: expected a JSON object with an agents object", file=sys.stderr)
    sys.exit(1)
')" || return 1
        export OPENCODE_CONFIG_CONTENT
        CMD+=(--agent porch-review --auto)
    else
        # --auto is current (opencode run --help): auto-approve non-denied permissions
        CMD+=(--agent build --auto)
    fi
    local selected_model="$MODEL"
    if [[ -n "$EFFORT" ]]; then
        selected_model="${selected_model}#${EFFORT}"
    fi
    CMD+=(-m "$selected_model")
    if [[ -z "${PORCH_DUMP_ARGV:-}" ]]; then
        CMD+=(--format json)
    else
        CMD+=(--format default)
    fi
}

build_cmd_gemini() {
    # Review only. Gemini Plan Mode is a planning workflow that writes a plan
    # artifact and restricts tools. YOLO keeps headless review fully capable;
    # extensions/MCP stay disabled and the trusted prompt forbids mutations and
    # delegation. Non-TTY stdin keeps the potentially large prompt off argv.
    # Gemini enables built-in subagents by default. A highest-precedence
    # temporary system settings file disables them for this invocation; `-e
    # none` is the documented way to disable every extension.
    RUNTIME_SETTINGS_FILE="$(mktemp "${TMPDIR:-/tmp}/porch-gemini-settings.XXXXXX")"
    printf '%s\n' '{"experimental":{"enableAgents":false,"extensionManagement":false,"extensionConfig":false}}' > "$RUNTIME_SETTINGS_FILE"
    export GEMINI_CLI_SYSTEM_SETTINGS_PATH="$RUNTIME_SETTINGS_FILE"
    CMD=("$BIN" --model "$MODEL" --approval-mode yolo -o text -e none --allowed-mcp-server-names "")
}

build_cmd_grok() {
    # One-shot headless: --prompt-file is documented as "Single-turn prompt from a file"
    # (same headless class as -p/--single for inline prompts). Do not also pass -p;
    # --prompt-file alone selects single-turn non-TUI mode.
    # Mode capability fields memory/subagents/shell/filesystem/web drive argv
    # where Grok Build exposes matching flags.
    CMD=("$BIN")
    if [[ "$ACCESS_POLICY" == "readonly" ]]; then
        # Kernel sandbox + strict tool allowlist (plan mode alone is NOT read-only).
        # The allowlist is exhaustive, so web tools must be named explicitly —
        # verified against Grok Build 0.2.112: without them the model reports it
        # has no web tool, with them it retrieves and cites live sources.
        # Review gets the `workspace` profile (writes to the working folder,
        # temp and ~/.grok, child network allowed) so its terminal can run
        # tests and lookups; the report-only prompt contract keeps it from
        # changing files. Without a shell the profile stays read-only.
        local grok_sandbox="read-only"
        if [[ "${MODE_CAP_SHELL:-true}" == "true" ]]; then
            grok_sandbox="workspace"
        fi
        CMD+=(--sandbox "$grok_sandbox" --no-plan)
        local grok_tools="read_file,grep,list_dir,run_terminal_cmd"
        if [[ "${MODE_CAP_WEB:-true}" == "true" ]]; then
            grok_tools+=",web_search,web_fetch"
        fi
        if [[ "$MODE_CAP_SUBAGENTS" == "researchers" ]]; then
            # One subagent type is defined and allowed: a read-only researcher
            # on the researcher profile's model. `Agent(name)` in --tools is
            # an allowlist of types; the sandbox is applied to the whole grok
            # process, so it holds for the subagent's shell as well, and a
            # subagent cannot spawn its own (verified on 1.0.89).
            local grok_agents
            grok_agents="$(
                PORCH_GROK_TOOLS="$grok_tools" \
                PORCH_GROK_MODEL="$RESEARCH_NATIVE_MODEL" \
                PORCH_GROK_EFFORT="$RESEARCH_NATIVE_EFFORT" \
                PORCH_GROK_PROMPT="$LIB_DIR/../../prompts/review-researcher.txt" \
                PYTHONPATH="$LIB_DIR${PYTHONPATH:+:$PYTHONPATH}" python3 -c '
import json, os
from researchers import RESEARCHER_AGENT
with open(os.environ["PORCH_GROK_PROMPT"], encoding="utf-8") as handle:
    prompt = handle.read()
agent = {
    "description": "Read-only researcher for one bounded fact-finding question",
    "prompt": prompt,
    "tools": os.environ["PORCH_GROK_TOOLS"].split(","),
}
if os.environ.get("PORCH_GROK_MODEL"):
    agent["model"] = os.environ["PORCH_GROK_MODEL"]
if os.environ.get("PORCH_GROK_EFFORT"):
    agent["effort"] = os.environ["PORCH_GROK_EFFORT"]
print(json.dumps({RESEARCHER_AGENT: agent}))
')" || return 1
            CMD+=(--agents "$grok_agents")
            CMD+=(--tools "$grok_tools,Agent(porch-researcher)")
            CMD+=(--disallowed-tools "search_replace,write")
        else
            CMD+=(--tools "$grok_tools")
            CMD+=(--disallowed-tools "search_replace,write,Agent")
            # mode_policy review: subagents=none — enforce it (Grok Build has
            # the flag). Media keeps subagents, as before.
            if [[ "$MODE_CAP_SUBAGENTS" == "none" ]]; then
                CMD+=(--no-subagents)
            fi
        fi
        # mode_policy review: memory=false — enforce when the flag exists.
        if [[ "${MODE_CAP_MEMORY:-false}" != "true" ]]; then
            CMD+=(--no-memory)
        fi
    else
        # YOLO: always-approve, no sandbox flag (default off = unrestricted)
        CMD+=(--always-approve)
    fi
    CMD+=(-m "$MODEL")
    if [[ -n "$EFFORT" ]]; then
        CMD+=(--reasoning-effort "$EFFORT")
    fi
    CMD+=(--output-format streaming-json)
    CMD+=(--verbatim)
    PROMPT_VIA_FILE=1
}

build_cmd_devin() {
    # Devin runs through `devin acp` (JSON-RPC over stdio) rather than print
    # mode: in `devin -p` a denied or confirmation-required tool call cancels
    # the whole session with no final text, so read-only review cannot be
    # enforced with config deny rules. `devin acp --agent-type review` is a
    # read-only + shell agent with no write/edit tools at all, so the boundary
    # holds by construction; exec stays available and is trusted to the
    # report-only prompt contract, same as claude-code. Delegate (yolo) uses
    # the default agent type plus session/set_mode bypass. The ACP helper
    # strips ACP_BACKEND from the child env — inside Devin Desktop it is
    # inherited as windsurf and the CLI reports "Not logged in".
    CMD=(
        python3 "$LIB_DIR/devin_acp_oneshot.py"
        --binary "$BIN" --model "$MODEL" --cwd "$PWD"
        --access "$ACCESS_POLICY" --web "${MODE_CAP_WEB:-false}"
    )
    PROMPT_VIA_FILE=1
}

build_cmd_agy() {
    # Headless stream-json in both directions: the prompt travels as one
    # stdin message (never argv) and the terminal result event carries the
    # authoritative answer. --dangerously-skip-permissions keeps tool calls
    # from being soft-denied without a prompt; explicit deny rules still win
    # over it and over any allow rule in the user's settings (Deny > Ask >
    # Allow). Review passes session-only deny rules, so agy's own file-write
    # tools fail with "permission check failed"; MCP tools and the browser
    # subagent are denied, and web reading only when the mode forbids web.
    # The shell stays available and is trusted to the report-only prompt
    # contract, as for the other backends. Plan mode is a planning workflow,
    # not a permission boundary, so it is not used.
    CMD=("$BIN" --input-format stream-json --output-format stream-json
         --dangerously-skip-permissions --disable-slash-commands)
    if [[ "$ACCESS_POLICY" == "readonly" ]]; then
        local deny='"write_file(*)","mcp(*)","execute_url(*)"'
        if [[ "${MODE_CAP_WEB:-true}" != "true" ]]; then
            deny+=',"read_url(*)"'
        fi
        if [[ "${MODE_CAP_SHELL:-true}" != "true" ]]; then
            # Media mode: without a shell the agent cannot "infer" a
            # transcript from file metadata or convert generated images.
            deny+=',"command(*)"'
        fi
        CMD+=(--settings "{\"permissions\":{\"deny\":[$deny]}}")
    fi
    if [[ "$MODE" == "media" && -n "${PORCH_OUTPUT_SCHEMA:-}" ]]; then
        CMD+=(--json-schema "$PORCH_OUTPUT_SCHEMA")
    fi
    if [[ -n "$MODEL" ]]; then
        CMD+=(--model "$MODEL")
    fi
    if [[ -n "$EFFORT" ]]; then
        CMD+=(--effort "$EFFORT")
    fi
}

build_cmd_cursor() {
    # Prompt on stdin, never as a positional argument. Review uses --mode plan,
    # which blocks edit and shell writes in print mode. --mode ask is refused
    # on the Free tier, and --sandbox enabled does not block writes. Delegate
    # adds --force and does not pass a mode. --model is omitted when empty so
    # the account default (Auto) is used.
    CMD=("$BIN" -p --trust --output-format stream-json --stream-partial-output)
    if [[ "$ACCESS_POLICY" == "readonly" ]]; then
        CMD+=(--mode plan)
    else
        CMD+=(--force)
    fi
    if [[ -n "$MODEL" ]]; then
        CMD+=(--model "$MODEL")
    fi
    CMD+=(--workspace "$PWD")
}

case "$BACKEND" in
    codex-cli)   build_cmd_codex ;;
    claude-code) build_cmd_claude ;;
    opencode)    build_cmd_opencode ;;
    gemini-cli)  build_cmd_gemini ;;
    grok-build)  build_cmd_grok ;;
    devin-cli)   build_cmd_devin ;;
    antigravity-cli) build_cmd_agy ;;
    cursor-cli)  build_cmd_cursor ;;
esac

# Prepare temp files
TMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/porch-backend.XXXXXX")"
cleanup_backend() {
    rm -rf "$TMP_DIR"
    [[ -z "$RUNTIME_SETTINGS_FILE" ]] || rm -f "$RUNTIME_SETTINGS_FILE"
}
trap cleanup_backend EXIT

if [[ "$BACKEND" == "cursor-cli" ]]; then
    # cursor-agent persists --model into cli-config.json even when the run
    # fails. Give every run an isolated CURSOR_CONFIG_DIR seeded from the
    # user's real config, so the default model is never rewritten as a side
    # effect of a porch run (one-shot, review or delegate).
    CURSOR_CONFIG_DIR="$(python3 "$LIB_DIR/cursor_config.py" --root "$TMP_DIR/cursor-config")"
    export CURSOR_CONFIG_DIR
fi

# Argv dump mode for tests — exact safety properties without executing
if [[ -n "${PORCH_DUMP_ARGV:-}" ]]; then
    dump_cmd=("${CMD[@]}")
    if [[ "$PROMPT_VIA_FILE" -eq 1 ]]; then
        dump_cmd+=(--prompt-file "__PROMPT_FILE__")
    elif [[ "$BACKEND" == "codex-cli" ]]; then
        dump_cmd+=(-o "__LAST_MSG__" -)
    fi
    printf '%s\0' "${dump_cmd[@]}" | AGENT_ID="$AGENT_ID" BACKEND="$BACKEND" MODE="$MODE" MODEL="$MODEL" DUMP_ARGV_PATH="$PORCH_DUMP_ARGV" python3 -c '
import json, os, sys
data = sys.stdin.buffer.read().split(b"\0")
argv = [x.decode() for x in data if x != b""]
obj = {
    "agent_id": os.environ["AGENT_ID"],
    "backend": os.environ["BACKEND"],
    "mode": os.environ["MODE"],
    "model": os.environ["MODEL"],
    "argv": argv,
}
env = {}
if os.environ.get("CURSOR_CONFIG_DIR"):
    env["CURSOR_CONFIG_DIR"] = os.environ["CURSOR_CONFIG_DIR"]
# What researcher mode hands the backend through its environment.
for name in ("PORCH_RESEARCHER_PROFILES", "CLAUDE_CODE_SUBAGENT_MODEL",
             "CLAUDE_CODE_SUBAGENT_MODEL_FORCE", "CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH"):
    if name in os.environ:
        env[name] = os.environ[name]
if os.environ.get("BACKEND") == "opencode" and os.environ.get("OPENCODE_CONFIG_CONTENT"):
    env["OPENCODE_CONFIG_CONTENT"] = os.environ["OPENCODE_CONFIG_CONTENT"]
if env:
    obj["env"] = env
with open(os.environ["DUMP_ARGV_PATH"], "w") as f:
    json.dump(obj, f)
    f.write("\n")
'
    [[ -z "$RUNTIME_SETTINGS_FILE" ]] || rm -f "$RUNTIME_SETTINGS_FILE"
    exit $EXIT_OK
fi

RAW_STREAM="$TMP_DIR/raw.stream"
NORM_STREAM="$TMP_DIR/normalized.jsonl"
FINAL_TEXT="$TMP_DIR/final.txt"
LAST_MSG="$TMP_DIR/last-message.txt"
PROMPT_PATH="$TMP_DIR/prompt.txt"
printf '%s' "$FULL_PROMPT" > "$PROMPT_PATH"
# Free the shell variable and ensure it cannot appear in child environments.
unset FULL_PROMPT
# PROMPT may also be large (stdin/file path); keep for diagnostics only when small.
# Always clear export attribute on PROMPT too if a parent exported it.
if [[ ${#PROMPT} -gt 8192 ]]; then
    unset PROMPT
fi
BACKEND_ERR="$TMP_DIR/stderr.txt"
: > "$RAW_STREAM"
: > "$FINAL_TEXT"

redact_backend_stderr() {
    [[ -s "$BACKEND_ERR" ]] || return 0
    local redacted="$TMP_DIR/stderr.redacted.txt"
    if python3 "$LIB_DIR/redact_stream.py" \
        --input "$BACKEND_ERR" --output "$redacted"; then
        mv "$redacted" "$BACKEND_ERR"
    else
        # Fail closed for display: never print an unredacted backend diagnostic
        # merely because the redactor itself failed.
        printf '%s\n' '[backend stderr redaction failed; original diagnostic suppressed]' \
            > "$BACKEND_ERR"
    fi
}

# Unbuffered Python so progress/raw flushes reach the parent before backend exit.
export PYTHONUNBUFFERED=1

# Run a backend with concurrent line streaming through normalize_stream.py.
# The second argument is an optional stdin file. Remaining args are the full
# command argv (including binary). Large prompts never enter process argv.
# Sets: BACKEND_RC, NORM_RC (and writes RAW_STREAM, NORM_STREAM, FINAL_TEXT).
run_streamed() {
    local backend="$1"
    local stdin_file="$2"
    shift 2
    local -a run_argv=("$@")
    if [[ "$backend" == "opencode" ]]; then
        run_argv=(python3 "$LIB_DIR/opencode_run.py" "$TMP_DIR" "$ARTIFACT_KEY" "${run_argv[@]}")
    fi
    local -a norm_argv=(
        python3 "$LIB_DIR/normalize_stream.py"
        --backend "$backend"
        --agent-id "$AGENT_ID"
        --model "$MODEL"
        --effort "$EFFORT"
        --access-policy "$ACCESS_POLICY"
        --input -
        --raw-out "$RAW_STREAM"
        --extract-text --text-out "$FINAL_TEXT"
        --progress
        --progress-id "$PROGRESS_ID"
    )
    # Callers may choose full previews, content-free compact counters, or silence.
    if [[ -n "${PORCH_PROGRESS_STYLE:-}" ]]; then
        norm_argv+=(--progress-style "$PORCH_PROGRESS_STYLE")
    fi
    if [[ -n "${PORCH_PROGRESS_INTERVAL:-}" ]]; then
        norm_argv+=(--progress-interval "$PORCH_PROGRESS_INTERVAL")
    fi
    if [[ -n "${PORCH_DEBUG_EVENTS:-}" ]]; then
        if [[ -n "${PORCH_DEBUG_EVENTS_PATH:-}" ]]; then
            norm_argv+=(--debug-events "$PORCH_DEBUG_EVENTS_PATH")
        else
            norm_argv+=(--debug-events)
        fi
    fi
    if [[ "$backend" != "grok-build" && "$backend" != "antigravity-cli" ]]; then
        norm_argv+=(--no-validate)
    fi

    set +e
    # Disable pipefail for this pipeline so a mid-stream normalizer failure
    # does not hide the backend exit; we read PIPESTATUS explicitly.
    # Capture PIPESTATUS *immediately* after the pipeline — before any other
    # command (including `fi`) overwrites it.
    set +o pipefail
    local -a _ps
    if [[ -n "$stdin_file" ]]; then
        "${run_argv[@]}" <"$stdin_file" 2>"$BACKEND_ERR" \
            | "${norm_argv[@]}" >"$NORM_STREAM"
        _ps=("${PIPESTATUS[@]}")
    else
        # File-based transports have no stdin payload. Do not pass a live
        # caller pipe to a backend that already received its prompt file.
        "${run_argv[@]}" </dev/null 2>"$BACKEND_ERR" \
            | "${norm_argv[@]}" >"$NORM_STREAM"
        _ps=("${PIPESTATUS[@]}")
    fi
    BACKEND_RC=${_ps[0]:-1}
    NORM_RC=${_ps[1]:-0}
    set -o pipefail
    set -e

    # Preserve backend PIPESTATUS when the normalizer also fails. Prefer the
    # backend non-zero (signal/crash) over a normalizer-only failure so
    # callers see the true process outcome. Grok contract additionally requires
    # end present / no error: if backend exited 0 but normalizer failed, surface 1.
    if [[ "${NORM_RC:-0}" -ne 0 && "${BACKEND_RC:-0}" -eq 0 ]]; then
        if [[ "$backend" == "grok-build" || "$backend" == "antigravity-cli" ]]; then
            BACKEND_RC=1
        fi
        # Other backends: normalizer failure alone does not rewrite a clean
        # backend exit unless Grok validation applies; PIPESTATUS[0] stays 0.
    fi
    # When both fail, keep BACKEND_RC as-is (already captured from PIPESTATUS[0]).
}

run_and_capture() {
    local exit_code=0
    BACKEND_RC=0
    NORM_RC=0

    case "$BACKEND" in
        codex-cli)
            run_streamed "codex-cli" "$PROMPT_PATH" "${CMD[@]}" -o "$LAST_MSG" -
            exit_code=$BACKEND_RC
            # Codex authoritative final message is -o last-message when present
            if [[ -s "$LAST_MSG" ]]; then
                cat "$LAST_MSG" > "$FINAL_TEXT"
            elif [[ ! -s "$FINAL_TEXT" && -s "$RAW_STREAM" ]]; then
                python3 "$LIB_DIR/normalize_stream.py" \
                    --backend codex-cli --agent-id "$AGENT_ID" \
                    --input "$RAW_STREAM" --extract-text --text-out "$FINAL_TEXT" \
                    --no-validate >/dev/null 2>/dev/null || true
            fi
            ;;
        claude-code)
            run_streamed "claude-code" "$PROMPT_PATH" "${CMD[@]}"
            exit_code=$BACKEND_RC
            # Shared assembly: authoritative non-empty result XOR deltas.
            # Empty/whitespace result must not erase non-empty answer deltas.
            # is_error/error results never become a successful answer body.
            claude_is_error=0
            if [[ -s "$NORM_STREAM" ]]; then
                if python3 -c '
import json,sys
for line in open(sys.argv[1], encoding="utf-8"):
    line=line.strip()
    if not line: continue
    try: o=json.loads(line)
    except Exception: continue
    if o.get("type")=="run_failed":
        sys.exit(0)
sys.exit(1)
' "$NORM_STREAM" 2>/dev/null; then
                    claude_is_error=1
                fi
                python3 -c '
import sys
sys.path.insert(0, sys.argv[3])
from events import assemble_final_text, PorchEvent, EventValidationError
import json
evts=[]
saw_fail=False
for line in open(sys.argv[1], encoding="utf-8"):
    line=line.strip()
    if not line: continue
    try:
        o=json.loads(line)
        if o.get("type")=="run_failed":
            saw_fail=True
            continue
        evts.append(PorchEvent.from_dict(o))
    except Exception:
        continue
# Forensic final may keep deltas for debugging, but is_error path must not
# promote a partial answer as the successful body when caller checks RC.
text = assemble_final_text(evts)
if text:
    open(sys.argv[2],"w",encoding="utf-8").write(text)
' "$NORM_STREAM" "$FINAL_TEXT" "$LIB_DIR" 2>/dev/null || true
            elif [[ -s "$RAW_STREAM" ]]; then
                python3 -c '
import json,sys
deltas=[]
result=None
is_err=False
for line in open(sys.argv[1]):
    line=line.strip()
    if not line: continue
    try:
        o=json.loads(line)
    except Exception:
        continue
    if o.get("type") in ("result", "result_success"):
        if o.get("is_error") or o.get("error"):
            is_err=True
            continue
        r=o.get("result")
        if isinstance(r, str) and r.strip():
            result=r
    elif o.get("type")=="content_block_delta":
        d=o.get("delta") or {}
        if d.get("type")=="text_delta":
            deltas.append(d.get("text",""))
text = result if result is not None else "".join(deltas)
if text:
    open(sys.argv[2],"w").write(text)
if is_err:
    open(sys.argv[2]+".is_error","w").write("1")
' "$RAW_STREAM" "$FINAL_TEXT" 2>/dev/null || true
                if [[ -f "${FINAL_TEXT}.is_error" ]]; then
                    claude_is_error=1
                    rm -f "${FINAL_TEXT}.is_error"
                fi
            fi
            # result/result_success with is_error or error → non-zero even when
            # the CLI process exits 0 and partial deltas exist. Artifacts are
            # preserved; do not emit a successful partial answer.
            if [[ "$claude_is_error" -eq 1 && "$exit_code" -eq 0 ]]; then
                exit_code=1
            fi
            ;;
        opencode)
            run_streamed "opencode" "$PROMPT_PATH" "${CMD[@]}"
            exit_code=$BACKEND_RC
            ;;
        gemini-cli)
            # Backend identity is gemini-cli (not plain); plain-text behaviour retained.
            if command -v "$BIN" &>/dev/null || [[ -x "$BIN" ]]; then
                run_streamed "gemini-cli" "$PROMPT_PATH" "${CMD[@]}"
                exit_code=$BACKEND_RC
            else
                echo "gemini CLI missing" >"$BACKEND_ERR"
                exit_code=4
            fi
            if [[ ! -s "$FINAL_TEXT" && -s "$RAW_STREAM" ]]; then
                cat "$RAW_STREAM" > "$FINAL_TEXT"
            fi
            ;;
        grok-build)
            # --prompt-file is the large-prompt one-shot headless path (see grok --help)
            run_streamed "grok-build" "" "${CMD[@]}" --prompt-file "$PROMPT_PATH"
            exit_code=$BACKEND_RC
            ;;
        devin-cli)
            # The ACP helper emits the raw JSON-RPC stream on stdout for
            # normalization; --prompt-file keeps large prompts off argv.
            if command -v "$BIN" &>/dev/null || [[ -x "$BIN" ]]; then
                run_streamed "devin-cli" "" "${CMD[@]}" --prompt-file "$PROMPT_PATH"
                exit_code=$BACKEND_RC
            else
                echo "devin CLI missing" >"$BACKEND_ERR"
                exit_code=4
            fi
            # Raw stream is JSON-RPC, not answer text: fall back through the
            # normalizer so only agent_message_chunk text becomes final.
            if [[ ! -s "$FINAL_TEXT" && -s "$RAW_STREAM" ]]; then
                python3 "$LIB_DIR/normalize_stream.py" \
                    --backend devin-cli --agent-id "$AGENT_ID" \
                    --input "$RAW_STREAM" --extract-text --text-out "$FINAL_TEXT" \
                    --no-validate >/dev/null 2>/dev/null || true
            fi
            ;;
        antigravity-cli)
            if command -v "$BIN" &>/dev/null || [[ -x "$BIN" ]]; then
                # One stream-json user message on stdin; EOF ends the session
                # after this turn. JSON encoding keeps any prompt text intact.
                AGY_INPUT="$TMP_DIR/agy-input.jsonl"
                python3 -c '
import json, sys
text = open(sys.argv[1], encoding="utf-8").read()
with open(sys.argv[2], "w", encoding="utf-8") as f:
    f.write(json.dumps({"event": "user", "message": {"content": text}}) + "\n")
' "$PROMPT_PATH" "$AGY_INPUT"
                run_streamed "antigravity-cli" "$AGY_INPUT" "${CMD[@]}"
                exit_code=$BACKEND_RC
            else
                echo "agy CLI missing" >"$BACKEND_ERR"
                exit_code=4
            fi
            ;;
        cursor-cli)
            run_streamed "cursor-cli" "$PROMPT_PATH" "${CMD[@]}"
            exit_code=$BACKEND_RC
            # result.result is authoritative. is_error or a non-success subtype
            # fails the run even when cursor-agent exits 0. Do not scrape a
            # Claude-shaped content_block_delta fallback out of the raw stream.
            cursor_is_error=0
            if [[ -s "$NORM_STREAM" ]]; then
                if python3 -c '
import json,sys
for line in open(sys.argv[1], encoding="utf-8"):
    line=line.strip()
    if not line: continue
    try: o=json.loads(line)
    except Exception: continue
    if o.get("type")=="run_failed":
        sys.exit(0)
sys.exit(1)
' "$NORM_STREAM" 2>/dev/null; then
                    cursor_is_error=1
                fi
                python3 -c '
import sys
sys.path.insert(0, sys.argv[3])
from events import assemble_final_text, PorchEvent
import json
evts=[]
for line in open(sys.argv[1], encoding="utf-8"):
    line=line.strip()
    if not line: continue
    try:
        o=json.loads(line)
        if o.get("type")=="run_failed":
            continue
        evts.append(PorchEvent.from_dict(o))
    except Exception:
        continue
text = assemble_final_text(evts)
if text:
    open(sys.argv[2],"w",encoding="utf-8").write(text)
' "$NORM_STREAM" "$FINAL_TEXT" "$LIB_DIR" 2>/dev/null || true
            elif [[ -s "$RAW_STREAM" ]]; then
                python3 "$LIB_DIR/normalize_stream.py" \
                    --backend cursor-cli --agent-id "$AGENT_ID" \
                    --input "$RAW_STREAM" --extract-text --text-out "$FINAL_TEXT" \
                    --no-validate >/dev/null 2>/dev/null || true
                if python3 -c '
import json,sys
for line in open(sys.argv[1], encoding="utf-8"):
    line=line.strip()
    if not line: continue
    try: o=json.loads(line)
    except Exception: continue
    if o.get("type")!="result":
        continue
    subtype=o.get("subtype") or "success"
    if o.get("is_error") or subtype!="success":
        sys.exit(0)
sys.exit(1)
' "$RAW_STREAM" 2>/dev/null; then
                    cursor_is_error=1
                fi
            fi
            if [[ "$cursor_is_error" -eq 1 && "$exit_code" -eq 0 ]]; then
                exit_code=1
            fi
            ;;
    esac
    # Non-zero `return` under `set -e` aborts the whole script before the
    # caller can read RC / emit progress_agent_done / persist failure artifacts.
    set +e
    return "$exit_code"
}

progress_info "running" "agent=$PROGRESS_ID backend=$BACKEND"

set +e
run_and_capture
RC=$?
set -e

# Parent workflows tee and archive this diagnostic, so scrub before any display
# or handoff. The unredacted temp file is replaced in place.
redact_backend_stderr

# Persist artifacts (best-effort; never fail the run on copy issues)
if [[ -n "${ART_RAW:-}" ]]; then
    mkdir -p "$(dirname "$ART_RAW")" "$(dirname "$ART_NORM")" "$(dirname "$ART_FINAL")" 2>/dev/null || true
    [[ -f "$RAW_STREAM" ]] && cp "$RAW_STREAM" "$ART_RAW" 2>/dev/null || true
    [[ -f "$NORM_STREAM" ]] && cp "$NORM_STREAM" "$ART_NORM" 2>/dev/null || true
    artifacts_write_final "$ARTIFACT_KEY" "$FINAL_TEXT" 2>/dev/null || true
fi

if [[ $RC -ne 0 ]]; then
    progress_agent_done "$PROGRESS_ID" "failed" "$RC"
    if [[ -s "$BACKEND_ERR" ]]; then
        echo "[$LABEL] backend stderr:" >&2
        cat "$BACKEND_ERR" >&2
    fi
    exit $RC
fi

if [[ ! -s "$FINAL_TEXT" ]]; then
    progress_agent_done "$PROGRESS_ID" "empty" 66
    echo "[$LABEL] empty response" >&2
    exit 66
fi

progress_agent_done "$PROGRESS_ID" "ok" 0
# Final answer ONLY on stdout
cat "$FINAL_TEXT"
exit 0
