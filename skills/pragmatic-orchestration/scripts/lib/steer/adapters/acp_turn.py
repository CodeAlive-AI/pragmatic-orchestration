"""Shared ACP stdio adapter for backends that merge mid-turn prompts.

`session/prompt` sent while a turn runs is accepted and merged into the SAME
turn; every in-flight request id gets its own response when that turn ends.
`session/cancel` resolves in-flight prompts with stopReason "cancelled" and the
session stays usable, so interrupt is cancel_and_send. There is no promptId
echo — correlate by JSON-RPC request id only. Final text is the concatenation
of `agent_message_chunk` updates; `agent_thought_chunk` and
`user_message_chunk` are observable but never part of the answer.

Honest ack ladder: `request_sent` when the JSON-RPC request is in flight;
`completed` / `cancelled` / `failed` once the request's own response resolves.
No ACP transport event proves semantic compliance, so adapters never claim
`applied` — they report only wire-observable lifecycle facts.

Background work (opt-in, `tracks_background_work`): some agents end a turn
with `end_turn` while a tool call they moved to the background is still
`in_progress`, then continue by themselves through unsolicited
`session/update`s once it finishes — no new prompt, no new response, no end
marker. With tracking on, such a stop does not finish the run. The run settles
when no prompt is in flight, no non-daemon tool call is open, nothing closed
since the last model call (`usage_update`), the newest update is a
`usage_update`, and SETTLE seconds passed quietly (`usage_settled`); without
that marker it settles after IDLE quiet seconds with no open tool call
(`idle_timeout`). An open background tool call is waited for without a cap.

Subclasses provide the argv, child environment and per-session configuration.
"""
from __future__ import annotations

import os
import queue
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from ..jsonrpc import JsonRpcProcess
from ..util import kill_process_group
from .base import AdapterEvent, BackendAdapter, DeliveryClass, SteerResult

_CANCEL_STOP_REASONS = frozenset({"cancelled", "canceled"})
_SUCCESS_STOP_REASONS = frozenset({"end_turn", "endturn"})
_INTERRUPT_WAIT_S = 30.0
# ACP tool call statuses are pending|in_progress|completed|failed; "cancelled"
# is accepted defensively. An unknown status never closes a call.
_TOOL_CLOSED_STATUSES = frozenset({"completed", "failed", "cancelled", "canceled"})
# Capability chatter that is not agent activity.
_INACTIVE_UPDATES = frozenset({"available_commands_update", "config_option_update",
                               "session_info_update"})


def _env_seconds(name: str, default: float) -> float:
    try:
        return max(0.0, float(os.environ.get(name, "") or default))
    except ValueError:
        return default


def _normalize_stop_reason(value: Any) -> str:
    return str(value or "").strip().replace("-", "_").lower()


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        if content.get("type") == "text" or "text" in content:
            return str(content.get("text") or "")
        if "content" in content:
            return _content_text(content["content"])
    if isinstance(content, list):
        return "".join(_content_text(c) for c in content)
    return ""


