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

_WEB_DIR = Path(__file__).resolve().parent / "web"


class ChatDock(QDockWidget):
    def __init__(self, dock_width: int) -> None:
        super().__init__(DOCK_TITLE, mw)
        self._configured_width = dock_width
        self._width_applied = False
        self.setObjectName(DOCK_OBJECT_NAME)
        self.setAllowedAreas(
            Qt.DockWidgetArea.LeftDockWidgetArea | Qt.DockWidgetArea.RightDockWidgetArea
        )
        self.setFeatures(
            QDockWidget.DockWidgetFeature.DockWidgetClosable
            | QDockWidget.DockWidgetFeature.DockWidgetMovable
            | QDockWidget.DockWidgetFeature.DockWidgetFloatable
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


def create_dock(dock_width: int) -> ChatDock:
    dock = ChatDock(dock_width)
    mw.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, dock)
    dock.hide()

    action = dock.toggleViewAction()
    action.setText(DOCK_TITLE)
    mw.form.menuTools.addAction(action)
    return dock
