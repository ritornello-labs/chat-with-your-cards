"""ProposalManager unit tests against a fake collection.

Covers validation, pins-as-constraints, the accept/reject flow, the
edit staleness guard, the session ledger with revert/undo, and
auto-accept with its per-session cap (DESIGN.md sections 5 and 8).
"""

from __future__ import annotations

import copy
import sqlite3
import sys
import unittest
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from chat_with_your_cards.proposals import (  # noqa: E402
    AI_TAG,
    ProposalError,
    ProposalManager,
)

BASIC = {"name": "Basic", "flds": [{"name": "Front"}, {"name": "Back"}], "css": ""}


class FakeCard:
    def __init__(self, did: int = 1, ord_: int = 0, reps: int = 0, cid: int = 0) -> None:
        self.did = did
        self.ord = ord_
        self.reps = reps
        self.id = cid
        self.nid = 0
        self.odid = 0  # home deck while the card sits in a filtered deck
        self.usn = -1  # -1 == pending sync, the state every touched row must hold
        self.memory_state: Any = None  # FSRS stability/difficulty, wiped by set_deck
        self.data = ""  # legacy JSON blob (s/d) fallback for FSRS detection


class FakeNote:
    def __init__(self, model: dict[str, Any]) -> None:
        self._model = model
        self._fields = {f["name"]: "" for f in model["flds"]}
        self.tags: list[str] = []
        self.id = 0
        self.usn = -1  # -1 == pending sync
        self._cards: list[FakeCard] = []

    def __getitem__(self, name: str) -> str:
        return self._fields[name]

    def __setitem__(self, name: str, value: str) -> None:
        if name not in self._fields:
            raise KeyError(name)
        self._fields[name] = value

    def items(self):
        return self._fields.items()

    def note_type(self) -> dict[str, Any]:
        return self._model

    def cards(self) -> list[FakeCard]:
        return self._cards


class _NamedId:
    def __init__(self, name: str, id_: int = 0) -> None:
        self.name = name
        self.id = id_


class FakeModels:
    def __init__(self, models: list[dict[str, Any]]) -> None:
        self._models = {m["name"]: m for m in models}

    def by_name(self, name: str) -> dict[str, Any] | None:
        return self._models.get(name)

    def all_names_and_ids(self) -> list[_NamedId]:
        return [_NamedId(name) for name in self._models]


class FakeDecks:
    """Deck dicts mirroring the legacy DeckManager surface the deck ops use:
    get/save (rename renames children, like the Rust backend), remove
    (filtered removal returns cards home), config presets, new_filtered."""

    def __init__(self, names: list[str], col: "FakeCol | None" = None) -> None:
        self._col = col
        self._decks: dict[int, dict[str, Any]] = {}
        self._next = 1
        for name in names:
            self._add(name)
        self._configs: dict[int, dict[str, Any]] = {
            1: {
                "id": 1,
                "name": "Default",
                "new": {"perDay": 20, "delays": [1.0, 10.0]},
                "rev": {"perDay": 200, "maxIvl": 36500},
            }
        }

    def _add(self, name: str, dyn: bool = False) -> int:
        did = self._next
        self._next += 1
        deck: dict[str, Any] = {"id": did, "name": name, "dyn": 1 if dyn else 0}
        if dyn:
            deck["terms"] = [["", 100, 6]]
            deck["resched"] = True
        else:
            deck["conf"] = 1
        self._decks[did] = deck
        return did

    def id_for_name(self, name: str) -> int:
        for deck in self._decks.values():
            if deck["name"] == name:
                return deck["id"]
        raise KeyError(name)

    def id(self, name: str) -> int:
        try:
            return self.id_for_name(name)
        except KeyError:
            return self._add(name)

    def name(self, did: int) -> str:
        deck = self._decks.get(did)
        return deck["name"] if deck else ""

    def get(self, did: int, default: bool = True) -> dict[str, Any] | None:
        # `default` mirrors the real DeckManager.get; the invariants pass
        # default=False and expect None for a missing id (never the Default
        # fallback). This double never fabricates a fallback deck either way.
        deck = self._decks.get(did)
        return dict(deck) if deck else None

    def save(self, deck: dict[str, Any]) -> None:
        stored = self._decks[deck["id"]]
        old, new = stored["name"], deck["name"]
        self._decks[deck["id"]] = dict(deck)
        if old != new:
            for other in self._decks.values():
                if other["name"].startswith(old + "::"):
                    other["name"] = new + other["name"][len(old):]

    def remove(self, dids: list[int]) -> None:
        for did in list(dids):
            deck = self._decks.pop(did, None)
            if deck and deck.get("dyn") and self._col is not None:
                self._col.sched._return_home(did)

    def all_names_and_ids(self) -> list[_NamedId]:
        return [_NamedId(d["name"], d["id"]) for d in self._decks.values()]

    def all(self) -> list[dict[str, Any]]:
        return [dict(d) for d in self._decks.values()]

    def config_dict_for_deck_id(self, did: int) -> dict[str, Any]:
        import copy

        return copy.deepcopy(self._configs[self._decks[did].get("conf", 1)])

    def update_config(self, conf: dict[str, Any]) -> None:
        import copy

        self._configs[conf["id"]] = copy.deepcopy(conf)

    def new_filtered(self, name: str) -> int:
        return self._add(name, dyn=True)


class FakeSched:
    def __init__(self, col: "FakeCol") -> None:
        self._col = col

    def rebuild_filtered_deck(self, did: int) -> int:
        self._return_home(did)
        deck = self._col.decks._decks[did]
        moved = 0
        for search, limit, _order in deck.get("terms", []):
            for cid in list(self._col.find_cards(search))[: int(limit)]:
                card = self._col._cards[cid]
                if card.did == did or card.odid:
                    continue
                card.odid = card.did
                card.did = did
                moved += 1
        return moved

    def empty_filtered_deck(self, did: int) -> None:
        self._return_home(did)

    def _return_home(self, did: int) -> None:
        for card in self._col._cards.values():
            if card.did == did and card.odid:
                card.did = card.odid
                card.odid = 0


class FakeTags:
    def __init__(self, col: "FakeCol") -> None:
        self._col = col

    def rename(self, old: str, new: str) -> None:
        for note in self._col._notes.values():
            if old in note.tags:
                note.tags = [new if t == old else t for t in note.tags]


class FakeDB:
    """DBProxy stand-in the postcondition invariants read through.

    scalar/list/all run REAL SQLite over a projection of the collection's
    notes/cards/col rows, rebuilt from the method-API dicts before each read so
    the invariants' SELECTs execute exactly as Anki's DBProxy would run them.
    transact(op) mirrors anki/dbproxy.py: it runs op and rolls the whole
    collection back (restoring the dict state) if op raises, so a mid-batch
    failure or a postcondition violation leaves nothing committed.
    """

    def __init__(self, col: "FakeCol") -> None:
        self._col = col

    def _fetch(self, sql: str, args: tuple) -> list:
        # A short-lived connection per read, projected from the current dicts
        # and closed immediately, so no sqlite handle outlives the query.
        conn = sqlite3.connect(":memory:")
        try:
            conn.executescript(
                "create table col (id integer primary key, scm integer);"
                "create table notes (id integer primary key, usn integer);"
                "create table cards (id integer primary key, nid integer, "
                "did integer, odid integer, usn integer);"
            )
            conn.execute("insert into col (id, scm) values (1, ?)", (self._col._scm,))
            for nid, note in self._col._notes.items():
                conn.execute(
                    "insert into notes (id, usn) values (?, ?)",
                    (int(nid), int(getattr(note, "usn", -1))),
                )
            for cid, card in self._col._cards.items():
                conn.execute(
                    "insert into cards (id, nid, did, odid, usn) values (?, ?, ?, ?, ?)",
                    (
                        int(cid),
                        int(getattr(card, "nid", 0)),
                        int(card.did),
                        int(getattr(card, "odid", 0)),
                        int(getattr(card, "usn", -1)),
                    ),
                )
            return conn.execute(sql, args).fetchall()
        finally:
            conn.close()

    def scalar(self, sql: str, *args: object) -> object:
        rows = self._fetch(sql, args)
        return rows[0][0] if rows else None

    def list(self, sql: str, *args: object) -> list:
        return [r[0] for r in self._fetch(sql, args)]

    def all(self, sql: str, *args: object) -> list:
        return [list(r) for r in self._fetch(sql, args)]

    def transact(self, op: "Any") -> None:
        snapshot = self._col._snapshot_state()
        try:
            op()
        except BaseException:
            self._col._restore_state(snapshot)
            raise


