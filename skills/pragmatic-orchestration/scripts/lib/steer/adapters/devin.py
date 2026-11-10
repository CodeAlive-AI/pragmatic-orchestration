"""Devin steerable adapter: `devin acp` concurrent same-turn prompts + cancel.

Wire facts verified against devin 3000.10.27 (/tmp probe transcripts):
- A second `session/prompt` sent while a turn runs is accepted and merged into
  the SAME turn; both request ids get their response when that turn ends.
  There is no promptId echo — correlate by JSON-RPC request id only.
- `session/cancel` resolves in-flight prompts with stopReason "cancelled" and
  the session stays usable (interrupt = cancel_and_send).
- Final text is the concatenation of `agent_message_chunk` updates only;
  `agent_thought_chunk` is observable but never part of the answer.
- `ACP_BACKEND` must be stripped from the child env (inside Devin Desktop it
  is inherited as "windsurf" and the CLI reports "Not logged in").

The shared turn, steer and cancel logic lives in acp_turn.AcpTurnAdapter.
"""
from __future__ import annotations

import os
from typing import Any, Dict, List

from .acp_turn import AcpTurnAdapter
from .base import AdapterEvent


class DevinAdapter(AcpTurnAdapter):
    backend_name = "devin-cli"
    raw_name = "devin"

    def _argv(self) -> List[str]:
        cmd = [self.binary, "acp"]
        if self.model:
            cmd += ["--model", self.model]
        return cmd

    def _child_env(self) -> Dict[str, str]:
        env = dict(os.environ)
        env.pop("ACP_BACKEND", None)
        return env

    def _configure_session(self) -> None:
        # Steerable delegate is yolo: bypass so exec/permissions never block.
        assert self.rpc and self.session_id
        try:
            self.rpc.request(
                "session/set_mode",
                {"sessionId": self.session_id, "modeId": "bypass"},
                timeout=30.0,
            )
        except Exception as e:
            self._events.put(
                AdapterEvent(kind="progress", data=f"set_mode_bypass_failed:{e}")
            )

    def _on_extension_notification(self, method: str, params: Dict[str, Any],
                                   msg: Dict[str, Any]) -> bool:
        if method != "_cognition.ai/agent_stopped":
            return False
        cause = str(params.get("cause") or "")
        if cause and cause != "complete":
            self._events.put(AdapterEvent(kind="progress", data=f"agent_stopped:{cause}", raw=msg))
        else:
            self._events.put(AdapterEvent(kind="progress", data="agent_stopped", raw=msg))
        return True
