"""Outbound message assembly for commands and dock review decisions."""

from __future__ import annotations

import sys
import tempfile
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

if "aqt" not in sys.modules:
    aqt_stub = types.ModuleType("aqt")
    aqt_stub.mw = None  # type: ignore[attr-defined]
    sys.modules["aqt"] = aqt_stub
    aqt_qt_stub = types.ModuleType("aqt.qt")
    aqt_qt_stub.QTimer = object  # type: ignore[attr-defined]
    sys.modules["aqt.qt"] = aqt_qt_stub

from chat_with_your_cards.controller import ChatController  # noqa: E402


class FakeSession:
    def __init__(self) -> None:
        self.streaming = False
        self.sent: list[tuple[str, list[dict] | None]] = []
        self.closed = False

    def send(self, text, _on_event, extra_blocks=None) -> None:
        self.sent.append((text, extra_blocks))

    def close(self) -> None:
        self.closed = True


def make_controller() -> tuple[ChatController, FakeSession, list[dict]]:
    pushed: list[dict] = []
    controller = ChatController(
        push=pushed.append,
        config={},
        system_prompt_builder=lambda: "",
        ensure_mcp=lambda: ("http://127.0.0.1:0/mcp", "tok"),
        workdir=Path(tempfile.mkdtemp(prefix="cwyc-context-")),
    )
    session = FakeSession()
    controller._session = session
    controller._context_for_send = lambda: (  # type: ignore[method-assign]
        "<current-card>Card context</current-card>",
        "card in Test",
    )
    return controller, session, pushed


class ControllerMessageContextTests(unittest.TestCase):
    def test_compact_is_sent_raw_without_card_or_pending_decisions(self) -> None:
        controller, session, pushed = make_controller()
        controller.note_proposal_decision(
            {"proposal_id": "p1", "decision": "accepted"}
        )

        controller.send_user_message("/compact")

        self.assertEqual("/compact", session.sent[0][0])
        self.assertEqual("session command", pushed[-1]["label"])
        self.assertEqual(1, len(controller._pending_proposal_decisions))

    def test_skill_invocation_stays_first_and_receives_card_context(self) -> None:
        controller, session, _pushed = make_controller()

        controller.send_user_message("/anki-card-authoring improve this")

        outbound = session.sent[0][0]
        self.assertTrue(outbound.startswith("/anki-card-authoring improve this"))
        self.assertIn("<current-card>Card context</current-card>", outbound)

    def test_manual_decisions_are_injected_once_on_next_real_turn(self) -> None:
        controller, session, _pushed = make_controller()
        controller.note_proposal_decision(
            {
                "proposal_id": "p1",
                "decision": "accepted",
                "proposal_kind": "edit",
                "field_values": {"Front": "kept"},
                "skipped_fields": ["Back"],
            }
        )
        controller.note_proposal_decision(
            {"proposal_id": "p2", "decision": "rejected"}
        )

        controller.send_user_message("Continue")
        first = session.sent[0][0]
        self.assertIn("<proposal-decisions>", first)
        self.assertIn('"proposal_id":"p1"', first)
        self.assertIn('"decision":"rejected"', first)

        controller.send_user_message("And again")
        self.assertNotIn("<proposal-decisions>", session.sent[1][0])

    def test_new_chat_discards_unreported_decisions(self) -> None:
        controller, session, _pushed = make_controller()
        controller.note_proposal_decision(
            {"proposal_id": "p1", "decision": "rejected"}
        )

        controller.new_chat()

        self.assertTrue(session.closed)
        self.assertEqual([], controller._pending_proposal_decisions)


if __name__ == "__main__":
    unittest.main()
