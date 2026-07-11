"""Mid-conversation model/effort switching, cancel/interrupt process
lifecycle, and control-request/control-response wiring in the Claude CLI
session.

The CLI apps let you change model mid-chat and keep the conversation;
we match that by respawning with --resume + the new flags on the next
send (DESIGN.md section 2). These tests drive ClaudeCliSession with a
fake subprocess and assert the respawn decision and its argv - plus (this
file already has the Popen-faking harness) cancel()'s process reaping and
interrupt()'s control-request/control-response round trip and fallbacks.
"""

from __future__ import annotations

import json
import queue
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from chat_with_your_cards.backends import claude_cli  # noqa: E402
from chat_with_your_cards.backends.claude_cli import ClaudeCliSession  # noqa: E402


class FakeStream:
    def __init__(self) -> None:
        self.written: list[str] = []

    def __iter__(self):  # empty stdout/stderr: reader thread exits at once
        return iter(())

    def write(self, text: str) -> None:
        self.written.append(text)

    def flush(self) -> None:
        pass


class QueueStdout:
    """A stdout fake a test can feed lines into live, so the reader thread
    (already blocked on `for line in process.stdout`) observes them as if
    the real CLI had written them - needed to exercise interrupt()'s
    control_response round trip, which depends on that thread routing the
    response back to the waiting threading.Event."""

    def __init__(self) -> None:
        self._queue: "queue.Queue[str | None]" = queue.Queue()

    def push_line(self, payload: dict) -> None:
        self._queue.put(json.dumps(payload) + "\n")

    def close(self) -> None:
        self._queue.put(None)

    def __iter__(self):
        return self

    def __next__(self) -> str:
        item = self._queue.get()
        if item is None:
            raise StopIteration
        return item


class FakePopen:
    instances: list["FakePopen"] = []
    # Tests override this to inject a QueueStdout when they need to push
    # simulated stdout lines (e.g. a control_response) at a controlled time.
    stdout_factory = FakeStream

    def __init__(self, args, **kwargs) -> None:
        self.args = args
        self.pid = 1000 + len(FakePopen.instances)
        self.returncode = None
        self._alive = True
        self.stdin = FakeStream()
        self.stdout = FakePopen.stdout_factory()
        self.stderr = FakeStream()
        self.terminated = False
        self.killed = False
        # When False, terminate() does not actually stop the process (as if
        # the CLI ignored SIGTERM) so cancel()'s wait(timeout=...) must time
        # out and fall through to kill().
        self.terminates_cleanly = True
        FakePopen.instances.append(self)

    def poll(self):
        return None if self._alive else self.returncode

    def terminate(self) -> None:
        self.terminated = True
        if self.terminates_cleanly:
            self._alive = False
            self.returncode = -15

    def wait(self, timeout=None):
        if self._alive:
            raise subprocess.TimeoutExpired(cmd=self.args, timeout=timeout or 0)
        return self.returncode

    def kill(self) -> None:
        self.killed = True
        self._alive = False
        self.returncode = -9


class ModelSwitchTest(unittest.TestCase):
    def setUp(self) -> None:
        FakePopen.instances = []
        self._orig_popen = claude_cli.subprocess.Popen
        claude_cli.subprocess.Popen = FakePopen  # type: ignore[assignment]
        self.session = ClaudeCliSession(
            cli_path="claude",
            system_prompt="SYS",
            mcp_url="http://127.0.0.1:1/mcp",
            mcp_token="tok",
            run_on_ui=lambda fn: fn(),
            workdir=Path(tempfile.mkdtemp(prefix="cwyc-switch-")),
            model="",
            effort="",
        )

    def tearDown(self) -> None:
        claude_cli.subprocess.Popen = self._orig_popen  # type: ignore[assignment]
        FakePopen.stdout_factory = FakeStream

    def _argv(self, popen: FakePopen) -> list[str]:
        return list(popen.args)

    def test_unchanged_model_reuses_process(self) -> None:
        self.session.prewarm()
        self.session._ensure_process()
        self.assertEqual(1, len(FakePopen.instances), "no respawn when nothing changed")

    def test_switch_respawns_with_new_flags_and_resume(self) -> None:
        self.session.prewarm()
        first = FakePopen.instances[0]
        self.assertNotIn("--model", self._argv(first))
        # Pretend a conversation exists so --resume has something to resume.
        self.session._state.session_id = "sess-77"

        self.session.set_model_effort("opus", "high")
        self.session._ensure_process()  # what the next send triggers

        self.assertTrue(first.terminated, "old process must be terminated")
        self.assertEqual(2, len(FakePopen.instances), "must respawn once")
        argv = self._argv(FakePopen.instances[1])
        self.assertEqual("opus", argv[argv.index("--model") + 1])
        self.assertEqual("high", argv[argv.index("--effort") + 1])
        self.assertEqual("sess-77", argv[argv.index("--resume") + 1])

    def test_switch_is_lazy_no_respawn_until_next_use(self) -> None:
        self.session.prewarm()
        self.session.set_model_effort("haiku", "low")
        # Storing the choice alone must not spawn or kill anything.
        self.assertEqual(1, len(FakePopen.instances))
        self.assertFalse(FakePopen.instances[0].terminated)


