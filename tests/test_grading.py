from __future__ import annotations

import copy
import re
import unittest
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

from chat_with_your_cards.grading import (
    CURSOR_CONFIG_KEY,
    GradeRating,
    GradingError,
    GradingEventRef,
    GradingTarget,
    fail_cards_now,
)


@dataclass
class FakeCard:
    id: int
    nid: int
    did: int = 1
    odid: int = 0
    queue: int = 2
    reps: int = 4
    usn: int = 0


@dataclass
class FakeNote:
    id: int
    guid: str


class FakeDecks:
    def __init__(self) -> None:
        self.items: dict[int, dict[str, Any]] = {
            1: {"id": 1, "name": "Home", "dyn": 0},
            2: {"id": 2, "name": "Preview", "dyn": 1, "resched": False},
            3: {"id": 3, "name": "Reschedule", "dyn": 1, "resched": True},
        }

    def get(self, did: int, default: bool = True) -> dict[str, Any] | None:
        if did in self.items:
            return self.items[did]
        return self.items[1] if default else None

    def all_names_and_ids(self) -> list[Any]:
        return [SimpleNamespace(id=did, name=deck["name"]) for did, deck in self.items.items()]


class FakeDB:
    def __init__(self, col: "FakeCol") -> None:
        self.col = col

    def transact(self, op: Any) -> None:
        snapshot = (
            copy.deepcopy(self.col.cards),
            copy.deepcopy(self.col.revlog),
            copy.deepcopy(self.col.config),
            self.col.scm,
        )
        try:
            op()
        except BaseException:
            self.col.cards, self.col.revlog, self.col.config, self.col.scm = snapshot
            raise

    def scalar(self, sql: str) -> int:
        normalized = " ".join(sql.lower().split())
        if "count() from notes" in normalized:
            return len(self.col.notes)
        if "count() from cards" in normalized:
            return len(self.col.cards)
        if "select scm from col" in normalized:
            return self.col.scm
        if "count() from revlog where cid in" in normalized:
            ids = _ids_from_sql(sql)
            return sum(1 for cid in self.col.revlog if cid in ids)
        raise AssertionError(f"unexpected scalar SQL: {sql}")

    def list(self, sql: str, *args: Any) -> list[int]:
        normalized = " ".join(sql.lower().split())
        if "did = ? and odid = 0" in normalized:
            did = int(args[0])
            return [c.id for c in self.col.cards.values() if c.did == did and c.odid == 0]
        if "usn != -1" in normalized:
            ids = _ids_from_sql(sql)
            return [c.id for c in self.col.cards.values() if c.id in ids and c.usn != -1]
        raise AssertionError(f"unexpected list SQL: {sql}")

    def all(self, sql: str, *args: Any) -> list[tuple[int, int]]:
        normalized = " ".join(sql.lower().split())
        if "select id, odid from cards where odid != 0" in normalized:
            ids = _ids_from_sql(sql)
            return [(c.id, c.odid) for c in self.col.cards.values() if c.id in ids and c.odid]
        raise AssertionError(f"unexpected all SQL: {sql}")


def _ids_from_sql(sql: str) -> set[int]:
    match = re.search(r"\bin\s*\(([^)]*)\)", sql, re.IGNORECASE)
    if not match:
        return set()
    return {int(value.strip()) for value in match.group(1).split(",") if value.strip()}


class FakeBackend:
    def __init__(self, col: "FakeCol") -> None:
        self.col = col
        self.calls: list[tuple[tuple[int, ...], int]] = []
        self.wrong_reps = False

    def grade_now(self, *, card_ids: list[int], rating: int) -> None:
        self.calls.append((tuple(card_ids), int(rating)))
        changed = False
        for cid in card_ids:
            card = self.col.cards[cid]
            deck = self.col.decks.get(card.did, default=False)
            preview = bool(deck and deck.get("dyn") and not deck.get("resched", True))
            self.col.revlog.append(cid)
            changed = True
            if int(rating) == GradeRating.EASY and preview:
                card.did = card.odid
                card.odid = 0
                if card.queue >= 0:
                    card.queue = 2
            elif int(rating) == GradeRating.AGAIN:
                card.reps += 2 if self.wrong_reps else 1
                card.queue = 1
            card.usn = -1
        if changed:
            self.col._push_undo()


