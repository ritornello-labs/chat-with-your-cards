"""Chat history: event-sourced transcripts (DESIGN.md section 9).

Each chat persists as one JSON file in user_files/transcripts/. The
events are the same payload dicts the webview renders (user_message,
assistant_text - coalesced deltas - tool chips, proposals and their
resolutions, usage), so loading a chat is a straight replay. The meta
carries the backend's own session id, so "continue this chat" maps to
the harness's native resume (claude --resume) while the pixels come
from our file. Harness session files are never parsed.

aqt-free; timestamps injected for testability.
"""

from __future__ import annotations

import json
import secrets
import time
from pathlib import Path
from typing import Any, Callable

# Payload types worth replaying. Everything transient (context chips,
# pins, agent state, dock state...) is display plumbing, not history.
RECORDED_TYPES = {
    "user_message",
    "assistant_text",
    "tool_call_started",
    "tool_call_finished",
    "proposal",
    "proposal_resolved",
    "grading",
    "inline_image",
    "inline_widget",
    "usage",
    "notice",
}
MAX_LIST = 50


def _set_aside_if_pending(event: dict[str, Any]) -> dict[str, Any]:
    """Replay a saved `pending` proposal as SET ASIDE, not as live.

    A restored chat's proposals no longer exist in the ProposalManager (a new
    session starts empty), so their Accept/Reject buttons silently do nothing
    - the card looks actionable and is a zombie. DESIGN.md always said replayed
    pending proposals render set-aside; nothing implemented it.

    Only the REPLAYED copy is changed. The stored buffer keeps what actually
    happened, so the transcript stays a faithful record.
    """
    if event.get("type") != "proposal":
        return event
    proposal = event.get("proposal")
    if not isinstance(proposal, dict) or proposal.get("status") != "pending":
        return event
    return {**event, "proposal": {**proposal, "status": "superseded"}}


def _short_title(text: str, limit: int = 60) -> str:
    text = " ".join(text.split())
    return text[: limit - 1] + "…" if len(text) > limit else text


class TranscriptStore:
    def __init__(self, directory: Path, now: Callable[[], float] = time.time) -> None:
        self._dir = directory
        self._now = now
        self._meta: dict[str, Any] = {}
        self._events: list[dict[str, Any]] = []
        self.begin()

    # ---- current chat ----

    def begin(self, transcript_id: str | None = None) -> None:
        """Start recording a fresh chat (or continue an existing id)."""
        self._meta = {
            "id": transcript_id or secrets.token_hex(6),
            "started_at": self._now(),
            "updated_at": self._now(),
            "title": "",
            "backend_session_id": None,
        }
        self._events = []

    def continue_from(self, transcript_id: str) -> list[dict[str, Any]] | None:
        """Load an old chat for resume: its events become the live buffer so
        new turns append to the same file. Returns the events for replay."""
        data = self.load(transcript_id)
        if data is None:
            return None
        self._meta = data["meta"]
        self._events = list(data["events"])
        return [_set_aside_if_pending(event) for event in self._events]

    def record(self, payload: dict[str, Any]) -> None:
        if payload.get("type") not in RECORDED_TYPES:
            return
        self._events.append(payload)
        self._meta["updated_at"] = self._now()
        if not self._meta["title"] and payload.get("type") == "user_message":
            self._meta["title"] = _short_title(str(payload.get("text", "")))

    def set_backend_session(self, session_id: str | None) -> None:
        if session_id:
            self._meta["backend_session_id"] = session_id

    @property
    def current_id(self) -> str:
        return str(self._meta["id"])

    @property
    def backend_session_id(self) -> str | None:
        return self._meta.get("backend_session_id")

    def flush(self) -> None:
        """Write the current chat to disk (no-op for empty chats)."""
        if not self._meta.get("title"):
            return  # nothing user-visible happened
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            path = self._dir / f"{self.current_id}.json"
            path.write_text(
                json.dumps({"meta": self._meta, "events": self._events}),
                encoding="utf-8",
            )
        except OSError:
            pass

    # ---- history ----

    def list(self) -> list[dict[str, Any]]:
        metas: list[dict[str, Any]] = []
        if not self._dir.exists():
            return metas
        for path in self._dir.glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                meta = dict(data["meta"])
                meta["events"] = len(data.get("events", []))
                metas.append(meta)
            except (OSError, ValueError, KeyError):
                continue
        metas.sort(key=lambda m: m.get("updated_at", 0), reverse=True)
        return metas[:MAX_LIST]

    def load(self, transcript_id: str) -> dict[str, Any] | None:
        path = self._dir / f"{Path(transcript_id).name}.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if "meta" not in data or "events" not in data:
            return None
        return data
