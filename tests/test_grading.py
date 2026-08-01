from __future__ import annotations

import copy
import re
import unittest
from dataclasses import dataclass
from typing import Any

from chat_with_your_cards._vendor.safe_collection_operations.grading import (
    CURSOR_CONFIG_KEY,
    fail_cards_now,
    get_grading_cursor,
    inspect_cards,
    make_cards_available,
)
from chat_with_your_cards._vendor.safe_collection_operations.models import (
    EventRef,
    OperationError,
    Rating,
    Target,
)


@dataclass
class FakeCard:
    id: int
    nid: int
    did: int = 1
    odid: int = 0
    queue: int = 2
    reps: int = 4

    def template(self) -> dict[str, str]:
        return {"name": "Card 1"}


@dataclass
class FakeNote:
    id: int
    guid: str

    def items(self) -> list[tuple[str, str]]:
        return [("Front", f"Prompt {self.id}"), ("Back", "Answer")]


class FakeDecks:
    def __init__(self) -> None:
        self.items = {
            1: {"id": 1, "name": "Home", "dyn": 0},
            2: {"id": 2, "name": "Preview", "dyn": 1, "resched": False},
            3: {"id": 3, "name": "Reschedule", "dyn": 1, "resched": True},
        }

    def get(self, deck_id: int, default: bool = True) -> dict[str, Any] | None:
        if deck_id in self.items:
            return self.items[deck_id]
        return self.items[1] if default else None

    def name(self, deck_id: int) -> str:
        deck = self.get(deck_id, default=False)
        return str(deck["name"]) if deck else ""


class FakeDB:
    def __init__(self, col: FakeCol) -> None:
        self.col = col

    def transact(self, operation: Any) -> None:
        snapshot = (
            copy.deepcopy(self.col.cards),
            copy.deepcopy(self.col.revlog),
            copy.deepcopy(self.col.config),
        )
        try:
            operation()
        except BaseException:
            self.col.cards, self.col.revlog, self.col.config = snapshot
            raise

    def scalar(self, sql: str) -> int:
        if "count() from revlog where cid in" not in " ".join(sql.lower().split()):
            raise AssertionError(f"unexpected scalar SQL: {sql}")
        ids = _ids_from_sql(sql)
        return sum(1 for card_id in self.col.revlog if card_id in ids)


def _ids_from_sql(sql: str) -> set[int]:
    match = re.search(r"\bin\s*\(([^)]*)\)", sql, re.IGNORECASE)
    if not match:
        return set()
    return {int(value.strip()) for value in match.group(1).split(",") if value.strip()}


class FakeBackend:
    def __init__(self, col: FakeCol) -> None:
        self.col = col
        self.calls: list[tuple[tuple[int, ...], int]] = []
        self.wrong_reps = False
        self.leech_ids: set[int] = set()
        self.restore_calls: list[tuple[int, ...]] = []

    def grade_now(self, *, card_ids: list[int], rating: int) -> None:
        self.calls.append((tuple(card_ids), int(rating)))
        for card_id in card_ids:
            card = self.col.cards[card_id]
            deck = self.col.decks.get(card.did, default=False)
            preview = bool(deck and deck.get("dyn") and not deck.get("resched", True))
            self.col.revlog.append(card_id)
            if int(rating) == Rating.EASY and preview:
                # Ending a preview returns the card home WITHOUT counting a
                # review, which is why a preview card still gets exactly +1.
                card.did = card.odid
                card.odid = 0
                if card.queue >= 0:
                    card.queue = 2
            else:
                # Every real rating counts a review. Firing only for AGAIN was
                # indistinguishable from correct while AGAIN was the only
                # rating the vendored core could record (#16).
                card.reps += 2 if self.wrong_reps else 1
                if card_id in self.leech_ids:
                    card.queue = -1
                else:
                    card.queue = 1 if int(rating) == Rating.AGAIN else 2
        self.col._push_undo()

    def restore_buried_and_suspended_cards(self, card_ids: list[int]) -> None:
        self.restore_calls.append(tuple(card_ids))
        for card_id in card_ids:
            self.col.cards[card_id].queue = 2
        self.col._push_undo()


class FakeSched:
    def __init__(self, col: FakeCol) -> None:
        self.col = col
        self.suspend_calls: list[tuple[int, ...]] = []
        self.bury_calls: list[tuple[tuple[int, ...], bool]] = []

    def suspend_cards(self, card_ids: list[int]) -> None:
        self.suspend_calls.append(tuple(card_ids))
        for card_id in card_ids:
            self.col.cards[card_id].queue = -1
        self.col._push_undo()

    def bury_cards(self, card_ids: list[int], *, manual: bool) -> None:
        self.bury_calls.append((tuple(card_ids), manual))
        for card_id in card_ids:
            self.col.cards[card_id].queue = -3 if manual else -2
        self.col._push_undo()


