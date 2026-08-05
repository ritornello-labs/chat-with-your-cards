"""LearningStore unit tests: snapshots, the edit scan, observation
persistence/consumption, nudge logic, and the skill file writer
(DESIGN.md section 15). Uses the fake collection from test_proposals;
FakeCol has no .db or note.mod, so these tests exercise the
field-comparison fallback (the bulk-mod fast path runs against real
Anki in the GUI smoke)."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from chat_with_your_cards.learning import (  # noqa: E402
    OBS_DELETED,
    OBS_EDITED,
    LearningStore,
)
from test_proposals import BASIC, FakeCol  # noqa: E402


def add_note(col: FakeCol, front: str = "Q?", back: str = "A.", tags=None):
    note = col.new_note(BASIC)
    note["Front"] = front
    note["Back"] = back
    note.tags = list(tags or [])
    col.add_note(note, col.decks.id("Default"))
    return note.id


class LearningStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "learning"
        self.skill = Path(self._tmp.name) / "skills" / "SKILL.md"
        self.skill.parent.mkdir(parents=True, exist_ok=True)
        self.skill.write_text("---\nname: t\n---\n\nold skill body\n", encoding="utf-8")
        self.col = FakeCol()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def store(self) -> LearningStore:
        return LearningStore(self.root, self.skill)

    # ---- snapshots + scan ----

    def test_scan_without_changes_records_nothing(self):
        store = self.store()
        nid = add_note(self.col)
        store.snapshot_notes(self.col, [nid])
        self.assertEqual(0, store.scan(self.col))
        self.assertEqual([], store.pending())

    def test_scan_detects_field_edit_once(self):
        store = self.store()
        nid = add_note(self.col)
        store.snapshot_notes(self.col, [nid])
        note = self.col.get_note(nid)
        note["Back"] = "A much better answer."
        self.col.update_note(note)
        self.assertEqual(1, store.scan(self.col))
        obs = store.pending()[0]
        self.assertEqual(OBS_EDITED, obs["kind"])
        self.assertEqual(nid, obs["note_id"])
        change = next(c for c in obs["changes"] if c["name"] == "Back")
        self.assertEqual("A.", change["before"])
        self.assertEqual("A much better answer.", change["after"])
        # The snapshot advanced: the same edit is never re-reported.
        self.assertEqual(0, store.scan(self.col))

    def test_whitespace_only_changes_are_not_signal(self):
        store = self.store()
        nid = add_note(self.col, back="two  words")
        store.snapshot_notes(self.col, [nid])
        note = self.col.get_note(nid)
        note["Back"] = " two words "
        self.col.update_note(note)
        self.assertEqual(0, store.scan(self.col))

    def test_scan_detects_tag_change(self):
        store = self.store()
        nid = add_note(self.col, tags=["ai-created"])
        store.snapshot_notes(self.col, [nid])
        note = self.col.get_note(nid)
        note.tags = ["ai-created", "verified"]
        self.col.update_note(note)
        self.assertEqual(1, store.scan(self.col))
        obs = store.pending()[0]
        self.assertEqual(["ai-created", "verified"], obs["tags_after"])

    def test_scan_reports_deletion_and_drops_snapshot(self):
        store = self.store()
        nid = add_note(self.col)
        store.snapshot_notes(self.col, [nid])
        self.col.remove_notes([nid])
        self.assertEqual(1, store.scan(self.col))
        obs = store.pending()[0]
        self.assertEqual(OBS_DELETED, obs["kind"])
        self.assertEqual("Q?", obs["fields"]["Front"])
        self.assertEqual(0, store.scan(self.col))  # snapshot gone

    def test_resync_refreshes_only_tracked_notes(self):
        store = self.store()
        tracked = add_note(self.col, front="tracked")
        untracked = add_note(self.col, front="untracked")
        store.snapshot_notes(self.col, [tracked])
        # A revert-style resync must not put untracked notes under watch.
        store.snapshot_notes(self.col, [tracked, untracked], add=False)
        self.assertEqual(1, store.stats()["snapshots"])
        # A system removal (create-revert) drops the snapshot, no observation.
        self.col.remove_notes([tracked])
        store.snapshot_notes(self.col, [tracked], add=False)
        self.assertEqual(0, store.stats()["snapshots"])
        self.assertEqual([], store.pending())

    # ---- review-time diffs ----

    def test_record_review_filters_no_op(self):
        store = self.store()
        recorded = store.record_review(
            proposal_kind="create",
            note_type="Basic",
            fields_before={"Front": "Q?"},
            fields_after={"Front": " Q? "},  # whitespace only
            tags_before=["a"],
            tags_after=["a"],
        )
        self.assertFalse(recorded)
        self.assertEqual([], store.pending())

    def test_record_review_captures_field_tag_deck_and_declined(self):
        store = self.store()
        recorded = store.record_review(
            proposal_kind="edit",
            note_type="Basic",
            deck_before="Default",
            deck_after="Math",
            tags_before=["ai-created", "todo"],
            tags_after=["ai-created"],
            fields_before={"Front": "Q?", "Back": "A."},
            fields_after={"Front": "Q?", "Back": "Shorter."},
            declined_fields=["Extra"],
        )
        self.assertTrue(recorded)
        obs = store.pending()[0]
        self.assertEqual("Math", obs["deck_after"])
        self.assertEqual(["ai-created"], obs["tags_after"])
        self.assertEqual(["Extra"], obs["declined_fields"])
        self.assertEqual(
            [{"name": "Back", "before": "A.", "after": "Shorter."}], obs["changes"]
        )

    # ---- persistence, consumption, nudge ----

    def test_observations_survive_reload_and_consume_persists(self):
        store = self.store()
        nid = add_note(self.col)
        store.snapshot_notes(self.col, [nid])
        note = self.col.get_note(nid)
        note["Front"] = "Better Q?"
        self.col.update_note(note)
        store.scan(self.col)
        ids = store.pending_ids()
        self.assertEqual(1, len(ids))

        reloaded = self.store()
        self.assertEqual(ids, reloaded.pending_ids())
        self.assertEqual(1, reloaded.stats()["snapshots"])
        reloaded.consume(ids)
        self.assertEqual([], reloaded.pending())
        self.assertEqual([], self.store().pending_ids())  # consumed on disk

    def test_nudge_threshold_and_staleness(self):
        store = self.store()
        self.assertFalse(store.nudge_state(threshold=1)["nudge"])
        store.record_review(
            proposal_kind="create",
            note_type="Basic",
            fields_before={"Front": "a"},
            fields_after={"Front": "b"},
        )
        self.assertTrue(store.nudge_state(threshold=1)["nudge"])
        self.assertFalse(store.nudge_state(threshold=5)["nudge"])
        # A single old observation nudges once it passes the age limit.
        next(iter(store._observations.values()))["ts"] = 1  # epoch: ancient
        self.assertTrue(store.nudge_state(threshold=5, days=7)["nudge"])

    def test_background_attempt_runs_once_until_new_evidence_arrives(self):
        store = self.store()
        store.record_review(
            proposal_kind="create",
            note_type="Basic",
            fields_before={"Front": "a"},
            fields_after={"Front": "b"},
        )
        first_ids = store.pending_ids()
        self.assertTrue(store.background_due(threshold=1, days=7))
        store.mark_background_attempt(first_ids)
        self.assertFalse(store.background_due(threshold=1, days=7))
        # A persisted no-pattern result also survives restart.
        reloaded = self.store()
        self.assertFalse(reloaded.background_due(threshold=1, days=7))
        # Any new correction changes the evidence fingerprint.
        reloaded.record_review(
            proposal_kind="edit",
            note_type="Basic",
            fields_before={"Back": "long"},
            fields_after={"Back": "short"},
        )
        self.assertTrue(reloaded.background_due(threshold=1, days=7))

    def test_review_ready_attempt_is_memory_only(self):
        store = self.store()
        store.record_review(
            proposal_kind="create",
            note_type="Basic",
            fields_before={"Front": "a"},
            fields_after={"Front": "b"},
        )
        ids = store.pending_ids()
        store.mark_background_attempt(ids, persist=False)
        self.assertFalse(store.background_due(threshold=1, days=7))
        # A held proposal is session-local, so restart must recompute it.
        self.assertTrue(self.store().background_due(threshold=1, days=7))

    # ---- skill file ----

    def test_write_skill_archives_previous_version(self):
        store = self.store()
        old = store.read_skill()
        self.assertIn("old skill body", old)
        backup = store.write_skill("---\nname: t\n---\n\nnew skill body\n")
        self.assertIsNotNone(backup)
        self.assertEqual(old, backup.read_text(encoding="utf-8"))
        self.assertIn("new skill body", store.read_skill())
        self.assertGreater(store.stats()["bytes"], 0)


if __name__ == "__main__":
    unittest.main()