class FakeCol:
    def __init__(self) -> None:
        self.models = FakeModels([BASIC])
        self.decks = FakeDecks(["Default"], col=self)
        self.sched = FakeSched(self)
        self.tags = FakeTags(self)
        self._notes: dict[int, FakeNote] = {}
        self._cards: dict[int, FakeCard] = {}
        self._next_id = 100
        self._scm = 1000  # schema mark; no op in this suite bumps it
        self.db = FakeDB(self)
        # Undo-queue stand-in for _discard_dangling_undo (proposals.py): real
        # Anki's dangling-undo-entry wart (SAFETY.md) can't be reproduced by
        # this dict-snapshot fake (there is no separate in-memory undo queue
        # here to leave dangling), so this only counts calls to confirm
        # ProposalManager asks for exactly the right number of pops - whether
        # a real col.undo() call is itself safe is verified against real Anki
        # by the gui_smoke probe.
        self.undo_calls = 0

    def undo(self) -> None:
        self.undo_calls += 1

    # --- transaction rollback support (see FakeDB.transact) ---

    def _snapshot_state(self) -> Any:
        # One deepcopy with a shared memo keeps cross-references (a card in both
        # _cards and its note._cards) identical in the snapshot.
        return copy.deepcopy(
            (
                self._notes,
                self._cards,
                self.decks._decks,
                self.decks._configs,
                self.decks._next,
                self._next_id,
                self._scm,
            )
        )

    def _restore_state(self, snapshot: Any) -> None:
        (
            self._notes,
            self._cards,
            self.decks._decks,
            self.decks._configs,
            self.decks._next,
            self._next_id,
            self._scm,
        ) = snapshot

    def new_note(self, model: dict[str, Any]) -> FakeNote:
        return FakeNote(model)

    def add_note(self, note: FakeNote, deck_id: int) -> None:
        note.id = self._next_id
        card = FakeCard(did=deck_id, cid=self._next_id * 10)
        card.nid = note.id
        note._cards = [card]
        self._cards[card.id] = card
        self._next_id += 1
        self._notes[note.id] = note

    def note_count(self) -> int:
        return len(self._notes)

    def card_count(self) -> int:
        return len(self._cards)

    def find_cards(self, query: str) -> list[int]:
        # Supports the deck:"X" / tag:"X" shapes the deck ops and move_cards
        # tests use; anything else matches everything.
        if query.startswith('deck:"') and query.endswith('"'):
            try:
                did = self.decks.id_for_name(query[6:-1])
            except KeyError:
                return []
            return [cid for cid, c in self._cards.items() if c.did == did]
        if query.startswith('tag:"') and query.endswith('"'):
            tag = query[5:-1]
            return [
                cid
                for cid, c in self._cards.items()
                if c.nid in self._notes and tag in self._notes[c.nid].tags
            ]
        return list(self._cards)

    def get_card(self, card_id: int) -> FakeCard:
        return self._cards[card_id]

    def set_deck(self, card_ids: list[int], deck_id: int) -> None:
        # Mirror Anki: set_deck into a filtered deck hard-errors
        # (CanNotMoveCardsInto, card/mod.rs:371), and any move clears odid and
        # wipes FSRS memory via clear_fsrs_data (hazard 8).
        deck = self.decks.get(deck_id)
        if deck and deck.get("dyn"):
            raise RuntimeError("CanNotMoveCardsInto")
        for cid in card_ids:
            card = self._cards[cid]
            card.did = deck_id
            card.odid = 0
            card.memory_state = None

    def get_note(self, note_id: int) -> FakeNote:
        # Like real Anki: a fresh Note object per call (cards stay shared so
        # tests can poke reps). In-memory mutation must not hit the stored
        # note until update_note.
        stored = self._notes[note_id]
        clone = FakeNote(stored._model)
        clone._fields = dict(stored._fields)
        clone.tags = list(stored.tags)
        clone.id = note_id
        clone._cards = stored._cards
        return clone

    def update_note(self, note: FakeNote) -> None:
        # Test hook: a field value of "BOOM" simulates a backend failure, so a
        # mid-batch rollback can be exercised (F3).
        if any(value == "BOOM" for value in note._fields.values()):
            raise RuntimeError("simulated backend failure")
        stored = self._notes[note.id]
        stored._fields = dict(note._fields)
        stored.tags = list(note.tags)

    def remove_notes(self, note_ids: list[int]) -> None:
        for nid in note_ids:
            for card in self._notes[nid]._cards:
                self._cards.pop(card.id, None)
            del self._notes[nid]

    def find_notes(self, query: str) -> list[int]:
        if query.startswith('tag:"') and query.endswith('"'):
            tag = query[5:-1]
            return [nid for nid, n in self._notes.items() if tag in n.tags]
        if query == "deck:*":
            return list(self._notes)
        return []


def make_manager(
    config: dict[str, Any] | None = None,
    writes: list | None = None,
    checkpoints: list | None = None,
    checkpoint_ok: bool = True,
    observes: list | None = None,
    apply_skill=None,
):
    col = FakeCol()
    pushed: list[dict[str, Any]] = []

    def checkpoint(reason: str, critical: bool) -> bool:
        if checkpoints is not None:
            checkpoints.append((reason, critical))
        return checkpoint_ok

    manager = ProposalManager(
        get_col=lambda: col,
        push=pushed.append,
        config=config if config is not None else {},
        after_write=(writes.append if writes is not None else None),
        checkpoint=checkpoint,
        observe=(observes.append if observes is not None else None),
        apply_skill=apply_skill,
    )
    return manager, col, pushed


def pushes_of(pushed: list[dict[str, Any]], type_: str) -> list[dict[str, Any]]:
    return [p for p in pushed if p["type"] == type_]


CREATE_ARGS = {
    "note_type": "Basic",
    "deck": "Default",
    "tags": ["analysis"],
    "fields": {"Front": "Q?", "Back": "A."},
    "rationale": "test",
}


class CreateFlowTests(unittest.TestCase):
    def test_submit_then_accept_creates_tagged_note(self) -> None:
        manager, col, pushed = make_manager()
        result = manager.submit_create(dict(CREATE_ARGS))
        self.assertEqual(result["status"], "pending_user_review")
        (proposal,) = pushes_of(pushed, "proposal")
        self.assertEqual(proposal["proposal"]["status"], "pending")
        self.assertEqual(
            [f["name"] for f in proposal["proposal"]["fields"]], ["Front", "Back"]
        )

        manager.accept({"id": result["proposal_id"]})
        (resolved,) = pushes_of(pushed, "proposal_resolved")
        self.assertEqual(resolved["status"], "accepted")
        note = col.get_note(resolved["note_id"])
        self.assertEqual(note["Front"], "Q?")
        self.assertIn(AI_TAG, note.tags)
        self.assertIn(manager.session_tag, note.tags)
        self.assertIn("analysis", note.tags)
        ledger = pushes_of(pushed, "ledger")[-1]
        self.assertEqual(len(ledger["entries"]), 1)

    def test_user_edits_before_accept_win(self) -> None:
        manager, col, pushed = make_manager()
        result = manager.submit_create(dict(CREATE_ARGS))
        manager.accept(
            {
                "id": result["proposal_id"],
                "fields": {"Front": "Edited Q?", "Back": "A."},
                "deck": "Other",
                "tags": ["mine"],
            }
        )
        (resolved,) = pushes_of(pushed, "proposal_resolved")
        note = col.get_note(resolved["note_id"])
        self.assertEqual(note["Front"], "Edited Q?")
        self.assertIn("mine", note.tags)
        self.assertEqual(col.decks.name(note.cards()[0].did), "Other")

    def test_shared_review_saves_revision_before_exact_accept(self) -> None:
        manager, col, pushed = make_manager()
        result = manager.submit_create(dict(CREATE_ARGS))
        proposal_id = result["proposal_id"]
        original = pushes_of(pushed, "proposal")[-1]["proposal"]
        self.assertEqual(original["revision"], 1)
        self.assertEqual(len(original["operation_digest"]), 64)

        manager.revise(
            {
                "id": proposal_id,
                "expected_revision": 1,
                "fields": {"Front": "Edited Q?", "Back": "Edited A."},
            }
        )
        revised = pushes_of(pushed, "proposal")[-1]["proposal"]
        self.assertEqual(revised["revision"], 2)
        self.assertNotEqual(revised["operation_digest"], original["operation_digest"])

        manager.accept({"id": proposal_id, "revision": 1})
        self.assertEqual(col._notes, {})
        self.assertIn("stale proposal revision", pushes_of(pushed, "proposal_error")[-1]["message"])

        manager.accept({"id": proposal_id, "revision": 2})
        resolved = pushes_of(pushed, "proposal_resolved")[-1]
        note = col.get_note(resolved["note_id"])
        self.assertEqual(note["Front"], "Edited Q?")
        self.assertEqual(note["Back"], "Edited A.")

    def test_reject_and_double_decision_ignored(self) -> None:
        manager, col, pushed = make_manager()
        result = manager.submit_create(dict(CREATE_ARGS))
        manager.reject({"id": result["proposal_id"]})
        manager.accept({"id": result["proposal_id"]})  # already rejected: no-op
        (resolved,) = pushes_of(pushed, "proposal_resolved")
        self.assertEqual(resolved["status"], "rejected")
        self.assertEqual(col._notes, {})

    def test_validation_errors(self) -> None:
        manager, _col, _pushed = make_manager()
        with self.assertRaises(ProposalError):
            manager.submit_create({**CREATE_ARGS, "note_type": "Nope"})
        with self.assertRaises(ProposalError):
            manager.submit_create({**CREATE_ARGS, "fields": {"Bogus": "x"}})
        with self.assertRaises(ProposalError):
            manager.submit_create({**CREATE_ARGS, "fields": {"Front": " ", "Back": "x"}})

    def test_missing_deck_warns_but_proceeds(self) -> None:
        manager, col, pushed = make_manager()
        result = manager.submit_create({**CREATE_ARGS, "deck": "Brand::New"})
        self.assertTrue(any("created" in w for w in result["warnings"]))
        manager.accept({"id": result["proposal_id"]})
        (resolved,) = pushes_of(pushed, "proposal_resolved")
        note = col.get_note(resolved["note_id"])
        self.assertEqual(col.decks.name(note.cards()[0].did), "Brand::New")

    def test_filtered_deck_is_rejected_as_home_deck(self) -> None:
        manager, col, _pushed = make_manager()
        col.decks.new_filtered("Cram")
        with self.assertRaisesRegex(ProposalError, "filtered deck"):
            manager.submit_create({**CREATE_ARGS, "deck": "Cram"})

    def test_filtered_deck_edited_in_before_accept_is_rejected(self) -> None:
        manager, col, pushed = make_manager()
        col.decks.new_filtered("Cram")
        result = manager.submit_create(dict(CREATE_ARGS))
        manager.accept({"id": result["proposal_id"], "deck": "Cram"})
        error = pushes_of(pushed, "proposal_error")[-1]
        self.assertIn("filtered deck", error["message"])
        self.assertEqual(0, col.note_count())


