"""Invariant postcondition tests against a fake collection.

The invariants read the collection through the DBProxy surface
(``col.db.scalar/list/all``) and the decks API, which the ProposalManager tests'
method-based ``FakeCol`` does not expose. So the double here backs ``col.db``
with a real in-memory SQLite connection (stdlib) holding the ``col``/``notes``/
``cards`` columns the checks read: the actual SELECTs run for real, exactly as
Anki's DBProxy would run them, instead of being pattern-matched. Decks stay an
in-memory dict mirroring ``get(default=False)`` and ``all_names_and_ids()``.
"""

from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from chat_with_your_cards.invariants import (  # noqa: E402
    Expectation,
    InvariantViolation,
    Scope,
    Snapshot,
    assert_all,
    count_delta_matches,
    no_dangling_odid,
    no_homeless_filtered_cards,
    schema_unchanged,
    snapshot,
    touched_rows_pending_sync,
)


class FakeDB:
    """DBProxy stand-in over an in-memory SQLite connection. scalar/list/all
    match anki/dbproxy.py: scalar = first column of first row (or None), list =
    first column of every row, all = every row as a list."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def scalar(self, sql: str, *args: object) -> object:
        row = self._conn.execute(sql, args).fetchone()
        return row[0] if row is not None else None

    def list(self, sql: str, *args: object) -> list:
        return [r[0] for r in self._conn.execute(sql, args).fetchall()]

    def all(self, sql: str, *args: object) -> list:
        return [list(r) for r in self._conn.execute(sql, args).fetchall()]


class _DeckId:
    def __init__(self, did: int, name: str) -> None:
        self.id = did
        self.name = name


class FakeDecks:
    """Backs the two decks calls the invariants use: get(did, default=False)
    returning a dict with a ``dyn`` flag (or None for a missing deck, never the
    Default fallback), and all_names_and_ids() for the live-deck-id set."""

    def __init__(self, decks: dict[int, dict]) -> None:
        self._decks = decks

    def get(self, did: int, default: bool = True) -> dict | None:
        deck = self._decks.get(int(did))
        if deck is None:
            # default=False is what the invariants pass; honor it faithfully.
            return dict(self._decks[1]) if default and 1 in self._decks else None
        return dict(deck)

    def all_names_and_ids(self) -> list[_DeckId]:
        return [_DeckId(d["id"], d["name"]) for d in self._decks.values()]


class FakeCol:
    def __init__(self, *, scm: int = 1000) -> None:
        self._conn = sqlite3.connect(":memory:")
        self._conn.executescript(
            "create table col (id integer primary key, scm integer);"
            "create table notes (id integer primary key, usn integer);"
            "create table cards (id integer primary key, nid integer, "
            "did integer, odid integer, usn integer);"
        )
        self._conn.execute("insert into col (id, scm) values (1, ?)", (scm,))
        self.db = FakeDB(self._conn)
        self._decks: dict[int, dict] = {1: {"id": 1, "name": "Default", "dyn": 0}}
        self.decks = FakeDecks(self._decks)

    # --- test construction helpers (not part of the collection API) ---

    def add_deck(self, did: int, name: str, *, dyn: int = 0) -> None:
        self._decks[did] = {"id": did, "name": name, "dyn": dyn}

    def drop_deck(self, did: int) -> None:
        self._decks.pop(did, None)

    def add_note(self, nid: int, *, usn: int = -1) -> None:
        self._conn.execute("insert into notes (id, usn) values (?, ?)", (nid, usn))

    def add_card(
        self,
        cid: int,
        *,
        nid: int = 0,
        did: int = 1,
        odid: int = 0,
        usn: int = -1,
    ) -> None:
        self._conn.execute(
            "insert into cards (id, nid, did, odid, usn) values (?, ?, ?, ?, ?)",
            (cid, nid, did, odid, usn),
        )

    def set_card(self, cid: int, **cols: int) -> None:
        assigns = ", ".join(f"{k} = ?" for k in cols)
        self._conn.execute(
            f"update cards set {assigns} where id = ?", (*cols.values(), cid)
        )

    def set_note(self, nid: int, **cols: int) -> None:
        assigns = ", ".join(f"{k} = ?" for k in cols)
        self._conn.execute(
            f"update notes set {assigns} where id = ?", (*cols.values(), nid)
        )

    def set_scm(self, scm: int) -> None:
        self._conn.execute("update col set scm = ? where id = 1", (scm,))


class HomelessFilteredTests(unittest.TestCase):
    def test_passes_when_filtered_cards_have_a_home(self) -> None:
        col = FakeCol()
        col.add_deck(2, "Cram", dyn=1)
        col.add_card(10, did=2, odid=1)  # in filtered deck, home recorded
        col.add_card(11, did=1, odid=0)  # normal deck, no home needed
        self.assertIsNone(no_homeless_filtered_cards(col, [1, 2]))

    def test_flags_filtered_card_with_no_home(self) -> None:
        col = FakeCol()
        col.add_deck(2, "Cram", dyn=1)
        col.add_card(10, did=2, odid=0)  # the corruption
        result = no_homeless_filtered_cards(col, [2])
        self.assertIsNotNone(result)
        assert result is not None
        self.assertIn("home", result)
        self.assertIn("10", result)

    def test_normal_deck_card_with_odid_zero_is_fine(self) -> None:
        col = FakeCol()
        col.add_card(10, did=1, odid=0)  # odid=0 is normal for a home deck
        self.assertIsNone(no_homeless_filtered_cards(col, [1]))

    def test_scoped_to_touched_decks(self) -> None:
        # A homeless card in a filtered deck NOT in scope is not this
        # mutation's business and must not be flagged.
        col = FakeCol()
        col.add_deck(2, "Cram", dyn=1)
        col.add_card(10, did=2, odid=0)
        self.assertIsNone(no_homeless_filtered_cards(col, [1]))
        self.assertIsNotNone(no_homeless_filtered_cards(col, [2]))

    def test_empty_scope_is_noop(self) -> None:
        col = FakeCol()
        self.assertIsNone(no_homeless_filtered_cards(col, []))


class DanglingOdidTests(unittest.TestCase):
    def test_passes_when_odid_points_at_live_deck(self) -> None:
        col = FakeCol()
        col.add_deck(2, "Cram", dyn=1)
        col.add_card(10, did=2, odid=1)  # home deck 1 exists
        self.assertIsNone(no_dangling_odid(col, [10]))

    def test_flags_odid_pointing_at_deleted_deck(self) -> None:
        col = FakeCol()
        col.add_deck(2, "Cram", dyn=1)
        col.add_deck(3, "Home", dyn=0)
        col.add_card(10, did=2, odid=3)
        col.drop_deck(3)  # home deck deleted out of band
        result = no_dangling_odid(col, [10])
        self.assertIsNotNone(result)
        assert result is not None
        self.assertIn("10", result)
        self.assertIn("3", result)

    def test_odid_zero_is_never_dangling(self) -> None:
        col = FakeCol()
        col.add_card(10, did=1, odid=0)
        self.assertIsNone(no_dangling_odid(col, [10]))

    def test_scoped_to_touched_cards(self) -> None:
        col = FakeCol()
        col.add_deck(2, "Cram", dyn=1)
        col.add_card(10, did=2, odid=999)  # dangling, but not in scope
        self.assertIsNone(no_dangling_odid(col, [11]))
        self.assertIsNotNone(no_dangling_odid(col, [10]))

    def test_empty_scope_is_noop(self) -> None:
        col = FakeCol()
        self.assertIsNone(no_dangling_odid(col, []))


class CountDeltaTests(unittest.TestCase):
    @staticmethod
    def _snap(notes: int, cards: int, scm: int = 1000) -> Snapshot:
        return Snapshot(
            note_count=notes, card_count=cards, scm=scm, scope=Scope()
        )

    def test_passes_when_deltas_match(self) -> None:
        before = self._snap(10, 20)
        after = self._snap(11, 22)
        expected = Expectation(note_delta=1, card_delta=2)
        self.assertIsNone(count_delta_matches(before, after, expected))

    def test_flags_card_blast_radius(self) -> None:
        # Declared +2 cards; a template add produced thousands.
        before = self._snap(1000, 1000)
        after = self._snap(1001, 5000)
        expected = Expectation(note_delta=1, card_delta=2)
        result = count_delta_matches(before, after, expected)
        self.assertIsNotNone(result)
        assert result is not None
        self.assertIn("cards", result)
        self.assertIn("+4000", result)  # 5000 - 1000, declared only +2

    def test_flags_note_delta_mismatch(self) -> None:
        before = self._snap(10, 20)
        after = self._snap(10, 20)  # nothing happened
        expected = Expectation(note_delta=1, card_delta=1)
        result = count_delta_matches(before, after, expected)
        self.assertIsNotNone(result)
        assert result is not None
        self.assertIn("notes", result)

    def test_negative_delta_for_delete(self) -> None:
        before = self._snap(10, 20)
        after = self._snap(8, 16)
        self.assertIsNone(
            count_delta_matches(
                before, after, Expectation(note_delta=-2, card_delta=-4)
            )
        )


class SchemaTests(unittest.TestCase):
    @staticmethod
    def _snap(scm: int) -> Snapshot:
        return Snapshot(note_count=0, card_count=0, scm=scm, scope=Scope())

    def test_passes_when_scm_unchanged(self) -> None:
        self.assertIsNone(schema_unchanged(self._snap(1000), self._snap(1000)))

    def test_flags_scm_bump(self) -> None:
        result = schema_unchanged(self._snap(1000), self._snap(1001))
        self.assertIsNotNone(result)
        assert result is not None
        self.assertIn("schema", result)
        self.assertIn("full sync", result)


class PendingSyncTests(unittest.TestCase):
    def test_passes_when_touched_rows_are_pending(self) -> None:
        col = FakeCol()
        col.add_note(100, usn=-1)
        col.add_card(1000, nid=100, usn=-1)
        self.assertIsNone(touched_rows_pending_sync(col, [100], [1000]))

    def test_flags_note_left_with_real_usn(self) -> None:
        col = FakeCol()
        col.add_note(100, usn=5)  # raw write: usn not reset to -1
        result = touched_rows_pending_sync(col, [100], [])
        self.assertIsNotNone(result)
        assert result is not None
        self.assertIn("note", result)
        self.assertIn("100", result)

    def test_flags_card_left_with_real_usn(self) -> None:
        col = FakeCol()
        col.add_card(1000, usn=7)
        result = touched_rows_pending_sync(col, [], [1000])
        self.assertIsNotNone(result)
        assert result is not None
        self.assertIn("card", result)

    def test_untouched_rows_ignored(self) -> None:
        # A row with a real usn that this mutation did not touch is irrelevant.
        col = FakeCol()
        col.add_note(100, usn=5)
        col.add_note(101, usn=-1)
        col.add_card(1000, nid=101, usn=-1)
        self.assertIsNone(touched_rows_pending_sync(col, [101], [1000]))

    def test_empty_scope_is_noop(self) -> None:
        col = FakeCol()
        self.assertIsNone(touched_rows_pending_sync(col, [], []))


class SnapshotTests(unittest.TestCase):
    def test_snapshot_captures_counts_scm_and_scope(self) -> None:
        col = FakeCol(scm=4242)
        col.add_note(100)
        col.add_note(101)
        col.add_card(1000, nid=100)
        scope = Scope(deck_ids=(1,), note_ids=(100, 101), card_ids=(1000,))
        snap = snapshot(col, scope)
        self.assertEqual(snap.note_count, 2)
        self.assertEqual(snap.card_count, 1)
        self.assertEqual(snap.scm, 4242)
        self.assertEqual(snap.scope, scope)


class AssertAllTests(unittest.TestCase):
    def _created_col(self) -> FakeCol:
        col = FakeCol(scm=1000)
        col.add_note(1, usn=0)  # pre-existing, untouched
        col.add_card(10, nid=1, did=1, odid=0, usn=0)
        return col

    def test_clean_create_passes(self) -> None:
        col = self._created_col()
        before = snapshot(col, Scope(deck_ids=(1,), note_ids=(2,), card_ids=(20,)))
        # A clean create of 1 note + 1 card, correctly marked pending.
        col.add_note(2, usn=-1)
        col.add_card(20, nid=2, did=1, odid=0, usn=-1)
        assert_all(col, before, Expectation(note_delta=1, card_delta=1))

    def test_homeless_filtered_card_rolls_back(self) -> None:
        col = self._created_col()
        col.add_deck(2, "Cram", dyn=1)
        before = snapshot(col, Scope(deck_ids=(2,), note_ids=(2,), card_ids=(20,)))
        col.add_note(2, usn=-1)
        col.add_card(20, nid=2, did=2, odid=0, usn=-1)  # homeless in filtered
        with self.assertRaises(InvariantViolation) as ctx:
            assert_all(col, before, Expectation(note_delta=1, card_delta=1))
        self.assertIn("no_homeless_filtered_cards", str(ctx.exception))

    def test_dangling_odid_rolls_back(self) -> None:
        col = self._created_col()
        col.add_deck(2, "Cram", dyn=1)
        before = snapshot(col, Scope(deck_ids=(2,), note_ids=(), card_ids=(20,)))
        col.add_card(20, nid=1, did=2, odid=777, usn=-1)  # home 777 does not exist
        with self.assertRaises(InvariantViolation) as ctx:
            assert_all(col, before, Expectation(note_delta=0, card_delta=1))
        self.assertIn("no_dangling_odid", str(ctx.exception))

    def test_count_blast_radius_rolls_back(self) -> None:
        col = self._created_col()
        before = snapshot(col, Scope(deck_ids=(1,), note_ids=(2,), card_ids=()))
        col.add_note(2, usn=-1)
        for cid in range(20, 4020):  # a template add flooded the queue
            col.add_card(cid, nid=2, did=1, odid=0, usn=-1)
        with self.assertRaises(InvariantViolation) as ctx:
            assert_all(col, before, Expectation(note_delta=1, card_delta=1))
        self.assertIn("count_delta_matches", str(ctx.exception))

    def test_undeclared_schema_bump_rolls_back(self) -> None:
        col = self._created_col()
        before = snapshot(col, Scope(deck_ids=(1,), note_ids=(), card_ids=()))
        col.set_scm(1001)  # scm bumped but expectation did not declare it
        with self.assertRaises(InvariantViolation) as ctx:
            assert_all(col, before, Expectation())
        self.assertIn("schema_unchanged", str(ctx.exception))

    def test_declared_schema_bump_is_allowed(self) -> None:
        col = self._created_col()
        # Declared schema change: +1 note per new template on the one note,
        # scm bumped. changes_schema waives the scm check.
        before = snapshot(col, Scope(deck_ids=(1,), note_ids=(1,), card_ids=(11,)))
        col.set_scm(1001)
        col.add_card(11, nid=1, did=1, odid=0, usn=-1)
        col.set_note(1, usn=-1)  # notetype edit re-marked the note pending
        assert_all(
            col,
            before,
            Expectation(note_delta=0, card_delta=1, changes_schema=True),
        )

    def test_unsynced_touched_row_rolls_back(self) -> None:
        col = self._created_col()
        before = snapshot(col, Scope(deck_ids=(1,), note_ids=(2,), card_ids=(20,)))
        col.add_note(2, usn=-1)
        col.add_card(20, nid=2, did=1, odid=0, usn=3)  # not marked pending
        with self.assertRaises(InvariantViolation) as ctx:
            assert_all(col, before, Expectation(note_delta=1, card_delta=1))
        self.assertIn("touched_rows_pending_sync", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
