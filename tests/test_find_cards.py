from __future__ import annotations

import unittest
from types import SimpleNamespace
from typing import Any

from chat_with_your_cards.tools.collection import (
    MAX_IDS_LIMIT,
    MAX_SEARCH_LIMIT,
    find_cards,
    get_card,
    search_notes,
)
from chat_with_your_cards.tools import build_registry


class FakeNote:
    def __init__(
        self,
        note_id: int,
        *,
        front: str,
        back: str,
        tags: list[str] | None = None,
    ) -> None:
        self.id = note_id
        self.tags = tags or []
        self._fields = {"Front": front, "Back": back, "Extra": "not in preview"}

    def items(self) -> list[tuple[str, str]]:
        return list(self._fields.items())

    def note_type(self) -> dict[str, str]:
        return {"name": "Basic (and reversed card)"}


class FakeCard:
    def __init__(
        self,
        card_id: int,
        note: FakeNote,
        *,
        template: str,
        ordinal: int,
        did: int = 1,
        odid: int = 0,
        queue: int = 2,
        flags: int = 0,
    ) -> None:
        self.id = card_id
        self.nid = note.id
        self.did = did
        self.odid = odid
        self.ord = ordinal
        self.queue = queue
        self.type = 2
        self.due = 42
        self.ivl = 12
        self.factor = 2500
        self.reps = 8
        self.lapses = 1
        self.flags = flags
        self._note = note
        self._template = template

    def note(self) -> FakeNote:
        return self._note

    def template(self) -> dict[str, str]:
        return {"name": self._template}


class FakeNamed:
    def __init__(self, name: str) -> None:
        self.name = name


class FakeDecks:
    NAMES = {1: "Default", 9: "Preview targets"}
    # Deliberately includes a nested deck whose leaf is a plausible thing to
    # search for on its own, which is the shape of the reported bug.
    ALL = ["Default", "Preview targets", "Decks::Spanish"]

    def name(self, deck_id: int) -> str:
        return self.NAMES[deck_id]

    def all_names_and_ids(self) -> list[FakeNamed]:
        return [FakeNamed(name) for name in self.ALL]


class FakeModels:
    def all_names_and_ids(self) -> list[FakeNamed]:
        return [FakeNamed("Basic"), FakeNamed("Cloze")]


class FakeTags:
    def all(self) -> list[str]:
        return ["probe", "leech"]


class FakeCollection:
    def __init__(self, cards: list[FakeCard], matches: list[int]) -> None:
        self.cards = {card.id: card for card in cards}
        self.matches = matches
        self.decks = FakeDecks()
        self.models = FakeModels()
        self.tags = FakeTags()
        self.queries: list[str] = []

    def find_cards(self, query: str) -> list[int]:
        self.queries.append(query)
        return list(self.matches)

    def get_card(self, card_id: int) -> FakeCard:
        return self.cards[card_id]


def _fixture() -> tuple[FakeCollection, Any]:
    shared_note = FakeNote(
        10,
        front="Which direction matched?",
        back="Only the reverse card did.",
        tags=["probe"],
    )
    other_note = FakeNote(20, front="Another note", back="Another answer")
    cards = [
        FakeCard(101, shared_note, template="Forward", ordinal=0),
        FakeCard(102, shared_note, template="Reverse", ordinal=1),
        FakeCard(
            103,
            other_note,
            template="Forward",
            ordinal=0,
            did=9,
            odid=1,
            queue=-3,
            flags=10,
        ),
    ]
    col = FakeCollection(cards, [102, 103, 101])
    return col, SimpleNamespace(col=col)


