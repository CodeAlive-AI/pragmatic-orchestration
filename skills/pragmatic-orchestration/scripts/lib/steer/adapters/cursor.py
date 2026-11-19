"""Cursor CLI steerable adapter: one print-mode process per turn.

`cursor-agent -p` exits after a single prompt. A follow-up is a new process
with `--resume <session_id>`, queued while the current turn is still running.
Interrupt is not supported. Review mode is not this adapter: the supervisor
path is delegate, so every turn passes `--force` and never `--mode plan`.
"""
from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

_LIB = Path(__file__).resolve().parents[2]
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

import cursor_config
from normalize_stream import normalize_cursor
from redact_stream import redact
from ..util import detached_popen_kwargs, kill_process_group, resolve_argv
from .base import AdapterEvent, BackendAdapter, DeliveryClass, SteerResult

_STDERR_TAIL_LIMIT = 8192
_STDERR_REASON_LIMIT = 300


class CursorAdapter(BackendAdapter):
    backend_name = "cursor-cli"
    auto_delivery = DeliveryClass.QUEUE_NEXT_TURN

    def __init__(self, **kwargs: Any):
        super().__init__(**kwargs)
        self.persist_session = False
        self.on_session_ready = None
        self.resume_thread_id = ""
        self.proc: Optional[subprocess.Popen] = None
        self._events: "queue.Queue[AdapterEvent]" = queue.Queue()
        self._lock = threading.RLock()
        self._queue: List[Dict[str, str]] = []
        self._inflight: Optional[str] = None
        self._gen = 0
        self._finished: set[int] = set()
        self._result_text: Optional[str] = None
        self._final_parts: List[str] = []
        self._cancelled = False
        self._session_announced = False
        self._reader: Optional[threading.Thread] = None
        self._stderr_reader: Optional[threading.Thread] = None
        self._stderr_tail = ""
        self._norm_state: Dict[str, Any] = {}
        # An isolated CURSOR_CONFIG_DIR keeps --model from rewriting the
        # user's default model in ~/.cursor/cli-config.json (cursor-agent
        # persists model.modelId even when the run fails).
        self._config_dir: Optional[str] = None

    def _argv(self, resume: bool) -> List[str]:
        cmd = [
            self.binary, "-p", "--trust",
            "--output-format", "stream-json",
            "--stream-partial-output",
            "--force",
            "--workspace", self.cwd or os.getcwd(),
        ]
        if self.model:
            cmd.extend(["--model", self.model])
        if resume:
            session_id = self.session_id or self.resume_thread_id
            if not session_id:
                raise RuntimeError("cursor-cli resume requested without a session id")
            cmd.extend(["--resume", session_id])
        return cmd

    def start(self, task: str) -> None:
        if self._config_dir is None:
            self._config_dir, link_warnings = cursor_config.prepare()
            for name in link_warnings:
                self._events.put(AdapterEvent(
                    kind="progress", data=f"cursor config entry not isolated: {name}"))
        self._spawn(task, resume=bool(self.resume_thread_id))

    def _spawn(self, prompt: str, *, resume: bool) -> None:
        with self._lock:
            self._gen += 1
            gen = self._gen
            self._done = False
            self._result_text = None
            self._final_parts = []
            self._error = None
            self._stderr_tail = ""
            self._norm_state = {}
        argv = resolve_argv(self._argv(resume), cwd=self.cwd)
        env = dict(os.environ)
        if self._config_dir:
            env["CURSOR_CONFIG_DIR"] = self._config_dir
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=self.cwd or None,
            env=env,
            text=True,
            bufsize=1,
            **detached_popen_kwargs(),
        )
        self.proc = proc
        # Drain stderr so a chatty process cannot block on a full pipe; the
        # bounded tail explains a failed exit (e.g. ActionRequiredError).
        self._stderr_reader = threading.Thread(
            target=self._drain_stderr, args=(proc,), daemon=True)
        self._stderr_reader.start()
        try:
            assert proc.stdin is not None
            proc.stdin.write(prompt)
            proc.stdin.close()
        except Exception as exc:
            with self._lock:
                self._error = f"cursor-cli stdin failed: {exc}"
                self._done = True
                self._exit_code = 1
                self._finished.add(gen)
            self._events.put(AdapterEvent(kind="error", data=self._error))
            self._events.put(AdapterEvent(kind="done", data=""))
            kill_process_group(proc.pid, timeout=3.0)
            return
        reader = threading.Thread(target=self._read, args=(gen, proc), daemon=True)
        self._reader = reader
        reader.start()

    def _drain_stderr(self, proc: subprocess.Popen) -> None:
        tail = ""
        try:
            if proc.stderr is not None:
                for chunk in proc.stderr:
                    tail = (tail + chunk)[-_STDERR_TAIL_LIMIT:]
        finally:
            if proc.stderr is not None:
                proc.stderr.close()
            with self._lock:
                self._stderr_tail = tail

    def _stderr_reason(self) -> str:
        """Bounded, redacted last stderr line for a failed process exit."""
        tail = (self._stderr_tail or "").strip()
        if not tail:
            return ""
        try:
            tail = redact(tail)
        except Exception:
            pass
        lines = [ln.strip() for ln in tail.splitlines() if ln.strip()]
        return (lines[-1] if lines else "")[:_STDERR_REASON_LIMIT]

    def _read(self, gen: int, proc: subprocess.Popen) -> None:
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                if gen != self._gen:
                    break
                self._on_line(line)
        finally:
            if proc.stdout is not None:
                proc.stdout.close()
            rc = proc.wait()
            # Let the stderr tail arrive so a failed exit can surface its reason.
            if self._stderr_reader is not None:
                self._stderr_reader.join(timeout=2.0)
            self._finish_turn(gen, rc if isinstance(rc, int) else 1)

    def _on_line(self, line: str) -> None:
        raw_line = line.strip()
        if not raw_line:
            return
        try:
            obj = json.loads(raw_line)
        except json.JSONDecodeError:
            self._events.put(AdapterEvent(kind="progress", data="non-json"))
            return
        if not isinstance(obj, dict):
            self._events.put(AdapterEvent(kind="progress", data="non-object"))
            return
        stream_type, data = normalize_cursor(obj, self._norm_state)
        text = data if isinstance(data, str) else ""
        if obj.get("type") == "result" and isinstance(obj.get("session_id"), str):
            self._note_session(obj["session_id"])
        if stream_type == "text" and text:
            with self._lock:
                self._final_parts.append(text)
            self._events.put(AdapterEvent(kind="text", data=text, raw=obj))
        elif stream_type == "thought" and text:
            self._events.put(AdapterEvent(kind="thought", data=text, raw=obj))
        elif stream_type == "tool_started":
            self._events.put(AdapterEvent(kind="tool", data=text or "tool", raw=obj))
        elif stream_type == "result":
            if text.strip():
                with self._lock:
                    self._result_text = text
            self._events.put(AdapterEvent(kind="progress", data="result", raw=obj))
        elif stream_type == "error":
            with self._lock:
                self._error = text or "cursor-cli result error"
            self._events.put(AdapterEvent(kind="error", data=self._error or "", raw=obj))
        else:
            self._events.put(AdapterEvent(kind="progress", data=text or stream_type, raw=obj))

    def _note_session(self, session_id: str) -> None:
        announce = False
        with self._lock:
            if not self.session_id:
                self.session_id = session_id
                announce = not self._session_announced
                self._session_announced = True
        if announce and self.on_session_ready:
            self.on_session_ready(session_id)

    def _finish_turn(self, gen: int, rc: int) -> None:
        prompt = ""
        with self._lock:
            if gen != self._gen or gen in self._finished:
                return
            self._finished.add(gen)
            if self._cancelled:
                self._done = True
                self._exit_code = 130 if self._exit_code in (None, 0) else self._exit_code
                return
            if self._error or rc != 0:
                if rc != 0 and not self._error:
                    self._error = f"cursor-agent exited {rc}"
                    detail = self._stderr_reason()
                    if detail:
                        self._error += f": {detail}"
                    self._events.put(AdapterEvent(kind="error", data=self._error))
                self._exit_code = rc if rc != 0 else 1
                self._fail_queued("turn_failed")
                self._done = True
                self._events.put(AdapterEvent(kind="done", data=""))
                return
            finished = self._inflight
            self._inflight = None
            if finished:
                self._events.put(AdapterEvent(
                    kind="steer_ack", data=finished,
                    raw={"promptId": finished, "status": "completed", "evidence": "resume_result"},
                ))
            if self._queue and (self.session_id or self.resume_thread_id):
                item = self._queue.pop(0)
                self._inflight = item["client_id"]
                prompt = item["content"]
            else:
                if self._queue and not (self.session_id or self.resume_thread_id):
                    self._fail_queued("no_session_id")
                self._done = True
                self._exit_code = 0
                self._events.put(AdapterEvent(kind="done", data=self._result_text or ""))
                return
        self._spawn(prompt, resume=True)

    def _fail_queued(self, evidence: str) -> None:
        pending = self._inflight
        self._inflight = None
        queued = list(self._queue)
        self._queue.clear()
        ids = ([pending] if pending else []) + [item["client_id"] for item in queued]
        for client_id in ids:
            self._events.put(AdapterEvent(
                kind="steer_ack", data=client_id,
                raw={"promptId": client_id, "status": "failed", "evidence": evidence},
            ))

    def poll_events(self) -> Any:
        while True:
            try:
                yield self._events.get_nowait()
            except queue.Empty:
                break

    def _queue_class(self) -> DeliveryClass:
        return DeliveryClass.QUEUE_NEXT_TURN

    def _interrupt_class(self) -> DeliveryClass:
        raise NotImplementedError("cursor-cli does not support interrupt")

    def steer(self, content: str, mode: str, client_id: str) -> SteerResult:
        with self._lock:
            if self._done or self._cancelled:
                return SteerResult(
                    ok=False, delivery_class=self.auto_delivery, status="rejected",
                    error="backend already completed",
                )
            try:
                dclass = self.map_mode(mode)
            except NotImplementedError as exc:
                return SteerResult(
                    ok=False, delivery_class=self.auto_delivery, status="rejected",
                    error=str(exc),
                )
            self._queue.append({"client_id": client_id, "content": content})
            return SteerResult(
                ok=True, delivery_class=dclass, status="queued",
                evidence="queued_for_resume", meta={"promptId": client_id},
            )

    def child_pid(self) -> Optional[int]:
        return self.proc.pid if self.proc else None

    def cancel(self) -> None:
        with self._lock:
            self._cancelled = True
            self._done = True
            self._exit_code = 130
            proc = self.proc
        if proc and proc.poll() is None:
            kill_process_group(proc.pid, timeout=3.0)

    def final_text(self) -> str:
        if self._result_text is not None and self._result_text.strip():
            return self._result_text
        return "".join(self._final_parts)

    def close(self) -> None:
        self.cancel()
        if self._config_dir:
            shutil.rmtree(self._config_dir, ignore_errors=True)
            self._config_dir = None