class FakeSched:
    def __init__(self, col: "FakeCol") -> None:
        self.col = col
        self.suspend_calls: list[tuple[int, ...]] = []

    def suspend_cards(self, card_ids: list[int]) -> None:
        self.suspend_calls.append(tuple(card_ids))
        changed = False
        for cid in card_ids:
            card = self.col.cards[cid]
            if card.queue != -1:
                card.queue = -1
                card.usn = -1
                changed = True
        if changed:
            self.col._push_undo()


class FakeCol:
    def __init__(self) -> None:
        self.decks = FakeDecks()
        self.cards: dict[int, FakeCard] = {}
        self.notes: dict[int, FakeNote] = {}
        self.revlog: list[int] = []
        self.config: dict[str, Any] = {}
        self.scm = 100
        self._undo_counter = 10
        self._undo_stack: list[int] = [10]
        self.undo_calls = 0
        self.merged: list[int] = []
        self.db = FakeDB(self)
        self._backend = FakeBackend(self)
        self.sched = FakeSched(self)

    def add_card(self, card: FakeCard, *, guid: str | None = None) -> None:
        self.cards[card.id] = card
        self.notes.setdefault(card.nid, FakeNote(card.nid, guid or f"guid-{card.nid}"))

    def get_card(self, cid: int) -> FakeCard:
        return self.cards[cid]

    def get_note(self, nid: int) -> FakeNote:
        return self.notes[nid]

    def get_config(self, key: str, default: Any = None) -> Any:
        return self.config.get(key, default)

    def set_config(self, key: str, value: Any, *, undoable: bool = False) -> None:
        self.config[key] = copy.deepcopy(value)

    def _push_undo(self) -> int:
        self._undo_counter += 1
        self._undo_stack.append(self._undo_counter)
        return self._undo_counter

    def add_custom_undo_entry(self, _name: str) -> int:
        return self._push_undo()

    def undo_status(self) -> Any:
        return SimpleNamespace(last_step=self._undo_stack[-1])

    def merge_undo_entries(self, target: int) -> None:
        self.merged.append(target)
        self._undo_stack = [step for step in self._undo_stack if step <= target]

    def undo(self) -> None:
        self.undo_calls += 1
        if len(self._undo_stack) <= 1:
            raise RuntimeError("no owned undo")
        self._undo_stack.pop()


