"""GUI smoke probe for Chat With Your Cards.

Runs inside a disposable Anki profile next to the add-on under test.
Checks that the add-on loads, the dock and shortcuts register, the
webview boots, and a full scripted chat round-trip (JS send button ->
pycmd bridge -> ScriptedBackend -> streamed events -> DOM) works.
Captures light and dark screenshots via mw.grab() (no OS permissions
needed), writes JSON to $ANKI_ADDON_WORKBENCH_RESULT, then exits.
"""

from __future__ import annotations

import importlib
import json
import os
import time
import traceback
from pathlib import Path
from typing import Any, Callable

# Force the deterministic demo backend before the add-on builds one: the
# smoke must not depend on (or spend money through) a real claude CLI.
os.environ["CWYC_BACKEND"] = "scripted"

from aqt import mw
from aqt.qt import QDockWidget, QKeySequence, QShortcut, QTimer

try:
    from aqt.qt import QTest
except ImportError:  # pragma: no cover
    from PyQt6.QtTest import QTest

ADDON_PACKAGE = "chat_with_your_cards"
DOCK_OBJECT_NAME = "chat_with_your_cards_dock"
MENU_LABEL = "Chat With Your Cards"

WEB_READY_TIMEOUT_MS = 20_000
STREAM_TIMEOUT_MS = 15_000
DOM_TIMEOUT_MS = 5_000

DEMO_MESSAGE = "please run a tool demo"
PROPOSE_MESSAGE = "propose a note about this"


def _wait_until(predicate: Callable[[], bool], timeout_ms: int, description: str) -> None:
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        if predicate():
            return
        QTest.qWait(50)
    raise AssertionError(f"timed out waiting for {description}")


def _eval_js(web: Any, script: str, timeout_ms: int, description: str) -> Any:
    holder: dict[str, Any] = {}
    web.evalWithCallback(script, lambda value: holder.__setitem__("value", value))
    _wait_until(lambda: "value" in holder, timeout_ms, description)
    return holder["value"]


def _shortcut_keys() -> list[str]:
    assert mw is not None
    return [
        shortcut.key().toString(QKeySequence.SequenceFormat.PortableText)
        for shortcut in mw.findChildren(QShortcut)
    ]


