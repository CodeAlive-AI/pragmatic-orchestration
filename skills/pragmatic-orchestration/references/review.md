# Independent review

`review ask` and `review code` send one request to exactly one explicitly selected
profile. The review contract instructs the backend to work alone and read-only;
its enforcement depends on the selected backend's capabilities. The caller must
independently assess findings and verify the result.

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
`3` backend failure or empty response, `4` configuration error, `5` usage error.

The old `--depth`, `--mode`, `--judge`, `--no-fallback`, `--dry-run` and
`--exclude` review options and selection of multiple profiles are removed. Replace
old code-review commands with `review code -a <exact-id> ...` and old multi-model
queries with a single `review ask -a <exact-id> ...`.

Before repository-backed reviews, perform a short read-only triage and supply
known relevant paths as a likely-incomplete seed. Ask the reviewer to explore
callers, tests, configuration and other relevant areas independently. Apply
[review-acceptance.md](review-acceptance.md) when assessing results.
