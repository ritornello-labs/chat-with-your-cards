"""Keyboard chords and the context-aware focus toggle (DESIGN.md section 9)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from aqt import mw
from aqt.qt import QKeySequence, QShortcut

if TYPE_CHECKING:
    from . import AddonState


def toggle_chat_focus(state: AddonState) -> None:
    """One chord, three context-aware behaviors that CYCLE the shell
    (user-requested 2026-07-13): collapsed rail -> expand + focus the
    composer; expanded but focus elsewhere -> focus the composer; focus in
    the chat -> collapse to the rail and hand focus back to the reviewer.
    So tapping Ctrl+J twice opens then puts the whole panel away. Esc keeps
    its gentler role: return focus WITHOUT collapsing."""
    dock = state.dock
    if dock is None:
        return
    if not dock.expanded or not dock.isVisible():
        _focus_chat(state)
        return
    focused = mw.app.focusWidget()
    if focused is not None and dock.isAncestorOf(focused):
        dock.set_expanded(False)
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
    """(Re)bind the chords. Safe to call again after a config edit: the old
    QShortcuts are disposed first, so editing a chord takes effect without an
    Anki restart - which is what config.md has always claimed, and what did
    not actually happen (found 2026-07-27: nothing re-registered these)."""
    for existing in state.shortcuts:
        try:
            existing.setEnabled(False)
            existing.setParent(None)
        except Exception:
            pass
    state.shortcuts = []

    toggle = QShortcut(QKeySequence(state.config["toggle_shortcut"]), mw)
    toggle.activated.connect(lambda: toggle_chat_focus(state))

    fresh = QShortcut(QKeySequence(state.config["new_chat_shortcut"]), mw)
    fresh.activated.connect(lambda: new_chat(state))

    state.shortcuts = [toggle, fresh]
