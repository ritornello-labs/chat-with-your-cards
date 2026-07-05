"""Ask-each-read: per-call approval with a blocking broker (DESIGN.md §5).

In ask-each-read mode every read tool call renders an inline Allow/Deny
chip and the MCP request BLOCKS until the user decides (or the timeout
denies). Blocking is safe because tool calls arrive on the MCP server's
per-connection threads, never on Anki's main thread; the UI push is
marshaled to the main thread by the injected callable.

Writes are not double-gated here: they are already user-reviewed through
the proposal flow.

aqt-free for unit testing.
"""

from __future__ import annotations

import threading
from typing import Any, Callable

APPROVAL_TIMEOUT_S = 120


class ApprovalBroker:
    def __init__(
        self,
        push_on_main: Callable[[dict[str, Any]], None],
        timeout_s: float = APPROVAL_TIMEOUT_S,
    ) -> None:
        self._push_on_main = push_on_main
        self._timeout_s = timeout_s
        self._lock = threading.Lock()
        self._counter = 0
        self._pending: dict[str, dict[str, Any]] = {}

    def request(self, tool: str, summary: str) -> bool:
        """Called on the MCP thread; blocks until the user answers.

        Returns True only on an explicit Allow; timeout or Deny is False.
        """
        event = threading.Event()
        with self._lock:
            self._counter += 1
            approval_id = f"a{self._counter}"
            entry: dict[str, Any] = {"event": event, "allow": False}
            self._pending[approval_id] = entry
        self._push_on_main(
            {
                "type": "tool_approval",
                "id": approval_id,
                "tool": tool,
                "summary": summary,
            }
        )
        answered = event.wait(self._timeout_s)
        with self._lock:
            self._pending.pop(approval_id, None)
        if not answered:
            self._push_on_main(
                {
                    "type": "tool_approval_resolved",
                    "id": approval_id,
                    "allow": False,
                    "reason": "timed out",
                }
            )
            return False
        return bool(entry["allow"])

    def respond(self, msg: dict[str, Any]) -> None:
        """Bridge entry point (main thread): the user clicked Allow/Deny."""
        approval_id = str(msg.get("id", ""))
        allow = bool(msg.get("allow"))
        with self._lock:
            entry = self._pending.get(approval_id)
            if entry is None:
                return
            entry["allow"] = allow
            event: threading.Event = entry["event"]
            event.set()
        self._push_on_main(
            {"type": "tool_approval_resolved", "id": approval_id, "allow": allow}
        )

    def deny_all(self) -> None:
        """Session teardown: unblock any waiting tool calls as denied."""
        with self._lock:
            for entry in self._pending.values():
                entry["allow"] = False
                pending_event: threading.Event = entry["event"]
                pending_event.set()
            self._pending.clear()
