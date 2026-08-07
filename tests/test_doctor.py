from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from chat_with_your_cards.doctor import CLAUDE_MIN_VERSION, _binary_row  # noqa: E402


class BinaryVersionTest(unittest.TestCase):
    def _row(self, output: str) -> dict:
        completed = mock.Mock(returncode=0, stdout=output, stderr="")
        with mock.patch("chat_with_your_cards.doctor.subprocess.run", return_value=completed):
            return _binary_row(
                "Claude Code",
                "/usr/bin/claude",
                "https://claude.com/claude-code",
                minimum=CLAUDE_MIN_VERSION,
            )

    def test_supported_version_is_ok(self) -> None:
        self.assertEqual("ok", self._row("2.1.220 (Claude Code)\n")["status"])

    def test_newer_version_is_ok(self) -> None:
        self.assertEqual("ok", self._row("2.2.0 (Claude Code)\n")["status"])

    def test_old_version_is_broken_with_required_version(self) -> None:
        row = self._row("2.1.219 (Claude Code)\n")
        self.assertEqual("broken", row["status"])
        self.assertIn("2.1.220+", row["detail"])

    def test_unparseable_version_warns(self) -> None:
        self.assertEqual("warn", self._row("Claude Code development build\n")["status"])


if __name__ == "__main__":
    unittest.main()
