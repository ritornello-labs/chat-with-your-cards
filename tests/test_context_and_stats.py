from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from chat_with_your_cards.context import (  # noqa: E402
    build_card_block,
    build_system_prompt,
    wrap_user_message,
)
from chat_with_your_cards.stats import (  # noqa: E402
    estimate_tokens,
    serialize_overview,
)
from chat_with_your_cards.tools.collection import extract_field_prefixes  # noqa: E402

CARD_INFO = {
    "card_id": 111,
    "note_id": 222,
    "deck": "Math::Analysis",
    "note_type": "Basic",
    "template": "Card 1",
    "tags": ["analysis", "limits"],
    "fields": {
        "Front": "Analysis: define limits",
        "Back": "for every epsilon > 0 ...",
    },
    "scheduling": {"interval_days": 12, "reps": 8, "lapses": 1},
}


def _stats(deck_count: int = 3, tag_count: int = 5) -> dict:
    return {
        "schema": 1,
        "computed_at": int(time.time()) - 120,
        "totals": {"notes": 100, "cards": 200},
        "decks": [
            {
                "name": "Deck" + "::Sub" * (i % 4),
                "notes": 10,
                "cards": 20,
                "review_secs": {"today": 65, "7d": 3600, "90d": 7200, "ever": 90000},
            }
            for i in range(deck_count)
        ],
        "tags": [{"name": f"tag{i}", "notes": i} for i in range(tag_count)],
        "note_types": [{"name": "Basic", "notes": 100}],
    }


class ContextTest(unittest.TestCase):
    def test_card_block_contains_fields_deck_and_clues(self) -> None:
        block = build_card_block(CARD_INFO)
        self.assertIn("Math::Analysis", block)
        self.assertIn("Front: Analysis: define limits", block)
        self.assertIn('"Analysis:"', block)
        self.assertIn("find_related", block)
        self.assertTrue(block.startswith("<current-card>"))
        self.assertTrue(block.endswith("</current-card>"))

    def test_prefix_extraction(self) -> None:
        self.assertEqual(
            ["Analysis"],
            extract_field_prefixes(["Analysis: define limits", "no prefix here"]),
        )
        self.assertEqual([], extract_field_prefixes(["https://example.com: nope"]))

    def test_system_prompt_mentions_tools_and_overview(self) -> None:
        prompt = build_system_prompt("OVERVIEW-TEXT", permission_mode="read-only")
        self.assertIn("search_notes", prompt)
        self.assertIn("find_related", prompt)
        self.assertIn("OVERVIEW-TEXT", prompt)
        self.assertIn("read-only", prompt)

    def test_system_prompt_without_overview_points_at_tools(self) -> None:
        prompt = build_system_prompt(None)
        self.assertIn("not computed yet", prompt)

    def test_wrap_user_message(self) -> None:
        self.assertEqual("hi", wrap_user_message("hi", None))
        wrapped = wrap_user_message("hi", "<current-card>x</current-card>")
        self.assertTrue(wrapped.startswith("<current-card>"))
        self.assertTrue(wrapped.endswith("hi"))


class OverviewSerializerTest(unittest.TestCase):
    def test_small_collection_fully_included(self) -> None:
        text = serialize_overview(_stats(), budget_tokens=8000)
        self.assertIn("100 notes, 200 cards", text)
        self.assertIn("rev today 1m", text)
        self.assertNotIn("folded", text)

    def test_over_budget_folds_with_annotations(self) -> None:
        stats = _stats(deck_count=400, tag_count=400)
        text = serialize_overview(stats, budget_tokens=500)
        self.assertLess(estimate_tokens(text), 3000)
        self.assertIn("deck_tree tool", text)
        self.assertIn("more tags", text)

    def test_deep_decks_folded_but_shallow_kept(self) -> None:
        stats = _stats(deck_count=400)
        text = serialize_overview(stats, budget_tokens=200)
        self.assertIn("Deck —", text)
        self.assertIn("more decks folded", text)


if __name__ == "__main__":
    unittest.main()
