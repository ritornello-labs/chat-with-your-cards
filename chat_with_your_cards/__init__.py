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
    "compact_tool_descriptions": True,
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

    state.proposals = ProposalManager(
        get_col=lambda: mw.col,
        push=state.dock.bridge.push,
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
        push=state.dock.bridge.push,
        config=config,
        system_prompt_builder=system_prompt,
        ensure_mcp=_ensure_mcp,
        workdir=USER_FILES / "agent-home",
        proposals=state.proposals,
    )
    _wire_bridge()
    shortcuts_mod.register_shortcuts(state)


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

        def execute_tool(name: str, args: dict[str, Any]) -> Any:
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
                registry.specs(include_writes=not read_only, include_trusted=trusted),
                compact=bool(state.config.get("compact_tool_descriptions", True)),
            ),
            execute_tool=execute_tool,
        )
        state.mcp.start()
    return state.mcp.url, state.mcp.token


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
    bridge.on("set_agent", _set_agent)
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
