"""media_staging.py: validation, staging lifecycle, marker rewriting."""

from __future__ import annotations

import os
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from chat_with_your_cards.media_staging import (
    MAX_MEDIA_FILE_BYTES,
    MAX_MEDIA_PER_PROPOSAL,
    MediaError,
    MediaStaging,
    rewrite_sound_markers,
    sound_markers,
)


class StagingTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        self.staging = MediaStaging(self.base / "staging")

    def _audio(self, name: str = "word.mp3", size: int = 1000) -> Path:
        path = self.base / name
        path.write_bytes(b"\xff\xfb" + b"x" * (size - 2))
        return path

    def test_stage_copies_and_payload_is_playable_data_uri(self) -> None:
        src = self._audio()
        staged = self.staging.stage("p1", [{"path": str(src)}])
        self.assertEqual(len(staged), 1)
        item = staged[0]
        self.assertEqual(item.filename, "word.mp3")
        self.assertEqual(item.mime, "audio/mpeg")
        self.assertTrue(item.path.is_file())
        # Source deletion must not hurt the staged copy (the /tmp race).
        os.unlink(src)
        payload = item.to_payload()
        self.assertEqual(payload["kind"], "audio")
        self.assertTrue(payload["src"].startswith("data:audio/mpeg;base64,"))
        self.assertEqual(payload["bytes"], 1000)

    def test_explicit_filename_wins_over_basename(self) -> None:
        src = self._audio("tmpXYZ.mp3")
        staged = self.staging.stage("p1", [{"path": str(src), "filename": "nihao.mp3"}])
        self.assertEqual(staged[0].filename, "nihao.mp3")

    def test_missing_file_rejected(self) -> None:
        with self.assertRaises(MediaError):
            self.staging.stage("p1", [{"path": str(self.base / "nope.mp3")}])

    def test_unsupported_extension_rejected(self) -> None:
        src = self.base / "evil.exe"
        src.write_bytes(b"MZ....")
        with self.assertRaisesRegex(MediaError, "unsupported media type"):
            self.staging.stage("p1", [{"path": str(src)}])

    def test_image_and_video_staged_with_kinds(self) -> None:
        # #10: images and video ride the same staging path as audio.
        png = self.base / "map.png"
        png.write_bytes(b"\x89PNG" + b"x" * 100)
        webm = self.base / "clip.webm"
        webm.write_bytes(b"\x1a\x45\xdf\xa3" + b"x" * 100)
        staged = self.staging.stage(
            "p1", [{"path": str(png)}, {"path": str(webm)}]
        )
        payloads = {item.filename: item.to_payload() for item in staged}
        self.assertEqual("image", payloads["map.png"]["kind"])
        self.assertTrue(payloads["map.png"]["src"].startswith("data:image/png;base64,"))
        self.assertEqual("video", payloads["clip.webm"]["kind"])
        self.assertTrue(payloads["clip.webm"]["src"].startswith("data:video/webm;base64,"))

    def test_media_references_cover_img_and_sound(self) -> None:
        from chat_with_your_cards.media_staging import media_references

        refs = media_references(
            {
                "Front": 'Where? <img src="map.png"> and <IMG SRC=\'alt.webp\'>',
                "Back": "[sound:word.mp3] <img src=bare.gif>",
            }
        )
        self.assertEqual({"map.png", "alt.webp", "word.mp3", "bare.gif"}, refs)

    def test_rewrite_media_markers_rewrites_img_and_sound(self) -> None:
        from chat_with_your_cards.media_staging import rewrite_media_markers

        fields = {
            "Front": '<img src="map.png"> stays <img src="other.png">',
            "Back": "[sound:word.mp3]",
        }
        out = rewrite_media_markers(
            fields, {"map.png": "map-2.png", "word.mp3": "word-2.mp3"}
        )
        self.assertEqual(
            '<img src="map-2.png"> stays <img src="other.png">', out["Front"]
        )
        self.assertEqual("[sound:word-2.mp3]", out["Back"])

    def test_bad_filenames_rejected(self) -> None:
        src = self._audio()
        for bad in ("a/b.mp3", "a]b.mp3", "a[b.mp3", "a:b.mp3", "x" * 130 + ".mp3"):
            with self.assertRaises(MediaError, msg=bad):
                self.staging.stage("p1", [{"path": str(src), "filename": bad}])

    def test_oversize_and_empty_rejected(self) -> None:
        big = self.base / "big.mp3"
        big.write_bytes(b"x" * (MAX_MEDIA_FILE_BYTES + 1))
        with self.assertRaises(MediaError):
            self.staging.stage("p1", [{"path": str(big)}])
        empty = self.base / "empty.mp3"
        empty.write_bytes(b"")
        with self.assertRaises(MediaError):
            self.staging.stage("p1", [{"path": str(empty)}])

    def test_too_many_rejected(self) -> None:
        items = [{"path": str(self._audio(f"a{i}.mp3"))} for i in range(MAX_MEDIA_PER_PROPOSAL + 1)]
        with self.assertRaises(MediaError):
            self.staging.stage("p1", items)

    def test_duplicate_names_rejected_and_nothing_left_behind(self) -> None:
        a = self._audio("a.mp3")
        b = self._audio("b.mp3")
        with self.assertRaises(MediaError):
            self.staging.stage(
                "p1",
                [{"path": str(a)}, {"path": str(b), "filename": "A.mp3"}],
            )
        # all-or-nothing: the failed stage removed its directory entirely
        self.assertFalse((self.base / "staging" / "p1").exists())

    def test_discard_removes_dir(self) -> None:
        self.staging.stage("p1", [{"path": str(self._audio())}])
        self.assertTrue(self.staging.staged_path("p1", "word.mp3").is_file())
        self.staging.discard("p1")
        self.assertFalse((self.base / "staging" / "p1").exists())

    def test_sweep_removes_only_old_dirs(self) -> None:
        self.staging.stage("old", [{"path": str(self._audio("o.mp3"))}])
        self.staging.stage("new", [{"path": str(self._audio("n.mp3"))}])
        old_dir = self.base / "staging" / "old"
        stale = time.time() - 10 * 86400
        os.utime(old_dir, (stale, stale))
        removed = self.staging.sweep(max_age_days=7)
        self.assertEqual(removed, 1)
        self.assertFalse(old_dir.exists())
        self.assertTrue((self.base / "staging" / "new").exists())


class MarkerTests(unittest.TestCase):
    def test_sound_markers_found_across_fields(self) -> None:
        found = sound_markers(
            {
                "Front": "你好 [sound:nihao.mp3]",
                "Back": "hello <b>x</b> [sound:hello.mp3] [sound:nihao.mp3]",
                "Extra": "no markers",
            }
        )
        self.assertEqual(found, {"nihao.mp3", "hello.mp3"})

    def test_rewrite_only_renamed_markers(self) -> None:
        fields = {
            "Front": "你好 [sound:nihao.mp3]",
            "Back": "[sound:hello.mp3] and [sound:nihao.mp3] text",
        }
        out = rewrite_sound_markers(fields, {"nihao.mp3": "nihao-1.mp3"})
        self.assertEqual(out["Front"], "你好 [sound:nihao-1.mp3]")
        self.assertEqual(out["Back"], "[sound:hello.mp3] and [sound:nihao-1.mp3] text")

    def test_rewrite_noop_without_renames(self) -> None:
        fields = {"Front": "[sound:a.mp3]"}
        self.assertEqual(rewrite_sound_markers(fields, {}), fields)


if __name__ == "__main__":
    unittest.main()