class PinsTests(unittest.TestCase):
    def test_pins_are_constraints(self) -> None:
        config = {
            "pins": {
                "deck": "Pinned::Deck",
                "note_type": "Basic",
                "tags": ["pinned-tag"],
                "fields": {"Back": "pinned back"},
            }
        }
        manager, _col, pushed = make_manager(config)
        result = manager.submit_create(
            {"deck": "Elsewhere", "fields": {"Front": "Q?"}, "tags": []}
        )
        (proposal,) = pushes_of(pushed, "proposal")
        payload = proposal["proposal"]
        self.assertEqual(payload["deck"], "Pinned::Deck")
        self.assertIn("pinned-tag", payload["tags"])
        back = next(f for f in payload["fields"] if f["name"] == "Back")
        self.assertEqual(back["new"], "pinned back")
        self.assertTrue(any("pin" in w for w in result["warnings"]))

    def test_pinned_note_type_rejects_other_types(self) -> None:
        manager, _col, _pushed = make_manager({"pins": {"note_type": "Basic"}})
        with self.assertRaises(ProposalError):
            manager.submit_create({**CREATE_ARGS, "note_type": "Cloze"})


class AutoAcceptTests(unittest.TestCase):
    def test_auto_accept_applies_and_caps(self) -> None:
        config = {"permission_mode": "auto-accept", "auto_accept_cap": 2}
        manager, col, pushed = make_manager(config)
        first = manager.submit_create(dict(CREATE_ARGS))
        second = manager.submit_create(dict(CREATE_ARGS))
        self.assertEqual(first["status"], "created")
        self.assertTrue(second["auto_accepted"])
        self.assertEqual(len(col._notes), 2)

        third = manager.submit_create(dict(CREATE_ARGS))
        self.assertEqual(third["status"], "pending_user_review")
        self.assertEqual(len(col._notes), 2)
        self.assertTrue(pushes_of(pushed, "notice"))

    def test_auto_accept_never_applies_edits(self) -> None:
        config = {"permission_mode": "auto-accept", "auto_accept_cap": 5}
        manager, col, _pushed = make_manager(config)
        created = manager.submit_create(dict(CREATE_ARGS))
        result = manager.submit_edit(
            {"note_id": created["note_id"], "field_changes": {"Front": "New Q?"}}
        )
        self.assertEqual(result["status"], "pending_user_review")
        self.assertEqual(col.get_note(created["note_id"])["Front"], "Q?")


class EditFlowTests(unittest.TestCase):
    def _created_note(self, manager, col, pushed) -> int:
        result = manager.submit_create(dict(CREATE_ARGS))
        manager.accept({"id": result["proposal_id"]})
        return pushes_of(pushed, "proposal_resolved")[-1]["note_id"]

    def test_noop_changes_rejected(self) -> None:
        manager, col, pushed = make_manager()
        nid = self._created_note(manager, col, pushed)
        with self.assertRaises(ProposalError):
            manager.submit_edit({"note_id": nid, "field_changes": {"Front": "Q?"}})

    def test_edit_accept_applies_selected_fields_and_tags(self) -> None:
        manager, col, pushed = make_manager()
        nid = self._created_note(manager, col, pushed)
        result = manager.submit_edit(
            {
                "note_id": nid,
                "field_changes": {"Front": "New Q?", "Back": "New A."},
                "add_tags": ["extra"],
                "remove_tags": ["analysis"],
            }
        )
        manager.accept(
            {
                "id": result["proposal_id"],
                "fields": {"Front": "New Q?", "Back": "New A."},
                "accepted_fields": ["Front"],  # per-field acceptance
            }
        )
        note = col.get_note(nid)
        self.assertEqual(note["Front"], "New Q?")
        self.assertEqual(note["Back"], "A.")  # not accepted
        self.assertIn("extra", note.tags)
        self.assertNotIn("analysis", note.tags)

    def test_edit_accepts_when_existing_cards_already_synced(self) -> None:
        # Regression (dogfood 2026-07-11): a field edit stamps the NOTE
        # usn=-1 via update_note but never re-stamps the note's existing
        # cards. A card already synced to the server (usn != -1) must not trip
        # touched_rows_pending_sync — the note carries the change, so it still
        # reaches other devices. Before the written-rows fix this raised
        # "touched card(s) not pending sync" and blocked every real edit.
        manager, col, pushed = make_manager()
        nid = self._created_note(manager, col, pushed)
        for card in col.get_note(nid).cards():
            card.usn = 5  # already synced to a server USN, not -1
        result = manager.submit_edit(
            {"note_id": nid, "field_changes": {"Front": "Reworded?"}}
        )
        manager.accept(
            {
                "id": result["proposal_id"],
                "fields": {"Front": "Reworded?"},
                "accepted_fields": ["Front"],
            }
        )
        self.assertFalse(pushes_of(pushed, "proposal_error"))
        self.assertEqual(col.get_note(nid)["Front"], "Reworded?")

    def test_staleness_guard_blocks_and_refreshes(self) -> None:
        manager, col, pushed = make_manager()
        nid = self._created_note(manager, col, pushed)
        result = manager.submit_edit(
            {"note_id": nid, "field_changes": {"Front": "New Q?"}}
        )
        # The note changes underneath (user edit / sync) before acceptance.
        changed = col.get_note(nid)
        changed["Front"] = "Changed elsewhere"
        col.update_note(changed)
        before = len(col._notes)
        manager.accept({"id": result["proposal_id"]})

        errors = pushes_of(pushed, "proposal_error")
        self.assertTrue(errors and "re-review" in errors[0]["message"])
        refreshed = pushes_of(pushed, "proposal")[-1]["proposal"]
        self.assertEqual(refreshed["status"], "pending")
        front = next(f for f in refreshed["fields"] if f["name"] == "Front")
        self.assertEqual(front["old"], "Changed elsewhere")
        self.assertEqual(col.get_note(nid)["Front"], "Changed elsewhere")
        self.assertEqual(len(col._notes), before)

        # Second accept against the refreshed baseline applies cleanly.
        manager.accept({"id": result["proposal_id"]})
        self.assertEqual(col.get_note(nid)["Front"], "New Q?")


class LedgerTests(unittest.TestCase):
    def test_revert_edit_restores_prior_values(self) -> None:
        manager, col, pushed = make_manager()
        create = manager.submit_create(dict(CREATE_ARGS))
        manager.accept({"id": create["proposal_id"]})
        nid = pushes_of(pushed, "proposal_resolved")[-1]["note_id"]
        edit = manager.submit_edit(
            {"note_id": nid, "field_changes": {"Front": "New Q?"}}
        )
        manager.accept({"id": edit["proposal_id"]})
        self.assertEqual(col.get_note(nid)["Front"], "New Q?")

        manager.revert({"id": edit["proposal_id"]})
        self.assertEqual(col.get_note(nid)["Front"], "Q?")
        undone = pushes_of(pushed, "proposal_resolved")[-1]
        self.assertEqual(undone["status"], "undone")

    def test_revert_create_deletes_unstudied_note_only(self) -> None:
        manager, col, pushed = make_manager()
        create = manager.submit_create(dict(CREATE_ARGS))
        manager.accept({"id": create["proposal_id"]})
        nid = pushes_of(pushed, "proposal_resolved")[-1]["note_id"]

        col.get_note(nid).cards()[0].reps = 3  # studied: refuse deletion
        manager.revert({"id": create["proposal_id"]})
        self.assertIn(nid, col._notes)
        self.assertTrue(any("studied" in n["text"] for n in pushes_of(pushed, "notice")))

        col.get_note(nid).cards()[0].reps = 0
        manager.revert({"id": create["proposal_id"]})
        self.assertNotIn(nid, col._notes)

    def test_undo_session_reverts_everything(self) -> None:
        manager, col, pushed = make_manager()
        for _ in range(2):
            result = manager.submit_create(dict(CREATE_ARGS))
            manager.accept({"id": result["proposal_id"]})
        self.assertEqual(len(col._notes), 2)
        manager.undo_session()
        self.assertEqual(len(col._notes), 0)
        ledger = pushes_of(pushed, "ledger")[-1]
        self.assertTrue(all(e["undone"] for e in ledger["entries"]))

    def test_new_session_resets_ledger_and_tag(self) -> None:
        manager, col, pushed = make_manager()
        result = manager.submit_create(dict(CREATE_ARGS))
        manager.accept({"id": result["proposal_id"]})
        old_tag = manager.session_tag
        manager.new_session()
        self.assertNotEqual(manager.session_tag, old_tag)
        manager.push_ui_state()
        ledger = pushes_of(pushed, "ledger")[-1]
        self.assertEqual(ledger["entries"], [])


