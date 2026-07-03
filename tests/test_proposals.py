"""ProposalManager unit tests against a fake collection.

Covers validation, pins-as-constraints, the accept/reject flow, the
edit staleness guard, the session ledger with revert/undo, and
auto-accept with its per-session cap (DESIGN.md sections 5 and 8).
"""

from __future__ import annotations

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
    def __init__(self, did: int = 1, ord_: int = 0, reps: int = 0) -> None:
        self.did = did
        self.ord = ord_
        self.reps = reps


class FakeNote:
    def __init__(self, model: dict[str, Any]) -> None:
        self._model = model
        self._fields = {f["name"]: "" for f in model["flds"]}
        self.tags: list[str] = []
        self.id = 0
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
    def __init__(self, name: str) -> None:
        self.name = name


class FakeModels:
    def __init__(self, models: list[dict[str, Any]]) -> None:
        self._models = {m["name"]: m for m in models}

    def by_name(self, name: str) -> dict[str, Any] | None:
        return self._models.get(name)

    def all_names_and_ids(self) -> list[_NamedId]:
        return [_NamedId(name) for name in self._models]


class FakeDecks:
    def __init__(self, names: list[str]) -> None:
        self._by_name = {name: i + 1 for i, name in enumerate(names)}

    def id_for_name(self, name: str) -> int:
        if name not in self._by_name:
            raise KeyError(name)
        return self._by_name[name]

    def id(self, name: str) -> int:
        if name not in self._by_name:
            self._by_name[name] = max(self._by_name.values(), default=0) + 1
        return self._by_name[name]

    def name(self, did: int) -> str:
        for name, deck_id in self._by_name.items():
            if deck_id == did:
                return name
        return ""


class FakeCol:
    def __init__(self) -> None:
        self.models = FakeModels([BASIC])
        self.decks = FakeDecks(["Default"])
        self._notes: dict[int, FakeNote] = {}
        self._next_id = 100

    def new_note(self, model: dict[str, Any]) -> FakeNote:
        return FakeNote(model)

    def add_note(self, note: FakeNote, deck_id: int) -> None:
        note.id = self._next_id
        note._cards = [FakeCard(did=deck_id)]
        self._next_id += 1
        self._notes[note.id] = note

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
        stored = self._notes[note.id]
        stored._fields = dict(note._fields)
        stored.tags = list(note.tags)

    def remove_notes(self, note_ids: list[int]) -> None:
        for nid in note_ids:
            del self._notes[nid]

    def find_notes(self, query: str) -> list[int]:
        return []


def make_manager(config: dict[str, Any] | None = None):
    col = FakeCol()
    pushed: list[dict[str, Any]] = []
    manager = ProposalManager(
        get_col=lambda: col,
        push=pushed.append,
        config=config if config is not None else {},
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


if __name__ == "__main__":
    unittest.main()
