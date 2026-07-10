"""Precondition-validator tests against a fake collection.

Mirrors tests/test_proposals.py's fake-collection pattern (no real ``anki``
package in dev deps). Each validator is exercised directly for its raise
behavior, then the check_create / check_edit entry points for how they collect
errors and warnings.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from chat_with_your_cards.contract import (  # noqa: E402
    WILL_CREATE,
    CheckResult,
    ContractError,
    check_cloze,
    check_create,
    check_edit,
    check_fields,
    check_first_field,
    check_media_refs,
    check_tags,
    resolve_writable_deck,
)

BASIC = {
    "name": "Basic",
    "type": 0,
    "flds": [{"name": "Front"}, {"name": "Back"}],
    "css": "",
}
CLOZE = {
    "name": "Cloze",
    "type": 1,
    "flds": [{"name": "Text"}, {"name": "Extra"}],
    "css": "",
}


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
    """Just the surface the deck chokepoint touches: id_for_name / get, with a
    normal/filtered (``dyn``) distinction and ``::`` hierarchy."""

    def __init__(self, names: list[str]) -> None:
        self._decks: dict[int, dict[str, Any]] = {}
        self._next = 1
        for name in names:
            self._add(name)

    def _add(self, name: str, dyn: bool = False) -> int:
        did = self._next
        self._next += 1
        self._decks[did] = {"id": did, "name": name, "dyn": 1 if dyn else 0}
        return did

    def add_filtered(self, name: str) -> int:
        return self._add(name, dyn=True)

    def add_normal(self, name: str) -> int:
        return self._add(name)

    def id_for_name(self, name: str) -> int:
        for deck in self._decks.values():
            if deck["name"] == name:
                return deck["id"]
        raise KeyError(name)

    def get(self, did: int) -> dict[str, Any] | None:
        deck = self._decks.get(did)
        return dict(deck) if deck else None


class FakeMedia:
    def __init__(self, present: list[str] | None = None) -> None:
        self._present = set(present or [])

    def have(self, fname: str) -> bool:
        return fname in self._present


class FakeCol:
    def __init__(
        self,
        deck_names: list[str] | None = None,
        media: FakeMedia | None = None,
        models: list[dict[str, Any]] | None = None,
    ) -> None:
        self.models = FakeModels(models or [BASIC, CLOZE])
        self.decks = FakeDecks(deck_names or ["Default"])
        self.media = media


def make_proposal(**kwargs: Any) -> Any:
    """A duck-typed stand-in for proposals.Proposal (only the attributes the
    contract reads)."""
    defaults: dict[str, Any] = {
        "note_type": "Basic",
        "deck": "Default",
        "fields": {"Front": "Q?", "Back": "A."},
        "tags": [],
        "add_tags": [],
    }
    defaults.update(kwargs)
    return type("FakeProposal", (), defaults)()


# --------------------------------------------------------------------------- #
# resolve_writable_deck                                                        #
# --------------------------------------------------------------------------- #


class ResolveWritableDeckTests(unittest.TestCase):
    def test_existing_normal_deck_returns_id(self) -> None:
        col = FakeCol(["Default", "Spanish"])
        did = col.decks.id_for_name("Spanish")
        self.assertEqual(did, resolve_writable_deck(col, "Spanish"))

    def test_missing_deck_returns_sentinel(self) -> None:
        col = FakeCol(["Default"])
        self.assertIs(WILL_CREATE, resolve_writable_deck(col, "Brand::New"))

    def test_filtered_leaf_rejected(self) -> None:
        col = FakeCol(["Default"])
        col.decks.add_filtered("Cram")
        with self.assertRaisesRegex(ContractError, "filtered deck"):
            resolve_writable_deck(col, "Cram")

    def test_filtered_ancestor_rejected(self) -> None:
        col = FakeCol(["Default"])
        col.decks.add_filtered("Cram")
        with self.assertRaisesRegex(ContractError, "MustBeLeafNode"):
            resolve_writable_deck(col, "Cram::Child")

    def test_normal_ancestor_of_missing_deck_is_fine(self) -> None:
        col = FakeCol(["Default", "Spanish"])
        self.assertIs(WILL_CREATE, resolve_writable_deck(col, "Spanish::Verbs"))

    def test_blank_name_rejected(self) -> None:
        col = FakeCol(["Default"])
        with self.assertRaises(ContractError):
            resolve_writable_deck(col, "   ")


# --------------------------------------------------------------------------- #
# check_fields                                                                 #
# --------------------------------------------------------------------------- #


class CheckFieldsTests(unittest.TestCase):
    def test_exact_match_returns_model(self) -> None:
        col = FakeCol()
        model = check_fields(col, "Basic", {"Front": "Q", "Back": "A"})
        self.assertEqual("Basic", model["name"])

    def test_unknown_note_type_lists_available(self) -> None:
        col = FakeCol()
        with self.assertRaisesRegex(ContractError, "does not exist"):
            check_fields(col, "Nope", {"Front": "Q", "Back": "A"})

    def test_surplus_field_rejected(self) -> None:
        col = FakeCol()
        with self.assertRaisesRegex(ContractError, "not in this note type|not in this"):
            check_fields(col, "Basic", {"Front": "Q", "Back": "A", "Extra": "x"})

    def test_missing_field_rejected(self) -> None:
        col = FakeCol()
        with self.assertRaisesRegex(ContractError, "Missing"):
            check_fields(col, "Basic", {"Front": "Q"})

    def test_merge_hazard_named_in_message(self) -> None:
        col = FakeCol()
        with self.assertRaises(ContractError) as ctx:
            check_fields(col, "Basic", {"Front": "Q", "Back": "A", "Note": "n"})
        self.assertIn("merges", str(ctx.exception))


# --------------------------------------------------------------------------- #
# check_first_field                                                            #
# --------------------------------------------------------------------------- #


class CheckFirstFieldTests(unittest.TestCase):
    def test_nonempty_passes(self) -> None:
        check_first_field(BASIC, {"Front": "Q?", "Back": ""})

    def test_empty_rejected(self) -> None:
        with self.assertRaisesRegex(ContractError, "duplicate key"):
            check_first_field(BASIC, {"Front": "", "Back": "A"})

    def test_html_only_first_field_rejected(self) -> None:
        with self.assertRaises(ContractError):
            check_first_field(BASIC, {"Front": "<br><div></div>", "Back": "A"})

    def test_entity_only_whitespace_rejected(self) -> None:
        with self.assertRaises(ContractError):
            check_first_field(BASIC, {"Front": "&nbsp; ", "Back": "A"})

    def test_image_only_first_field_passes(self) -> None:
        # Media filenames are preserved by the csum strip, so an image-only
        # first field is legitimately non-empty.
        check_first_field(BASIC, {"Front": '<img src="cat.jpg">', "Back": "A"})

    def test_missing_first_field_key_rejected(self) -> None:
        with self.assertRaises(ContractError):
            check_first_field(BASIC, {"Back": "A"})


# --------------------------------------------------------------------------- #
# check_cloze                                                                  #
# --------------------------------------------------------------------------- #


class CheckClozeTests(unittest.TestCase):
    def test_standard_type_is_noop(self) -> None:
        check_cloze(BASIC, {"Front": "no cloze here", "Back": ""})

    def test_cloze_with_marker_passes(self) -> None:
        check_cloze(CLOZE, {"Text": "The capital is {{c1::Paris}}.", "Extra": ""})

    def test_multi_number_marker_passes(self) -> None:
        check_cloze(CLOZE, {"Text": "{{c1,2::both}} hidden", "Extra": ""})

    def test_cloze_without_marker_rejected(self) -> None:
        with self.assertRaisesRegex(ContractError, "cloze"):
            check_cloze(CLOZE, {"Text": "plain text, no deletion", "Extra": ""})

    def test_marker_in_any_field_counts(self) -> None:
        check_cloze(CLOZE, {"Text": "plain", "Extra": "{{c1::in extra}}"})


# --------------------------------------------------------------------------- #
# check_tags                                                                   #
# --------------------------------------------------------------------------- #


class CheckTagsTests(unittest.TestCase):
    def test_clean_tags_returned(self) -> None:
        self.assertEqual(["a", "b::c"], check_tags(["a", "b::c"]))

    def test_space_rejected(self) -> None:
        with self.assertRaisesRegex(ContractError, "space"):
            check_tags(["two words"])

    def test_ideographic_space_rejected(self) -> None:
        with self.assertRaisesRegex(ContractError, "U\\+3000|ideographic"):
            check_tags(["漢字　tag"])

    def test_nfc_normalization(self) -> None:
        # U+0065 U+0301 (e + combining acute) -> U+00E9 (é) under NFC.
        out = check_tags(["café"])
        self.assertEqual(["café"], out)

    def test_empty_list(self) -> None:
        self.assertEqual([], check_tags([]))


# --------------------------------------------------------------------------- #
# check_media_refs                                                             #
# --------------------------------------------------------------------------- #


class CheckMediaRefsTests(unittest.TestCase):
    def test_present_image_no_warning(self) -> None:
        col = FakeCol(media=FakeMedia(["cat.jpg"]))
        self.assertEqual([], check_media_refs(col, {"Front": '<img src="cat.jpg">'}))

    def test_missing_image_warns(self) -> None:
        col = FakeCol(media=FakeMedia([]))
        warnings = check_media_refs(col, {"Front": '<img src="ghost.png">'})
        self.assertEqual(1, len(warnings))
        self.assertIn("ghost.png", warnings[0])

    def test_missing_sound_warns(self) -> None:
        col = FakeCol(media=FakeMedia([]))
        warnings = check_media_refs(col, {"Back": "listen [sound:clip.mp3]"})
        self.assertIn("clip.mp3", warnings[0])

    def test_remote_and_data_uris_skipped(self) -> None:
        col = FakeCol(media=FakeMedia([]))
        fields = {
            "Front": '<img src="https://x/y.png">',
            "Back": '<img src="data:image/png;base64,AAAA">',
        }
        self.assertEqual([], check_media_refs(col, fields))

    def test_percent_encoded_name_resolved(self) -> None:
        col = FakeCol(media=FakeMedia(["my file.jpg"]))
        self.assertEqual([], check_media_refs(col, {"Front": '<img src="my%20file.jpg">'}))

    def test_no_media_object_no_warning(self) -> None:
        col = FakeCol(media=None)
        self.assertEqual([], check_media_refs(col, {"Front": '<img src="x.png">'}))

    def test_reference_deduplicated(self) -> None:
        col = FakeCol(media=FakeMedia([]))
        fields = {"Front": '<img src="x.png">', "Back": '<img src="x.png">'}
        self.assertEqual(1, len(check_media_refs(col, fields)))


# --------------------------------------------------------------------------- #
# check_create entry point                                                     #
# --------------------------------------------------------------------------- #


class CheckCreateTests(unittest.TestCase):
    def test_clean_proposal_ok(self) -> None:
        col = FakeCol(["Default"])
        result = check_create(col, make_proposal())
        self.assertTrue(result.ok)
        self.assertEqual([], result.errors)
        self.assertEqual(col.decks.id_for_name("Default"), result.deck_id)

    def test_missing_deck_is_warning_not_error(self) -> None:
        col = FakeCol(["Default"])
        result = check_create(col, make_proposal(deck="New::Deck"))
        self.assertTrue(result.ok)
        self.assertIs(WILL_CREATE, result.deck_id)
        self.assertTrue(any("will be created" in w for w in result.warnings))

    def test_filtered_deck_is_error(self) -> None:
        col = FakeCol(["Default"])
        col.decks.add_filtered("Cram")
        result = check_create(col, make_proposal(deck="Cram"))
        self.assertFalse(result.ok)
        self.assertTrue(any("filtered deck" in e for e in result.errors))

    def test_errors_accumulate_across_validators(self) -> None:
        col = FakeCol(["Default"])
        col.decks.add_filtered("Cram")
        proposal = make_proposal(
            deck="Cram",
            fields={"Front": "", "Back": "A"},
            tags=["bad tag"],
        )
        result = check_create(col, proposal)
        # filtered deck + empty first field + space in tag = three distinct errors.
        self.assertGreaterEqual(len(result.errors), 3)

    def test_notetype_failure_skips_model_checks(self) -> None:
        col = FakeCol(["Default"])
        result = check_create(col, make_proposal(note_type="Ghost"))
        self.assertFalse(result.ok)
        # Exactly one error (the notetype), not a cascade from first-field/cloze.
        self.assertEqual(1, len(result.errors))
        self.assertIn("does not exist", result.errors[0])

    def test_cloze_missing_marker_is_error(self) -> None:
        col = FakeCol(["Default"])
        proposal = make_proposal(
            note_type="Cloze",
            fields={"Text": "no marker", "Extra": ""},
        )
        result = check_create(col, proposal)
        self.assertFalse(result.ok)
        self.assertTrue(any("cloze" in e for e in result.errors))

    def test_media_warning_does_not_block(self) -> None:
        col = FakeCol(["Default"], media=FakeMedia([]))
        proposal = make_proposal(fields={"Front": '<img src="x.png">', "Back": "A"})
        result = check_create(col, proposal)
        self.assertTrue(result.ok)
        self.assertTrue(result.warnings)

    def test_raise_if_failed_joins_errors(self) -> None:
        col = FakeCol(["Default"])
        result = check_create(col, make_proposal(fields={"Front": "", "Back": "A"}))
        with self.assertRaises(ContractError):
            result.raise_if_failed()

    def test_tags_normalized_on_result(self) -> None:
        col = FakeCol(["Default"])
        result = check_create(col, make_proposal(tags=["café"]))
        self.assertEqual(["café"], result.tags)


# --------------------------------------------------------------------------- #
# check_edit entry point                                                       #
# --------------------------------------------------------------------------- #


class CheckEditTests(unittest.TestCase):
    def test_partial_fields_are_ok(self) -> None:
        # An edit touches only some fields; that must NOT trip the exact-count rule.
        col = FakeCol(["Default"])
        result = check_edit(col, make_proposal(fields={"Back": "new answer"}))
        self.assertTrue(result.ok)

    def test_unknown_field_rejected(self) -> None:
        col = FakeCol(["Default"])
        result = check_edit(col, make_proposal(fields={"Nope": "x"}))
        self.assertFalse(result.ok)
        self.assertTrue(any("not part of" in e for e in result.errors))

    def test_blanking_first_field_rejected(self) -> None:
        col = FakeCol(["Default"])
        result = check_edit(col, make_proposal(fields={"Front": ""}))
        self.assertFalse(result.ok)

    def test_editing_only_back_ignores_first_field(self) -> None:
        col = FakeCol(["Default"])
        result = check_edit(col, make_proposal(fields={"Back": ""}))
        self.assertTrue(result.ok)  # first field untouched, so no first-field error

    def test_bad_add_tag_rejected(self) -> None:
        col = FakeCol(["Default"])
        result = check_edit(
            col, make_proposal(fields={"Back": "x"}, add_tags=["two words"])
        )
        self.assertFalse(result.ok)

    def test_media_warning_surfaced_on_edit(self) -> None:
        col = FakeCol(["Default"], media=FakeMedia([]))
        result = check_edit(col, make_proposal(fields={"Back": '<img src="m.png">'}))
        self.assertTrue(result.ok)
        self.assertTrue(result.warnings)


class CheckResultTests(unittest.TestCase):
    def test_ok_when_no_errors(self) -> None:
        self.assertTrue(CheckResult().ok)
        self.assertFalse(CheckResult(errors=["x"]).ok)

    def test_raise_if_failed_noop_when_clean(self) -> None:
        CheckResult(warnings=["just a warning"]).raise_if_failed()  # must not raise


if __name__ == "__main__":
    unittest.main()
