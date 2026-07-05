"""ApprovalBroker: blocking Allow/Deny for ask-each-read."""

from __future__ import annotations

import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from chat_with_your_cards.approvals import ApprovalBroker  # noqa: E402


class ApprovalBrokerTests(unittest.TestCase):
    def _broker(self, timeout=5.0):
        pushed: list[dict] = []
        return ApprovalBroker(pushed.append, timeout_s=timeout), pushed

    def _answer_from_thread(self, broker, pushed, allow: bool):
        def responder():
            # Wait for the request payload to appear, then answer it.
            import time

            for _ in range(200):
                requests = [p for p in pushed if p["type"] == "tool_approval"]
                if requests:
                    broker.respond({"id": requests[0]["id"], "allow": allow})
                    return
                time.sleep(0.01)

        thread = threading.Thread(target=responder)
        thread.start()
        return thread

    def test_allow_unblocks_true(self) -> None:
        broker, pushed = self._broker()
        thread = self._answer_from_thread(broker, pushed, True)
        result = broker.request("search_notes", '{"query": "x"}')
        thread.join()
        self.assertTrue(result)
        resolved = [p for p in pushed if p["type"] == "tool_approval_resolved"]
        self.assertTrue(resolved and resolved[0]["allow"])

    def test_deny_unblocks_false(self) -> None:
        broker, pushed = self._broker()
        thread = self._answer_from_thread(broker, pushed, False)
        result = broker.request("get_note", "{}")
        thread.join()
        self.assertFalse(result)

    def test_timeout_denies_and_reports(self) -> None:
        broker, pushed = self._broker(timeout=0.05)
        result = broker.request("search_notes", "{}")
        self.assertFalse(result)
        resolved = [p for p in pushed if p["type"] == "tool_approval_resolved"]
        self.assertTrue(resolved and resolved[0]["reason"] == "timed out")

    def test_unknown_response_ignored(self) -> None:
        broker, pushed = self._broker(timeout=0.05)
        broker.respond({"id": "nope", "allow": True})  # no pending: no crash
        self.assertFalse(broker.request("get_note", "{}"))

    def test_deny_all_unblocks_waiters(self) -> None:
        broker, pushed = self._broker(timeout=5.0)
        results: list[bool] = []

        def waiter():
            results.append(broker.request("search_notes", "{}"))

        thread = threading.Thread(target=waiter)
        thread.start()
        import time

        for _ in range(200):
            if any(p["type"] == "tool_approval" for p in pushed):
                break
            time.sleep(0.01)
        broker.deny_all()
        thread.join(timeout=2)
        self.assertEqual([False], results)


if __name__ == "__main__":
    unittest.main()
