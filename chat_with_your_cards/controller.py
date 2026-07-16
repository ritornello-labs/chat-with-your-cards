"""Chat session lifecycle: backend selection, context, event fan-out."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Callable

from aqt import mw
from aqt.qt import QTimer

from .backends import (
    ChatEvent,
    Done,
    ProposalRequest,
    ScriptedBackend,
    TextDelta,
    event_to_dict,
)
from .backends.claude_cli import ClaudeCliBackend, find_claude_cli
from .context import build_card_block, extract_card_info, wrap_user_message

BACKEND_ENV = "CWYC_BACKEND"


def _setup_platform() -> str:
    """Coarse platform id for the onboarding setup card (task #19): picks
    which install one-liner the UI shows. sys.platform is "darwin" (macOS),
    "linux" (Linux, incl. WSL), "win32"/"cygwin" (Windows) - anything else
    (BSDs, etc.) falls back to "linux" since the curl one-liner works there
    too, rather than showing an "unknown" platform with no instructions."""
    if sys.platform == "darwin":
        return "darwin"
    if sys.platform.startswith("win") or sys.platform == "cygwin":
        return "windows"
    return "linux"


def _qt_schedule(delay_ms: int, callback: Callable[[], None]) -> None:
    QTimer.singleShot(delay_ms, callback)


def _run_on_ui(callback: Callable[[], None]) -> None:
    mw.taskman.run_on_main(callback)


AGENT_TOOLS_VALUES = ("sandbox", "acceptEdits", "auto", "full")


def _norm_agent_tools(value: Any) -> str:
    """Canonical agent-tools axis (DESIGN.md section 5): how much of the CLI's
    own shell/file toolset the agent gets, and how calls are approved in our
    headless session. 'sandbox' (default) removes those tools; 'acceptEdits' /
    'auto' / 'full' leave them on and map to --permission-mode acceptEdits /
    auto / bypassPermissions. Anything unrecognized falls back to sandbox."""
    v = str(value).strip()
    return v if v in AGENT_TOOLS_VALUES else "sandbox"


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
        transcripts: Any = None,
    ) -> None:
        self._push = push
        self._config = config
        self._system_prompt_builder = system_prompt_builder
        self._ensure_mcp = ensure_mcp
        self._workdir = workdir
        self._proposals = proposals
        self._transcripts = transcripts
        self._assistant_buffer = ""
        self._pending_resume: str | None = None
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
                # A structured event (task #19), not a one-line notice: the UI
                # renders a full setup card (install + sign-in steps + a
                # no-restart "Re-check" button) instead of a message the user
                # has no obvious action for. Superseded the old plain-notice
                # fallback so the two don't show redundantly.
                self._push({"type": "setup_needed", "platform": _setup_platform()})
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
            fast_mode=lambda: bool(self._config.get("fast_mode", False)),
            agent_tools=lambda: _norm_agent_tools(self._config.get("agent_tools")),
            web_access=bool(self._config.get("web_access", True)),
            extra_env=extra_env,
            mcp_servers=self._config.get("mcp_servers") or {},
            mcp_inherit_user=bool(self._config.get("mcp_inherit_user", False)),
            mcp_disabled=list(self._config.get("mcp_disabled") or []),
        )

    def ensure_ready(self) -> None:
        """Pre-warm on chat focus: build backend, spawn the CLI process."""
        if self._backend is None:
            self._backend = self._build_backend()
        if self._session is None:
            context: dict[str, Any] = {}
            if self._pending_resume:
                context["resume_session_id"] = self._pending_resume
                self._pending_resume = None
            self._session = self._backend.start_session(context)
        prewarm = getattr(self._session, "prewarm", None)
        if prewarm is not None:
            prewarm()

    def recheck_backend(self) -> bool:
        """The setup card's "Re-check" button (task #19): re-run CLI discovery
        with NO Anki restart required. find_claude_cli is just shutil.which +
        a few Path.exists() calls (see backends/claude_cli.py) - cheap enough
        to run straight on the main thread, unlike the doctor panel's
        `--version` subprocesses.

        On success: tear down whatever demo session/backend ensure_ready()
        already built (the dock pre-warms on every focus, so by the time the
        setup card is showing, a ScriptedBackend session is very likely
        already live) and rebuild for real, so the user's very next message
        goes to the real Claude Code - no restart, no new chat needed. Also
        resets the notice-once flag so a future CLI loss (or a still-missing
        find on a later retry) can surface again.
        """
        cli_path = find_claude_cli(str(self._config.get("claude_cli_path", "")))
        if cli_path is None:
            self._push(
                {
                    "type": "notice",
                    "text": "Still can't find Claude Code. If you just installed it, "
                    "open a new terminal (or restart Anki) so it picks up the updated "
                    "PATH, then try Re-check again. Running `claude --version` in a "
                    "terminal confirms it's installed.",
                }
            )
            return False
        if self._session is not None:
            self._session.close()
            self._session = None
        self._backend = None
        self._backend_notice_sent = False
        self.ensure_ready()
        self._push({"type": "notice", "text": "Claude Code found — you're all set."})
        self._push({"type": "setup_resolved"})
        return True

    # ---- chat actions ----

    @property
    def streaming(self) -> bool:
        return self._session is not None and self._session.streaming

    @property
    def backend_session_id(self) -> str | None:
        """The harness's own session id (for --resume / open-in-Claude-Code)."""
        return getattr(self._session, "session_id", None)

    def send_user_message(self, text: str) -> None:
        text = text.strip()
        if not text or self.streaming:
            return
        self.ensure_ready()
        if self._transcripts is not None:
            self._transcripts.record({"type": "user_message", "text": text})
        card_block, label = self._context_for_send()
        self._push({"type": "context", "label": label})
        # The collection overview is NOT injected here anymore (design change
        # 2026-07-14): it cost ~8k tokens on the first message of EVERY
        # session whether or not the conversation needed collection
        # structure. The agent now fetches it on demand via the
        # get_collection_overview tool (see context.build_system_prompt).
        self._session.send(wrap_user_message(text, card_block), self._on_event)

    def _context_for_send(self) -> tuple[str | None, str]:
        info = current_card_info()
        if info is None:
            self._last_card_id_sent = None
            return None, "collection (tools on demand)"
        label = f"card in {info['deck']}"
        if info["card_id"] == self._last_card_id_sent:
            return None, label
        self._last_card_id_sent = info["card_id"]
        return build_card_block(info), label

    def push_context_chip(self) -> None:
        """Live-update the pre-chat context chip as the user moves between
        the reviewer and other screens (DESIGN.md section 9: 'the chip
        updates as the user moves', previously only refreshed on send)."""
        info = current_card_info()
        if info is None:
            self._push(
                {"type": "context", "label": "collection (tools on demand)", "kind": "overview"}
            )
        else:
            self._push(
                {"type": "context", "label": f"card in {info['deck']}", "kind": "card"}
            )

    def cancel(self) -> None:
        if self._session is None or not self._session.streaming:
            return
        # ClaudeCliSession exposes interrupt() (not part of the ChatSession
        # Protocol, same probe pattern as prewarm/set_model_effort) so the
        # stop button can keep the process - and the conversation - alive
        # instead of terminate()-and-respawn-with---resume. If it
        # acknowledges, the CLI's own aborted-turn Done/ErrorEvent will
        # reach the UI through the normal event flow, so stay silent here
        # to avoid a double "stopped" signal. Any falsy/missing interrupt()
        # (ScriptedSession, an old CLI, a timeout) falls back to the
        # original cancel()-then-push behavior.
        interrupt = getattr(self._session, "interrupt", None)
        if interrupt is not None:
            if not interrupt():
                self._push({"type": "cancelled"})
            return
        self._session.cancel()
        self._push({"type": "cancelled"})

    def new_chat(self) -> None:
        if self._session is not None:
            self._session.close()
            self._session = None
        # Rebuild the backend on a fresh chat so config read as VALUES at build
        # time - the MCP setup (mcp_servers / mcp_inherit_user / mcp_disabled),
        # unlike model/effort which are read fresh via callables - is picked up.
        # This is why MCP config changes "apply to new chats". Cheap: the build
        # constructs an object; no process spawns until ensure_ready/prewarm.
        self._backend = None
        self._last_card_id_sent = None
        self._pending_resume = None
        self._assistant_buffer = ""
        self.event_log.clear()
        if self._transcripts is not None:
            self._transcripts.flush()
            self._transcripts.begin()
        if self._proposals is not None:
            self._proposals.new_session()
        self._push({"type": "reset"})

    # ---- chat history ----

    def push_history_list(self) -> None:
        if self._transcripts is None:
            return
        self._push({"type": "history", "sessions": self._transcripts.list()})

    def restore_last_chat(self) -> None:
        """Reopen the most recent saved chat on launch (config restore_last_chat).
        No-op when there is no prior chat. Uses the same replay + native resume
        path as picking a chat from History."""
        if self._transcripts is None:
            return
        sessions = self._transcripts.list()
        if not sessions:
            return
        last_id = str(sessions[0].get("id", ""))
        if last_id:
            self.load_history(last_id)

    def load_history(self, transcript_id: str) -> None:
        """Replay a saved chat into the UI and continue it via the backend's
        native resume (DESIGN.md section 9)."""
        if self._transcripts is None:
            return
        self._transcripts.flush()  # save whatever chat was in progress
        events = self._transcripts.continue_from(transcript_id)
        if events is None:
            self._push({"type": "notice", "text": "That chat could not be loaded."})
            return
        if self._session is not None:
            self._session.close()
            self._session = None
        self._pending_resume = self._transcripts.backend_session_id
        self._assistant_buffer = ""
        self._last_card_id_sent = None
        self.event_log.clear()
        if self._proposals is not None:
            self._proposals.new_session()
        self._push({"type": "reset"})
        self._push({"type": "history_load", "events": events})
        self.push_agent_state()

    def set_agent_config(
        self,
        model: str,
        effort: str,
        fast_mode: bool = False,
        agent_tools: str = "sandbox",
    ) -> None:
        """Change model/effort/fast-mode/agent-tools. Applied to the live
        session mid-conversation: the CLI process respawns with --resume on the
        next message, so the same chat continues under the new settings
        (matching the CLI apps). fast_mode and agent_tools ride the identical
        respawn path - they have no mid-session toggle upstream either
        (--settings / --disallowedTools / --permission-mode are spawn-time
        only). agent_tools is an orthogonal axis from permission_mode: it gates
        the CLI's own shell/file tools, not collection writes. The choice is
        the caller's to persist (see __init__ _set_agent)."""
        from .backends.claude_cli import VALID_EFFORTS

        model = (model or "").strip()
        effort = (effort or "").strip().lower()
        if effort and effort not in VALID_EFFORTS:
            effort = ""
        fast_mode = bool(fast_mode)
        agent_tools = _norm_agent_tools(agent_tools)
        changed = (
            model != str(self._config.get("model", ""))
            or effort != str(self._config.get("effort", ""))
            or fast_mode != bool(self._config.get("fast_mode", False))
            or agent_tools != _norm_agent_tools(self._config.get("agent_tools"))
        )
        self._config["model"] = model
        self._config["effort"] = effort
        self._config["fast_mode"] = fast_mode
        self._config["agent_tools"] = agent_tools
        if changed and self._session is not None:
            apply = getattr(self._session, "set_model_effort", None)
            if apply is not None:
                apply(model, effort, fast_mode, agent_tools)
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
        if effort:
            label = f"{label} · {effort} effort"
        if bool(self._config.get("fast_mode", False)):
            label = f"{label} · fast"
        tools = _norm_agent_tools(self._config.get("agent_tools"))
        if tools != "sandbox":
            tool_label = {
                "acceptEdits": "accept-edits tools",
                "auto": "auto tools",
                "full": "full tools",
            }.get(tools, f"{tools} tools")
            label = f"{label} · {tool_label}"
        return label

    VALID_MODES = (
        "default",
        "ask-each-read",
        "read-only",
        "auto-accept",
        "trusted-writes",
    )

    def set_permission_mode(self, mode: str) -> None:
        """Runtime mode switch (chip / Shift+Tab). Gating is enforced live at
        the MCP boundary; the advertised tool list only changes when the MCP
        server is (re)built, so read-only/trusted advertising lags until the
        next Anki run - enforcement does not."""
        mode = (mode or "").strip()
        if mode not in self.VALID_MODES:
            return
        self._config["permission_mode"] = mode
        self.push_agent_state()

    def push_agent_state(self) -> None:
        self._push(
            {
                "type": "agent",
                "backend": str(self._config.get("backend", "auto")),
                "model": str(self._config.get("model", "")),
                "effort": str(self._config.get("effort", "")),
                "mode": str(self._config.get("permission_mode", "default")),
                "fast": bool(self._config.get("fast_mode", False)),
                "tools": _norm_agent_tools(self._config.get("agent_tools")),
            }
        )

    def shutdown(self) -> None:
        if self._transcripts is not None:
            self._flush_assistant_text()
            self._transcripts.flush()
        if self._session is not None:
            self._session.close()
            self._session = None

    def _flush_assistant_text(self) -> None:
        if self._transcripts is not None and self._assistant_buffer.strip():
            self._transcripts.record(
                {"type": "assistant_text", "text": self._assistant_buffer}
            )
        self._assistant_buffer = ""

    def _on_event(self, event: ChatEvent) -> None:
        self.event_log.append(event)
        if isinstance(event, ProposalRequest):
            # Demo/scripted backends request proposals as events; route them
            # through the real ProposalManager (which pushes the proposal
            # card itself). Real backends propose via the MCP tools instead.
            self._handle_proposal_request(event)
            return
        # Transcript: deltas coalesce into one assistant_text per block
        # (raw deltas are excluded from recording); everything else is
        # recorded by the shared recording push in the add-on glue.
        if isinstance(event, TextDelta):
            self._assistant_buffer += event.text
        else:
            self._flush_assistant_text()
        if isinstance(event, Done) and self._transcripts is not None:
            self._transcripts.set_backend_session(self.backend_session_id)
            self._transcripts.flush()
        payload = event_to_dict(event)
        if payload.get("type") == "usage" and not self._is_byok():
            # On subscription (harness login) the CLI reports total_cost_usd as
            # a notional API-EQUIVALENT figure - the user pays $0 per message, so
            # showing a dollar amount is misleading. Drop it; tokens still show
            # (they reflect real context/usage size). dogfood 2026-07-11.
            payload["cost_usd"] = None
        self._push(payload)

    def _is_byok(self) -> bool:
        """True when the user configured their own API key (bring-your-own-key),
        so per-message dollar cost is real. Empty = subscription auth."""
        keys = (
            "anthropic_api_key",
            "anthropic_api_key_op",
            "openai_api_key",
            "openai_api_key_op",
        )
        return any(str(self._config.get(k, "")).strip() for k in keys)

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
