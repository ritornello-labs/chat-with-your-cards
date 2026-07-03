"""Chat session lifecycle: backend selection, context, event fan-out."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable

from aqt import mw
from aqt.qt import QTimer

from .backends import ChatEvent, ScriptedBackend, event_to_dict
from .backends.claude_cli import ClaudeCliBackend, find_claude_cli
from .context import build_card_block, extract_card_info, wrap_user_message

BACKEND_ENV = "CWYC_BACKEND"


def _qt_schedule(delay_ms: int, callback: Callable[[], None]) -> None:
    QTimer.singleShot(delay_ms, callback)


def _run_on_ui(callback: Callable[[], None]) -> None:
    mw.taskman.run_on_main(callback)


class ChatController:
    """Owns the current chat session and fans events out to the UI.

    event_log keeps every backend event of the current chat; the GUI smoke
    probe asserts against it, and later milestones will persist it as the
    session transcript (DESIGN.md section 9).
    """

    def __init__(
        self,
        push: Callable[[dict[str, Any]], None],
        *,
        config: dict[str, Any],
        system_prompt_builder: Callable[[], str],
        ensure_mcp: Callable[[], tuple[str, str]],
        workdir: Path,
    ) -> None:
        self._push = push
        self._config = config
        self._system_prompt_builder = system_prompt_builder
        self._ensure_mcp = ensure_mcp
        self._workdir = workdir
        self._backend: Any = None
        self._backend_notice_sent = False
        self._session: Any = None
        self._last_card_id_sent: int | None = None
        self.backend_kind: str = "unset"
        self.event_log: list[ChatEvent] = []

    # ---- backend selection ----

    def _build_backend(self) -> Any:
        choice = os.environ.get(BACKEND_ENV) or str(self._config.get("backend", "auto"))
        if choice not in ("auto", "claude", "scripted"):
            choice = "auto"
        if choice == "scripted":
            self.backend_kind = "scripted"
            return ScriptedBackend(_qt_schedule)

        cli_path = find_claude_cli(str(self._config.get("claude_cli_path", "")))
        if cli_path is None:
            self.backend_kind = "scripted"
            if not self._backend_notice_sent:
                self._backend_notice_sent = True
                self._push(
                    {
                        "type": "notice",
                        "text": "Claude Code CLI not found — using the built-in "
                        "demo backend. Install Claude Code or set "
                        "claude_cli_path in the add-on config.",
                    }
                )
            return ScriptedBackend(_qt_schedule)

        url, token = self._ensure_mcp()
        self.backend_kind = "claude"
        return ClaudeCliBackend(
            cli_path=cli_path,
            system_prompt_builder=self._system_prompt_builder,
            mcp_url=url,
            mcp_token=token,
            run_on_ui=_run_on_ui,
            workdir=self._workdir,
        )

    def ensure_ready(self) -> None:
        """Pre-warm on chat focus: build backend, spawn the CLI process."""
        if self._backend is None:
            self._backend = self._build_backend()
        if self._session is None:
            self._session = self._backend.start_session({})
        prewarm = getattr(self._session, "prewarm", None)
        if prewarm is not None:
            prewarm()

    # ---- chat actions ----

    @property
    def streaming(self) -> bool:
        return self._session is not None and self._session.streaming

    def send_user_message(self, text: str) -> None:
        text = text.strip()
        if not text or self.streaming:
            return
        self.ensure_ready()
        card_block, label = self._context_for_send()
        self._push({"type": "context", "label": label})
        self._session.send(wrap_user_message(text, card_block), self._on_event)

    def _context_for_send(self) -> tuple[str | None, str]:
        info = current_card_info()
        if info is None:
            self._last_card_id_sent = None
            return None, "collection overview"
        label = f"card in {info['deck']}"
        if info["card_id"] == self._last_card_id_sent:
            return None, label
        self._last_card_id_sent = info["card_id"]
        return build_card_block(info), label

    def cancel(self) -> None:
        if self._session is not None and self._session.streaming:
            self._session.cancel()
            self._push({"type": "cancelled"})

    def new_chat(self) -> None:
        if self._session is not None:
            self._session.close()
            self._session = None
        self._last_card_id_sent = None
        self.event_log.clear()
        self._push({"type": "reset"})

    def shutdown(self) -> None:
        if self._session is not None:
            self._session.close()
            self._session = None

    def _on_event(self, event: ChatEvent) -> None:
        self.event_log.append(event)
        self._push(event_to_dict(event))


def current_card_info() -> dict[str, Any] | None:
    if mw.state != "review" or mw.col is None:
        return None
    reviewer = getattr(mw, "reviewer", None)
    card = getattr(reviewer, "card", None) if reviewer else None
    if card is None:
        return None
    return extract_card_info(mw.col, card)
