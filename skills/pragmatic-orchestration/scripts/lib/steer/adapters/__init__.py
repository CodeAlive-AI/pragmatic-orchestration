from .agy import AgyAdapter
from .base import AdapterEvent, BackendAdapter, DeliveryClass, SteerResult
from .claude import ClaudeAdapter
from .codex import CodexAdapter
from .devin import DevinAdapter
from .grok import GrokAdapter
from .opencode import OpenCodeAdapter

ADAPTERS = {
    "antigravity-cli": AgyAdapter,
    "claude-code": ClaudeAdapter,
    "codex-cli": CodexAdapter,
    "devin-cli": DevinAdapter,
    "opencode": OpenCodeAdapter,
    "grok-build": GrokAdapter,
}


def make_adapter(backend: str, **kwargs) -> BackendAdapter:
    cls = ADAPTERS.get(backend)
    if not cls:
        raise ValueError(f"no steerable adapter for backend: {backend}")
    return cls(**kwargs)


__all__ = [
    "AdapterEvent",
    "BackendAdapter",
    "DeliveryClass",
    "SteerResult",
    "make_adapter",
    "ADAPTERS",
    "AgyAdapter",
    "ClaudeAdapter",
    "CodexAdapter",
    "DevinAdapter",
    "OpenCodeAdapter",
    "GrokAdapter",
]
