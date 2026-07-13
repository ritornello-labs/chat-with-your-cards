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

    def test_system_prompt_mentions_tools(self) -> None:
        prompt = build_system_prompt(permission_mode="read-only")
        self.assertIn("search_notes", prompt)
        self.assertIn("find_related", prompt)
        self.assertIn("read-only", prompt)

    def test_system_prompt_has_no_overview_or_conventions_params(self) -> None:
        # COMPLIANCE.md rule 3: these two unbounded inputs must not be
        # re-accepted by build_system_prompt (overview -> first user message,
        # conventions -> a skill). A regression here would be a signature
        # change, which this call exercises directly.
        with self.assertRaises(TypeError):
            build_system_prompt("OVERVIEW-TEXT")  # type: ignore[call-arg]
        with self.assertRaises(TypeError):
            build_system_prompt(conventions="be terse")  # type: ignore[call-arg]

    def test_system_prompt_points_at_overview_first_message_and_tools(self) -> None:
        # Fixed, generalized fallback sentence - present regardless of
        # whether an overview happens to be available this run, since
        # build_system_prompt no longer knows either way.
        prompt = build_system_prompt(permission_mode="default")
        self.assertIn("<collection-overview>", prompt)
        self.assertIn("first message", prompt)
        self.assertIn("deck_tree", prompt)
        self.assertIn("collection_stats", prompt)

    def test_system_prompt_points_at_conventions_skill_when_writes_allowed(self) -> None:
        prompt = build_system_prompt(permission_mode="default")
        self.assertIn("note-conventions", prompt)
        self.assertIn("skill", prompt)

    def test_system_prompt_read_only_skips_conventions_pointer(self) -> None:
        # Read-only sessions can never propose/edit a card, so the
        # conventions-skill pointer (only relevant to those tools) is
        # omitted, matching the pre-refactor behavior of the inlined block.
        prompt = build_system_prompt(permission_mode="read-only")
        self.assertNotIn("note-conventions", prompt)

    def test_system_prompt_sandbox_states_no_shell(self) -> None:
        # Default agent-tools mode: the tight sandbox constraint must be
        # present and must NOT claim shell is available.
        prompt = build_system_prompt(permission_mode="default", agent_tools="sandbox")
        self.assertIn("No shell/file-writing", prompt)
        self.assertNotIn("You have full shell", prompt)

    def test_system_prompt_full_states_shell_and_injection_warning(self) -> None:
        # Full agent-tools mode: the constraint must flip to acknowledging the
        # shell/file tools and warn that card content is untrusted - it must
        # not still say shell is off (that would lie to the agent).
        prompt = build_system_prompt(permission_mode="default", agent_tools="full")
        self.assertIn("full shell/file tools", prompt)
        self.assertIn("untrusted", prompt)
        self.assertIn("AnkiConnect", prompt)
        self.assertNotIn("No shell/file-writing", prompt)

    def test_system_prompt_length_ceiling_worst_case(self) -> None:
        """COMPLIANCE.md rule 3 regression guard: the assembled --append-system-prompt content must stay bounded (under 4,000 chars) in the worst case. Assert real headroom below that in
        the worst case across every permission mode AND both agent-tools
        modes, with a heavily-pinned session (every pin type set, a long deck
        path, the longest stock note-type name, several tags, two field
        defaults) - larger than any pin configuration the dock's dropdowns/chip
        editor would realistically produce."""
        maximal_pins = {
            "deck": "Language::Mandarin::Vocabulary::HSK3",
            "note_type": "Basic (and reversed card)",
            "tags": ["mandarin", "hsk3", "vocab", "ai-created", "chapter-4"],
            "fields": {"Source": "Duolingo", "Extra": "from placement test"},
        }
        for mode in (
            "default",
            "ask-each-read",
            "read-only",
            "auto-accept",
            "trusted-writes",
        ):
            for agent_tools in ("sandbox", "full"):
                with self.subTest(mode=mode, agent_tools=agent_tools):
                    prompt = build_system_prompt(
                        permission_mode=mode,
                        agent_tools=agent_tools,
                        pins=maximal_pins,
                    )
                    self.assertLess(len(prompt), 4000)

    def test_wrap_user_message(self) -> None:
        self.assertEqual("hi", wrap_user_message("hi", None))
        wrapped = wrap_user_message("hi", "<current-card>x</current-card>")
        self.assertTrue(wrapped.startswith("<current-card>"))
        self.assertTrue(wrapped.endswith("hi"))

    def test_wrap_user_message_with_overview_block(self) -> None:
        overview = "<collection-overview>OV</collection-overview>"
        card = "<current-card>x</current-card>"
        # Overview alone.
        wrapped = wrap_user_message("hi", None, overview)
        self.assertEqual(f"{overview}\n\nhi", wrapped)
        # Overview stacks ahead of the card block, both ahead of the text.
        wrapped = wrap_user_message("hi", card, overview)
        self.assertEqual(f"{overview}\n\n{card}\n\nhi", wrapped)
        # Neither block: unchanged text, matching the pre-existing 2-arg call.
        self.assertEqual("hi", wrap_user_message("hi", None, None))


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
