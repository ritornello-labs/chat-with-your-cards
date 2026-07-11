from __future__ import annotations

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

    def test_thinking_delta_with_text_emits_thinking_delta(self) -> None:
        # Anthropic's Messages API streams extended-thinking text via
        # delta.thinking (NOT delta.text like text_delta) - confirmed both
        # from the API docs and from a live Claude Code CLI capture below.
        events = parse_stream_line(
            {
                "type": "stream_event",
                "event": {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {
                        "type": "thinking_delta",
                        "thinking": "Let me work through this step by step...",
                    },
                },
            },
            self.state,
        )
        self.assertEqual(
            [ThinkingDelta("Let me work through this step by step...")], events
        )
        # Unlike text_delta, a thinking_delta must not touch the paragraph-
        # break bookkeeping used for the visible answer.
        self.assertEqual(0, self.state.turn_text_chars)
        self.assertEqual(0, self.state.streamed_chars)
        self.assertIsNone(self.state.last_block_index)

    def test_thinking_delta_with_empty_text_emits_estimated_tokens(self) -> None:
        # Real shape observed from the installed CLI (2.1.207, --effort max):
        # this account/tier returns thinking_delta events with an empty
        # "thinking" string throughout (only signature_delta carries opaque,
        # encrypted content) - i.e. thinking is redacted, not absent. The
        # parser must still surface these (empty text, live estimated_tokens)
        # so the UI can show a "Thinking..." indicator - dropping them (the
        # old behavior) left the UI with no signal that thinking was
        # happening at all (DESIGN.md section 9).
        events = parse_stream_line(
            {
                "type": "stream_event",
                "event": {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {
                        "type": "thinking_delta",
                        "thinking": "",
                        "estimated_tokens": 50,
                    },
                },
            },
            self.state,
        )
        self.assertEqual([ThinkingDelta("", 50)], events)

    def test_thinking_content_block_start_opens_indicator(self) -> None:
        # Opens the UI's thinking state immediately, before any delta with a
        # token estimate has arrived - text and estimated_tokens both blank.
        events = parse_stream_line(
            {
                "type": "stream_event",
                "event": {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "thinking", "thinking": "", "signature": ""},
                },
            },
            self.state,
        )
        self.assertEqual([ThinkingDelta("", None)], events)

    def test_non_thinking_content_block_start_emits_nothing(self) -> None:
        events = parse_stream_line(
            {
                "type": "stream_event",
                "event": {
                    "type": "content_block_start",
                    "index": 1,
                    "content_block": {"type": "text", "text": ""},
                },
            },
            self.state,
        )
        self.assertEqual([], events)

    def test_signature_delta_emits_nothing(self) -> None:
        events = parse_stream_line(
            {
                "type": "stream_event",
                "event": {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "signature_delta", "signature": "opaque-token"},
                },
            },
            self.state,
        )
        self.assertEqual([], events)

    def test_thinking_between_text_blocks_does_not_glue_or_corrupt_breaks(self) -> None:
        # text (index 0) -> thinking (index 1, interleaved) -> text (index 2).
        # The thinking block must not be glued into the surrounding text, and
        # must not prevent the paragraph break the index change earns.
        text0 = {
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "First part."},
            },
        }
        thinking1 = {
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "index": 1,
                "delta": {"type": "thinking_delta", "thinking": "pondering..."},
            },
        }
        text2 = {
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "index": 2,
                "delta": {"type": "text_delta", "text": "Second part."},
            },
        }
        self.assertEqual([TextDelta("First part.")], parse_stream_line(text0, self.state))
        self.assertEqual(
            [ThinkingDelta("pondering...")], parse_stream_line(thinking1, self.state)
        )
        # last_block_index is still 0 (thinking deltas never update it), so
        # index 2 != 0 correctly triggers a paragraph break here.
        self.assertEqual(
            [TextDelta("\n\nSecond part.")], parse_stream_line(text2, self.state)
        )

    def test_full_assistant_message_thinking_block_not_resurfaced(self) -> None:
        # The full "assistant" message repeats every content block, including
        # thinking ones. Re-surfacing it would double-emit (if streamed) or
        # leak redacted thinking text into the visible answer.
        events = parse_stream_line(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "thinking", "thinking": "", "signature": "tok"},
                        {"type": "text", "text": "The answer is 42."},
                    ]
                },
            },
            self.state,
        )
        self.assertEqual([TextDelta("The answer is 42.")], events)

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
        # A second message's text in the same turn is separated from the first
        # by a paragraph break, not glued ("part one" + "synthetic follow-up").
        self.assertEqual(
            [TextDelta("\n\nsynthetic follow-up")],
            parse_stream_line(full_2, self.state),
        )

    def test_second_streamed_message_gets_paragraph_break(self) -> None:
        # Two assistant messages stream in one turn with no tool call between;
        # without a separator their text glued as "…style.Let me…" (2026-07-06).
        first_delta = {
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "…to match style."},
            },
        }
        first_full = {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "…to match style."}]},
        }
        second_delta = {
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "Let me look."},
            },
        }
        self.assertEqual(
            [TextDelta("…to match style.")],
            parse_stream_line(first_delta, self.state),
        )
        self.assertEqual([], parse_stream_line(first_full, self.state))
        self.assertEqual(
            [TextDelta("\n\nLet me look.")],
            parse_stream_line(second_delta, self.state),
        )

    def test_new_content_block_in_one_message_separates(self) -> None:
        # Two text content blocks in a single streamed message (index 0 then 1)
        # must not run together.
        block0 = {
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "First para."},
            },
        }
        block1 = {
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "index": 1,
                "delta": {"type": "text_delta", "text": "Second para."},
            },
        }
        self.assertEqual([TextDelta("First para.")], parse_stream_line(block0, self.state))
        self.assertEqual(
            [TextDelta("\n\nSecond para.")], parse_stream_line(block1, self.state)
        )

    def test_first_text_of_turn_has_no_leading_break(self) -> None:
        # After a turn ends, the next turn's opening text is not prefixed.
        delta = {
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "Hi."},
            },
        }
        parse_stream_line(delta, self.state)
        parse_stream_line({"type": "result", "subtype": "success"}, self.state)
        self.assertEqual([TextDelta("Hi.")], parse_stream_line(delta, self.state))

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

    def test_result_with_usage_emits_usage_update(self) -> None:
        from chat_with_your_cards.backends import UsageUpdate

        events = parse_stream_line(
            {
                "type": "result",
                "subtype": "success",
                "total_cost_usd": 0.0421,
                "usage": {"input_tokens": 12000, "output_tokens": 450},
            },
            self.state,
        )
        self.assertEqual(2, len(events))
        usage = events[0]
        assert isinstance(usage, UsageUpdate)
        self.assertAlmostEqual(0.0421, usage.cost_usd or 0)
        self.assertEqual(12000, usage.input_tokens)
        self.assertEqual(450, usage.output_tokens)
        self.assertIsInstance(events[1], Done)

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
            # control_response is intercepted by ClaudeCliSession._read_loop
            # before parse_stream_line ever sees it; asserted here too as
            # defense in depth - it must be inert if it somehow arrived.
            {
                "type": "control_response",
                "response": {"subtype": "success", "request_id": "req_1_x"},
            },
        ):
            self.assertEqual([], parse_stream_line(obj, self.state))

    def test_interrupted_turn_marker_message_is_silent(self) -> None:
        # Real shape (captured live, CLI 2.1.207): after a successful
        # interrupt, the CLI emits a synthetic "user" message narrating the
        # abort instead of a tool_result. It must not surface as a tool
        # event or any other UI-visible event.
        events = parse_stream_line(
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "[Request interrupted by user]"}
                    ],
                },
            },
            self.state,
        )
        self.assertEqual([], events)

    # -- Real captured stream fixtures --------------------------------
    #
    # Both sequences below are sanitized (session ids replaced, the
    # signature_delta's opaque base64 payload truncated) but otherwise
    # verbatim captures from the real installed CLI (2.1.207), run with the
    # exact flags build_cli_args() produces, on 2026-07-11.

    THINKING_BEARING_STREAM: list[dict] = [
        # `claude -p --output-format stream-json --include-partial-messages
        #  --verbose --effort max` on "A farmer has chickens and rabbits...".
        # This account/CLI redacts thinking text (empty "thinking" fields
        # throughout, real content only in the opaque signature), so every
        # ThinkingDelta below carries empty text - see
        # test_thinking_delta_with_empty_text_emits_estimated_tokens for that
        # in isolation. This sequence exercises the full real ordering:
        # thinking block open (content_block_start) + two growing-estimate
        # deltas + close, THEN a text block, with no corruption of the
        # text's paragraph bookkeeping.
        {
            "type": "stream_event",
            "event": {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "thinking", "thinking": "", "signature": ""},
            },
        },
        {
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "index": 0,
                "delta": {
                    "type": "thinking_delta",
                    "thinking": "",
                    "estimated_tokens": 50,
                },
            },
        },
        {
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "index": 0,
                "delta": {
                    "type": "thinking_delta",
                    "thinking": "",
                    "estimated_tokens": 200,
                },
            },
        },
        {
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "index": 0,
                "delta": {
                    "type": "signature_delta",
                    "signature": "EtQGCokBCA8YAipANMHOF5d7Blgs90F0tF...(truncated)",
                },
            },
        },
        {"type": "stream_event", "event": {"type": "content_block_stop", "index": 0}},
        {
            "type": "stream_event",
            "event": {
                "type": "content_block_start",
                "index": 1,
                "content_block": {"type": "text", "text": ""},
            },
        },
        {
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "index": 1,
                "delta": {"type": "text_delta", "text": "**"},
            },
        },
        {
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "index": 1,
                "delta": {
                    "type": "text_delta",
                    "text": "Setting up the equations**\n\nLet c = number of "
                    "chickens, r = number of rabbits.",
                },
            },
        },
        {"type": "stream_event", "event": {"type": "content_block_stop", "index": 1}},
    ]

    INTERRUPT_SEQUENCE: list[dict] = [
        # Same live CLI, "Write 300 words about oak trees." interrupted ~2s
        # in via a control_request over stdin (see claude_cli.py's
        # interrupt()). Captured with a raw subprocess (not through
        # ClaudeCliSession) to see the unfiltered wire bytes; the
        # control_response line itself is consumed by _read_loop before
        # parse_stream_line, so it is omitted from what's fed below (see
        # test_unknown_lines_ignored for its own inertness).
        {
            "type": "system",
            "subtype": "init",
            "session_id": "sess-interrupt-demo",
        },
        {"type": "system", "subtype": "status", "status": "requesting"},
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [{"type": "text", "text": "[Request interrupted by user]"}],
            },
            "session_id": "sess-interrupt-demo",
        },
        {
            "type": "result",
            "subtype": "error_during_execution",
            "is_error": True,
            "session_id": "sess-interrupt-demo",
            "total_cost_usd": 0,
            "usage": {"input_tokens": 0, "output_tokens": 0},
            "terminal_reason": "aborted_streaming",
        },
    ]

    def test_real_thinking_bearing_stream_isolates_thinking_from_text(self) -> None:
        events: list = []
        for obj in self.THINKING_BEARING_STREAM:
            events.extend(parse_stream_line(obj, self.state))
        # Redacted (empty) thinking text still yields ThinkingDelta events -
        # one opening the indicator (content_block_start) and one per
        # growing estimated_tokens delta - interleaved with, but never
        # corrupting, the visible answer's two real TextDelta chunks (no
        # spurious break glued in front - first text of the turn - and none
        # between them - same content-block index).
        self.assertEqual(
            [
                ThinkingDelta("", None),
                ThinkingDelta("", 50),
                ThinkingDelta("", 200),
                TextDelta("**"),
                TextDelta(
                    "Setting up the equations**\n\nLet c = number of chickens, "
                    "r = number of rabbits."
                ),
            ],
            events,
        )

    def test_real_interrupt_sequence_ends_turn_cleanly(self) -> None:
        from chat_with_your_cards.backends import UsageUpdate

        events: list = []
        for obj in self.INTERRUPT_SEQUENCE:
            events.extend(parse_stream_line(obj, self.state))
        self.assertEqual(3, len(events))
        self.assertIsInstance(events[0], UsageUpdate)
        error = events[1]
        assert isinstance(error, ErrorEvent)
        self.assertEqual("error_during_execution", error.message)
        self.assertIsInstance(events[2], Done)
        self.assertEqual("sess-interrupt-demo", self.state.session_id)


