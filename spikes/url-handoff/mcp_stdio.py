"""Minimal MCP stdio server for the URL-handoff spike.

Exposes one blocking tool: `propose_card` creates a proposal, hands the agent a
URL, and waits for a human to resolve it in a browser. This is the RFC 8628
device-grant shape -- the constrained host (a TUI) delegates the rich UI to a
browser and polls.

Deliberately stdlib-only and dependency-free so it runs identically under
Claude Code and Codex with no install step.

Register (Claude Code):
    claude mcp add anki-proposals -- python3 /abs/path/to/mcp_stdio.py

Register (Codex), in ~/.codex/config.toml:
    [mcp_servers.anki-proposals]
    command = "python3"
    args = ["/abs/path/to/mcp_stdio.py"]
"""

from __future__ import annotations

import json
import sys
from typing import Any

import server

PROTOCOL_VERSION = "2025-06-18"

TOOLS = [
    {
        "name": "propose_card",
        "description": (
            "Propose a new Anki note for human review. Returns a URL the human must open "
            "to approve, edit, or reject it. This call BLOCKS until they decide or it times "
            "out. Show the returned URL to the human verbatim. Nothing is written to any "
            "collection: this is a spike."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "deck": {"type": "string", "description": "Target deck name."},
                "front": {"type": "string", "description": "Front field."},
                "back": {"type": "string", "description": "Back field."},
                "timeout_seconds": {
                    "type": "number",
                    "description": "How long to wait for the human. Default 180.",
                },
            },
            "required": ["deck", "front", "back"],
        },
    },
    {
        "name": "check_proposal",
        "description": "Re-check a proposal's state by id, e.g. after propose_card timed out.",
        "inputSchema": {
            "type": "object",
            "properties": {"id": {"type": "string"}},
            "required": ["id"],
        },
    },
]

BASE_URL = ""


def _text(payload: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": payload}]}


def call_tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
    if name == "propose_card":
        p = server.STORE.create(args["deck"], args["front"], args["back"])
        url = f"{BASE_URL}/p/{p.id}"
        timeout = float(args.get("timeout_seconds", 180))
        server.STORE.wait(p.id, timeout)
        fresh = server.STORE.get(p.id)
        assert fresh is not None
        if fresh.state == server.PENDING:
            return _text(
                json.dumps(
                    {
                        "verdict": "timed_out",
                        "id": p.id,
                        "url": url,
                        "hint": "Still open. Ask the human to visit the URL, then call check_proposal.",
                    }
                )
            )
        return _text(json.dumps({"verdict": fresh.state, "url": url, **fresh.public()}, indent=2))

    if name == "check_proposal":
        found = server.STORE.get(args["id"])
        if found is None:
            return {**_text('{"error": "no such proposal"}'), "isError": True}
        return _text(json.dumps({"verdict": found.state, **found.public()}, indent=2))

    return {**_text(f'{{"error": "unknown tool {name}"}}'), "isError": True}


def handle(msg: dict[str, Any]) -> dict[str, Any] | None:
    method, mid = msg.get("method"), msg.get("id")

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": mid,
            "result": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "anki-proposals-spike", "version": "0.1.0"},
                "instructions": (
                    "When you propose a card, show the human the returned URL as a clickable link "
                    "and tell them you are waiting on it."
                ),
            },
        }

    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}}

    if method == "tools/call":
        params = msg.get("params") or {}
        try:
            result = call_tool(params.get("name", ""), params.get("arguments") or {})
        except Exception as exc:  # surface as a tool error, never crash the transport
            result = {**_text(f'{{"error": {json.dumps(str(exc))}}}'), "isError": True}
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    if mid is None:  # a notification; nothing to answer
        return None

    return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"no method {method}"}}


def main() -> None:
    global BASE_URL
    _, BASE_URL = server.start()
    print(f"proposal service on {BASE_URL}", file=sys.stderr, flush=True)

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        reply = handle(msg)
        if reply is not None:
            sys.stdout.write(json.dumps(reply) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