class TagConfigTests(unittest.TestCase):
    def test_custom_created_tag(self) -> None:
        manager, col, pushed = make_manager({"created_tag": "robot"})
        result = manager.submit_create(dict(CREATE_ARGS))
        manager.accept({"id": result["proposal_id"]})
        note = col.get_note(pushes_of(pushed, "proposal_resolved")[-1]["note_id"])
        self.assertIn("robot", note.tags)
        self.assertNotIn(AI_TAG, note.tags)

    def test_no_tags_when_all_disabled(self) -> None:
        config = {"created_tag": "", "session_tag_prefix": ""}
        manager, col, pushed = make_manager(config)
        result = manager.submit_create({**CREATE_ARGS, "tags": []})
        manager.accept({"id": result["proposal_id"]})
        note = col.get_note(pushes_of(pushed, "proposal_resolved")[-1]["note_id"])
        self.assertEqual([], note.tags)
        self.assertEqual("", manager.session_tag)

    def test_edited_tag_stamped_on_accepted_edit(self) -> None:
        manager, col, pushed = make_manager({"edited_tag": "touched"})
        create = manager.submit_create(dict(CREATE_ARGS))
        manager.accept({"id": create["proposal_id"]})
        nid = pushes_of(pushed, "proposal_resolved")[-1]["note_id"]
        edit = manager.submit_edit(
            {"note_id": nid, "field_changes": {"Front": "New Q?"}}
        )
        manager.accept({"id": edit["proposal_id"]})
        self.assertIn("touched", col.get_note(nid).tags)

    def test_edited_tag_off_when_empty(self) -> None:
        manager, col, pushed = make_manager({"edited_tag": ""})
        create = manager.submit_create(dict(CREATE_ARGS))
        manager.accept({"id": create["proposal_id"]})
        nid = pushes_of(pushed, "proposal_resolved")[-1]["note_id"]
        edit = manager.submit_edit(
            {"note_id": nid, "field_changes": {"Front": "New Q?"}}
        )
        manager.accept({"id": edit["proposal_id"]})
        self.assertNotIn("ai-edited", col.get_note(nid).tags)


class SupersedeTests(unittest.TestCase):
    def test_supersede_deactivates_prior_pending(self) -> None:
        manager, _col, pushed = make_manager()
        first = manager.submit_create(dict(CREATE_ARGS))
        manager.submit_create({**CREATE_ARGS, "supersedes": first["proposal_id"]})
        superseded = [
            p for p in pushes_of(pushed, "proposal_resolved") if p["status"] == "superseded"
        ]
        self.assertEqual(1, len(superseded))
        self.assertEqual(first["proposal_id"], superseded[0]["id"])

    def test_supersede_ignores_already_resolved(self) -> None:
        manager, _col, pushed = make_manager()
        first = manager.submit_create(dict(CREATE_ARGS))
        manager.accept({"id": first["proposal_id"]})
        before = len(pushes_of(pushed, "proposal_resolved"))
        manager.submit_create({**CREATE_ARGS, "supersedes": first["proposal_id"]})
        # No new "superseded" resolution for an already-accepted proposal.
        superseded = [
            p for p in pushes_of(pushed, "proposal_resolved") if p["status"] == "superseded"
        ]
        self.assertEqual([], superseded)
        self.assertGreaterEqual(len(pushes_of(pushed, "proposal_resolved")), before)

    def test_public_supersede_sets_aside_pending(self) -> None:
        # The "Suggest change" + send flow calls this directly (bridge
        # proposal_supersede) to set the revised-away card aside.
        manager, _col, pushed = make_manager()
        first = manager.submit_create(dict(CREATE_ARGS))
        manager.supersede({"id": first["proposal_id"]})
        superseded = [
            p for p in pushes_of(pushed, "proposal_resolved") if p["status"] == "superseded"
        ]
        self.assertEqual([first["proposal_id"]], [p["id"] for p in superseded])

    def test_public_supersede_noop_when_resolved(self) -> None:
        manager, _col, pushed = make_manager()
        first = manager.submit_create(dict(CREATE_ARGS))
        manager.reject({"id": first["proposal_id"]})
        manager.supersede({"id": first["proposal_id"]})
        superseded = [
            p for p in pushes_of(pushed, "proposal_resolved") if p["status"] == "superseded"
        ]
        self.assertEqual([], superseded)


class RestoreTests(unittest.TestCase):
    def test_restore_superseded_returns_to_pending(self) -> None:
        manager, _col, pushed = make_manager()
        first = manager.submit_create(dict(CREATE_ARGS))
        manager.submit_create({**CREATE_ARGS, "supersedes": first["proposal_id"]})
        manager.restore({"id": first["proposal_id"]})
        restored = pushes_of(pushed, "proposal")[-1]["proposal"]
        self.assertEqual(first["proposal_id"], restored["id"])
        self.assertEqual("pending", restored["status"])
        # And it can then be accepted normally.
        manager.accept({"id": first["proposal_id"]})
        self.assertEqual(
            "accepted", pushes_of(pushed, "proposal_resolved")[-1]["status"]
        )

    def test_restore_rejected_returns_to_pending(self) -> None:
        manager, _col, pushed = make_manager()
        first = manager.submit_create(dict(CREATE_ARGS))
        manager.reject({"id": first["proposal_id"]})
        manager.restore({"id": first["proposal_id"]})
        self.assertEqual(
            "pending", pushes_of(pushed, "proposal")[-1]["proposal"]["status"]
        )


class ReaddTests(unittest.TestCase):
    def _create_then_undo(self, manager, col, pushed) -> str:
        result = manager.submit_create(dict(CREATE_ARGS))
        manager.accept({"id": result["proposal_id"]})
        manager.revert({"id": result["proposal_id"]})
        return result["proposal_id"]

    def test_readd_recreates_undone_note(self) -> None:
        manager, col, pushed = make_manager()
        pid = self._create_then_undo(manager, col, pushed)
        self.assertEqual(0, len(col._notes))
        manager.readd({"id": pid})
        self.assertEqual(1, len(col._notes))
        note = next(iter(col._notes.values()))
        self.assertIn(AI_TAG, note.tags)
        self.assertEqual(
            "accepted", pushes_of(pushed, "proposal_resolved")[-1]["status"]
        )

    def test_readd_reapplies_undone_edit(self) -> None:
        manager, col, pushed = make_manager()
        create = manager.submit_create(dict(CREATE_ARGS))
        manager.accept({"id": create["proposal_id"]})
        nid = pushes_of(pushed, "proposal_resolved")[-1]["note_id"]
        edit = manager.submit_edit(
            {"note_id": nid, "field_changes": {"Front": "New Q?"}}
        )
        manager.accept({"id": edit["proposal_id"]})
        manager.revert({"id": edit["proposal_id"]})
        self.assertEqual(col.get_note(nid)["Front"], "Q?")

        manager.readd({"id": edit["proposal_id"]})
        self.assertEqual(col.get_note(nid)["Front"], "New Q?")
        # Re-adding an edit is itself revertible again.
        manager.revert({"id": edit["proposal_id"]})
        self.assertEqual(col.get_note(nid)["Front"], "Q?")

    def test_readd_only_acts_on_undone(self) -> None:
        manager, col, pushed = make_manager()
        result = manager.submit_create(dict(CREATE_ARGS))  # still pending
        manager.readd({"id": result["proposal_id"]})
        self.assertEqual(0, len(col._notes))


class AfterWriteTests(unittest.TestCase):
    def test_after_write_fires_on_create_and_revert(self) -> None:
        writes: list = []
        manager, _col, pushed = make_manager(writes=writes)
        result = manager.submit_create(dict(CREATE_ARGS))
        manager.accept({"id": result["proposal_id"]})
        nid = pushes_of(pushed, "proposal_resolved")[-1]["note_id"]
        self.assertIn(nid, writes[-1])
        writes.clear()
        manager.revert({"id": result["proposal_id"]})
        self.assertIn(nid, writes[-1])

    def test_preview_request_pushes_update(self) -> None:
        manager, _col, pushed = make_manager()
        result = manager.submit_create(dict(CREATE_ARGS))
        manager.preview_request(
            {"id": result["proposal_id"], "fields": {"Front": "Live edit"}}
        )
        updates = pushes_of(pushed, "preview_update")
        self.assertEqual(1, len(updates))
        self.assertEqual(result["proposal_id"], updates[0]["id"])


def _seed_notes(manager, col, pushed, n=3, tag="analysis"):
    ids = []
    for i in range(n):
        result = manager.submit_create(
            {**CREATE_ARGS, "fields": {"Front": f"Q{i}?", "Back": f"A{i}."},
             "tags": [tag]}
        )
        if "proposal_id" in result:  # gated mode: accept manually
            manager.accept({"id": result["proposal_id"]})
            ids.append(pushes_of(pushed, "proposal_resolved")[-1]["note_id"])
        else:  # trusted/auto mode: applied directly
            ids.append(result["note_id"])
    return ids


