"""Ask-each-read: per-call approval with a short-blocking broker (DESIGN.md §5).

In ask-each-read mode every read tool call renders an inline Allow/Deny chip.
The MCP request blocks only briefly (APPROVAL_GRACE_S) - long enough that a
user who is watching just clicks and the call proceeds with no interruption -
and otherwise returns PENDING so the caller can fail fast with an accurate
message.

Why not simply block until the user answers: the MCP *client* has its own tool
timeout, shorter than any patient wait we might choose. Blocking past it does
not buy patience, it just gets the call killed somewhere we cannot see - and
the agent, handed a bare timeout, invents a cause. That is exactly what
happened in dogfooding: four calls "timed out" and the agent told the user
their collection was busy mid-sync when in truth four approval prompts were
sitting unanswered on screen.

So instead the chip STAYS LIVE after the grace expires, and the answer is
remembered (once, briefly) as a decision: whenever the user does click, the
agent's next attempt at the same call consumes that decision and proceeds
without re-prompting. Retries of a call that is still awaiting an answer
re-use the live chip rather than stacking duplicates.

Blocking at all is safe because tool calls arrive on the MCP server's
per-connection threads, never on Anki's main thread; the UI push is marshaled
to the main thread by the injected callable.

Writes are not double-gated here: they are already user-reviewed through
the proposal flow.

aqt-free for unit testing.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable

# How long a tool call blocks waiting for a click before giving up its slot.
# Must stay comfortably under the MCP client's own tool timeout.
APPROVAL_GRACE_S = 10.0

# How long an answer given AFTER the call gave up stays consumable by a retry.
# One-shot and short: approving a call is not standing consent for the same
# call an hour later, which would quietly defeat the whole mode.
DECISION_TTL_S = 300.0

ALLOW = "allow"
DENY = "deny"
PENDING = "pending"


class ApprovalBroker:
    def __init__(
        self,
        push_on_main: Callable[[dict[str, Any]], None],
        grace_s: float = APPROVAL_GRACE_S,
        decision_ttl_s: float = DECISION_TTL_S,
    ) -> None:
        self._push_on_main = push_on_main
        self._grace_s = grace_s
        self._decision_ttl_s = decision_ttl_s
        self._lock = threading.Lock()
        self._counter = 0
        self._pending: dict[str, dict[str, Any]] = {}
        self._by_key: dict[str, str] = {}  # request key -> live approval id
        self._decided: dict[str, tuple[bool, float]] = {}  # key -> (allow, at)

    @staticmethod
    def _key(tool: str, summary: str) -> str:
        return f"{tool}\x00{summary}"

    def _take_decision(self, key: str) -> bool | None:
        """Consume a decision for `key` if one is fresh. Caller holds the lock."""
        decided = self._decided.pop(key, None)
        if decided is None:
            return None
        allow, at = decided
        if time.monotonic() - at > self._decision_ttl_s:
            return None
        return allow

    def request(self, tool: str, summary: str) -> str:
        """Called on the MCP thread. Returns ALLOW, DENY, or PENDING.

        PENDING means "no answer yet" - NOT a denial. The chip stays live and
        the answer will be waiting for the next attempt.
        """
        key = self._key(tool, summary)
        with self._lock:
            decided = self._take_decision(key)
            if decided is not None:
                return ALLOW if decided else DENY

            approval_id = self._by_key.get(key)
            entry = self._pending.get(approval_id) if approval_id else None
            if entry is None:
                # No live chip for this exact call: make one.
                self._counter += 1
                approval_id = f"a{self._counter}"
                entry = {"event": threading.Event(), "key": key}
                self._pending[approval_id] = entry
                self._by_key[key] = approval_id
                push = {
                    "type": "tool_approval",
                    "id": approval_id,
                    "tool": tool,
                    "summary": summary,
                }
            else:
                # A retry of a call the user has not answered yet: wait on the
                # SAME chip instead of stacking another identical prompt.
                push = None

        if push is not None:
            self._push_on_main(push)

        # Track live waiters so respond() can tell whether the answer arrived
        # in time to be used, or after the call had already given up (in which
        # case the chip says so, instead of implying the agent carried on).
        with self._lock:
            entry["waiters"] = int(entry.get("waiters", 0)) + 1
        entry["event"].wait(self._grace_s)

        with self._lock:
            entry["waiters"] = max(0, int(entry.get("waiters", 1)) - 1)
            decided = self._take_decision(key)
        if decided is None:
            # Grace expired. Deliberately leave the chip pending and un-resolved
            # so the user can still answer it; the caller reports "waiting on
            # you" rather than pretending this was a refusal.
            return PENDING
        return ALLOW if decided else DENY

    def respond(self, msg: dict[str, Any]) -> None:
        """Bridge entry point (main thread): the user clicked Allow/Deny."""
        approval_id = str(msg.get("id", ""))
        allow = bool(msg.get("allow"))
        with self._lock:
            entry = self._pending.pop(approval_id, None)
            if entry is None:
                return
            key = str(entry["key"])
            self._by_key.pop(key, None)
            # Recorded even when a waiter is still inside its grace window: the
            # waiter consumes it on wake, and if it already gave up the next
            # attempt consumes it instead.
            self._decided[key] = (allow, time.monotonic())
            # Nobody still waiting = the call already gave up its slot, so this
            # answer cannot resume it; only the NEXT attempt can consume it.
            late = int(entry.get("waiters", 0)) == 0
            event: threading.Event = entry["event"]
            event.set()
        self._push_on_main(
            {
                "type": "tool_approval_resolved",
                "id": approval_id,
                "allow": allow,
                "late": late,
            }
        )

    def deny_all(self) -> None:
        """Session teardown: unblock any waiting tool calls as denied and
        resolve their chips, so nothing is left looking answerable."""
        with self._lock:
            pending = list(self._pending.items())
            self._pending.clear()
            self._by_key.clear()
            # Drop remembered answers too: a decision must never survive into
            # the next session and silently approve something there.
            self._decided.clear()
            for _approval_id, entry in pending:
                self._decided[str(entry["key"])] = (False, time.monotonic())
                pending_event: threading.Event = entry["event"]
                pending_event.set()
        for approval_id, _entry in pending:
            self._push_on_main(
                {
                    "type": "tool_approval_resolved",
                    "id": approval_id,
                    "allow": False,
                    "reason": "session ended",
                }
            )
