"""read_epub: pure-stdlib EPUB parsing (listing, chapter text, figures)."""

from __future__ import annotations

import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from chat_with_your_cards.tools.documents import read_epub  # noqa: E402

CONTAINER = """<?xml version="1.0"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>"""

OPF = """<?xml version="1.0"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="id">
  <metadata/>
  <manifest>
    <item id="c1" href="ch1.xhtml" media-type="application/xhtml+xml"/>
    <item id="c2" href="ch2.xhtml" media-type="application/xhtml+xml"/>
    <item id="img1" href="images/fig1.png" media-type="image/png"/>
  </manifest>
  <spine>
    <itemref idref="c1"/>
    <itemref idref="c2"/>
  </spine>
</package>"""

CH1 = """<html><head><title>Chapter 1: Limits</title></head>
<body><h1>Chapter 1: Limits</h1>
<p>The epsilon-delta definition pins down closeness.</p>
<img src="images/fig1.png"/>
</body></html>"""

CH2 = """<html><body><h1>Chapter 2: Continuity</h1>
<p>Continuity is a limit statement in disguise.</p></body></html>"""

PNG_1PX = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108020000009077"
    "53de0000000c4944415478da63f8cfc000000301010018dd8db00000000049"
    "454e44ae426082"
)


def make_epub() -> str:
    path = Path(tempfile.mkdtemp(prefix="cwyc-epub-")) / "book.epub"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr("META-INF/container.xml", CONTAINER)
        z.writestr("OEBPS/content.opf", OPF)
        z.writestr("OEBPS/ch1.xhtml", CH1)
        z.writestr("OEBPS/ch2.xhtml", CH2)
        z.writestr("OEBPS/images/fig1.png", PNG_1PX)
    return str(path)


class Ctx:
    col = None
    stats = None
    proposals = None
    config: dict = {}


class ReadEpubTests(unittest.TestCase):
    def test_listing_without_chapter(self) -> None:
        result = read_epub(Ctx(), {"path": make_epub()})
        chapters = result["chapters"]
        self.assertEqual(2, len(chapters))
        self.assertEqual("Chapter 1: Limits", chapters[0]["title"])
        self.assertGreater(chapters[0]["chars"], 10)

    def test_chapter_text_and_figures(self) -> None:
        result = read_epub(Ctx(), {"path": make_epub(), "chapter": 0})
        self.assertEqual("text", result[0]["type"])
        import json

        header = json.loads(result[0]["text"])
        self.assertIn("epsilon-delta definition", header["text"])
        self.assertEqual(1, header["figures"])
        self.assertEqual("image", result[1]["type"])
        self.assertEqual("image/png", result[1]["mimeType"])

    def test_file_uri_accepted(self) -> None:
        path = make_epub()
        result = read_epub(Ctx(), {"path": "file://" + path})
        self.assertEqual(2, len(result["chapters"]))

    def test_mobi_rejected_with_guidance(self) -> None:
        with self.assertRaises(ValueError) as caught:
            read_epub(Ctx(), {"path": "/books/thing.mobi"})
        self.assertIn("Calibre", str(caught.exception))

    def test_missing_and_bad_files(self) -> None:
        with self.assertRaises(ValueError) as missing:
            read_epub(Ctx(), {"path": "/nope/gone.epub"})
        self.assertIn("moved or renamed", str(missing.exception))
        bad = Path(tempfile.mkdtemp(prefix="cwyc-bad-")) / "not.epub"
        bad.write_text("plain text")
        with self.assertRaises(ValueError):
            read_epub(Ctx(), {"path": str(bad)})

    def test_chapter_out_of_range(self) -> None:
        with self.assertRaises(ValueError):
            read_epub(Ctx(), {"path": make_epub(), "chapter": 9})


if __name__ == "__main__":
    unittest.main()