class BulkOpsTests(unittest.TestCase):
    def test_rename_tag_accept_and_revert(self) -> None:
        checkpoints: list = []
        manager, col, pushed = make_manager(checkpoints=checkpoints)
        _seed_notes(manager, col, pushed)
        result = manager.submit_rename_tag(
            {"old_tag": "analysis", "new_tag": "math"}
        )
        self.assertEqual("pending_user_review", result["status"])
        self.assertEqual(3, result["affected"])
        manager.accept({"id": result["proposal_id"]})
        # A tag rename is ledger-revertible, so its backup is non-critical.
        self.assertTrue(checkpoints)  # backup before applying
        self.assertEqual((False,), (checkpoints[-1][1],))
        self.assertEqual(3, len(col.find_notes('tag:"math"')))
        self.assertEqual(0, len(col.find_notes('tag:"analysis"')))
        manager.revert({"id": result["proposal_id"]})
        self.assertEqual(3, len(col.find_notes('tag:"analysis"')))

    def test_rename_missing_tag_rejected(self) -> None:
        manager, _col, _pushed = make_manager()
        with self.assertRaises(ProposalError):
            manager.submit_rename_tag({"old_tag": "nope", "new_tag": "x"})

    def test_find_replace_apply_and_revert(self) -> None:
        manager, col, pushed = make_manager()
        ids = _seed_notes(manager, col, pushed)
        result = manager.submit_find_replace(
            {"search": "Q1", "replacement": "QUESTION-1"}
        )
        self.assertEqual(1, result["affected"])
        payload = pushes_of(pushed, "proposal")[-1]["proposal"]
        self.assertEqual("bulk", payload["kind"])
        self.assertTrue(payload["samples"])
        manager.accept({"id": result["proposal_id"]})
        self.assertEqual("QUESTION-1?", col.get_note(ids[1])["Front"])
        manager.revert({"id": result["proposal_id"]})
        self.assertEqual("Q1?", col.get_note(ids[1])["Front"])

    def test_find_replace_skips_stale_notes(self) -> None:
        manager, col, pushed = make_manager()
        ids = _seed_notes(manager, col, pushed)
        result = manager.submit_find_replace({"search": "A", "replacement": "B"})
        # One note changes underneath before acceptance.
        changed = col.get_note(ids[0])
        changed["Back"] = "mutated"
        col.update_note(changed)
        manager.accept({"id": result["proposal_id"]})
        resolved = pushes_of(pushed, "proposal_resolved")[-1]
        self.assertTrue(any("skipped" in w for w in resolved.get("warnings", [])))
        self.assertEqual("mutated", col.get_note(ids[0])["Back"])  # untouched
        self.assertEqual("B1.", col.get_note(ids[1])["Back"])  # applied

    def test_move_cards_apply_and_revert(self) -> None:
        manager, col, pushed = make_manager()
        _seed_notes(manager, col, pushed)
        result = manager.submit_move_cards(
            {"query": 'deck:"Default"', "deck": "Archive"}
        )
        self.assertEqual(3, result["affected"])
        manager.accept({"id": result["proposal_id"]})
        archive_id = col.decks.id("Archive")
        self.assertEqual(3, len([c for c in col._cards.values() if c.did == archive_id]))
        manager.revert({"id": result["proposal_id"]})
        default_id = col.decks.id("Default")
        self.assertEqual(3, len([c for c in col._cards.values() if c.did == default_id]))

    def test_move_cards_rejects_filtered_destination(self) -> None:
        manager, col, pushed = make_manager()
        _seed_notes(manager, col, pushed)
        col.decks.new_filtered("Cram")
        with self.assertRaisesRegex(ProposalError, "filtered deck"):
            manager.submit_move_cards(
                {"query": 'deck:"Default"', "deck": "Cram"}
            )


class DeleteTests(unittest.TestCase):
    def test_delete_gated_even_in_trusted_mode(self) -> None:
        checkpoints: list = []
        manager, col, pushed = make_manager(
            {"permission_mode": "trusted-writes"}, checkpoints=checkpoints
        )
        ids = _seed_notes(manager, col, pushed)
        result = manager.submit_delete_notes({"note_ids": ids[:2]})
        self.assertEqual("pending_user_review", result["status"])
        self.assertEqual(3, len(col._notes))  # nothing deleted yet

        manager.accept({"id": result["proposal_id"]})
        self.assertEqual(1, len(col._notes))
        # Delete is irreversible -> its checkpoint must be critical (sync).
        self.assertTrue(checkpoints)
        self.assertTrue(checkpoints[-1][1], "delete checkpoint should be critical")
        resolved = pushes_of(pushed, "proposal_resolved")[-1]
        self.assertFalse(resolved["revertible"])
        # Ledger revert refuses.
        manager.revert({"id": result["proposal_id"]})
        self.assertEqual(1, len(col._notes))
        self.assertTrue(
            any("backup" in n["text"] for n in pushes_of(pushed, "notice"))
        )

    def test_delete_aborts_when_backup_checkpoint_fails(self) -> None:
        # A critical (delete) checkpoint failure must ABORT before anything is
        # touched - a destructive op must never proceed without a safety net.
        checkpoints: list = []
        manager, col, pushed = make_manager(
            checkpoints=checkpoints, checkpoint_ok=False
        )
        ids = _seed_notes(manager, col, pushed, n=2)
        ledger_mark = len(manager._ledger)
        result = manager.submit_delete_notes({"note_ids": ids})
        manager.accept({"id": result["proposal_id"]})

        self.assertEqual(2, len(col._notes))  # nothing deleted
        errors = pushes_of(pushed, "proposal_error")
        self.assertTrue(errors)
        self.assertIn("backup failed", errors[-1]["message"])
        self.assertIn("not performed", errors[-1]["message"])
        proposal = manager._proposals[result["proposal_id"]]
        self.assertEqual("pending", proposal.status)  # left for retry, not lost
        self.assertEqual(ledger_mark, len(manager._ledger))  # no phantom entry


class BackupWarningTests(unittest.TestCase):
    """Non-critical checkpoint failures (bulk/change-set/deck-options: all
    ledger-revertible) must not block the write - only surface a warning on
    the resolved proposal (Job 2's backup-abort contract, the non-critical
    half; DeleteTests covers the critical/abort half)."""

    def test_bulk_rename_tag_warns_but_still_applies(self) -> None:
        manager, col, pushed = make_manager(checkpoint_ok=False)
        _seed_notes(manager, col, pushed)
        result = manager.submit_rename_tag({"old_tag": "analysis", "new_tag": "math"})
        manager.accept({"id": result["proposal_id"]})

        self.assertEqual(3, len(col.find_notes('tag:"math"')))  # still applied
        resolved = pushes_of(pushed, "proposal_resolved")[-1]
        self.assertTrue(
            any("backup checkpoint failed" in w for w in resolved["warnings"])
        )

    def test_change_set_warns_but_still_applies(self) -> None:
        manager, col, pushed = make_manager(checkpoint_ok=False)
        ids = _seed_notes(manager, col, pushed, n=2)
        cs = manager.open_change_set({"title": "Tidy"})
        manager.add_to_change_set(
            {"change_set_id": cs["change_set_id"], "note_id": ids[0],
             "field_changes": {"Back": "tidy"}}
        )
        manager.close_change_set({"change_set_id": cs["change_set_id"]})
        manager.accept({"id": cs["change_set_id"]})

        self.assertEqual("tidy", col.get_note(ids[0])["Back"])  # still applied
        resolved = pushes_of(pushed, "proposal_resolved")[-1]
        self.assertTrue(
            any("backup checkpoint failed" in w for w in resolved["warnings"])
        )

    def test_deck_options_warns_but_still_applies(self) -> None:
        manager, col, pushed = make_manager(checkpoint_ok=False)
        result = manager.submit_set_deck_options(
            {"deck": "Default", "options": {"new.perDay": 30}}
        )
        manager.accept({"id": result["proposal_id"]})

        conf = col.decks.config_dict_for_deck_id(col.decks.id("Default"))
        self.assertEqual(30, conf["new"]["perDay"])  # still applied
        resolved = pushes_of(pushed, "proposal_resolved")[-1]
        self.assertTrue(
            any("backup checkpoint failed" in w for w in resolved["warnings"])
        )


class ChangeSetTests(unittest.TestCase):
    def _open_with_edits(self, manager, col, pushed, ids):
        cs = manager.open_change_set({"title": "Tidy answers"})
        cs_id = cs["change_set_id"]
        for i, nid in enumerate(ids):
            manager.add_to_change_set(
                {
                    "change_set_id": cs_id,
                    "note_id": nid,
                    "field_changes": {"Back": f"tidy {i}"},
                }
            )
        return cs_id

    def test_full_flow_apply_and_revert(self) -> None:
        checkpoints: list = []
        manager, col, pushed = make_manager(checkpoints=checkpoints)
        ids = _seed_notes(manager, col, pushed)
        cs_id = self._open_with_edits(manager, col, pushed, ids)

        # Accept while still open is refused.
        manager.accept({"id": cs_id})
        self.assertTrue(pushes_of(pushed, "proposal_error"))

        result = manager.close_change_set({"change_set_id": cs_id, "summary": "s"})
        self.assertEqual("pending_user_review", result["status"])
        payload = pushes_of(pushed, "proposal")[-1]["proposal"]
        self.assertEqual("change_set", payload["kind"])
        self.assertFalse(payload["open"])
        self.assertEqual(3, payload["count"])
        self.assertTrue(payload["samples"])

        manager.accept({"id": cs_id})
        self.assertTrue(checkpoints)
        for i, nid in enumerate(ids):
            self.assertEqual(f"tidy {i}", col.get_note(nid)["Back"])
        entries = pushes_of(pushed, "ledger")[-1]["entries"]
        self.assertEqual(1, len([e for e in entries if e["kind"] == "change_set"]))

        manager.revert({"id": cs_id})
        for i, nid in enumerate(ids):
            self.assertEqual(f"A{i}.", col.get_note(nid)["Back"])

    def test_stale_item_skipped_and_reported(self) -> None:
        manager, col, pushed = make_manager()
        ids = _seed_notes(manager, col, pushed)
        cs_id = self._open_with_edits(manager, col, pushed, ids)
        manager.close_change_set({"change_set_id": cs_id})
        mutated = col.get_note(ids[0])
        mutated["Back"] = "user edit"
        col.update_note(mutated)
        manager.accept({"id": cs_id})
        resolved = pushes_of(pushed, "proposal_resolved")[-1]
        self.assertTrue(any("skipped" in w for w in resolved["warnings"]))
        self.assertEqual("user edit", col.get_note(ids[0])["Back"])
        self.assertEqual("tidy 1", col.get_note(ids[1])["Back"])

    def test_empty_close_discards(self) -> None:
        manager, _col, pushed = make_manager()
        cs = manager.open_change_set({"title": "empty"})
        result = manager.close_change_set({"change_set_id": cs["change_set_id"]})
        self.assertEqual("discarded", result["status"])


