"""AgentGuard — stop dangerous AI agent actions before they execute."""

from .core import (
    AgentGuard,
    ApprovalRequest,
    ActionDenied,
    ResolveOutcome,
    ApprovalStore,
    InMemoryStore,
    classify,
)

__all__ = [
    "AgentGuard",
    "ApprovalRequest",
    "ActionDenied",
    "ResolveOutcome",
    "ApprovalStore",
    "InMemoryStore",
    "classify",
]
__version__ = "0.2.0"
