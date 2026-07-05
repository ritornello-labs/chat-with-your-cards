"""Chat With Your Cards - a collapsible AI chat dock for Anki.

Importable without Anki (unit tests import .backends etc.); everything
that touches aqt is guarded behind the mw check below.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from .controller import ChatController
    from .dock import ChatDock
    from .mcp_server import McpServer
    from .proposals import ProposalManager
    from .stats import StatsCache

DEFAULT_CONFIG: dict[str, Any] = {
    "toggle_shortcut": "Ctrl+J",
    "new_chat_shortcut": "Ctrl+Shift+J",
    "dock_width": 420,
    "backend": "auto",
    "claude_cli_path": "",
    "model": "",
    "effort": "",
    "web_access": True,
    "suggested_questions": True,
    "anthropic_api_key": "",
    "anthropic_api_key_op": "",
    "openai_api_key": "",
    "openai_api_key_op": "",
    "permission_mode": "default",
    "stats_refresh_minutes": 30,
    "context_token_budget": 8000,
    "auto_accept_cap": 20,
    "write_budget": 200,
    "conventions_prompt": "",
    "created_tag": "ai-created",
    "edited_tag": "ai-edited",
    "session_tag_prefix": "ai-chat-dock::session-",
    "pins": {},
}

USER_FILES = Path(__file__).resolve().parent / "user_files"


@dataclass
class AddonState:
    dock: Optional[ChatDock] = None
    controller: Optional[ChatController] = None
    stats_cache: Optional[StatsCache] = None
    mcp: Optional[McpServer] = None
    proposals: Optional[ProposalManager] = None
    transcripts: Any = None
    approvals: Any = None
    shortcuts: list[Any] = field(default_factory=list)
    web_ready: bool = False
    config: dict[str, Any] = field(default_factory=dict)


state = AddonState()

try:
    from aqt import mw
except ImportError:  # running outside Anki (unit tests)
    mw = None  # type: ignore[assignment]


def toggle_chat_focus() -> None:
    from . import shortcuts as shortcuts_mod

    shortcuts_mod.toggle_chat_focus(state)


def new_chat() -> None:
    from . import shortcuts as shortcuts_mod

    shortcuts_mod.new_chat(state)


def _setup() -> None:
    from aqt import gui_hooks

    from . import dock as dock_mod
    from . import shortcuts as shortcuts_mod
    from .context import build_system_prompt
    from .controller import ChatController
    from .proposals import ProposalManager
    from .skills import load_conventions
    from .stats import StatsCache

    config = dict(DEFAULT_CONFIG)
    config.update(mw.addonManager.getConfig(__name__) or {})
    state.config = config

    state.stats_cache = StatsCache(
        USER_FILES / "stats_cache.json",
        refresh_minutes=int(config["stats_refresh_minutes"]),
    )
    state.stats_cache.start()

    state.dock = dock_mod.create_dock(dock_width=int(config["dock_width"]))

    from .transcripts import TranscriptStore

    state.transcripts = TranscriptStore(USER_FILES / "transcripts")

    dock = state.dock

    def recording_push(payload: dict[str, Any]) -> None:
        # One pipe to the webview that also feeds the chat transcript
        # (TranscriptStore ignores payload types not worth replaying).
        if state.transcripts is not None:
            state.transcripts.record(payload)
        dock.bridge.push(payload)

    state.proposals = ProposalManager(
        get_col=lambda: mw.col,
        push=recording_push,
        config=config,
        save_pins=_save_pins,
        after_write=_refresh_reviewer,
        checkpoint=_backup_checkpoint,
    )

    from .skills import materialize_agent_skills

    conventions = load_conventions(USER_FILES, str(config.get("conventions_prompt", "")))
    materialize_agent_skills(USER_FILES / "agent-home")

    def system_prompt() -> str:
        cache = state.stats_cache
        overview = (
            cache.overview(int(config["context_token_budget"])) if cache else None
        )
        return build_system_prompt(
            overview,
            permission_mode=str(config["permission_mode"]),
            pins=state.proposals.pins if state.proposals else None,
            conventions=conventions,
        )

    state.controller = ChatController(
        push=recording_push,
        config=config,
        system_prompt_builder=system_prompt,
        ensure_mcp=_ensure_mcp,
        workdir=USER_FILES / "agent-home",
        proposals=state.proposals,
        transcripts=state.transcripts,
    )
    _wire_bridge()
    shortcuts_mod.register_shortcuts(state)

    # Live context chip: refresh as the user moves between screens/cards.
    def _chip(*_args: Any) -> None:
        if state.controller is not None:
            state.controller.push_context_chip()

    gui_hooks.reviewer_did_show_question.append(_chip)
    gui_hooks.state_did_change.append(_chip)


class _ToolCtx:
    @property
    def col(self) -> Any:
        return mw.col

    @property
    def stats(self) -> dict[str, Any] | None:
        return state.stats_cache.stats if state.stats_cache else None

    @property
    def proposals(self) -> Any:
        return state.proposals

    @property
    def config(self) -> dict[str, Any]:
        return state.config


def _ensure_mcp() -> tuple[str, str]:
    """Start the MCP server on first use; returns (url, bearer token)."""
    if state.mcp is None:
        from .mcp_server import McpServer, tool_specs_for_mcp
        from .tools import build_registry

        registry = build_registry()
        ctx = _ToolCtx()
        mode = str(state.config.get("permission_mode", "default"))
        read_only = mode == "read-only"
        trusted = mode == "trusted-writes"

        specs_by_name = {spec.name: spec for spec in registry.specs(include_trusted=True)}

        from .approvals import ApprovalBroker

        def push_on_main(payload: dict[str, Any]) -> None:
            mw.taskman.run_on_main(
                lambda: state.dock.bridge.push(payload) if state.dock else None
            )

        state.approvals = ApprovalBroker(push_on_main)

        def execute_tool(name: str, args: dict[str, Any]) -> Any:
            # The MCP server is the security boundary: enforce the LIVE
            # permission mode here, not just in what tools are advertised.
            # This is what makes runtime mode switching sound.
            spec = specs_by_name.get(name)
            live_mode = str(state.config.get("permission_mode", "default"))
            if spec is not None and spec.writes and live_mode == "read-only":
                raise PermissionError(
                    "this session is read-only; the user must switch the "
                    "permission mode to allow writes"
                )
            if spec is not None and spec.trusted_only and live_mode != "trusted-writes":
                raise PermissionError(
                    f"{name} is only available in trusted-writes mode"
                )
            if (
                spec is not None
                and not spec.writes
                and live_mode == "ask-each-read"
            ):
                # Blocks this MCP thread on the inline Allow/Deny chip.
                # Writes are not double-gated: proposals review them anyway.
                import json as _json

                summary = _json.dumps(args, ensure_ascii=False)[:120]
                if not state.approvals.request(name, summary):
                    raise PermissionError(f"the user declined this {name} call")
            box: dict[str, Any] = {}
            done = threading.Event()

            def run() -> None:
                try:
                    if mw.col is None:
                        raise RuntimeError("collection is not open")
                    box["result"] = registry.call(ctx, name, args)
                except Exception as exc:  # noqa: BLE001 - marshaled to caller
                    box["error"] = exc
                finally:
                    done.set()

            mw.taskman.run_on_main(run)
            if not done.wait(timeout=15):
                raise TimeoutError("Anki main thread did not respond within 15s")
            if "error" in box:
                raise box["error"]
            return box["result"]

        state.mcp = McpServer(
            tool_specs=tool_specs_for_mcp(
                registry.specs(include_writes=not read_only, include_trusted=trusted)
            ),
            execute_tool=execute_tool,
        )
        state.mcp.start()
        _write_project_mcp_json(state.mcp.url, state.mcp.token)
    return state.mcp.url, state.mcp.token


def _write_project_mcp_json(url: str, token: str) -> None:
    """Write agent-home/.mcp.json so a terminal Claude Code opened in that
    directory (the open-in-Claude-Code flow) discovers the anki tools too.
    Rewritten each Anki run because the port and token rotate."""
    import json as _json

    path = USER_FILES / "agent-home" / ".mcp.json"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            _json.dumps(
                {
                    "mcpServers": {
                        "anki": {
                            "type": "http",
                            "url": url,
                            "headers": {"Authorization": f"Bearer {token}"},
                        }
                    }
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        path.chmod(0o600)  # carries the bearer token
    except OSError:
        pass


def _wire_bridge() -> None:
    from . import shortcuts as shortcuts_mod

    assert state.dock is not None and state.controller is not None
    bridge = state.dock.bridge
    controller = state.controller

    bridge.on("ready", lambda _msg: _mark_web_ready())
    bridge.on("send", lambda msg: controller.send_user_message(str(msg.get("text", ""))))
    bridge.on("cancel", lambda _msg: controller.cancel())
    bridge.on("new_chat", lambda _msg: new_chat())
    bridge.on("toggle_focus", lambda _msg: toggle_chat_focus())
    bridge.on("focus_reviewer", lambda _msg: shortcuts_mod.focus_main_window())

    proposals = state.proposals
    assert proposals is not None
    bridge.on("proposal_accept", proposals.accept)
    bridge.on("proposal_reject", proposals.reject)
    bridge.on("proposal_revert", proposals.revert)
    bridge.on("proposal_readd", proposals.readd)
    bridge.on("proposal_restore", proposals.restore)
    bridge.on("proposal_preview", proposals.preview_request)
    bridge.on("undo_session", lambda _msg: proposals.undo_session())
    bridge.on("set_pins", lambda msg: proposals.set_pins(msg.get("pins") or {}))
    bridge.on("open_session_browser", lambda _msg: _open_session_browser())
    bridge.on(
        "open_in_claude",
        lambda msg: _open_in_claude_code(str(msg.get("target", "terminal"))),
    )
    bridge.on("list_history", lambda _msg: controller.push_history_list())
    bridge.on("load_history", lambda msg: controller.load_history(str(msg.get("id", ""))))
    bridge.on("run_doctor", lambda _msg: _run_doctor())
    bridge.on(
        "tool_approval_response",
        lambda msg: state.approvals.respond(msg) if state.approvals else None,
    )
    bridge.on("set_agent", _set_agent)
    bridge.on("set_permission_mode", _set_permission_mode)
    bridge.on("toggle_float", lambda _msg: state.dock.toggle_float() if state.dock else None)


def _mark_web_ready() -> None:
    state.web_ready = True
    if state.dock is not None:
        state.dock.web.eval("window.chatUI && window.chatUI.ackReady();")
    if state.proposals is not None:
        state.proposals.push_ui_state()
    if state.controller is not None:
        state.controller.push_agent_state()
    if state.dock is not None:
        state.dock.push_dock_state()
        state.dock.bridge.push(
            {
                "type": "ui_config",
                "suggested_questions": bool(
                    state.config.get("suggested_questions", True)
                ),
            }
        )
    if state.controller is not None:
        state.controller.push_context_chip()
    _push_collection_meta()


def _push_collection_meta() -> None:
    """Feed the pins selectors: deck names, note types + fields, tags."""
    if state.dock is None or mw.col is None:
        return
    try:
        decks = sorted(mw.col.decks.all_names())
        note_types = [
            {"name": m["name"], "fields": [f["name"] for f in m["flds"]]}
            for m in (mw.col.models.by_name(nt.name) for nt in mw.col.models.all_names_and_ids())
            if m is not None
        ]
        tags = sorted(mw.col.tags.all())
    except Exception:
        return
    state.dock.bridge.push(
        {
            "type": "collection_meta",
            "decks": decks,
            "note_types": note_types,
            "tags": tags,
        }
    )


def _set_permission_mode(msg: dict[str, Any]) -> None:
    if state.controller is None:
        return
    state.controller.set_permission_mode(str(msg.get("mode", "")))
    config = mw.addonManager.getConfig(__name__) or {}
    config["permission_mode"] = state.config.get("permission_mode", "default")
    mw.addonManager.writeConfig(__name__, config)


def _set_agent(msg: dict[str, Any]) -> None:
    if state.controller is None:
        return
    model = str(msg.get("model", ""))
    effort = str(msg.get("effort", ""))
    state.controller.set_agent_config(model, effort)
    config = mw.addonManager.getConfig(__name__) or {}
    config["model"] = state.config.get("model", "")
    config["effort"] = state.config.get("effort", "")
    mw.addonManager.writeConfig(__name__, config)


def _save_pins(pins: dict[str, Any]) -> None:
    config = mw.addonManager.getConfig(__name__) or {}
    config["pins"] = pins
    mw.addonManager.writeConfig(__name__, config)


def _backup_checkpoint(reason: str) -> None:
    """Force an Anki backup before bulk/delete/change-set applies, so even
    non-ledger-revertible operations have a way back."""
    if mw is None or mw.col is None:
        return
    try:
        mw.col.create_backup(
            backup_folder=mw.pm.backupFolder(), force=True, wait_for_completion=False
        )
    except Exception:
        # Backups are defense-in-depth; their failure must not block the op.
        pass


def _refresh_reviewer(note_ids: list[int]) -> None:
    """Re-render the reviewer if it is showing a note we just wrote to, so an
    accepted edit shows without the user leaving and re-entering review."""
    if mw is None or mw.state != "review":
        return
    reviewer: Any = getattr(mw, "reviewer", None)
    card = getattr(reviewer, "card", None) if reviewer else None
    if reviewer is None or card is None or card.nid not in set(note_ids):
        return
    try:
        card.load()  # re-read the mutated note from the collection
        if getattr(reviewer, "state", "question") == "answer":
            reviewer._showAnswer()
        else:
            reviewer._showQuestion()
    except Exception:
        # Best-effort: a private-API drift shouldn't break the write itself.
        pass


def _run_doctor() -> None:
    """Gather the setup report off the main thread (version subprocesses can
    take a second each) and push it to the doctor panel."""
    from .backends.claude_cli import find_claude_cli
    from .doctor import gather_report

    config = dict(state.config)
    backend_kind = state.controller.backend_kind if state.controller else "unset"
    mcp_url = state.mcp.url if state.mcp else None

    def work() -> list[dict[str, Any]]:
        return gather_report(
            config=config,
            backend_kind=backend_kind,
            mcp_url=mcp_url,
            agent_home=USER_FILES / "agent-home",
            find_claude=find_claude_cli,
        )

    def done(future: Any) -> None:
        try:
            results = future.result()
        except Exception as exc:
            results = [{"label": "Doctor", "status": "broken", "detail": str(exc)}]
        if state.dock is not None:
            state.dock.bridge.push({"type": "doctor", "results": results})

    mw.taskman.run_in_background(work, done)


def _open_in_claude_code(target: str = "terminal") -> None:
    """Hand this chat to a full-power Claude Code.

    Terminal: same session id via --resume, same cwd (agent-home carries
    .mcp.json for our anki tools plus the skills dir).
    GUI (desktop app): the claude://code/new deep link cannot resume a
    session by id (not supported upstream yet), so we open a NEW desktop
    session in agent-home with a prompt pointing at our transcript file -
    the agent reads it and picks the conversation up from there.

    See DESIGN.md section 14 "Tracked upstream dependencies": when Claude
    Desktop gains a resume-by-uuid deep link, replace the transcript
    handoff below with a direct resume link.
    """
    import shlex
    import subprocess
    import sys as _sys
    import urllib.parse

    agent_home = USER_FILES / "agent-home"
    sid = state.controller.backend_session_id if state.controller else None

    def notice(text: str) -> None:
        if state.dock is not None:
            state.dock.bridge.push({"type": "notice", "text": text})

    if target == "gui":
        prompt = (
            "This continues an Anki chat from the Chat With Your Cards "
            "add-on (the anki MCP tools are configured in this folder)."
        )
        if state.transcripts is not None:
            state.transcripts.flush()
            transcript = (
                USER_FILES / "transcripts" / f"{state.transcripts.current_id}.json"
            )
            if transcript.exists():
                prompt += (
                    f" Read the conversation so far at {transcript} before "
                    "responding, then continue helping."
                )
        url = (
            "claude://code/new?folder="
            + urllib.parse.quote(str(agent_home), safe="")
            + "&q="
            + urllib.parse.quote(prompt, safe="")
        )
        try:
            if _sys.platform == "darwin":
                subprocess.run(["open", url], check=True, timeout=10)
            else:
                import webbrowser

                webbrowser.open(url)
            notice(
                "Opened in the Claude Code desktop app (new session - the "
                "app can't resume by id yet, so it reads this chat's "
                "transcript instead)."
            )
        except Exception:
            notice(f"Could not open the desktop app; deep link: {url}")
        return

    cmd = f"cd {shlex.quote(str(agent_home))} && claude"
    if sid:
        cmd += f" --resume {shlex.quote(str(sid))}"

    if _sys.platform == "darwin":
        script = cmd.replace("\\", "\\\\").replace('"', '\\"')
        try:
            subprocess.run(
                [
                    "osascript",
                    "-e", 'tell application "Terminal" to activate',
                    "-e", f'tell application "Terminal" to do script "{script}"',
                ],
                check=True,
                capture_output=True,
                timeout=15,
            )
            notice(
                "Opened this chat in Claude Code (Terminal). It has your anki "
                "tools via .mcp.json plus Claude Code's full toolset."
            )
            return
        except Exception:
            pass
    try:
        from aqt.qt import QApplication

        QApplication.clipboard().setText(cmd)
        notice(
            "Command copied to the clipboard - paste it into a terminal to "
            "continue this chat in Claude Code."
        )
    except Exception:
        notice(f"Run this in a terminal to continue in Claude Code: {cmd}")


def _open_session_browser() -> None:
    """One-click review of this session's AI-created notes in the Browser."""
    if state.proposals is None:
        return
    session_tag = state.proposals.session_tag
    if not session_tag:
        state.dock.bridge.push(
            {
                "type": "notice",
                "text": "Session tagging is off (session_tag_prefix is empty), so "
                "this session's notes can't be filtered in the Browser.",
            }
        ) if state.dock else None
        return
    from aqt import dialogs

    query = f'tag:"{session_tag}"'
    browser = dialogs.open("Browser", mw)
    try:
        browser.search_for(query)
    except AttributeError:
        browser.form.searchEdit.lineEdit().setText(query)
        browser.onSearchActivated()


def _teardown() -> None:
    if state.approvals is not None:
        state.approvals.deny_all()
    if state.controller is not None:
        state.controller.shutdown()
    if state.mcp is not None:
        state.mcp.stop()
        state.mcp = None
    _save_dock_width()


def _save_dock_width() -> None:
    if state.dock is None:
        return
    config = mw.addonManager.getConfig(__name__) or {}
    config["dock_width"] = state.dock.width()
    mw.addonManager.writeConfig(__name__, config)


if mw is not None:
    from aqt import gui_hooks

    mw.addonManager.setWebExports(__name__, r"web/.*\.(css|js)")
    gui_hooks.main_window_did_init.append(_setup)
    gui_hooks.profile_will_close.append(_teardown)
