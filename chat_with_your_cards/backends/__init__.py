"""Chat backends: backend-neutral events plus concrete implementations."""

from .base import (
    ChatBackend,
    ChatEvent,
    ChatSession,
    Done,
    ErrorEvent,
    EventCallback,
    TextDelta,
    ToolCallFinished,
    ToolCallStarted,
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
    "ScriptedBackend",
    "ScriptedSession",
    "TextDelta",
    "ToolCallFinished",
    "ToolCallStarted",
    "event_to_dict",
]
