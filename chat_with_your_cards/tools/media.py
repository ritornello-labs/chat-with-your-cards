"""Media tools: let the agent actually see a card's images.

Card fields are HTML; the agent otherwise only sees `<img src="x.jpg">`
as text (a filename, not pixels). get_card_images loads the referenced
files from the collection's media folder and returns them as MCP image
content blocks, which the CLI's MCP client surfaces to the model.

Audio is deliberately not handled: the model cannot ingest audio, so a
`[sound:...]` reference would need a transcription step that is out of
scope here.
"""

from __future__ import annotations

import base64
import json
import os
import re
from typing import Any

from .registry import ToolContext, ToolRegistry, ToolSpec

_IMG_SRC_RE = re.compile(r"""<img[^>]+src\s*=\s*["']([^"']+)["']""", re.IGNORECASE)
_MIME_BY_EXT = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
}
MAX_IMAGES = 4
MAX_IMAGE_BYTES = 3_500_000


def image_filenames(field_values: list[str]) -> list[str]:
    """Local <img src> filenames across a note's fields, de-duplicated.

    Remote (`http(s)://`) and inline `data:` sources are skipped: we have
    no local bytes for them and the agent cannot fetch them itself.
    """
    seen: set[str] = set()
    out: list[str] = []
    for value in field_values:
        for match in _IMG_SRC_RE.finditer(value):
            src = match.group(1).strip()
            if "://" in src or src.lower().startswith("data:"):
                continue
            if src not in seen:
                seen.add(src)
                out.append(src)
    return out


def _note_from_args(ctx: ToolContext, args: dict[str, Any]) -> Any:
    if args.get("card_id") is not None:
        return ctx.col.get_card(int(args["card_id"])).note()
    if args.get("note_id") is not None:
        return ctx.col.get_note(int(args["note_id"]))
    raise ValueError("get_card_images needs card_id or note_id")


def get_card_images(ctx: ToolContext, args: dict[str, Any]) -> list[dict[str, Any]]:
    note = _note_from_args(ctx, args)
    names = image_filenames(list(dict(note.items()).values()))
    media_dir = ctx.col.media.dir()

    blocks: list[dict[str, Any]] = []
    skipped: list[str] = []
    for name in names:
        if len(blocks) >= MAX_IMAGES:
            skipped.append(f"{len(names) - len(blocks)} more not shown (cap {MAX_IMAGES})")
            break
        mime = _MIME_BY_EXT.get(os.path.splitext(name)[1].lower())
        path = os.path.join(media_dir, name)
        if mime is None:
            skipped.append(f"{name} (unsupported type)")
            continue
        if not os.path.exists(path):
            skipped.append(f"{name} (missing from media)")
            continue
        size = os.path.getsize(path)
        if size > MAX_IMAGE_BYTES:
            skipped.append(f"{name} (too large: {size // 1024}KB)")
            continue
        with open(path, "rb") as handle:
            data = base64.b64encode(handle.read()).decode("ascii")
        blocks.append({"type": "image", "data": data, "mimeType": mime})

    header = f"{len(blocks)} image(s) from note {note.id}"
    if not names:
        header = f"note {note.id} has no local images"
    if skipped:
        header += "; skipped: " + ", ".join(skipped)
    return [{"type": "text", "text": header}, *blocks]


_ANCHOR_RE = re.compile(r"<a\b[^>]*>", re.IGNORECASE)
_HREF_RE = re.compile(r"""href=["']([^"']+)["']""", re.IGNORECASE)
_DATA_SOURCE_RE = re.compile(r"""data-source=(?:'({[^']*})'|"({[^"]*})")""", re.IGNORECASE)
_URI_RE = re.compile(
    r"""(?:href=["']([^"']+)["'])|((?:https?|file)://[^\s"'<>]+)|(/[^\s"'<>]*?\.(?:pdf|epub)\b)""",
    re.IGNORECASE,
)
_PAGE_FRAGMENT_RE = re.compile(r"#page=(\d+)", re.IGNORECASE)