class FakeCol:
    def __init__(self) -> None:
        self.decks = FakeDecks()
        self.cards: dict[int, FakeCard] = {}
        self.notes: dict[int, FakeNote] = {}
        self.revlog: list[int] = []
        self.config: dict[str, Any] = {}
        self._undo_counter = 10
        self._undo_stack = [10]
        self.undo_calls = 0
        self.merged: list[int] = []
        self.db = FakeDB(self)
        self._backend = FakeBackend(self)
        self.sched = FakeSched(self)

    def add_card(self, card: FakeCard, *, guid: str | None = None) -> None:
        self.cards[card.id] = card
        self.notes.setdefault(card.nid, FakeNote(card.nid, guid or f"guid-{card.nid}"))

    def get_card(self, card_id: int) -> FakeCard:
        return self.cards[card_id]

    def get_note(self, note_id: int) -> FakeNote:
        return self.notes[note_id]

    def get_config(self, key: str, default: Any = None) -> Any:
        return self.config.get(key, default)

    def set_config(self, key: str, value: Any, *, undoable: bool = False) -> None:
        del undoable
        self.config[key] = copy.deepcopy(value)

    def _push_undo(self) -> int:
        self._undo_counter += 1
        self._undo_stack.append(self._undo_counter)
        return self._undo_counter

    def add_custom_undo_entry(self, _name: str) -> int:
        return self._push_undo()

    def merge_undo_entries(self, target: int) -> None:
        self.merged.append(target)
        self._undo_stack = [step for step in self._undo_stack if step <= target]

    def undo(self) -> None:
        self.undo_calls += 1
        if len(self._undo_stack) <= 1:
            raise RuntimeError("no owned undo")
        self._undo_stack.pop()


