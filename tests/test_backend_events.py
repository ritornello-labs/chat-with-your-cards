from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from chat_with_your_cards.backends import (  # noqa: E402
    Done,
    ErrorEvent,
    TextDelta,
    ThinkingDelta,
    ToolCallFinished,
    ToolCallStarted,
    UsageUpdate,
    event_to_dict,
)


class EventToDictTest(unittest.TestCase):
    def test_text_delta(self) -> None:
        self.assertEqual(
            {"type": "text_delta", "text": "hello "},
            event_to_dict(TextDelta("hello ")),
        )

    def test_thinking_delta(self) -> None:
        self.assertEqual(
            {
                "type": "thinking_delta",
                "text": "hmm, let's see... ",
                "estimated_tokens": None,
            },
            event_to_dict(ThinkingDelta("hmm, let's see... ")),
        )

    def test_thinking_delta_with_estimated_tokens(self) -> None:
        self.assertEqual(
            {"type": "thinking_delta", "text": "", "estimated_tokens": 150},
            event_to_dict(ThinkingDelta("", 150)),
        )

    def test_tool_call_started(self) -> None:
        self.assertEqual(
            {
                "type": "tool_call_started",
                "call_id": "call-1",
                "tool": "search_notes",
                "summary": 'deck:current "limit"',
            },
            event_to_dict(ToolCallStarted("call-1", "search_notes", 'deck:current "limit"')),
        )

    def test_tool_call_finished(self) -> None:
        self.assertEqual(
            {
                "type": "tool_call_finished",
                "call_id": "call-1",
                "ok": True,
                "summary": "12 notes",
            },
            event_to_dict(ToolCallFinished("call-1", True, "12 notes")),
        )

    def test_usage_with_cache_tokens(self) -> None:
        self.assertEqual(
            {
                "type": "usage",
                "cost_usd": 0.42,
                "input_tokens": 1200,
                "output_tokens": 300,
                "cache_read_tokens": 500_000,
                "cache_creation_tokens": 12_000,
                "context_window": 1_000_000,
                "fast_mode_state": "on",
            },
            event_to_dict(
                UsageUpdate(
                    cost_usd=0.42,
                    input_tokens=1200,
                    output_tokens=300,
                    cache_read_tokens=500_000,
                    cache_creation_tokens=12_000,
                    context_window=1_000_000,
                    fast_mode_state="on",
                )
            ),
        )

    def test_usage_defaults_cache_tokens_to_none(self) -> None:
        payload = event_to_dict(UsageUpdate(cost_usd=None, input_tokens=10, output_tokens=5))
        self.assertIsNone(payload["cache_read_tokens"])
        self.assertIsNone(payload["cache_creation_tokens"])
        self.assertIsNone(payload["context_window"])
        self.assertIsNone(payload["fast_mode_state"])

    def test_done(self) -> None:
        self.assertEqual({"type": "done"}, event_to_dict(Done()))

    def test_error(self) -> None:
        self.assertEqual(
            {"type": "error", "message": "backend exploded"},
            event_to_dict(ErrorEvent("backend exploded")),
        )

    def test_all_events_json_serializable(self) -> None:
        events = [
            TextDelta("x"),
            ThinkingDelta("x"),
            ToolCallStarted("call-1", "search_notes", "q"),
            ToolCallFinished("call-1", False, "boom"),
            Done(),
            ErrorEvent("nope"),
        ]
        for event in events:
            with self.subTest(event=event):
                json.dumps(event_to_dict(event))


if __name__ == "__main__":
    unittest.main()
