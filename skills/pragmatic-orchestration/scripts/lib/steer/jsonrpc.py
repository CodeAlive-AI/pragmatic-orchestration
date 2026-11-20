"""Minimal concurrent JSON-RPC / NDJSON line protocol helpers (stdlib)."""
from __future__ import annotations

import json
import queue
import subprocess
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from .util import detached_popen_kwargs, resolve_argv, kill_process_group

# What a wire classifier answers for a received message.
HOLD = "hold"    # superseded by the next message with the same key: keep the last
FLUSH = "flush"  # a boundary: write what is held, then this message


class RawWire:
    """The saved wire log of one backend process (``raw/<agent>.jsonl``).

    Every line is written as it arrives, with one exception. A backend may
    send messages that each replace the previous one (Codex repeats the whole
    turn diff after every edit); a classifier names those, and the log keeps
    only the last per key, written at the next boundary or when the log
    closes. A supervisor killed outright loses the held messages of the
    unfinished turn and nothing else.
    """

    def __init__(self, fp, classify: Optional[Callable[[Dict[str, Any]], Optional[Tuple[str, str]]]] = None):
        self._fp = fp
        self._classify = classify
        self._held: Dict[str, str] = {}
        self._lock = threading.Lock()

    def write(self, line: str, msg: Optional[Dict[str, Any]] = None) -> None:
        with self._lock:
            if self._fp is None:
                return
            if self._classify is not None and isinstance(msg, dict):
                try:
                    verdict = self._classify(msg)
                except Exception:
                    verdict = None  # a classifier fault must not lose the line
                if verdict:
                    action, key = verdict
                    if action == HOLD:
                        self._held[key] = line
                        return
                    if action == FLUSH:
                        self._write_held()
            self._emit(line)

    def flush_held(self) -> None:
        with self._lock:
            if self._fp is not None:
                self._write_held()

    def close(self) -> None:
        with self._lock:
            if self._fp is None:
                return
            try:
                self._write_held()
                self._fp.close()
            except Exception:
                pass
            self._fp = None

    def _write_held(self) -> None:
        held, self._held = self._held, {}
        for line in held.values():
            self._emit(line)

    def _emit(self, line: str) -> None:
        self._fp.write(line if line.endswith("\n") else line + "\n")
        self._fp.flush()