class AcpTurnAdapter(BackendAdapter):
    backend_name = "acp"
    raw_name = "acp"
    auto_delivery = DeliveryClass.SAME_TURN
    # Off by default; a subclass turns it on (see the module docstring).
    tracks_background_work = False

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.rpc: Optional[JsonRpcProcess] = None
        self.session_id: Optional[str] = None
        self._events: "queue.Queue[AdapterEvent]" = queue.Queue()
        self._raw_path = Path(self.artifacts_dir) / "raw" / f"{self.agent_id or self.raw_name}.jsonl"
        self._lock = threading.Lock()
        # rid -> (prompt client_id, waiter thread)
        self._in_flight: Dict[Any, Dict[str, Any]] = {}
        self._prompt_result: Dict[Any, Dict[str, Any]] = {}
        self._resolution_order: List[Any] = []
        self._initial_rid: Optional[Any] = None
        self._all_text: List[str] = []
        self._cancelled = False
        # Interrupt steers in flight between session/cancel and the successor
        # prompt: the cancelled turn resolving must not terminalize the run.
        self._pending_interrupt = 0
        self.agent_info: Dict[str, Any] = {}
        # Background-work tracking (reader thread writes, under _lock).
        self._open_tools: Dict[str, str] = {}  # toolCallId -> title
        self._update_seq = 0
        self._last_close_seq = 0
        self._last_usage_seq = 0
        self._last_activity = time.time()
        self._awaiting_background = False
        self._continuation_text = False
        self._settle_s = _env_seconds("PORCH_ACP_BACKGROUND_SETTLE_S", 30.0)
        self._idle_s = _env_seconds("PORCH_ACP_BACKGROUND_IDLE_S", 300.0)

    # ---- backend hooks --------------------------------------------------

    def _is_daemon_tool(self, update: Dict[str, Any]) -> bool:
        """True for a tool call the agent itself does not wait for."""
        return False

    def _close_session(self) -> None:
        """Ask the agent to stop session work before the process is killed."""

    def _argv(self) -> List[str]:
        raise NotImplementedError

    def _child_env(self) -> Dict[str, str]:
        return dict(os.environ)

    def _configure_session(self) -> None:
        """Apply mode/model settings after session/new, before the task."""

    def _on_extension_notification(self, method: str, params: Dict[str, Any],
                                   msg: Dict[str, Any]) -> bool:
        """Handle a backend-specific notification; return True when consumed."""
        return False

    def _emit_steer_ack(self, prompt_id: str, *, status: str, evidence: str,
                        raw: Optional[Dict[str, Any]] = None) -> None:
        if not prompt_id:
            return
        payload: Dict[str, Any] = {
            "promptId": prompt_id,
            "status": status,
            "evidence": evidence,
        }
        if raw is not None:
            payload["raw"] = raw
        self._events.put(AdapterEvent(kind="steer_ack", data=f"{status}:{evidence}", raw=payload))

    # ---- wire callbacks -------------------------------------------------

    def _on_notification(self, msg: Dict[str, Any]) -> None:
        method = msg.get("method") or ""
        params = msg.get("params") or {}
        if method == "session/update":
            self._on_session_update(params if isinstance(params, dict) else {})
            return
        if self._on_extension_notification(method, params if isinstance(params, dict) else {}, msg):
            return
        self._events.put(AdapterEvent(kind="progress", data=method or "notification", raw=msg))

    def _on_session_update(self, params: Dict[str, Any]) -> None:
        update = params.get("update") or {}
        if not isinstance(update, dict):
            update = {}
        utype = update.get("sessionUpdate") or update.get("type") or ""
        if self.tracks_background_work and utype not in _INACTIVE_UPDATES:
            self._track_update(utype, update)
        if utype == "agent_message_chunk":
            text = _content_text(update.get("content"))
            if text:
                with self._lock:
                    if self._awaiting_background and not self._in_flight \
                            and not self._continuation_text:
                        # First answer text of an unsolicited continuation.
                        self._continuation_text = True
                        if self._all_text:
                            self._all_text.append("\n\n")
                    self._all_text.append(text)
                self._events.put(AdapterEvent(kind="text", data=text,
                                              raw={"update": update, "params": params}))
            else:
                self._events.put(AdapterEvent(kind="progress", data=utype,
                                              raw={"update": update, "params": params}))
            return
        if utype == "agent_thought_chunk":
            self._events.put(AdapterEvent(kind="thought",
                                          data=_content_text(update.get("content")),
                                          raw={"update": update, "params": params}))
            return
        if utype == "tool_call":
            self._events.put(AdapterEvent(
                kind="tool",
                data=str(update.get("title") or update.get("kind") or "tool"),
                raw={"update": update, "params": params}))
            return
        self._events.put(AdapterEvent(kind="progress", data=utype or "session/update",
                                      raw={"update": update, "params": params}))

    def _track_update(self, utype: str, update: Dict[str, Any]) -> None:
        changed = None
        with self._lock:
            self._update_seq += 1
            self._last_activity = time.time()
            call_id = str(update.get("toolCallId") or "")
            status = str(update.get("status") or "").lower()
            if utype == "usage_update":
                self._last_usage_seq = self._update_seq
            elif utype == "tool_call" and call_id:
                if status in _TOOL_CLOSED_STATUSES:
                    self._last_close_seq = self._update_seq
                elif not self._is_daemon_tool(update):
                    self._open_tools[call_id] = str(update.get("title") or update.get("kind") or "tool")
                    changed = True
            elif utype == "tool_call_update" and call_id and status in _TOOL_CLOSED_STATUSES:
                self._last_close_seq = self._update_seq
                if self._open_tools.pop(call_id, None) is not None:
                    changed = True
            if changed and self._awaiting_background:
                changed = self._background_summary_locked()
            else:
                changed = None
        if changed:
            self._events.put(AdapterEvent(kind="progress", data=changed))

    def _background_summary_locked(self) -> str:
        if not self._open_tools:
            return "awaiting_background_continuation"
        names = ", ".join(f"{t} ({cid})" for cid, t in list(self._open_tools.items())[:5])
        return f"awaiting_background_tools: {names}"

    def _background_pending_locked(self) -> bool:
        """Work the agent will still do on its own after the last stop."""
        return bool(self._open_tools) or self._last_close_seq > self._last_usage_seq

    def _on_server_request(self, msg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        method = msg.get("method") or ""
        rid = msg.get("id")
        if self.tracks_background_work:
            with self._lock:
                self._last_activity = time.time()
                self._update_seq += 1
        if method == "session/request_permission":
            options = (msg.get("params") or {}).get("options") or []
            option_id = None
            fallback = None
            for o in options:
                if not isinstance(o, dict):
                    continue
                kind = str(o.get("kind") or "")
                if kind == "allow_once":
                    option_id = str(o.get("optionId") or "allow_once")
                    break
                if kind.startswith("allow") and fallback is None:
                    fallback = str(o.get("optionId") or "")
            option_id = option_id or fallback
            if option_id is None:
                return None
            return {
                "jsonrpc": "2.0",
                "id": rid,
                "result": {"outcome": {"outcome": "selected", "optionId": option_id}},
            }
        if method == "elicitation/create":
            return {"jsonrpc": "2.0", "id": rid, "result": {"action": "cancel"}}
        return None

    # ---- lifecycle ------------------------------------------------------

    def start(self, task: str) -> None:
        self._raw_path.parent.mkdir(parents=True, exist_ok=True)
        env = self._child_env()
        self.rpc = JsonRpcProcess(
            self._argv(),
            cwd=self.cwd,
            env=env,
            on_notification=self._on_notification,
            on_server_request=self._on_server_request,
        )
        self.rpc.set_raw_out(str(self._raw_path))
        self.rpc.start()
        init = self.rpc.request(
            "initialize",
            {
                "protocolVersion": 1,
                "clientCapabilities": {
                    "fs": {"readTextFile": False, "writeTextFile": False},
                    "terminal": False,
                },
                "clientInfo": {"name": "pragmatic-orchestration", "version": "1.0.0"},
            },
            timeout=60.0,
        )
        info = (init or {}).get("agentInfo") if isinstance(init, dict) else None
        self.agent_info = info if isinstance(info, dict) else {}
        result = self.rpc.request(
            "session/new", {"cwd": self.cwd, "mcpServers": []}, timeout=60.0
        )
        self.session_id = (result or {}).get("sessionId")
        if not self.session_id:
            raise RuntimeError(f"{self.backend_name} session/new missing sessionId: {result}")
        self._configure_session()
        self._send_prompt(task, client_id="initial")

    def _send_prompt(self, text: str, *, client_id: str) -> Any:
        assert self.rpc and self.session_id
        rid, q = self.rpc.start_request(
            "session/prompt",
            {
                "sessionId": self.session_id,
                "prompt": [{"type": "text", "text": text}],
            },
        )
        with self._lock:
            self._in_flight[rid] = {"client_id": client_id}
            if self._initial_rid is None:
                self._initial_rid = rid
        t = threading.Thread(target=self._wait_prompt, args=(rid, q, client_id), daemon=True)
        t.start()
        return rid

    def _wait_prompt(self, rid: Any, q: "queue.Queue[Dict[str, Any]]", client_id: str) -> None:
        assert self.rpc
        try:
            # Poll the response queue so a dead child cannot hang the waiter
            # forever. await_response pops the pending entry on timeout, so
            # wait on the queue directly instead of looping over it.
            result: Any = None
            while True:
                try:
                    resp = q.get(timeout=1.0)
                except queue.Empty:
                    if self.rpc.poll_exit() is not None or self._cancelled:
                        self.rpc.drop_pending(rid)
                        raise RuntimeError(f"{self.backend_name} acp exited before prompt response")
                    continue
                self.rpc.drop_pending(rid)
                if "error" in resp:
                    raise RuntimeError(f"JSON-RPC error: {resp['error']}")
                result = resp.get("result")
                break
        except Exception as e:
            with self._lock:
                self._in_flight.pop(rid, None)
                self._prompt_result.setdefault(rid, {})
                self._resolution_order.append(rid)
            self._events.put(AdapterEvent(kind="error", data=f"prompt {client_id}: {e}",
                                          raw={"promptId": client_id}))
            self._emit_steer_ack(client_id, status="failed", evidence="prompt_request_error",
                                 raw={"promptId": client_id, "error": str(e)})
            self._maybe_mark_done(rid)
            return
        stop = _normalize_stop_reason(
            result.get("stopReason") if isinstance(result, dict) else ""
        )
        with self._lock:
            self._in_flight.pop(rid, None)
            self._prompt_result[rid] = result if isinstance(result, dict) else {}
            self._resolution_order.append(rid)
        if stop in _SUCCESS_STOP_REASONS:
            status, evidence = "completed", "prompt_result_end_turn"
        elif stop in _CANCEL_STOP_REASONS:
            status, evidence = "cancelled", "prompt_result_cancelled"
        else:
            status, evidence = "failed", f"prompt_result_{stop or 'missing'}"
        self._emit_steer_ack(client_id, status=status, evidence=evidence,
                             raw={"promptId": client_id, "result": result})
        self._maybe_mark_done(rid)

    def _maybe_mark_done(self, resolved_rid: Any) -> None:
        with self._lock:
            if self._done:
                return
            if self._in_flight:
                return
            if self._pending_interrupt:
                return
            if resolved_rid != self._initial_rid and self._initial_rid not in self._prompt_result:
                # The initial prompt has not resolved yet.
                return
            last_rid = self._resolution_order[-1] if self._resolution_order else resolved_rid
            last_stop = _normalize_stop_reason(
                (self._prompt_result.get(last_rid) or {}).get("stopReason")
            )
            awaiting = None
            if (self.tracks_background_work and not self._cancelled
                    and last_stop in _SUCCESS_STOP_REASONS
                    and self._background_pending_locked()):
                # The agent ended its turn but will continue on its own.
                self._awaiting_background = True
                self._continuation_text = False
                awaiting = self._background_summary_locked()
            elif self._cancelled:
                exit_code, data = 130, "cancelled"
            elif last_stop in _SUCCESS_STOP_REASONS:
                exit_code, data = 0, "end_turn"
            elif last_stop in _CANCEL_STOP_REASONS:
                exit_code, data = 130, "cancelled"
            else:
                exit_code, data = 1, f"stop:{last_stop or 'missing'}"
            if awaiting is None:
                self._awaiting_background = False
                self._done = True
                self._exit_code = exit_code
        if awaiting is not None:
            self._events.put(AdapterEvent(kind="progress", data=awaiting))
            return
        self._events.put(AdapterEvent(kind="done", data=data,
                                      raw={"promptId": last_rid}))

    def _maybe_settle_background(self) -> None:
        """Finish a run whose agent continued after end_turn and went quiet."""
        with self._lock:
            if self._done or not self._awaiting_background:
                return
            if self._in_flight or self._pending_interrupt or self._open_tools:
                return
            quiet = time.time() - self._last_activity
            settled = (self._last_close_seq <= self._last_usage_seq
                       and self._last_usage_seq == self._update_seq)
            if settled and quiet >= self._settle_s:
                evidence = "usage_settled"
            elif quiet >= self._idle_s:
                evidence = "idle_timeout"
            else:
                return
            self._awaiting_background = False
            self._done = True
            self._exit_code = 0
        self._events.put(AdapterEvent(kind="done", data=f"end_turn:{evidence}",
                                      raw={"completion_evidence": evidence}))

    def poll_events(self) -> Iterator[AdapterEvent]:
        self._maybe_settle_background()
        if self.rpc:
            rc = self.rpc.poll_exit()
            if rc is not None and not self._done:
                with self._lock:
                    self._done = True
                    self._exit_code = 130 if self._cancelled else (rc or 1)
                    if rc != 0 and not self._error:
                        self._error = f"{self.backend_name} exited {rc}"
                self._events.put(AdapterEvent(kind="done", data=str(rc)))
        while True:
            try:
                yield self._events.get_nowait()
            except queue.Empty:
                break

    def _queue_class(self) -> DeliveryClass:
        return DeliveryClass.SAME_TURN

    def _interrupt_class(self) -> DeliveryClass:
        return DeliveryClass.CANCEL_AND_SEND

    def steer(self, content: str, mode: str, client_id: str) -> SteerResult:
        if not self.rpc or not self.session_id:
            return SteerResult(ok=False, delivery_class=self.auto_delivery,
                               status="rejected", error="backend not ready")
        with self._lock:
            if self._done and not self._in_flight:
                return SteerResult(ok=False, delivery_class=self.auto_delivery,
                                   status="rejected", error="backend already completed")
        try:
            dclass = self.map_mode(mode)
        except NotImplementedError as e:
            return SteerResult(ok=False, delivery_class=self.auto_delivery,
                               status="rejected", error=str(e))
        prompt_id = client_id or str(uuid.uuid4())
        if dclass == DeliveryClass.CANCEL_AND_SEND:
            with self._lock:
                self._pending_interrupt += 1
            try:
                try:
                    self.rpc.notify("session/cancel", {"sessionId": self.session_id})
                except Exception as e:
                    return SteerResult(ok=False, delivery_class=dclass, status="failed",
                                       error=f"session/cancel: {e}")
                # Wait (bounded) for in-flight prompts to resolve before sending
                # the successor — otherwise it would merge into the still-running
                # turn. _pending_interrupt keeps _maybe_mark_done from
                # terminalizing on the cancelled turn's resolution.
                deadline = time.time() + _INTERRUPT_WAIT_S
                while time.time() < deadline:
                    with self._lock:
                        if not self._in_flight:
                            break
                    time.sleep(0.05)
                try:
                    self._send_prompt(content, client_id=prompt_id)
                except Exception as e:
                    return SteerResult(ok=False, delivery_class=dclass, status="failed", error=str(e))
            finally:
                with self._lock:
                    self._pending_interrupt -= 1
        else:
            try:
                self._send_prompt(content, client_id=prompt_id)
            except Exception as e:
                return SteerResult(ok=False, delivery_class=dclass, status="failed", error=str(e))
        evidence = ("acp_cancel_then_prompt" if dclass == DeliveryClass.CANCEL_AND_SEND
                    else "acp_concurrent_prompt")
        return SteerResult(
            ok=True,
            delivery_class=dclass,
            status="request_sent",
            evidence=evidence,
            meta={"promptId": prompt_id, "client_id": client_id},
        )

    def child_pid(self) -> Optional[int]:
        return self.rpc.pid() if self.rpc else None

    def cancel(self) -> None:
        with self._lock:
            already_done = self._done
            self._cancelled = True
        if self.rpc and self.session_id:
            try:
                self.rpc.notify("session/cancel", {"sessionId": self.session_id})
            except Exception:
                pass
            if self.rpc.poll_exit() is None:
                try:
                    self._close_session()
                except Exception:
                    pass
            pid = self.rpc.pid()
            self.rpc.terminate()
            if pid:
                kill_process_group(pid, timeout=3.0)
        with self._lock:
            if not already_done and not self._done:
                self._done = True
                self._exit_code = 130
                self._events.put(AdapterEvent(kind="done", data="cancelled"))
            else:
                self._done = True
                if self._exit_code is None:
                    self._exit_code = 130

    def final_text(self) -> str:
        with self._lock:
            return "".join(self._all_text)

    def close(self) -> None:
        self.cancel()
