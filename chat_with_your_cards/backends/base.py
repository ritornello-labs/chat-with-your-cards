"""Backend-neutral chat event vocabulary and backend protocols.

This module must stay importable without Anki: no aqt imports. The event
union defined here is the single contract shared by the UI bridge, the
ScriptedBackend, and the future CLI/API backends (DESIGN.md section 2).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Protocol, Union


@dataclass(frozen=True)
class TextDelta:
    text: str


@dataclass(frozen=True)
class ToolCallStarted:
    call_id: str
    tool: str
    summary: str


@dataclass(frozen=True)
class ToolCallFinished:
    call_id: str
    ok: bool
    summary: str


@dataclass(frozen=True)
class Done:
    pass


@dataclass(frozen=True)
class ErrorEvent:
    message: str


ChatEvent = Union[TextDelta, ToolCallStarted, ToolCallFinished, Done, ErrorEvent]

EventCallback = Callable[[ChatEvent], None]


def event_to_dict(event: ChatEvent) -> dict[str, Any]:
    """Serialize an event to the JSON payload consumed by web/app.js."""
    if isinstance(event, TextDelta):
        return {"type": "text_delta", "text": event.text}
    if isinstance(event, ToolCallStarted):
        return {
            "type": "tool_call_started",
            "call_id": event.call_id,
            "tool": event.tool,
            "summary": event.summary,
        }
    if isinstance(event, ToolCallFinished):
        return {
            "type": "tool_call_finished",
            "call_id": event.call_id,
            "ok": event.ok,
            "summary": event.summary,
        }
    if isinstance(event, Done):
        return {"type": "done"}
    if isinstance(event, ErrorEvent):
        return {"type": "error", "message": event.message}
    raise TypeError(f"unknown chat event: {event!r}")


class ChatSession(Protocol):
    def send(self, text: str, on_event: EventCallback) -> None:
        """Send a user message; deliver resulting events via on_event."""

    def cancel(self) -> None:
        """Stop any in-flight response. No events after this except none."""

    def close(self) -> None:
        """Release resources. The session must not be used afterwards."""


class ChatBackend(Protocol):
    def start_session(self, context: dict[str, Any]) -> ChatSession:
        """Start a fresh chat session with the given (opaque, for now) context."""
