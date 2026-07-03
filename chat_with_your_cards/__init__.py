"""Chat With Your Cards - a collapsible AI chat dock for Anki.

Importable without Anki (unit tests import .backends); everything that
touches aqt is guarded behind the mw check below.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from .controller import ChatController
    from .dock import ChatDock

DEFAULT_CONFIG: dict[str, Any] = {
    "toggle_shortcut": "Ctrl+J",
    "new_chat_shortcut": "Ctrl+Shift+J",
    "dock_width": 420,
}


@dataclass
class AddonState:
    dock: Optional[ChatDock] = None
    controller: Optional[ChatController] = None
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
    from .controller import ChatController

    config = dict(DEFAULT_CONFIG)
    config.update(mw.addonManager.getConfig(__name__) or {})
    state.config = config

    state.dock = dock_mod.create_dock(dock_width=int(config["dock_width"]))
    state.controller = ChatController(push=state.dock.bridge.push)
    _wire_bridge()
    shortcuts_mod.register_shortcuts(state)


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
    gui_hooks.profile_will_close.append(_save_dock_width)
