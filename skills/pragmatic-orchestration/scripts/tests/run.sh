#!/bin/bash
# pragmatic-orchestration v5 test suite (fake backends only — no network).
set -euo pipefail

TESTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$TESTS_DIR/.." && pwd)"
SKILL_DIR="$(cd "$SCRIPTS_DIR/.." && pwd)"
LIB_DIR="$SCRIPTS_DIR/lib"
FAKES="$TESTS_DIR/fakes"
FIX="$TESTS_DIR/fixtures"
PORCH="$SCRIPTS_DIR/porch"

chmod +x "$FAKES"/* "$PORCH" "$LIB_DIR"/*.sh "$LIB_DIR/normalize_stream.py" 2>/dev/null || true

export PORCH_CONFIG="$FIX/test-config.json"
export PORCH_BIN_CODEX="$FAKES/fake-codex"
export PORCH_BIN_CLAUDE="$FAKES/fake-claude"
export PORCH_BIN_OPENCODE="$FAKES/fake-opencode"
export PORCH_BIN_GROK="$FAKES/fake-grok"
export PORCH_BIN_GEMINI="$FAKES/fake-gemini"
export PORCH_BIN_DEVIN="$FAKES/fake-devin"
export PORCH_BIN_AGY="$FAKES/fake-agy"
export PORCH_SUPPRESS_SHELL_WARN=1
# Parent shells (prior delegate runs, harnesses) must not leak mode wrappers
# into the offline suite.
unset PORCH_RAW_PROMPT PORCH_SKIP_OUTPUT_TEMPLATE PORCH_MODE \
  PORCH_SINGLE_AGENT PORCH_ARTIFACT_KEY 2>/dev/null || true
# Per-backend overrides from the caller (Claude Code exports CLAUDE_EFFORT, for
# example) would replace the fixture's model and effort.
unset AGY_MODEL AGY_EFFORT CLAUDE_MODEL CLAUDE_EFFORT CODEX_MODEL CODEX_EFFORT DEVIN_MODEL \
  GEMINI_MODEL GROK_MODEL GROK_EFFORT OPENCODE_MODEL OPENCODE_EFFORT 2>/dev/null || true

PASS=0
FAIL=0
assert_eq() {
  local name="$1" got="$2" want="$3"
  if [[ "$got" == "$want" ]]; then
    echo "  PASS  $name"
    PASS=$((PASS+1))
  else
    echo "  FAIL  $name"
    echo "        got:  $got"
    echo "        want: $want"
    FAIL=$((FAIL+1))
  fi
}
assert_contains() {
  local name="$1" hay="$2" needle="$3"
  if [[ "$hay" == *"$needle"* ]]; then
    echo "  PASS  $name"
    PASS=$((PASS+1))
  else
    echo "  FAIL  $name (missing: $needle)"
    echo "        haystack: $hay"
    FAIL=$((FAIL+1))
  fi
}
assert_not_contains() {
  local name="$1" hay="$2" needle="$3"
  if [[ "$hay" != *"$needle"* ]]; then
    echo "  PASS  $name"
    PASS=$((PASS+1))
  else
    echo "  FAIL  $name (unexpected: $needle)"
    FAIL=$((FAIL+1))
  fi
}
assert_file() {
  local name="$1" path="$2"
  if [[ -f "$path" && -s "$path" ]]; then
    echo "  PASS  $name"
    PASS=$((PASS+1))
  else
    echo "  FAIL  $name (missing/empty: $path)"
    FAIL=$((FAIL+1))
  fi
}
assert_le() {
  local name="$1" got="$2" limit="$3"
  if (( got <= limit )); then
    echo "  PASS  $name"
    PASS=$((PASS+1))
  else
    echo "  FAIL  $name (got $got, expected <= $limit)"
    FAIL=$((FAIL+1))
  fi
}

echo "=== Agent-facing entrypoint documentation ==="
if grep -R -n --include='*.md' 'scripts/porch' \
    "$SKILL_DIR/SKILL.md" "$SKILL_DIR/references" >/dev/null; then
  echo "  FAIL  agent-facing docs contain cwd-dependent scripts/porch"
  FAIL=$((FAIL+1))
else
  echo "  PASS  agent-facing docs use the resolved PORCH entrypoint"
  PASS=$((PASS+1))
fi
assert_contains "skill documents entrypoint verification" \
  "$(cat "$SKILL_DIR/SKILL.md")" 'test -x "$PORCH"'

echo "=== CLI dispatch ==="
out=$("$PORCH" --help 2>&1) || true
assert_contains "help mentions review" "$out" "review"
assert_contains "help mentions delegate" "$out" "delegate"
assert_contains "help mentions quota" "$out" "quota"
assert_not_contains "help omits removed explore mode" "$out" "porch explore"

set +e
removed_explore_err=$("$PORCH" explore "q" 2>&1 >/dev/null)
rc=$?
set -e
assert_eq "removed explore command exits with usage error" "$rc" "5"
assert_contains "removed explore command names supported modes" \
  "$removed_explore_err" "expected review|delegate|quota"
assert_contains "unknown command lists media mode" "$removed_explore_err" "|media)"

out=$("$PORCH" --list-agents 2>/dev/null)
assert_contains "list-agents has grok" "$out" 'id="grok"'
assert_contains "list-agents has grok-build backend" "$out" 'backend="grok-build"'
assert_contains "default grok profile uses Grok 4.7" "$out" \
  'id="grok" label="Grok Test" backend="grok-build" model="grok-4.7" role="analyst" enabled="true"'
assert_contains "Grok 4.7 Fast is selectable in the fixture" "$out" \
  'id="grok-fast" label="Grok Fast Test" backend="grok-build" model="grok-4.7-build-fast" role="analyst" enabled="false"'

out=$(env PORCH_CONFIG="$SKILL_DIR/config.example.json" "$PORCH" --list-agents 2>/dev/null)
assert_contains "production config includes grok" "$out" 'id="grok"'
assert_contains "skill-root default grok profile uses Grok 4.7" "$out" \
  'id="grok" label="Grok 4.7 (native)" backend="grok-build" model="grok-4.7" role="analyst" enabled="true"'
assert_contains "Grok 4.7 Fast is opt-in" "$out" \
  'id="grok-fast" label="Grok 4.7 Fast (native)" backend="grok-build" model="grok-4.7-build-fast" role="analyst" enabled="false"'
assert_contains "Grok 4.5 remains selectable" "$out" \
  'id="grok-4.5" label="Grok 4.5 (native)" backend="grok-build" model="grok-4.5" role="analyst" enabled="false"'
assert_contains "Codex Astra remains selectable but disabled by default" "$out" \
  'id="codex-gpt-6-astra" label="Codex GPT-6 Astra" backend="codex-cli" model="gpt-6-astra" role="analyst" enabled="false"'
assert_contains "Codex Sol is enabled by default" "$out" \
  'id="codex" label="Codex GPT-6.1 Sol" backend="codex-cli" model="gpt-6.1-sol" role="analyst" enabled="true"'
assert_contains "Codex GPT-6.1 Sol is explicitly selectable without duplicating the default pool" "$out" \
  'id="codex-gpt-6.1-sol" label="Codex GPT-6.1 Sol" backend="codex-cli" model="gpt-6.1-sol" role="analyst" enabled="false"'
assert_contains "Codex GPT-6.1 Terra is selectable but disabled by default" "$out" \
  'id="codex-gpt-6.1-terra" label="Codex GPT-6.1 Terra" backend="codex-cli" model="gpt-6.1-terra" role="analyst" enabled="false"'
assert_contains "Devin Gemini 4 Argon is selectable but disabled by default" "$out" \
  'id="devin-gemini-4-argon" label="Devin Gemini 4 Argon High" backend="devin-cli" model="gemini-4-argon-high" role="lateral" enabled="false"'
assert_contains "Antigravity Gemini 4 Argon is selectable but disabled by default" "$out" \
  'id="agy-gemini-4-argon" label="Antigravity Gemini 4 Argon High" backend="antigravity-cli" model="gemini-4-argon" role="lateral" enabled="false"'
assert_contains "Antigravity Gemini 4 Carbon is opt-in for accounts with access" "$out" \
  'id="agy-gemini-4-carbon" label="Antigravity Gemini 4 Carbon High (limited access)" backend="antigravity-cli" model="gemini-4-carbon" role="lateral" enabled="false"'
assert_contains "Antigravity Gemini 3.8 Flash is selectable but disabled by default" "$out" \
  'id="agy-gemini-3.8-flash" label="Antigravity Gemini 3.8 Flash High" backend="antigravity-cli" model="gemini-3.8-flash" role="lateral" enabled="false"'
assert_contains "Codex Luna is enabled by default" "$out" \
  'id="codex-gpt-6-luna" label="Codex GPT-6 Luna" backend="codex-cli" model="gpt-6-luna" role="analyst" enabled="true"'
assert_contains "Claude Fable remains selectable but disabled by default" "$out" \
  'id="claude-fable" label="Claude Fable" backend="claude-code" model="fable" role="analyst" enabled="false"'
assert_contains "Claude Haiku is selectable but disabled by default" "$out" \
  'id="claude-haiku" label="Claude Haiku" backend="claude-code" model="haiku" role="lateral" enabled="false"'
assert_contains "Gemini 4 Argon Contributor is selectable but disabled by default" "$out" \
  'id="opencode-go-gemini-4-argon-contributor" label="OC-Go Gemini 4 Argon Contributor" backend="opencode" model="opencode-go/gemini-4-argon-contributor" role="lateral" enabled="false"'
assert_contains "Muse Spark 1.3 Contributor is selectable but disabled by default" "$out" \
  'id="opencode-go-muse-spark-1.3-contributor" label="OC-Go Muse Spark 1.3 Contributor" backend="opencode" model="opencode-go/muse-spark-1.3-contributor" role="lateral" enabled="false"'
assert_contains "DeepSeek V4.1 Flash is selectable but disabled by default" "$out" \
  'id="opencode-go-deepseek-v4.1-flash" label="OC-Go DeepSeek V4.1 Flash" backend="opencode" model="opencode-go/deepseek-v4.1-flash" role="analyst" enabled="false"'

# Unknown command
set +e
"$PORCH" foobar >/dev/null 2>&1
rc=$?
set -e
assert_eq "unknown command exit 5" "$rc" "5"

set +e
"$PORCH" quota unknown >/dev/null 2>&1
rc=$?
set -e
assert_eq "quota unknown provider exit 5" "$rc" "5"

echo "=== Argv safety (PORCH_DUMP_ARGV) ==="
dump_review() {
  local agent="$1" outfile="$2"
  PORCH_DUMP_ARGV="$outfile" \
    "$LIB_DIR/backend_run.sh" --mode review --agent-id "$agent" --raw "hello" >/dev/null
}
dump_delegate() {
  local agent="$1" outfile="$2"
  PORCH_DUMP_ARGV="$outfile" \
    "$LIB_DIR/backend_run.sh" --mode delegate --agent-id "$agent" --raw "hello" >/dev/null
}

TMP=$(mktemp -d)
export XDG_DATA_HOME="$TMP/xdg-data"
unset OPENCODE_DB OPENCODE_PASSWORD OPENCODE_SERVER_PASSWORD OPENCODE_CONFIG_CONTENT

# Production profiles must reach their native harnesses with the exact current
# model IDs, not merely appear correctly in --list-agents output.
env PORCH_CONFIG="$SKILL_DIR/config.example.json" PORCH_DUMP_ARGV="$TMP/astra-review.json" \
  "$LIB_DIR/backend_run.sh" --mode review --agent-id codex-gpt-6-astra --raw "hello" >/dev/null
argv=$(python3 -c 'import json; print(" ".join(json.load(open("'"$TMP/astra-review.json"'"))["argv"]))')
assert_contains "Codex production profile launches GPT-6 Astra" "$argv" "--model gpt-6-astra"
assert_contains "Codex Astra production profile uses high effort" "$argv" 'model_reasoning_effort="high"'

for spec in 'codex gpt-6.1-sol high' 'codex-gpt-6.1-sol gpt-6.1-sol high' 'codex-gpt-6.1-terra gpt-6.1-terra xhigh' 'codex-gpt-6-luna gpt-6-luna low' 'grok grok-4.7 high' 'grok-fast grok-4.7-build-fast high' 'grok-4.5 grok-4.5 high'; do
  read -r profile model effort <<< "$spec"
  for mode in review delegate; do
    env PORCH_CONFIG="$SKILL_DIR/config.example.json" PORCH_DUMP_ARGV="$TMP/current-model.json" \
      "$LIB_DIR/backend_run.sh" --mode "$mode" --agent-id "$profile" --raw "hello" >/dev/null
    argv=$(python3 -c 'import json,sys; print(" ".join(json.load(open(sys.argv[1]))["argv"]))' "$TMP/current-model.json")
    if [[ "$profile" == grok* ]]; then
      assert_contains "$profile $mode launches current model" "$argv" "-m $model"
      assert_contains "$profile $mode effort" "$argv" "--reasoning-effort $effort"
    else
      assert_contains "$profile $mode launches current model" "$argv" "--model $model"
      assert_contains "$profile $mode effort" "$argv" "model_reasoning_effort=\"$effort\""
    fi
  done
done

# Devin's reasoning level is encoded in the model id, including over ACP.
for mode in review delegate; do
  env PORCH_CONFIG="$SKILL_DIR/config.example.json" PORCH_DUMP_ARGV="$TMP/devin-argon-$mode.json" \
    "$LIB_DIR/backend_run.sh" --mode "$mode" --agent-id devin-gemini-4-argon --raw "hello" >/dev/null
  argv=$(python3 -c 'import json,sys; print(" ".join(json.load(open(sys.argv[1]))["argv"]))' "$TMP/devin-argon-$mode.json")
  assert_contains "Devin Gemini 4 Argon $mode uses high model id" "$argv" "--model gemini-4-argon-high"
done

for spec in claude-code:opus:medium claude-opus:opus:medium claude-fable:fable:high claude-sonnet:sonnet:high claude-haiku:haiku:medium; do
  IFS=: read -r profile model effort <<< "$spec"
  for mode in review delegate; do
    env PORCH_CONFIG="$SKILL_DIR/config.example.json" PORCH_DUMP_ARGV="$TMP/$profile-$mode.json" \
      "$LIB_DIR/backend_run.sh" --mode "$mode" --agent-id "$profile" --raw "hello" >/dev/null
    argv=$(python3 -c 'import json,sys; print(" ".join(json.load(open(sys.argv[1]))["argv"]))' "$TMP/$profile-$mode.json")
    assert_contains "$profile $mode uses model alias" "$argv" "--model $model"
    assert_contains "$profile $mode uses configured effort" "$argv" "--effort $effort"
  done
done

env PORCH_CONFIG="$SKILL_DIR/config.example.json" PORCH_DUMP_ARGV="$TMP/muse-spark-review.json" \
  "$LIB_DIR/backend_run.sh" --mode review --agent-id opencode-go-muse-spark-1.3-contributor --raw "hello" >/dev/null
argv=$(python3 -c 'import json; print(" ".join(json.load(open("'"$TMP/muse-spark-review.json"'"))["argv"]))')
assert_contains "Muse Spark production profile uses exact model" "$argv" "-m opencode-go/muse-spark-1.3-contributor"
assert_contains "Muse Spark production profile uses maximum effort" "$argv" "#xhigh"

env PORCH_CONFIG="$SKILL_DIR/config.example.json" PORCH_DUMP_ARGV="$TMP/deepseek-v41-flash-review.json" \
  "$LIB_DIR/backend_run.sh" --mode review --agent-id opencode-go-deepseek-v4.1-flash --raw "hello" >/dev/null
argv=$(python3 -c 'import json; print(" ".join(json.load(open("'"$TMP/deepseek-v41-flash-review.json"'"))["argv"]))')
assert_contains "DeepSeek V4.1 Flash production profile uses exact model" "$argv" "-m opencode-go/deepseek-v4.1-flash"
assert_contains "DeepSeek V4.1 Flash production profile uses maximum effort" "$argv" "#max"

for mode in review delegate; do
  env PORCH_CONFIG="$SKILL_DIR/config.example.json" PORCH_DUMP_ARGV="$TMP/argon-$mode.json" \
    "$LIB_DIR/backend_run.sh" --mode "$mode" --agent-id opencode-go-gemini-4-argon-contributor --raw "hello" >/dev/null
  argv=$(python3 -c 'import json,sys; print(" ".join(json.load(open(sys.argv[1]))["argv"]))' "$TMP/argon-$mode.json")
  assert_contains "Gemini 4 Argon $mode uses exact Go model" "$argv" "-m opencode-go/gemini-4-argon-contributor"
  assert_contains "Gemini 4 Argon $mode defaults to high effort" "$argv" "#high"
done

for mode in review delegate; do
  argon_log="$TMP/argon-$mode-runtime.jsonl"
  if [[ "$mode" == review ]]; then
    out=$(PORCH_FAKE_ARGV_LOG="$argon_log" PORCH_RUN_DIR="$TMP/argon-review-run" \
      "$PORCH" review ask --progress none -a opencode-go-gemini-4-argon-contributor "Review this")
  else
    out=$(PORCH_FAKE_ARGV_LOG="$argon_log" PORCH_RUN_DIR="$TMP/argon-delegate-run" \
      "$PORCH" delegate --one-shot -a opencode-go-gemini-4-argon-contributor "Implement this")
  fi
  assert_contains "Gemini 4 Argon $mode completes with fake OpenCode" "$out" "FAKE_OPENCODE_OK"
  argv=$(python3 -c 'import json,sys; print(" ".join(json.loads(open(sys.argv[1]).readline())["argv"]))' "$argon_log")
  assert_contains "Gemini 4 Argon $mode runtime uses Go catalog id" "$argv" "-m opencode-go/gemini-4-argon-contributor"
  assert_contains "Gemini 4 Argon $mode runtime uses high variant" "$argv" "#high"
done

# Exercise profile selection through public review and one-shot delegate paths.
for spec in 'devin-gemini-4-argon gemini-4-argon-high FAKE_DEVIN_OK' 'codex-gpt-6.1-terra gpt-6.1-terra FAKE_CODEX_OK'; do
  read -r profile model answer <<< "$spec"
  for mode in review delegate; do
    profile_log="$TMP/$profile-$mode-runtime.jsonl"
    if [[ "$mode" == review ]]; then
      out=$(PORCH_FAKE_ARGV_LOG="$profile_log" PORCH_RUN_DIR="$TMP/$profile-review-run" \
        "$PORCH" review ask --progress none -a "$profile" "Review this")
    else
      out=$(PORCH_FAKE_ARGV_LOG="$profile_log" PORCH_RUN_DIR="$TMP/$profile-delegate-run" \
        "$PORCH" delegate --one-shot -a "$profile" "Implement this")
    fi
    assert_contains "$profile $mode completes with fake backend" "$out" "$answer"
    argv=$(python3 -c 'import json,sys; print(" ".join(json.loads(open(sys.argv[1]).readline())["argv"]))' "$profile_log")
    assert_contains "$profile $mode runtime uses exact model" "$argv" "--model $model"
    if [[ "$profile" == codex-gpt-6.1-terra ]]; then
      assert_contains "$profile $mode defaults to xhigh effort" "$argv" 'model_reasoning_effort="xhigh"'
    fi
  done
done

# Codex review: sandbox read-only, never full-bypass
dump_review codex "$TMP/codex-review.json"
argv=$(python3 -c 'import json; print(" ".join(json.load(open("'"$TMP/codex-review.json"'"))["argv"]))')
assert_contains "codex review has read-only sandbox" "$argv" "--sandbox read-only"
assert_not_contains "codex review no full bypass" "$argv" "--dangerously-bypass-approvals-and-sandbox"
assert_contains "codex review disables multi-agent" "$argv" "--disable multi_agent"
assert_contains "codex review disables multi-agent v2" "$argv" "--disable multi_agent_v2"
assert_contains "codex review enables web search" "$argv" "--search"

# Codex delegate: YOLO bypass, no read-only sandbox
dump_delegate codex "$TMP/codex-del.json"
argv=$(python3 -c 'import json; print(" ".join(json.load(open("'"$TMP/codex-del.json"'"))["argv"]))')
assert_contains "codex delegate has YOLO bypass" "$argv" "--dangerously-bypass-approvals-and-sandbox"
assert_not_contains "codex delegate no read-only sandbox" "$argv" "--sandbox read-only"

# Per-invocation overrides must work in the ordinary shell runner, not only in
# the steerable Python config loader.
CODEX_MODEL="gpt-codex-override" CODEX_EFFORT="xhigh" \
  PORCH_DUMP_ARGV="$TMP/codex-overrides.json" \
  "$LIB_DIR/backend_run.sh" --mode delegate --agent-id codex --raw "hello" >/dev/null
argv=$(python3 -c 'import json; print(" ".join(json.load(open("'"$TMP/codex-overrides.json"'"))["argv"]))')
assert_contains "codex shell runner honors CODEX_MODEL" "$argv" "--model gpt-codex-override"
assert_contains "codex shell runner honors CODEX_EFFORT" "$argv" 'model_reasoning_effort="xhigh"'
assert_not_contains "codex effort override replaces config effort" "$argv" 'model_reasoning_effort="high"'

# Claude review: non-planning report workflow with full Bash.
dump_review claude-code "$TMP/claude-review.json"
argv=$(python3 -c 'import json; print(" ".join(json.load(open("'"$TMP/claude-review.json"'"))["argv"]))')
assert_contains "claude review uses non-planning dontAsk mode" "$argv" "--permission-mode dontAsk"
assert_not_contains "claude review does not activate plan workflow" "$argv" "--permission-mode plan"
assert_contains "claude review denies Edit" "$argv" "Edit"
assert_not_contains "claude review keeps Bash available" "$argv" "Edit,Write,NotebookEdit,Bash"
assert_contains "claude review preapproves Bash" "$argv" "Bash,WebSearch,WebFetch"
assert_contains "claude review disables subagents" "$argv" "Agent,Task"
assert_contains "claude review keeps web research" "$argv" "WebSearch,WebFetch"
assert_not_contains "claude review web approval is not a write approval" "$argv" "--allowedTools Edit"
assert_contains "claude review selects Opus 5.5" "$argv" "--model claude-opus-5-5"
assert_contains "claude review uses medium effort" "$argv" "--effort medium"
assert_not_contains "claude review no skip-permissions" "$argv" "--dangerously-skip-permissions"
assert_contains "claude review disables customizations" "$argv" "--safe-mode"
assert_contains "claude review disables session persistence" "$argv" "--no-session-persistence"
assert_contains "claude review disables Chrome integration" "$argv" "--no-chrome"

CLAUDE_MODEL="claude-override" CLAUDE_EFFORT="max" \
  PORCH_DUMP_ARGV="$TMP/claude-overrides.json" \
  "$LIB_DIR/backend_run.sh" --mode review --agent-id claude-code --raw "hello" >/dev/null
argv=$(python3 -c 'import json; print(" ".join(json.load(open("'"$TMP/claude-overrides.json"'"))["argv"]))')
assert_contains "claude shell runner honors CLAUDE_MODEL" "$argv" "--model claude-override"
assert_contains "claude shell runner honors CLAUDE_EFFORT" "$argv" "--effort max"

# Claude delegate: skip permissions, no plan
dump_delegate claude-code "$TMP/claude-del.json"
argv=$(python3 -c 'import json; print(" ".join(json.load(open("'"$TMP/claude-del.json"'"))["argv"]))')
assert_contains "claude delegate YOLO" "$argv" "--dangerously-skip-permissions"
assert_not_contains "claude delegate no dontAsk" "$argv" "--permission-mode dontAsk"

# OpenCode review: dedicated primary review agent, not the plan agent.
dump_review opencode "$TMP/oc-review.json"
argv=$(python3 -c 'import json; print(" ".join(json.load(open("'"$TMP/oc-review.json"'"))["argv"]))')
assert_contains "opencode review uses dedicated review agent" "$argv" "--agent porch-review"
assert_contains "opencode review auto-approves allowed diagnostics" "$argv" "--auto"
assert_not_contains "opencode review avoids plan agent" "$argv" "--agent plan"

# OpenCode delegate: build + auto
dump_delegate opencode "$TMP/oc-del.json"
argv=$(python3 -c 'import json; print(" ".join(json.load(open("'"$TMP/oc-del.json"'"))["argv"]))')
assert_contains "opencode delegate build" "$argv" "--agent build"
assert_contains "opencode delegate auto" "$argv" "--auto"
assert_not_contains "opencode delegate no plan" "$argv" "--agent plan"

# OpenCode Go Kimi K3: exact catalog model id and configured max reasoning.
dump_review opencode-go-kimi-k3 "$TMP/oc-go-kimi-k3-review.json"
argv=$(python3 -c 'import json; print(" ".join(json.load(open("'"$TMP/oc-go-kimi-k3-review.json"'"))["argv"]))')
assert_contains "OpenCode Go Kimi K3 exact model" "$argv" "-m opencode-go/kimi-k3"
assert_contains "OpenCode Go Kimi K3 uses max reasoning" "$argv" "#max"

# New OpenCode Go profiles: exact catalog model ids and supported reasoning variants.
dump_review opencode-go-muse-spark-1.3-contributor "$TMP/oc-go-muse-spark-review.json"
argv=$(python3 -c 'import json; print(" ".join(json.load(open("'"$TMP/oc-go-muse-spark-review.json"'"))["argv"]))')
assert_contains "OpenCode Go Muse Spark 1.3 Contributor exact model" "$argv" "-m opencode-go/muse-spark-1.3-contributor"
assert_contains "OpenCode Go Muse Spark 1.3 Contributor uses maximum reasoning" "$argv" "#xhigh"

dump_review opencode-go-deepseek-v4.1-flash "$TMP/oc-go-deepseek-v41-flash-review.json"
argv=$(python3 -c 'import json; print(" ".join(json.load(open("'"$TMP/oc-go-deepseek-v41-flash-review.json"'"))["argv"]))')
assert_contains "OpenCode Go DeepSeek V4.1 Flash exact model" "$argv" "-m opencode-go/deepseek-v4.1-flash"
assert_contains "OpenCode Go DeepSeek V4.1 Flash uses maximum reasoning" "$argv" "#max"

PORCH_FAKE_ARGV_LOG="$TMP/oc-go-kimi-k3-prompt.jsonl" \
  "$LIB_DIR/backend_run.sh" --mode review --agent-id opencode-go-kimi-k3 "review this" >/dev/null
kimi_prompt=$(python3 -c 'import json; print(json.loads(open("'"$TMP/oc-go-kimi-k3-prompt.jsonl"'").readline())["stdin"])')
assert_contains "OpenCode Go Kimi K3 adds adversarial review prompt" "$kimi_prompt" "KIMI_K3_ADVERSARIAL_REVIEW_MARKER"
oc_review_policy=$(python3 -c 'import json; print(json.loads(open("'"$TMP/oc-go-kimi-k3-prompt.jsonl"'").readline())["review_policy"])')
assert_contains "opencode review policy denies edits" "$oc_review_policy" "'edit': 'deny'"
assert_contains "opencode review policy denies delegation" "$oc_review_policy" "'subagent': 'deny'"
oc_review_agent_prompt=$(python3 -c 'import json; print(json.loads(open("'"$TMP/oc-go-kimi-k3-prompt.jsonl"'").readline())["review_agent_prompt"])')
assert_contains "opencode review agent prompt requires work alone" "$oc_review_agent_prompt" "Work alone"
assert_contains "opencode review agent prompt requires blast-radius search" "$oc_review_agent_prompt" "real blast radius"

OPENCODE_MODEL="provider/model-override" OPENCODE_EFFORT="thinking" \
  PORCH_DUMP_ARGV="$TMP/opencode-overrides.json" \
  "$LIB_DIR/backend_run.sh" --mode delegate --agent-id opencode --raw "hello" >/dev/null
argv=$(python3 -c 'import json; print(" ".join(json.load(open("'"$TMP/opencode-overrides.json"'"))["argv"]))')
assert_contains "opencode shell runner honors OPENCODE_MODEL" "$argv" "-m provider/model-override"
assert_contains "opencode shell runner honors OPENCODE_EFFORT" "$argv" "provider/model-override#thinking"

OPENCODE_EFFORT="none" PORCH_DUMP_ARGV="$TMP/opencode-none.json" \
  "$LIB_DIR/backend_run.sh" --mode delegate --agent-id opencode --raw "hello" >/dev/null
argv=$(python3 -c 'import json; print(" ".join(json.load(open("'"$TMP/opencode-none.json"'"))["argv"]))')
assert_not_contains "opencode shell runner treats none as no variant" "$argv" "#none"

# Grok review: sandbox read-only + tool allow/deny
dump_review grok "$TMP/grok-review.json"
argv=$(python3 -c 'import json; print(" ".join(json.load(open("'"$TMP/grok-review.json"'"))["argv"]))')
assert_contains "grok review sandbox read-only" "$argv" "--sandbox read-only"
assert_contains "grok review tools allowlist" "$argv" "--tools"
# The Grok allowlist is exhaustive: omitting the web tools silently removes web
# research from every review, which is exactly how it was lost before.
assert_contains "grok review keeps web research" "$argv" "web_search,web_fetch"
assert_contains "grok review keeps terminal diagnostics" "$argv" "run_terminal_cmd"
assert_contains "grok review explicitly disables plan mode" "$argv" "--no-plan"
assert_contains "grok review disables subagents" "$argv" "--no-subagents"
assert_contains "grok review disallowed tools" "$argv" "--disallowed-tools"
assert_contains "grok review streaming-json" "$argv" "streaming-json"
assert_contains "grok review prompt-file one-shot" "$argv" "--prompt-file"
assert_not_contains "grok review no always-approve" "$argv" "--always-approve"
# --prompt-file is the single-turn-from-file path; do not also require -p/--single
# (those take an inline prompt string and conflict with --prompt-file).

# Grok delegate: always-approve, no sandbox
dump_delegate grok "$TMP/grok-del.json"
argv=$(python3 -c 'import json; print(" ".join(json.load(open("'"$TMP/grok-del.json"'"))["argv"]))')
assert_contains "grok delegate always-approve" "$argv" "--always-approve"
assert_not_contains "grok delegate no sandbox" "$argv" "--sandbox"
assert_contains "grok delegate streaming-json" "$argv" "streaming-json"
assert_contains "grok delegate prompt-file one-shot" "$argv" "--prompt-file"

GROK_MODEL="grok-override" GROK_EFFORT="max" \
  PORCH_DUMP_ARGV="$TMP/grok-overrides.json" \
  "$LIB_DIR/backend_run.sh" --mode delegate --agent-id grok --raw "hello" >/dev/null
argv=$(python3 -c 'import json; print(" ".join(json.load(open("'"$TMP/grok-overrides.json"'"))["argv"]))')
assert_contains "grok shell runner honors GROK_MODEL" "$argv" "-m grok-override"
assert_contains "grok shell runner honors GROK_EFFORT" "$argv" "--reasoning-effort max"

GEMINI_MODEL="gemini-override" PORCH_DUMP_ARGV="$TMP/gemini-overrides.json" \
  "$LIB_DIR/backend_run.sh" --mode review --agent-id gemini-cli --raw "hello" >/dev/null
argv=$(python3 -c 'import json; print(" ".join(json.load(open("'"$TMP/gemini-overrides.json"'"))["argv"]))')
assert_contains "gemini shell runner honors GEMINI_MODEL" "$argv" "--model gemini-override"

# Gemini happy path: stdout answer, artifacts, backend label gemini-cli (not plain)
export PORCH_RUN_DIR="$TMP/run-gemini-ok"
export PORCH_SAVE_OUTPUTS=1
export PORCH_FAKE_GEMINI_MODE=ok
export PORCH_FAKE_ARGV_LOG="$TMP/gemini-runtime-argv.jsonl"
: > "$PORCH_FAKE_ARGV_LOG"
mkdir -p "$PORCH_RUN_DIR"
set +e
gem_out=$("$LIB_DIR/backend_run.sh" --mode review --agent-id gemini-cli --raw "gemini q" 2>"$TMP/gem-ok.err")
gem_rc=$?
set -e
assert_eq "gemini happy-path exit 0" "$gem_rc" "0"
assert_eq "gemini happy-path stdout" "$gem_out" "FAKE_GEMINI_OK"
gemini_runtime=$(tail -1 "$PORCH_FAKE_ARGV_LOG")
assert_contains "gemini review injects system settings" "$gemini_runtime" '"system_settings_path_set": true'
assert_contains "gemini review disables subagents" "$gemini_runtime" '"agents_enabled": false'
assert_file "gemini final artifact" "$PORCH_RUN_DIR/final/gemini-cli.txt"
assert_file "gemini raw artifact" "$PORCH_RUN_DIR/raw/gemini-cli.jsonl"
if [[ -f "$PORCH_RUN_DIR/normalized/gemini-cli.jsonl" ]]; then
  assert_contains "gemini normalized backend label" \
    "$(cat "$PORCH_RUN_DIR/normalized/gemini-cli.jsonl")" '"backend": "gemini-cli"'
else
  echo "  FAIL  gemini normalized artifact missing"
  FAIL=$((FAIL+1))
fi
# Gemini review uses the full non-planning tool loop.
dump_review gemini-cli "$TMP/gem-plan.json"
argv=$(python3 -c 'import json; print(" ".join(json.load(open("'"$TMP/gem-plan.json"'"))["argv"]))')
assert_contains "gemini review uses yolo approval" "$argv" "--approval-mode yolo"
assert_not_contains "gemini review avoids plan mode" "$argv" "--approval-mode plan"
assert_contains "gemini review disables extensions" "$argv" "-e none"

# Explicit empty-answer exit 66
export PORCH_RUN_DIR="$TMP/run-gemini-empty"
mkdir -p "$PORCH_RUN_DIR"
export PORCH_FAKE_GEMINI_MODE=empty
set +e
"$LIB_DIR/backend_run.sh" --mode review --agent-id gemini-cli --raw "empty" \
  >/dev/null 2>"$TMP/gem-empty.err"
gem_empty_rc=$?
set -e
assert_eq "gemini empty-answer exit 66" "$gem_empty_rc" "66"
assert_contains "gemini empty message" "$(cat "$TMP/gem-empty.err")" "empty"
export PORCH_FAKE_GEMINI_MODE=ok

# Devin CLI one-shot runs through `devin acp` (ACP JSON-RPC over stdio) via
# the devin_acp_oneshot.py helper: readonly → --agent-type review (a read-only
# + shell agent, no write/edit tools); yolo → session/set_mode bypass.
dump_review devin "$TMP/devin-review.json"
argv=$(python3 -c 'import json; print(" ".join(json.load(open("'"$TMP/devin-review.json"'"))["argv"]))')
assert_contains "devin review uses ACP one-shot driver" "$argv" "devin_acp_oneshot.py"
assert_contains "devin review passes readonly access" "$argv" "--access readonly"
assert_contains "devin review selects SWE-2 Flash" "$argv" "--model swe-2-flash-high"
assert_contains "devin review prompt-file placeholder" "$argv" "--prompt-file __PROMPT_FILE__"
assert_not_contains "devin review no print-mode permission flag" "$argv" "--permission-mode"
assert_not_contains "devin review no runtime config" "$argv" "--config"
dump_delegate devin "$TMP/devin-delegate.json"
argv=$(python3 -c 'import json; print(" ".join(json.load(open("'"$TMP/devin-delegate.json"'"))["argv"]))')
assert_contains "devin delegate passes yolo access" "$argv" "--access yolo"

DEVIN_MODEL="devin-override" PORCH_DUMP_ARGV="$TMP/devin-overrides.json" \
  "$LIB_DIR/backend_run.sh" --mode review --agent-id devin --raw "hello" >/dev/null
argv=$(python3 -c 'import json; print(" ".join(json.load(open("'"$TMP/devin-overrides.json"'"))["argv"]))')
assert_contains "devin shell runner honors DEVIN_MODEL" "$argv" "--model devin-override"

# Devin happy path: stdout answer (thought excluded), artifacts, ACP env contract.
# ACP_BACKEND=windsurf is exported to prove the helper strips it (real Devin
# reports "Not logged in" when it leaks through from Devin Desktop).
export PORCH_RUN_DIR="$TMP/run-devin-ok"
export PORCH_SAVE_OUTPUTS=1
export PORCH_FAKE_DEVIN_MODE=ok
export PORCH_FAKE_ARGV_LOG="$TMP/devin-runtime-argv.jsonl"
: > "$PORCH_FAKE_ARGV_LOG"
mkdir -p "$PORCH_RUN_DIR"
set +e
dev_out=$(ACP_BACKEND=windsurf "$LIB_DIR/backend_run.sh" --mode review --agent-id devin --raw "devin q" 2>"$TMP/dev-ok.err")
dev_rc=$?
set -e
assert_eq "devin happy-path exit 0" "$dev_rc" "0"
assert_eq "devin happy-path stdout" "$dev_out" "FAKE_DEVIN_OK"
assert_not_contains "devin thought excluded from stdout" "$dev_out" "FAKE_THOUGHT"
assert_file "devin final artifact" "$PORCH_RUN_DIR/final/devin.txt"
assert_file "devin raw artifact" "$PORCH_RUN_DIR/raw/devin.jsonl"
devin_final=$(cat "$PORCH_RUN_DIR/final/devin.txt")
assert_eq "devin final is message chunks only" "$devin_final" "FAKE_DEVIN_OK"
if [[ -f "$PORCH_RUN_DIR/normalized/devin.jsonl" ]]; then
  devin_norm=$(cat "$PORCH_RUN_DIR/normalized/devin.jsonl")
  assert_contains "devin normalized backend label" "$devin_norm" '"backend": "devin-cli"'
  assert_contains "devin normalized has text event" "$devin_norm" '"type": "answer_delta"'
else
  echo "  FAIL  devin normalized artifact missing"
  FAIL=$((FAIL+1))
fi
devin_acp_check=$(python3 - "$PORCH_FAKE_ARGV_LOG" <<'PY'
import json, sys
rows = [json.loads(l) for l in open(sys.argv[1]) if l.strip()]
main = [r for r in rows if "argv" in r][-1]
assert main["agent_type"] == "review", main
assert main["has_ACP_BACKEND"] is False, main
assert main["model"] == "swe-2-flash-high", main
assert "acp" in main["argv"], main
assert any(r.get("event") == "prompt" for r in rows), rows
print("ok")
PY
)
assert_eq "devin review ACP env/argv contract" "$devin_acp_check" "ok"

# Delegate --one-shot: yolo posture = default agent type + set_mode bypass.
export PORCH_RUN_DIR="$TMP/run-devin-del"
mkdir -p "$PORCH_RUN_DIR"
: > "$PORCH_FAKE_ARGV_LOG"
set +e
dev_del_out=$("$PORCH" delegate -a devin --one-shot "implement x" 2>"$TMP/dev-del.err")
dev_del_rc=$?
set -e
assert_eq "devin delegate --one-shot exit 0" "$dev_del_rc" "0"
assert_eq "devin delegate --one-shot stdout" "$dev_del_out" "FAKE_DEVIN_OK"
devin_del_check=$(python3 - "$PORCH_FAKE_ARGV_LOG" <<'PY'
import json, sys
rows = [json.loads(l) for l in open(sys.argv[1]) if l.strip()]
main = [r for r in rows if "argv" in r][-1]
assert main["agent_type"] is None, main
assert "--agent-type" not in main["argv"], main
set_mode = [r for r in rows if r.get("event") == "set_mode"]
assert set_mode and set_mode[-1].get("modeId") == "bypass", rows
print("ok")
PY
)
assert_eq "devin delegate uses bypass mode" "$devin_del_check" "ok"

# Antigravity CLI one-shot: headless stream-json both ways, prompt on stdin.
# Review adds session-only deny rules that agy enforces over user allow rules.
for spec in 'agy-gemini-4-argon gemini-4-argon' 'agy-gemini-4-carbon gemini-4-carbon' 'agy-gemini-3.8-flash gemini-3.8-flash'; do
  read -r profile model <<< "$spec"
  for mode in review delegate; do
    env PORCH_CONFIG="$SKILL_DIR/config.example.json" PORCH_DUMP_ARGV="$TMP/$profile-$mode.json" \
      "$LIB_DIR/backend_run.sh" --mode "$mode" --agent-id "$profile" --raw "hello" >/dev/null
    argv=$(python3 -c 'import json,sys; print(" ".join(json.load(open(sys.argv[1]))["argv"]))' "$TMP/$profile-$mode.json")
    assert_contains "$profile $mode selects model family" "$argv" "--model $model"
    assert_contains "$profile $mode selects high effort" "$argv" "--effort high"
    assert_contains "$profile $mode reads stream-json input" "$argv" "--input-format stream-json"
    assert_contains "$profile $mode writes stream-json output" "$argv" "--output-format stream-json"
    assert_contains "$profile $mode never prompts for permission" "$argv" "--dangerously-skip-permissions"
    assert_contains "$profile $mode sends slash text literally" "$argv" "--disable-slash-commands"
    assert_not_contains "$profile $mode has no print-mode prompt flag" "$argv" " -p "
    assert_not_contains "$profile $mode avoids plan workflow" "$argv" "--mode plan"
    if [[ "$mode" == review ]]; then
      assert_contains "$profile review denies file writes, MCP and browser" "$argv" \
        '--settings {"permissions":{"deny":["write_file(*)","mcp(*)","execute_url(*)"]}}'
    else
      assert_not_contains "$profile delegate keeps full access" "$argv" "--settings"
    fi
  done
done
AGY_MODEL=gemini-3.8-flash AGY_EFFORT=low PORCH_DUMP_ARGV="$TMP/agy-overrides.json" \
  "$LIB_DIR/backend_run.sh" --mode review --agent-id agy --raw "hello" >/dev/null
argv=$(python3 -c 'import json; print(" ".join(json.load(open("'"$TMP/agy-overrides.json"'"))["argv"]))')
assert_contains "agy shell runner honors AGY_MODEL and AGY_EFFORT" "$argv" "--model gemini-3.8-flash --effort low"

export PORCH_RUN_DIR="$TMP/run-agy-ok"
export PORCH_SAVE_OUTPUTS=1
export PORCH_FAKE_AGY_MODE=write
export PORCH_FAKE_ARGV_LOG="$TMP/agy-runtime-argv.jsonl"
: > "$PORCH_FAKE_ARGV_LOG"
mkdir -p "$PORCH_RUN_DIR" "$TMP/agy-cwd"
set +e
agy_out=$(cd "$TMP/agy-cwd" && "$LIB_DIR/backend_run.sh" --mode review --agent-id agy --raw "agy q" 2>"$TMP/agy-ok.err")
agy_rc=$?
set -e
assert_eq "agy review exit 0" "$agy_rc" "0"
assert_eq "agy review stdout is the result response" "$agy_out" "FAKE_AGY_OK"
assert_file "agy final artifact" "$PORCH_RUN_DIR/final/agy.txt"
assert_file "agy raw artifact" "$PORCH_RUN_DIR/raw/agy.jsonl"
assert_contains "agy normalized backend label" "$(cat "$PORCH_RUN_DIR/normalized/agy.jsonl")" '"backend": "antigravity-cli"'
assert_contains "agy normalized authoritative result" "$(cat "$PORCH_RUN_DIR/normalized/agy.jsonl")" '"type": "result"'
assert_eq "agy review write tool was denied" "$([[ -e "$TMP/agy-cwd/fake-agy-write.txt" ]] && echo written || echo denied)" "denied"
agy_check=$(python3 - "$PORCH_FAKE_ARGV_LOG" <<'PY'
import json, sys
rows = [json.loads(l) for l in open(sys.argv[1]) if l.strip()]
main = [r for r in rows if "argv" in r][-1]
assert main["transport"] == "headless", main
assert "write_file(*)" in main["deny"], main
assert not any("agy q" in a for a in main["argv"]), main
assert main["has_FULL_PROMPT"] is False, main
prompts = [r for r in rows if r.get("event") == "stdin_prompt"]
assert len(prompts) == 1 and prompts[0]["text"].endswith("agy q"), prompts
print("ok")
PY
)
assert_eq "agy review prompt travels on stdin" "$agy_check" "ok"

export PORCH_RUN_DIR="$TMP/run-agy-del"
mkdir -p "$PORCH_RUN_DIR"
export PORCH_FAKE_AGY_MODE=write
set +e
agy_del_out=$(cd "$TMP/agy-cwd" && "$PORCH" delegate -a agy --one-shot "implement x" 2>"$TMP/agy-del.err")
agy_del_rc=$?
set -e
assert_eq "agy delegate --one-shot exit 0" "$agy_del_rc" "0"
assert_eq "agy delegate --one-shot stdout" "$agy_del_out" "FAKE_AGY_OK"
assert_eq "agy delegate may write" "$([[ -e "$TMP/agy-cwd/fake-agy-write.txt" ]] && echo written || echo denied)" "written"

for spec in 'fail 1' 'interrupted 1' 'noresult 1' 'crash 1' 'empty 66'; do
  read -r agy_mode want <<< "$spec"
  export PORCH_RUN_DIR="$TMP/run-agy-$agy_mode"
  mkdir -p "$PORCH_RUN_DIR"
  export PORCH_FAKE_AGY_MODE="$agy_mode"
  set +e
  agy_bad_out=$("$LIB_DIR/backend_run.sh" --mode review --agent-id agy --raw "q" 2>"$TMP/agy-$agy_mode.err")
  agy_bad_rc=$?
  set -e
  assert_eq "agy $agy_mode exit $want" "$agy_bad_rc" "$want"
  assert_eq "agy $agy_mode prints no answer" "$agy_bad_out" ""
done
assert_contains "agy failed result is explained" "$(cat "$TMP/agy-fail.err")" "agy result ERROR: fake agy failure"
assert_contains "agy interrupted result is explained" "$(cat "$TMP/agy-interrupted.err")" "agy result INTERRUPTED"
unset PORCH_FAKE_AGY_MODE

# Explicit empty-answer exit 66 + backend failure surfaces stderr
export PORCH_RUN_DIR="$TMP/run-devin-empty"
mkdir -p "$PORCH_RUN_DIR"
export PORCH_FAKE_DEVIN_MODE=empty
set +e
"$LIB_DIR/backend_run.sh" --mode review --agent-id devin --raw "empty" \
  >/dev/null 2>"$TMP/dev-empty.err"
dev_empty_rc=$?
set -e
assert_eq "devin empty-answer exit 66" "$dev_empty_rc" "66"
export PORCH_FAKE_DEVIN_MODE=fail
set +e
"$LIB_DIR/backend_run.sh" --mode review --agent-id devin --raw "fail" \
  >/dev/null 2>"$TMP/dev-fail.err"
dev_fail_rc=$?
set -e
assert_eq "devin backend failure non-zero" "$dev_fail_rc" "1"
assert_contains "devin failure surfaces stderr" "$(cat "$TMP/dev-fail.err")" "stopReason=refusal"
export PORCH_FAKE_DEVIN_MODE=crash
set +e
"$LIB_DIR/backend_run.sh" --mode review --agent-id devin --raw "crash" \
  >/dev/null 2>"$TMP/dev-crash.err"
dev_crash_rc=$?
set -e
assert_eq "devin backend crash non-zero" "$dev_crash_rc" "1"
export PORCH_FAKE_DEVIN_MODE=ok

# Prompts must not be embedded in argv: large tasks are delivered over stdin
# (or a temporary prompt file for Grok), avoiding the OS ARG_MAX ceiling.
echo "=== Unbounded prompt transport ==="
large_prompt="$TMP/large-prompt.txt"
awk 'BEGIN {
  printf "BEGIN_LARGE_PROMPT\n"
  for (i = 0; i < 131072; i++) printf "x"
  printf "\nEND_LARGE_PROMPT\n"
}' > "$large_prompt"
export PORCH_FAKE_ARGV_LOG="$TMP/large-prompt-argv.jsonl"
: > "$PORCH_FAKE_ARGV_LOG"
# Parent shells may carry a leftover FULL_PROMPT; backend_run must not re-export it.
unset FULL_PROMPT 2>/dev/null || true
for agent in codex claude-code opencode gemini-cli grok devin agy; do
  export PORCH_RUN_DIR="$TMP/run-large-$agent"
  mkdir -p "$PORCH_RUN_DIR"
  env -u FULL_PROMPT "$LIB_DIR/backend_run.sh" --mode review --agent-id "$agent" --raw \
    < "$large_prompt" >/dev/null 2>"$TMP/large-$agent.err"
done
transport_check=$(python3 - "$PORCH_FAKE_ARGV_LOG" <<'PY'
import json
import sys

rows = [json.loads(line) for line in open(sys.argv[1]) if line.strip()]
# Primary CLI rows only (ignore grok-prompt-meta helpers and devin ACP event rows).
primary = [r for r in rows if r.get("bin") in ("codex", "claude", "opencode", "gemini", "grok", "devin", "agy")
           and "argv" in r]
assert len(primary) == 7, primary
for row in primary:
    argv = "\0".join(row["argv"])
    assert "BEGIN_LARGE_PROMPT" not in argv
    assert "END_LARGE_PROMPT" not in argv
    assert row.get("has_FULL_PROMPT") is False, row["bin"]
    for forbidden in (
        "--max-turns",
        "--max-budget-usd",
        "--max-tokens",
        "--max-output-tokens",
        "--max-steps",
    ):
        assert forbidden not in row["argv"], (row["bin"], forbidden, row["argv"])
    if row["bin"] == "grok":
        assert "--prompt-file" in row["argv"]
    elif row["bin"] == "devin":
        # ACP path: prompt body travels as session/prompt text, never argv.
        assert "acp" in row["argv"]
        prompts = [r for r in rows if r.get("bin") == "devin" and r.get("event") == "prompt"]
        assert prompts, "devin session/prompt event missing"
        data = prompts[-1].get("text") or ""
        assert data.startswith("BEGIN_LARGE_PROMPT\n"), row["bin"]
        assert data.endswith("\nEND_LARGE_PROMPT"), row["bin"]
        assert len(data) > 131072, row["bin"]
    elif row["bin"] == "agy":
        # Headless path: one JSON-encoded stream-json message on stdin.
        prompts = [r for r in rows if r.get("bin") == "agy" and r.get("event") == "stdin_prompt"]
        assert prompts, "agy stdin prompt event missing"
        data = prompts[-1].get("text") or ""
        assert data.startswith("BEGIN_LARGE_PROMPT\n"), row["bin"]
        assert data.endswith("\nEND_LARGE_PROMPT"), row["bin"]
        assert len(data) > 131072, row["bin"]
    else:
        data = row.get("stdin", "")
        assert data.startswith("BEGIN_LARGE_PROMPT\n"), row["bin"]
        assert data.endswith("\nEND_LARGE_PROMPT"), row["bin"]
        assert len(data) > 131072, row["bin"]
# Grok body arrives via --prompt-file (meta row)
meta = [r for r in rows if r.get("bin") == "grok-prompt-meta"]
assert meta and meta[-1]["prompt_len"] > 131072, meta
print("ok")
PY
)
assert_eq "large prompts use stdin or prompt-file, never argv" "$transport_check" "ok"

# Gemini cannot delegate
set +e
"$LIB_DIR/backend_run.sh" --mode delegate --agent-id gemini-cli --raw "x" >/dev/null 2>"$TMP/gem.err"
rc=$?
set -e
assert_eq "gemini delegate rejected" "$rc" "4"
assert_contains "gemini delegate message" "$(cat "$TMP/gem.err")" "review-only"

# JSON booleans must round-trip as lowercase shell values. Otherwise a
# non-Gemini backend with supports_delegate=false would bypass this guard.
printf '%s\n' '{"agents":{"review-codex":{"backend":"codex-cli","model":"m","supports_delegate":false}}}' \
  > "$TMP/review-only-config.json"
set +e
PORCH_CONFIG="$TMP/review-only-config.json" \
  "$LIB_DIR/backend_run.sh" --mode delegate --agent-id review-codex --raw "x" \
  >/dev/null 2>"$TMP/review-only.err"
rc=$?
set -e
assert_eq "non-gemini supports_delegate=false rejected" "$rc" "4"
assert_contains "supports_delegate=false message" "$(cat "$TMP/review-only.err")" "supports_delegate=false"

set +e
PORCH_CONFIG="$TMP/review-only-config.json" \
  "$PORCH" delegate -a review-codex --steerable "x" \
  >/dev/null 2>"$TMP/review-only-steerable.err"
rc=$?
set -e
assert_eq "steerable review-only agent rejected before supervisor" "$rc" "4"
assert_contains "steerable review-only has config message" "$(cat "$TMP/review-only-steerable.err")" "review-only"
assert_not_contains "steerable review-only has no traceback" "$(cat "$TMP/review-only-steerable.err")" "Traceback"

echo "=== delegate wait / watch / list ==="
out=$("$PORCH" delegate --help 2>&1) || true
assert_contains "delegate help mentions wait" "$out" "delegate wait"
assert_contains "delegate help mentions watch" "$out" "delegate watch"
assert_contains "delegate help mentions list" "$out" "delegate list"
assert_contains "delegate help mentions detach" "$out" "--detach"
assert_contains "delegate help mentions one-shot escape hatch" "$out" "--one-shot"
assert_contains "delegate help documents steerable default" "$out" "by default"
assert_contains "delegate help documents observation timeout" "$out" "--timeout"
assert_contains "delegate help documents wait-any" "$out" "wait-any"
assert_contains "delegate help documents durable sessions" "$out" "--persist-session"
assert_contains "delegate help documents continuation" "$out" "--continue-run"

set +e
"$PORCH" delegate -a grok --one-shot --steerable "x" \
  >/dev/null 2>"$TMP/delegate-conflicting-mode.err"
rc=$?
set -e
assert_eq "delegate rejects conflicting execution modes" "$rc" "5"
assert_contains "delegate explains conflicting execution modes" \
  "$(cat "$TMP/delegate-conflicting-mode.err")" "mutually exclusive"

set +e
"$PORCH" delegate -a grok --one-shot --detach "x" \
  >/dev/null 2>"$TMP/delegate-detached-oneshot.err"
rc=$?
set -e
assert_eq "delegate rejects detached one-shot" "$rc" "5"
assert_contains "delegate explains detached one-shot" \
  "$(cat "$TMP/delegate-detached-oneshot.err")" "mutually exclusive"

# Every observer command must fail cleanly on a bad id: no hang, no traceback.
export PORCH_STEER_DIR="$TMP/steer-empty"
mkdir -p "$PORCH_STEER_DIR"

set +e
"$PORCH" delegate wait >/dev/null 2>"$TMP/wait-no-id.err"
rc=$?
set -e
assert_eq "wait without a run id is a usage error" "$rc" "2"

for sub in wait watch; do
  set +e
  "$PORCH" delegate "$sub" bogus_run_id >/dev/null 2>"$TMP/$sub-bogus.err"
  rc=$?
  set -e
  assert_eq "$sub on an unknown run exits 5" "$rc" "5"
  assert_contains "$sub on an unknown run explains why" "$(cat "$TMP/$sub-bogus.err")" "unknown run"
  assert_not_contains "$sub on an unknown run has no traceback" "$(cat "$TMP/$sub-bogus.err")" "Traceback"

  set +e
  "$PORCH" delegate "$sub" ../escape >/dev/null 2>"$TMP/$sub-escape.err"
  rc=$?
  set -e
  assert_eq "$sub rejects a traversal run id" "$rc" "5"
  assert_not_contains "$sub traversal rejection has no traceback" "$(cat "$TMP/$sub-escape.err")" "Traceback"
done

out=$("$PORCH" delegate list --json 2>/dev/null)
assert_eq "list on an empty registry is empty json" "$out" "[]"
set +e
"$PORCH" delegate list >/dev/null 2>&1
rc=$?
set -e
assert_eq "list on an empty registry succeeds" "$rc" "0"
unset PORCH_STEER_DIR

echo "=== Exact agent selection for delegate ==="
set +e
"$PORCH" delegate "do something" >/dev/null 2>"$TMP/del-no-a.err"
rc=$?
set -e
assert_eq "delegate without -a fails" "$rc" "5"

set +e
"$PORCH" delegate -a 'opencode-*' "x" >/dev/null 2>"$TMP/del-glob.err"
rc=$?
set -e
assert_eq "delegate rejects globs" "$rc" "5"
assert_contains "glob error message" "$(cat "$TMP/del-glob.err")" "exact agent id"

echo "=== Grok streaming-json extraction ==="
export PORCH_FAKE_ARGV_LOG="$TMP/fake-argv.jsonl"
: > "$PORCH_FAKE_ARGV_LOG"
export PORCH_RUN_DIR="$TMP/run-grok-ok"
mkdir -p "$PORCH_RUN_DIR"
export PORCH_FAKE_GROK_MODE=ok
out=$(PORCH_SINGLE_AGENT=1 "$LIB_DIR/backend_run.sh" --mode review --agent-id grok --raw "ping" 2>"$TMP/grok.err")
assert_eq "grok extracts final text" "$out" "FAKE_GROK_OK"
assert_file "grok raw artifact" "$PORCH_RUN_DIR/raw/grok.jsonl"
assert_file "grok normalized artifact" "$PORCH_RUN_DIR/normalized/grok.jsonl"
assert_file "grok final artifact" "$PORCH_RUN_DIR/final/grok.txt"
assert_file "grok primary final.txt" "$PORCH_RUN_DIR/final.txt"
assert_contains "progress on stderr" "$(cat "$TMP/grok.err")" "[porch]"
# stdout must be clean final only
assert_not_contains "stdout has no progress" "$out" "[porch]"

export PORCH_FAKE_GROK_MODE=error-event
export PORCH_RUN_DIR="$TMP/run-grok-err"
mkdir -p "$PORCH_RUN_DIR"
set +e
"$LIB_DIR/backend_run.sh" --mode review --agent-id grok --raw "ping" >/dev/null 2>"$TMP/grok-err.err"
rc=$?
set -e
assert_eq "grok error event fails" "$rc" "1"

export PORCH_FAKE_GROK_MODE=missing-end
export PORCH_RUN_DIR="$TMP/run-grok-noend"
mkdir -p "$PORCH_RUN_DIR"
set +e
"$LIB_DIR/backend_run.sh" --mode review --agent-id grok --raw "ping" >/dev/null 2>"$TMP/grok-noend.err"
rc=$?
set -e
assert_eq "grok missing end fails" "$rc" "1"

echo "=== Live progress before backend exit (timing/order) ==="
# Fake emits early thought, sleeps, then final. Progress must appear on stderr
# while the backend is still alive — not only after completion.
export PORCH_FAKE_GROK_MODE=slow
export PORCH_FAKE_SLOW_SLEEP=1.0
export PORCH_FAKE_SLOW_MARKER="$TMP/slow-early.marker"
export PORCH_RUN_DIR="$TMP/run-grok-slow"
mkdir -p "$PORCH_RUN_DIR"
rm -f "$PORCH_FAKE_SLOW_MARKER" "$TMP/slow.err" "$TMP/slow.out" "$TMP/slow.live"
: > "$TMP/slow.err"
set +e
"$LIB_DIR/backend_run.sh" --mode review --agent-id grok --raw "ping" \
  >"$TMP/slow.out" 2>"$TMP/slow.err" &
slow_pid=$!
set -e
# Wait until the fake has emitted the early event (still sleeping afterward)
seen_live=0
for _ in $(seq 1 80); do
  if [[ -f "$PORCH_FAKE_SLOW_MARKER" ]]; then
    # Give the pipeline a beat to flush progress, then require progress while alive
    sleep 0.15
    if grep -q 'type=thought' "$TMP/slow.err" 2>/dev/null; then
      if kill -0 "$slow_pid" 2>/dev/null; then
        seen_live=1
        echo 1 > "$TMP/slow.live"
      fi
    fi
    break
  fi
  # Bail early if the process already died without the marker
  if ! kill -0 "$slow_pid" 2>/dev/null; then
    break
  fi
  sleep 0.05
done
set +e
wait "$slow_pid"
slow_rc=$?
set -e
assert_eq "slow grok exit 0" "$slow_rc" "0"
assert_eq "slow grok final text" "$(cat "$TMP/slow.out")" "FAKE_GROK_OK"
assert_eq "progress observed before backend exit" "$seen_live" "1"
assert_contains "slow stderr has thought event" "$(cat "$TMP/slow.err")" "type=thought"
assert_contains "slow stderr has early-progress" "$(cat "$TMP/slow.err")" "early-progress"
assert_not_contains "slow stdout clean" "$(cat "$TMP/slow.out")" "[porch]"

export PORCH_FAKE_GROK_MODE=ok
unset PORCH_FAKE_SLOW_MARKER PORCH_FAKE_SLOW_SLEEP

echo "=== Backend stderr credential redaction ==="
export PORCH_FAKE_GROK_MODE=secret-fail
set +e
"$LIB_DIR/backend_run.sh" --mode review --agent-id grok --raw "redact" \
  >"$TMP/redact.out" 2>"$TMP/redact.err"
redact_rc=$?
set -e
assert_eq "secret stderr backend still fails" "$redact_rc" "2"
assert_contains "secret stderr is visibly redacted" "$(cat "$TMP/redact.err")" "<redacted>"
assert_not_contains "query credential absent from stderr" "$(cat "$TMP/redact.err")" "TEST_SECRET_VALUE"
assert_not_contains "authorization credential absent from stderr" "$(cat "$TMP/redact.err")" "TEST_BEARER_VALUE"
export PORCH_FAKE_GROK_MODE=ok

echo "=== Review ask with fakes (stdout/stderr split) ==="
export PORCH_RUN_DIR="$TMP/run-ask"
mkdir -p "$PORCH_RUN_DIR"

# A generated prompt is not implicitly connected to a shell command. Make the
# usage failure tell agent callers exactly how to transport it in the same
# invocation, before any backend is selected or started.
set +e
"$PORCH" review ask --progress compact -a claude-fable </dev/null \
  >"$TMP/ask-missing-prompt.out" 2>"$TMP/ask-missing-prompt.err"
missing_prompt_rc=$?
set -e
assert_eq "review ask without prompt exits 5" "$missing_prompt_rc" "5"
assert_contains "missing prompt explains accepted transports" \
  "$(cat "$TMP/ask-missing-prompt.err")" \
  "pass it as a positional argument, pipe it to stdin, or use --prompt-file FILE"

set +e
printf '  \n\t\n' | "$PORCH" review ask --progress compact -a claude-fable \
  >"$TMP/ask-blank-prompt.out" 2>"$TMP/ask-blank-prompt.err"
blank_prompt_rc=$?
set -e
assert_eq "review ask with blank stdin exits 5" "$blank_prompt_rc" "5"
assert_contains "blank stdin explains accepted transports" \
  "$(cat "$TMP/ask-blank-prompt.err")" \
  "pass it as a positional argument, pipe it to stdin, or use --prompt-file FILE"
assert_not_contains "blank stdin is not accepted as prompt transport" \
  "$(cat "$TMP/ask-blank-prompt.err")" "using stdin as the prompt"

export PORCH_RUN_DIR="$TMP/run-ask2"
mkdir -p "$PORCH_RUN_DIR"
out=$("$PORCH" review ask -a codex "What is 2+2?" 2>"$TMP/ask2.err")
assert_contains "ask has codex answer" "$out" "FAKE_CODEX_OK"
assert_contains "ask progress stderr" "$(cat "$TMP/ask2.err")" "[porch]"
assert_not_contains "ask stdout clean" "$out" "[porch]"

# A prompt file is transport only in public review: even an inherited raw-mode
# environment must not strip the read-only/work-alone/blast-radius framework.
printf '%s\n' '<initial_relevant_files completeness="likely-partial"><file path="src/a.py"/></initial_relevant_files>' > "$TMP/review-question.md"
: > "$PORCH_FAKE_ARGV_LOG"
PORCH_RAW_PROMPT=1 "$PORCH" review ask --progress none -a codex \
  --prompt-file "$TMP/review-question.md" >/dev/null 2>"$TMP/ask-file.err"
ask_file_prompt=$(python3 - "$PORCH_FAKE_ARGV_LOG" <<'PY'
import json, sys
rows = [json.loads(line) for line in open(sys.argv[1]) if line.strip()]
row = rows[-1]
print(row.get("stdin", "") + "\n" + "\n".join(row.get("argv", [])))
PY
)
assert_contains "review prompt-file keeps framework" "$ask_file_prompt" "INDEPENDENT ADVISORY MODE"
assert_contains "review prompt-file keeps read-only" "$ask_file_prompt" "READ-ONLY REVIEW"
assert_contains "review prompt-file keeps work-alone" "$ask_file_prompt" "WORK ALONE"
assert_contains "review prompt-file keeps blast-radius policy" "$ask_file_prompt" "SEED, NOT A BOUNDARY"
assert_contains "review prompt-file enables official-doc research" "$ask_file_prompt" "CURRENT OFFICIAL DOCUMENTATION"
assert_contains "review prompt-file keeps caller seed" "$ask_file_prompt" "src/a.py"
last_contract_line=$(printf '%s\n' "$ask_file_prompt" | grep -n 'PORCH REVIEW CONTRACT' | tail -1 | cut -d: -f1)
seed_line=$(printf '%s\n' "$ask_file_prompt" | grep -n 'src/a.py' | tail -1 | cut -d: -f1)
if [[ -n "$last_contract_line" && -n "$seed_line" && "$last_contract_line" -gt "$seed_line" ]]; then
  echo "  PASS  review prompt ends with trusted contract recap"
  PASS=$((PASS+1))
else
  echo "  FAIL  review prompt ends with trusted contract recap"
  FAIL=$((FAIL+1))
fi

# Runtime overrides must be reflected in both human and machine-readable
# provenance, not merely passed to the backend process.
export PORCH_RUN_DIR="$TMP/run-ask-override"
mkdir -p "$PORCH_RUN_DIR"
out=$(CLAUDE_MODEL="claude-runtime" CLAUDE_EFFORT="max" \
  "$PORCH" review ask -a claude-code "What is 2+2?" 2>"$TMP/ask-override.err")
assert_contains "ask heading uses resolved model" "$out" "claude-runtime"
assert_contains "ask heading uses resolved effort" "$out" "effort=max"
assert_not_contains "ask heading omits stale configured model" "$out" "claude-opus-5-5"
start_raw=$(python3 -c 'import json,sys; print(json.loads(open(sys.argv[1]).readline())["raw"])' \
  "$PORCH_RUN_DIR/normalized/claude-code.jsonl")
assert_contains "run_started persists resolved model" "$start_raw" "claude-runtime"
assert_contains "run_started persists resolved effort" "$start_raw" "'effort': 'max'"
assert_contains "run_started persists access policy" "$start_raw" "readonly"

export PORCH_RUN_DIR="$TMP/run-ask-override-xml"
mkdir -p "$PORCH_RUN_DIR"
xml=$(CLAUDE_MODEL="claude-runtime" CLAUDE_EFFORT="max" \
  "$PORCH" review ask --xml -a claude-code "What is 2+2?" 2>/dev/null)
assert_contains "ask XML uses resolved model" "$xml" 'model="claude-runtime"'
assert_contains "ask XML uses resolved effort" "$xml" 'effort="max"'

echo "=== Windows .cmd shim binary resolution ==="
# On Windows the contract resolves binaries with Python's shutil.which, which
# walks PATHEXT and returns the `foo.CMD` sibling spelled with backslashes. npm
# writes that shim without the executable bit, so bash can neither `command -v`
# the path nor exec-test it, and every npm-installed backend was rejected with a
# false "backend CLI not found". Reproduced here on any platform by pointing the
# override at such a path; the extensionless launcher is the usable one.
SHIM_DIR="$TMP/cmd-shim"
mkdir -p "$SHIM_DIR"
cp "$FAKES/fake-codex" "$SHIM_DIR/shimcodex"
chmod +x "$SHIM_DIR/shimcodex"
printf '@echo off\r\n' > "$SHIM_DIR/shimcodex.cmd"
chmod 644 "$SHIM_DIR/shimcodex.cmd"

export PORCH_RUN_DIR="$TMP/run-ask-cmd-shim"
mkdir -p "$PORCH_RUN_DIR"
set +e
out=$(PATH="$SHIM_DIR:$PATH" PORCH_BIN_CODEX="$SHIM_DIR\\shimcodex.CMD" \
  "$PORCH" review ask --progress none -a codex "review this" \
  2>"$TMP/ask-cmd-shim.err")
set -e
assert_contains "windows .cmd path falls back to extensionless launcher" "$out" "FAKE_CODEX_OK"
assert_not_contains "windows .cmd path is not reported missing" \
  "$(cat "$TMP/ask-cmd-shim.err")" "backend CLI not found"

# A bare name that resolves to nothing must still fail; the fallback only
# applies to values that look like a resolved path.
set +e
PATH="$SHIM_DIR:$PATH" PORCH_BIN_CODEX="definitely-absent-cli" \
  "$PORCH" review ask --progress none -a codex "review this" \
  >"$TMP/ask-absent-bin.out" 2>"$TMP/ask-absent-bin.err"
absent_bin_rc=$?
set -e
assert_eq "missing bare binary still fails" "$absent_bin_rc" "3"

# A missing .CMD path must not pick a different installation from PATH.
set +e
PATH="$SHIM_DIR:$PATH" PORCH_BIN_CODEX="$TMP/missing/shimcodex.CMD" \
  PORCH_RUN_DIR="$TMP/run-ask-missing-cmd" \
  "$PORCH" review ask --progress none -a codex "review this" \
  >"$TMP/ask-missing-cmd.out" 2>"$TMP/ask-missing-cmd.err"
missing_cmd_rc=$?
set -e
assert_eq "missing .CMD sibling fails instead of searching PATH" "$missing_cmd_rc" "3"
assert_not_contains "missing .CMD sibling does not run another CLI" \
  "$(cat "$TMP/ask-missing-cmd.out")" "FAKE_CODEX_OK"

# Other extensions do not imply an npm shim, even when a sibling exists.
set +e
PATH="$SHIM_DIR:$PATH" PORCH_BIN_CODEX="$SHIM_DIR/shimcodex.exe" \
  PORCH_RUN_DIR="$TMP/run-ask-missing-exe" \
  "$PORCH" review ask --progress none -a codex "review this" \
  >"$TMP/ask-missing-exe.out" 2>"$TMP/ask-missing-exe.err"
missing_exe_rc=$?
set -e
assert_eq "missing non-.CMD binary still fails" "$missing_exe_rc" "3"
assert_not_contains "missing non-.CMD binary does not run sibling" \
  "$(cat "$TMP/ask-missing-exe.out")" "FAKE_CODEX_OK"

echo "=== Delegate with fake ==="
export PORCH_RUN_DIR="$TMP/run-del"
mkdir -p "$PORCH_RUN_DIR"
export PORCH_RAW_PROMPT=1
set +e
out=$("$PORCH" delegate -a grok --one-shot "implement the feature" 2>"$TMP/del.err")
rc=$?
set -e
assert_eq "delegate exit 0" "$rc" "0"
assert_eq "delegate stdout is answer" "$out" "FAKE_GROK_OK"
# Verify YOLO argv was used
assert_contains "delegate fake saw always-approve" "$(cat "$PORCH_FAKE_ARGV_LOG")" "--always-approve"

echo "=== Shell-interpolation warning scope ==="
# Positional with backticks → warn; --prompt-file / stdin with same content → silent.
unset PORCH_SUPPRESS_SHELL_WARN
printf 'Explain `foo` and $(bar).\n' > "$TMP/prompt-with-ticks.txt"
set +e
"$LIB_DIR/backend_run.sh" --mode review --agent-id grok --raw \
  "Explain \`foo\` and \$(bar)." >/dev/null 2>"$TMP/warn-pos.err"
set -e
assert_contains "positional backticks warns" "$(cat "$TMP/warn-pos.err")" "WARNING: prompt contains literal backticks"

set +e
"$LIB_DIR/backend_run.sh" --mode review --agent-id grok --raw \
  --prompt-file "$TMP/prompt-with-ticks.txt" >/dev/null 2>"$TMP/warn-file.err"
set -e
assert_not_contains "prompt-file no shell warn" "$(cat "$TMP/warn-file.err")" "WARNING: prompt contains literal backticks"

set +e
"$LIB_DIR/backend_run.sh" --mode review --agent-id grok --raw \
  >/dev/null 2>"$TMP/warn-stdin.err" < "$TMP/prompt-with-ticks.txt"
set -e
assert_not_contains "stdin no shell warn" "$(cat "$TMP/warn-stdin.err")" "WARNING: prompt contains literal backticks"
export PORCH_SUPPRESS_SHELL_WARN=1

echo "=== normalize_stream.py unit ==="
printf '%s\n' \
  '{"type":"text","data":"A"}' \
  '{"type":"text","data":"B"}' \
  '{"type":"end","stopReason":"EndTurn"}' \
  > "$TMP/stream.jsonl"
python3 "$LIB_DIR/normalize_stream.py" --backend grok-build --agent-id grok \
  --input "$TMP/stream.jsonl" --extract-text --text-out "$TMP/stream.txt" > "$TMP/norm.jsonl"
assert_eq "normalize concat text" "$(cat "$TMP/stream.txt")" "AB"
assert_contains "normalize has run_completed" "$(cat "$TMP/norm.jsonl")" '"type": "run_completed"'

for _ in $(seq 1 30); do
  printf '%s\n' '{"type":"thought","data":"word "}'
done > "$TMP/token-stream.jsonl"
printf '%s\n' '{"type":"end","stopReason":"EndTurn"}' >> "$TMP/token-stream.jsonl"
python3 "$LIB_DIR/normalize_stream.py" --backend grok-build --agent-id grok \
  --input "$TMP/token-stream.jsonl" --progress \
  >"$TMP/token-norm.jsonl" 2>"$TMP/token-progress.err"
progress_lines=$(wc -l < "$TMP/token-progress.err" | tr -d ' ')
assert_le "token deltas coalesce into compact progress" "$progress_lines" "6"

printf '%s\n' \
  '{"type":"tool_call","toolName":"run_terminal_command","status":"pending","rawInput":{"command":"secret command must not leak"}}' \
  '{"type":"tool_call_update","status":"in_progress","rawOutput":"secret output must not leak"}' \
  '{"type":"tool_call_update","status":"completed","rawOutput":"secret output must not leak"}' \
  '{"type":"end","stopReason":"EndTurn"}' \
  > "$TMP/tool-progress-stream.jsonl"
python3 "$LIB_DIR/normalize_stream.py" --backend grok-build --agent-id grok \
  --input "$TMP/tool-progress-stream.jsonl" --progress --progress-style compact \
  --progress-interval 0 >"$TMP/tool-progress-norm.jsonl" 2>"$TMP/tool-progress.err"
assert_contains "compact progress reports Grok tool liveness" \
  "$(cat "$TMP/tool-progress.err")" "type=tool"
assert_not_contains "compact tool progress hides command" \
  "$(cat "$TMP/tool-progress.err")" "secret command"
assert_not_contains "compact tool progress hides output" \
  "$(cat "$TMP/tool-progress.err")" "secret output"
assert_contains "normalized Grok tool start is persisted" \
  "$(cat "$TMP/tool-progress-norm.jsonl")" '"type": "tool_started"'
assert_contains "normalized Grok tool completion is persisted" \
  "$(cat "$TMP/tool-progress-norm.jsonl")" '"type": "tool_completed"'

printf '%s\n' '{"type":"error","message":"x"}' > "$TMP/stream-err.jsonl"
set +e
python3 "$LIB_DIR/normalize_stream.py" --backend grok-build --agent-id grok \
  --input "$TMP/stream-err.jsonl" >/dev/null 2>&1
rc=$?
set -e
assert_eq "normalize error fails" "$rc" "1"

echo "=== Claude stream-json: authoritative result wins over distinct delta ==="
# Delta and result strings differ — final answer must be result, never delta/concat.
export PORCH_FAKE_CLAUDE_MODE=ok
export PORCH_RUN_DIR="$TMP/run-claude-dup"
mkdir -p "$PORCH_RUN_DIR"
export PORCH_FAKE_ARGV_LOG="$TMP/claude-dup-argv.jsonl"
: > "$PORCH_FAKE_ARGV_LOG"
out=$(PORCH_SINGLE_AGENT=1 "$LIB_DIR/backend_run.sh" \
  --mode review --agent-id claude-code --raw "ping" 2>"$TMP/claude-dup.err")
assert_eq "claude backend_run prefers result over delta" "$out" "FAKE_CLAUDE_RESULT"
assert_not_contains "claude backend_run has no delta text" "$out" "FAKE_CLAUDE_DELTA"
# Count occurrences in final artifact
claude_final="$PORCH_RUN_DIR/final/claude-code.txt"
if [[ -f "$claude_final" ]]; then
  count=$(grep -o 'FAKE_CLAUDE_RESULT' "$claude_final" | wc -l | tr -d ' ')
  delta_count=$(grep -c 'FAKE_CLAUDE_DELTA' "$claude_final" 2>/dev/null || true)
else
  count=$(grep -o 'FAKE_CLAUDE_RESULT' <<<"$out" | wc -l | tr -d ' ')
  delta_count=0
fi
delta_count="${delta_count:-0}"
assert_eq "claude final artifact result once" "$count" "1"
assert_eq "claude final artifact no delta" "$delta_count" "0"
# Normalized stream still has both text delta and result events
assert_file "claude raw artifact" "$PORCH_RUN_DIR/raw/claude-code.jsonl"
assert_contains "claude raw has delta" "$(cat "$PORCH_RUN_DIR/raw/claude-code.jsonl")" "content_block_delta"
assert_contains "claude raw has result" "$(cat "$PORCH_RUN_DIR/raw/claude-code.jsonl")" '"type":"result"'
assert_file "claude normalized artifact" "$PORCH_RUN_DIR/normalized/claude-code.jsonl"
assert_contains "claude norm has answer_delta event" "$(cat "$PORCH_RUN_DIR/normalized/claude-code.jsonl")" '"type": "answer_delta"'
assert_contains "claude norm has result event" "$(cat "$PORCH_RUN_DIR/normalized/claude-code.jsonl")" '"type": "result"'
assert_contains "claude progress on stderr" "$(cat "$TMP/claude-dup.err")" "[porch]"

# result-only stream (no deltas)
export PORCH_FAKE_CLAUDE_MODE=result-only
export PORCH_RUN_DIR="$TMP/run-claude-result-only"
mkdir -p "$PORCH_RUN_DIR"
out=$(PORCH_SINGLE_AGENT=1 "$LIB_DIR/backend_run.sh" \
  --mode review --agent-id claude-code --raw "ping" 2>"$TMP/claude-ro.err")
assert_eq "claude result-only answer once" "$out" "FAKE_CLAUDE_RESULT"
count=$(grep -o 'FAKE_CLAUDE_RESULT' <<<"$out" | wc -l | tr -d ' ')
assert_eq "claude result-only count" "$count" "1"
export PORCH_FAKE_CLAUDE_MODE=ok

echo "=== Claude is_error result: non-zero exit even when CLI exits 0 ==="
export PORCH_FAKE_CLAUDE_MODE=is-error
export PORCH_RUN_DIR="$TMP/run-claude-is-error"
mkdir -p "$PORCH_RUN_DIR"
set +e
out=$(PORCH_SINGLE_AGENT=1 "$LIB_DIR/backend_run.sh" \
  --mode review --agent-id claude-code --raw "ping" 2>"$TMP/claude-is-error.err")
rc=$?
set -e
if [[ $rc -ne 0 ]]; then
  assert_eq "claude is_error one-shot non-zero" "nonzero" "nonzero"
else
  assert_eq "claude is_error one-shot non-zero" "0" "nonzero"
fi
assert_not_contains "claude is_error no successful partial on stdout" "$out" "FAKE_CLAUDE_PARTIAL"
assert_not_contains "claude is_error no partial-ok on stdout" "$out" "partial-ok"
# Forensic artifacts preserved (raw/normalized; stderr/stdout separation).
assert_file "claude is_error raw artifact" "$PORCH_RUN_DIR/raw/claude-code.jsonl"
assert_file "claude is_error normalized artifact" "$PORCH_RUN_DIR/normalized/claude-code.jsonl"
assert_contains "claude is_error raw has is_error" \
  "$(cat "$PORCH_RUN_DIR/raw/claude-code.jsonl")" "is_error"
assert_contains "claude is_error norm has run_failed" \
  "$(cat "$PORCH_RUN_DIR/normalized/claude-code.jsonl")" "run_failed"
export PORCH_FAKE_CLAUDE_MODE=ok

echo "=== OpenCode 2 JSON text chunks ==="
printf '%s\n' \
  '{"type":"text","part":{"type":"text","text":"Hel"}}' \
  '{"type":"text","part":{"type":"text","text":"lo"}}' \
  > "$TMP/oc-hello.jsonl"
python3 "$LIB_DIR/normalize_stream.py" --backend opencode --agent-id opencode \
  --input "$TMP/oc-hello.jsonl" --extract-text --text-out "$TMP/oc-hello.txt" \
  --no-validate >/dev/null
assert_eq "opencode v2 oneshot text assembly" "$(cat "$TMP/oc-hello.txt")" "Hello"

# Unit-level: normalize_stream extract-text with *distinct* delta+result
printf '%s\n' \
  '{"type":"content_block_delta","delta":{"type":"text_delta","text":"DELTA_ONLY"}}' \
  '{"type":"result","result":"RESULT_WINS"}' \
  > "$TMP/claude-stream.jsonl"
python3 "$LIB_DIR/normalize_stream.py" --backend claude-code --agent-id claude \
  --input "$TMP/claude-stream.jsonl" --extract-text --text-out "$TMP/claude-extract.txt" \
  --no-validate --progress >"$TMP/claude-norm.jsonl" 2>"$TMP/claude-norm.err"
assert_eq "normalize claude result wins over delta" "$(cat "$TMP/claude-extract.txt")" "RESULT_WINS"
assert_not_contains "normalize extract has no delta" "$(cat "$TMP/claude-extract.txt")" "DELTA_ONLY"
assert_contains "normalize keeps result event" "$(cat "$TMP/claude-norm.jsonl")" '"type": "result"'
assert_contains "normalize keeps answer_delta event" "$(cat "$TMP/claude-norm.jsonl")" '"type": "answer_delta"'

printf '%s\n' \
  '{"type":"result","result":"ONLY"}' \
  > "$TMP/claude-stream-ro.jsonl"
python3 "$LIB_DIR/normalize_stream.py" --backend claude-code --agent-id claude \
  --input "$TMP/claude-stream-ro.jsonl" --extract-text --text-out "$TMP/claude-extract-ro.txt" \
  --no-validate >/dev/null
assert_eq "normalize claude result-only once" "$(cat "$TMP/claude-extract-ro.txt")" "ONLY"

echo "=== Prompt > ARG_MAX; inherited FULL_PROMPT must not reach child ==="
ARG_MAX_BYTES="$(getconf ARG_MAX 2>/dev/null || echo 262144)"
# Slightly larger than ARG_MAX so an env export of the body would E2BIG.
OVER_SIZE=$((ARG_MAX_BYTES + 4096))
huge_prompt="$TMP/huge-prompt.txt"
python3 -c '
import sys
n = int(sys.argv[1])
sys.stdout.write("BEGIN_HUGE_PROMPT\n")
sys.stdout.write("H" * n)
sys.stdout.write("\nEND_HUGE_PROMPT\n")
' "$OVER_SIZE" > "$huge_prompt"
export PORCH_FAKE_ARGV_LOG="$TMP/huge-argv.jsonl"
: > "$PORCH_FAKE_ARGV_LOG"
export PORCH_RUN_DIR="$TMP/run-huge"
mkdir -p "$PORCH_RUN_DIR"
# Start with an *inherited exported* FULL_PROMPT (do not env -u). backend_run
# must unset it so the child does not see it, while still delivering >ARG_MAX body.
export FULL_PROMPT="LEAKED_INHERITED_FULL_PROMPT_MUST_NOT_REACH_CHILD"
set +e
"$LIB_DIR/backend_run.sh" --mode review --agent-id codex --raw \
  < "$huge_prompt" >"$TMP/huge.out" 2>"$TMP/huge.err"
huge_rc=$?
set -e
assert_eq "huge prompt backend exit 0" "$huge_rc" "0"
assert_eq "huge prompt final answer" "$(cat "$TMP/huge.out")" "FAKE_CODEX_OK"
huge_check=$(python3 - "$PORCH_FAKE_ARGV_LOG" "$OVER_SIZE" <<'PY'
import json, sys
rows = [json.loads(l) for l in open(sys.argv[1]) if l.strip()]
assert rows, "no fake log rows"
codex = [r for r in rows if r.get("bin") == "codex"]
assert codex, rows
row = codex[-1]
assert row.get("has_FULL_PROMPT") is False, row
stdin = row.get("stdin") or ""
assert "LEAKED_INHERITED_FULL_PROMPT" not in stdin, stdin[:120]
assert stdin.startswith("BEGIN_HUGE_PROMPT\n"), stdin[:80]
assert stdin.rstrip().endswith("END_HUGE_PROMPT"), stdin[-80:]
assert len(stdin) > int(sys.argv[2]), len(stdin)
print("ok")
PY
)
assert_eq "huge prompt reaches fake; FULL_PROMPT not in env" "$huge_check" "ok"

# Grok path (prompt-file) also must not re-export inherited FULL_PROMPT
: > "$PORCH_FAKE_ARGV_LOG"
export PORCH_RUN_DIR="$TMP/run-huge-grok"
mkdir -p "$PORCH_RUN_DIR"
export FULL_PROMPT="LEAKED_INHERITED_FULL_PROMPT_MUST_NOT_REACH_CHILD"
set +e
"$LIB_DIR/backend_run.sh" --mode review --agent-id grok --raw \
  < "$huge_prompt" >"$TMP/huge-grok.out" 2>"$TMP/huge-grok.err"
huge_grok_rc=$?
set -e
assert_eq "huge grok exit 0" "$huge_grok_rc" "0"
huge_grok_check=$(python3 - "$PORCH_FAKE_ARGV_LOG" "$OVER_SIZE" <<'PY'
import json, sys
rows = [json.loads(l) for l in open(sys.argv[1]) if l.strip()]
meta = [r for r in rows if r.get("bin") == "grok-prompt-meta"]
base = [r for r in rows if r.get("bin") == "grok"]
assert base and meta, rows
assert all(r.get("has_FULL_PROMPT") is False for r in base + meta), (base, meta)
m = meta[-1]
assert m["prompt_len"] > int(sys.argv[2]), m
assert m["prompt_startswith"].startswith("BEGIN_HUGE_PROMPT")
assert m["prompt_endswith"].endswith("END_HUGE_PROMPT")
print("ok")
PY
)
assert_eq "huge grok prompt-file; FULL_PROMPT absent" "$huge_grok_check" "ok"
unset FULL_PROMPT 2>/dev/null || true

echo "=== Delegate positional normalized off argv ==="
export PORCH_FAKE_ARGV_LOG="$TMP/del-pos-argv.jsonl"
: > "$PORCH_FAKE_ARGV_LOG"
export PORCH_RUN_DIR="$TMP/run-del-pos"
mkdir -p "$PORCH_RUN_DIR"
set +e
out=$("$PORCH" delegate -a grok --one-shot "implement positional task XYZ" 2>"$TMP/del-pos.err")
rc=$?
set -e
assert_eq "delegate positional exit 0" "$rc" "0"
assert_eq "delegate positional stdout" "$out" "FAKE_GROK_OK"
del_pos_check=$(python3 - "$PORCH_FAKE_ARGV_LOG" <<'PY'
import json, sys
rows = [json.loads(l) for l in open(sys.argv[1]) if l.strip() and '"bin": "grok"' in l or '"bin":"grok"' in l.replace(" ","")]
# parse properly
rows = [json.loads(l) for l in open(sys.argv[1]) if l.strip()]
grok = [r for r in rows if r.get("bin") == "grok"]
assert grok, rows
argv = grok[-1]["argv"]
joined = "\0".join(argv)
assert "implement positional task XYZ" not in joined, argv
assert "--prompt-file" in argv
print("ok")
PY
)
assert_eq "delegate positional not in backend argv" "$del_pos_check" "ok"
# --prompt-file path still works
printf 'from file task\n' > "$TMP/del-file-prompt.txt"
: > "$PORCH_FAKE_ARGV_LOG"
export PORCH_RUN_DIR="$TMP/run-del-file"
mkdir -p "$PORCH_RUN_DIR"
set +e
out=$("$PORCH" delegate -a grok --one-shot --prompt-file "$TMP/del-file-prompt.txt" 2>"$TMP/del-file.err")
rc=$?
set -e
assert_eq "delegate --prompt-file exit 0" "$rc" "0"
assert_eq "delegate --prompt-file stdout" "$out" "FAKE_GROK_OK"

echo "=== Failure stderr not duplicated ==="
# Use missing-end so a known normalize diagnostic is always present once.
export PORCH_FAKE_GROK_MODE=missing-end
export PORCH_RUN_DIR="$TMP/run-stderr-dup"
mkdir -p "$PORCH_RUN_DIR"
export PORCH_RAW_PROMPT=1
set +e
"$PORCH" review ask -a grok "fail please" >"$TMP/stderr-dup.out" 2>"$TMP/stderr-dup.err"
set -e
export PORCH_FAKE_GROK_MODE=ok
# progress_agent_done failed line should appear once (live via tee, not re-catted)
fail_lines=$(grep -c 'status=failed' "$TMP/stderr-dup.err" 2>/dev/null || true)
fail_lines="${fail_lines:-0}"
assert_eq "failed status once on stderr" "$fail_lines" "1"
# Known diagnostic must appear exactly once (not <=1 — zero would be a false green).
norm_err_count=$(grep -c 'grok stream missing end event' "$TMP/stderr-dup.err" 2>/dev/null || true)
norm_err_count="${norm_err_count:-0}"
assert_eq "normalize error exactly once on stderr" "$norm_err_count" "1"
# backend stderr banner from backend_run should not be doubled
banner_count=$(grep -c 'backend stderr:' "$TMP/stderr-dup.err" 2>/dev/null || true)
banner_count="${banner_count:-0}"
assert_le "backend stderr banner not duplicated" "$banner_count" "1"

echo "=== Review ask input transport (offline) ==="
if PYTHONDONTWRITEBYTECODE=1 python3 "$TESTS_DIR/review_input_test.py" -v; then
  PASS=$((PASS + 1))
else
  FAIL=$((FAIL + 1))
fi

echo "=== Shared runtime contracts (offline) ==="
if PYTHONDONTWRITEBYTECODE=1 python3 "$TESTS_DIR/opencode_go_test.py" -v; then
  PASS=$((PASS + 1))
else
  FAIL=$((FAIL + 1))
fi

set +e
CONTRACT_OUT=$(PYTHONPATH="$LIB_DIR${PYTHONPATH:+:$PYTHONPATH}" python3 "$TESTS_DIR/runtime_contracts.py" 2>&1)
CONTRACT_RC=$?
set -e
printf '%s\n' "$CONTRACT_OUT"
c_pass=$(printf '%s\n' "$CONTRACT_OUT" | sed -n 's/.*runtime contract tests: \([0-9]*\) passed.*/\1/p' | tail -1)
c_fail=$(printf '%s\n' "$CONTRACT_OUT" | sed -n 's/.*runtime contract tests: [0-9]* passed, \([0-9]*\) failed.*/\1/p' | tail -1)
c_pass=${c_pass:-0}
c_fail=${c_fail:-1}
PASS=$((PASS + c_pass))
FAIL=$((FAIL + c_fail))
if [[ $CONTRACT_RC -ne 0 && $c_fail -eq 0 ]]; then
  echo "  FAIL  runtime contracts non-zero exit without fail count"
  FAIL=$((FAIL + 1))
