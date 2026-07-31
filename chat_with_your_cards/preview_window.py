"""Large resizable preview window for proposal cards (#2, route B).

The in-card preview is a small square; complex cards need room. This is
our own QDialog + AnkiWebView over the question/answer/CSS that
proposals.py already computes through the REAL templates
(Note.ephemeral_card, the same call Anki's card-layout screen uses) -
deliberately NOT a subclass of aqt.browser.previewer.Previewer, whose
render path reloads the note from the DB and so breaks for create
proposals (decision 2026-07-23, recorded in the task). Divergence from
Anki's own window is limited to chrome we add ourselves.

build_preview_html is pure (unit-tested); everything Qt is inside
show_preview, which must be called on the main thread.
"""

from __future__ import annotations

from typing import Any

# Face order: edits show Before/After answer sides (the interesting
# comparison); creations show Front/Back like a card on the desk.
_EDIT_FACES = ("Before", "After")
_CREATE_FACES = ("Front", "Back")

_open_dialog: Any = None  # singleton: reopening replaces content, not windows


def build_preview_html(
    content: str | None, css: str | None, night: bool
) -> str:
    """The same srcdoc shape the dock's in-card preview iframes use, so the
    big window renders identically, just bigger. Night-mode classes mirror
    Anki's own so night-aware templates behave."""
    body_class = "card" + (" nightMode night_mode" if night else "")
    background = "#2c2c2c" if night else "#ffffff"
    return (
        "<!doctype html><html><head><meta charset='utf-8'><style>"
        + (css or "")
        + "\nhtml{overflow:auto;background:"
        + background
        + ";}body{margin:16px;}"
        + "</style></head><body class=\""
        + body_class
        + "\">"
        + (content or "<i>(nothing rendered)</i>")
        + "</body></html>"
    )


def preview_faces(previews: dict[str, Any]) -> list[tuple[str, str, str]]:
    """Flatten a previews payload into (label, content, css) faces."""
    before = previews.get("before") or None
    after = previews.get("after") or None
    faces: list[tuple[str, str, str]] = []
    if before and after:
        faces.append(
            (_EDIT_FACES[0], before.get("answer") or "", before.get("css") or "")
        )
        faces.append(
            (_EDIT_FACES[1], after.get("answer") or "", after.get("css") or "")
        )
    elif after:
        faces.append(
            (_CREATE_FACES[0], after.get("question") or "", after.get("css") or "")
        )
        faces.append(
            (_CREATE_FACES[1], after.get("answer") or "", after.get("css") or "")
        )
    return faces


def show_preview(title: str, previews: dict[str, Any]) -> Any:
    """Open (or refresh) the resizable preview dialog. Main thread only.

    Returns the dialog so the gui-smoke probe can assert on and close it.
    """
    global _open_dialog
    from aqt import mw
    from aqt.qt import (
        QDialog,
        QHBoxLayout,
        QPushButton,
        QVBoxLayout,
        Qt,
    )
    from aqt.theme import theme_manager
    from aqt.utils import restoreGeom, saveGeom
    from aqt.webview import AnkiWebView

    faces = preview_faces(previews)
    if not faces:
        return None
    night = bool(getattr(theme_manager, "night_mode", False))

    if _open_dialog is not None:
        try:
            _open_dialog.close()
        except Exception:
            pass
        _open_dialog = None

    dialog = QDialog(mw)
    dialog.setWindowTitle(title)
    dialog.setWindowFlag(Qt.WindowType.Window, True)  # resizable, own entry
    layout = QVBoxLayout(dialog)
    layout.setContentsMargins(8, 8, 8, 8)

    web = AnkiWebView(parent=dialog)
    layout.addWidget(web, stretch=1)

    buttons = QHBoxLayout()
    face_buttons: list[Any] = []

    def show_face(index: int) -> None:
        label, content, css = faces[index]
        web.setHtml(build_preview_html(content, css, night))
        for i, button in enumerate(face_buttons):
            button.setChecked(i == index)

    for i, (label, _content, _css) in enumerate(faces):
        button = QPushButton(label)
        button.setCheckable(True)
        button.clicked.connect(lambda _checked=False, index=i: show_face(index))
        buttons.addWidget(button)
        face_buttons.append(button)
    buttons.addStretch(1)
    close_button = QPushButton("Close")
    close_button.clicked.connect(dialog.close)
    buttons.addWidget(close_button)
    layout.addLayout(buttons)

    geom_key = "cwycProposalPreview"
    restoreGeom(dialog, geom_key, default_size=(700, 620))

    def on_finished(_result: int) -> None:
        global _open_dialog
        saveGeom(dialog, geom_key)
        web.cleanup()
        _open_dialog = None

    dialog.finished.connect(on_finished)
    # Edits open on the interesting side (After); creations front-up.
    show_face(1 if faces[0][0] == _EDIT_FACES[0] else 0)
    dialog.show()
    _open_dialog = dialog
    return dialog