class JsonRpcProcess:
    """
    Supervise a child process speaking newline-delimited JSON-RPC 2.0.
    Matches request IDs; supports concurrent in-flight requests (needed for
    Grok ACP concurrent session/prompt).
    """

    def __init__(
        self,
        argv: List[str],
        *,
        cwd: Optional[str] = None,
        env: Optional[Dict[str, str]] = None,
        on_notification: Optional[Callable[[Dict[str, Any]], None]] = None,
        on_server_request: Optional[Callable[[Dict[str, Any]], Optional[Dict[str, Any]]]] = None,
    ):
        self.argv = argv
        self.cwd = cwd
        self.env = env
        self.on_notification = on_notification
        self.on_server_request = on_server_request
        self.proc: Optional[subprocess.Popen] = None
        self._id = 0
        self._pending: Dict[Any, "queue.Queue[Dict[str, Any]]"] = {}
        self._lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._reader: Optional[threading.Thread] = None
        self._stderr_thread: Optional[threading.Thread] = None
        self.stderr_lines: List[str] = []
        self.raw_out_path: Optional[str] = None
        self._raw: Optional[RawWire] = None
        self._closed = False
        self.notifications: "queue.Queue[Dict[str, Any]]" = queue.Queue()
        self._exit_code: Optional[int] = None

    def start(self) -> None:
        popen_kwargs: Dict[str, Any] = dict(
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=self.cwd,
            env=self.env,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
        popen_kwargs.update(detached_popen_kwargs())
        self.proc = subprocess.Popen(resolve_argv(self.argv, cwd=self.cwd, env=self.env), **popen_kwargs)
        self._reader = threading.Thread(target=self._read_stdout, daemon=True)
        self._reader.start()
        self._stderr_thread = threading.Thread(target=self._read_stderr, daemon=True)
        self._stderr_thread.start()

    def set_raw_out(self, path: str, classify=None) -> None:
        """Save the wire to ``path``; ``classify`` names superseded messages (see RawWire)."""
        self.raw_out_path = path
        self._raw = RawWire(open(path, "a", encoding="utf-8"), classify)

    def set_raw_out_fp(self, fp) -> None:
        """Attach an already-open file-like object for raw wire capture."""
        self._raw = RawWire(fp)

    def _write_raw(self, line: str, msg: Optional[Dict[str, Any]] = None) -> None:
        if self._raw:
            self._raw.write(line, msg)

    def _read_stdout(self) -> None:
        try:
            self._read_stdout_lines()
        finally:
            # The backend is gone: nothing can supersede what is held.
            if self._raw:
                self._raw.flush_held()

    def _read_stdout_lines(self) -> None:
        assert self.proc and self.proc.stdout
        for line in self.proc.stdout:
            line = line.rstrip("\n")
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                self._write_raw(line)
                self.notifications.put({"method": "_non_json", "params": {"line": line}})
                continue
            self._write_raw(line, msg)
            # Response to our request
            if "id" in msg and ("result" in msg or "error" in msg) and "method" not in msg:
                rid = msg["id"]
                with self._lock:
                    q = self._pending.get(rid)
                if q is not None:
                    q.put(msg)
                else:
                    self.notifications.put(msg)
                continue
            # Server request (has method + id) — answer if handler provided
            if "method" in msg and "id" in msg:
                resp = None
                if self.on_server_request:
                    try:
                        resp = self.on_server_request(msg)
                    except Exception as e:
                        resp = {
                            "jsonrpc": "2.0",
                            "id": msg["id"],
                            "error": {"code": -32000, "message": str(e)},
                        }
                if resp is None:
                    resp = {
                        "jsonrpc": "2.0",
                        "id": msg["id"],
                        "error": {"code": -32601, "message": f"Unsupported server request: {msg.get('method')}"},
                    }
                try:
                    self.write_line(resp)
                except Exception:
                    pass
                continue
            # Notification
            self.notifications.put(msg)
            if self.on_notification:
                try:
                    self.on_notification(msg)
                except Exception:
                    pass
        rc = self.proc.wait() if self.proc else 1
        self._exit_code = rc
        # A dead child will never answer; fail in-flight waiters immediately
        # instead of letting them block for the full request timeout.
        with self._lock:
            pending = list(self._pending.items())
            self._pending.clear()
        for rid, q in pending:
            try:
                q.put_nowait(
                    {"id": rid, "error": {"code": -32000,
                                          "message": f"process exited rc={rc}"}}
                )
            except queue.Full:
                pass

    def _read_stderr(self) -> None:
        assert self.proc and self.proc.stderr
        for line in self.proc.stderr:
            self.stderr_lines.append(line.rstrip("\n"))

    def next_id(self) -> int:
        with self._lock:
            self._id += 1
            return self._id

    def start_request(
        self, method: str, params: Optional[Dict[str, Any]] = None
    ) -> Tuple[Any, "queue.Queue[Dict[str, Any]]"]:
        """Send a request without waiting; return (id, response_queue)."""
        if not self.proc or not self.proc.stdin:
            raise RuntimeError("process not started")
        rid = self.next_id()
        q: "queue.Queue[Dict[str, Any]]" = queue.Queue(maxsize=1)
        with self._lock:
            self._pending[rid] = q
        msg: Dict[str, Any] = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            msg["params"] = params
        line = json.dumps(msg, ensure_ascii=False) + "\n"
        with self._write_lock:
            self.proc.stdin.write(line)
            self.proc.stdin.flush()
        self._write_raw(">> " + line.rstrip("\n"))
        return rid, q

    def await_response(
        self,
        rid: Any,
        q: "queue.Queue[Dict[str, Any]]",
        *,
        timeout: Optional[float] = None,
        method: str = "",
    ) -> Any:
        try:
            if timeout is None:
                resp = q.get()
            else:
                resp = q.get(timeout=timeout)
        except queue.Empty as e:
            with self._lock:
                self._pending.pop(rid, None)
            raise TimeoutError(f"JSON-RPC timeout method={method} id={rid}") from e
        with self._lock:
            self._pending.pop(rid, None)
        if "error" in resp:
            err = resp["error"]
            raise RuntimeError(f"JSON-RPC error method={method}: {err}")
        return resp.get("result")

    def drop_pending(self, rid: Any) -> None:
        """Discard a pending request entry after a manual queue wait."""
        with self._lock:
            self._pending.pop(rid, None)

    def request(
        self,
        method: str,
        params: Optional[Dict[str, Any]] = None,
        timeout: Optional[float] = None,
    ) -> Any:
        rid, q = self.start_request(method, params)
        return self.await_response(rid, q, timeout=timeout, method=method)

    def notify(self, method: str, params: Optional[Dict[str, Any]] = None) -> None:
        if not self.proc or not self.proc.stdin:
            raise RuntimeError("process not started")
        msg: Dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        line = json.dumps(msg, ensure_ascii=False) + "\n"
        with self._write_lock:
            self.proc.stdin.write(line)
            self.proc.stdin.flush()
        self._write_raw(">> " + line.rstrip("\n"))

    def write_line(self, obj: Dict[str, Any]) -> None:
        if not self.proc or not self.proc.stdin:
            raise RuntimeError("process not started")
        line = json.dumps(obj, ensure_ascii=False) + "\n"
        with self._write_lock:
            self.proc.stdin.write(line)
            self.proc.stdin.flush()
        self._write_raw(">> " + line.rstrip("\n"))

    def drain_notifications(self, max_items: int = 1000) -> List[Dict[str, Any]]:
        out = []
        for _ in range(max_items):
            try:
                out.append(self.notifications.get_nowait())
            except queue.Empty:
                break
        return out

    def poll_exit(self) -> Optional[int]:
        if self._exit_code is not None:
            return self._exit_code
        if not self.proc:
            return None
        return self.proc.poll()

    def pid(self) -> Optional[int]:
        return self.proc.pid if self.proc else None

    def terminate(self) -> None:
        if self._closed:
            return
        if self.proc and self.proc.poll() is None:
            # Capture and stop the group while its leader still exists. Closing
            # stdin or waiting for the parent first can orphan live tools.
            kill_process_group(self.proc.pid, timeout=3.0, process=self.proc)
            try:
                if self.proc.stdin:
                    self.proc.stdin.close()
            except Exception:
                pass
            self.proc.wait(timeout=3)
        self._closed = True
        if self._raw:
            self._raw.close()