class FindCardsTests(unittest.TestCase):
    def test_preserves_exact_matching_sibling_and_native_order(self) -> None:
        col, ctx = _fixture()

        result = find_cards(ctx, {"query": "card:2 flag:2", "limit": 2})

        self.assertEqual(col.queries, ["card:2 flag:2"])
        self.assertEqual(result["total"], 3)
        self.assertEqual(result["shown"], 2)
        self.assertEqual(result["next_offset"], 2)
        self.assertEqual([card["card_id"] for card in result["cards"]], [102, 103])
        self.assertEqual(result["cards"][0]["template"], "Reverse")
        self.assertEqual(result["cards"][0]["note_id"], 10)
        self.assertNotIn("Extra", result["cards"][0]["fields_preview"])
        self.assertIn("not authorization", result["selection_note"])

    # ---- detail levels (user, 2026-07-23: `limit` was doing verbosity duty) ----

    def test_detail_count_returns_only_the_number(self) -> None:
        """The whole point: asking "how many?" should say so, not smuggle it
        through limit=1 and discard the row."""
        _col, ctx = _fixture()

        result = find_cards(ctx, {"query": "*", "detail": "count"})

        self.assertEqual(result["total"], 3)
        self.assertNotIn("cards", result)
        self.assertNotIn("card_ids", result)

    def test_detail_ids_returns_ids_without_summaries(self) -> None:
        _col, ctx = _fixture()

        result = find_cards(ctx, {"query": "*", "detail": "ids", "limit": 2})

        # Native match order, same as the summaries path (fixture: 102,103,101).
        self.assertEqual(result["card_ids"], [102, 103])
        self.assertNotIn("cards", result)
        self.assertEqual(result["total"], 3)

    def test_detail_defaults_to_full_summaries(self) -> None:
        _col, ctx = _fixture()

        self.assertIn("cards", find_cards(ctx, {"query": "*"}))

    def test_unknown_detail_is_a_clear_error(self) -> None:
        _col, ctx = _fixture()

        with self.assertRaises(ValueError) as caught:
            find_cards(ctx, {"query": "*", "detail": "verbose"})
        self.assertIn("detail", str(caught.exception))

    def test_pagination_reaches_remaining_matches(self) -> None:
        _col, ctx = _fixture()

        result = find_cards(ctx, {"query": "*", "offset": 2, "limit": 20})

        self.assertEqual(result["offset"], 2)
        self.assertEqual(result["shown"], 1)
        self.assertIsNone(result["next_offset"])
        self.assertEqual([card["card_id"] for card in result["cards"]], [101])

    def test_reports_filtered_hidden_and_flag_state(self) -> None:
        _col, ctx = _fixture()

        result = find_cards(ctx, {"query": "cid:103", "offset": 1, "limit": 1})
        (card,) = result["cards"]

        self.assertEqual(card["current_deck"], "Preview targets")
        self.assertEqual(card["home_deck"], "Default")
        self.assertTrue(card["in_filtered_deck"])
        self.assertEqual(card["hidden_state"], "manually buried")
        self.assertEqual(card["scheduling"]["queue"], -3)
        self.assertEqual(card["scheduling"]["user_flag"], 2)

    def test_get_card_exposes_the_same_card_level_context(self) -> None:
        col, ctx = _fixture()

        result = get_card(ctx, {"card_id": 103})

        self.assertEqual(result["card_id"], 103)
        self.assertEqual(result["current_deck"], "Preview targets")
        self.assertEqual(result["home_deck"], "Default")
        self.assertEqual(result["template_ordinal"], 0)
        self.assertEqual(result["hidden_state"], "manually buried")
        self.assertEqual(result["scheduling"]["user_flag"], 2)
        self.assertEqual(col.queries, [])

    def test_empty_result_from_a_bad_deck_name_is_an_error(self) -> None:
        """Zero rows because the deck does not exist must not come back looking
        like an answer - that is how `deck:Default` was reported as an empty
        deck when the real one was `Decks::Default` (dogfood 2026-07-23)."""
        col, ctx = _fixture()
        col.matches = []

        for detail in ("count", "ids", "full"):
            with self.assertRaises(ValueError) as caught:
                find_cards(ctx, {"query": "deck:Spanish", "detail": detail})
            self.assertIn("Decks::Spanish", str(caught.exception))

    def test_empty_result_from_a_real_deck_is_just_empty(self) -> None:
        col, ctx = _fixture()
        col.matches = []

        result = find_cards(ctx, {"query": "deck:Decks::Spanish is:due"})

        self.assertEqual(0, result["total"])

    def test_diagnosis_never_breaks_a_search(self) -> None:
        """It only ever runs on an already-failed search, so a collection that
        cannot answer name queries must degrade to a plain empty result."""
        col, ctx = _fixture()
        col.matches = []
        del col.models

        self.assertEqual(0, find_cards(ctx, {"query": "deck:Nonsense"})["total"])

    def test_registry_exposes_find_cards_as_a_read_tool(self) -> None:
        spec = next(spec for spec in build_registry().specs() if spec.name == "find_cards")

        self.assertFalse(spec.writes)
        # Raised for detail='ids' (#4); 'full' stays capped in the handler.
        self.assertEqual(
            spec.input_schema["properties"]["limit"]["maximum"], MAX_IDS_LIMIT
        )
        self.assertIn("exact matching cards", spec.description)
        self.assertIn("blindly", spec.description)

    def test_ids_detail_allows_pages_beyond_the_full_cap(self) -> None:
        """#4: the flat 100-cap meant bulk selection could not even enumerate
        its targets. detail='ids' now pages up to MAX_IDS_LIMIT."""
        col, ctx = _fixture()
        col.matches = list(range(1000, 1400))

        result = find_cards(ctx, {"query": "*", "detail": "ids", "limit": 400})
        self.assertEqual(400, len(result["card_ids"]))
        self.assertIsNone(result["next_offset"])

        # 'full' keeps the old cap: summaries are expensive context.
        col.matches = [102, 103, 101]
        result = find_cards(ctx, {"query": "*", "detail": "full", "limit": 400})
        self.assertEqual(3, result["shown"])


class SearchNotesPagingTests(unittest.TestCase):
    """search_notes gained find_cards' detail/limit/offset contract (#4)."""

    def _ctx(self, n: int = 250) -> Any:
        notes = {
            nid: FakeNote(nid, front=f"front {nid}", back="back")
            for nid in range(1, n + 1)
        }
        col = SimpleNamespace(
            find_notes=lambda query: list(notes),
            get_note=lambda nid: notes[nid],
        )
        return SimpleNamespace(col=col)

    def test_detail_count(self) -> None:
        result = search_notes(self._ctx(), {"query": "*", "detail": "count"})
        self.assertEqual(result, {"query": "*", "total": 250})

    def test_detail_ids_pages_beyond_the_old_cap(self) -> None:
        result = search_notes(
            self._ctx(), {"query": "*", "detail": "ids", "limit": 250}
        )
        self.assertEqual(250, len(result["note_ids"]))
        self.assertIsNone(result["next_offset"])
        self.assertNotIn("notes", result)

    def test_detail_ids_offset_paging(self) -> None:
        result = search_notes(
            self._ctx(), {"query": "*", "detail": "ids", "limit": 100, "offset": 200}
        )
        self.assertEqual(50, result["shown"])
        self.assertEqual(200, result["offset"])
        self.assertIsNone(result["next_offset"])

    def test_full_detail_stays_capped(self) -> None:
        result = search_notes(self._ctx(), {"query": "*", "limit": 250})
        self.assertEqual(MAX_SEARCH_LIMIT, result["shown"])
        self.assertEqual(MAX_SEARCH_LIMIT, result["next_offset"])
        self.assertEqual(250, result["total"])
        self.assertIn("fields_preview", result["notes"][0])


if __name__ == "__main__":
    unittest.main()
