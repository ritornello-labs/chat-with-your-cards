"""Deferring a card to later in the session (task #32).

The point of the design is what it does NOT do: no bury, no reschedule, no
scheduling field touched. The persisted half is a marker that expires by
meaning; the "show it next" half is session-only, as the user asked.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from chat_with_your_cards.deferral import MARKER_KEY, DeferralManager  # noqa: E402


class FakeCard:
    def __init__(self, card_id: int, custom_data: str = "") -> None:
        self.id = card_id
        self.custom_data = custom_data
        self.queue = 2
        self.due = 5
        self.type = 2


class FakeCol:
    def __init__(self, today: int = 7) -> None:
        self.cards = {1: FakeCard(1), 2: FakeCard(2), 3: FakeCard(3)}
        self.sched = SimpleNamespace(today=today)
        self.updated: list[int] = []

    def get_card(self, card_id: int) -> FakeCard:
        return self.cards[int(card_id)]

    def update_card(self, card: FakeCard) -> None:
        self.updated.append(int(card.id))

    def find_cards(self, _query: str) -> list[int]:
        return list(self.cards)


def entry(card_id: int) -> SimpleNamespace:
    return SimpleNamespace(card=SimpleNamespace(id=card_id))


class MarkerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.col = FakeCol()
        self.manager = DeferralManager(lambda: self.col)

    def test_defer_marks_without_touching_scheduling(self) -> None:
        before = vars(self.col.cards[1]).copy()
        self.manager.defer(1)
        card = self.col.cards[1]
        self.assertEqual({MARKER_KEY: 7}, json.loads(card.custom_data))
        # The whole point: Bury/reschedule change these; this must not.
        for field in ("queue", "due", "type"):
            self.assertEqual(before[field], getattr(card, field))

    def test_marker_is_only_valid_on_the_day_it_was_made(self) -> None:
        """It expires by MEANING, so there is no cleanup pass to forget."""
        self.manager.defer(1)
        self.assertTrue(self.manager.is_deferred(self.col.cards[1]))
        self.col.sched.today = 8
        self.assertFalse(self.manager.is_deferred(self.col.cards[1]))

    def test_defer_preserves_other_custom_data(self) -> None:
        """FSRS keeps its memory state here; clobbering it would be a real bug."""
        self.col.cards[1].custom_data = json.dumps({"s": 1.5, "d": 4.2})
        self.manager.defer(1)
        data = json.loads(self.col.cards[1].custom_data)
        self.assertEqual(1.5, data["s"])
        self.assertIn(MARKER_KEY, data)

    def test_undefer_leaves_other_custom_data_behind(self) -> None:
        self.col.cards[1].custom_data = json.dumps({"s": 1.5})
        self.manager.defer(1)
        self.manager.undefer(1)
        self.assertEqual({"s": 1.5}, json.loads(self.col.cards[1].custom_data))

    def test_undefer_on_an_untouched_card_writes_nothing(self) -> None:
        self.manager.undefer(2)
        self.assertEqual([], self.col.updated)

    def test_unparseable_custom_data_is_not_fatal(self) -> None:
        self.col.cards[1].custom_data = "not json"
        self.assertFalse(self.manager.is_deferred(self.col.cards[1]))
        self.manager.defer(1)
        self.assertTrue(self.manager.is_deferred(self.col.cards[1]))


class OrderingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.col = FakeCol()
        self.manager = DeferralManager(lambda: self.col)
        self.entries = [entry(1), entry(2), entry(3)]

    def deferred(self, ids: set[int]):
        return lambda e: int(e.card.id) in ids

    def test_nothing_deferred_keeps_ankis_own_order(self) -> None:
        index = self.manager.choose(self.entries, self.deferred(set()))
        self.assertEqual(0, index)

    def test_the_first_undeferred_card_wins(self) -> None:
        index = self.manager.choose(self.entries, self.deferred({1, 2}))
        self.assertEqual(3, self.entries[index].card.id)

    def test_deferred_top_card_is_skipped(self) -> None:
        index = self.manager.choose(self.entries, self.deferred({1}))
        self.assertEqual(2, self.entries[index].card.id)

    def test_a_pinned_card_beats_everything(self) -> None:
        self.manager.show_next(3)
        index = self.manager.choose(self.entries, self.deferred(set()))
        self.assertEqual(3, self.entries[index].card.id)

    def test_show_next_clears_the_marker(self) -> None:
        """Otherwise the card comes back and is skipped the instant it lands."""
        self.manager.defer(3)
        self.manager.show_next(3)
        self.assertFalse(self.manager.is_deferred(self.col.cards[3]))

    def test_all_deferred_shows_one_anyway(self) -> None:
        """The fetch window is a horizon, not infinity. Showing a deferred card
        beats blanking the reviewer or looping."""
        index = self.manager.choose(self.entries, self.deferred({1, 2, 3}))
        self.assertEqual(0, index)

    def test_deferring_the_pinned_card_drops_the_pin(self) -> None:
        self.manager.show_next(2)
        self.manager.defer(2)
        self.assertIsNone(self.manager.pinned)

    def test_the_pin_is_session_only(self) -> None:
        """The user asked for exactly this: 'not now' persists, 'show it next'
        does not."""
        self.manager.show_next(2)
        self.manager.clear_session()
        self.assertIsNone(self.manager.pinned)


if __name__ == "__main__":
    unittest.main()
