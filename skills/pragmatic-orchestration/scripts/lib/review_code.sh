#!/bin/bash
# Build a code review request for one explicitly selected review profile.
set -euo pipefail
LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$LIB_DIR/common.sh"
if [[ -n "${PORCH_REVIEW_MODE:-}" ]]; then
    echo 'Error: PORCH_REVIEW_MODE was removed; unset it and select one profile with -a ID' >&2
    exit "$EXIT_USAGE"
fi
agent=""; input=""; diff=false
args=(); related=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        -a|--agent|--agents)
            [[ $# -ge 2 && -z "$agent" && -n "$2" ]] || { echo 'Error: select exactly one profile with -a ID' >&2; exit "$EXIT_USAGE"; }
            agent="$2"; shift 2 ;;
        --related)
            [[ $# -ge 2 && -f "$2" ]] || { echo 'Error: --related requires an existing file' >&2; exit "$EXIT_USAGE"; }
            related+=("$2"); shift 2 ;;
        --related=*)
            path="${1#--related=}"; [[ -f "$path" ]] || { echo "Error: related file not found: $path" >&2; exit "$EXIT_USAGE"; }
            related+=("$path"); shift ;;
        --diff) diff=true; shift ;;
        --xml) args+=("$1"); shift ;;
        --progress) [[ $# -ge 2 ]] || { echo 'Error: --progress requires a value' >&2; exit "$EXIT_USAGE"; }; args+=("$1" "$2"); shift 2 ;;
        --progress=*) args+=("$1"); shift ;;
        --depth|--depth=*|--mode|--mode=*|--judge|--judge=*|--no-fallback|--dry-run|-x|--exclude)
            echo 'Error: multi-review options were removed; use review code -a ID FILE or --diff' >&2; exit "$EXIT_USAGE" ;;
        -h|--help) echo 'Usage: porch review code -a ID [--related FILE] [--xml] [--progress STYLE] <FILE|--diff>'; exit 0 ;;
        --) shift; [[ $# -eq 1 && -z "$input" ]] || { echo 'Error: expected one input file' >&2; exit "$EXIT_USAGE"; }; input="$1"; shift ;;
        -*) echo "Error: unknown option: $1" >&2; exit "$EXIT_USAGE" ;;
        *) [[ -z "$input" ]] || { echo 'Error: expected one input file' >&2; exit "$EXIT_USAGE"; }; input="$1"; shift ;;
    esac
done
[[ -n "$agent" && "$agent" != *','* && "$agent" != *'*'* && "$agent" != *'?'* && "$agent" != *'['* ]] || { echo 'Error: select exactly one exact profile ID with -a ID' >&2; exit "$EXIT_USAGE"; }
if $diff; then
    [[ -z "$input" && ! -t 0 ]] || { echo 'Error: --diff requires piped input and no file' >&2; exit "$EXIT_USAGE"; }
else
    [[ -n "$input" && -f "$input" ]] || { echo "Error: input file not found: $input" >&2; exit "$EXIT_USAGE"; }
fi
prompt_file=$(mktemp)
input_file=$(mktemp)
trap 'rm -f "$prompt_file" "$input_file"' EXIT
if $diff; then cat > "$input_file"; else cat < "$input" > "$input_file"; fi
[[ -s "$input_file" ]] || { echo 'Error: review input is empty' >&2; exit "$EXIT_USAGE"; }
{
    printf '%s\n' 'Review the following code or diff for actionable defects, including correctness and security. Provide file and line references, evidence, impact, and a concrete fix. Investigate the repository independently; the following paths are an incomplete navigation seed, not a scope boundary.'
    if ! $diff; then printf '\nPrimary file: %s\n' "$input"; fi
    for path in "${related[@]:-}"; do [[ -z "$path" ]] || printf 'Related file: %s\n' "$path"; done
    printf '\n<review-input>\n'
    cat "$input_file"
    printf '\n</review-input>\n'
} > "$prompt_file"
bash "$LIB_DIR/review_ask.sh" -a "$agent" ${args[@]+"${args[@]}"} --prompt-file "$prompt_file"