class BuildCliArgsTest(unittest.TestCase):
    def test_flags_present(self) -> None:
        args = build_cli_args(
            cli_path="/usr/local/bin/claude",
            system_prompt="SYS",
            mcp_config_path="/tmp/cfg.json",
        )
        self.assertIn("--include-partial-messages", args)
        self.assertIn("--strict-mcp-config", args)
        allowed = args[args.index("--allowedTools") + 1]
        self.assertIn("mcp__anki", allowed)
        self.assertNotIn("--resume", args)

    def test_resume_appended(self) -> None:
        args = build_cli_args(
            cli_path="claude",
            system_prompt="SYS",
            mcp_config_path="cfg",
            resume_session_id="sess-9",
        )
        self.assertEqual(["--resume", "sess-9"], args[-2:])

    def test_model_and_effort_flags(self) -> None:
        args = build_cli_args(
            cli_path="claude",
            system_prompt="SYS",
            mcp_config_path="cfg",
            model="opus",
            effort="high",
        )
        self.assertEqual("opus", args[args.index("--model") + 1])
        self.assertEqual("high", args[args.index("--effort") + 1])

    def test_blank_model_effort_and_invalid_effort_omitted(self) -> None:
        args = build_cli_args(
            cli_path="claude",
            system_prompt="SYS",
            mcp_config_path="cfg",
            model="  ",
            effort="turbo",  # not a valid level
        )
        self.assertNotIn("--model", args)
        self.assertNotIn("--effort", args)


if __name__ == "__main__":
    unittest.main()