class NativeGradingTests(unittest.TestCase):
    def test_future_card_gets_exactly_one_native_again(self) -> None:
        col = FakeCol()
        col.add_card(FakeCard(101, 201, reps=7))

        result = fail_cards_now(col, [101])

        self.assertEqual(result.card_ids, (101,))
        self.assertEqual(col._backend.calls, [((101,), Rating.AGAIN)])
        self.assertEqual((col.cards[101].reps, col.revlog), (8, [101]))

    def test_preview_card_exits_alone_then_gets_again_at_home(self) -> None:
        col = FakeCol()
        col.add_card(FakeCard(101, 201, did=2, odid=1))
        col.add_card(FakeCard(102, 202, did=2, odid=1))

        result = fail_cards_now(col, [101])

        self.assertEqual(result.preview_exits, (101,))
        self.assertEqual(
            col._backend.calls,
            [((101,), Rating.EASY), ((101,), Rating.AGAIN)],
        )
        self.assertEqual((col.cards[101].did, col.cards[101].odid), (1, 0))
        self.assertEqual((col.cards[102].did, col.cards[102].odid), (2, 1))
        self.assertEqual((col.cards[101].reps, col.revlog), (5, [101, 101]))

    def test_rescheduling_filtered_card_gets_again_in_place(self) -> None:
        col = FakeCol()
        col.add_card(FakeCard(101, 201, did=3, odid=1))

        result = fail_cards_now(col, [101])

        self.assertEqual(result.rescheduling_filtered, (101,))
        self.assertEqual(col._backend.calls, [((101,), Rating.AGAIN)])
        self.assertEqual((col.cards[101].did, col.cards[101].odid), (3, 1))

    def test_each_existing_hidden_state_is_restored_exactly(self) -> None:
        cases = (
            (-1, "preserved_suspended"),
            (-2, "preserved_sched_buried"),
            (-3, "preserved_user_buried"),
        )
        for queue, result_field in cases:
            with self.subTest(queue=queue):
                col = FakeCol()
                col.add_card(FakeCard(101, 201, queue=queue))
                result = fail_cards_now(col, [101])
                self.assertEqual(col.cards[101].queue, queue)
                self.assertEqual(getattr(result, result_field), (101,))
                self.assertEqual(col.cards[101].reps, 5)

    def test_native_leech_suspension_is_stronger_than_previous_burial(self) -> None:
        col = FakeCol()
        col.add_card(FakeCard(101, 201, queue=-3))
        col._backend.leech_ids.add(101)

        result = fail_cards_now(col, [101])

        self.assertEqual(col.cards[101].queue, -1)
        self.assertEqual(result.newly_suspended, (101,))
        self.assertEqual(result.preserved_user_buried, ())
        self.assertEqual(col.sched.bury_calls, [])

    def test_duplicate_targets_are_graded_once_and_conflicts_fail(self) -> None:
        col = FakeCol()
        col.add_card(FakeCard(101, 201), guid="stable")
        result = fail_cards_now(col, [101, 101])
        self.assertEqual(result.card_ids, (101,))
        with self.assertRaisesRegex(OperationError, "conflicting"):
            fail_cards_now(col, [Target(101, "a"), Target(101, "b")])

    def test_event_retry_is_noop_and_cursor_is_per_stream(self) -> None:
        col = FakeCol()
        col.add_card(FakeCard(101, 201), guid="stable")
        event = EventRef("stream-a", 1, "event-a")
        target = Target(101, "stable")

        self.assertFalse(fail_cards_now(col, [target], event=event).already_applied)
        self.assertTrue(fail_cards_now(col, [target], event=event).already_applied)
        self.assertEqual(col.cards[101].reps, 5)
        self.assertEqual(col.config[CURSOR_CONFIG_KEY]["stream-a"]["sequence"], 1)
        self.assertEqual(
            get_grading_cursor(col, "stream-a"),
            {"stream_id": "stream-a", "sequence": 1, "event_id": "event-a"},
        )
        self.assertEqual(
            get_grading_cursor(col, "unused"),
            {"stream_id": "unused", "sequence": 0, "event_id": None},
        )

    def test_event_gap_reuse_and_missing_guid_are_rejected(self) -> None:
        col = FakeCol()
        col.add_card(FakeCard(101, 201), guid="stable")
        target = Target(101, "stable")
        fail_cards_now(col, [target], event=EventRef("stream-a", 1, "event-a"))
        with self.assertRaisesRegex(OperationError, "different event id"):
            fail_cards_now(col, [target], event=EventRef("stream-a", 1, "event-b"))
        with self.assertRaisesRegex(OperationError, "gap"):
            fail_cards_now(col, [target], event=EventRef("stream-a", 3, "event-c"))
        with self.assertRaisesRegex(OperationError, "missing its note GUID"):
            fail_cards_now(col, [101], event=EventRef("stream-b", 1, "event-d"))

    def test_stale_guid_and_malformed_filtered_state_fail_before_answer(self) -> None:
        for card, message in (
            (FakeCard(101, 201), "expected note"),
            (FakeCard(101, 201, did=2, odid=0), "homeless"),
            (FakeCard(101, 201, did=1, odid=9), "not filtered"),
        ):
            with self.subTest(message=message):
                col = FakeCol()
                col.add_card(card, guid="actual")
                target = (
                    Target(101, "stale")
                    if message == "expected note"
                    else Target(101, "actual")
                )
                with self.assertRaisesRegex(OperationError, message):
                    fail_cards_now(col, [target], event=EventRef("stream", 1, "event"))
                self.assertEqual(col._backend.calls, [])

    def test_postcondition_failure_rolls_back_and_cleans_owned_undo(self) -> None:
        col = FakeCol()
        col.add_card(FakeCard(101, 201, reps=7))
        col._backend.wrong_reps = True
        original_undo = list(col._undo_stack)
        with self.assertRaisesRegex(OperationError, r"expected \+1"):
            fail_cards_now(col, [101])
        self.assertEqual((col.cards[101].reps, col.revlog), (7, []))
        self.assertEqual(col._undo_stack, original_undo)
        self.assertEqual(col.undo_calls, 2)

    def test_make_available_only_removes_hidden_state(self) -> None:
        col = FakeCol()
        for card_id, queue in ((101, -1), (102, -2), (103, -3), (104, 2)):
            col.add_card(FakeCard(card_id, card_id + 100, queue=queue))
        revlog_before = list(col.revlog)

        result = make_cards_available(col, [101, 102, 103, 104, 101])

        self.assertEqual(result.restored_suspended, (101,))
        self.assertEqual(result.restored_sched_buried, (102,))
        self.assertEqual(result.restored_user_buried, (103,))
        self.assertEqual(col._backend.restore_calls, [((101, 102, 103))])
        self.assertEqual(col.revlog, revlog_before)

    def test_inspect_cards_resolves_guid_and_filtered_context(self) -> None:
        col = FakeCol()
        col.add_card(FakeCard(101, 201, did=2, odid=1, queue=-3), guid="stable")

        result = inspect_cards(col, [101])

        self.assertEqual(
            result["cards"],
            [
                {
                    "card_id": 101,
                    "note_id": 201,
                    "note_guid": "stable",
                    "current_deck_id": 2,
                    "home_deck_id": 1,
                    "queue": -3,
                    "reps": 4,
                    "preview_filtered": True,
                    "rescheduling_filtered": False,
                }
            ],
        )


if __name__ == "__main__":
    unittest.main()
