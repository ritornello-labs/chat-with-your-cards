from __future__ import annotations

import random
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from chat_with_your_cards.backends import Done, TextDelta, ThinkingDelta  # noqa: E402
from chat_with_your_cards.backends.fixtures import (  # noqa: E402
    SCRIPTS,
    select_script,
)
from chat_with_your_cards.backends.scripted import compile_script  # noqa: E402


class FixtureScriptsTest(unittest.TestCase):
    def test_expected_scripts_exist(self) -> None:
        self.assertEqual(
            {
                "default",
                "public_explain",
                "tool",
                "prerequisite",
                "long",
                "propose",
                "public_propose",
            },
            set(SCRIPTS),
        )

    def test_every_script_compiles_to_valid_timeline(self) -> None:
        for name, steps in SCRIPTS.items():
            with self.subTest(script=name):
                timeline = compile_script(steps, random.Random(0))
                events = [event for _, event in timeline]
                self.assertIsInstance(events[-1], Done)
                self.assertEqual(1, len([e for e in events if isinstance(e, Done)]))
                self.assertTrue(any(isinstance(e, TextDelta) for e in events))
                for delay, _ in timeline:
                    self.assertGreaterEqual(delay, 0)

    def test_text_steps_have_nonempty_markdown(self) -> None:
        for name, steps in SCRIPTS.items():
            for step in steps:
                if step["kind"] != "text":
                    continue
                with self.subTest(script=name):
                    self.assertTrue(step["markdown"].strip())

    def test_tool_steps_have_required_keys(self) -> None:
        required = {"tool", "summary", "result", "ok", "duration_ms"}
        for name, steps in SCRIPTS.items():
            for step in steps:
                if step["kind"] != "tool":
                    continue
                with self.subTest(script=name):
                    self.assertTrue(required.issubset(step))

    def test_selection_by_keyword(self) -> None:
        self.assertIs(SCRIPTS["tool"], select_script("please run a TOOL demo"))
        self.assertIs(SCRIPTS["long"], select_script("give me the long version"))
        self.assertIs(SCRIPTS["propose"], select_script("PROPOSE a note for this"))
        self.assertIs(SCRIPTS["default"], select_script("explain this card"))

    def test_tool_script_opens_with_a_thinking_phase(self) -> None:
        # DESIGN.md section 9 / the demo/fixtures task: the scripted "tool"
        # reply opens with empty-text, growing-estimated_tokens
        # ThinkingDelta beats, so the rotating "Thinking..." indicator (both
        # UIs) and the gui_smoke probe's DOM assertions have something real
        # to exercise before any text or tool call arrives.
        timeline = compile_script(SCRIPTS["tool"], random.Random(0))
        events = [event for _, event in timeline]
        thinking = [e for e in events if isinstance(e, ThinkingDelta)]
        self.assertGreaterEqual(len(thinking), 2)
        for delta in thinking:
            self.assertEqual("", delta.text)
            self.assertIsNotNone(delta.estimated_tokens)
        tokens = [d.estimated_tokens for d in thinking]
        self.assertEqual(sorted(tokens), tokens, "estimated_tokens must grow monotonically")
        # The thinking phase is the script's opening step, so it must be a
        # contiguous prefix of the compiled timeline (all thinking, then
        # something else - text, in this script's case).
        self.assertTrue(all(isinstance(e, ThinkingDelta) for e in events[: len(thinking)]))
        self.assertFalse(isinstance(events[len(thinking)], ThinkingDelta))

    def test_propose_script_requests_a_valid_creation(self) -> None:
        from chat_with_your_cards.backends import ProposalRequest

        steps = SCRIPTS["propose"]
        timeline = compile_script(steps, random.Random(0))
        requests = [e for _, e in timeline if isinstance(e, ProposalRequest)]
        self.assertEqual(1, len(requests))
        request = requests[0]
        self.assertEqual("create", request.kind)
        # Must reference stock objects so it validates in a fresh profile.
        self.assertEqual("Basic", request.payload["note_type"])
        self.assertEqual("Default", request.payload["deck"])
        self.assertTrue(request.payload["fields"]["Front"].strip())


if __name__ == "__main__":
    unittest.main()