class TrustedWritesTests(unittest.TestCase):
    def test_create_and_edit_apply_directly(self) -> None:
        manager, col, pushed = make_manager({"permission_mode": "trusted-writes"})
        result = manager.submit_create(dict(CREATE_ARGS))
        self.assertEqual("created", result["status"])
        nid = result["note_id"]
        self.assertIn(nid, col._notes)

        edit = manager.submit_edit(
            {"note_id": nid, "field_changes": {"Front": "direct"}}
        )
        self.assertEqual("applied", edit["status"])
        self.assertEqual("direct", col.get_note(nid)["Front"])

    def test_bulk_applies_directly_with_checkpoint(self) -> None:
        checkpoints: list = []
        manager, col, pushed = make_manager(
            {"permission_mode": "trusted-writes"}, checkpoints=checkpoints
        )
        _seed_notes(manager, col, pushed)
        result = manager.submit_rename_tag({"old_tag": "analysis", "new_tag": "x"})
        self.assertEqual("applied", result["status"])
        self.assertEqual(3, len(col.find_notes('tag:"x"')))
        self.assertTrue(checkpoints)

    def test_budget_pauses_to_gated_proposals(self) -> None:
        manager, col, pushed = make_manager(
            {"permission_mode": "trusted-writes", "write_budget": 4}
        )
        ids = _seed_notes(manager, col, pushed)  # 3 writes consumed
        edit1 = manager.submit_edit(
            {"note_id": ids[0], "field_changes": {"Front": "e1"}}
        )
        self.assertEqual("applied", edit1["status"])  # 4th write fits
        edit2 = manager.submit_edit(
            {"note_id": ids[1], "field_changes": {"Front": "e2"}}
        )
        self.assertEqual("pending_user_review", edit2["status"])  # budget hit
        self.assertEqual("Q1?", col.get_note(ids[1])["Front"])  # not applied
        self.assertTrue(
            any("budget" in n["text"].lower() for n in pushes_of(pushed, "notice"))
        )
        # Manual accept still works after the pause.
        manager.accept({"id": edit2["proposal_id"]})
        self.assertEqual("e2", col.get_note(ids[1])["Front"])

    def test_change_set_direct_apply_in_trusted(self) -> None:
        manager, col, pushed = make_manager({"permission_mode": "trusted-writes"})
        ids = _seed_notes(manager, col, pushed)
        cs = manager.open_change_set({"title": "t"})
        manager.add_to_change_set(
            {
                "change_set_id": cs["change_set_id"],
                "note_id": ids[0],
                "field_changes": {"Back": "swept"},
            }
        )
        result = manager.close_change_set({"change_set_id": cs["change_set_id"]})
        self.assertEqual("applied", result["status"])
        self.assertEqual("swept", col.get_note(ids[0])["Back"])


class LearningObserveTests(unittest.TestCase):
    """The capture hooks the learning store hangs off (DESIGN.md 15)."""

    def test_create_accept_emits_applied_and_reviewed_diff(self) -> None:
        observes: list[dict[str, Any]] = []
        manager, col, _ = make_manager(observes=observes)
        result = manager.submit_create(dict(CREATE_ARGS))
        manager.accept(
            {
                "id": result["proposal_id"],
                "fields": {"Front": "Q?", "Back": "Shorter."},
                "tags": ["analysis", "verified"],
            }
        )
        (applied,) = [e for e in observes if e["event"] == "applied"]
        self.assertEqual(1, len(applied["note_ids"]))
        (reviewed,) = [e for e in observes if e["event"] == "reviewed"]
        self.assertEqual("create", reviewed["proposal_kind"])
        self.assertEqual("A.", reviewed["fields_before"]["Back"])
        self.assertEqual("Shorter.", reviewed["fields_after"]["Back"])
        self.assertIn("verified", reviewed["tags_after"])

    def test_verbatim_direct_accept_produces_empty_diff(self) -> None:
        observes: list[dict[str, Any]] = []
        manager, _, _ = make_manager(
            config={"permission_mode": "trusted-writes"}, observes=observes
        )
        manager.submit_create(dict(CREATE_ARGS))
        reviewed = [e for e in observes if e["event"] == "reviewed"]
        # Trusted-writes applies without user review: any reviewed event must
        # carry no differences (the store filters it to nothing).
        for event in reviewed:
            self.assertEqual(event["fields_before"], event["fields_after"])

    def test_edit_accept_reports_declined_fields(self) -> None:
        observes: list[dict[str, Any]] = []
        manager, col, _ = make_manager(observes=observes)
        create = manager.submit_create(dict(CREATE_ARGS))
        manager.accept({"id": create["proposal_id"]})
        note_id = next(iter(col._notes))
        edit = manager.submit_edit(
            {
                "note_id": note_id,
                "field_changes": {"Front": "New Q?", "Back": "New A."},
            }
        )
        manager.accept({"id": edit["proposal_id"], "accepted_fields": ["Front"]})
        reviewed = [e for e in observes if e["event"] == "reviewed"][-1]
        self.assertEqual("edit", reviewed["proposal_kind"])
        self.assertEqual(["Back"], reviewed["declined_fields"])

    def test_revert_emits_resync_not_applied(self) -> None:
        observes: list[dict[str, Any]] = []
        manager, _, _ = make_manager(observes=observes)
        result = manager.submit_create(dict(CREATE_ARGS))
        manager.accept({"id": result["proposal_id"]})
        observes.clear()
        manager.revert({"id": result["proposal_id"]})
        kinds = [e["event"] for e in observes]
        self.assertIn("resync", kinds)
        self.assertNotIn("applied", kinds)


SKILL_UPDATE_ARGS = {
    "summary": "Keep answers to one line.",
    "patterns": ["You shortened 7 answers."],
    "new_content": "---\nname: t\n---\n\nnew body\n",
}


class SkillUpdateTests(unittest.TestCase):
    def test_always_pending_even_under_trusted_writes(self) -> None:
        manager, _, pushed = make_manager(
            config={"permission_mode": "trusted-writes"},
            apply_skill=lambda _p: [],
        )
        result = manager.submit_skill_update(
            dict(SKILL_UPDATE_ARGS), old_content="old", observation_ids=["a", "b"]
        )
        self.assertEqual("pending_user_review", result["status"])
        (proposal,) = pushes_of(pushed, "proposal")
        self.assertEqual("skill_update", proposal["proposal"]["kind"])
        self.assertEqual(2, proposal["proposal"]["count"])
        self.assertIn("diff", proposal["proposal"]["op_args"])

    def test_accept_applies_via_callable_and_is_not_revertible(self) -> None:
        applied: list[Any] = []

        def apply_skill(proposal: Any) -> list[str]:
            applied.append(proposal.op_args["new_content"])
            return ["archived as SKILL-1.md"]

        manager, _, pushed = make_manager(apply_skill=apply_skill)
        result = manager.submit_skill_update(
            dict(SKILL_UPDATE_ARGS), old_content="old", observation_ids=["a"]
        )
        manager.accept({"id": result["proposal_id"]})
        self.assertEqual([SKILL_UPDATE_ARGS["new_content"]], applied)
        (resolved,) = pushes_of(pushed, "proposal_resolved")
        self.assertEqual("accepted", resolved["status"])
        self.assertFalse(resolved["revertible"])
        self.assertEqual(["archived as SKILL-1.md"], resolved["warnings"])
        # Skill updates never enter the note ledger.
        self.assertEqual([], pushes_of(pushed, "ledger")[-1]["entries"])

    def test_validation_requires_summary_patterns_and_real_change(self) -> None:
        manager, _, _ = make_manager(apply_skill=lambda _p: [])
        with self.assertRaises(ProposalError):
            manager.submit_skill_update(
                {"summary": "", "patterns": [], "new_content": "x"},
                old_content="old",
                observation_ids=[],
            )
        with self.assertRaises(ProposalError):
            manager.submit_skill_update(
                dict(SKILL_UPDATE_ARGS, new_content="old"),
                old_content="old",
                observation_ids=[],
            )

    def test_accept_without_apply_callable_reports_error(self) -> None:
        manager, _, pushed = make_manager()
        result = manager.submit_skill_update(
            dict(SKILL_UPDATE_ARGS), old_content="old", observation_ids=[]
        )
        manager.accept({"id": result["proposal_id"]})
        (error,) = pushes_of(pushed, "proposal_error")
        self.assertIn("not available", error["message"])


