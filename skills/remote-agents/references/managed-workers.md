# Managed workers: the durable-daemon plane

For work that must survive SSH drops, chat restarts, and supervision gaps,
run a **managed-worker daemon** on the host (the reference implementation is a
Grok ACP daemon controlled by a local `*-agentctl` CLI over SSH; the pattern
below is implementation-agnostic). `porch` covers ordinary remote delegation;
this plane is for worker fleets with explicit lifecycle.

## Topology

```text
Mac
└─ manager agent (local)
   └─ *-agentctl (CLI-only control)
      └─ OpenSSH over AWS SSM
         └─ one-shot remote relay
            └─ Unix socket 0600
               └─ persistent daemon
                  ├─ agent runtime (ACP or equivalent)
                  ├─ per-worker state: agent.json + events.jsonl
                  └─ per-worker callback MCP (worker-facing only)
```

The manager never moves to the VM. Each control request is one authenticated
SSH invocation carrying one JSON command; ControlMaster amortizes setup. The
relay reaches only the daemon's Unix socket as the daemon user — no TCP
control endpoint exists.

## Why a daemon

A session runtime (ACP or similar) supplies session identity, streaming
output, plans, tool metadata, cancellation, and honest prompt lifecycle. A
persistent daemon owns it because local CLI, SSH, or the manager may restart
without terminating the worker. The daemon records per-prompt lifecycle —
`request_sent → queued → running → completed/cancelled/superseded/dropped` —
and never claims a steer was applied merely because it was written.

## Honest prompt lifecycle

- `ack=request_sent` means the request reached the daemon — not that the
  worker accepted it. Confirm via `status.prompts[]`/events.
- Each `interrupt`/`steer` supersedes or queues; inspect the journal before
  claiming what happened.
- Daemon or VM restart marks live sessions `interrupted`; recover from the
  journal, start a replacement, and report the discontinuity.
- `watch`/`wait` with persisted `nextCursors` preserves per-agent event
  order; persist cursors only after processing the events.

## Contracts and callbacks

Workers run with the remote user's full authority — ACP is an orchestration
boundary, not containment. So:

- Give complete contracts: objective, absolute remote `cwd`, context, allowed
  scope, constraints, validation, deliverables.
- `env-check` (allowlisted manifest: tool sha256, git HEAD, min resources,
  approved HTTPS reachability) before trusting a host for a class of work.
- Callback MCP is worker-facing only (e.g. `manager_notify`); it is never
  registered on the manager side. Callback tokens are per-worker, never
  returned through the manager CLI.
- Journal every callback; the manager polls them, they are not trusted input.

## Durability

| Event | Worker survives | Recovery |
|---|---:|---|
| Local CLI / SSH exits | yes | next request opens another relay |
| Manager restarts | yes | `list` then `tail`/`result` |
| Daemon restarts | no active turn | journal → `interrupted` |
| VM reboot/stop | no active turn | same |
| VM/disk destroyed | no | restore from repo/EBS backup |

## Guardrails

- `list --active-only` must be empty before restarting the daemon for an
  upgrade; a terminal prompt can still belong to a live session — verify,
  export, close first.
- Safe-stop refuses while sessions are live; a `--force` bypass requires
  independent verification that nothing is running.
- Close completed workers — they count toward `maxAgents` until closed.
