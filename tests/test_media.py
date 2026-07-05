"""get_card_images tool + MCP image content-block passthrough."""

from __future__ import annotations

import base64
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from chat_with_your_cards.mcp_server import _as_content_blocks  # noqa: E402
from chat_with_your_cards.tools.media import (  # noqa: E402
    get_card_images,
    image_filenames,
)

PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9"
    "awAAAABJRU5ErkJggg=="
)


class FakeNote:
    def __init__(self, fields: dict[str, str], note_id: int = 7) -> None:
        self._fields = fields
        self.id = note_id

    def items(self):
        return self._fields.items()

    def note_type(self):
        return {"name": "Basic"}


class FakeMedia:
    def __init__(self, directory: Path) -> None:
        self._dir = str(directory)

    def dir(self) -> str:
        return self._dir


class FakeCol:
    def __init__(self, note: FakeNote, media_dir: Path) -> None:
        self._note = note
        self.media = FakeMedia(media_dir)

    def get_note(self, note_id: int) -> FakeNote:
        return self._note


class FakeCtx:
    def __init__(self, col: FakeCol, config: dict | None = None) -> None:
        self._col = col
        self._config = config or {}

    @property
    def col(self):
        return self._col

    @property
    def stats(self):
        return None

    @property
    def proposals(self):
        return None

    @property
    def config(self):
        return self._config


class ImageFilenameTests(unittest.TestCase):
    def test_extracts_local_skips_remote_and_data(self) -> None:
        fields = [
            '<img src="paste-1.jpg"> and <img src="paste-1.jpg">',  # dupe
            "<img src='https://example.com/x.png'>",  # remote
            '<img src="data:image/png;base64,AAAA">',  # inline
            'text <img  src = "diagram.PNG" > more',
        ]
        self.assertEqual(["paste-1.jpg", "diagram.PNG"], image_filenames(fields))

    def test_no_images(self) -> None:
        self.assertEqual([], image_filenames(["just text", "<b>bold</b>"]))


class GetCardImagesTests(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile

        self.media_dir = Path(tempfile.mkdtemp(prefix="cwyc-media-"))
        (self.media_dir / "seaking.png").write_bytes(PNG_1PX)

    def _ctx(self, fields: dict[str, str]) -> FakeCtx:
        return FakeCtx(FakeCol(FakeNote(fields), self.media_dir))

    def test_returns_text_header_then_image_block(self) -> None:
        ctx = self._ctx({"Front": "Name?", "Image": '<img src="seaking.png">'})
        result = get_card_images(ctx, {"note_id": 7})
        self.assertEqual("text", result[0]["type"])
        self.assertIn("1 image", result[0]["text"])
        self.assertEqual("image", result[1]["type"])
        self.assertEqual("image/png", result[1]["mimeType"])
        self.assertEqual(base64.b64encode(PNG_1PX).decode(), result[1]["data"])

    def test_missing_file_is_reported_not_crashed(self) -> None:
        ctx = self._ctx({"Image": '<img src="gone.png">'})
        result = get_card_images(ctx, {"note_id": 7})
        self.assertEqual(1, len(result))  # header only, no image block
        self.assertIn("missing", result[0]["text"])

    def test_no_images_message(self) -> None:
        ctx = self._ctx({"Front": "plain text"})
        result = get_card_images(ctx, {"note_id": 7})
        self.assertEqual(1, len(result))
        self.assertIn("no local images", result[0]["text"])

    def test_needs_an_id(self) -> None:
        with self.assertRaises(ValueError):
            get_card_images(self._ctx({}), {})


class SourceExtractionTests(unittest.TestCase):
    def test_hrefs_bare_urls_and_pdf_paths(self) -> None:
        from chat_with_your_cards.tools.media import extract_sources

        fields = {
            "Extra": '<a href="https://en.wikipedia.org/wiki/Limit">Wikipedia</a> '
            "and see /home/example/books/analysis.pdf p.212",
            "Text": "plain https://example.com/spec. Also "
            '<img src="data:image/png;base64,AAAA">',
        }
        sources = extract_sources(fields)
        uris = {s["uri"]: s for s in sources}
        self.assertIn("https://en.wikipedia.org/wiki/Limit", uris)
        self.assertEqual("pdf", uris["/home/example/books/analysis.pdf"]["kind"])
        self.assertIn("https://example.com/spec", uris)  # trailing dot stripped
        self.assertFalse(any(u.startswith("data:") for u in uris))

    def test_field_restriction(self) -> None:
        from chat_with_your_cards.tools.media import extract_sources

        fields = {"Extra": "https://keep.example", "Text": "https://drop.example"}
        sources = extract_sources(fields, ["Extra"])
        self.assertEqual(["https://keep.example"], [s["uri"] for s in sources])

    def test_get_card_sources_respects_note_type_config(self) -> None:
        from chat_with_your_cards.tools.media import get_card_sources

        note = FakeNote(
            {"Text": "https://drop.example", "Extra": "https://keep.example"}
        )
        ctx = FakeCtx(
            FakeCol(note, Path(".")),
            config={"source_fields": {"Basic": ["Extra"]}},
        )
        result = get_card_sources(ctx, {"note_id": 7})
        self.assertEqual(
            ["https://keep.example"], [s["uri"] for s in result["sources"]]
        )

    def test_get_card_sources_default_all_fields(self) -> None:
        from chat_with_your_cards.tools.media import get_card_sources

        note = FakeNote({"Text": "https://a.example", "Extra": "https://b.example"})
        ctx = FakeCtx(FakeCol(note, Path(".")))
        result = get_card_sources(ctx, {"note_id": 7})
        self.assertEqual(2, len(result["sources"]))


class ContentBlockPassthroughTests(unittest.TestCase):
    def test_image_blocks_passed_through(self) -> None:
        blocks = [
            {"type": "text", "text": "1 image"},
            {"type": "image", "data": "AAAA", "mimeType": "image/png"},
        ]
        self.assertEqual(blocks, _as_content_blocks(blocks))

    def test_plain_dict_wrapped_as_text(self) -> None:
        out = _as_content_blocks({"total": 12})
        self.assertEqual(1, len(out))
        self.assertEqual("text", out[0]["type"])
        self.assertIn("total", out[0]["text"])

    def test_plain_list_wrapped_as_text(self) -> None:
        # A list that is NOT content blocks must be JSON, not passed through.
        out = _as_content_blocks([1, 2, 3])
        self.assertEqual("text", out[0]["type"])


if __name__ == "__main__":
    unittest.main()