fi

echo "=== Quota parsing (offline) ==="
set +e
QUOTA_OUT=$(python3 "$TESTS_DIR/quota_test.py" 2>&1)
QUOTA_RC=$?
set -e
printf '%s\n' "$QUOTA_OUT"
quota_count=$(printf '%s\n' "$QUOTA_OUT" | sed -n 's/^Ran \([0-9][0-9]*\) tests.*/\1/p' | tail -1)
quota_count=${quota_count:-0}
if [[ $QUOTA_RC -eq 0 && $quota_count -gt 0 ]]; then
  PASS=$((PASS + quota_count))
else
  FAIL=$((FAIL + 1))
fi

echo "=== Steerable delegate (deterministic fakes) ==="
# Subprocess suite with its own counters; fold into PASS/FAIL.
chmod +x "$FAKES"/steer/* 2>/dev/null || true
set +e
PYTHONPATH="$LIB_DIR${PYTHONPATH:+:$PYTHONPATH}" python3 -u "$TESTS_DIR/steer/test_steer_e2e.py" 2>&1 | tee "$TMP/steer-suite.log"
STEER_RC=${PIPESTATUS[0]}
set -e
# Parse "steer tests: N passed, M failed"
steer_pass=$(sed -n 's/.*steer tests: \([0-9]*\) passed.*/\1/p' "$TMP/steer-suite.log" | tail -1)
steer_fail=$(sed -n 's/.*steer tests: [0-9]* passed, \([0-9]*\) failed.*/\1/p' "$TMP/steer-suite.log" | tail -1)
steer_pass=${steer_pass:-0}
steer_fail=${steer_fail:-1}
PASS=$((PASS + steer_pass))
FAIL=$((FAIL + steer_fail))
if [[ $STEER_RC -ne 0 && $steer_fail -eq 0 ]]; then
  echo "  FAIL  steerable suite non-zero exit without fail count"
  FAIL=$((FAIL + 1))
