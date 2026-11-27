#!/bin/bash
# Independent review with exactly one explicitly selected profile.
set -euo pipefail
LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && { pwd -W 2>/dev/null || pwd; })"
source "$LIB_DIR/common.sh"
source "$LIB_DIR/config.sh"
source "$LIB_DIR/progress.sh"
source "$LIB_DIR/artifacts.sh"
unset PORCH_RAW_PROMPT 2>/dev/null || true
format=markdown; agent=""; prompt=""; prompt_file=""; source_kind=""; context_stdin=false; list=false
# --researcher ID (repeatable): the worker may start read-only researchers on
# these profiles. --assignment: the brief sets the role and the answer structure.
researchers=""; style=adviser
add_researcher() {
    [[ -n "$1" && "$1" != -* && "$1" != *','* && "$1" != *'*'* && "$1" != *'?'* && "$1" != *'['* ]] || { echo 'Error: --researcher requires one exact profile ID; repeat the flag for several' >&2; exit "$EXIT_USAGE"; }
    researchers="${researchers:+$researchers,}$1"
}
progress="${PORCH_PROGRESS:-full}"
while [[ $# -gt 0 ]]; do
    case "$1" in
        --xml) format=xml; shift ;;
        --progress) [[ $# -ge 2 ]] || { echo 'Error: --progress requires a value' >&2; exit "$EXIT_USAGE"; }; progress="$2"; shift 2 ;;
        --progress=*) progress="${1#--progress=}"; shift ;;
        --list-agents) list=true; shift ;;
        --prompt-file) [[ $# -ge 2 && -n "$2" && -z "$source_kind" ]] || { echo 'Error: --prompt-file requires one path and cannot be combined with a question' >&2; exit "$EXIT_USAGE"; }; prompt_file="$2"; source_kind=file; shift 2 ;;
        --context-stdin) context_stdin=true; shift ;;
        --researcher) [[ $# -ge 2 ]] || { echo 'Error: --researcher requires a profile ID' >&2; exit "$EXIT_USAGE"; }; add_researcher "$2"; shift 2 ;;
        --researcher=*) add_researcher "${1#--researcher=}"; shift ;;
        --assignment) style=assignment; shift ;;
        -a|--agent|--agents) [[ $# -ge 2 && -n "$2" && -z "$agent" ]] || { echo 'Error: select exactly one profile with -a ID' >&2; exit "$EXIT_USAGE"; }; agent="$2"; shift 2 ;;
        -x|--exclude) echo 'Error: --exclude was removed; select exactly one profile with -a ID' >&2; exit "$EXIT_USAGE" ;;
        -h|--help) cat <<'HELP'
Usage: porch review ask -a ID [--xml] [--progress full|compact|none]
                        [--researcher ID ...] [--assignment]
                        [--prompt-file FILE | QUESTION] [--context-stdin]

One read-only request to exactly one profile. By default the worker is an
independent adviser that works alone and answers under five headings.

  --researcher ID   (repeatable) The worker may start read-only researchers on
                    these profiles, and nothing else. A profile of the worker's
                    own backend gives the model for its built-in read-only
                    subagent; where the worker's shell is not sandboxed, every
                    named profile can also be asked through a nested
                    `porch review ask -a ID`. Supported backends: Codex CLI,
                    Claude Code, OpenCode, Grok Build, Antigravity CLI. A
                    backend that cannot do it, or cannot reach any named
                    profile, is refused with exit 4.
  --assignment      The prompt is a read-only assignment, not a question to an
                    adviser: no adviser role, no profile role text, no five
                    headings. The worker answers in the structure of its brief.

Exit: 0 success, 3 backend failure or empty response, 4 configuration error or
unsupported request, 5 usage error. Details: references/review.md
HELP
            exit 0 ;;
        --) shift; [[ $# -eq 1 && -z "$source_kind" ]] || { echo 'Error: expected one question' >&2; exit "$EXIT_USAGE"; }; prompt="$1"; source_kind=positional; shift ;;
        -*) echo "Error: unknown option: $1" >&2; exit "$EXIT_USAGE" ;;
        *) [[ -z "$source_kind" ]] || { echo 'Error: cannot combine --prompt-file with a positional prompt' >&2; exit "$EXIT_USAGE"; }; prompt="$1"; source_kind=positional; shift ;;
    esac
done
progress_set_style "$progress" || { echo "Error: --progress must be full, compact, or none (got: $progress)" >&2; exit "$EXIT_USAGE"; }
config_validate || exit "$EXIT_CONFIG_ERROR"
if $list; then config_xml_plan; exit "$EXIT_OK"; fi
[[ -n "$agent" && "$agent" != *','* && "$agent" != *'*'* && "$agent" != *'?'* && "$agent" != *'['* ]] || { echo 'Error: select exactly one exact profile ID with -a ID; use porch --list-agents to inspect profiles' >&2; exit "$EXIT_USAGE"; }
if [[ -n "${PORCH_RESEARCHER_PROFILES+x}" ]]; then
    # Started from a read-only review worker's shell: this run is one of its
    # researchers. It asks only a profile the worker was given, starts no
    # researchers of its own, and keeps its own run folder (the worker's shell
    # still carries the parent's).
    [[ -z "$researchers" ]] || { echo 'Error: a researcher cannot start researchers of its own: --researcher is not available in a review worker shell' >&2; exit "$EXIT_CONFIG_ERROR"; }
    case ",$PORCH_RESEARCHER_PROFILES," in
        *",$agent,"*) ;;
        *) echo "Error: profile '$agent' is not a researcher profile of this review worker (allowed: ${PORCH_RESEARCHER_PROFILES:-none})" >&2; exit "$EXIT_CONFIG_ERROR" ;;
    esac
    unset PORCH_RUN_DIR PORCH_SKIP_OUTPUT_TEMPLATE 2>/dev/null || true
