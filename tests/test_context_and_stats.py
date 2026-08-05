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

    def test_card_block_points_at_the_template_source(self) -> None:
        """The block lists field VALUES; without a pointer to the template an
        agent confidently reports that a card has no <iframe> when the template
        is what contains it (dogfood 2026-07-23)."""
        block = build_card_block(CARD_INFO)
        self.assertIn("get_note_type", block)
        self.assertIn(CARD_INFO["note_type"], block)
        self.assertIn("template", block.lower())

    def test_prefix_extraction(self) -> None:
        self.assertEqual(
            ["Analysis"],
            extract_field_prefixes(["Analysis: define limits", "no prefix here"]),
        )
        self.assertEqual([], extract_field_prefixes(["https://example.com: nope"]))

    def test_system_prompt_mentions_tools(self) -> None:
        prompt = build_system_prompt(permission_mode="read-only")
        self.assertIn("search_notes", prompt)
        self.assertIn("find_cards", prompt)
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

    def test_system_prompt_points_at_overview_tool(self) -> None:
        # The overview is a TOOL (get_collection_overview, design change
        # 2026-07-14) - the prompt must steer the agent to it, never claim
        # a <collection-overview> block will arrive in a message.
        prompt = build_system_prompt(permission_mode="default")
        self.assertIn("get_collection_overview", prompt)
        self.assertNotIn("<collection-overview>", prompt)
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
        """COMPLIANCE.md rule 3 regression guard: the assembled
        --append-system-prompt content must stay bounded (under 4,000 chars)
        in the worst case across every permission mode AND both agent-tools
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
            "full-collection",
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

    def test_wrap_user_message_takes_no_overview_block(self) -> None:
        # The overview moved to the get_collection_overview tool (design
        # change 2026-07-14): wrap_user_message must not re-grow an
        # overview parameter.
        with self.assertRaises(TypeError):
            wrap_user_message("hi", None, "<collection-overview>OV</collection-overview>")  # type: ignore[call-arg]


class OverviewToolTest(unittest.TestCase):
    """get_collection_overview: the on-demand replacement for the overview
    that used to be injected into the first user message (2026-07-14)."""

    class _Ctx:
        def __init__(self, stats: dict | None, config: dict) -> None:
            self.stats = stats
            self.config = config

    def test_returns_overview_text(self) -> None:
        from chat_with_your_cards.tools.collection import get_collection_overview

        result = get_collection_overview(
            self._Ctx(_stats(), {"context_token_budget": 8000}), {}
        )
        self.assertTrue(result["available"])
        self.assertIn("Deck", result["overview"])
        self.assertIn("tag0", result["overview"])

    def test_budget_arg_overrides_config(self) -> None:
        from chat_with_your_cards.tools.collection import get_collection_overview

        ctx = self._Ctx(_stats(deck_count=40, tag_count=200), {"context_token_budget": 8000})
        small = get_collection_overview(ctx, {"budget_tokens": 200})["overview"]
        large = get_collection_overview(ctx, {})["overview"]
        self.assertLess(len(small), len(large))

    def test_unavailable_without_stats(self) -> None:
        from chat_with_your_cards.tools.collection import get_collection_overview

        result = get_collection_overview(self._Ctx(None, {}), {})
        self.assertFalse(result["available"])


class GetNoteTypeTemplatesTest(unittest.TestCase):
    """get_note_type must expose the real template source. It used to return
    only NAMES, which left the agent blind to how a card renders: asked why an
    embedded iframe showed "Invalid path", it could see the fields but not the
    <iframe src="{{Wikipedia}}"> using them, so it reported no iframe existed
    and had to ask the user to paste the template (dogfood 2026-07-23)."""

    AFMT = '{{#Wikipedia}}\n<iframe src="{{Wikipedia}}" style="height: 100vh;"></iframe>\n{{/Wikipedia}}'

    class _Ctx:
        def __init__(self, model: dict | None) -> None:
            outer = self

            class _Models:
                def by_name(self, name: str) -> dict | None:
                    return outer._model

            class _Col:
                models = _Models()

            self._model = model
            self.col = _Col()

    def _model(self, afmt: str | None = None, css: str = ".card { color: black; }") -> dict:
        return {
            "name": "River",
            "flds": [{"name": "Name"}, {"name": "Wikipedia"}],
            "tmpls": [
                {"name": "Card 1", "qfmt": "{{Name}}", "afmt": afmt if afmt is not None else self.AFMT}
            ],
            "css": css,
        }

    def test_returns_full_template_source_and_css(self) -> None:
        from chat_with_your_cards.tools.collection import get_note_type

        result = get_note_type(self._Ctx(self._model()), {"name": "River"})
        self.assertEqual(result["fields"], ["Name", "Wikipedia"])
        (template,) = result["templates"]
        self.assertEqual(template["name"], "Card 1")
        self.assertEqual(template["qfmt"], "{{Name}}")
        # The whole point: the iframe is visible in the returned source.
        self.assertIn("<iframe", template["afmt"])
        self.assertIn('src="{{Wikipedia}}"', template["afmt"])
        self.assertIn("color: black", result["css"])

    def test_oversized_template_truncation_is_announced(self) -> None:
        from chat_with_your_cards.tools.collection import MAX_TEMPLATE_CHARS, get_note_type

        huge = "x" * (MAX_TEMPLATE_CHARS + 500)
        result = get_note_type(self._Ctx(self._model(afmt=huge)), {"name": "River"})
        afmt = result["templates"][0]["afmt"]
        self.assertIn("500 more characters", afmt)
        # The note must say how to read the rest FROM HERE - "open it in Anki"
        # is a dead end for the agent.
        self.assertIn(f"offset={MAX_TEMPLATE_CHARS}", afmt)

    def test_offset_and_max_chars_reach_the_rest(self) -> None:
        from chat_with_your_cards.tools.collection import MAX_TEMPLATE_CHARS, get_note_type

        tail = "THE-END"
        huge = "x" * (MAX_TEMPLATE_CHARS + 500) + tail
        ctx = self._Ctx(self._model(afmt=huge))
        # Page 2 via the offset the truncation note named.
        page2 = get_note_type(ctx, {"name": "River", "offset": MAX_TEMPLATE_CHARS})
        self.assertIn(tail, page2["templates"][0]["afmt"])
        # Or widen the window in one call.
        whole = get_note_type(ctx, {"name": "River", "max_chars": len(huge) + 10})
        self.assertIn(tail, whole["templates"][0]["afmt"])
        self.assertNotIn("more characters", whole["templates"][0]["afmt"])

    def test_max_chars_is_clamped_to_the_hard_ceiling(self) -> None:
        from chat_with_your_cards.tools.collection import (
            HARD_MAX_TEMPLATE_CHARS,
            get_note_type,
        )

        huge = "x" * (HARD_MAX_TEMPLATE_CHARS + 5_000)
        result = get_note_type(
            self._Ctx(self._model(afmt=huge)), {"name": "River", "max_chars": 10_000_000}
        )
        afmt = result["templates"][0]["afmt"]
        self.assertLess(len(afmt), HARD_MAX_TEMPLATE_CHARS + 500)
        self.assertIn("more characters", afmt)

    def test_missing_note_type_is_an_error(self) -> None:
        from chat_with_your_cards.tools.collection import get_note_type

        with self.assertRaises(ValueError):
            get_note_type(self._Ctx(None), {"name": "Nope"})


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
