"""Chat session lifecycle: wires the backend to the web UI bridge."""

from __future__ import annotations

from typing import Any, Callable, Optional

from aqt.qt import QTimer

from .backends import ChatEvent, ScriptedBackend, ScriptedSession, event_to_dict


def _qt_schedule(delay_ms: int, callback: Callable[[], None]) -> None:
    QTimer.singleShot(delay_ms, callback)


class ChatController:
    """Owns the current chat session and fans events out to the UI.

    event_log keeps every backend event of the current chat; the GUI smoke
    probe asserts against it, and later milestones will persist it as the
    session transcript (DESIGN.md section 9).
    """

    def __init__(self, push: Callable[[dict[str, Any]], None]) -> None:
        self._push = push
        self._backend = ScriptedBackend(_qt_schedule)
        self._session: Optional[ScriptedSession] = None
        self.event_log: list[ChatEvent] = []

    @property
    def streaming(self) -> bool:
        return self._session is not None and self._session.streaming

    def send_user_message(self, text: str) -> None:
        text = text.strip()
        if not text or self.streaming:
            return
        if self._session is None:
            self._session = self._backend.start_session({})
        self._session.send(text, self._on_event)

    def cancel(self) -> None:
        if self._session is not None and self._session.streaming:
            self._session.cancel()
            self._push({"type": "cancelled"})

    def new_chat(self) -> None:
        if self._session is not None:
            self._session.close()
            self._session = None
        self.event_log.clear()
        self._push({"type": "reset"})

    def _on_event(self, event: ChatEvent) -> None:
        self.event_log.append(event)
        self._push(event_to_dict(event))
