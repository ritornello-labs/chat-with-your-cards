"""Chat session lifecycle: backend selection, context, event fan-out."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable

from aqt import mw
from aqt.qt import QTimer

from .backends import ChatEvent, ProposalRequest, ScriptedBackend, event_to_dict
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
        proposals: Any = None,
    ) -> None:
        self._push = push
        self._config = config
        self._system_prompt_builder = system_prompt_builder
        self._ensure_mcp = ensure_mcp
        self._workdir = workdir
        self._proposals = proposals
        self._backend: Any = None
        self._backend_notice_sent = False
        self._session: Any = None
        self._last_card_id_sent: int | None = None
        self.backend_kind: str = "unset"
        self.event_log: list[ChatEvent] = []

    # ---- backend selection ----

    def _build_backend(self) -> Any:
        choice = os.environ.get(BACKEND_ENV) or str(self._config.get("backend", "auto"))
        if choice in ("codex", "pi"):
            # Adapters are designed (DESIGN.md backend strategy) but not yet
            # implemented; be explicit rather than silently substituting.
            self._push(
                {
                    "type": "notice",
                    "text": f"The {choice} backend is planned but not implemented "
                    "yet - using Claude Code (or the demo backend) instead.",
                }
            )
            choice = "auto"
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
        from .keys import resolve_agent_env

        extra_env, key_problems = resolve_agent_env(self._config)
        for problem in key_problems:
            self._push({"type": "notice", "text": f"API key config: {problem}"})
        self.backend_kind = "claude"
        return ClaudeCliBackend(
            cli_path=cli_path,
            system_prompt_builder=self._system_prompt_builder,
            mcp_url=url,
            mcp_token=token,
            run_on_ui=_run_on_ui,
            workdir=self._workdir,
            log_path=self._workdir.parent / "logs" / "backend.log",
            model_effort=lambda: (
                str(self._config.get("model", "")),
                str(self._config.get("effort", "")),
            ),
            web_access=bool(self._config.get("web_access", True)),
            extra_env=extra_env,
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
        if self._proposals is not None:
            self._proposals.new_session()
        self._push({"type": "reset"})

    def set_agent_config(self, model: str, effort: str) -> None:
        """Change model/effort. Applied to the live session mid-conversation:
        the CLI process respawns with --resume on the next message, so the
        same chat continues under the new model (matching the CLI apps). The
        choice is the caller's to persist (see __init__ _set_agent)."""
        from .backends.claude_cli import VALID_EFFORTS

        model = (model or "").strip()
        effort = (effort or "").strip().lower()
        if effort and effort not in VALID_EFFORTS:
            effort = ""
        changed = model != str(self._config.get("model", "")) or effort != str(
            self._config.get("effort", "")
        )
        self._config["model"] = model
        self._config["effort"] = effort
        if changed and self._session is not None:
            apply = getattr(self._session, "set_model_effort", None)
            if apply is not None:
                apply(model, effort)
                suffix = (
                    " It applies from your next message."
                    if self.streaming
                    else " Continuing this chat under the new model."
                )
                self._push(
                    {"type": "notice", "text": f"Switched to {self._agent_label()}.{suffix}"}
                )
        self.push_agent_state()

    def _agent_label(self) -> str:
        names = {
            "": "the default model",
            "fable": "Fable",
            "opus": "Opus",
            "sonnet": "Sonnet",
            "haiku": "Haiku",
        }
        model = str(self._config.get("model", ""))
        effort = str(self._config.get("effort", ""))
        label = names.get(model, model)
        return f"{label} · {effort} effort" if effort else label

    def push_agent_state(self) -> None:
        self._push(
            {
                "type": "agent",
                "backend": str(self._config.get("backend", "auto")),
                "model": str(self._config.get("model", "")),
                "effort": str(self._config.get("effort", "")),
            }
        )

    def shutdown(self) -> None:
        if self._session is not None:
            self._session.close()
            self._session = None

    def _on_event(self, event: ChatEvent) -> None:
        self.event_log.append(event)
        if isinstance(event, ProposalRequest):
            # Demo/scripted backends request proposals as events; route them
            # through the real ProposalManager (which pushes the proposal
            # card itself). Real backends propose via the MCP tools instead.
            self._handle_proposal_request(event)
            return
        self._push(event_to_dict(event))

    def _handle_proposal_request(self, event: ProposalRequest) -> None:
        if self._proposals is None:
            self._push({"type": "notice", "text": "Proposals are not available."})
            return
        try:
            if event.kind == "edit":
                self._proposals.submit_edit(event.payload)
            else:
                self._proposals.submit_create(event.payload)
        except Exception as exc:  # ProposalError or collection trouble
            self._push({"type": "notice", "text": f"Proposal failed: {exc}"})


def current_card_info() -> dict[str, Any] | None:
    if mw.state != "review" or mw.col is None:
        return None
    reviewer = getattr(mw, "reviewer", None)
    card = getattr(reviewer, "card", None) if reviewer else None
    if card is None:
        return None
    return extract_card_info(mw.col, card)