def _run_checks() -> dict[str, Any]:
    assert mw is not None
    checks: list[dict[str, Any]] = []

    def check(name: str, fn: Callable[[], Any]) -> Any:
        value = fn()
        checks.append({"name": name, "ok": True})
        return value

    addon = check(
        "module imported",
        lambda: importlib.import_module(ADDON_PACKAGE),
    )
    state = addon.state

    dock = mw.findChild(QDockWidget, DOCK_OBJECT_NAME)

    def _dock_exists() -> Any:
        if dock is None or state.dock is not dock:
            raise AssertionError("chat dock not found on the main window")
        if dock.isVisible():
            raise AssertionError("dock should start hidden")
        return dock

    check("dock exists and starts hidden", _dock_exists)

    def _tools_action() -> None:
        texts = [a.text().replace("&", "") for a in mw.form.menuTools.actions()]
        if MENU_LABEL not in texts:
            raise AssertionError(f"Tools menu is missing {MENU_LABEL!r}: {texts}")

    check("Tools menu action present", _tools_action)

    def _shortcuts_registered() -> None:
        keys = _shortcut_keys()
        for expected in (state.config["toggle_shortcut"], state.config["new_chat_shortcut"]):
            normalized = QKeySequence(expected).toString(
                QKeySequence.SequenceFormat.PortableText
            )
            if normalized not in keys:
                raise AssertionError(f"shortcut {expected!r} not registered (found {keys})")
        if len(state.shortcuts) != 2:
            raise AssertionError(f"expected 2 tracked shortcuts, got {len(state.shortcuts)}")

    check("shortcuts registered", _shortcuts_registered)

    def _web_ready() -> None:
        try:
            _wait_until(lambda: state.web_ready, WEB_READY_TIMEOUT_MS, "webview ready ping")
        except AssertionError:
            diagnostics = _eval_js(
                dock.web,
                "(function() { return {"
                "  pycmd: typeof pycmd,"
                "  chatUI: typeof window.chatUI,"
                "  marked: typeof window.marked,"
                "  stylesheets: document.styleSheets.length,"
                "  root: document.getElementById('cwyc-root') ? true : false,"
                "  title: document.title"
                "}; })();",
                DOM_TIMEOUT_MS,
                "ready-timeout diagnostics",
            )
            raise AssertionError(f"webview ready ping never arrived; page state: {diagnostics}")

    check("webview ready ping received", _web_ready)

    def _toggle_shows_dock() -> None:
        addon.toggle_chat_focus()
        _wait_until(dock.isVisible, 3_000, "dock to become visible after toggle")

    check("toggle shows dock", _toggle_shows_dock)

    def _scripted_chat() -> dict[str, Any]:
        controller = state.controller
        script = (
            "(function() {"
            f"  var input = document.getElementById('cwyc-input');"
            f"  input.value = {json.dumps(DEMO_MESSAGE)};"
            "  document.getElementById('cwyc-send').click();"
            "  return true;"
            "})();"
        )
        result = _eval_js(dock.web, script, DOM_TIMEOUT_MS, "demo send click")
        if result is not True:
            raise AssertionError(f"send click eval returned {result!r}")

        def _stream_finished() -> bool:
            return any(type(e).__name__ == "Done" for e in controller.event_log)

        _wait_until(_stream_finished, STREAM_TIMEOUT_MS, "scripted stream to finish")

        names = [type(e).__name__ for e in controller.event_log]
        deltas = names.count("TextDelta")
        if deltas < 2:
            raise AssertionError(f"expected several TextDelta events, got {deltas}")
        started = [e for e in controller.event_log if type(e).__name__ == "ToolCallStarted"]
        finished = [e for e in controller.event_log if type(e).__name__ == "ToolCallFinished"]
        if len(started) != 1 or len(finished) != 1 or started[0].call_id != finished[0].call_id:
            raise AssertionError(f"expected one matched tool call pair, got {names}")
        return {"events": len(names), "text_deltas": deltas}

    stream_info = check("scripted chat streams end-to-end", _scripted_chat)

    def _dom_rendered() -> dict[str, Any]:
        QTest.qWait(300)  # allow the final render to settle
        dom = _eval_js(
            dock.web,
            "(function() { return {"
            "  user: document.querySelectorAll('.msg-user').length,"
            "  assistant: document.querySelectorAll('.msg-assistant').length,"
            "  chips: document.querySelectorAll('.tool-chip').length,"
            "  chips_ok: document.querySelectorAll('.cwyc-tool-ok').length,"
            "  streaming: document.querySelectorAll('.cwyc-streaming').length,"
            "  input_style_height: document.getElementById('cwyc-input').style.height,"
            "  input_rect: document.getElementById('cwyc-input')"
            "    .getBoundingClientRect().height,"
            "  input_scroll: document.getElementById('cwyc-input').scrollHeight,"
            "  input_box_sizing: getComputedStyle("
            "    document.getElementById('cwyc-input')).boxSizing,"
            "  composer_rect: document.getElementById('cwyc-composer')"
            "    .getBoundingClientRect().height"
            "}; })();",
            DOM_TIMEOUT_MS,
            "DOM state query",
        )
        if dom.get("composer_rect", 0) > 120:
            raise AssertionError(f"composer blew up: {dom}")
        if dom["user"] < 1 or dom["assistant"] < 1 or dom["chips"] < 1:
            raise AssertionError(f"transcript DOM incomplete: {dom}")
        if dom["chips_ok"] != dom["chips"]:
            raise AssertionError(f"tool chip did not finish: {dom}")
        if dom["streaming"] != 0:
            raise AssertionError(f"assistant message still marked streaming: {dom}")
        return dom

    dom_info = check("transcript DOM rendered", _dom_rendered)

    def _focus_toggle_returns() -> None:
        dock.web.setFocus()
        QTest.qWait(100)
        addon.toggle_chat_focus()
        QTest.qWait(200)
        focused = mw.app.focusWidget()
        if focused is not None and dock.isAncestorOf(focused):
            raise AssertionError("focus stayed inside the dock after second toggle")

    check("second toggle returns focus", _focus_toggle_returns)

    def _proposal_round_trip() -> dict[str, Any]:
        """Scripted propose -> proposal card -> accept -> real note + ledger."""
        script = (
            "(function() {"
            "  var input = document.getElementById('cwyc-input');"
            f"  input.value = {json.dumps(PROPOSE_MESSAGE)};"
            "  document.getElementById('cwyc-send').click();"
            "  return true;"
            "})();"
        )
        if _eval_js(dock.web, script, DOM_TIMEOUT_MS, "propose send click") is not True:
            raise AssertionError("propose send click failed")

        def _card_rendered() -> bool:
            return bool(
                _eval_js(
                    dock.web,
                    "document.querySelectorAll('.cwyc-proposal').length",
                    DOM_TIMEOUT_MS,
                    "proposal card count",
                )
            )

        _wait_until(_card_rendered, STREAM_TIMEOUT_MS, "proposal card to render")
        card = _eval_js(
            dock.web,
            "(function() {"
            "  var p = document.querySelector('.cwyc-proposal');"
            "  return {"
            "    kind: p.querySelector('.cwyc-proposal-kind').textContent,"
            "    status: p.querySelector('.cwyc-proposal-status').textContent,"
            "    fields: p.querySelectorAll('.cwyc-field').length,"
            "    accept: p.querySelectorAll('.cwyc-btn-accept').length,"
            "    preview_tabs: p.querySelectorAll('.cwyc-preview-tab').length"
            "  };"
            "})();",
            DOM_TIMEOUT_MS,
            "proposal card state",
        )
        if card["kind"] != "New note" or card["fields"] < 2 or card["accept"] != 1:
            raise AssertionError(f"proposal card malformed: {card}")
        if card["preview_tabs"] != 2:
            raise AssertionError(f"expected Front/Back preview tabs: {card}")

        before_ids = set(mw.col.find_notes('tag:"ai-created"'))
        _eval_js(
            dock.web,
            "(function() { document.querySelector('.cwyc-btn-accept').click(); "
            "return true; })();",
            DOM_TIMEOUT_MS,
            "proposal accept click",
        )

        def _note_created() -> bool:
            return len(set(mw.col.find_notes('tag:"ai-created"')) - before_ids) == 1

        _wait_until(_note_created, DOM_TIMEOUT_MS, "accepted note to appear")
        (note_id,) = set(mw.col.find_notes('tag:"ai-created"')) - before_ids
        note = mw.col.get_note(note_id)
        session_tag = state.proposals.session_tag
        if session_tag not in note.tags:
            raise AssertionError(f"session tag missing: {note.tags}")

        QTest.qWait(300)
        resolved = _eval_js(
            dock.web,
            "(function() {"
            "  var p = document.querySelector('.cwyc-proposal');"
            "  var ledger = document.getElementById('cwyc-ledger');"
            "  return {"
            "    status: p.querySelector('.cwyc-proposal-status').textContent,"
            "    revert: p.querySelectorAll('.cwyc-btn-revert').length,"
            "    ledger_visible: !ledger.hidden,"
            "    ledger_text: document.getElementById('cwyc-ledger-label').textContent"
            "  };"
            "})();",
            DOM_TIMEOUT_MS,
            "resolved proposal state",
        )
        if resolved["status"] != "Accepted" or not resolved["ledger_visible"]:
            raise AssertionError(f"proposal did not resolve in the UI: {resolved}")
        return {"note_id": note_id, "card": card, "resolved": resolved}

    proposal_info = check("proposal accept round-trip", _proposal_round_trip)

    return {
        "ok": True,
        "checks": checks,
        "stream": stream_info,
        "dom": dom_info,
        "proposal": proposal_info,
        "anki_version": getattr(mw, "appVersion", None),
    }


