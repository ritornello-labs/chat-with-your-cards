"""Setting a card aside (task #32, redesigned 2026-07-29).

The invariant that forced the redesign: the backend only answers its own
queue top ("not at top of queue" on anything else), so a card can only be
served later by genuinely LEAVING the queue - marker + bury, one undo entry.
The recall pin floats a card to the true top by transiently parking whatever
sits ahead of it, released on the next fetch.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from chat_with_your_cards.deferral import (  # noqa: E402
    MARKER_KEY,
    PARK_KEY,
    SUMMARY_MAX_CHARS,
    DeferralManager,
    card_summary,
    render_text,
)


class FakeCard:
    def __init__(self, card_id: int, custom_data: str = "") -> None:
        self.id = card_id
        self.custom_data = custom_data
        self.queue = 2
        self.due = 5
        self.type = 2
        self.ivl = 12


class FakeSched:
    def __init__(self, col: "FakeCol") -> None:
        self.col = col
        self.today = 7
        self.buried: list[list[int]] = []
        self.unburied: list[list[int]] = []

    def bury_cards(self, ids, manual=True):
        self.buried.append([int(i) for i in ids])
        for i in ids:
            self.col.cards[int(i)].queue = -3

    def unbury_cards(self, ids):
        self.unburied.append([int(i) for i in ids])
        for i in ids:
            if int(i) in self.col.cards:
                self.col.cards[int(i)].queue = 2

    def get_queued_cards(self, fetch_limit=1):
        entries = [
            SimpleNamespace(card=SimpleNamespace(id=c.id))
            for c in self.col.cards.values()
            if c.queue >= 0
        ][:fetch_limit]
        return SimpleNamespace(cards=entries)


class FakeCol:
    def __init__(self) -> None:
        self.cards = {i: FakeCard(i) for i in (1, 2, 3, 4)}
        self.sched = FakeSched(self)
        self.undo_entries: list[str] = []
        self.merges = 0

    def get_card(self, card_id: int) -> FakeCard:
        return self.cards[int(card_id)]

    def update_card(self, card: FakeCard) -> None:
        pass

    def add_custom_undo_entry(self, name: str) -> int:
        self.undo_entries.append(name)
        return len(self.undo_entries)

    def merge_undo_entries(self, target: int) -> None:
        self.merges += 1

    def find_cards(self, query: str) -> list[int]:
        # Emulate prop:cdn:<key>=<day> against the fakes' custom_data.
        if "prop:cdn:" not in query:
            return []
        key, _, day = query.split("prop:cdn:")[1].partition("=")
        out = []
        for card in self.cards.values():
            try:
                data = json.loads(card.custom_data or "{}")
            except ValueError:
                continue
            if str(data.get(key)) == day:
                out.append(card.id)
        return out


class DeferTests(unittest.TestCase):
    def setUp(self) -> None:
        self.col = FakeCol()
        self.manager = DeferralManager(lambda: self.col)

    def test_defer_marks_and_buries_as_one_undo_entry(self) -> None:
        """One named entry, so Anki's own Cmd+Z reverts marker AND bury at
        once (the user's requested undo path)."""
        self.manager.defer(1)
        card = self.col.cards[1]
        self.assertEqual(7, json.loads(card.custom_data)[MARKER_KEY])
        self.assertEqual(-3, card.queue)
        self.assertEqual(["Set Card Aside"], self.col.undo_entries)
        self.assertGreaterEqual(self.col.merges, 2)

    def test_defer_touches_no_scheduling_field(self) -> None:
        before = (self.col.cards[1].due, self.col.cards[1].type, self.col.cards[1].ivl)
        self.manager.defer(1)
        after = (self.col.cards[1].due, self.col.cards[1].type, self.col.cards[1].ivl)
        self.assertEqual(before, after)

    def test_marker_expires_by_meaning(self) -> None:
        self.manager.defer(1)
        self.assertTrue(self.manager.is_deferred(self.col.cards[1]))
        self.col.sched.today = 8
        self.assertFalse(self.manager.is_deferred(self.col.cards[1]))

    def test_defer_preserves_fsrs_custom_data(self) -> None:
        self.col.cards[1].custom_data = json.dumps({"s": 1.5, "d": 4.2})
        self.manager.defer(1)
        data = json.loads(self.col.cards[1].custom_data)
        self.assertEqual(1.5, data["s"])
        self.assertIn(MARKER_KEY, data)

    def test_undefer_unburies_and_unmarks(self) -> None:
        self.manager.defer(1)
        self.manager.undefer(1)
        self.assertEqual(2, self.col.cards[1].queue)
        self.assertNotIn(MARKER_KEY, self.col.cards[1].custom_data)

    def test_deferred_ids_come_from_the_synced_marker(self) -> None:
        self.manager.defer(2)
        self.assertEqual([2], self.manager.deferred_card_ids())

    def test_deferred_ids_list_newest_first(self) -> None:
        """The tray shows the most recently set-aside card on top, and
        bring_back_deferred takes ids[0] as "newest" - same contract."""
        self.manager.defer(2)
        self.manager.defer(4)
        self.manager.defer(1)
        self.assertEqual([1, 4, 2], self.manager.deferred_card_ids())

    def test_unknown_order_cards_list_after_known_ones(self) -> None:
        """A card whose defer this session never saw (synced in, or from
        before a restart) still lists - after the ones we ordered."""
        self.manager.defer(3)
        self.manager._mark(self.col, 1, MARKER_KEY)  # marker, no session order
        self.assertEqual([3, 1], self.manager.deferred_card_ids())


class RecallPinTests(unittest.TestCase):
    def setUp(self) -> None:
        self.col = FakeCol()
        self.manager = DeferralManager(lambda: self.col)

    def test_pin_parks_the_cards_ahead_and_only_those(self) -> None:
        """Floating card 3 must park 1 and 2 (they become buried, marked) and
        leave 4 alone - the stock fetch then serves 3 as the TRUE top, which
        is what keeps answering it valid."""
        self.manager.show_next(3)
        self.manager._before_fetch()
        self.assertEqual(-3, self.col.cards[1].queue)
        self.assertEqual(-3, self.col.cards[2].queue)
        self.assertEqual(2, self.col.cards[3].queue)
        self.assertEqual(2, self.col.cards[4].queue)
        self.assertIn(PARK_KEY, self.col.cards[1].custom_data)
        self.assertIsNone(self.manager.pinned)  # spent

    def test_next_fetch_releases_the_parked_cards(self) -> None:
        self.manager.show_next(3)
        self.manager._before_fetch()
        self.manager._before_fetch()  # the fetch AFTER the pinned card
        self.assertEqual(2, self.col.cards[1].queue)
        self.assertEqual(2, self.col.cards[2].queue)
        self.assertNotIn(PARK_KEY, self.col.cards[1].custom_data)

    def test_crash_healing_releases_marker_only_parks(self) -> None:
        """A park that survived a crash has the marker but no session memory;
        the sweep must still release it."""
        self.manager._mark(self.col, 2, PARK_KEY)
        self.col.cards[2].queue = -3
        self.manager._before_fetch()
        self.assertEqual(2, self.col.cards[2].queue)
        self.assertNotIn(PARK_KEY, self.col.cards[2].custom_data)

    def test_pin_to_an_unreachable_card_is_dropped(self) -> None:
        self.col.cards[3].queue = -1  # suspended meanwhile
        self.manager.show_next(3)
        self.col.cards[3].queue = -1  # undefer's unbury un-buried it; re-hide
        self.manager._before_fetch()
        self.assertIsNone(self.manager.pinned)
        self.assertEqual([], self.manager._parked)

    def test_pin_already_on_top_parks_nothing(self) -> None:
        self.manager.show_next(1)
        self.manager._before_fetch()
        self.assertEqual([], self.manager._parked)
        self.assertEqual(2, self.col.cards[2].queue)

    def test_clear_session_releases_parks(self) -> None:
        self.manager.show_next(3)
        self.manager._before_fetch()
        self.manager.clear_session()
        self.assertEqual(2, self.col.cards[1].queue)


class RenderableCard(FakeCard):
    """A fake with just enough surface for card_summary (task #33)."""

    def __init__(self, card_id: int, question: str, answer: str, did: int = 7) -> None:
        super().__init__(card_id)
        self.did = did
        self.odid = 0
        self._q = question
        self._a = answer

    def question(self) -> str:
        return self._q

    def answer(self) -> str:
        return self._a


class SummaryTests(unittest.TestCase):
    """render_text / card_summary: what the set-aside tray shows (task #33)."""

    def test_render_text_strips_markup_and_marks_media(self) -> None:
        html = (
            "<style>.card{color:red}</style>"
            '<div>Locate on the <b>blank map</b>:</div>'
            '<img src="br.png"> [sound:xuexi.mp3] [anki:play:q:0]'
            "&nbsp;Rio&nbsp;Grande"
        )
        text = render_text(html)
        self.assertNotIn("<", text)
        self.assertNotIn("color:red", text)
        self.assertIn("Locate on the blank map:", text)
        self.assertIn("[image]", text)
        self.assertIn("[audio]", text)
        self.assertIn("Rio Grande", text)

    def test_render_text_caps_length(self) -> None:
        text = render_text("word " * 500)
        self.assertLessEqual(len(text), SUMMARY_MAX_CHARS)
        self.assertTrue(text.endswith("…"))

    def test_card_summary_splits_the_answer_at_the_hr(self) -> None:
        col = FakeCol()
        col.decks = SimpleNamespace(name=lambda did: "Math::Analysis")
        card = RenderableCard(
            9,
            "Define the limit of f at a.",
            "Define the limit of f at a.<hr id=answer>For every epsilon...",
        )
        col.cards[9] = card
        summary = card_summary(col, 9)
        self.assertEqual("Math::Analysis", summary["deck"])
        self.assertEqual("Define the limit of f at a.", summary["front"])
        self.assertEqual("For every epsilon...", summary["back"])
        self.assertNotIn("Define the limit", summary["back"])

    def test_card_summary_survives_a_broken_template(self) -> None:
        col = FakeCol()
        col.decks = SimpleNamespace(name=lambda did: "Deck")
        card = RenderableCard(9, "", "")
        card.question = lambda: (_ for _ in ()).throw(RuntimeError("boom"))  # type: ignore[method-assign]
        card.note = lambda: SimpleNamespace(values=lambda: ["Raw front", "Raw back"])  # type: ignore[attr-defined]
        col.cards[9] = card
        summary = card_summary(col, 9)
        self.assertEqual("Raw front", summary["front"])
        self.assertEqual("Raw back", summary["back"])


if __name__ == "__main__":
    unittest.main()
