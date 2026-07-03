"""Minimal MCP server over localhost HTTP.

MCP here is only the wire protocol between the CLI agent subprocess and
the add-on's in-process tools (DESIGN.md section 2). Hand-rolled
JSON-RPC over http.server: no dependencies, AnkiWeb-friendly, aqt-free
(tool execution is an injected callable; the add-on glue marshals it
onto Anki's main thread).

Security: binds 127.0.0.1 on a random port; every request must carry
the per-session bearer token; the server dies with the session.
"""

from __future__ import annotations

import json
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "chat-with-your-cards"
SERVER_VERSION = "0.1.0"

# (name, arguments) -> JSON-serializable result; raise for tool errors.
ToolExecutor = Callable[[str, dict[str, Any]], Any]


class McpServer:
    def __init__(
        self,
        *,
        tool_specs: list[dict[str, Any]],
        execute_tool: ToolExecutor,
        token: str | None = None,
    ) -> None:
        self._tool_specs = tool_specs
        self._execute_tool = execute_tool
        self.token = token or secrets.token_hex(16)
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        assert self._httpd is not None, "server not started"
        return f"http://127.0.0.1:{self._httpd.server_address[1]}/mcp"

    def start(self) -> None:
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt: str, *args: Any) -> None:  # silence
                pass

            def _send(self, status: int, payload: dict[str, Any] | None) -> None:
                body = b"" if payload is None else json.dumps(payload).encode()
                self.send_response(status)
                if body:
                    self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if body:
                    self.wfile.write(body)

            def _authorized(self) -> bool:
                header = self.headers.get("Authorization", "")
                return header == f"Bearer {outer.token}"

            def do_GET(self) -> None:  # noqa: N802
                # No server-initiated stream support; clients fall back to POST.
                self._send(405, None)

            def do_DELETE(self) -> None:  # noqa: N802
                self._send(200, None)

            def do_POST(self) -> None:  # noqa: N802
                if not self._authorized():
                    self._send(401, None)
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    message = json.loads(self.rfile.read(length) or b"{}")
                except (ValueError, json.JSONDecodeError):
                    self._send(400, None)
                    return
                response = outer._handle_message(message)
                if response is None:
                    self._send(202, None)
                else:
                    self._send(200, response)

        class Server(ThreadingHTTPServer):
            def handle_error(self, request: Any, client_address: Any) -> None:
                # The CLI's streamable-HTTP client resets idle connections
                # instead of closing them cleanly; that surfaces here as a
                # ConnectionResetError/BrokenPipeError per socket. It is
                # benign - swallow it rather than dumping a traceback into
                # Anki's error log. Anything else still propagates.
                import sys

                exc = sys.exc_info()[1]
                if isinstance(exc, (ConnectionResetError, BrokenPipeError)):
                    return
                super().handle_error(request, client_address)

        self._httpd = Server(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, name="cwyc-mcp", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        self._thread = None

    # ---- JSON-RPC ----

    def _handle_message(self, message: dict[str, Any]) -> dict[str, Any] | None:
        if "id" not in message:  # notification
            return None
        request_id = message["id"]
        method = message.get("method", "")
        params = message.get("params") or {}
        try:
            result = self._dispatch(method, params)
        except _MethodNotFound:
            return _error(request_id, -32601, f"method not found: {method}")
        except Exception as exc:
            return _error(request_id, -32603, str(exc))
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    def _dispatch(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "initialize":
            requested = params.get("protocolVersion", PROTOCOL_VERSION)
            return {
                "protocolVersion": requested,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            }
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": self._tool_specs}
        if method == "tools/call":
            name = params.get("name", "")
            arguments = params.get("arguments") or {}
            try:
                result = self._execute_tool(name, arguments)
            except Exception as exc:
                return {
                    "content": [{"type": "text", "text": f"Tool error: {exc}"}],
                    "isError": True,
                }
            return {
                "content": [
                    {"type": "text", "text": json.dumps(result, ensure_ascii=False)}
                ],
                "isError": False,
            }
        raise _MethodNotFound(method)


class _MethodNotFound(Exception):
    pass


def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }


def tool_specs_for_mcp(specs: list[Any]) -> list[dict[str, Any]]:
    """Convert registry ToolSpecs into MCP tools/list entries."""
    return [
        {
            "name": spec.name,
            "description": spec.description,
            "inputSchema": spec.input_schema,
        }
        for spec in specs
    ]
