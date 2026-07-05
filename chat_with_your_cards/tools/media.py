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


_URI_RE = re.compile(
    r"""(?:href=["']([^"']+)["'])|((?:https?|file)://[^\s"'<>]+)|(/[^\s"'<>]*?\.pdf\b)""",
    re.IGNORECASE,
)


def extract_sources(
    fields: dict[str, str], allowed_fields: list[str] | None = None
) -> list[dict[str, str]]:
    """Source references in a note's fields (user decision 2026-07-05):
    any URI is a potential source; an optional per-note-type field
    restriction narrows where we look, default is all fields."""
    seen: set[str] = set()
    out: list[dict[str, str]] = []
    for name, value in fields.items():
        if allowed_fields and name not in allowed_fields:
            continue
        for match in _URI_RE.finditer(value):
            uri = next(g for g in match.groups() if g)
            uri = uri.strip().rstrip(".,;:)")
            if uri in seen or uri.lower().startswith("data:"):
                continue
            seen.add(uri)
            kind = "web"
            if uri.lower().startswith("file://") or uri.startswith("/"):
                kind = "pdf" if uri.lower().endswith(".pdf") else "file"
            elif uri.lower().endswith(".pdf"):
                kind = "pdf-url"
            out.append({"uri": uri, "field": name, "kind": kind})
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
        "note": "Open web sources with WebFetch; open local files (PDFs "
        "included) with the Read tool. A renamed/moved local file simply "
        "won't resolve - report that rather than guessing.",
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
