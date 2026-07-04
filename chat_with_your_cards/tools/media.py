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


def register_media_tools(registry: ToolRegistry) -> None:
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