class _FakeSessionTestCase(unittest.TestCase):
    """Shared Popen-faking setup for cancel()/interrupt() tests."""

    def setUp(self) -> None:
        FakePopen.instances = []
        FakePopen.stdout_factory = FakeStream
        self._orig_popen = claude_cli.subprocess.Popen
        claude_cli.subprocess.Popen = FakePopen  # type: ignore[assignment]
        self.session = ClaudeCliSession(
            cli_path="claude",
            system_prompt="SYS",
            mcp_url="http://127.0.0.1:1/mcp",
            mcp_token="tok",
            run_on_ui=lambda fn: fn(),
            workdir=Path(tempfile.mkdtemp(prefix="cwyc-interrupt-")),
            model="",
            effort="",
        )

    def tearDown(self) -> None:
        claude_cli.subprocess.Popen = self._orig_popen  # type: ignore[assignment]
        FakePopen.stdout_factory = FakeStream


class CancelReapingTest(_FakeSessionTestCase):
    """cancel() now waits for the process to actually die before letting the
    next send() respawn, escalating to kill() if terminate() is ignored."""

    def test_cancel_reaps_cleanly_terminated_process_without_kill(self) -> None:
        self.session.prewarm()
        popen = FakePopen.instances[0]

        self.session.cancel()

        self.assertTrue(popen.terminated)
        self.assertFalse(popen.killed, "a process that dies on SIGTERM is never kill()ed")
        self.assertIsNone(self.session._process, "next send() must respawn")
        self.assertEqual(1, self.session._generation)

    def test_cancel_kills_process_that_ignores_terminate(self) -> None:
        self.session.prewarm()
        popen = FakePopen.instances[0]
        popen.terminates_cleanly = False  # simulate a hung CLI

        self.session.cancel()

        self.assertTrue(popen.terminated, "terminate() is still tried first")
        self.assertTrue(popen.killed, "must escalate to kill() after the wait times out")
        self.assertIsNone(self.session._process)

    def test_cancel_on_already_dead_process_does_not_call_kill(self) -> None:
        self.session.prewarm()
        popen = FakePopen.instances[0]
        popen._alive = False  # process already exited on its own
        popen.returncode = 0

        self.session.cancel()

        self.assertFalse(popen.terminated, "terminate() is only sent to a live process")
        self.assertFalse(popen.killed)


