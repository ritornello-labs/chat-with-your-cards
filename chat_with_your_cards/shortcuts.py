"""Keyboard chords and the context-aware focus toggle (DESIGN.md section 9)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from aqt import mw
from aqt.qt import QKeySequence, QShortcut

if TYPE_CHECKING:
    from . import AddonState


def toggle_chat_focus(state: AddonState) -> None:
    """One chord, three context-aware behaviors: expand the rail (and focus
    the composer), focus an expanded-but-unfocused chat, or hand focus back
    to the reviewer. The dock itself is always visible (rail when collapsed);
    collapsing is the header's chevron or the Escape-like click on the rail's
    own collapse affordance, not this chord."""
    dock = state.dock
    if dock is None:
        return
    if not dock.expanded or not dock.isVisible():
        _focus_chat(state)
        return
    focused = mw.app.focusWidget()
    if focused is not None and dock.isAncestorOf(focused):
        focus_main_window()
    else:
        _focus_chat(state)


def _focus_chat(state: AddonState) -> None:
    assert state.dock is not None
    state.dock.focus_composer()
    # Pre-warm on focus: spawn the CLI backend while the user types, so the
    # first send has no startup latency (DESIGN.md section 9).
    if state.controller is not None:
        state.controller.ensure_ready()


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
