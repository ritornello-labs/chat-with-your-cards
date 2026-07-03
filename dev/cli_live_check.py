#!/usr/bin/env python3
"""Live end-to-end check of the Claude CLI backend, without Anki.

Starts the real McpServer with canned tools, spawns the real claude CLI
through ClaudeCliBackend, sends one message that requires a tool call,
and asserts the full event flow: TextDelta(s), a matched tool call pair
against OUR server, and Done. Costs a few tokens; run manually:

    uv run python dev/cli_live_check.py
"""

from __future__ import annotations

import queue
import sys
import tempfile
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
    ClaudeCliBackend,
    find_claude_cli,
)
from chat_with_your_cards.mcp_server import McpServer  # noqa: E402

FAKE_DECKS = {
    "available": True,
    "decks": [
        {"name": "Spanish", "notes": 420, "cards": 900},
        {"name": "Spanish::Verbs", "notes": 120, "cards": 240},
        {"name": "Math::Analysis", "notes": 88, "cards": 176},
    ],
}

TOOL_SPECS = [
    {
        "name": "deck_tree",
        "description": "Full deck list annotated with note/card counts.",
        "inputSchema": {"type": "object", "properties": {}},
    }
]

calls: list[str] = []


def execute_tool(name: str, args: dict) -> dict:
    calls.append(name)
    if name == "deck_tree":
        return FAKE_DECKS
    raise KeyError(name)


def main() -> int:
    cli = find_claude_cli()
    if cli is None:
        print("SKIP: claude CLI not found")
        return 1
    print(f"claude CLI: {cli}")

    server = McpServer(tool_specs=TOOL_SPECS, execute_tool=execute_tool)
    server.start()
    print(f"mcp server: {server.url}")

    events: queue.Queue = queue.Queue()
    backend = ClaudeCliBackend(
        cli_path=cli,
        system_prompt_builder=lambda: (
            "You are a test harness assistant. Use the anki MCP tools to "
            "answer. Reply in one short sentence."
        ),
        mcp_url=server.url,
        mcp_token=server.token,
        run_on_ui=lambda fn: fn(),  # deliver on reader thread; fine here
        workdir=Path(tempfile.mkdtemp(prefix="cwyc-live-")),
    )
    session = backend.start_session({})
    session.send(
        "Call the deck_tree tool and tell me how many notes the Spanish deck has.",
        events.put,
    )

    text = ""
    seen: list[str] = []
    try:
        while True:
            event = events.get(timeout=120)
            seen.append(type(event).__name__)
            if isinstance(event, TextDelta):
                text += event.text
            elif isinstance(event, (ToolCallStarted, ToolCallFinished)):
                print(f"  {event}")
            elif isinstance(event, ErrorEvent):
                print(f"  ERROR event: {event.message}")
            if isinstance(event, Done):
                break
    except queue.Empty:
        print("FAIL: timed out waiting for events")
        return 1
    finally:
        session.close()
        server.stop()

    print(f"assistant text: {text.strip()!r}")
    print(f"events: {seen}")
    print(f"server-side tool calls: {calls}")

    ok = (
        "TextDelta" in seen
        and "ToolCallStarted" in seen
        and "ToolCallFinished" in seen
        and calls == ["deck_tree"]
        and "420" in text
        and "ErrorEvent" not in seen
    )
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
