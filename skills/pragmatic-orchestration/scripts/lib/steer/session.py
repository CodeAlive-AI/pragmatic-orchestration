"""Opt-in Codex conversation continuation, serialized across linked runs.

A failed/uncertain turn is never replayed. Explicit recovery sends a new prompt
after cleanup. The handle and latest-run claim are written before contacting the
provider, so even a failed resume leaves an inspectable, recoverable claimant.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from .registry import Registry, RegistryError, TERMINAL_STATUSES
from .util import atomic_write_json, read_json, open_lock_fd, lock_exclusive

# What a continuation takes from its source run instead of the profile or the
# environment: the native thread was built with these and resumes only with
# them, so a launch-time default for a new run has nothing to change here.
INHERITED_FIELDS = ("model", "effort")


def _not_latest(registry: Registry, source: str, latest: Any) -> str:
    """One actionable line for a continuation that names an older run of its chain."""
    head = f"source {source} is not the latest session run"
    if not isinstance(latest, str) or not latest:
        return f"{head}; session state names no successor; start a new run with --persist-session"
    try:
        meta = registry.load_meta(latest)
    except RegistryError:
        return f"{head}; its successor {latest} is unreadable; inspect it before continuing"
    status = meta.get("status")
    if status not in TERMINAL_STATUSES:
        return (f"{head}; successor {latest} is {status or 'starting'}; wait for it "
                f"(porch delegate wait {latest}), then pass --continue-run {latest}")
    if status == "completed" and meta.get("exit_code") == 0:
        return f"{head}; continue the successor instead: --continue-run {latest}"
    return (f"{head}; successor {latest} ended {status}; inspect its effects, then pass "
            f"--continue-run {latest} --recover-session")


def check_source(registry: Registry, source: str, *, recover_session: bool) -> Dict[str, Any]:
    """Outcome, session handle and anchor checks on the source; returns its meta."""
    previous = registry.load_meta(source)
    if recover_session:
        if previous.get("status") not in ("completed", "failed", "cancelled"):
            raise RegistryError("recovery requires a terminal run; use steer/cancel for active work")
        if previous.get("cleanup_status") != "completed":
            raise RegistryError("recovery requires completed backend cleanup; inspect and reconcile the old writer first")
    elif previous.get("status") != "completed" or previous.get("exit_code") != 0:
        raise RegistryError(f"continue requires a successful completed run; {source} is {previous.get('status')} "
                            f"(exit {previous.get('exit_code')}), previous outcome may be unknown; inspect task effects, "
                            "then add --recover-session with a new remaining-work instruction")
    handle = previous.get("session_handle")
    if not isinstance(handle, dict) or handle.get("backend") != "codex-cli" or not handle.get("thread_id"):
        raise RegistryError("run has no durable Codex session; start with --persist-session")
    anchor = handle.get("anchor_run_id")
    if not isinstance(anchor, str) or not anchor:
        raise RegistryError("invalid session anchor")
    registry.load_meta(anchor)  # validates existence and ownership
    return previous


def check_latest(registry: Registry, source: str, anchor: str) -> None:
    state_path = registry.run_path(anchor) / "session.json"
    if not state_path.is_file():
        raise RegistryError("session coordination state is missing; cannot safely continue after registry loss; "
                            "start a new run with --persist-session for the remaining work")
    state = read_json(state_path)
    latest = state.get("latest_run_id") if isinstance(state, dict) else None
    if latest != source:
        raise RegistryError(_not_latest(registry, source, latest))


def check_identity(previous: Dict[str, Any], current: Dict[str, Any]) -> None:
    """Fields a continuation cannot inherit: each names both values and the fix."""
    src = previous.get("run_id") or "the source"
    if previous.get("agent_id") != current.get("agent_id"):
        raise RegistryError(f"continue requires unchanged agent_id: {src} ran -a {previous.get('agent_id')}, "
                            f"this launch has -a {current.get('agent_id')}; pass -a {previous.get('agent_id')}")
    if previous.get("backend") != current.get("backend"):
        raise RegistryError(f"continue requires unchanged backend: {src} ran {previous.get('backend')}, profile "
                            f"{current.get('agent_id')} now resolves {current.get('backend')}; restore the profile "
                            "or start a new run with --persist-session")
    for field in INHERITED_FIELDS:
        if previous.get(field) != current.get(field):
            raise RegistryError(f"continue requires unchanged {field}: {src} ran {previous.get(field)!r}, "
                                f"this run has {current.get(field)!r}")
    if previous.get("backend_binary") != current.get("backend_binary"):
        old = previous.get("backend_binary")
        raise RegistryError(f"continue requires unchanged backend_binary: {src} ran {old}, this launch resolves "
                            f"{current.get('backend_binary')}; set PORCH_BIN_CODEX={old}")
    if Path(previous["cwd"]).resolve() != Path(current["cwd"]).resolve():
        raise RegistryError(f"continue requires the original working directory: {src} ran in {previous['cwd']}, "
                            f"this launch is in {current['cwd']}; run from {previous['cwd']}")


def plan_continuation(registry: Registry, source: str, *, agent_id: str, cwd: str,
                      recover_session: bool = False, notices: Optional[List[str]] = None) -> Dict[str, str]:
    """Every check a continuation allows without the backend, before any run exists.

    Returns the inherited model and effort. A refusal raises RegistryError with
    one actionable line. SessionLease repeats the checks under the session lock.
    """
    from .config_loader import agent_settings
    from backend_contract import EFFORT_ENV, MODEL_ENV

    _agent, backend, model, binary = agent_settings(agent_id)
    effort = _agent.get("effort") or ""
    previous = check_source(registry, source, recover_session=recover_session)
    inherited = {field: str(previous.get(field) or "") for field in INHERITED_FIELDS}
    check_identity(previous, {"agent_id": agent_id, "backend": backend, "backend_binary": binary,
                              "cwd": cwd, **inherited})
    check_latest(registry, source, previous["session_handle"]["anchor_run_id"])
    if notices is not None:
        for field, launch, env_map in (("model", model, MODEL_ENV), ("effort", effort, EFFORT_ENV)):
            if launch == inherited[field]:
                continue
            env = env_map.get(backend, "")
            origin = (f"{env}={os.environ[env]}" if env and os.environ.get(env)
                      else f"profile {agent_id} {field} {launch or '(none)'}")
            notices.append(f"porch: continuation keeps the source run's {field} {inherited[field] or '(none)'}; "
                           f"{origin} applies to new runs only")
    return inherited


class SessionLease:
    def __init__(self, registry: Registry, run_id: str, source: str = "", *, recover_session: bool = False):
        self.registry = registry
        self.run_id = run_id
        self.source = source
        self.recover_session = recover_session
        self.handle: Dict[str, Any] = {}
        self._fd = None

    def acquire(self) -> None:
        if self.recover_session and not self.source:
            raise RegistryError("--recover-session requires --continue-run")
        current = self.registry.load_meta(self.run_id)
        anchor = self.run_id
        if self.source:
            previous = check_source(self.registry, self.source, recover_session=self.recover_session)
            self.handle = dict(previous["session_handle"])
            anchor = self.handle["anchor_run_id"]
            check_identity(previous, current)
        anchor_dir = self.registry.run_path(anchor)
        lock_path = anchor_dir / "control" / "session.lock"
        self._fd = open_lock_fd(lock_path)
        try:
            lock_exclusive(self._fd, blocking=False)
            state_path = anchor_dir / "session.json"
            if self.source:
                check_latest(self.registry, self.source, anchor)
            self.handle.update(backend="codex-cli", anchor_run_id=anchor)
            if self.source:
                # Save before claiming: a crash or native resume failure must not
                # strand the latest claimant without its existing thread handle.
                self.registry.update_meta(self.run_id, session_handle=dict(self.handle))
            atomic_write_json(state_path, {"latest_run_id": self.run_id})
        except BlockingIOError as e:
            self.close()
            raise RegistryError("session is already in use; no instruction was sent") from e
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None


def main(argv: Optional[list] = None) -> int:
    """`preflight`: refuse an impossible continuation in the launching process."""
    p = argparse.ArgumentParser(prog="steer-session")
    sub = p.add_subparsers(dest="cmd", required=True)
    pre = sub.add_parser("preflight")
    pre.add_argument("--agent-id", required=True)
    pre.add_argument("--continue-run", required=True)
    pre.add_argument("--cwd", default=os.getcwd())
    pre.add_argument("--recover-session", action="store_true")
    pre.add_argument("--registry-root", default="")
    args = p.parse_args(argv)
    registry = Registry(Path(args.registry_root) if args.registry_root else None)
    notices: List[str] = []
    try:
        plan_continuation(registry, args.continue_run, agent_id=args.agent_id, cwd=args.cwd,
                          recover_session=args.recover_session, notices=notices)
    except (RegistryError, KeyError, ValueError, OSError) as e:
        message = e.args[0] if isinstance(e, KeyError) and e.args else str(e)
        sys.stderr.write(f"Error: cannot continue {args.continue_run}: {message}\n")
        return e.exit_code if isinstance(e, RegistryError) else 5
    for line in notices:
        sys.stderr.write(line + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
