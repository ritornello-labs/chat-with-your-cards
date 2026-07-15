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
    # The dock is always visible; collapsed means the slim rail. Both persist
    # across sessions (saved at teardown alongside dock_width).
    "dock_collapsed": True,
    "dock_side": "right",
    # Composer vim keybindings (Settings > "Vim keys in composer", or here).
    # vim_mappings are personal [keys, mapped-to, mode] triples with vim
    # `:map` semantics (mode: normal | insert | visual) - the user's vimrc
    # equivalent. Ships EMPTY by default (stock vim behavior; decided
    # 2026-07-14): personal mappings belong in the user's own config, e.g.
    # [["fd", "<Esc>", "insert"], ["j", "gj", "normal"]].
    "vim_mode": False,
    "vim_mappings": [],
    "theme": "teal",
    "backend": "auto",
    "claude_cli_path": "",
    "model": "",
    "effort": "",
    "fast_mode": False,
    "agent_tools": "sandbox",
    "web_access": True,
    "mcp_servers": {},
    "mcp_inherit_user": False,
    "mcp_disabled": [],
    "suggested_questions": True,
    "restore_last_chat": False,
    "open_in_claude_target": "terminal",
    "terminal_app": "",
    "source_fields": {},
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
    "custom_instructions": "",
    "created_tag": "ai-created",
    "edited_tag": "ai-edited",
    "session_tag_prefix": "ai-chat-dock::session-",
    "learning_nudge_threshold": 10,
    "learning_nudge_days": 7,
    "pins": {},
}

def _norm_agent_tools(value: Any) -> str:
    """Clamp an agent-tools value to a known tier (delegates to the controller's
    single source of truth; imported lazily to keep package import light)."""
    from .controller import _norm_agent_tools as _norm

    return _norm(value)


# Selectable colour palettes (ui styles.css cwyc-theme-* blocks). "teal" is the
# default; anything else falls back to it.
VALID_THEMES = ("teal", "indigo", "evergreen")


def _norm_theme(value: Any) -> str:
    v = str(value).strip()
    return v if v in VALID_THEMES else "teal"

# Visible kickoff message for the skill-review chat (the nudge chip and the
# History note make it clear this starts a NEW chat; the previous one stays
# resumable in History).
SKILL_REVIEW_PROMPT = (
    "I've been editing the cards you created or changed. Use "
    "get_edit_observations to see what I changed, look for recurring "
    "patterns across the observations (the skill-maintenance skill has the "
    "ground rules), explain in plain language what you notice, then propose "
    "an update to the card-authoring skill with propose_skill_update. If "
    "there is no real pattern, just say so."
)

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
    learning: Any = None
    last_checkpoint: Any = None
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


