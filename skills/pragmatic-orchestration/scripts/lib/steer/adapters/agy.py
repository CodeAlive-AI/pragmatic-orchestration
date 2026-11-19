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

Background work (live probes on agy 1.5.12, 2026-11-20): a `run_command`
outlasting its `WaitMsBeforeAsync` (at most 10 s) moves to the background and
agy ends the turn with `end_turn` while that tool call stays `in_progress`.
Since 1.5.10 agy then continues by itself through unsolicited updates
(`tool_call_update` completed, new tool calls, text) when the command ends, so
background tracking is on from 1.5.10 (see acp_turn). `IsDaemon` tool calls
(dev servers, watchers, standing timers) are not waited for. `session/cancel`
with no prompt running fails the background tool call and stops the command;
`session/close` stops background tasks and subagents, so it is sent before the
process is killed.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Tuple

from .acp_turn import AcpTurnAdapter

# Capability chatter re-sent while skills load and on every config change.
_SILENT_UPDATES = frozenset({"available_commands_update", "config_option_update"})
# First version that delivers post-turn continuations without a new prompt.
_CONTINUATION_VERSION = (1, 5, 10)
_CLOSE_TIMEOUT_S = 5.0


def _version_tuple(value: Any) -> Tuple[int, ...]:
    m = re.match(r"\s*(\d+)\.(\d+)\.(\d+)", str(value or ""))
    return tuple(int(x) for x in m.groups()) if m else ()


class AgyAdapter(AcpTurnAdapter):
    backend_name = "antigravity-cli"
    raw_name = "agy"

    def _argv(self) -> List[str]:
        return [self.binary, "acp", "--dangerously-skip-permissions"]

    @property
    def tracks_background_work(self) -> bool:  # type: ignore[override]
        version = _version_tuple(self.agent_info.get("version"))
        return bool(version) and version >= _CONTINUATION_VERSION

    def _is_daemon_tool(self, update: Dict[str, Any]) -> bool:
        for key in ("rawInput", "args"):
            value = update.get(key)
            if isinstance(value, dict) and value.get("IsDaemon") is True:
                return True
        return False

    def _close_session(self) -> None:
        if not self.rpc or not self.session_id:
            return
        self.rpc.request("session/close", {"sessionId": self.session_id},
                         timeout=_CLOSE_TIMEOUT_S)

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
