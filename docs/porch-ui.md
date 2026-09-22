# Optional Porch UI

## Product contract

Open the UI at any time to see existing delegated runs and watch their output.
Closing or restarting the UI must never launch, cancel, reap, or change a worker.
The registry and normalized artifacts remain the source of truth. The first
version observes registered `delegate` runs; review fan-out, one-shot runs, and
provider-internal children without registry records are outside this view.

## UX plan

- A quiet, warm-neutral working surface with system/light/dark themes and one blue accent. Two panes,
  readable typography, dividers instead of dashboard cards.
- Left: searchable run list, combined searchable dropdowns for launcher, executor
  profile, and project; All / Active / Errors / Finished lifecycle filters.
  **Errors** denotes failed or uncertain runs, never a request for approval.
  Counts respect the selected launcher, executor, project, and search.
  Runs are grouped by the caller recorded at launch. Each row shows the semantic
  run name, executor, project, and actual lifecycle state.
- Right: run identity and model, Live output / Final answer tabs, optional metadata.
  Text streams remain readable paragraphs; reasoning and tool details use
  disclosure. Output is always treated as text, never HTML or executable links.
- Follow output scrolls only while enabled. Scrolling up pauses following without
  stopping ingestion. A Jump to latest action resumes it. Switching runs resets
  only the local view, never the run.
- First connection loads a recent output window; Load from start is explicit.
  A bounded display reports omitted history. Complete normalized output and final
  answers can be saved separately. A final answer is not concatenated with deltas.
- Loading, empty registry, no matches, unreadable run, stale supervisor,
  disconnected server, missing output, and journal replacement are distinct states.
  Silence never means idle or failed. Reconnection retains the last known display.
- Keyboard-accessible controls, visible focus, reduced motion, responsive
  single-column layout. Restrained transitions on selection and disclosures.

## Architecture and decisions

`porch ui` starts a loopback-only Python stdlib HTTP observer and prints a local
URL. It opens no browser implicitly. `porch ui --desktop` wraps that same page in
Electron. Only the UI needs Node dependencies/build artifacts; the existing CLI
and workers stay dependency-free with respect to the frontend.

Dependency direction: React view → read-only HTTP API → Registry + artifact reader.
Electron only hosts the page. No new agent framework, database, provider adapter,
event bus, background indexing daemon, or second run lifecycle.

Use React, strict TypeScript, Vite, and a small CSS stylesheet. Next.js SSR, Mastra,
auth accounts, and PostgreSQL from the [vibe-stack reference](https://github.com/CodeAlive-AI/vibe-stack/blob/main/docs/web-app.md)
solve needs absent from this local observer. Paseo inspires task-first lists and
progressive disclosure; no Paseo code or daemon contract is transplanted.

The existing compact `delegate events` projection deliberately truncates text.
The observer therefore reads complete normalized event data with byte cursors,
bounded I/O, replacement detection, and partial-line handling. It never exports
provider `raw` payloads. Event data stays full fidelity within explicit size limits.
Polling is sequential (one second for selected output, two for discovery), with
fast bounded catch-up. This is sufficient for local monitoring without introducing
WebSocket lifecycle state. Hidden windows poll less often.

HTTP uses an ephemeral bearer token delivered in the URL fragment, exact Host and
Origin checks, no CORS, no mutations, fixed artifact routes, and a restrictive CSP.
Electron has sandboxing and context isolation, no Node in the renderer, no preload
bridge, and denies new windows, permissions, non-output downloads, and external navigation.
The observer has no worker shutdown hook.

## Acceptance and verification plan

Checkpointed implementation (explicit contract; no hard-coded happy-path phase):

1. Exercise HTTP against isolated real registry/artifact fixtures. Verify discovery,
   long Unicode output, partial writes, cursor continuation/reset, final answers,
   malformed journals, stale runs, and auth/path boundaries.
2. Check reducer invariants: deltas coalesce without duplication, result stays
   separate, retained output is bounded and omission is visible.
3. Build/typecheck/lint the app. Launch the real Electron shell against a temporary
   registry: append output after opening, switch runs, filter, disconnect/reconnect,
   and close/reopen while a fake worker remains alive. Inspect actual screenshots.
4. Run existing offline runtime tests with fake providers; spend no model tokens.

Launch controls and a provider-child graph are deferred until their contracts are
needed. The observer must not manufacture hierarchy or steering guarantees.

## Implemented and verified (2026-09-23)

Implemented the optional `porch ui` / `porch ui --desktop` entrypoints, Python
observer, React view, Electron shell, output export, skill documentation, and CI
checks. A small supervisor metadata addition records caller provenance and stamps the
executor identity for nested Porch launches. Pre-existing working-tree changes
were retained. Plugin manifests and the optional UI were bumped to
`2.0.0-beta.1` for the beta commit.

Verified locally on macOS:

- Strict TypeScript, Biome, and production Vite build pass.
- Eight presentation tests, 13 real HTTP/registry tests, and 54 orchestration
unit/integration tests pass.
- Electron acceptance passes: existing runs, actual Unicode journal appends,
  literal rendering of hostile HTML, sanitized output download, selection and
  combined searchable filters, keyboard selection, launcher grouping, theme
  switching and persistence, separate final answer, scroll pause/follow,
  connection loss/recovery,
  narrow layout, and close/reopen with the fixture worker alive and metadata intact.
  Desktop, filter, and narrow screenshots were visually inspected. On macOS the
  Electron window is placed on the built-in display, which acceptance verifies.
- Native platform suite: 20 tests, four Windows-only skips, no failures.
- Full offline runtime harness: **986 passed, 0 failed** on the final sequential
  run. An earlier concurrent run had four cascading assertions fail in the short
  Grok steer fixture because it completed before steer arrived; its isolated
  rerun passed 16 assertions, and the full sequential rerun passed unchanged.

The UI was built locally and opened against the standard user registry. No real
provider calls were needed for verification. Windows/Linux native desktop behavior
and the newly added CI job were not executed on this Mac. Remaining scope limits
and launch instructions are in the [UI README](../skills/pragmatic-orchestration/ui/README.md).

The filter control uses [Base UI Combobox](https://base-ui.com/react/components/combobox)
with an input inside its popup, so long profile IDs and project paths can be
found by typing while keeping a compact sidebar. The generated brand mark and
prompt are stored under [`ui/public/brand`](../skills/pragmatic-orchestration/ui/public/brand/README.md).

`porch ui --desktop --mine` is the skill's user-invoked "show my subagents"
path. New run metadata includes an opaque launcher instance: a hash of the
Codex/Claude session id, or the parent Porch run id for nested delegates. The
view starts on this session's active runs and selects the newest, while
`--focus-run RUN_ID` selects an exact known run. Legacy records without a
session marker are not inferred as current. Browser/Electron acceptance verifies
cross-session exclusion, two active runs from one session, newest-run selection,
and exact selection of a completed run. New records include a high-resolution
creation order so runs started in the same second are ordered reliably.

The run list shows Porch turns (the initial prompt plus accepted follow-ups),
while run information exposes the executor's native session ID when available.
The macOS desktop bundle and helper processes are branded Porch. The final
Playwright run verified both changes on the built-in laptop display.
