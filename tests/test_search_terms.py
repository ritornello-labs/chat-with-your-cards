"""search_terms.py: why an empty Anki search is empty.

The bug this closes: `deck:Default` returned 0 cards and was reported to the
user as "that deck is empty". The real deck was `Decks::Default` (dogfood
2026-07-23). Anki does not error on a name that does not exist, so a typo and
an empty deck are indistinguishable in the result.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from chat_with_your_cards.search_terms import (  # noqa: E402
    diagnose,
    diagnose_collection,
    find_unknown_terms,
    parse_terms,
    suggest,
)

DECKS = [
    "Default",  # unrelated top-level deck, to keep leaf-matching honest
    "Decks",
    "Decks::Default",
    "Decks::Spanish",
    "Decks::Spanish::Verbs",
    "Math::Analysis",
]
TAGS = ["leech", "marked", "geo::brazil", "geo::china"]
NOTE_TYPES = ["Basic", "Basic (and reversed card)", "Cloze"]


def _diagnose(query: str, decks=None) -> str | None:
    return diagnose(
        query,
        decks=DECKS if decks is None else decks,
        tags=TAGS,
        note_types=NOTE_TYPES,
    )


class ParseTests(unittest.TestCase):
    def test_both_quoting_styles_are_the_same_term(self) -> None:
        # Anki accepts either; they must not parse differently here.
        for query in ('deck:"Math::Analysis"', '"deck:Math::Analysis"'):
            terms = parse_terms(query)
            self.assertEqual(1, len(terms), query)
            self.assertEqual("deck", terms[0].prefix)
            self.assertEqual("Math::Analysis", terms[0].value)

    def test_negation_grouping_and_booleans(self) -> None:
        terms = parse_terms('(-deck:Foo or tag:leech) AND note:Basic')
        self.assertEqual(
            [("deck", "Foo", True), ("tag", "leech", False), ("note", "Basic", False)],
            [(t.prefix, t.value, t.negated) for t in terms],
        )

    def test_bare_words_and_escaped_colons_are_not_terms(self) -> None:
        self.assertEqual([], parse_terms("hello world"))
        self.assertEqual([], parse_terms(r"time\:30"))

    def test_prefix_is_case_insensitive(self) -> None:
        self.assertEqual("deck", parse_terms("Deck:Foo")[0].prefix)


class ExistenceTests(unittest.TestCase):
    def test_real_names_raise_nothing(self) -> None:
        for query in (
            'deck:"Decks::Spanish"',
            "deck:decks::spanish",  # Anki search is case-insensitive
            "deck:Decks",  # parent: matches its subdecks too
            "tag:geo",  # tags are hierarchical the same way
            "tag:geo::brazil",
            'note:"Basic (and reversed card)"',
            "deck:Decks::*",
            "deck:*",
            "tag:none",
            "deck:filtered",
            "deck:current",
            "is:due -deck:Math::Analysis",
        ):
            self.assertIsNone(_diagnose(query), query)

    def test_the_dogfood_case(self) -> None:
        message = _diagnose("deck:Default", decks=["Decks::Default", "Decks"])
        self.assertIsNotNone(message)
        assert message is not None
        self.assertIn("Decks::Default", message)
        self.assertIn("no deck by that name", message)
        # It must actively warn against reporting the zero as an answer.
        self.assertIn("NOT", message)

    def test_typo_is_caught_with_a_near_miss(self) -> None:
        message = _diagnose('deck:"Math::Analisys"')
        assert message is not None
        self.assertIn("Math::Analysis", message)

    def test_unknown_tag_and_note_type(self) -> None:
        self.assertIn("no tag by that name", _diagnose("tag:leach") or "")
        self.assertIn("no note type by that name", _diagnose("note:Bassic") or "")

    def test_note_types_are_not_hierarchical(self) -> None:
        # `note:Basic` must not be satisfied by "Basic (and reversed card)";
        # only an exact name counts. (It IS satisfied by "Basic" itself.)
        self.assertIsNone(_diagnose("note:Basic"))
        self.assertIsNotNone(
            diagnose("note:Basic", decks=DECKS, tags=TAGS, note_types=["Cloze"])
        )

    def test_wildcards_are_honoured(self) -> None:
        self.assertIsNone(_diagnose("deck:Decks::Span*"))
        self.assertIsNone(_diagnose("deck:Math::Analysi_"))
        self.assertIsNotNone(_diagnose("deck:Nope*"))

    def test_escaped_wildcard_is_a_literal(self) -> None:
        # `deck:Deck\*` asks for a deck literally named "Deck*".
        self.assertIsNotNone(_diagnose(r"deck:Deck\*"))

    def test_every_broken_term_is_reported_once(self) -> None:
        message = _diagnose("deck:Nope tag:alsonope deck:Nope")
        assert message is not None
        self.assertEqual(1, message.count('deck:"Nope"'))
        self.assertIn('tag:"alsonope"', message)

    def test_negated_term_is_still_reported(self) -> None:
        # `-deck:Typo` excludes nothing; silently including those cards is as
        # wrong as silently excluding them.
        self.assertIsNotNone(_diagnose("-deck:Typo"))

    def test_unchecked_prefixes_are_left_alone(self) -> None:
        for query in ("is:due", "prop:ivl>21", "re:^a", "added:1", "flag:1"):
            self.assertIsNone(_diagnose(query), query)


class SuggestTests(unittest.TestCase):
    def test_leaf_match_outranks_a_near_miss(self) -> None:
        # "Default" is 6 edits from "Decks::Default" but is exactly its leaf,
        # which is why suggestions are tiered rather than distance-only.
        best = suggest("Default", ["Defaults", "Decks::Default"])[0]
        self.assertEqual("Decks::Default", best)

    def test_no_plausible_name_suggests_nothing(self) -> None:
        self.assertEqual((), suggest("qqqqzzzz", DECKS))
        message = _diagnose("deck:qqqqzzzz")
        assert message is not None
        self.assertNotIn("did you mean", message)


class _Named:
    def __init__(self, name: str) -> None:
        self.name = name


class _NameList:
    def __init__(self, names: list[str]) -> None:
        self._names = names

    def all_names_and_ids(self) -> list[_Named]:
        return [_Named(name) for name in self._names]


class _Tags:
    def all(self) -> list[str]:
        return list(TAGS)


class FakeCol:
    decks = _NameList(DECKS)
    models = _NameList(NOTE_TYPES)
    tags = _Tags()


class CollectionAdapterTests(unittest.TestCase):
    """diagnose_collection is duck-typed over the live collection and must
    never raise - it only ever runs on an already-failed search."""

    def test_reads_names_off_the_collection(self) -> None:
        self.assertIsNone(diagnose_collection(FakeCol(), "deck:Decks::Spanish"))
        # Same shape as the dogfood failure: right leaf, missing parent path.
        self.assertIn(
            "Decks::Spanish", diagnose_collection(FakeCol(), "deck:Spanish") or ""
        )

    def test_blank_query_and_broken_collection_stay_silent(self) -> None:
        self.assertIsNone(diagnose_collection(FakeCol(), "   "))

        class Broken:
            @property
            def decks(self):
                raise RuntimeError("collection closed")

        self.assertIsNone(diagnose_collection(Broken(), "deck:Whatever"))


class UnknownTermShapeTests(unittest.TestCase):
    def test_problem_carries_the_written_name_unescaped(self) -> None:
        problems = find_unknown_terms(
            r'deck:"My\:Deck"', decks=[], tags=[], note_types=[]
        )
        self.assertEqual(1, len(problems))
        self.assertEqual("My:Deck", problems[0].value)
        self.assertEqual("deck", problems[0].kind)


if __name__ == "__main__":
    unittest.main()
