"""Keyboard chords and the context-aware focus toggle (DESIGN.md section 9)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from aqt import mw
from aqt.qt import QKeySequence, QShortcut

if TYPE_CHECKING:
    from . import AddonState


def toggle_chat_focus(state: AddonState) -> None:
    """One chord, three context-aware behaviors."""
    dock = state.dock
    if dock is None:
        return
    if not dock.isVisible():
        dock.focus_composer()
        return
    focused = mw.app.focusWidget()
    if focused is not None and dock.isAncestorOf(focused):
        focus_main_window()
    else:
        dock.focus_composer()


def focus_main_window() -> None:
    """Return focus to the reviewer/deck browser (they all live in mw.web)."""
    mw.activateWindow()
    mw.web.setFocus()


def new_chat(state: AddonState) -> None:
    """Fresh chat with fresh context; focus stays in the composer."""
    if state.controller is not None:
        state.controller.new_chat()
    if state.dock is not None:
        state.dock.focus_composer()


def register_shortcuts(state: AddonState) -> None:
    toggle = QShortcut(QKeySequence(state.config["toggle_shortcut"]), mw)
    toggle.activated.connect(lambda: toggle_chat_focus(state))

    fresh = QShortcut(QKeySequence(state.config["new_chat_shortcut"]), mw)
    fresh.activated.connect(lambda: new_chat(state))

    state.shortcuts = [toggle, fresh]
