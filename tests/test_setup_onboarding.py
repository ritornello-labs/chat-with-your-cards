"""First-run onboarding for no-CLI installs (workspace task #19).

Covers the two halves of the flow at the controller boundary:
  - `_build_backend()` pushes a structured `setup_needed` event (not the old
    plain notice) the first time Claude Code can't be found, exactly once.
  - `recheck_backend()` (the setup card's "Re-check" button) re-runs
    discovery with no Anki restart: on success it rebuilds the backend/
    session and pushes `setup_resolved`; on failure it pushes a notice
    instead and leaves the demo backend running.

ChatController imports aqt at module load, so this stubs a minimal aqt /
aqt.qt into sys.modules before importing it, same as test_agent_tools_state.py.
"""

from __future__ import annotations

import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

if "aqt" not in sys.modules:
    aqt_stub = types.ModuleType("aqt")
    aqt_stub.mw = None  # type: ignore[attr-defined]
    sys.modules["aqt"] = aqt_stub
    aqt_qt_stub = types.ModuleType("aqt.qt")
    aqt_qt_stub.QTimer = object  # type: ignore[attr-defined]
    sys.modules["aqt.qt"] = aqt_qt_stub

from chat_with_your_cards import controller as controller_mod  # noqa: E402
from chat_with_your_cards.controller import ChatController, _setup_platform  # noqa: E402


class FakeSession:
    """Stand-in for ClaudeCliSession: no subprocess, just a close() to assert
    against so recheck_backend's teardown-and-rebuild is actually exercised."""

    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class FakeClaudeCliBackend:
    """Stand-in for ClaudeCliBackend: constructed with the same kwargs (so a
    signature drift would fail loudly) but never touches a real process."""

    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs

    def start_session(self, context):
        return FakeSession()


def _make_controller(**config_overrides) -> tuple[ChatController, list[dict]]:
    config = {
        "backend": "auto",
        "model": "",
        "effort": "",
        "fast_mode": False,
        "agent_tools": "sandbox",
        "permission_mode": "default",
        "claude_cli_path": "",
    }
    config.update(config_overrides)
    pushed: list[dict] = []
    controller = ChatController(
        push=pushed.append,
        config=config,
        system_prompt_builder=lambda: "",
        ensure_mcp=lambda: ("http://127.0.0.1:0/mcp", "tok"),
        workdir=Path(tempfile.mkdtemp(prefix="cwyc-setup-")),
    )
    return controller, pushed


def _events(pushed: list[dict], type_: str) -> list[dict]:
    return [p for p in pushed if p.get("type") == type_]


class SetupPlatformTests(unittest.TestCase):
    def test_darwin_maps_to_darwin(self) -> None:
        with mock.patch.object(controller_mod.sys, "platform", "darwin"):
            self.assertEqual("darwin", _setup_platform())

    def test_windows_variants_map_to_windows(self) -> None:
        for raw in ("win32", "cygwin"):
            with mock.patch.object(controller_mod.sys, "platform", raw):
                self.assertEqual("windows", _setup_platform())

    def test_linux_and_other_unix_map_to_linux(self) -> None:
        for raw in ("linux", "freebsd13"):
            with mock.patch.object(controller_mod.sys, "platform", raw):
                self.assertEqual("linux", _setup_platform())


