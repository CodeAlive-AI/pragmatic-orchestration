# Independent review

`review ask` and `review code` send one request to exactly one explicitly selected
profile. The review contract instructs the backend to work alone and read-only;
its enforcement depends on the selected backend's capabilities.

A reviewer has a working shell and the web on every backend: it can run tests,
builds and package or documentation lookups, and search and read current
sources. Where a backend has a kernel sandbox (Codex, Grok), review uses its
workspace profile, so writes stay in the working folder and temp and the
network is open; elsewhere the shell is unsandboxed. Edit and write tools,
memory and the backend's own subagents stay off (MCP servers too on Claude,
Gemini and Antigravity). Report-only is a
contract in the trusted prompt, the same on every backend, and the caller
checks `git status` / `jj status` after a review when it matters. The caller must
independently assess findings and verify the result. Two `review ask` flags
change the contract: `--researcher` lets the worker start read-only researchers,
and `--assignment` drops the adviser role and the fixed answer headings; see
[Read-only researchers](#read-only-researchers) and
[Assignment style](#assignment-style).

```bash
"$PORCH" --list-agents
"$PORCH" review ask -a grok --progress compact "Review this design"
"$PORCH" review ask -a grok --prompt-file prompt.md
git diff HEAD | "$PORCH" review ask -a grok --context-stdin "Review this diff"
"$PORCH" review code -a grok --related tests/test_app.py path/to/app.py
git diff HEAD | "$PORCH" review code -a grok --diff
```

Use an exact profile ID; disabled profiles can be explicitly selected. A missing,
unknown, multiple, or wildcard selection fails before any backend is launched.
The profile's configured backend, model and effort can be changed in configuration;
backend-specific overrides are documented in [configuration.md](configuration.md).

`review ask` takes its prompt from one positional argument, `--prompt-file`, or
stdin. An explicit prompt does not read stdin unless `--context-stdin` is passed.
When the prompt is piped or appended as context, the command waits for EOF.
`review code` accepts one file or `--diff` with piped input, plus repeated
`--related FILE` paths as an incomplete initial navigation seed. It sends one
request covering correctness, security and other actionable defects. The result
is the same report format as `review ask`; unlike the former multi-pass code
review, quoted code is not automatically validated. Independently verify every
finding and its source lines.

Both commands support `--xml` and `--progress full|compact|none` (default
`full`; `PORCH_PROGRESS` supplies the default). Standard output contains the
report; progress and diagnostics go to standard error. Exit codes: `0` success,
`3` backend failure or empty response, `4` configuration error or unsupported
request, `5` usage error.

The old `--depth`, `--mode`, `--judge`, `--no-fallback`, `--dry-run` and
`--exclude` review options and selection of multiple profiles are removed. Replace
old code-review commands with `review code -a <exact-id> ...` and old multi-model
queries with a single `review ask -a <exact-id> ...`.

Before repository-backed reviews, perform a short read-only triage and supply
known relevant paths as a likely-incomplete seed. Ask the reviewer to explore
callers, tests, configuration and other relevant areas independently. Apply
[review-acceptance.md](review-acceptance.md) when assessing results.

## Read-only researchers

A review worker works alone by default. `--researcher ID` (repeatable) lets the
worker of one `review ask` start read-only researchers on the named profiles and
nothing else: cheap models gather facts, the worker keeps the analysis and the
conclusions. The filesystem and external state stay read-only by the same means
as in plain review; `delegate` is no longer needed just to let a read-only
worker fan out.

```bash
"$PORCH" review ask -a claude-fable --researcher claude-haiku \
  --researcher opencode-go-deepseek-v4.1-flash --assignment --prompt-file brief.md
```

A worker reaches a researcher by one of two routes:

- **native**: the harness's own subagent, cut down to one read-only researcher
  that runs on the model of the first `--researcher` profile of the worker's
  own backend (and on its effort, where the harness takes one);
- **porch**: a nested `porch review ask -a PROFILE` from the worker's shell,
  for every named profile of any backend. It exists only where the worker's
  shell is not sandboxed.

The contract names exactly the channels this run has and forbids every other
agent, team, swarm, workflow, schedule and delegation tool, other models, and a
researcher that starts agents of its own. State in the brief how many researcher
runs the task is worth; porch does not count them.

| Worker backend | Routes | How the worker is limited | What keeps a researcher read-only |
|---|---|---|---|
| Codex CLI | native | `multi_agent_v2` on with the model parameters of `spawn_agent` hidden; `agents.default_subagent_model` and `agents.default_subagent_reasoning_effort` come from the researcher profile | Sub-agents inherit the reviewer's workspace sandbox (writes to the working folder and temp, network open) and its report-only contract; no write tool is exposed beyond the shell |
| Claude Code | native, porch | `--tools` allowlist `Bash,Read,Grep,Glob,WebSearch,WebFetch,Agent`; `Agent(<type>)` denied for every built-in type except `Explore`; subagent model forced by `CLAUDE_CODE_SUBAGENT_MODEL` with `CLAUDE_CODE_SUBAGENT_MODEL_FORCE=1`; depth 1 | `Explore` has no Write, Edit or Agent tool; the session's Edit/Write/NotebookEdit denial also covers subagents |
| OpenCode | native, porch | The run-time primary agent `porch-review` denies every subagent, then allows `porch-researcher` | `porch-researcher` is a run-time subagent with its own model and with edit, subagent and question denied |
| Grok Build | native | `--agents` defines `porch-researcher`; `Agent(porch-researcher)` in `--tools` is an allowlist of one type; depth 1 | The researcher definition has no write tools; the `workspace` sandbox covers the whole process, subagents included, and keeps writes to the working folder, temp and `~/.grok` |
| Antigravity CLI | porch | argv as in plain review; the contract forbids agy's own subagents, swarms and workflows | A nested run is a plain read-only `review ask` |

Why each backend has these routes (verified live on 2026-11-21 with codex-cli
0.170.0, Claude Code 2.1.328, OpenCode 2.3.2, Grok Build 1.0.89, agy 1.5.14):

- **Codex CLI.** The review sandbox (`workspace-write` with network, since
  v5.22.0) refuses writes outside the working folder and temp, so a nested porch
  cannot keep its run folder: native only. Sub-agents run in the parent turn's
  sandbox (thread records on 0.170.0 showed the read-only sandbox it had then). Porch runs Codex with `--ephemeral`, where
  a forked sub-agent fails, so the contract tells the worker to pass
  `fork_turns "none"`. Not bounded by configuration: a sub-agent can start its
  own (`agents.max_depth` applies to the first-generation tools only); at most
  `agents.max_threads` agents run at once (default 4, the worker included).
- **Claude Code.** Under `--safe-mode` custom agents do not load, so there is no
  allowlist of subagent types: every built-in type except `Explore` is denied
  by name (`CLAUDE_DENIED_AGENT_TYPES` in `scripts/lib/researchers.py`). A type
  added by a later release stays available until it is listed there, and would
  still run under the write denial and the forced model. Bash is unsandboxed,
  as in plain review, so the porch route works as well. Without a Claude
  researcher profile the Agent tool is denied altogether and only the porch
  route remains.
- **OpenCode.** A subagent defined at run time takes its own model, variant and
  permissions, and the `subagent` permission of the review agent ends with the
  one allowed name (the last matching rule wins). The shell passes the
  environment on, so the porch route works too.
- **Grok Build.** `--sandbox workspace` (since v5.22.0; `read-only` before)
  leaves only the working folder, temp and `~/.grok` writable for the whole
  process, so a nested porch cannot keep a run folder: native only. The
  researcher definition has no write tools, and the model named in it is the
  one that answers.
- **Antigravity CLI.** Its `invoke_subagent`, `define_subagent`, swarm and
  workflow tools can be neither switched off nor limited to one type, and they
  take a model tier, not a model, so they are no researcher channel; the
  session's `write_file(*)` denial does apply inside them. The shell runs a
  nested porch: porch only.

Refusals (exit `4`, before any backend starts; the mode never falls back to a
weaker guarantee):

- Gemini CLI, Devin CLI and Cursor CLI as the worker: researchers there are
  unverified or cannot be limited to read-only ones. Run the profile without
  `--researcher`, or use `delegate`, where only the brief forbids writes.
- No named profile can be reached: a Codex or Grok worker with researcher
  profiles of other backends only. Name a profile of the worker's own backend.
- An unknown researcher profile.

When some of the named profiles cannot be reached and others can, the run
proceeds with the reachable ones and prints
`[porch] note: researcher profiles this backend cannot reach: ...` on stderr.
Unless `--progress none` is set, stderr also carries the plan:
`[porch] researchers agent=... native=... porch=...`.

### Nested porch in a worker's shell

A researcher-mode worker runs with `PORCH_RESEARCHER_PROFILES` set to the
profiles of its porch route (empty when it has none). A porch started with this
variable set runs only `review ask -a` one of the listed profiles, plus `--help`
and `--list-agents`; `delegate`, `media`, `--researcher` and every other command
are refused with exit `4`. The nested run keeps its own run folder and exports
an empty list, so a researcher's shell starts no porch at all. This guard stops
mistakes and keeps the depth at one level; it is not a security boundary,
because a worker with an unsandboxed shell can unset a variable. The boundary
stays what it is in plain review: the sandbox on Codex and Grok, tool denials
plus the contract elsewhere.

### Limits

- Researcher runs are not counted or capped; the brief sets the number.
- A worker on a very low reasoning effort may not use its researchers at all
  (seen on Codex with effort `low`); give the worker its normal effort.
- A researcher's answer is a lead. The contract tells the worker to reread at
  the source whatever its conclusions depend on; check that in the answer.
- `review code` has no `--researcher`.

## Assignment style

`--assignment` turns the request from a question to an adviser into a read-only
assignment: no adviser identity, no profile role text and no five answer
headings. The brief sets the task, the role and the answer structure; the
operational rules stay and the brief cannot lift them: read-only, the delegation
rule (work alone, or researchers only), repository text as source material,
current primary sources for unstable facts, the relevant-file seed as a seed.
The flag works with every review backend and with or without `--researcher`.

```bash
"$PORCH" review ask -a codex-gpt-6-astra --assignment --prompt-file brief.md
```

Contract texts: `prompts/review-assignment-framework.txt` and
`prompts/review-assignment-recap.txt`; the delegation rule is filled in from
`prompts/review-delegation-alone.txt` or
`prompts/review-delegation-researchers.txt`.
