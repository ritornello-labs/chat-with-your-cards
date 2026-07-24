from __future__ import annotations

import unittest
from typing import Any

from chat_with_your_cards.grading import GradingManager
from test_grading import FakeCard, FakeCol


class GradingWorkflowTests(unittest.TestCase):
    def manager(
        self,
        col: FakeCol,
        *,
        mode: str = "default",
        cap: int = 20,
    ) -> tuple[GradingManager, list[dict[str, Any]], list[list[int]]]:
        pushes: list[dict[str, Any]] = []
        refreshes: list[list[int]] = []
        manager = GradingManager(
            get_col=lambda: col,
            push=pushes.append,
            config={
                "permission_mode": mode,
                "auto_accept_cap": cap,
                "write_budget": cap,
            },
            after_change=refreshes.append,
        )
        return manager, pushes, refreshes

    def test_default_mode_preflights_then_waits_for_confirmation(self) -> None:
        col = FakeCol()
        col.add_card(FakeCard(101, 201, queue=-3), guid="stable-guid")
        manager, pushes, refreshes = self.manager(col)

        response = manager.submit_fail(
            {"card_ids": [101], "rationale": "The learner missed this fact."}
        )

        self.assertEqual(response["status"], "pending_user_confirmation")
        self.assertEqual(col.revlog, [])
        grading = pushes[-1]["grading"]
        self.assertEqual(grading["status"], "pending")
        self.assertEqual(grading["cards"][0]["hidden_state"], "manually buried")
        self.assertIn("will remain", grading["warnings"][0])

        manager.accept({"id": response["grading_id"]})

        grading = pushes[-1]["grading"]
        self.assertEqual(grading["status"], "accepted")
        self.assertEqual(col.revlog, [101])
        self.assertEqual(col.cards[101].queue, -3)
        self.assertEqual(grading["available_card_ids"], [101])
        self.assertEqual(refreshes, [[101]])

    def test_hidden_state_removal_is_a_separate_explicit_action(self) -> None:
        col = FakeCol()
        col.add_card(FakeCard(101, 201, queue=-1), guid="stable-guid")
        manager, pushes, refreshes = self.manager(col)
        response = manager.submit_fail({"card_ids": [101]})
        manager.accept({"id": response["grading_id"]})
        reviews = list(col.revlog)

        manager.make_available_from_failure({"id": response["grading_id"]})

        grading = pushes[-1]["grading"]
        self.assertEqual(col.cards[101].queue, 2)
        self.assertEqual(col.revlog, reviews)
        self.assertEqual(grading["available_card_ids"], [])
        self.assertIsNone(grading["cards"][0]["hidden_state"])
        self.assertIn("Review history is unchanged", grading["availability"]["note"])
        self.assertEqual(refreshes, [[101], [101]])

    def test_auto_accept_applies_under_cap_then_falls_back_to_chip(self) -> None:
        col = FakeCol()
        col.add_card(FakeCard(101, 201), guid="guid-a")
        col.add_card(FakeCard(102, 202), guid="guid-b")
        manager, pushes, _refreshes = self.manager(col, mode="auto-accept", cap=1)

        first = manager.submit_fail({"card_ids": [101]})
        second = manager.submit_fail({"card_ids": [102]})

        self.assertEqual(first["status"], "auto-accepted")
        self.assertEqual(second["status"], "pending_user_confirmation")
        self.assertEqual(col.revlog, [101])
        self.assertEqual(pushes[0]["grading"]["status"], "applying")
        self.assertEqual(pushes[1]["grading"]["status"], "auto-accepted")
        self.assertEqual(pushes[-1]["grading"]["status"], "pending")

    def test_confirmation_fails_closed_if_card_identity_changes(self) -> None:
        col = FakeCol()
        col.add_card(FakeCard(101, 201), guid="guid-before")
        manager, pushes, _refreshes = self.manager(col)
        response = manager.submit_fail({"card_ids": [101]})
        col.notes[201].guid = "guid-after"

        manager.accept({"id": response["grading_id"]})

        self.assertEqual(col.revlog, [])
        self.assertEqual(pushes[-1]["grading"]["status"], "failed")
        self.assertIn("changed identity", pushes[-1]["grading"]["warnings"][-1])

    def test_reject_never_writes(self) -> None:
        col = FakeCol()
        col.add_card(FakeCard(101, 201))
        manager, pushes, _refreshes = self.manager(col)
        response = manager.submit_fail({"card_ids": [101]})

        manager.reject({"id": response["grading_id"]})

        self.assertEqual(col.revlog, [])
        self.assertEqual(pushes[-1]["grading"]["status"], "rejected")

    def test_switching_to_read_only_blocks_pending_ui_writes(self) -> None:
        col = FakeCol()
        col.add_card(FakeCard(101, 201, queue=-1))
        manager, pushes, _refreshes = self.manager(col)
        response = manager.submit_fail({"card_ids": [101]})
        manager._config["permission_mode"] = "read-only"

        manager.accept({"id": response["grading_id"]})

        self.assertEqual(col.revlog, [])
        self.assertEqual(pushes[-1]["grading"]["status"], "pending")
        self.assertIn("now read-only", pushes[-1]["grading"]["warnings"][-1])

        manager._config["permission_mode"] = "default"
        manager.accept({"id": response["grading_id"]})
        manager._config["permission_mode"] = "read-only"
        manager.make_available_from_failure({"id": response["grading_id"]})

        self.assertEqual(col.revlog, [101])
        self.assertEqual(col.cards[101].queue, -1)
        self.assertEqual(pushes[-1]["grading"]["available_card_ids"], [101])
        self.assertIn("now read-only", pushes[-1]["grading"]["warnings"][-1])


if __name__ == "__main__":
    unittest.main()
