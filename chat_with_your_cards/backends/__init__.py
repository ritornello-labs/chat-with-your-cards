"""Chat backends: backend-neutral events plus concrete implementations."""

from .base import (
    ChatBackend,
    ChatEvent,
    ChatSession,
    Done,
    ErrorEvent,
    EventCallback,
    PermissionDenied,
    ProposalRequest,
    TextDelta,
    ThinkingDelta,
    ToolCallFinished,
    ToolCallStarted,
    UsageUpdate,
    event_to_dict,
)
from .scripted import ScriptedBackend, ScriptedSession

__all__ = [
    "ChatBackend",
    "ChatEvent",
    "ChatSession",
    "Done",
    "ErrorEvent",
    "EventCallback",
    "PermissionDenied",
    "ProposalRequest",
    "ScriptedBackend",
    "ScriptedSession",
    "TextDelta",
    "ThinkingDelta",
    "ToolCallFinished",
    "ToolCallStarted",
    "UsageUpdate",
    "event_to_dict",
]
