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
class ThinkingDelta:
    """A chunk of the model's extended-thinking/reasoning stream.

    Ephemeral by design: never persisted to transcripts (see controller.py),
    and kept out of the visible answer text (see claude_cli.py's parser).

    ``text`` is empty at every observed reasoning effort level today (the
    CLI/account redacts the actual reasoning text upstream - only an opaque
    encrypted signature carries it), so this must not be treated as "no
    thinking happened": ``estimated_tokens`` is what actually proves a
    thinking phase is live, and both UIs drive their "Thinking..." indicator
    off it rather than off non-empty text (DESIGN.md section 9).
    """

    text: str = ""
    estimated_tokens: int | None = None


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
class ProposalRequest:
    """A scripted/demo backend asking the add-on to submit a note proposal.

    Real backends never emit this - their proposals arrive through the
    MCP propose_note tools. The controller intercepts this event and
    routes it into the same ProposalManager, so demos and smoke tests
    exercise the genuine proposal path.
    """

    kind: str  # "create" | "edit"
    payload: dict[str, Any]


@dataclass(frozen=True)
class UsageUpdate:
    """Cumulative session cost/usage as reported by the harness.

    ``cache_read_tokens``/``cache_creation_tokens`` mirror the Anthropic API's
    ``cache_read_input_tokens``/``cache_creation_input_tokens`` usage fields
    (see claude_cli.py's ``result`` handling). Together with ``input_tokens``
    they approximate the size of the context sent on the last turn.

    ``context_window`` is the real per-turn window size the CLI reports in the
    result's ``modelUsage`` map (dogfood 2026-07-13: the stream DOES carry it,
    contra the old "no window in stream" note). When present it supersedes the
    UI's hardcoded per-model table (context_window_for / contextWindow.ts),
    which stays as the fallback for scripted/older backends that omit it.

    ``fast_mode_state`` ("on"/"off") is the CLI's ground-truth report of whether
    the running process actually engaged fast mode - it verifies the requested
    ``--settings {"fastMode":true}`` took effect, rather than trusting the arg
    shape alone.
    """

    cost_usd: float | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_creation_tokens: int | None = None
    context_window: int | None = None
    fast_mode_state: str | None = None


@dataclass(frozen=True)
class Done:
    pass


@dataclass(frozen=True)
class ErrorEvent:
    message: str


@dataclass(frozen=True)
class PermissionDenied:
    """Tool calls the agent's permission layer refused during this turn.

    Under the non-sandbox tiers (`auto` in particular) a safety classifier can
    block a call, and the turn still ends `subtype: "success"` - the model just
    works around it in prose. Without this event the block is invisible: the
    reply reads as the assistant choosing not to act, when in fact the mode we
    put it in stopped it. The `result` message's `permission_denials` array is
    the signal (shape verified against CLI 2.1.208 by forcing a block).
    """

    denials: tuple[dict[str, str], ...]


ChatEvent = Union[
    TextDelta,
    ThinkingDelta,
    ToolCallStarted,
    ToolCallFinished,
    ProposalRequest,
    UsageUpdate,
    Done,
    ErrorEvent,
    PermissionDenied,
]

EventCallback = Callable[[ChatEvent], None]


def event_to_dict(event: ChatEvent) -> dict[str, Any]:
    """Serialize an event to the JSON payload the web/next UI consumes
    (mirrored by ui/src/events.ts)."""
    if isinstance(event, TextDelta):
        return {"type": "text_delta", "text": event.text}
    if isinstance(event, ThinkingDelta):
        return {
            "type": "thinking_delta",
            "text": event.text,
            "estimated_tokens": event.estimated_tokens,
        }
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
    if isinstance(event, ProposalRequest):
        return {"type": "proposal_request", "kind": event.kind, "payload": event.payload}
    if isinstance(event, UsageUpdate):
        return {
            "type": "usage",
            "cost_usd": event.cost_usd,
            "input_tokens": event.input_tokens,
            "output_tokens": event.output_tokens,
            "cache_read_tokens": event.cache_read_tokens,
            "cache_creation_tokens": event.cache_creation_tokens,
            "context_window": event.context_window,
            "fast_mode_state": event.fast_mode_state,
        }
    if isinstance(event, Done):
        return {"type": "done"}
    if isinstance(event, ErrorEvent):
        return {"type": "error", "message": event.message}
    if isinstance(event, PermissionDenied):
        return {
            "type": "permission_denied",
            "denials": [dict(entry) for entry in event.denials],
        }
    raise TypeError(f"unknown chat event: {event!r}")


class ChatSession(Protocol):
    def send(
        self,
        text: str,
        on_event: EventCallback,
        extra_blocks: list[dict] | None = None,
    ) -> None:
        """Send a user message; deliver resulting events via on_event.
        ``extra_blocks`` are optional Messages-API content blocks (#15b,
        e.g. base64 image blocks) appended after the text; backends without
        multimodal support ignore them."""

    def cancel(self) -> None:
        """Stop any in-flight response. No events after this except none."""

    def close(self) -> None:
        """Release resources. The session must not be used afterwards."""


class ChatBackend(Protocol):
    def start_session(self, context: dict[str, Any]) -> ChatSession:
        """Start a fresh chat session with the given (opaque, for now) context."""