class InterruptTest(_FakeSessionTestCase):
    """interrupt() ports the control_request/control_response wire framing
    from the Claude Agent SDK's Query (MIT-licensed; see claude_cli.py's
    interrupt() docstring for the source). These tests drive the full round
    trip through a live-queue stdout fake so the reader thread genuinely
    routes the response back to interrupt()'s waiting threading.Event."""

    def _spawn_and_send(self) -> FakePopen:
        FakePopen.stdout_factory = QueueStdout
        events: list = []
        self.session.send("hello", events.append)
        return FakePopen.instances[-1]

    def _await_control_request(self, popen: FakePopen, timeout: float = 2.0) -> dict:
        """Poll stdin for the control_request interrupt() just wrote, since
        it runs on a background thread in these tests (interrupt() blocks
        on the response event, so the test drives the reply concurrently)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for line in popen.stdin.written:
                obj = json.loads(line)
                if obj.get("type") == "control_request":
                    return obj
            time.sleep(0.005)
        raise AssertionError("interrupt() never wrote a control_request")

    def test_interrupt_success_keeps_same_process_and_generation(self) -> None:
        popen = self._spawn_and_send()
        result: dict = {}

        def call_interrupt() -> None:
            result["acknowledged"] = self.session.interrupt(timeout=2.0)

        caller = threading.Thread(target=call_interrupt)
        caller.start()

        request = self._await_control_request(popen)
        self.assertEqual("interrupt", request["request"]["subtype"])
        self.assertTrue(request["request_id"].startswith("req_1_"))

        popen.stdout.push_line(
            {
                "type": "control_response",
                "response": {
                    "request_id": request["request_id"],
                    "subtype": "success",
                    "response": {},
                },
            }
        )
        caller.join(timeout=2.0)
        self.assertFalse(caller.is_alive(), "interrupt() must return once acked")

        self.assertTrue(result["acknowledged"])
        self.assertIs(self.session._process, popen, "no respawn on success")
        self.assertFalse(popen.terminated)
        self.assertFalse(popen.killed)
        self.assertEqual(0, self.session._generation, "no generation bump on success")
        self.assertEqual({}, self.session._pending_control)
        popen.stdout.close()

    def test_interrupt_error_subtype_falls_back_to_cancel(self) -> None:
        popen = self._spawn_and_send()
        result: dict = {}

        def call_interrupt() -> None:
            result["acknowledged"] = self.session.interrupt(timeout=2.0)

        caller = threading.Thread(target=call_interrupt)
        caller.start()

        request = self._await_control_request(popen)
        popen.stdout.push_line(
            {
                "type": "control_response",
                "response": {
                    "request_id": request["request_id"],
                    "subtype": "error",
                    "error": "unsupported",
                },
            }
        )
        caller.join(timeout=2.0)

        self.assertFalse(result["acknowledged"])
        self.assertTrue(popen.terminated, "an error subtype falls back to cancel()")
        self.assertIsNone(self.session._process)
        self.assertEqual(1, self.session._generation)
        popen.stdout.close()

    def test_interrupt_timeout_falls_back_to_cancel(self) -> None:
        popen = self._spawn_and_send()
        # Nobody ever answers - interrupt() must not wedge the stop button.
        acknowledged = self.session.interrupt(timeout=0.05)

        self.assertFalse(acknowledged)
        self.assertTrue(popen.terminated)
        self.assertIsNone(self.session._process)
        self.assertEqual(1, self.session._generation)
        popen.stdout.close()

    def test_interrupt_with_no_live_process_falls_back_immediately(self) -> None:
        # Nothing spawned yet: self._process is None.
        acknowledged = self.session.interrupt(timeout=1.0)

        self.assertFalse(acknowledged)
        self.assertEqual(1, self.session._generation)
        self.assertIsNone(self.session._process)

    def test_interrupt_write_failure_falls_back_to_cancel(self) -> None:
        popen = self._spawn_and_send()

        def boom(_text: str) -> None:
            raise BrokenPipeError("pipe closed")

        popen.stdin.write = boom  # type: ignore[method-assign]

        acknowledged = self.session.interrupt(timeout=1.0)

        self.assertFalse(acknowledged)
        self.assertTrue(popen.terminated)
        self.assertIsNone(self.session._process)
        popen.stdout.close()

    def test_control_response_is_never_dispatched_as_a_chat_event(self) -> None:
        # An unsolicited/unmatched control_response (no pending interrupt())
        # must be silently dropped by the read loop, and must not stop it
        # from processing the ordinary events that follow.
        popen = self._spawn_and_send()
        collected: list = []
        self.session._on_event = collected.append

        popen.stdout.push_line(
            {
                "type": "control_response",
                "response": {"request_id": "req_unmatched", "subtype": "success"},
            }
        )
        popen.stdout.push_line(
            {
                "type": "stream_event",
                "event": {
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": "hi"},
                },
            }
        )
        popen.stdout.push_line({"type": "result", "subtype": "success"})
        popen.stdout.close()

        # _read_loop runs on the reader thread; give it a moment to drain.
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and not collected:
            time.sleep(0.005)
        # Let the (short) remaining lines flush too.
        time.sleep(0.05)

        kinds = [type(e).__name__ for e in collected]
        # Exactly the two ordinary events - the control_response line
        # produced nothing at all, dispatched or otherwise.
        self.assertEqual(["TextDelta", "Done"], kinds)


if __name__ == "__main__":
    unittest.main()
