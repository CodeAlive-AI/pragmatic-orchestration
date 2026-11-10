"""Antigravity CLI steerable adapter: `agy acp` same-turn steering + cancel.

Wire facts verified against agy 1.5.4 (live probes, 2026-11-10):
- A second `session/prompt` sent while a turn runs is injected into that
  turn (the server sends the user message to the same conversation without a
  new update stream); every in-flight request resolves when the turn ends.
- `session/cancel` (notification) resolves the running prompt with
  stopReason "cancelled"; the next prompt keeps the conversation context.
- Model and thinking level are per-session config options:
  `session/set_config_option {configId: "model"}` accepts a family
  (`gemini-4-argon`) or an effort-suffixed id (`gemini-4-argon-high`);
  `{configId: "thought_level"}` takes low|medium|high where the model offers
  it. Unknown values fail with JSON-RPC -32602, so a bad profile fails the
  launch instead of silently running the account's default model.
- Updates include `user_message_chunk` echoes of every prompt; they are
  progress only and never part of the answer.
- `--dangerously-skip-permissions` auto-approves tool calls for the
  full-access delegate; explicit deny rules in the user's settings still apply.
"""
from __future__ import annotations

from typing import Any, Dict, List

from .acp_turn import AcpTurnAdapter

# Capability chatter re-sent while skills load and on every config change.
_SILENT_UPDATES = frozenset({"available_commands_update", "config_option_update"})


class AgyAdapter(AcpTurnAdapter):
    backend_name = "antigravity-cli"
    raw_name = "agy"

    def _argv(self) -> List[str]:
        return [self.binary, "acp", "--dangerously-skip-permissions"]

    def _configure_session(self) -> None:
        assert self.rpc and self.session_id
        if self.model:
            self._set_option("model", self.model)
        if self.effort:
            self._set_option("thought_level", self.effort)

    def _set_option(self, config_id: str, value: str) -> None:
        assert self.rpc and self.session_id
        try:
            self.rpc.request(
                "session/set_config_option",
                {"sessionId": self.session_id, "configId": config_id, "value": value},
                timeout=30.0,
            )
        except Exception as e:
            raise RuntimeError(f"agy rejected {config_id}={value!r}: {e}") from e

    def _on_session_update(self, params: Dict[str, Any]) -> None:
        update = params.get("update") if isinstance(params.get("update"), dict) else {}
        if (update.get("sessionUpdate") or update.get("type")) in _SILENT_UPDATES:
            return
        super()._on_session_update(params)
