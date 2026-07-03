from __future__ import annotations

import json
import sys
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from chat_with_your_cards.mcp_server import McpServer, tool_specs_for_mcp  # noqa: E402
from chat_with_your_cards.tools import ToolSpec  # noqa: E402

SPECS = tool_specs_for_mcp(
    [
        ToolSpec(
            "echo",
            "Echo the input back.",
            {"type": "object", "properties": {"text": {"type": "string"}}},
            lambda ctx, args: args,
        )
    ]
)


def _execute(name: str, args: dict[str, Any]) -> Any:
    if name == "echo":
        return {"echoed": args.get("text", "")}
    if name == "boom":
        raise RuntimeError("kaboom")
    raise KeyError(f"unknown tool: {name}")


class McpServerTest(unittest.TestCase):
    server: McpServer

    @classmethod
    def setUpClass(cls) -> None:
        cls.server = McpServer(tool_specs=SPECS, execute_tool=_execute)
        cls.server.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.stop()

    def _post(
        self,
        payload: dict[str, Any],
        *,
        token: str | None = None,
    ) -> tuple[int, dict[str, Any] | None]:
        request = urllib.request.Request(
            self.server.url,
            data=json.dumps(payload).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token or self.server.token}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                body = response.read()
                return response.status, json.loads(body) if body else None
        except urllib.error.HTTPError as error:
            return error.code, None

    def test_rejects_missing_or_wrong_token(self) -> None:
        status, _ = self._post({"jsonrpc": "2.0", "id": 1, "method": "ping"}, token="nope")
        self.assertEqual(401, status)

    def test_abrupt_connection_reset_is_swallowed(self) -> None:
        # The CLI's HTTP client resets idle connections; the server must not
        # dump a ConnectionResetError traceback (Anki error report 2026-07-03).
        # Connect, send a partial request line, then reset via SO_LINGER=0.
        import socket
        import struct
        import urllib.parse

        parsed = urllib.parse.urlparse(self.server.url)
        sock = socket.create_connection((parsed.hostname, parsed.port), timeout=5)
        sock.sendall(b"POST /mcp HTTP/1.1\r\n")
        sock.setsockopt(
            socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0)
        )
        sock.close()
        # The server must still serve subsequent requests normally.
        status, body = self._post({"jsonrpc": "2.0", "id": 99, "method": "ping"})
        self.assertEqual(200, status)
        assert body is not None
        self.assertEqual({}, body["result"])

    def test_initialize_echoes_protocol_version(self) -> None:
        status, body = self._post(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2025-03-26", "capabilities": {}},
            }
        )
        self.assertEqual(200, status)
        assert body is not None
        self.assertEqual("2025-03-26", body["result"]["protocolVersion"])
        self.assertIn("tools", body["result"]["capabilities"])

    def test_notification_returns_202(self) -> None:
        status, body = self._post(
            {"jsonrpc": "2.0", "method": "notifications/initialized"}
        )
        self.assertEqual(202, status)
        self.assertIsNone(body)

    def test_tools_list(self) -> None:
        status, body = self._post({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        self.assertEqual(200, status)
        assert body is not None
        tools = body["result"]["tools"]
        self.assertEqual(["echo"], [t["name"] for t in tools])
        self.assertIn("inputSchema", tools[0])

    def test_tools_call_success(self) -> None:
        status, body = self._post(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "echo", "arguments": {"text": "hi"}},
            }
        )
        self.assertEqual(200, status)
        assert body is not None
        result = body["result"]
        self.assertFalse(result["isError"])
        self.assertEqual({"echoed": "hi"}, json.loads(result["content"][0]["text"]))

    def test_tools_call_error_is_soft(self) -> None:
        status, body = self._post(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {"name": "boom", "arguments": {}},
            }
        )
        self.assertEqual(200, status)
        assert body is not None
        result = body["result"]
        self.assertTrue(result["isError"])
        self.assertIn("kaboom", result["content"][0]["text"])

    def test_unknown_method_is_json_rpc_error(self) -> None:
        status, body = self._post({"jsonrpc": "2.0", "id": 5, "method": "wat"})
        self.assertEqual(200, status)
        assert body is not None
        self.assertEqual(-32601, body["error"]["code"])


if __name__ == "__main__":
    unittest.main()
