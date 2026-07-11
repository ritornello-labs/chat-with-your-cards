"""The chat dock: a QDockWidget hosting an AnkiWebView with the chat UI."""

from __future__ import annotations

from typing import Any

from aqt import mw
from aqt.qt import QDockWidget, Qt
from aqt.webview import AnkiWebView

from .bridge import Bridge

DOCK_OBJECT_NAME = "chat_with_your_cards_dock"
DOCK_TITLE = "Chat With Your Cards"

# Floor on the dock width. The width persists across sessions, so an
# accidental drag to a sliver otherwise reopens as an unusable ~70px column
# (composer wrapping one word per line). This both clamps a too-small saved
# value on load and hard-stops the user from dragging below it. 320 keeps the
# composer control row on a single line for the common labels; longer ones
# wrap gracefully (the assistant-ui bundle's composer styling) rather than
# overflow.
MIN_DOCK_WIDTH = 320


class ChatDock(QDockWidget):
    def __init__(self, dock_width: int) -> None:
        super().__init__(DOCK_TITLE, mw)
        self._configured_width = max(int(dock_width), MIN_DOCK_WIDTH)
        self._width_applied = False
        self.setMinimumWidth(MIN_DOCK_WIDTH)
        self.setObjectName(DOCK_OBJECT_NAME)
        self.setAllowedAreas(
            Qt.DockWidgetArea.LeftDockWidgetArea | Qt.DockWidgetArea.RightDockWidgetArea
        )
        # Docked-only by design: no DockWidgetFloatable, so the panel can never
        # be torn off into a stray floating window (user request 2026-07-06).
        # Movable is kept so it can still swap between the left/right edges.
        self.setFeatures(
            QDockWidget.DockWidgetFeature.DockWidgetClosable
            | QDockWidget.DockWidgetFeature.DockWidgetMovable
        )
        self.web = AnkiWebView(parent=self, title="chat with your cards")
        # The ready ping and early messages must not be dropped before the
        # collection opens (webview.py drops bridge cmds when requiresCol).
        self.web.requiresCol = False
        self.setWidget(self.web)
        self.bridge = Bridge(self.web)
        self._load_ui()

    def _load_ui(self) -> None:
        # The assistant-ui frontend is the only UI (DESIGN.md section 9,
        # 2026-07-11). Anki loads the committed bundle from web/next/ via the
        # standard stdHtml path: a <div id="cwyc-root"></div> body fragment
        # plus the bundle registered as web exports. The bundle defers mounting
        # until DOMContentLoaded (stdHtml injects js= into <head>, before this
        # body fragment exists) and creates its own #cwyc-root if absent. The
        # standalone web/next/index.html the build emits is a dev-only artifact
        # and is never loaded here.
        addon_pkg = mw.addonManager.addonFromModule(__name__)
        base = f"/_addons/{addon_pkg}/web"
        self.web.stdHtml(
            body='<div id="cwyc-root"></div>',
            css=[f"{base}/next/bundle.css"],
            js=[f"{base}/next/bundle.js"],
            context=self,
        )

    def focus_composer(self) -> None:
        if not self.isVisible():
            self.show()
        self.raise_()
        self.web.setFocus()
        self.web.eval("window.chatUI && window.chatUI.focusComposer();")

    def showEvent(self, event: Any) -> None:  # noqa: N802 (Qt naming)
        super().showEvent(event)
        # resizeDocks only sticks on visible docks, so apply the configured
        # width on first show rather than at creation time (dock starts hidden).
        if not self._width_applied:
            self._width_applied = True
            mw.resizeDocks([self], [self._configured_width], Qt.Orientation.Horizontal)


def create_dock(dock_width: int) -> ChatDock:
    dock = ChatDock(dock_width)
    mw.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, dock)
    dock.hide()
    # The Tools-menu entry is built in __init__ (it needs the shortcut config
    # and the focus-toggle callback), not the bare checkable dock-title toggle.
    return dock
