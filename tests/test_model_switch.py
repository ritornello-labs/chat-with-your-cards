"""Mid-conversation model/effort switching in the Claude CLI session.

The CLI apps let you change model mid-chat and keep the conversation;
we match that by respawning with --resume + the new flags on the next
send (DESIGN.md section 2). These tests drive ClaudeCliSession with a
fake subprocess and assert the respawn decision and its argv.
"""

from __future__ import annotations

import sys
import tempfile
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


class FakePopen:
    instances: list["FakePopen"] = []

    def __init__(self, args, **kwargs) -> None:
        self.args = args
        self.pid = 1000 + len(FakePopen.instances)
        self.returncode = None
        self._alive = True
        self.stdin = FakeStream()
        self.stdout = FakeStream()
        self.stderr = FakeStream()
        self.terminated = False
        FakePopen.instances.append(self)

    def poll(self):
        return None if self._alive else self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self._alive = False
        self.returncode = -15


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


if __name__ == "__main__":
    unittest.main()