class DeckOpTests(unittest.TestCase):
    def _accept_last(self, manager, pushed) -> str:
        pid = pushes_of(pushed, "proposal")[-1]["proposal"]["id"]
        manager.accept({"id": pid})
        return pid

    def _last_resolved(self, pushed) -> dict[str, Any]:
        return pushes_of(pushed, "proposal_resolved")[-1]

    def test_create_deck_accept_and_revert(self) -> None:
        manager, col, pushed = make_manager()
        result = manager.submit_create_deck({"name": "Math::Series"})
        self.assertEqual("pending_user_review", result["status"])
        pid = self._accept_last(manager, pushed)
        self.assertTrue(col.decks.id_for_name("Math::Series"))
        self.assertTrue(self._last_resolved(pushed)["revertible"])
        manager.revert({"id": pid})
        with self.assertRaises(KeyError):
            col.decks.id_for_name("Math::Series")

    def test_create_deck_existing_rejected(self) -> None:
        manager, _col, _pushed = make_manager()
        with self.assertRaises(ProposalError):
            manager.submit_create_deck({"name": "Default"})

    def test_create_deck_revert_refuses_when_cards_arrived(self) -> None:
        manager, col, pushed = make_manager()
        manager.submit_create_deck({"name": "Inbox"})
        pid = self._accept_last(manager, pushed)
        manager.submit_create({**CREATE_ARGS, "deck": "Inbox"})
        self._accept_last(manager, pushed)
        manager.revert({"id": pid})
        notices = pushes_of(pushed, "notice")
        self.assertIn("contains cards", notices[-1]["text"])
        self.assertTrue(col.decks.id_for_name("Inbox"))  # still there

    def test_rename_deck_renames_children_and_reverts(self) -> None:
        manager, col, pushed = make_manager()
        col.decks.id("Spanish")
        col.decks.id("Spanish::Verbs")
        result = manager.submit_rename_deck(
            {"deck": "Spanish", "new_name": "Español"}
        )
        self.assertEqual("pending_user_review", result["status"])
        warnings = pushes_of(pushed, "proposal")[-1]["proposal"]["warnings"]
        self.assertIn("1 subdeck(s)", warnings[0])
        pid = self._accept_last(manager, pushed)
        self.assertTrue(col.decks.id_for_name("Español::Verbs"))
        with self.assertRaises(KeyError):
            col.decks.id_for_name("Spanish")
        manager.revert({"id": pid})
        self.assertTrue(col.decks.id_for_name("Spanish::Verbs"))

    def test_rename_deck_validation(self) -> None:
        manager, col, _pushed = make_manager()
        col.decks.id("A")
        col.decks.id("B")
        with self.assertRaises(ProposalError):  # collision
            manager.submit_rename_deck({"deck": "A", "new_name": "B"})
        with self.assertRaises(ProposalError):  # under itself
            manager.submit_rename_deck({"deck": "A", "new_name": "A::sub"})
        with self.assertRaises(ProposalError):  # missing source
            manager.submit_rename_deck({"deck": "Nope", "new_name": "C"})

    def test_set_deck_options_apply_and_revert(self) -> None:
        checkpoints: list = []
        manager, col, pushed = make_manager(checkpoints=checkpoints)
        manager.submit_set_deck_options(
            {"deck": "Default", "options": {"new.perDay": 40}}
        )
        payload = pushes_of(pushed, "proposal")[-1]["proposal"]
        self.assertEqual(1, payload["count"])
        self.assertIn("new.perDay", payload["samples"][0]["text"])
        pid = self._accept_last(manager, pushed)
        did = col.decks.id_for_name("Default")
        self.assertEqual(40, col.decks.config_dict_for_deck_id(did)["new"]["perDay"])
        self.assertIn(("deck options", False), checkpoints)
        manager.revert({"id": pid})
        self.assertEqual(20, col.decks.config_dict_for_deck_id(did)["new"]["perDay"])

    def test_set_deck_options_validation(self) -> None:
        manager, _col, _pushed = make_manager()
        with self.assertRaises(ProposalError):  # unknown dot path
            manager.submit_set_deck_options(
                {"deck": "Default", "options": {"new.bogus": 1}}
            )
        with self.assertRaises(ProposalError):  # type mismatch
            manager.submit_set_deck_options(
                {"deck": "Default", "options": {"new.perDay": "forty"}}
            )
        with self.assertRaises(ProposalError):  # no effective change
            manager.submit_set_deck_options(
                {"deck": "Default", "options": {"new.perDay": 20}}
            )
        with self.assertRaises(ProposalError):  # empty options
            manager.submit_set_deck_options({"deck": "Default", "options": {}})

    def test_set_deck_options_shared_preset_warns(self) -> None:
        manager, col, pushed = make_manager()
        col.decks.id("Other")  # second deck on the Default preset
        manager.submit_set_deck_options(
            {"deck": "Default", "options": {"rev.perDay": 300}}
        )
        warnings = pushes_of(pushed, "proposal")[-1]["proposal"]["warnings"]
        self.assertIn("shared by 2 decks", warnings[0])

    def test_filtered_deck_lifecycle(self) -> None:
        manager, col, pushed = make_manager()
        _seed_notes(manager, col, pushed)  # 3 notes / 3 cards in Default
        manager.submit_create_filtered_deck(
            {
                "name": "Cram",
                "terms": [{"search": 'deck:"Default"', "limit": 10, "order": 6}],
            }
        )
        create_pid = self._accept_last(manager, pushed)
        resolved = self._last_resolved(pushed)
        self.assertIn("gathered 3 card(s)", resolved["warnings"][0])
        cram = col.decks.id_for_name("Cram")
        self.assertEqual(3, len([c for c in col._cards.values() if c.did == cram]))

        # Empty: cards return home; not ledger-revertible, no ledger entry.
        ledger_before = len(manager._ledger)
        manager.submit_filtered_deck_action({"deck": "Cram", "action": "empty"})
        self._accept_last(manager, pushed)
        self.assertFalse(self._last_resolved(pushed)["revertible"])
        self.assertEqual(ledger_before, len(manager._ledger))
        self.assertEqual(0, len([c for c in col._cards.values() if c.did == cram]))

        # Rebuild gathers again.
        manager.submit_filtered_deck_action({"deck": "Cram", "action": "rebuild"})
        self._accept_last(manager, pushed)
        self.assertEqual(3, len([c for c in col._cards.values() if c.did == cram]))

        # Update narrows the gather; revert restores terms and rebuilds.
        manager.submit_update_filtered_deck(
            {
                "deck": "Cram",
                "terms": [{"search": 'tag:"analysis"', "limit": 1, "order": 1}],
            }
        )
        update_pid = self._accept_last(manager, pushed)
        self.assertEqual(1, len([c for c in col._cards.values() if c.did == cram]))
        manager.revert({"id": update_pid})
        deck = col.decks.get(cram)
        self.assertEqual([['deck:"Default"', 10, 6]], deck["terms"])
        self.assertEqual(3, len([c for c in col._cards.values() if c.did == cram]))

        # Reverting the create removes the deck and sends every card home.
        manager.revert({"id": create_pid})
        with self.assertRaises(KeyError):
            col.decks.id_for_name("Cram")
        default_did = col.decks.id_for_name("Default")
        self.assertEqual(
            3, len([c for c in col._cards.values() if c.did == default_did])
        )
        self.assertTrue(all(c.odid == 0 for c in col._cards.values()))

    def test_filtered_term_validation(self) -> None:
        manager, _col, _pushed = make_manager()
        with self.assertRaises(ProposalError):  # no terms
            manager.submit_create_filtered_deck({"name": "X", "terms": []})
        with self.assertRaises(ProposalError):  # bad order
            manager.submit_create_filtered_deck(
                {"name": "X", "terms": [{"search": "a", "order": 9}]}
            )
        with self.assertRaises(ProposalError):  # bad limit
            manager.submit_create_filtered_deck(
                {"name": "X", "terms": [{"search": "a", "limit": 0}]}
            )
        with self.assertRaises(ProposalError):  # empty search
            manager.submit_create_filtered_deck(
                {"name": "X", "terms": [{"search": "  "}]}
            )
        with self.assertRaises(ProposalError):  # existing name
            manager.submit_create_filtered_deck(
                {"name": "Default", "terms": [{"search": "a"}]}
            )

    def test_ops_reject_wrong_deck_type(self) -> None:
        manager, col, _pushed = make_manager()
        col.decks.new_filtered("Cram")
        with self.assertRaises(ProposalError):  # options on a filtered deck
            manager.submit_set_deck_options(
                {"deck": "Cram", "options": {"new.perDay": 1}}
            )
        with self.assertRaises(ProposalError):  # action on a normal deck
            manager.submit_filtered_deck_action(
                {"deck": "Default", "action": "rebuild"}
            )
        with self.assertRaises(ProposalError):  # update on a normal deck
            manager.submit_update_filtered_deck(
                {"deck": "Default", "terms": [{"search": "a"}]}
            )
        with self.assertRaises(ProposalError):  # nothing to change
            manager.submit_update_filtered_deck({"deck": "Cram"})
        with self.assertRaises(ProposalError):  # unknown action
            manager.submit_filtered_deck_action({"deck": "Cram", "action": "x"})

    def test_trusted_writes_applies_directly_within_budget(self) -> None:
        manager, col, _pushed = make_manager(
            {"permission_mode": "trusted-writes", "write_budget": 2}
        )
        result = manager.submit_create_deck({"name": "X"})
        self.assertEqual("applied", result["status"])
        self.assertTrue(col.decks.id_for_name("X"))
        result = manager.submit_rename_deck({"deck": "X", "new_name": "Y"})
        self.assertEqual("applied", result["status"])
        # Budget exhausted: falls back to a gated proposal card.
        result = manager.submit_create_deck({"name": "Z"})
        self.assertEqual("pending_user_review", result["status"])

    def test_deck_op_after_deck_change_fires(self) -> None:
        calls: list[int] = []
        col = FakeCol()
        pushed: list[dict[str, Any]] = []
        manager = ProposalManager(
            get_col=lambda: col,
            push=pushed.append,
            config={},
            after_deck_change=lambda: calls.append(1),
        )
        manager.submit_create_deck({"name": "X"})
        pid = pushes_of(pushed, "proposal")[-1]["proposal"]["id"]
        manager.accept({"id": pid})
        self.assertEqual(1, len(calls))
        manager.revert({"id": pid})
        self.assertEqual(2, len(calls))


