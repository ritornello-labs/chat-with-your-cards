"""Document tools beyond what the harness reads natively.

Research conclusion (2026-07-05): the Claude Code Read tool opens PDFs
natively — page ranges, rendered visually, figures included — so PDFs
need no bespoke tool. EPUB is not natively readable, but the format is
just a zip of XHTML, so read_epub below parses it with the stdlib
(AnkiWeb-friendly: no dependencies). MOBI/AZW3 are proprietary binary
formats and deliberately unsupported: the tool tells the agent to ask
the user to convert with Calibre.
"""

from __future__ import annotations

import base64
import posixpath
import re
import zipfile
from typing import Any
from xml.etree import ElementTree

from .registry import ToolContext, ToolRegistry, ToolSpec

MAX_CHAPTER_CHARS = 60_000
MAX_IMAGES = 4
MAX_IMAGE_BYTES = 3_500_000

_NS = {
    "cnt": "urn:oasis:names:tc:opendocument:xmlns:container",
    "opf": "http://www.idpf.org/2007/opf",
}
_IMG_MIME = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
             ".gif": "image/gif", ".webp": "image/webp"}
_TAG_RE = re.compile(r"<[^>]+>")
_TITLE_RE = re.compile(r"<(?:h1|h2|title)[^>]*>(.*?)</(?:h1|h2|title)>", re.IGNORECASE | re.DOTALL)
_IMG_SRC_RE = re.compile(r"""<img[^>]+src\s*=\s*["']([^"']+)["']""", re.IGNORECASE)
_BLOCK_RE = re.compile(r"</(?:p|div|h[1-6]|li|blockquote|tr)>", re.IGNORECASE)


def _spine_documents(archive: zipfile.ZipFile) -> list[str]:
    """Reading-order XHTML paths from container.xml -> OPF spine."""
    container = ElementTree.fromstring(archive.read("META-INF/container.xml"))
    rootfile = container.find(".//cnt:rootfile", _NS)
    if rootfile is None:
        raise ValueError("not a valid EPUB: no rootfile in container.xml")
    opf_path = rootfile.get("full-path", "")
    opf = ElementTree.fromstring(archive.read(opf_path))
    base = posixpath.dirname(opf_path)
    hrefs = {
        item.get("id"): item.get("href")
        for item in opf.findall(".//opf:manifest/opf:item", _NS)
    }
    docs = []
    for ref in opf.findall(".//opf:spine/opf:itemref", _NS):
        href = hrefs.get(ref.get("idref"))
        if href:
            docs.append(posixpath.normpath(posixpath.join(base, href)))
    return docs


def _html_to_text(html: str) -> str:
    html = re.sub(r"<(script|style)\b.*?</\1>", " ", html, flags=re.IGNORECASE | re.DOTALL)
    html = _BLOCK_RE.sub("\n", html)
    html = re.sub(r"<br\s*/?>", "\n", html, flags=re.IGNORECASE)
    text = _TAG_RE.sub("", html)
    text = text.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<")
    text = text.replace("&gt;", ">").replace("&quot;", '"').replace("&#39;", "'")
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _chapter_title(html: str, fallback: str) -> str:
    match = _TITLE_RE.search(html)
    if match:
        title = _TAG_RE.sub("", match.group(1)).strip()
        if title:
            return " ".join(title.split())[:120]
    return fallback


def read_epub(ctx: ToolContext, args: dict[str, Any]) -> Any:
    path = str(args.get("path", "")).strip()
    if path.lower().startswith("file://"):
        path = path[7:]
    if not path:
        raise ValueError("read_epub needs a path")
    if path.lower().endswith((".mobi", ".azw", ".azw3")):
        raise ValueError(
            "MOBI/AZW is a proprietary binary format this tool does not parse. "
            "Ask the user to convert the book to EPUB (Calibre does this in one "
            "step) and use that file instead."
        )
    try:
        archive = zipfile.ZipFile(path)
    except FileNotFoundError:
        raise ValueError(f"no such file: {path} (was it moved or renamed?)") from None
    except zipfile.BadZipFile:
        raise ValueError(f"{path} is not an EPUB (not a zip archive)") from None

    with archive:
        docs = _spine_documents(archive)
        chapter = args.get("chapter")
        if chapter is None:
            listing = []
            for index, doc in enumerate(docs):
                try:
                    html = archive.read(doc).decode("utf-8", "replace")
                except KeyError:
                    continue
                listing.append(
                    {
                        "chapter": index,
                        "title": _chapter_title(html, posixpath.basename(doc)),
                        "chars": len(_html_to_text(html)),
                    }
                )
            return {
                "path": path,
                "chapters": listing,
                "note": "Call read_epub again with a chapter index for its text "
                "and figures.",
            }

        index = int(chapter)
        if not 0 <= index < len(docs):
            raise ValueError(f"chapter must be 0..{len(docs) - 1}")
        doc = docs[index]
        html = archive.read(doc).decode("utf-8", "replace")
        text = _html_to_text(html)
        truncated = len(text) > MAX_CHAPTER_CHARS
        if truncated:
            text = text[:MAX_CHAPTER_CHARS]

        blocks: list[dict[str, Any]] = []
        skipped = []
        for src in _IMG_SRC_RE.findall(html):
            if len(blocks) >= MAX_IMAGES:
                skipped.append("image cap reached")
                break
            image_path = posixpath.normpath(posixpath.join(posixpath.dirname(doc), src))
            mime = _IMG_MIME.get(posixpath.splitext(image_path)[1].lower())
            if mime is None:
                continue
            try:
                data = archive.read(image_path)
            except KeyError:
                skipped.append(f"{src} missing")
                continue
            if len(data) > MAX_IMAGE_BYTES:
                skipped.append(f"{src} too large")
                continue
            blocks.append(
                {
                    "type": "image",
                    "data": base64.b64encode(data).decode("ascii"),
                    "mimeType": mime,
                }
            )
        header = {
            "path": path,
            "chapter": index,
            "title": _chapter_title(html, posixpath.basename(doc)),
            "text": text,
            "truncated": truncated,
            "figures": len(blocks),
            "figures_skipped": skipped,
        }
        import json as _json

        return [
            {"type": "text", "text": _json.dumps(header, ensure_ascii=False)},
            *blocks,
        ]


def register_document_tools(registry: ToolRegistry) -> None:
    registry.register(
        ToolSpec(
            "read_epub",
            "Read an EPUB book from the user's disk (card sources are often "
            "books). Without a chapter: lists chapters with titles and sizes. "
            "With chapter=N: returns that chapter's text plus its figures as "
            "images. PDFs do NOT need this - use the Read tool directly. "
            "MOBI/AZW is unsupported (ask the user to convert via Calibre).",
            {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Absolute path or file:// URI"},
                    "chapter": {"type": "integer", "description": "Chapter index from the listing"},
                },
                "required": ["path"],
            },
            read_epub,
        )
    )