class NativeGradingTests(unittest.TestCase):
    def test_normal_future_card_gets_one_native_again(self) -> None:
        col = FakeCol()
        col.add_card(FakeCard(101, 201, reps=7))

        result = fail_cards_now(col, [101])

        self.assertEqual(result.card_ids, (101,))
        self.assertEqual(col._backend.calls, [((101,), GradeRating.AGAIN)])
        self.assertEqual(col.cards[101].reps, 8)
        self.assertEqual(col.revlog, [101])
        self.assertEqual(len(col.merged), 1)

    def test_preview_card_exits_individually_then_gets_again(self) -> None:
        col = FakeCol()
        col.add_card(FakeCard(101, 201, did=2, odid=1))
        col.add_card(FakeCard(102, 202, did=2, odid=1))

        result = fail_cards_now(col, [101])

        self.assertEqual(result.preview_exits, (101,))
        self.assertEqual(
            col._backend.calls,
            [((101,), GradeRating.EASY), ((101,), GradeRating.AGAIN)],
        )
        self.assertEqual((col.cards[101].did, col.cards[101].odid), (1, 0))
        self.assertEqual((col.cards[102].did, col.cards[102].odid), (2, 1))
        self.assertEqual(col.cards[101].reps, 5)
        self.assertEqual(col.revlog, [101, 101])

    def test_rescheduling_filtered_card_uses_one_again_and_stays_valid(self) -> None:
        col = FakeCol()
        col.add_card(FakeCard(101, 201, did=3, odid=1))

        result = fail_cards_now(col, [101])

        self.assertEqual(result.rescheduling_filtered, (101,))
        self.assertEqual(col._backend.calls, [((101,), GradeRating.AGAIN)])
        self.assertEqual((col.cards[101].did, col.cards[101].odid), (3, 1))

    def test_suspension_is_restored_after_recording_failure(self) -> None:
        col = FakeCol()
        col.add_card(FakeCard(101, 201, queue=-1))

        result = fail_cards_now(col, [101])

        self.assertEqual(result.restored_suspended, (101,))
        self.assertEqual(col.sched.suspend_calls, [(101,)])
        self.assertEqual(col.cards[101].queue, -1)
        self.assertEqual(col.cards[101].reps, 5)

    def test_transient_burial_is_consumed_by_the_answer(self) -> None:
        col = FakeCol()
        col.add_card(FakeCard(101, 201, queue=-3))

        result = fail_cards_now(col, [101])

        self.assertEqual(result.restored_suspended, ())
        self.assertEqual(col.sched.suspend_calls, [])
        self.assertEqual(col.cards[101].queue, 1)
        self.assertEqual(col.cards[101].reps, 5)

    def test_duplicate_card_ids_are_graded_once(self) -> None:
        col = FakeCol()
        col.add_card(FakeCard(101, 201))

        result = fail_cards_now(col, [101, 101])

        self.assertEqual(result.card_ids, (101,))
        self.assertEqual(col.cards[101].reps, 5)
        self.assertEqual(col.revlog, [101])

    def test_event_cursor_makes_retry_a_noop(self) -> None:
        col = FakeCol()
        col.add_card(FakeCard(101, 201), guid="stable-guid")
        event = GradingEventRef("stream-a", 1, "event-a")
        target = GradingTarget(101, "stable-guid")

        first = fail_cards_now(col, [target], event=event)
        second = fail_cards_now(col, [target], event=event)

        self.assertFalse(first.already_applied)
        self.assertTrue(second.already_applied)
        self.assertEqual(col.cards[101].reps, 5)
        self.assertEqual(len(col._backend.calls), 1)
        self.assertEqual(col.config[CURSOR_CONFIG_KEY]["sequence"], 1)

    def test_event_gap_and_reused_sequence_are_rejected(self) -> None:
        col = FakeCol()
        col.add_card(FakeCard(101, 201), guid="stable-guid")
        target = GradingTarget(101, "stable-guid")
        fail_cards_now(
            col, [target], event=GradingEventRef("stream-a", 1, "event-a")
        )

        with self.assertRaisesRegex(GradingError, "different event id"):
            fail_cards_now(
                col, [target], event=GradingEventRef("stream-a", 1, "event-b")
            )
        with self.assertRaisesRegex(GradingError, "gap"):
            fail_cards_now(
                col, [target], event=GradingEventRef("stream-a", 3, "event-c")
            )

    def test_stale_note_guid_is_rejected_before_any_answer(self) -> None:
        col = FakeCol()
        col.add_card(FakeCard(101, 201), guid="actual-guid")

        with self.assertRaisesRegex(GradingError, "expected note"):
            fail_cards_now(
                col,
                [GradingTarget(101, "stale-guid")],
                event=GradingEventRef("stream-a", 1, "event-a"),
            )

        self.assertEqual(col._backend.calls, [])
        self.assertNotIn(CURSOR_CONFIG_KEY, col.config)

    def test_homeless_filtered_card_is_rejected(self) -> None:
        col = FakeCol()
        col.add_card(FakeCard(101, 201, did=2, odid=0))

        with self.assertRaisesRegex(GradingError, "homeless"):
            fail_cards_now(col, [101])

        self.assertEqual(col._backend.calls, [])

    def test_postcondition_failure_rolls_back_and_cleans_owned_undo(self) -> None:
        col = FakeCol()
        col.add_card(FakeCard(101, 201, reps=7))
        col._backend.wrong_reps = True
        original_undo_stack = list(col._undo_stack)

        with self.assertRaisesRegex(GradingError, r"expected \+1"):
            fail_cards_now(col, [101])

        self.assertEqual(col.cards[101].reps, 7)
        self.assertEqual(col.revlog, [])
        self.assertEqual(col._undo_stack, original_undo_stack)
        self.assertEqual(col.undo_calls, 2)  # custom boundary + Grade Now


if __name__ == "__main__":
    unittest.main()