class SafetyWiringTests(unittest.TestCase):
    """F1/F2/F3 and the contract+invariants chokepoint (SAFETY.md Part 2)."""

    def test_f1_move_revert_of_filtered_card_returns_to_home_deck(self) -> None:
        # A card living in a filtered deck has did=<filtered>, odid=<home>.
        # move_cards must capture the HOME deck (odid), so a later revert sends
        # the card home instead of calling set_deck into a filtered deck (which
        # hard-errors with CanNotMoveCardsInto).
        manager, col, pushed = make_manager()
        _seed_notes(manager, col, pushed, n=1)
        manager.submit_create_filtered_deck(
            {
                "name": "Cram",
                "terms": [{"search": 'deck:"Default"', "limit": 10, "order": 6}],
            }
        )
        cram_pid = pushes_of(pushed, "proposal")[-1]["proposal"]["id"]
        manager.accept({"id": cram_pid})
        cram = col.decks.id_for_name("Cram")
        default = col.decks.id_for_name("Default")
        card = next(iter(col._cards.values()))
        self.assertEqual(card.did, cram)  # gathered into the filtered deck
        self.assertEqual(card.odid, default)  # home recorded in odid

        result = manager.submit_move_cards(
            {"query": 'deck:"Cram"', "deck": "Archive"}
        )
        move_pid = result["proposal_id"]
        manager.accept({"id": move_pid})
        archive = col.decks.id_for_name("Archive")
        self.assertEqual(card.did, archive)

        # F1: the ledger captured the home deck (Default), never the filtered id.
        entry = next(e for e in manager._ledger if e.id == move_pid)
        self.assertEqual({default}, set(entry.data["card_decks"].values()))
        self.assertNotIn(cram, entry.data["card_decks"].values())

        # Revert sends the card to its normal home deck, no crash, no notice.
        manager.revert({"id": move_pid})
        self.assertEqual(card.did, default)
        self.assertFalse(
            any("Could not revert" in n["text"] for n in pushes_of(pushed, "notice"))
        )

    def test_f1_move_revert_backend_error_becomes_notice(self) -> None:
        # If a revert's stored destination is somehow a filtered deck, set_deck
        # hard-errors; the revert path must convert that to a clean notice, not
        # let the backend exception escape unhandled (and must not mark undone).
        from chat_with_your_cards.proposals import LedgerEntry

        manager, col, pushed = make_manager()
        _seed_notes(manager, col, pushed, n=1)
        col.decks.new_filtered("Cram")
        cram = col.decks.id_for_name("Cram")
        cid = next(iter(col._cards))
        manager._ledger.append(
            LedgerEntry(
                id="pX",
                kind="bulk",
                note_id=0,
                label="bad move",
                data={"op": "move_cards", "card_decks": {cid: cram}},
            )
        )
        manager.revert({"id": "pX"})
        self.assertTrue(
            any("Could not revert" in n["text"] for n in pushes_of(pushed, "notice"))
        )
        entry = next(e for e in manager._ledger if e.id == "pX")
        self.assertFalse(entry.undone)

    def test_f2_move_proposal_warns_about_fsrs_memory_loss(self) -> None:
        manager, col, pushed = make_manager()
        _seed_notes(manager, col, pushed, n=2)
        cards = list(col._cards.values())
        cards[0].memory_state = "s=1.0,d=5.0"  # this card carries FSRS memory
        # cards[1].memory_state stays None (no memory to lose)
        result = manager.submit_move_cards(
            {"query": 'deck:"Default"', "deck": "Archive"}
        )
        warnings = pushes_of(pushed, "proposal")[-1]["proposal"]["warnings"]
        self.assertTrue(any("FSRS memory" in w for w in warnings))
        self.assertTrue(any("1 card" in w for w in warnings))
        self.assertTrue(any("cannot be restored" in w for w in warnings))
        self.assertEqual(result["warnings"], warnings)

    def test_f2_no_warning_when_no_card_has_fsrs_memory(self) -> None:
        manager, col, pushed = make_manager()
        _seed_notes(manager, col, pushed, n=2)  # memory_state defaults to None
        manager.submit_move_cards({"query": 'deck:"Default"', "deck": "Archive"})
        warnings = pushes_of(pushed, "proposal")[-1]["proposal"]["warnings"]
        self.assertFalse(any("FSRS" in w for w in warnings))

    def test_f3_midbatch_failure_rolls_back_with_nothing_committed(self) -> None:
        manager, col, pushed = make_manager()
        ids = _seed_notes(manager, col, pushed, n=2)  # Back = "A0.", "A1."
        cs = manager.open_change_set({"title": "batch"})
        cs_id = cs["change_set_id"]
        manager.add_to_change_set(
            {"change_set_id": cs_id, "note_id": ids[0], "field_changes": {"Back": "ok0"}}
        )
        manager.add_to_change_set(
            {"change_set_id": cs_id, "note_id": ids[1], "field_changes": {"Back": "BOOM"}}
        )
        manager.close_change_set({"change_set_id": cs_id})

        manager.accept({"id": cs_id})  # note0 applies, note1 fails mid-batch

        # The whole batch rolled back: note0's committed change is undone.
        self.assertEqual("A0.", col.get_note(ids[0])["Back"])
        self.assertEqual("A1.", col.get_note(ids[1])["Back"])
        # Surfaced as a proposal error and left no ledger entry behind.
        self.assertTrue(pushes_of(pushed, "proposal_error"))
        self.assertEqual([], [e for e in manager._ledger if e.kind == "change_set"])

    def test_f3_create_rolls_back_when_postcondition_fails(self) -> None:
        # A postcondition violation inside the transaction must roll the create
        # back so nothing is committed. Force it by having add_note also spawn a
        # stray extra card, so the observed card delta (+2) disagrees with the
        # declared +1 and count_delta_matches raises.
        manager, col, pushed = make_manager()
        real_add_note = col.add_note

        def add_note_with_stray(note: Any, deck_id: int) -> None:
            real_add_note(note, deck_id)
            stray = FakeCard(did=deck_id, cid=note.id * 10 + 1)
            stray.nid = note.id
            col._cards[stray.id] = stray  # blast-radius: an undeclared extra card

        col.add_note = add_note_with_stray  # type: ignore[method-assign]
        result = manager.submit_create(dict(CREATE_ARGS))
        manager.accept({"id": result["proposal_id"]})

        self.assertTrue(pushes_of(pushed, "proposal_error"))
        self.assertEqual(0, col.note_count())  # rolled back: no note committed
        self.assertEqual(0, col.card_count())
        self.assertEqual([], manager._ledger)
        # SAFETY.md's dangling-undo-entry wart: add_note's own backend call
        # already completed (and would have pushed a real undo entry on real
        # Anki) before count_delta_matches caught the stray card and rolled
        # the SQL back. _discard_dangling_undo must ask for exactly one pop.
        self.assertEqual(1, col.undo_calls)


class UndoDiscardTests(unittest.TestCase):
    """SAFETY.md's "Known wart on the rollback path": a postcondition that
    rejects a write AFTER the backend already applied it leaves a dangling
    in-memory undo entry behind (real-Anki behavior; FakeCol has no separate
    undo queue to reproduce that on, see FakeCol.undo_calls). These tests
    lock in the *wiring*: _discard_dangling_undo is asked for exactly as many
    pops as backend RPCs actually ran - never a guess, never more than that
    (which would risk eating into real prior undo history). Whether a real
    col.undo() call is itself a safe no-op against a real, already-rolled-
    -back collection is verified separately by the gui_smoke probe."""

    @staticmethod
    def _force_postcondition_failure():
        from chat_with_your_cards import proposals as proposals_mod

        original = proposals_mod.invariants.assert_all

        def boom(*_a: Any, **_kw: Any) -> None:
            raise proposals_mod.invariants.InvariantViolation("forced")

        proposals_mod.invariants.assert_all = boom
        return proposals_mod, original

    def test_bulk_find_replace_pops_one_per_applied_note(self) -> None:
        manager, col, pushed = make_manager()
        _seed_notes(manager, col, pushed, n=3)
        result = manager.submit_find_replace({"search": "A", "replacement": "B"})

        proposals_mod, original = self._force_postcondition_failure()
        try:
            manager.accept({"id": result["proposal_id"]})
        finally:
            proposals_mod.invariants.assert_all = original

        self.assertTrue(pushes_of(pushed, "proposal_error"))
        # All 3 seeded notes match "A" -> update_note ran 3 times before the
        # postcondition raised, so exactly 3 entries are dangling.
        self.assertEqual(3, col.undo_calls)

    def test_precondition_failure_never_calls_undo(self) -> None:
        manager, col, pushed = make_manager()
        result = manager.submit_create(dict(CREATE_ARGS))
        self.assertEqual("pending_user_review", result["status"])
        # The note type vanishes between submit and accept (e.g. deleted in
        # another window): contract.check_create's precondition rejects it
        # INSIDE accept(), before execute() ever touches the backend. There
        # is nothing dangling to clean up, and calling undo() here would
        # wrongly pop a real, unrelated prior entry.
        del col.models._models["Basic"]
        manager.accept({"id": result["proposal_id"]})
        self.assertTrue(pushes_of(pushed, "proposal_error"))
        self.assertEqual(0, col.undo_calls)

    def test_revert_pops_after_a_forced_corruption_check(self) -> None:
        from chat_with_your_cards import invariants as invariants_mod

        manager, col, pushed = make_manager()
        _seed_notes(manager, col, pushed, n=1)
        result = manager.submit_find_replace({"search": "A0", "replacement": "Z0"})
        manager.accept({"id": result["proposal_id"]})
        self.assertEqual(0, col.undo_calls)  # clean apply, nothing dangling yet

        original = invariants_mod.no_homeless_filtered_cards
        invariants_mod.no_homeless_filtered_cards = lambda *_a, **_kw: "forced"
        try:
            manager.revert({"id": result["proposal_id"]})
        finally:
            invariants_mod.no_homeless_filtered_cards = original

        self.assertTrue(
            any("Could not revert" in n["text"] for n in pushes_of(pushed, "notice"))
        )
        # mutate_change_set ran to completion (one update_note) before the
        # forced corruption check rejected the revert.
        self.assertEqual(1, col.undo_calls)


if __name__ == "__main__":
    unittest.main()
