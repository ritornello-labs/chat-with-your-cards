"""TranscriptStore: recording, flushing, listing, resume metadata."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from chat_with_your_cards.transcripts import TranscriptStore  # noqa: E402


def make_store():
    clock = {"t": 1000.0}

    def now() -> float:
        clock["t"] += 1
        return clock["t"]

    return TranscriptStore(Path(tempfile.mkdtemp(prefix="cwyc-tr-")), now=now), clock


class TranscriptStoreTests(unittest.TestCase):
    def test_records_only_replayable_types_and_titles_from_first_message(self) -> None:
        store, _ = make_store()
        store.record({"type": "context", "label": "x"})  # transient: dropped
        store.record({"type": "user_message", "text": "Explain limits to me"})
        store.record({"type": "assistant_text", "text": "Sure."})
        store.record({"type": "pins", "pins": {}})  # transient: dropped
        store.flush()
        sessions = store.list()
        self.assertEqual(1, len(sessions))
        self.assertEqual("Explain limits to me", sessions[0]["title"])
        self.assertEqual(2, sessions[0]["events"])

    def test_empty_chats_are_not_saved(self) -> None:
        store, _ = make_store()
        store.record({"type": "notice", "text": "backend notice"})  # no user msg
        store.flush()
        self.assertEqual([], store.list())

    def test_load_and_continue_preserves_backend_session(self) -> None:
        store, _ = make_store()
        store.record({"type": "user_message", "text": "hi"})
        store.set_backend_session("sess-42")
        store.flush()
        old_id = store.current_id

        store.begin()  # new chat
        events = store.continue_from(old_id)
        self.assertIsNotNone(events)
        self.assertEqual("hi", events[0]["text"])
        self.assertEqual("sess-42", store.backend_session_id)
        # Appending after resume lands in the same file.
        store.record({"type": "user_message", "text": "continued"})
        store.flush()
        data = store.load(old_id)
        self.assertEqual(2, len(data["events"]))

    def test_replayed_pending_proposal_is_set_aside(self) -> None:
        """A restored chat's proposals are gone from the ProposalManager, so a
        card replayed as `pending` is a zombie: it looks actionable and its
        buttons do nothing."""
        store, _ = make_store()
        store.record({"type": "user_message", "text": "add a card"})
        store.record(
            {"type": "proposal", "proposal": {"id": "p1", "status": "pending"}}
        )
        store.record(
            {"type": "proposal", "proposal": {"id": "p2", "status": "accepted"}}
        )
        store.flush()
        old_id = store.current_id

        store.begin()
        events = store.continue_from(old_id)
        assert events is not None
        self.assertEqual("superseded", events[1]["proposal"]["status"])
        # Anything already resolved is replayed untouched.
        self.assertEqual("accepted", events[2]["proposal"]["status"])
        # The stored transcript still records what actually happened.
        self.assertEqual("pending", store.load(old_id)["events"][1]["proposal"]["status"])

    def test_list_sorted_newest_first(self) -> None:
        store, _ = make_store()
        store.record({"type": "user_message", "text": "first chat"})
        store.flush()
        store.begin()
        store.record({"type": "user_message", "text": "second chat"})
        store.flush()
        titles = [m["title"] for m in store.list()]
        self.assertEqual(["second chat", "first chat"], titles)

    def test_load_missing_or_traversal_ids(self) -> None:
        store, _ = make_store()
        self.assertIsNone(store.load("nope"))
        self.assertIsNone(store.load("../../etc/passwd"))


if __name__ == "__main__":
    unittest.main()
