# Visual QA on the Windows desktop

Bounded, evidence-producing GUI test runs inside the interactive desktop.
Requires: desktop started (`windows-desktop.md`), a UI-automation MCP driver
installed on the host, and an agent CLI for the worker.

## The QA driver is external

This skill does not ship a UI driver. Install a direct pywinauto/Win32/UIA MCP
server on the host (a dedicated `windows-qa`-style skill provides one) and set
`qa.mcpServer`/`qa.mcpName` in config. Contract the driver must meet:

- `qa_refresh_and_list_windows`, `select_application_window`,
  `qa_view_screenshot(scope="window"|"desktop", region=[x,y,w,h])` returning
  **native MCP image content** (not base64 text), physical pixels, no resizing.
- Artifact PNGs written under `qa.artifactDir` with recorded
  `artifact_path`/`image_size` metadata so runs are auditable offline.

`verify-qa-vision.py` + `validate-qa-vision.py` are the installation smoke
test: they draw a random fixture window, verify the transport directly
(native image blocks, pixel-exact crops, invalid-region rejection), then run
the agent with a visual-only prompt and compare its JSON answer against ground
truth. Configure via `QA_*` env vars (see the script docstring); only
`verified-report.json` with `ok=true` establishes success. It is a check, not
a resident service.

## Dispatching a QA worker

```powershell
# on the host (or via ssh <alias> 'powershell ... -File ...' after scp-ing inputs)
scripts/Start-VisualQa.ps1 -PromptFile <scenario.txt> -OutputDirectory <workRoot>\runs\<run>
```

It registers a temporary scheduled task (`Interactive` logon) running
`run-visual-qa.py`: bounded by `-TimeoutSeconds` (≤3600) and `-MaxTurns`
(≤500), single-writer locked (one UI worker per desktop), process-tree cleanup
on timeout (`taskkill /T /F`), artifacts in the output dir: `status.json`,
`events.jsonl`, `answer.md`, `effective-prompt.txt`.

The runner injects a GUI-launch preamble: reuse existing windows, launch GUI
processes only through the scheduled-task launcher (`--launcher`, default
`<workRoot>/desktop-helpers/Start-Interactive.ps1`), verify PID survival, and
never treat process start as UI readiness.

## Prompt contract for QA scenarios

State in the prompt: the exact application/window under test, what to do
step-by-step, what to capture (explicit `qa_view_screenshot` calls), the
assertion that defines pass/fail, and "do not modify anything" where
appropriate. The worker answers; the **manager** inspects artifacts — an
answer is not a pass.

## Evidence

```bash
scripts/fetch-qa-screenshots.py --remote-run <workRoot>/runs/<run> --output-dir <local-dir>
```

Fetches `events.jsonl`, extracts `artifact_path` entries, downloads each PNG,
verifies signature + `image_size` match, writes `screenshots.json` with
sha256s. Inspect the actual images — never trust a textual "I saw it".

## Rules

- One interactive desktop = one UI worker at a time; the lock is in the
  Windows account, observers do not change it.
- `MainWindowTitle`/`MainWindowHandle` queried over SSH can be empty across
  sessions even when the window is visible — verify through the driver.
- A lock screen kills capture, not transport: probe before blaming the driver.
- Distinguish "QA worker failed" from "app under test failed" — keep both
  artifacts.