fi

# Bounded observation, failed abort, and durable Codex continuation regressions.
if PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$LIB_DIR${PYTHONPATH:+:$PYTHONPATH}" python3 "$TESTS_DIR/steer/test_claude_interrupt.py"; then
  PASS=$((PASS + 1))
else
  FAIL=$((FAIL + 1))
fi

if PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$LIB_DIR${PYTHONPATH:+:$PYTHONPATH}" python3 "$TESTS_DIR/steer/test_orchestration.py"; then
  PASS=$((PASS + 1))
else
  FAIL=$((FAIL + 1))
fi

# Portable locking, process lifetime, launchers, and native supervisor checks.
if PYTHONDONTWRITEBYTECODE=1 python3 "$TESTS_DIR/fleet_test.py"; then
  PASS=$((PASS + 1))
else
  FAIL=$((FAIL + 1))
fi

if PYTHONDONTWRITEBYTECODE=1 python3 "$TESTS_DIR/agentcore_guest_test.py"; then
  PASS=$((PASS + 1))
else
  FAIL=$((FAIL + 1))
fi

if PYTHONDONTWRITEBYTECODE=1 python3 "$TESTS_DIR/platform_test.py"; then
  PASS=$((PASS + 1))
else
  FAIL=$((FAIL + 1))