def _install_tools_menu(config: dict[str, Any]) -> None:
    """Add a clearly-labeled 'Chat With Your Cards' submenu to Anki's Tools
    menu, with verbs and their shortcuts — instead of a bare add-on-title
    checkbox that silently toggled dock visibility (which also diverged from
    what the Ctrl+J chord does). Both entries drive the same actions as the
    chords, so the menu and the keyboard agree."""
    from aqt.qt import QAction, QKeySequence, QMenu

    def hint(seq: str) -> str:
        return QKeySequence(seq).toString(QKeySequence.SequenceFormat.NativeText)

    menu = QMenu("Chat With Your Cards", mw)
    open_action = QAction(f"Toggle chat\t{hint(config['toggle_shortcut'])}", mw)
    open_action.triggered.connect(lambda *_a: toggle_chat_focus())
    new_action = QAction(f"New chat\t{hint(config['new_chat_shortcut'])}", mw)
    new_action.triggered.connect(lambda *_a: new_chat())
    menu.addAction(open_action)
    menu.addAction(new_action)
    mw.form.menuTools.addMenu(menu)


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

    state.dock = dock_mod.create_dock(
        dock_width=int(config["dock_width"]),
        collapsed=bool(config.get("dock_collapsed", True)),
        side=str(config.get("dock_side", "right")),
    )

    from .transcripts import TranscriptStore

    state.transcripts = TranscriptStore(USER_FILES / "transcripts")

    dock = state.dock

    def recording_push(payload: dict[str, Any]) -> None:
        # One pipe to the webview that also feeds the chat transcript
        # (TranscriptStore ignores payload types not worth replaying).
        if state.transcripts is not None:
            state.transcripts.record(payload)
        dock.bridge.push(payload)

    from .learning import LearningStore
    from .skills import (
        materialize_agent_environment,
        materialize_agent_skills,
        materialize_conventions_agent_skill,
    )

    # State the dock's hard tool limits where the agent AND its subagents see
    # them (agent-home/CLAUDE.md, loaded from cwd), so the agent stops trying
    # to write files / spawn subagents to do so (dogfood 2026-07-12).
    materialize_agent_environment(
        USER_FILES / "agent-home", str(config.get("agent_tools", "sandbox"))
    )
    conventions = load_conventions(USER_FILES, str(config.get("conventions_prompt", "")))
    # Mirror conventions into the agent-home skills dir so the harness
    # auto-discovers them (COMPLIANCE.md rule 3) instead of them being
    # inlined into --append-system-prompt - see context.build_system_prompt.
    materialize_conventions_agent_skill(USER_FILES / "agent-home", conventions)
    card_skill_path = materialize_agent_skills(USER_FILES / "agent-home")
    state.learning = LearningStore(USER_FILES / "learning", card_skill_path)

    state.proposals = ProposalManager(
        get_col=lambda: mw.col,
        push=recording_push,
        config=config,
        save_pins=_save_pins,
        after_write=_refresh_reviewer,
        checkpoint=_backup_checkpoint,
        observe=_learning_observe,
        apply_skill=_apply_skill_update,
        after_deck_change=_refresh_deck_ui,
    )

    def system_prompt() -> str:
        return build_system_prompt(
            permission_mode=str(config["permission_mode"]),
            agent_tools=str(config.get("agent_tools", "sandbox")),
            pins=state.proposals.pins if state.proposals else None,
            custom_instructions=str(config.get("custom_instructions", "")),
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
    _install_tools_menu(config)

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

    @property
    def learning(self) -> Any:
        return state.learning


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
    bridge.on("proposal_supersede", proposals.supersede)
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
    bridge.on(
        "set_open_in_claude_target",
        lambda msg: _set_open_target(str(msg.get("target", "terminal"))),
    )
    bridge.on("list_history", lambda _msg: controller.push_history_list())
    bridge.on("load_history", lambda msg: controller.load_history(str(msg.get("id", ""))))
    bridge.on("run_doctor", lambda _msg: _run_doctor())
    bridge.on("start_skill_review", lambda _msg: _start_skill_review())
    bridge.on(
        "tool_approval_response",
        lambda msg: state.approvals.respond(msg) if state.approvals else None,
    )
    bridge.on("set_agent", _set_agent)
    bridge.on("set_permission_mode", _set_permission_mode)
    bridge.on(
        "set_dock_expanded",
        lambda msg: state.dock.set_expanded(bool(msg.get("expanded", True)))
        if state.dock is not None
        else None,
    )
    bridge.on("set_setting", _set_setting)
    bridge.on("open_addon_config", lambda _msg: _open_addon_config())
    # Saving the add-on's config in Anki's built-in editor re-pushes the live
    # preferences (vim mode/mappings, theme, dock side, ...) so an edit applies
    # without an Anki restart.
    mw.addonManager.setConfigUpdatedAction(__name__, _on_config_updated)


def _mark_web_ready() -> None:
    state.web_ready = True
    if state.dock is not None:
        state.dock.web.eval("window.chatUI && window.chatUI.ackReady();")
    if state.proposals is not None:
        state.proposals.push_ui_state()
    if state.controller is not None:
        state.controller.push_agent_state()
    if state.dock is not None:
        state.dock.bridge.push(
            {
                "type": "ui_config",
                "suggested_questions": bool(
                    state.config.get("suggested_questions", True)
                ),
                "open_in_claude_target": _norm_open_target(
                    state.config.get("open_in_claude_target", "terminal")
                ),
            }
        )
    if state.controller is not None:
        state.controller.push_context_chip()
    # Authoritative dock + settings state (the body fragment planted the
    # initial dock state for first paint; this keeps a reloaded webview honest
    # and feeds the Settings panel).
    if state.dock is not None:
        state.dock.push_state(animating=False)
    _push_settings()
    _push_collection_meta()
    _scan_learning()
    # Optionally reopen the last chat where the user left off (default off:
    # a fresh chat each launch). Runs after the initial UI state is pushed.
    if state.controller is not None and bool(state.config.get("restore_last_chat")):
        state.controller.restore_last_chat()


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


def _open_addon_config() -> None:
    """Settings > 'Edit config…': open Anki's config editor directly on THIS
    add-on's config (the vimrc equivalent - vim_mappings - lives there, plus
    every advanced key documented in config.md). Anki 25.09 has no
    mw.onAddons (first cut failed silently on that - dogfood 2026-07-14);
    the real path is AddonsDialog(mw.addonManager) + ConfigEditor(dlg, addon,
    conf), both self-showing. Any failure now surfaces as a notice, never
    silence."""

    def notice(text: str) -> None:
        if state.dock is not None:
            state.dock.bridge.push({"type": "notice", "text": text})

    try:
        from aqt.addons import AddonsDialog, ConfigEditor

        addon = mw.addonManager.addonFromModule(__name__)
        conf = mw.addonManager.getConfig(__name__) or {}
        dlg = AddonsDialog(mw.addonManager)
        ConfigEditor(dlg, addon, conf)
        notice(
            "Config editor opened. vim_mappings holds [keys, mapped-to, mode] "
            "triples (vim :map semantics); restart Anki to apply changes."
        )
    except Exception as exc:
        _log_line(f"open_addon_config failed: {exc}")
        notice(
            "Couldn't open the config editor - use Tools > Add-ons > "
            '"Chat With Your Cards" > Config instead.'
        )


def _push_settings() -> None:
    """Feed the Settings panel (gear icon) its authoritative snapshot."""
    if state.dock is None:
        return
    # Only well-formed [keys, mapped-to, mode] string triples reach the UI;
    # a malformed hand-edited config entry is dropped, never crashes vim.
    mappings = [
        [str(m[0]), str(m[1]), str(m[2])]
        for m in (state.config.get("vim_mappings") or [])
        if isinstance(m, (list, tuple)) and len(m) == 3
    ]
    state.dock.bridge.push(
        {
            "type": "settings",
            "restore_last_chat": bool(state.config.get("restore_last_chat", False)),
            "dock_side": str(state.config.get("dock_side", "right")),
            "toggle_shortcut": str(state.config.get("toggle_shortcut", "")),
            "new_chat_shortcut": str(state.config.get("new_chat_shortcut", "")),
            "vim_mode": bool(state.config.get("vim_mode", False)),
            "vim_mappings": mappings,
            "theme": _norm_theme(state.config.get("theme")),
        }
    )


def _set_setting(msg: dict[str, Any]) -> None:
    """Settings-panel writes: a small whitelist, each persisted via
    writeConfig and applied live where it can be. Unknown keys are ignored
    (never a generic config poke - the panel is not a JSON editor)."""
    key = str(msg.get("key", ""))
    value = msg.get("value")
    if key == "restore_last_chat":
        state.config["restore_last_chat"] = bool(value)
    elif key == "vim_mode":
        state.config["vim_mode"] = bool(value)
    elif key == "theme":
        state.config["theme"] = _norm_theme(value)
    elif key == "dock_side":
        side = "left" if value == "left" else "right"
        state.config["dock_side"] = side
        if state.dock is not None:
            from .dock import move_dock

            move_dock(state.dock, side)
    else:
        return
    config = mw.addonManager.getConfig(__name__) or {}
    config[key] = state.config[key]
    mw.addonManager.writeConfig(__name__, config)
    _push_settings()


def _on_config_updated(*_args: Any) -> None:
    """Anki's built-in add-on config editor was saved (registered via
    setConfigUpdatedAction). Reload config and re-push the *preferences* that
    can change live - vim mode/mappings, theme, dock side, shortcuts, suggested
    questions, open-in-Claude target - so editing them (e.g. a vim mapping) now
    applies WITHOUT an Anki restart. Agent keys (model/effort/fast/agent_tools/
    permission_mode) keep their existing "applies on your next message"
    semantics and are deliberately not re-applied here, so a config edit can't
    yank the model out from under an in-flight chat. Reads getConfig fresh
    rather than the passed arg, so it's robust to the callback's signature."""
    if state.dock is None:
        return
    merged = dict(DEFAULT_CONFIG)
    merged.update(mw.addonManager.getConfig(__name__) or {})
    # Mutate the existing dict in place - do NOT rebind `state.config`. Other
    # components (ProposalManager, controller, dock) captured a reference to
    # this dict at init; rebinding would leave them reading a stale config.
    state.config.clear()
    state.config.update(merged)
    desired_side = "left" if merged.get("dock_side") == "left" else "right"
    if state.dock.side != desired_side:
        from .dock import move_dock

        move_dock(state.dock, desired_side)
    _push_settings()
    state.dock.bridge.push(
        {
            "type": "ui_config",
            "suggested_questions": bool(merged.get("suggested_questions", True)),
            "open_in_claude_target": _norm_open_target(
                merged.get("open_in_claude_target", "terminal")
            ),
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
    # Default to the current stored value (not False) when "fast" is absent,
    # so a hand-crafted/legacy set_agent command that only carries
    # model/effort can't silently flip fast mode off.
    fast_mode = bool(msg.get("fast", state.config.get("fast_mode", False)))
    # Same guard for the agent-tools axis: a tools-less command keeps whatever
    # the user last chose instead of silently resetting to sandbox.
    agent_tools = _norm_agent_tools(msg.get("tools", state.config.get("agent_tools", "sandbox")))
    state.controller.set_agent_config(model, effort, fast_mode, agent_tools)
    config = mw.addonManager.getConfig(__name__) or {}
    config["model"] = state.config.get("model", "")
    config["effort"] = state.config.get("effort", "")
    config["fast_mode"] = state.config.get("fast_mode", False)
    config["agent_tools"] = state.config.get("agent_tools", "sandbox")
    mw.addonManager.writeConfig(__name__, config)


def _norm_open_target(target: Any) -> str:
    """Canonical open-in-Claude target vocabulary is 'terminal' | 'desktop'.
    'gui' is the legacy word (classic UI / older config) and maps to 'desktop'
    so a config written before the assistant-ui rebuild still works. Anything
    else falls back to 'terminal'."""
    t = str(target).strip().lower()
    if t in ("desktop", "gui"):
        return "desktop"
    return "terminal"


def _set_open_target(target: str) -> None:
    """Persist the default 'Open in Claude Code' target (terminal / desktop)
    the split button acts on, so it survives restarts. Normalized so the new
    UI's 'desktop' is accepted (the old check rejected it - dogfood
    2026-07-12: picking Desktop silently did nothing, then opened terminal)."""
    normalized = _norm_open_target(target)
    state.config["open_in_claude_target"] = normalized
    config = mw.addonManager.getConfig(__name__) or {}
    config["open_in_claude_target"] = normalized
    mw.addonManager.writeConfig(__name__, config)


def _save_pins(pins: dict[str, Any]) -> None:
    config = mw.addonManager.getConfig(__name__) or {}
    config["pins"] = pins
    mw.addonManager.writeConfig(__name__, config)


def _learning_observe(event: dict[str, Any]) -> None:
    """Capture hook the ProposalManager fires at content-write sites; feeds
    the learning store (DESIGN.md section 15). Best-effort: learning must
    never break a write."""
    store = state.learning
    if store is None or mw is None:
        return
    kind = str(event.get("event", ""))
    try:
        note_ids = [int(n) for n in (event.get("note_ids") or [])]
        if kind == "applied":
            store.snapshot_notes(mw.col, note_ids)
        elif kind == "resync":
            store.snapshot_notes(mw.col, note_ids, add=False)
        elif kind == "reviewed":
            recorded = store.record_review(
                proposal_kind=str(event.get("proposal_kind", "")),
                note_type=str(event.get("note_type", "")),
                deck_before=str(event.get("deck_before", "")),
                deck_after=str(event.get("deck_after", "")),
                tags_before=event.get("tags_before"),
                tags_after=event.get("tags_after"),
                fields_before=event.get("fields_before"),
                fields_after=event.get("fields_after"),
                declined_fields=event.get("declined_fields"),
            )
            if recorded:
                _push_learning_state()
    except Exception as exc:
        _log_line(f"learning capture failed ({kind}): {exc}")


def _apply_skill_update(proposal: Any) -> list[str]:
    """Accepted skill-update proposal: archive the prior skill, write the new
    one, consume the observations it was based on."""
    from .proposals import ProposalError

    store = state.learning
    if store is None:
        raise ProposalError("the learning store is not available")
    backup = store.write_skill(str(proposal.op_args.get("new_content", "")))
    store.consume([str(i) for i in proposal.op_args.get("observation_ids") or []])
    _push_learning_state()
    if backup is not None:
        return [
            "Previous skill version archived as "
            f"user_files/learning/skill-backups/{backup.name}"
        ]
    return []


def _push_learning_state() -> None:
    """Nudge chip state: pending observation count + whether to nudge."""
    if state.dock is None or state.learning is None:
        return
    nudge = state.learning.nudge_state(
        int(state.config.get("learning_nudge_threshold", 10)),
        int(state.config.get("learning_nudge_days", 7)),
    )
    state.dock.bridge.push({"type": "learning", **nudge})


def _scan_learning() -> None:
    """Diff tracked notes against their snapshots (catches edits made in the
    editor, the Browser, or on another device after sync). Cheap: one bulk
    mod query, full reads only for changed notes."""
    if state.learning is None or mw is None or mw.col is None:
        return
    try:
        found = state.learning.scan(mw.col)
        if found:
            _log_line(f"learning scan: {found} new observation(s)")
    except Exception as exc:
        _log_line(f"learning scan failed: {exc}")
    _push_learning_state()


def _start_skill_review() -> None:
    """Nudge chip clicked: fresh scan, then a NEW chat seeded with the
    reflection prompt (the previous chat stays resumable in History)."""
    if state.controller is None:
        return
    _scan_learning()
    state.controller.new_chat()
    if state.dock is not None:
        # The webview normally renders the user bubble itself on send; this
        # send originates in Python, so push it explicitly (transcripts get
        # it from send_user_message - a plain bridge push avoids recording
        # the message twice).
        state.dock.bridge.push({"type": "user_message", "text": SKILL_REVIEW_PROMPT})
    state.controller.send_user_message(SKILL_REVIEW_PROMPT)


def _log_line(message: str) -> None:
    """Append one line to the shared backend log (best-effort)."""
    try:
        from .backends.claude_cli import BackendLog

        BackendLog(USER_FILES / "logs" / "backend.log").write(message)
    except Exception:
        pass


def _backup_checkpoint(reason: str, critical: bool = False) -> bool:
    """Force an Anki backup before bulk/delete/change-set applies, so even
    non-ledger-revertible operations have a way back.

    critical=True (irreversible ops like delete) waits for the backup to
    finish so it is on disk before the destructive change proceeds -
    otherwise the "safety net" would be racing the delete. Reversible ops
    (ledger-undoable) back up asynchronously to avoid stalling the UI, and
    on a huge collection even that is best-effort insurance behind the
    ledger.

    Returns True when the checkpoint is safe to proceed on, False only when
    it actually failed. ``col.create_backup(force=True, ...)`` can legitimately
    return False for "nothing has changed since the last backup" (see its
    pylib docstring) - that is NOT a failure, there is still a good backup on
    disk, so we still return True. Only an exception - the documented failure
    signal - means no safety net exists. ProposalManager (proposals.py) treats
    False as fatal for critical=True writes (delete: the op ABORTS rather than
    proceed with no way back) and as a surfaced warning otherwise; this
    function's job is only to report the outcome honestly, never to swallow
    it (previously it silently ate the exception and let the write proceed
    regardless)."""
    state.last_checkpoint = {"reason": reason, "critical": critical,
                             "created": None, "error": None}
    if mw is None or mw.col is None:
        state.last_checkpoint["error"] = "collection is not open"
        return False
    try:
        created = mw.col.create_backup(
            backup_folder=mw.pm.backupFolder(),
            force=True,
            wait_for_completion=critical,
        )
        state.last_checkpoint["created"] = bool(created)
        return True
    except Exception as exc:
        # Backups are defense-in-depth, but a failure must be visible (backend
        # log + doctor) and, for critical writes, must block the op - see the
        # docstring above.
        state.last_checkpoint["error"] = str(exc)
        _log_line(f"backup checkpoint failed ({reason}): {exc}")
        return False


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


def _refresh_deck_ui() -> None:
    """Redraw the deck list / overview after a deck operation, so a created,
    renamed, or rebuilt deck shows without the user switching screens."""
    if mw is None:
        return
    try:
        if mw.state == "deckBrowser":
            mw.deckBrowser.refresh()
        elif mw.state == "overview":
            mw.overview.refresh()
    except Exception:
        pass  # cosmetic refresh; never let it break the write


def _run_doctor() -> None:
    """Gather the setup report off the main thread (version subprocesses can
    take a second each) and push it to the doctor panel."""
    from .backends.claude_cli import find_claude_cli
    from .doctor import gather_report

    config = dict(state.config)
    backend_kind = state.controller.backend_kind if state.controller else "unset"
    mcp_url = state.mcp.url if state.mcp else None
    learning_stats = state.learning.stats() if state.learning else None

    def work() -> list[dict[str, Any]]:
        return gather_report(
            config=config,
            backend_kind=backend_kind,
            mcp_url=mcp_url,
            agent_home=USER_FILES / "agent-home",
            find_claude=find_claude_cli,
            learning=learning_stats,
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
    .mcp.json for our anki tools plus the skills dir), same agent settings
    (model/effort/fast/agent-tools flags).
    Desktop app: there is STILL no resume-by-id deep link (re-verified
    2026-07-13, DESIGN.md section 14) - but interactive claude executes a
    slash command passed as the initial prompt (verified empirically with
    /cost under a PTY), and `/desktop` migrates the CURRENT session to the
    desktop app. So the desktop path resumes the session in a terminal and
    immediately runs /desktop: a true resume, with the terminal window as
    scaffolding. Fallback (no session yet, non-macOS, or terminal
    automation failed): the old claude://code/new deep link opening a NEW
    desktop session pointed at this chat's transcript file.
    """
    import shlex
    import subprocess
    import sys as _sys
    import urllib.parse

    agent_home = USER_FILES / "agent-home"
    # Live session id if a message was exchanged; else the resume id of a chat
    # loaded from History (session not respawned yet) - either lets the target
    # continue THIS conversation rather than starting blank.
    sid = state.controller.backend_session_id if state.controller else None
    if not sid and state.transcripts is not None:
        sid = state.transcripts.backend_session_id

    def notice(text: str) -> None:
        if state.dock is not None:
            state.dock.bridge.push({"type": "notice", "text": text})

    # Carry the dock's agent settings into the handed-off session so the
    # conversation continues under the same model/effort/fast-mode - and the
    # same environment power: full agent tools maps to bypassPermissions,
    # exactly what the dock itself spawns with (user-requested 2026-07-13).
    # The collection-write permission mode needs no flag: it is enforced live
    # by our MCP server, which the handed-off session talks to via .mcp.json.
    from .backends.claude_cli import VALID_EFFORTS

    extra_args: list[str] = []
    model = str(state.config.get("model", "")).strip()
    effort = str(state.config.get("effort", "")).strip()
    if model:
        extra_args += ["--model", model]
    if effort in VALID_EFFORTS:
        extra_args += ["--effort", effort]
    if state.config.get("fast_mode"):
        import json as _json

        extra_args += ["--settings", _json.dumps({"fastMode": True})]
    # Carry the dock's agent-tools tier into the resumed session so the terminal
    # / desktop handoff starts in the same posture (sandbox adds nothing - a real
    # terminal has an interactive approver, so the human gates tools there).
    _resume_perm_mode = {
        "acceptEdits": "acceptEdits",
        "auto": "auto",
        "full": "bypassPermissions",
    }.get(_norm_agent_tools(state.config.get("agent_tools", "sandbox")))
    if _resume_perm_mode:
        extra_args += ["--permission-mode", _resume_perm_mode]

    def resume_argv(initial_prompt: str | None = None) -> list[str]:
        argv = ["claude"]
        if sid:
            argv += ["--resume", str(sid)]
        argv += extra_args
        if initial_prompt is not None:
            argv.append(initial_prompt)
        return argv

    def resume_command(initial_prompt: str | None = None) -> str:
        # POSIX-shell form (macOS/Linux terminals and the clipboard fallback).
        parts = " ".join(shlex.quote(a) for a in resume_argv(initial_prompt))
        return f"cd {shlex.quote(str(agent_home))} && {parts}"

    if _norm_open_target(target) == "desktop":
        if sid and _desktop_handoff_invisible(sid, extra_args, agent_home):
            notice(
                "Resuming this chat and handing it to the Claude Code desktop "
                "app… (runs invisibly; you'll get a notice if it can't)."
            )
            return
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
                "Opened in the Claude Code desktop app (new session reading "
                "this chat's transcript - no session to truly resume here)."
            )
        except Exception:
            notice(f"Could not open the desktop app; deep link: {url}")
        return

    cmd = resume_command()
    app = str(state.config.get("terminal_app", "")).strip()
    opened = False
    if _sys.platform == "darwin":
        opened = _open_macos_terminal(cmd, app)
    elif _sys.platform.startswith("linux"):
        opened = _open_linux_terminal(cmd)
    elif _sys.platform == "win32":
        opened = _open_windows_terminal(resume_argv(), agent_home)
    if opened:
        where = app or ("Terminal" if _sys.platform == "darwin" else "a terminal window")
        if sid:
            carried = " under this chat's model/effort settings" if extra_args else ""
            notice(
                f"Resumed this chat in Claude Code ({where}){carried} - it has "
                "your anki tools via .mcp.json plus Claude Code's full toolset."
            )
        else:
            # No exchanged message yet -> no session to resume. Be honest
            # rather than implying the conversation carried over (dogfood
            # 2026-07-12: 'the chat wasn't even loaded there').
            notice(
                f"Opened a fresh Claude Code ({where}) in this chat's folder "
                "(no message sent yet, so there was nothing to resume)."
            )
        return
    try:
        from aqt.qt import QApplication

        QApplication.clipboard().setText(cmd)
        notice(
            "Command copied to the clipboard - paste it into a terminal to "
            "continue this chat in Claude Code."
        )
    except Exception:
        notice(f"Run this in a terminal to continue in Claude Code: {cmd}")


def _desktop_handoff_invisible(
    sid: str, extra_args: list[str], agent_home: Path
) -> bool:
    """Migrate this chat to the Claude Code desktop app with NO visible
    terminal: run `claude --resume <sid> … "/desktop"` on a hidden PTY (the
    CLI needs a tty to run interactively; it does not need a window - the
    /desktop-under-script(1) probe proved this, 2026-07-13). POSIX only
    (macOS + Linux; Windows has no pty module and falls back to the
    deep-link path).

    A watcher thread drains the PTY (the TUI blocks if nobody reads) and
    reports: clean exit = migrated (that is /desktop's behavior - hand off,
    then exit); still running at the deadline = migration unavailable
    (older CLI / API-key auth), so terminate and tell the user the visible
    fallback. Returns False only on spawn failure, so the caller can fall
    back to the deep link immediately."""
    import os
    import select
    import subprocess
    import sys as _sys
    import threading

    if _sys.platform != "darwin" and not _sys.platform.startswith("linux"):
        return False
    from .backends.claude_cli import find_claude_cli

    claude = find_claude_cli(str(state.config.get("claude_cli_path", "")).strip())
    if not claude:
        return False
    try:
        import pty

        master, slave = pty.openpty()
    except Exception:
        return False
    env = dict(os.environ)
    env.setdefault("TERM", "xterm-256color")
    try:
        proc = subprocess.Popen(
            [claude, "--resume", str(sid), *extra_args, "/desktop"],
            cwd=str(agent_home),
            stdin=slave,
            stdout=slave,
            stderr=slave,
            env=env,
            start_new_session=True,
        )
    except Exception as exc:
        os.close(master)
        os.close(slave)
        _log_line(f"invisible /desktop spawn failed: {exc}")
        return False
    os.close(slave)

    def notice_on_main(text: str) -> None:
        def push() -> None:
            if state.dock is not None:
                state.dock.bridge.push({"type": "notice", "text": text})

        mw.taskman.run_on_main(push)

    def watch() -> None:
        import time

        deadline = time.monotonic() + 90
        try:
            while time.monotonic() < deadline and proc.poll() is None:
                readable, _, _ = select.select([master], [], [], 0.5)
                if readable:
                    try:
                        if not os.read(master, 4096):
                            break
                    except OSError:
                        break
        finally:
            try:
                os.close(master)
            except OSError:
                pass
        if proc.poll() == 0:
            notice_on_main("Handed this chat to the Claude Code desktop app.")
            return
        if proc.poll() is None:
            proc.terminate()
        _log_line(f"invisible /desktop did not migrate (exit={proc.poll()})")
        notice_on_main(
            "The desktop migration didn't complete (older CLI or API-key "
            "auth?). Fallback: Open in Claude Code > Terminal, then type "
            "/desktop there."
        )

    threading.Thread(target=watch, name="cwyc-desktop-handoff", daemon=True).start()
    return True


def _open_linux_terminal(command: str) -> bool:
    """Open a visible terminal running `command` on Linux (user-requested
    2026-07-14 - the terminal handoff was macOS-only). Best-effort sweep of
    common emulators; the first one found wins. Returns False so the caller
    can fall back to the clipboard."""
    import shutil
    import subprocess

    candidates: list[tuple[str, list[str]]] = [
        ("x-terminal-emulator", ["-e"]),  # Debian alternatives symlink
        ("gnome-terminal", ["--"]),
        ("konsole", ["-e"]),
        ("xfce4-terminal", ["-x"]),
        ("kitty", []),
        ("alacritty", ["-e"]),
        ("wezterm", ["start", "--"]),
        ("xterm", ["-e"]),
    ]
    for name, flags in candidates:
        path = shutil.which(name)
        if not path:
            continue
        try:
            subprocess.Popen(
                [path, *flags, "bash", "-lc", command], start_new_session=True
            )
            return True
        except Exception:
            continue
    return False


def _open_windows_terminal(argv: list[str], cwd: Path) -> bool:
    """Open a visible cmd window running `argv` on Windows (user-requested
    2026-07-14). `start` is a cmd builtin, hence shell=True; the empty ""
    is the window title - without it, start eats a quoted program path as
    the title. /k keeps the window open, which is the point (it hosts the
    interactive resumed session)."""
    import subprocess

    try:
        quoted = subprocess.list2cmdline(argv)
        subprocess.Popen(f'start "" cmd /k "{quoted}"', shell=True, cwd=str(cwd))
        return True
    except Exception:
        return False


def _open_macos_terminal(command: str, app: str) -> bool:
    """Run `command` in a macOS terminal, honoring the configured terminal_app.

    Empty (or "Terminal") drives Apple Terminal via AppleScript `do script`
    (the known-good default). Any other app name launches a throwaway
    executable `.command` script in that app via `open -a`, which most
    terminals (iTerm, Warp, Ghostty, kitty, …) run on open. Best-effort:
    returns False so the caller can fall back to the clipboard."""
    import subprocess
    import sys as _sys

    if _sys.platform != "darwin":
        return False
    if not app or app.lower() in ("terminal", "terminal.app"):
        script = command.replace("\\", "\\\\").replace('"', '\\"')
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
            return True
        except Exception:
            return False
    try:
        import os as _os
        import stat
        import tempfile

        fd, path = tempfile.mkstemp(suffix=".command", prefix="cwyc-cc-")
        with _os.fdopen(fd, "w") as handle:
            handle.write(f"#!/bin/bash\n{command}\n")
        _os.chmod(path, _os.stat(path).st_mode | stat.S_IXUSR)
        subprocess.run(
            ["open", "-a", app, path], check=True, capture_output=True, timeout=15
        )
        return True
    except Exception:
        return False


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
    # expanded_width, not width(): while collapsed the live width is the rail.
    config["dock_width"] = state.dock.expanded_width
    config["dock_collapsed"] = not state.dock.expanded
    mw.addonManager.writeConfig(__name__, config)


if mw is not None:
    from aqt import gui_hooks

    mw.addonManager.setWebExports(__name__, r"web/.*\.(css|js)")
    gui_hooks.main_window_did_init.append(_setup)
    gui_hooks.profile_will_close.append(_teardown)
