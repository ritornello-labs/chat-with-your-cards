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

    def test_pdf_stages_as_document_with_agent_note(self) -> None:
        # #15b: PDFs are context material - staged, path-listed, and the
        # block tells the agent to READ it (or say file tools are off).
        pdf = self._file("paper.pdf", b"%PDF-1.4 fake")
        result = addon._stage_composer_files([str(pdf)])
        self.assertEqual(1, result["added"])
        self.assertEqual("document", addon.state.attachments[0]["kind"])
        block = addon._attachment_message_block(addon.state.attachments)
        self.assertIn("read them from the path", block)

    def test_dropped_paths_stage_and_skip_missing(self) -> None:
        png = self._file("dropped.png")
        count = addon._handle_dropped_paths(
            [str(png), str(self.base / "no-such-file.png"), ""]
        )
        self.assertEqual(1, count)
        self.assertEqual("dropped.png", addon.state.attachments[0]["name"])

    def test_attach_pasted_decodes_and_stages(self) -> None:
        import base64

        payload = base64.b64encode(b"\x89PNG-pasted-bytes").decode()
        addon._attach_pasted(
            {"name": "", "mime": "image/png", "data": f"data:image/png;base64,{payload}"}
        )
        self.assertEqual(1, len(addon.state.attachments))
        entry = addon.state.attachments[0]
        self.assertTrue(entry["name"].startswith("pasted-"), entry)
        self.assertTrue(entry["name"].endswith(".png"))
        self.assertEqual(b"\x89PNG-pasted-bytes", Path(entry["path"]).read_bytes())

    def test_attach_pasted_rejects_garbage(self) -> None:
        tooltips: list[str] = []
        original = addon._tooltip_result
        addon._tooltip_result = tooltips.append
        try:
            addon._attach_pasted({"data": "not-a-data-url"})
        finally:
            addon._tooltip_result = original
        self.assertEqual([], addon.state.attachments)
        self.assertTrue(tooltips and "Could not read" in tooltips[0])

    def test_image_context_blocks_cap_and_filter(self) -> None:
        addon._stage_composer_files(
            [str(self._file("small.png")), str(self._file("word.mp3", b"\xff\xfbx"))]
        )
        big = dict(addon.state.attachments[0])
        big["size"] = addon.IMAGE_BLOCK_MAX_BYTES + 1
        entries = addon.state.attachments + [big]
        blocks = addon._image_context_blocks(entries)
        self.assertEqual(1, len(blocks))  # audio and oversized image skipped
        self.assertEqual("image", blocks[0]["type"])
        self.assertEqual("image/png", blocks[0]["source"]["media_type"])
        import base64

        self.assertEqual(
            b"\x89PNGxxxx", base64.b64decode(blocks[0]["source"]["data"])
        )
        # And the message block is honest about what rides inline vs not.
        block_text = addon._attachment_message_block(entries)
        self.assertIn("also shown to you inline", block_text)
        self.assertIn("Too large to show inline", block_text)


if __name__ == "__main__":
    unittest.main()