fi

if PYTHONDONTWRITEBYTECODE=1 python3 "$TESTS_DIR/cancellation_test.py"; then
  PASS=$((PASS + 1))
else
  FAIL=$((FAIL + 1))
fi

echo "=== Media mode: transcription and images (fake agy/codex/claude/ffmpeg) ==="
if PYTHONDONTWRITEBYTECODE=1 python3 "$TESTS_DIR/media_test.py" -v; then
  PASS=$((PASS + 1))
else
  FAIL=$((FAIL + 1))
fi

# Session-history readers (synthetic fixtures, offline).
echo "=== Session history readers (offline) ==="
set +e
SESS_OUT=$(PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$LIB_DIR${PYTHONPATH:+:$PYTHONPATH}" python3 "$TESTS_DIR/sessions_test.py" 2>&1)
SESS_RC=$?
set -e
printf '%s\n' "$SESS_OUT" | tail -5
sess_count=$(printf '%s\n' "$SESS_OUT" | sed -n 's/^Ran \([0-9][0-9]*\) tests.*/\1/p' | tail -1)
sess_count=${sess_count:-0}
if [[ $SESS_RC -eq 0 && $sess_count -gt 0 ]]; then
  PASS=$((PASS + sess_count))
else
  FAIL=$((FAIL + 1))
fi

# CLI surface for steerable modes
help_out=$("$PORCH" delegate -h 2>&1) || true
assert_contains "delegate help mentions steerable" "$help_out" "steerable"
assert_contains "delegate help mentions steer" "$help_out" "steer"
assert_contains "delegate help mentions status" "$help_out" "status"
assert_contains "delegate help mentions cancel" "$help_out" "cancel"

# shellcheck source=progress.sh
source "$TESTS_DIR/progress.sh"

echo ""
echo "Results: $PASS passed, $FAIL failed"
rm -rf "$TMP"
if [[ $FAIL -gt 0 ]]; then
  exit 1
fi
exit 0
