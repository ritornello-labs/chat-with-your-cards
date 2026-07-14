"""The chat dock: a QDockWidget hosting an AnkiWebView with the chat UI.

Shell redesign (dogfood 2026-07-13): the dock is ALWAYS visible - "closed"
means collapsed to a slim rail the webview renders (ember + vertical
wordmark), never hidden, so the add-on stays one click away. The native Qt
title bar is removed entirely (setTitleBarWidget(QWidget())): the webview's
own header row is the dock's chrome, so the panel reads as one warm surface
instead of an Anki-styled frame around a differently-styled web page.
Expanding/collapsing animates the dock width with a QVariantAnimation while
the webview crossfades between the full UI and the rail (dock_state pushes
at animation start/end - see ui/src/App.tsx's Shell).
"""

from __future__ import annotations

from typing import Any

from aqt import mw
from aqt.qt import QDockWidget, QEasingCurve, Qt, QVariantAnimation, QWidget
from aqt.webview import AnkiWebView

from .bridge import Bridge

DOCK_OBJECT_NAME = "chat_with_your_cards_dock"
DOCK_TITLE = "Chat With Your Cards"

# The collapsed rail: wide enough for a comfortable click target and the
# vertical wordmark, narrow enough to cost nothing. Must match the webview's
# rail layout (ui/src/styles.css "dock shell" section).
RAIL_WIDTH = 44

# Floor on the EXPANDED width. The width persists across sessions, so an
# accidental drag to a sliver otherwise reopens as an unusable ~70px column.
# 360 keeps the composer control row on one line for the common chip labels;
# longer ones wrap onto a second row rather than cramming (styles.css).
MIN_DOCK_WIDTH = 360

# Qt's QWIDGETSIZE_MAX (not re-exported by aqt.qt): "no maximum".
_WIDGET_SIZE_MAX = 16777215

# Snappy but readable; the webview crossfade (130-170ms) nests inside it.
ANIM_MS = 220