fi
if [[ "$source_kind" == file ]]; then
    [[ -f "$prompt_file" ]] || { echo "Error: prompt file not found: $prompt_file" >&2; exit "$EXIT_USAGE"; }
    prompt="$(cat < "$prompt_file")"
fi
if $context_stdin; then
    [[ -n "$source_kind" && "$prompt" =~ [^[:space:]] && ! -t 0 ]] || { echo 'Error: --context-stdin requires a non-empty explicit prompt and piped context' >&2; exit "$EXIT_USAGE"; }
    context=$(cat)
    [[ "$context" =~ [^[:space:]] ]] || { echo 'Error: --context-stdin requires non-empty context' >&2; exit "$EXIT_USAGE"; }
    prompt="${prompt}"$'\n\n--- Input ---\n'"${context}"
elif [[ -z "$source_kind" && ! -t 0 ]]; then
    prompt=$(cat)
fi
[[ "$prompt" =~ [^[:space:]] ]] || { echo 'Error: No prompt provided; pass it as a positional argument, pipe it to stdin, or use --prompt-file FILE' >&2; exit "$EXIT_USAGE"; }
found=false
while IFS= read -r candidate; do [[ "$candidate" != "$agent" ]] || found=true; done < <(config_all_agents)
$found || { echo "Error: unknown profile '$agent'; use porch --list-agents" >&2; exit "$EXIT_CONFIG_ERROR"; }
run_args=(--mode review)
if [[ -n "$researchers" ]]; then
    # Refuse before anything starts when this backend cannot give the worker
    # read-only researchers on these profiles (lib/researchers.py).
    python3 "$LIB_DIR/researchers.py" plan "$agent" --researcher "$researchers" >/dev/null || exit "$EXIT_CONFIG_ERROR"
    run_args=(--mode review-researchers --researchers "$researchers")
fi
[[ "$style" == adviser ]] || run_args+=(--style "$style")
[[ "$source_kind" != positional ]] || warn_shell_special_in_prompt "$prompt"
export PORCH_MODE=review-ask PORCH_SUPPRESS_SHELL_WARN=1 PORCH_SINGLE_AGENT=1
unset PORCH_ARTIFACT_KEY 2>/dev/null || true
artifacts_init_run ask
out=$(mktemp); err=$(mktemp); request=$(mktemp); report=$(mktemp)
trap 'rm -f "$out" "$err" "$request" "$report"' EXIT
printf '%s' "$prompt" > "$request"
resolved=$(python3 "$LIB_DIR/backend_contract.py" resolve "$agent" --mode review) || exit "$EXIT_CONFIG_ERROR"
eval "$(RESOLVED_JSON="$resolved" python3 - <<'PY'
import json, os, shlex
for key, value in json.loads(os.environ['RESOLVED_JSON']).items():
    if key in ('backend', 'label', 'model', 'effort', 'role'):
        print(f'{key.upper()}={shlex.quote(str(value or ""))}')
PY
)"
progress_stage ask "agent=$agent"
code=0
set +e
set +o pipefail
"$LIB_DIR/backend_run.sh" "${run_args[@]}" --agent-id "$agent" < "$request" 2>&1 1>"$out" | tee "$err" >&2
pipeline=("${PIPESTATUS[@]}")
set -o pipefail
set -e
code="${pipeline[0]}"
if [[ "$code" -eq 0 && ! -s "$out" ]]; then code=66; fi
status=ok; [[ "$code" -eq 0 ]] || status=failed
if [[ "$format" == xml ]]; then
    {
        printf '<porch-report prompt-length="%s">\n' "${#prompt}"
        printf '  <agent id="%s" label="%s" backend="%s" model="%s" effort="%s" role="%s" status="%s" exit-code="%s">\n' "$(printf '%s' "$agent" | xml_escape)" "$(printf '%s' "${LABEL:-$agent}" | xml_escape)" "$(printf '%s' "${BACKEND:-}" | xml_escape)" "$(printf '%s' "${MODEL:-}" | xml_escape)" "$(printf '%s' "${EFFORT:-}" | xml_escape)" "$(printf '%s' "${ROLE:-analyst}" | xml_escape)" "$status" "$code"
        if [[ "$status" == ok ]]; then printf '    <response>'; cat "$out" | cdata_wrap; printf '</response>\n'; else printf '    <error>'; cat "$err" | cdata_wrap; printf '</error>\n'; fi
        printf '  </agent>\n</porch-report>\n'
    } > "$report"
else
    { printf '## %s Response (%s' "${LABEL:-$agent}" "${MODEL:-}"; [[ -z "${EFFORT:-}" ]] || printf ', effort=%s' "$EFFORT"; printf ')\n\n'; if [[ "$status" == ok ]]; then cat "$out"; else printf 'Review failed with exit code %s.\n' "$code"; fi; } > "$report"
fi
artifacts_set_primary_final "$report"
cat "$report"
[[ "$status" == ok ]] || exit "$EXIT_ALL_FAILED"
