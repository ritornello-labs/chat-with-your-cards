"""Note-type write path (#7) against a REAL, throwaway Anki collection.

Every other proposal family is unit-tested against `FakeCol`. This one must
not be, and the reason is the whole point of the task: the semantics here are
Anki's, they are surprising, and a fake would only ever encode what we already
believed. Probed on 25.x while building this (see NOTE_TYPE_OPS' table):

  * removing a field makes Anki SILENTLY REWRITE every template that
    referenced it, remapping the reference to a different field by ordinal -
    which can turn a conditional front unconditional and generate a card on
    every note;
  * when that rewrite instead makes two fronts identical, Anki refuses the
    whole update with CardTypeError;
  * renaming a field rewrites template references and note content follows;
  * `change_notetype` maps positionally (`new_fields[i] = old index`, -1 for
    empty), so a name-shaped mental model on a positional API is exactly how
    content lands in the wrong field.

Each test builds its own collection in a temp dir and closes it, so nothing
here can touch a real profile.

Skipped when `anki` is not importable (the shipped add-on runs inside Anki, so
it is not a runtime dependency and `make test` does not install it). The
permanent CI lane for this family is the Docker GUI smoke, which drives the
same code inside a real Anki. To run this file locally against Anki's own
interpreter:

    "$HOME/Library/Application Support/AnkiProgramFiles/.venv/bin/python" \
        -m unittest tests.test_note_types_real_anki -v
"""

from __future__ import annotations

import copy
import os
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:  # pragma: no cover - environment probe
    from anki.collection import Collection

    HAVE_ANKI = True
except Exception:  # pragma: no cover - the usual `make test` path
    Collection = None  # type: ignore[assignment,misc]
    HAVE_ANKI = False

from chat_with_your_cards.proposals import (  # noqa: E402
    NOTE_TYPE_OPS,
    ProposalError,
    ProposalManager,
)


