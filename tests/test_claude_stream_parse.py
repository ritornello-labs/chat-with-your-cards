from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from chat_with_your_cards.backends import (  # noqa: E402
    Done,
    ErrorEvent,
    TextDelta,
    ToolCallFinished,
    ToolCallStarted,
)
from chat_with_your_cards.backends.claude_cli import (  # noqa: E402
    ParserState,
    build_cli_args,
    parse_stream_line,
)


class ParseStreamLineTest(unittest.TestCase):
    def setUp(self) -> None:
        self.state = ParserState()

    def test_init_captures_session_id(self) -> None:
        events = parse_stream_line(
            {"type": "system", "subtype": "init", "session_id": "abc-123"},
            self.state,
        )
        self.assertEqual([], events)
        self.assertEqual("abc-123", self.state.session_id)

    def test_text_delta(self) -> None:
        events = parse_stream_line(
            {
                "type": "stream_event",
                "event": {
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": "Hello "},
                },
            },
            self.state,
        )
        self.assertEqual([TextDelta("Hello ")], events)

    def test_assistant_tool_use_starts_call_once(self) -> None:
        line = {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": "Looking..."},
                    {
                        "type": "tool_use",
                        "id": "toolu_1",
                        "name": "mcp__anki__search_notes",
                        "input": {"query": 'deck:current "limit"'},
                    },
                ]
            },
        }
        first = parse_stream_line(line, self.state)
        second = parse_stream_line(line, self.state)  # full message repeats

        # No deltas preceded this message, so its text is surfaced too.
        self.assertEqual(2, len(first))
        self.assertEqual(TextDelta("Looking..."), first[0])
        started = first[1]
        assert isinstance(started, ToolCallStarted)
        self.assertEqual("toolu_1", started.call_id)
        self.assertEqual("mcp__anki__search_notes", started.tool)
        self.assertIn("limit", started.summary)
        self.assertEqual([], second, "duplicate tool_use must not re-start the chip")

    def test_streamed_text_not_duplicated_by_full_message(self) -> None:
        parse_stream_line(
            {
                "type": "stream_event",
                "event": {
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": "full text"},
                },
            },
            self.state,
        )
        events = parse_stream_line(
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "full text"}]},
            },
            self.state,
        )
        self.assertEqual([], events)

    def test_unstreamed_assistant_text_is_surfaced(self) -> None:
        # Synthetic CLI messages (e.g. "You've hit your session limit ·
        # resets 1pm") arrive as full assistant messages with no partial
        # deltas; dropping them left the chat silently hanging (2026-07-03).
        events = parse_stream_line(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "text", "text": "You've hit your session limit"}
                    ]
                },
            },
            self.state,
        )
        self.assertEqual([TextDelta("You've hit your session limit")], events)

    def test_unstreamed_text_counter_resets_per_message(self) -> None:
        streamed = {
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "delta": {"type": "text_delta", "text": "part one"},
            },
        }
        full_1 = {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "part one"}]},
        }
        full_2 = {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "synthetic follow-up"}]},
        }
        parse_stream_line(streamed, self.state)
        self.assertEqual([], parse_stream_line(full_1, self.state))
        self.assertEqual(
            [TextDelta("synthetic follow-up")], parse_stream_line(full_2, self.state)
        )

    def test_user_tool_result(self) -> None:
        events = parse_stream_line(
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_1",
                            "content": [{"type": "text", "text": '{"total": 12}'}],
                        }
                    ]
                },
            },
            self.state,
        )
        self.assertEqual(1, len(events))
        finished = events[0]
        assert isinstance(finished, ToolCallFinished)
        self.assertEqual("toolu_1", finished.call_id)
        self.assertTrue(finished.ok)

    def test_error_tool_result(self) -> None:
        events = parse_stream_line(
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_2",
                            "is_error": True,
                            "content": "boom",
                        }
                    ]
                },
            },
            self.state,
        )
        finished = events[0]
        assert isinstance(finished, ToolCallFinished)
        self.assertFalse(finished.ok)

    def test_result_success_is_done(self) -> None:
        events = parse_stream_line(
            {"type": "result", "subtype": "success", "session_id": "s2"},
            self.state,
        )
        self.assertEqual([Done()], events)
        self.assertEqual("s2", self.state.session_id)

    def test_result_error_emits_error_then_done(self) -> None:
        events = parse_stream_line(
            {
                "type": "result",
                "subtype": "error_during_execution",
                "is_error": True,
                "result": "something failed",
            },
            self.state,
        )
        self.assertEqual(2, len(events))
        self.assertIsInstance(events[0], ErrorEvent)
        self.assertIsInstance(events[1], Done)

    def test_unknown_lines_ignored(self) -> None:
        for obj in (
            {"type": "stream_event", "event": {"type": "message_stop"}},
            {"type": "whatever"},
            {},
        ):
            self.assertEqual([], parse_stream_line(obj, self.state))


class BuildCliArgsTest(unittest.TestCase):
    def test_flags_present(self) -> None:
        args = build_cli_args(
            cli_path="/usr/local/bin/claude",
            system_prompt="SYS",
            mcp_config_path="/tmp/cfg.json",
        )
        self.assertIn("--include-partial-messages", args)
        self.assertIn("--strict-mcp-config", args)
        self.assertIn("mcp__anki", args)
        self.assertNotIn("--resume", args)

    def test_resume_appended(self) -> None:
        args = build_cli_args(
            cli_path="claude",
            system_prompt="SYS",
            mcp_config_path="cfg",
            resume_session_id="sess-9",
        )
        self.assertEqual(["--resume", "sess-9"], args[-2:])


if __name__ == "__main__":
    unittest.main()