class BuildBackendSetupNeededTests(unittest.TestCase):
    def test_missing_cli_pushes_setup_needed_not_a_notice(self) -> None:
        controller, pushed = _make_controller()
        with mock.patch.object(controller_mod, "find_claude_cli", return_value=None), \
             mock.patch.object(controller_mod.sys, "platform", "darwin"):
            controller.ensure_ready()
        setup_events = _events(pushed, "setup_needed")
        self.assertEqual(1, len(setup_events))
        self.assertEqual("darwin", setup_events[0]["platform"])
        self.assertEqual("scripted", controller.backend_kind)
        # Superseded the old plain-notice fallback: no redundant "notice" push
        # advertising the same missing-CLI fact.
        self.assertEqual([], _events(pushed, "notice"))

    def test_setup_needed_fires_only_once_per_controller_lifetime(self) -> None:
        controller, pushed = _make_controller()
        with mock.patch.object(controller_mod, "find_claude_cli", return_value=None):
            controller.ensure_ready()
            # A fresh chat rebuilds the backend (new_chat() clears _backend),
            # but the CLI is still missing - the notice-once guard must still
            # hold across that rebuild.
            controller.new_chat()
            controller.ensure_ready()
        self.assertEqual(1, len(_events(pushed, "setup_needed")))

    def test_cli_found_uses_claude_backend_and_no_setup_needed(self) -> None:
        controller, pushed = _make_controller()
        with mock.patch.object(controller_mod, "find_claude_cli", return_value="/usr/bin/claude"), \
             mock.patch.object(controller_mod, "ClaudeCliBackend", FakeClaudeCliBackend):
            controller.ensure_ready()
        self.assertEqual("claude", controller.backend_kind)
        self.assertEqual([], _events(pushed, "setup_needed"))


class RecheckBackendTests(unittest.TestCase):
    def test_recheck_still_missing_pushes_notice_not_resolved(self) -> None:
        controller, pushed = _make_controller()
        with mock.patch.object(controller_mod, "find_claude_cli", return_value=None):
            controller.ensure_ready()  # demo backend, setup_needed fired once
            pushed.clear()
            result = controller.recheck_backend()
        self.assertFalse(result)
        self.assertEqual([], _events(pushed, "setup_resolved"))
        notices = _events(pushed, "notice")
        self.assertEqual(1, len(notices))
        self.assertIn("can't find Claude Code", notices[0]["text"])

    def test_recheck_found_rebuilds_backend_and_resolves_no_restart(self) -> None:
        controller, pushed = _make_controller()
        # Start out on the demo backend with a live (fake) session, as the
        # real dock would have via ensure_ready() on chat focus.
        with mock.patch.object(controller_mod, "find_claude_cli", return_value=None):
            controller.ensure_ready()
        demo_session = controller._session
        self.assertIsNotNone(demo_session)
        pushed.clear()

        with mock.patch.object(controller_mod, "find_claude_cli", return_value="/usr/bin/claude"), \
             mock.patch.object(controller_mod, "ClaudeCliBackend", FakeClaudeCliBackend):
            result = controller.recheck_backend()

        self.assertTrue(result)
        self.assertEqual("claude", controller.backend_kind)
        # The old demo session was torn down and replaced, not left dangling
        # alongside the new one.
        self.assertIsNot(demo_session, controller._session)
        self.assertIsInstance(controller._session, FakeSession)
        self.assertEqual(1, len(_events(pushed, "setup_resolved")))
        found_notices = [p for p in _events(pushed, "notice") if "found" in p["text"].lower()]
        self.assertEqual(1, len(found_notices))

    def test_recheck_success_resets_notice_flag_so_a_later_loss_resurfaces(self) -> None:
        controller, pushed = _make_controller()
        with mock.patch.object(controller_mod, "find_claude_cli", return_value=None):
            controller.ensure_ready()
        with mock.patch.object(controller_mod, "find_claude_cli", return_value="/usr/bin/claude"), \
             mock.patch.object(controller_mod, "ClaudeCliBackend", FakeClaudeCliBackend):
            self.assertTrue(controller.recheck_backend())
        # Simulate the CLI disappearing again on a later chat: setup_needed
        # must be able to fire again now that recheck_backend reset the flag.
        controller.new_chat()
        pushed.clear()
        with mock.patch.object(controller_mod, "find_claude_cli", return_value=None):
            controller.ensure_ready()
        self.assertEqual(1, len(_events(pushed, "setup_needed")))


if __name__ == "__main__":
    unittest.main()
