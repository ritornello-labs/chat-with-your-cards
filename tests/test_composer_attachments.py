"""Composer attachments (#15a): staging, removal, send-block, lifecycle.

The glue module's helpers are pure enough to unit-test with USER_FILES
patched to a temp dir and a fake dock capturing pushes; the native file
picker itself is Qt and only provable in the GUI smoke.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import chat_with_your_cards as addon  # noqa: E402


class ComposerAttachmentTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        self._old_user_files = addon.USER_FILES
        self._old_attachments = addon.state.attachments
        self._old_dock = addon.state.dock
        addon.USER_FILES = self.base / "user_files"
        addon.state.attachments = []
        self.pushed: list[dict] = []
        addon.state.dock = SimpleNamespace(
            bridge=SimpleNamespace(push=self.pushed.append)
        )

        def restore() -> None:
            addon.USER_FILES = self._old_user_files
            addon.state.attachments = self._old_attachments
            addon.state.dock = self._old_dock

        self.addCleanup(restore)

    def _file(self, name: str, payload: bytes = b"\x89PNGxxxx") -> Path:
        path = self.base / name
        path.write_bytes(payload)
        return path

    def test_stage_push_and_agent_block(self) -> None:
        png = self._file("map.png")
        mp3 = self._file("word.mp3", b"\xff\xfbxx")
        result = addon._stage_composer_files([str(png), str(mp3)])
        self.assertEqual(2, result["added"])
        self.assertEqual([], result["errors"])
        kinds = [entry["kind"] for entry in addon.state.attachments]
        self.assertEqual(["image", "audio"], kinds)
        for entry in addon.state.attachments:
            self.assertTrue(Path(entry["path"]).is_file(), entry)
        # The UI push carries no paths - those are agent-facing only.
        push = self.pushed[-1]
        self.assertEqual("attachments", push["type"])
        self.assertNotIn("path", push["items"][0])
        block = addon._attachment_message_block(addon.state.attachments)
        self.assertIn("map.png", block)
        self.assertIn("propose_note's media[]", block)
        self.assertIn(addon.state.attachments[0]["path"], block)

    def test_unsupported_file_is_a_per_file_error(self) -> None:
        bad = self._file("evil.exe", b"MZ..")
        good = self._file("ok.png")
        result = addon._stage_composer_files([str(bad), str(good)])
        self.assertEqual(1, result["added"])
        self.assertEqual(1, len(result["errors"]))
        self.assertIn("evil.exe", result["errors"][0])

    def test_remove_discards_only_that_file(self) -> None:
        addon._stage_composer_files(
            [str(self._file("a.png")), str(self._file("b.png"))]
        )
        first, second = addon.state.attachments
        addon._remove_composer_attachment(first["id"])
        self.assertEqual([second], addon.state.attachments)
        self.assertFalse(Path(first["path"]).exists())
        self.assertTrue(Path(second["path"]).is_file())

    def test_send_clears_list_but_keeps_files(self) -> None:
        addon._stage_composer_files([str(self._file("keep.png"))])
        path = Path(addon.state.attachments[0]["path"])
        addon._clear_composer_attachments(discard_files=False)
        self.assertEqual([], addon.state.attachments)
        self.assertTrue(path.is_file())  # the agent may propose with it later
        self.assertEqual([], self.pushed[-1]["items"])

    def test_new_chat_discards_files(self) -> None:
        addon._stage_composer_files([str(self._file("gone.png"))])
        path = Path(addon.state.attachments[0]["path"])
        addon._clear_composer_attachments(discard_files=True)
        self.assertFalse(path.exists())
        self.assertEqual([], addon.state.attachments)


if __name__ == "__main__":
    unittest.main()
