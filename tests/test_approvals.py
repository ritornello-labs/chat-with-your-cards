"""ApprovalBroker: short-blocking Allow/Deny for ask-each-read."""

from __future__ import annotations

import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from chat_with_your_cards.approvals import (  # noqa: E402
    ALLOW,
    DENY,
    PENDING,
    ApprovalBroker,
)


class ApprovalBrokerTests(unittest.TestCase):
    def _broker(self, grace=5.0, ttl=300.0):
        pushed: list[dict] = []
        return ApprovalBroker(pushed.append, grace_s=grace, decision_ttl_s=ttl), pushed

    def _answer_from_thread(self, broker, pushed, allow: bool):
        def responder():
            # Wait for the request payload to appear, then answer it.
            for _ in range(200):
                requests = [p for p in pushed if p["type"] == "tool_approval"]
                if requests:
                    broker.respond({"id": requests[0]["id"], "allow": allow})
                    return
                time.sleep(0.01)

        thread = threading.Thread(target=responder)
        thread.start()
        return thread

    def test_allow_unblocks(self) -> None:
        broker, pushed = self._broker()
        thread = self._answer_from_thread(broker, pushed, True)
        result = broker.request("search_notes", '{"query": "x"}')
        thread.join()
        self.assertEqual(ALLOW, result)
        resolved = [p for p in pushed if p["type"] == "tool_approval_resolved"]
        self.assertTrue(resolved and resolved[0]["allow"])

    def test_deny_unblocks(self) -> None:
        broker, pushed = self._broker()
        thread = self._answer_from_thread(broker, pushed, False)
        result = broker.request("get_note", "{}")
        thread.join()
        self.assertEqual(DENY, result)

    # ---- the dogfood bug (2026-07-23) ----

    def test_unanswered_is_pending_not_denied(self) -> None:
        """The bug: an unanswered prompt came back as a denial, so the agent
        reported a refusal (and invented causes) while the chip sat waiting."""
        broker, pushed = self._broker(grace=0.05)
        self.assertEqual(PENDING, broker.request("search_notes", "{}"))

    def test_unanswered_chip_stays_answerable(self) -> None:
        """Giving up on the call must NOT resolve the chip - the user can still
        answer it, and that answer is what makes the retry work."""
        broker, pushed = self._broker(grace=0.05)
        broker.request("search_notes", "{}")
        self.assertEqual([], [p for p in pushed if p["type"] == "tool_approval_resolved"])
        requests = [p for p in pushed if p["type"] == "tool_approval"]
        self.assertEqual(1, len(requests))
        broker.respond({"id": requests[0]["id"], "allow": True})
        resolved = [p for p in pushed if p["type"] == "tool_approval_resolved"]
        self.assertTrue(resolved and resolved[0]["allow"])

    def test_late_answer_is_consumed_by_the_retry(self) -> None:
        broker, pushed = self._broker(grace=0.05)
        self.assertEqual(PENDING, broker.request("search_notes", '{"q": 1}'))
        requests = [p for p in pushed if p["type"] == "tool_approval"]
        broker.respond({"id": requests[0]["id"], "allow": True})
        # The retry proceeds immediately, without a second prompt.
        self.assertEqual(ALLOW, broker.request("search_notes", '{"q": 1}'))
        self.assertEqual(1, len([p for p in pushed if p["type"] == "tool_approval"]))

    def test_decision_is_one_shot(self) -> None:
        """Approving a call is not standing consent for the same call again."""
        broker, pushed = self._broker(grace=0.05)
        broker.request("get_note", "{}")
        requests = [p for p in pushed if p["type"] == "tool_approval"]
        broker.respond({"id": requests[0]["id"], "allow": True})
        self.assertEqual(ALLOW, broker.request("get_note", "{}"))
        # Second retry gets a fresh prompt rather than riding the old answer.
        self.assertEqual(PENDING, broker.request("get_note", "{}"))
        self.assertEqual(2, len([p for p in pushed if p["type"] == "tool_approval"]))

    def test_stale_decision_expires(self) -> None:
        broker, pushed = self._broker(grace=0.05, ttl=0.01)
        broker.request("get_note", "{}")
        requests = [p for p in pushed if p["type"] == "tool_approval"]
        broker.respond({"id": requests[0]["id"], "allow": True})
        time.sleep(0.05)
        self.assertEqual(PENDING, broker.request("get_note", "{}"))

    def test_retry_reuses_the_live_chip(self) -> None:
        """Retries used to stack a new identical prompt each time."""
        broker, pushed = self._broker(grace=0.05)
        for _ in range(3):
            self.assertEqual(PENDING, broker.request("search_notes", '{"q": "x"}'))
        self.assertEqual(1, len([p for p in pushed if p["type"] == "tool_approval"]))

    def test_different_args_get_their_own_chip(self) -> None:
        broker, pushed = self._broker(grace=0.05)
        broker.request("search_notes", '{"q": "a"}')
        broker.request("search_notes", '{"q": "b"}')
        self.assertEqual(2, len([p for p in pushed if p["type"] == "tool_approval"]))

    def test_unknown_response_ignored(self) -> None:
        broker, pushed = self._broker(grace=0.05)
        broker.respond({"id": "nope", "allow": True})  # no pending: no crash
        self.assertEqual(PENDING, broker.request("get_note", "{}"))

    def test_deny_all_unblocks_and_resolves(self) -> None:
        broker, pushed = self._broker(grace=5.0)
        results: list[str] = []

        def waiter():
            results.append(broker.request("search_notes", "{}"))

        thread = threading.Thread(target=waiter)
        thread.start()
        for _ in range(200):
            if any(p["type"] == "tool_approval" for p in pushed):
                break
            time.sleep(0.01)
        broker.deny_all()
        thread.join(timeout=2)
        self.assertEqual([DENY], results)
        resolved = [p for p in pushed if p["type"] == "tool_approval_resolved"]
        self.assertTrue(resolved and resolved[0]["reason"] == "session ended")

    def test_deny_all_does_not_leak_consent_into_the_next_session(self) -> None:
        broker, pushed = self._broker(grace=0.05)
        broker.request("get_note", "{}")
        requests = [p for p in pushed if p["type"] == "tool_approval"]
        broker.respond({"id": requests[0]["id"], "allow": True})
        broker.deny_all()
        self.assertEqual(PENDING, broker.request("get_note", "{}"))


if __name__ == "__main__":
    unittest.main()
