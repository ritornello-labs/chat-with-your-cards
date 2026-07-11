from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from chat_with_your_cards.backends import (  # noqa: E402
    ChatEvent,
    Done,
    ScriptedBackend,
    TextDelta,
    ToolCallFinished,
    ToolCallStarted,
)


class ImmediateScheduler:
    """Collects callbacks and runs them in scheduled order on demand."""

    def __init__(self) -> None:
        self.queue: list[tuple[int, object]] = []

    def schedule(self, delay_ms: int, callback) -> None:
        self.queue.append((delay_ms, callback))

    def run_all(self) -> None:
        for _, callback in sorted(self.queue, key=lambda item: item[0]):
            callback()  # type: ignore[operator]
        self.queue.clear()

    def run_first(self, count: int) -> None:
        pending = sorted(self.queue, key=lambda item: item[0])
        for _, callback in pending[:count]:
            callback()  # type: ignore[operator]
        self.queue = pending[count:]


def collect(events: list[ChatEvent]):
    return events.append


class ScriptedBackendTest(unittest.TestCase):
    def _session(self):
        scheduler = ImmediateScheduler()
        backend = ScriptedBackend(scheduler.schedule, seed=7)
        return scheduler, backend.start_session({})

    def test_default_script_streams_and_ends_with_single_done(self) -> None:
        scheduler, session = self._session()
        events: list[ChatEvent] = []
        session.send("explain this card", collect(events))
        scheduler.run_all()

        self.assertGreater(len([e for e in events if isinstance(e, TextDelta)]), 1)
        self.assertEqual(1, len([e for e in events if isinstance(e, Done)]))
        self.assertIsInstance(events[-1], Done)
        self.assertFalse(session.streaming)

    def test_text_deltas_reassemble_verbatim(self) -> None:
        scheduler, session = self._session()
        events: list[ChatEvent] = []
        session.send("explain this card", collect(events))
        scheduler.run_all()

        text = "".join(e.text for e in events if isinstance(e, TextDelta))
        self.assertIn("epsilon-delta definition", text)
        self.assertIn("Want me to find related cards in this deck?", text)

    def test_tool_script_emits_matched_tool_pair(self) -> None:
        scheduler, session = self._session()
        events: list[ChatEvent] = []
        session.send("please run a tool demo", collect(events))
        scheduler.run_all()

        started = [e for e in events if isinstance(e, ToolCallStarted)]
        finished = [e for e in events if isinstance(e, ToolCallFinished)]
        self.assertEqual(1, len(started))
        self.assertEqual(1, len(finished))
        self.assertEqual(started[0].call_id, finished[0].call_id)
        self.assertLess(events.index(started[0]), events.index(finished[0]))

    def test_cancel_stops_emission_and_no_done(self) -> None:
        scheduler, session = self._session()
        events: list[ChatEvent] = []
        session.send("explain this card", collect(events))

        scheduler.run_first(3)
        self.assertEqual(3, len(events))
        session.cancel()
        scheduler.run_all()

        self.assertEqual(3, len(events), "no events may arrive after cancel")
        self.assertFalse(any(isinstance(e, Done) for e in events))
        self.assertFalse(session.streaming)

    def test_interrupt_behaves_like_cancel_and_reports_unacknowledged(self) -> None:
        scheduler, session = self._session()
        events: list[ChatEvent] = []
        session.send("explain this card", collect(events))

        scheduler.run_first(3)
        self.assertEqual(3, len(events))
        acknowledged = session.interrupt()
        scheduler.run_all()

        self.assertFalse(
            acknowledged, "no real CLI to ack a control_request against"
        )
        self.assertEqual(3, len(events), "no events may arrive after interrupt")
        self.assertFalse(any(isinstance(e, Done) for e in events))
        self.assertFalse(session.streaming)

    def test_send_after_interrupt_streams_fresh_response(self) -> None:
        scheduler, session = self._session()
        first: list[ChatEvent] = []
        session.send("explain this card", collect(first))
        scheduler.run_first(2)
        session.interrupt()
        scheduler.run_all()

        second: list[ChatEvent] = []
        session.send("long version please", collect(second))
        scheduler.run_all()
        self.assertIsInstance(second[-1], Done)
        self.assertGreater(len(second), 5)

    def test_send_after_cancel_streams_fresh_response(self) -> None:
        scheduler, session = self._session()
        first: list[ChatEvent] = []
        session.send("explain this card", collect(first))
        scheduler.run_first(2)
        session.cancel()
        scheduler.run_all()

        second: list[ChatEvent] = []
        session.send("long version please", collect(second))
        scheduler.run_all()
        self.assertIsInstance(second[-1], Done)
        self.assertGreater(len(second), 5)

    def test_send_while_streaming_raises(self) -> None:
        _, session = self._session()
        session.send("explain this card", lambda e: None)
        with self.assertRaises(RuntimeError):
            session.send("again", lambda e: None)

    def test_streaming_flag_lifecycle(self) -> None:
        scheduler, session = self._session()
        self.assertFalse(session.streaming)
        session.send("explain this card", lambda e: None)
        self.assertTrue(session.streaming)
        scheduler.run_all()
        self.assertFalse(session.streaming)


if __name__ == "__main__":
    unittest.main()