def _save_screenshots(result: dict[str, Any]) -> None:
    path = os.environ.get("ANKI_ADDON_WORKBENCH_SCREENSHOT")
    if not path or mw is None:
        return
    light_path = Path(path)
    light_path.parent.mkdir(parents=True, exist_ok=True)
    QTest.qWait(200)
    if not mw.grab().save(str(light_path), "PNG"):
        raise RuntimeError(f"failed to save screenshot to {light_path}")
    result["screenshot"] = str(light_path)

    try:
        from aqt.theme import Theme, theme_manager

        mw.pm.set_theme(Theme.DARK)
        theme_manager.apply_style()
        QTest.qWait(700)
        addon = importlib.import_module(ADDON_PACKAGE)
        result["dark_page_state"] = _eval_js(
            addon.state.dock.web,
            "(function() { return {"
            "  htmlClass: document.documentElement.className,"
            "  canvas: getComputedStyle(document.documentElement)"
            "    .getPropertyValue('--canvas').trim()"
            "}; })();",
            DOM_TIMEOUT_MS,
            "dark page state",
        )
        # Force a full recomposite: QtWebEngine leaves stale (light) tiles
        # after a pure CSS-class flip, and mw.grab() would photograph them.
        _eval_js(
            addon.state.dock.web,
            "(function() {"
            "  document.body.style.display = 'none';"
            "  void document.body.offsetHeight;"
            "  document.body.style.display = '';"
            "  return true;"
            "})();",
            DOM_TIMEOUT_MS,
            "dark repaint force",
        )
        QTest.qWait(500)
        dark_path = light_path.with_name(light_path.stem + "-dark.png")
        if not mw.grab().save(str(dark_path), "PNG"):
            raise RuntimeError(f"failed to save screenshot to {dark_path}")
        result["screenshot_dark"] = str(dark_path)
    except Exception as exc:  # dark theme is best-effort
        result["screenshot_dark_error"] = str(exc)


def _write_result(payload: dict[str, Any]) -> None:
    path = os.environ.get("ANKI_ADDON_WORKBENCH_RESULT")
    if not path:
        return
    Path(path).write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _finish() -> None:
    if mw is not None:
        mw.unloadProfileAndExit()


def _run_and_quit() -> None:
    try:
        result = _run_checks()
    except Exception as exc:
        result = {"ok": False, "error": str(exc), "traceback": traceback.format_exc()}

    try:
        _save_screenshots(result)
    except Exception as exc:
        result["screenshot_error"] = str(exc)

    _write_result(result)
    QTimer.singleShot(100, _finish)


def _schedule() -> None:
    QTimer.singleShot(800, _run_and_quit)


from aqt import gui_hooks  # noqa: E402

gui_hooks.main_window_did_init.append(_schedule)