class ChatDock(QDockWidget):
    def __init__(self, dock_width: int, collapsed: bool) -> None:
        super().__init__(DOCK_TITLE, mw)
        self.expanded_width = max(int(dock_width), MIN_DOCK_WIDTH)
        self.expanded = not collapsed
        self._anim: QVariantAnimation | None = None
        self._width_applied = False
        self.setObjectName(DOCK_OBJECT_NAME)
        self.setAllowedAreas(
            Qt.DockWidgetArea.LeftDockWidgetArea | Qt.DockWidgetArea.RightDockWidgetArea
        )
        # No native chrome at all: no title bar (an empty widget replaces it),
        # no close/float buttons, no drag handle. Collapse lives in the
        # webview header; the dock side moves via Settings (set_setting).
        self.setTitleBarWidget(QWidget(self))
        self.setFeatures(QDockWidget.DockWidgetFeature.NoDockWidgetFeatures)
        if not self.expanded:
            self._pin_width(RAIL_WIDTH)
        else:
            self.setMinimumWidth(min(MIN_DOCK_WIDTH, self._avail_width()))
        self.web = AnkiWebView(parent=self, title="chat with your cards")
        # The ready ping and early messages must not be dropped before the
        # collection opens (webview.py drops bridge cmds when requiresCol).
        self.web.requiresCol = False
        self.setWidget(self.web)
        self.bridge = Bridge(self.web)
        self._load_ui()

    @property
    def side(self) -> str:
        area = mw.dockWidgetArea(self)
        return "left" if area == Qt.DockWidgetArea.LeftDockWidgetArea else "right"

    def _avail_width(self) -> int:
        """Widest the dock may claim without over-constraining the window.

        A pinned/minimum width the QMainWindow cannot honor does not fail
        gracefully: Qt lays the dock out at the demanded width and CLIPS it
        at the window edge (real Anki, stock ~670px window, 2026-07-13: the
        composer's send button was cut in half). Leave the central widget a
        usable column and never demand more than that."""
        return max(mw.width() - 330, RAIL_WIDTH + 36)

    def expand_target(self) -> int:
        """The width an expand actually aims for: the saved width, clamped
        to what the window can give, never below the (equally clamped)
        MIN_DOCK_WIDTH floor."""
        avail = self._avail_width()
        return max(min(self.expanded_width, avail), min(MIN_DOCK_WIDTH, avail))

    def _load_ui(self) -> None:
        # The assistant-ui frontend is the only UI (DESIGN.md section 9,
        # 2026-07-11). Anki loads the committed bundle from web/next/ via the
        # standard stdHtml path: a <div id="cwyc-root"></div> body fragment
        # plus the bundle registered as web exports. The bundle defers mounting
        # until DOMContentLoaded (stdHtml injects js= into <head>, before this
        # body fragment exists) and creates its own #cwyc-root if absent. The
        # inline script plants the initial dock state so the first paint
        # already shows the right layer (rail vs full chat) - the bundle in
        # <head> runs before this body fragment, so it reads the global at
        # mount time, not at load time (App.tsx Shell). The standalone
        # web/next/index.html the build emits is a dev-only artifact and is
        # never loaded here.
        import json as _json

        addon_pkg = mw.addonManager.addonFromModule(__name__)
        base = f"/_addons/{addon_pkg}/web"
        initial = _json.dumps(
            {"expanded": self.expanded, "width": self.expand_target(), "side": self.side}
        )
        self.web.stdHtml(
            body=(
                f"<script>window.CWYC_INITIAL_DOCK = {initial};</script>"
                '<div id="cwyc-root"></div>'
            ),
            css=[f"{base}/next/bundle.css"],
            js=[f"{base}/next/bundle.js"],
            context=self,
        )

    # ---- expand/collapse ----------------------------------------------------

    def push_state(self, animating: bool) -> None:
        """Tell the webview which layer to show (and when to width-pin).
        `width` is the width the full layer should pin to: the clamped
        expand target, so the pinned layer matches the dock's real final
        width instead of an off-window ideal."""
        self.bridge.push(
            {
                "type": "dock_state",
                "expanded": self.expanded,
                "animating": animating,
                "width": self.expand_target(),
                "side": self.side,
            }
        )

    def set_expanded(self, expanded: bool, animate: bool = True) -> None:
        if expanded == self.expanded and self._anim is None:
            return
        if self._anim is not None:
            self._anim.stop()
            self._anim = None
        self.expanded = expanded
        target = self.expand_target() if expanded else RAIL_WIDTH
        self.push_state(animating=animate and self.isVisible())
        if not animate or not self.isVisible():
            self._pin_width(target)
            self._finish_resize()
            return
        anim = QVariantAnimation(self)
        anim.setStartValue(self.width())
        anim.setEndValue(target)
        anim.setDuration(ANIM_MS)
        anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        anim.valueChanged.connect(lambda value: self._pin_width(int(value)))
        anim.finished.connect(self._finish_resize)
        self._anim = anim
        anim.start()

    def _pin_width(self, width: int) -> None:
        # Pinning min=max is the one reliable way to drive a QDockWidget's
        # width frame-by-frame (resizeDocks only takes suggestions).
        self.setMinimumWidth(width)
        self.setMaximumWidth(width)

    def _finish_resize(self) -> None:
        self._anim = None
        if self.expanded:
            # Unpin so the user can drag-resize again; keep the floor (also
            # clamped to the window - see _avail_width). The collapsed rail
            # stays pinned at RAIL_WIDTH (not resizable).
            self.setMinimumWidth(min(MIN_DOCK_WIDTH, self._avail_width()))
            self.setMaximumWidth(_WIDGET_SIZE_MAX)
            mw.resizeDocks([self], [self.expand_target()], Qt.Orientation.Horizontal)
        self.push_state(animating=False)

    def resizeEvent(self, event: Any) -> None:  # noqa: N802 (Qt naming)
        super().resizeEvent(event)
        # Track the user's drag-resizes (only meaningful while expanded and
        # not mid-animation) so collapse->expand returns to the same width and
        # teardown persists it. The MIN_DOCK_WIDTH guard also keeps
        # small-window layout squeezes (which clamp below the nominal floor -
        # see _avail_width) from silently overwriting the remembered width.
        if self.expanded and self._anim is None and self.width() >= MIN_DOCK_WIDTH:
            self.expanded_width = self.width()

    def focus_composer(self) -> None:
        if not self.expanded:
            self.set_expanded(True)
        self.raise_()
        self.web.setFocus()
        self.web.eval("window.chatUI && window.chatUI.focusComposer();")

    def showEvent(self, event: Any) -> None:  # noqa: N802 (Qt naming)
        super().showEvent(event)
        # resizeDocks only sticks on visible docks, so apply the configured
        # width on first show rather than at creation time.
        if not self._width_applied:
            self._width_applied = True
            if self.expanded:
                mw.resizeDocks([self], [self.expand_target()], Qt.Orientation.Horizontal)


def create_dock(dock_width: int, collapsed: bool, side: str) -> ChatDock:
    dock = ChatDock(dock_width, collapsed)
    area = (
        Qt.DockWidgetArea.LeftDockWidgetArea
        if side == "left"
        else Qt.DockWidgetArea.RightDockWidgetArea
    )
    mw.addDockWidget(area, dock)
    # Always visible: collapsed = the slim rail, never hidden.
    return dock


def move_dock(dock: ChatDock, side: str) -> None:
    """Settings' dock-side switch: re-add on the other edge, keep the width."""
    area = (
        Qt.DockWidgetArea.LeftDockWidgetArea
        if side == "left"
        else Qt.DockWidgetArea.RightDockWidgetArea
    )
    mw.addDockWidget(area, dock)
    if dock.expanded:
        mw.resizeDocks([dock], [dock.expand_target()], Qt.Orientation.Horizontal)
    # The rail chevrons point by side; let the webview redraw them.
    dock.push_state(animating=False)
