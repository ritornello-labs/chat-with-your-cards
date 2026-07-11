"""The chat dock: a QDockWidget hosting an AnkiWebView with the chat UI."""

from __future__ import annotations

from pathlib import Path
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
# wrap gracefully (flex-wrap in styles.css) rather than overflow.
MIN_DOCK_WIDTH = 320

_WEB_DIR = Path(__file__).resolve().parent / "web"


class ChatDock(QDockWidget):
    def __init__(self, dock_width: int, ui_mode: str = "classic") -> None:
        super().__init__(DOCK_TITLE, mw)
        # "classic" = the vanilla-JS web/ UI; "next" = the assistant-ui bundle
        # in web/next/ (DESIGN.md section 9, 2026-07-10). Unknown values fall
        # back to classic so a bad config never leaves the dock blank.
        self._ui_mode = ui_mode if ui_mode in ("classic", "next") else "classic"
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
        addon_pkg = mw.addonManager.addonFromModule(__name__)
        base = f"/_addons/{addon_pkg}/web"
        if self._ui_mode == "next":
            # assistant-ui frontend: same stdHtml path as classic, loading the
            # committed bundle from web/next/. The bundle defers mounting until
            # DOMContentLoaded (stdHtml injects js= into <head>, before this
            # body fragment exists) and creates its own #cwyc-root if absent.
            # The standalone web/next/index.html is a dev-only artifact and is
            # never loaded here (DESIGN.md section 9, 2026-07-10).
            self.web.stdHtml(
                body='<div id="cwyc-root"></div>',
                css=[f"{base}/next/bundle.css"],
                js=[f"{base}/next/bundle.js"],
                context=self,
            )
            return
        body = (_WEB_DIR / "index.html").read_text(encoding="utf-8")
        self.web.stdHtml(
            body=body,
            css=[f"{base}/styles.css"],
            js=[f"{base}/vendor/marked.min.js", f"{base}/app.js"],
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


def create_dock(dock_width: int, ui_mode: str = "classic") -> ChatDock:
    dock = ChatDock(dock_width, ui_mode)
    mw.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, dock)
    dock.hide()
    # The Tools-menu entry is built in __init__ (it needs the shortcut config
    # and the focus-toggle callback), not the bare checkable dock-title toggle.
    return dock