def _source_kind(uri: str) -> str:
    lowered = uri.lower().split("#", 1)[0]
    if lowered.endswith(".pdf"):
        return "pdf" if (uri.startswith("/") or lowered.startswith("file://")) else "pdf-url"
    if lowered.endswith((".epub", ".mobi", ".azw3")):
        return "ebook"
    if lowered.startswith("file://") or uri.startswith("/"):
        return "file"
    return "web"


def extract_sources(
    fields: dict[str, str], allowed_fields: list[str] | None = None
) -> list[dict[str, Any]]:
    """Source references in a note's fields (user decisions 2026-07-05).

    Any URI is a potential source; an optional per-note-type field
    restriction narrows where we look (default: all fields). Position
    metadata rides along two ways:
    - a standard `#page=N` URI fragment -> meta {"page": N}
    - a `data-source='{...json...}'` attribute on the anchor (page,
      chapter, section, quote, ...) -> merged into meta verbatim
    """
    seen: set[str] = set()
    out: list[dict[str, Any]] = []

    def add(name: str, uri: str, meta: dict[str, Any]) -> None:
        uri = uri.strip().rstrip(".,;:)")
        if not uri or uri in seen or uri.lower().startswith("data:"):
            return
        fragment = _PAGE_FRAGMENT_RE.search(uri)
        if fragment and "page" not in meta:
            meta["page"] = int(fragment.group(1))
        seen.add(uri)
        entry: dict[str, Any] = {"uri": uri, "field": name, "kind": _source_kind(uri)}
        if meta:
            entry["meta"] = meta
        out.append(entry)

    for name, value in fields.items():
        if allowed_fields and name not in allowed_fields:
            continue
        # Anchors first: they may carry data-source position metadata.
        for tag in _ANCHOR_RE.findall(value):
            href = _HREF_RE.search(tag)
            if not href:
                continue
            meta: dict[str, Any] = {}
            data = _DATA_SOURCE_RE.search(tag)
            if data:
                try:
                    parsed = json.loads(data.group(1) or data.group(2))
                    if isinstance(parsed, dict):
                        meta.update(parsed)
                except ValueError:
                    pass
            add(name, href.group(1), meta)
        # Then bare URLs / file paths outside anchors.
        for match in _URI_RE.finditer(value):
            uri = next(g for g in match.groups() if g)
            add(name, uri, {})
    return out


def get_card_sources(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    note = _note_from_args(ctx, args)
    fields = dict(note.items())
    note_type = note.note_type()["name"]
    restriction = None
    config = getattr(ctx, "config", None) or {}
    source_fields = config.get("source_fields") or {}
    if isinstance(source_fields, dict) and source_fields.get(note_type):
        restriction = [str(f) for f in source_fields[note_type]]
    sources = extract_sources(fields, restriction)
    return {
        "note_id": note.id,
        "sources": sources,
        "note": "Open web sources with WebFetch; local PDFs with the Read "
        "tool (use meta.page to jump straight to the right pages); EPUBs "
        "with read_epub. A renamed/moved local file simply won't resolve - "
        "report that rather than guessing.",
    }


def register_media_tools(registry: ToolRegistry) -> None:
    registry.register(
        ToolSpec(
            "get_card_sources",
            "Find the sources a card came from: any URI in its fields (web "
            "links, file:// links, absolute .pdf paths). Use when the user "
            "asks about the material behind a card or wants new cards from "
            "the same source - then open web sources with WebFetch and local "
            "files with Read.",
            {
                "type": "object",
                "properties": {
                    "card_id": {"type": "integer"},
                    "note_id": {"type": "integer"},
                },
            },
            get_card_sources,
        )
    )
    registry.register(
        ToolSpec(
            "get_card_images",
            "View the actual images embedded in a card/note (not just their "
            "filenames). Returns the image content itself. Use this whenever "
            "the visual matters - e.g. the card shows a picture and the user "
            "asks about what is shown.",
            {
                "type": "object",
                "properties": {
                    "card_id": {"type": "integer"},
                    "note_id": {"type": "integer"},
                },
            },
            get_card_images,
        )
    )
