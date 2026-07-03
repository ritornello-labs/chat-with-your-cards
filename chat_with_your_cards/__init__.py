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
    from .stats import StatsCache

DEFAULT_CONFIG: dict[str, Any] = {
    "toggle_shortcut": "Ctrl+J",
    "new_chat_shortcut": "Ctrl+Shift+J",
    "dock_width": 420,
    "backend": "auto",
    "claude_cli_path": "",
    "permission_mode": "default",
    "stats_refresh_minutes": 30,
    "context_token_budget": 8000,
}

USER_FILES = Path(__file__).resolve().parent / "user_files"


@dataclass
class AddonState:
    dock: Optional[ChatDock] = None
    controller: Optional[ChatController] = None
    stats_cache: Optional[StatsCache] = None
    mcp: Optional[McpServer] = None
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

    def system_prompt() -> str:
        cache = state.stats_cache
        overview = (
            cache.overview(int(config["context_token_budget"])) if cache else None
        )
        return build_system_prompt(
            overview, permission_mode=str(config["permission_mode"])
        )

    state.controller = ChatController(
        push=state.dock.bridge.push,
        config=config,
        system_prompt_builder=system_prompt,
        ensure_mcp=_ensure_mcp,
        workdir=USER_FILES / "agent-home",
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


def _ensure_mcp() -> tuple[str, str]:
    """Start the MCP server on first use; returns (url, bearer token)."""
    if state.mcp is None:
        from .mcp_server import McpServer, tool_specs_for_mcp
        from .tools import build_registry

        registry = build_registry()
        ctx = _ToolCtx()
        read_only = state.config.get("permission_mode") == "read-only"

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
                registry.specs(include_writes=not read_only)
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


def _mark_web_ready() -> None:
    state.web_ready = True
    if state.dock is not None:
        state.dock.web.eval("window.chatUI && window.chatUI.ackReady();")


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