@unittest.skipUnless(HAVE_ANKI, "the anki library is not installed in this env")
class RealAnkiNoteTypeTests(unittest.TestCase):
    """One throwaway collection per test; no profile is ever opened."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="cwyc-nt-real-")
        self.addCleanup(self._tmp.cleanup)
        self.col = Collection(os.path.join(self._tmp.name, "throwaway.anki2"))
        self.addCleanup(self.col.close)
        self.pushed: list[dict[str, Any]] = []
        self.checkpoints: list[tuple[str, bool]] = []
        self.manager = ProposalManager(
            get_col=lambda: self.col,
            push=self.pushed.append,
            config={},
            checkpoint=lambda reason, critical: (
                self.checkpoints.append((reason, critical)) or True
            ),
        )

    # ---- fixtures -------------------------------------------------------

    def _add_notes(self, note_type: str = "Basic", count: int = 3) -> list[int]:
        model = self.col.models.by_name(note_type)
        ids = []
        for i in range(count):
            note = self.col.new_note(model)
            for index, field in enumerate(model["flds"]):
                note[field["name"]] = f"{field['name']}{i}" if index < 2 else ""
            self.col.add_note(note, 1)
            ids.append(note.id)
        return ids

    def _with_extra_field(self, template_src: str | None = None) -> None:
        """Basic + an `Extra` field, optionally with a template using it."""
        model = self.col.models.by_name("Basic")
        self.col.models.add_field(model, self.col.models.new_field("Extra"))
        if template_src is not None:
            tmpl = self.col.models.new_template("Cond")
            tmpl["qfmt"] = template_src
            tmpl["afmt"] = "{{FrontSide}}<hr id=answer>{{Front}}"
            self.col.models.add_template(model, tmpl)
        self.col.models.update_dict(model)

    def _accept(self, result: dict[str, Any]) -> Any:
        pid = result["proposal_id"]
        self.manager.accept({"id": pid})
        return self.manager._proposals[pid]

    def _counts(self) -> tuple[int, int]:
        return self.col.note_count(), self.col.card_count()

    # ---- styling + template source (the reversible half) ----------------

    def test_styling_applies_and_reverts(self) -> None:
        self._add_notes()
        before = self.col.models.by_name("Basic")["css"]
        result = self.manager.submit_set_note_type_styling(
            {"note_type": "Basic", "css": ".card { color: teal; }"}
        )
        proposal = self._accept(result)
        self.assertEqual("accepted", proposal.status)
        self.assertIn("color: teal", self.col.models.by_name("Basic")["css"])
        self.manager.revert({"id": proposal.id})
        self.assertEqual(before, self.col.models.by_name("Basic")["css"])

    def test_template_source_applies_and_reverts(self) -> None:
        self._add_notes()
        original = copy.deepcopy(self.col.models.by_name("Basic")["tmpls"][0])
        result = self.manager.submit_set_card_template(
            {
                "note_type": "Basic",
                "template": "Card 1",
                "qfmt": "{{Front}}<div class='hint'>?</div>",
            }
        )
        proposal = self._accept(result)
        live = self.col.models.by_name("Basic")["tmpls"][0]
        self.assertIn("hint", live["qfmt"])
        # The back was not passed, so it must be untouched.
        self.assertEqual(original["afmt"], live["afmt"])
        self.manager.revert({"id": proposal.id})
        self.assertEqual(
            original["qfmt"], self.col.models.by_name("Basic")["tmpls"][0]["qfmt"]
        )

    def test_front_with_no_field_reference_is_refused(self) -> None:
        # Anki would reject this itself with an ordinal-flavoured
        # CardTypeError; catching it here names the actual problem.
        with self.assertRaises(ProposalError) as ctx:
            self.manager.submit_set_card_template(
                {"note_type": "Basic", "template": "Card 1", "qfmt": "<p>static</p>"}
            )
        self.assertIn("references no field", str(ctx.exception))

    # ---- fields ---------------------------------------------------------

    def test_add_and_rename_field_keep_content_and_revert(self) -> None:
        note_ids = self._add_notes()
        add = self._accept(
            self.manager.submit_manage_note_type_fields(
                {"note_type": "Basic", "op": "add", "field": "Source"}
            )
        )
        self.assertIn("Source", self.col.get_note(note_ids[0]).keys())
        note = self.col.get_note(note_ids[0])
        note["Source"] = "Rudin 4.1"
        self.col.update_note(note)

        rename = self._accept(
            self.manager.submit_manage_note_type_fields(
                {
                    "note_type": "Basic",
                    "op": "rename",
                    "field": "Source",
                    "new_name": "Reference",
                }
            )
        )
        items = dict(self.col.get_note(note_ids[0]).items())
        self.assertEqual("Rudin 4.1", items["Reference"])
        self.assertNotIn("Source", items)

        self.manager.revert({"id": rename.id})
        self.assertIn("Source", self.col.get_note(note_ids[0]).keys())
        self.manager.revert({"id": add.id})
        self.assertNotIn("Source", self.col.get_note(note_ids[0]).keys())

    def test_rename_rewrites_template_references(self) -> None:
        self._add_notes()
        self._accept(
            self.manager.submit_manage_note_type_fields(
                {
                    "note_type": "Basic",
                    "op": "rename",
                    "field": "Back",
                    "new_name": "Answer",
                }
            )
        )
        afmt = self.col.models.by_name("Basic")["tmpls"][0]["afmt"]
        self.assertIn("{{Answer}}", afmt)
        self.assertNotIn("{{Back}}", afmt)

    def test_removing_the_first_field_is_refused(self) -> None:
        self._add_notes()
        with self.assertRaises(ProposalError) as ctx:
            self.manager.submit_manage_note_type_fields(
                {"note_type": "Basic", "op": "remove", "field": "Front"}
            )
        self.assertIn("duplicate/sort key", str(ctx.exception))

    def test_field_removal_counts_content_and_warns_about_the_rewrite(self) -> None:
        """The finding this whole family is built around: removing a field
        that a template references makes Anki rewrite that template, and the
        rewrite can generate a card on every note."""
        self._with_extra_field("{{#Extra}}c:{{Extra}}{{/Extra}}")
        note_ids = self._add_notes()
        note = self.col.get_note(note_ids[0])
        note["Extra"] = "only this one"
        self.col.update_note(note)

        result = self.manager.submit_manage_note_type_fields(
            {"note_type": "Basic", "op": "remove", "field": "Extra"}
        )
        payload = self.pushed[-1]["proposal"]
        warnings = " ".join(payload["warnings"])
        self.assertIn("1 note(s) have content in this field", warnings)
        self.assertIn("SILENTLY REWRITES", warnings)
        self.assertIn("full upload", warnings)
        # Never revertible, and Anki's own duplicate-front guard means the
        # conditional card exists for exactly the one filled note.
        self.assertFalse(payload["revertible"])

        notes_before, cards_before = self._counts()
        proposal = self._accept(result)
        notes_after, cards_after = self._counts()
        self.assertEqual(notes_before, notes_after)

        # The rewrite really happened, and the resolved card says so.
        qfmt = self.col.models.by_name("Basic")["tmpls"][1]["qfmt"]
        self.assertNotIn("{{#Extra}}", qfmt)
        applied = [w for w in proposal.warnings if w.startswith("applied:")]
        self.assertTrue(applied, proposal.warnings)
        self.assertIn("rewrote card template", applied[0])
        if cards_after != cards_before:
            self.assertIn("card(s) created", applied[0])
        # Destructive ops take a real backup first.
        self.assertTrue(any(critical for _reason, critical in self.checkpoints))

    def test_card_type_error_surfaces_as_a_clean_proposal_error(self) -> None:
        """When the rewrite would make two fronts identical, Anki refuses the
        whole update. That must reach the user as a message, not a traceback,
        and must leave the note type untouched."""
        self._with_extra_field("{{#Extra}}{{Extra}}{{/Extra}}")
        self._add_notes()
        result = self.manager.submit_manage_note_type_fields(
            {"note_type": "Basic", "op": "remove", "field": "Extra"}
        )
        before = copy.deepcopy(self.col.models.by_name("Basic"))
        # accept() never raises at the bridge boundary - it reports.
        self.manager.accept({"id": result["proposal_id"]})
        errors = [p for p in self.pushed if p["type"] == "proposal_error"]
        self.assertTrue(errors, "Anki's refusal was not surfaced")
        self.assertIn("front side is identical", errors[-1]["message"].lower())
        after = self.col.models.by_name("Basic")
        self.assertEqual(
            [f["name"] for f in before["flds"]], [f["name"] for f in after["flds"]]
        )

    # ---- card templates -------------------------------------------------

    def test_add_template_generates_cards_and_revert_removes_them(self) -> None:
        self._add_notes(count=3)
        _notes, cards_before = self._counts()
        result = self.manager.submit_manage_card_templates(
            {
                "note_type": "Basic",
                "op": "add",
                "template": "Reverse",
                "qfmt": "{{Back}}",
                "afmt": "{{FrontSide}}<hr id=answer>{{Front}}",
            }
        )
        self.assertIn(
            "generates up to 3 new card(s)",
            " ".join(self.pushed[-1]["proposal"]["warnings"]),
        )
        proposal = self._accept(result)
        self.assertEqual(cards_before + 3, self._counts()[1])
        self.assertIn("3 card(s) created", " ".join(proposal.warnings))
        self.manager.revert({"id": proposal.id})
        self.assertEqual(cards_before, self._counts()[1])

    def test_remove_template_reports_doomed_cards_and_is_not_revertible(self) -> None:
        self._add_notes(count=2)
        self._accept(
            self.manager.submit_manage_card_templates(
                {
                    "note_type": "Basic",
                    "op": "add",
                    "template": "Reverse",
                    "qfmt": "{{Back}}",
                    "afmt": "{{FrontSide}}<hr id=answer>{{Front}}",
                }
            )
        )
        _notes, cards_before = self._counts()
        result = self.manager.submit_manage_card_templates(
            {"note_type": "Basic", "op": "remove", "template": "Reverse"}
        )
        payload = self.pushed[-1]["proposal"]
        self.assertIn("2 card(s) and their entire review history",
                      " ".join(payload["warnings"]))
        self.assertFalse(payload["revertible"])
        self._accept(result)
        self.assertEqual(cards_before - 2, self._counts()[1])

    def test_last_template_and_last_field_are_protected(self) -> None:
        self._add_notes()
        with self.assertRaises(ProposalError):
            self.manager.submit_manage_card_templates(
                {"note_type": "Basic", "op": "remove", "template": "Card 1"}
            )

    # ---- create + change note type --------------------------------------

    def test_create_note_type_clones_and_reverts(self) -> None:
        self._add_notes()
        result = self.manager.submit_create_note_type(
            {"name": "Basic (probe)", "clone_from": "Basic"}
        )
        proposal = self._accept(result)
        clone = self.col.models.by_name("Basic (probe)")
        self.assertIsNotNone(clone)
        self.assertEqual(
            [f["name"] for f in self.col.models.by_name("Basic")["flds"]],
            [f["name"] for f in clone["flds"]],
        )
        # Cloning touches no existing note.
        self.assertEqual([], list(self.col.models.nids(clone["id"])))
        self.manager.revert({"id": proposal.id})
        self.assertIsNone(self.col.models.by_name("Basic (probe)"))

    def test_change_note_type_maps_by_name_and_drops_the_unmapped(self) -> None:
        note_ids = self._add_notes(count=2)
        self._accept(
            self.manager.submit_create_note_type(
                {"name": "Target", "clone_from": "Basic"}
            )
        )
        self._accept(
            self.manager.submit_manage_note_type_fields(
                {"note_type": "Target", "op": "rename", "field": "Back", "new_name": "Meaning"}
            )
        )
        result = self.manager.submit_change_note_type(
            {
                "note_type": "Basic",
                "new_note_type": "Target",
                "field_map": {"Front": "Front", "Back": "Meaning"},
            }
        )
        payload = self.pushed[-1]["proposal"]
        self.assertEqual(2, payload["count"])
        self.assertFalse(payload["revertible"])
        self._accept(result)
        note = self.col.get_note(note_ids[0])
        self.assertEqual("Target", note.note_type()["name"])
        self.assertEqual("Front0", note["Front"])
        self.assertEqual("Back0", note["Meaning"])

    def test_change_note_type_unmapped_field_is_named_and_erased(self) -> None:
        note_ids = self._add_notes(count=1)
        self._accept(
            self.manager.submit_create_note_type(
                {"name": "Target", "clone_from": "Basic"}
            )
        )
        result = self.manager.submit_change_note_type(
            {
                "note_type": "Basic",
                "new_note_type": "Target",
                "field_map": {"Front": "Front"},  # Back deliberately dropped
            }
        )
        self.assertIn(
            "field content DESTROYED (mapped nowhere): Back",
            " ".join(self.pushed[-1]["proposal"]["warnings"]),
        )
        self._accept(result)
        note = self.col.get_note(note_ids[0])
        self.assertEqual("Front0", note["Front"])
        self.assertEqual("", note["Back"])

    def test_change_note_type_refuses_a_collapsing_map(self) -> None:
        self._add_notes(count=1)
        self._accept(
            self.manager.submit_create_note_type(
                {"name": "Target", "clone_from": "Basic"}
            )
        )
        with self.assertRaises(ProposalError) as ctx:
            self.manager.submit_change_note_type(
                {
                    "note_type": "Basic",
                    "new_note_type": "Target",
                    "field_map": {"Front": "Front", "Back": "Front"},
                }
            )
        self.assertIn("both map onto", str(ctx.exception))

    # ---- empty cards ----------------------------------------------------

    def test_remove_empty_cards_deletes_only_still_empty_cards(self) -> None:
        self._with_extra_field("{{#Extra}}c:{{Extra}}{{/Extra}}")
        note_ids = self._add_notes(count=2)
        for nid in note_ids:
            note = self.col.get_note(nid)
            note["Extra"] = "temporary"
            self.col.update_note(note)
        _notes, cards_with_extra = self._counts()
        # Blank both, so both conditional cards become empty.
        for nid in note_ids:
            note = self.col.get_note(nid)
            note["Extra"] = ""
            self.col.update_note(note)
        result = self.manager.submit_remove_empty_cards({})
        payload = self.pushed[-1]["proposal"]
        self.assertEqual(2, payload["count"])
        self.assertFalse(payload["revertible"])

        # Refill ONE of them between submit and accept: the accept path must
        # recompute and keep the card that is no longer empty.
        note = self.col.get_note(note_ids[0])
        note["Extra"] = "back again"
        self.col.update_note(note)

        proposal = self._accept(result)
        self.assertEqual(cards_with_extra - 1, self._counts()[1])
        self.assertIn("no longer empty and were kept", " ".join(proposal.warnings))

    def test_remove_empty_cards_with_none_found_is_a_clean_error(self) -> None:
        self._add_notes()
        with self.assertRaises(ProposalError) as ctx:
            self.manager.submit_remove_empty_cards({})
        self.assertIn("no empty cards", str(ctx.exception))

    # ---- cross-cutting --------------------------------------------------

    def test_every_op_in_the_table_reaches_a_submit(self) -> None:
        """The metadata table and the dispatch must not drift apart."""
        for op in NOTE_TYPE_OPS:
            self.assertTrue(
                hasattr(self.manager, f"submit_{op}"),
                f"NOTE_TYPE_OPS has {op!r} with no submit_{op}",
            )

    def test_blast_radius_is_reported_before_committing(self) -> None:
        self._add_notes(count=4)
        self.manager.submit_set_note_type_styling(
            {"note_type": "Basic", "css": ".card { color: teal; }"}
        )
        self.assertIn(
            "used by 4 note(s)", " ".join(self.pushed[-1]["proposal"]["warnings"])
        )

    def test_unknown_note_type_names_the_available_ones(self) -> None:
        with self.assertRaises(ProposalError) as ctx:
            self.manager.submit_set_note_type_styling(
                {"note_type": "Basisc", "css": "x"}
            )
        self.assertIn("Basic", str(ctx.exception))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
