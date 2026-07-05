"""Read tools over the Anki collection.

All functions run on Anki's main thread (the MCP layer marshals them
there) and receive a ToolContext with the live collection plus the
cached stats snapshot. Returns are JSON-serializable dicts.
"""

from __future__ import annotations

import re
from typing import Any

from .registry import ToolContext, ToolRegistry, ToolSpec

SNIPPET_CHARS = 120
DEFAULT_SEARCH_LIMIT = 20
MAX_SEARCH_LIMIT = 100

_TAG_STRIP = re.compile(r"<[^>]+>")


def _plain(text: str, limit: int = SNIPPET_CHARS) -> str:
    text = _TAG_STRIP.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[: limit - 1] + "…" if len(text) > limit else text


def _note_summary(col: Any, note_id: int) -> dict[str, Any]:
    note = col.get_note(note_id)
    fields = dict(note.items())
    first_two = list(fields.items())[:2]
    return {
        "note_id": note_id,
        "note_type": note.note_type()["name"],
        "tags": note.tags,
        "fields_preview": {name: _plain(value) for name, value in first_two},
    }


def search_notes(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    query = str(args["query"])
    limit = min(int(args.get("limit", DEFAULT_SEARCH_LIMIT)), MAX_SEARCH_LIMIT)
    note_ids = list(ctx.col.find_notes(query))
    return {
        "query": query,
        "total": len(note_ids),
        "shown": min(limit, len(note_ids)),
        "notes": [_note_summary(ctx.col, nid) for nid in note_ids[:limit]],
    }


def get_note(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    note = ctx.col.get_note(int(args["note_id"]))
    cards = []
    for card in note.cards():
        deck = ctx.col.decks.name(card.did)
        cards.append({"card_id": card.id, "deck": deck, "template_ordinal": card.ord})
    return {
        "note_id": note.id,
        "note_type": note.note_type()["name"],
        "tags": note.tags,
        "fields": dict(note.items()),
        "cards": cards,
    }


def get_card(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    card = ctx.col.get_card(int(args["card_id"]))
    note = card.note()
    template = card.template()
    return {
        "card_id": card.id,
        "note_id": note.id,
        "deck": ctx.col.decks.name(card.did),
        "note_type": note.note_type()["name"],
        "template": template.get("name"),
        "tags": note.tags,
        "fields": dict(note.items()),
        "scheduling": {
            "type": card.type,
            "queue": card.queue,
            "interval_days": card.ivl,
            "ease_factor": card.factor,
            "reps": card.reps,
            "lapses": card.lapses,
            "due": card.due,
        },
    }


def _tree_from_stats(ctx: ToolContext, key: str, prefix: str | None) -> dict[str, Any]:
    stats = ctx.stats
    if not stats:
        return {"available": False, "reason": "stats cache not computed yet"}
    items = stats.get(key, [])
    if prefix:
        items = [item for item in items if item["name"].startswith(prefix)]
    return {
        "available": True,
        "computed_at": stats.get("computed_at"),
        key: items,
    }


def deck_tree(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    return _tree_from_stats(ctx, "decks", args.get("prefix"))


def tag_tree(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    return _tree_from_stats(ctx, "tags", args.get("prefix"))


def collection_stats(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    stats = ctx.stats
    if not stats:
        return {"available": False, "reason": "stats cache not computed yet"}
    return {
        "available": True,
        "computed_at": stats.get("computed_at"),
        "totals": stats.get("totals", {}),
        "note_types": stats.get("note_types", []),
    }


def list_note_types(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    models = []
    for nt in ctx.col.models.all_names_and_ids():
        models.append({"name": nt.name, "id": nt.id})
    return {"note_types": models}


def get_note_type(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    model = ctx.col.models.by_name(str(args["name"]))
    if model is None:
        raise ValueError(f"note type not found: {args['name']!r}")
    return {
        "name": model["name"],
        "fields": [f["name"] for f in model["flds"]],
        "templates": [t["name"] for t in model["tmpls"]],
    }


_PREFIX_RE = re.compile(r"^([A-Za-z][\w /&-]{0,30}):\s+\S")


def extract_field_prefixes(field_values: list[str]) -> list[str]:
    """Clue heuristic: leading 'Topic:' prefixes in field values."""
    prefixes = []
    for value in field_values:
        match = _PREFIX_RE.match(_plain(value, 200))
        if match:
            prefixes.append(match.group(1))
    return sorted(set(prefixes))


def find_related(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """Clue-based related-card search: prefixes, tags, deck, keywords."""
    limit = min(int(args.get("limit", 10)), MAX_SEARCH_LIMIT)
    if "note_id" in args:
        note = ctx.col.get_note(int(args["note_id"]))
        deck_name = ctx.col.decks.name(note.cards()[0].did) if note.cards() else None
    elif "card_id" in args:
        card = ctx.col.get_card(int(args["card_id"]))
        note = card.note()
        deck_name = ctx.col.decks.name(card.did)
    else:
        raise ValueError("find_related needs note_id or card_id")

    queries: list[str] = []
    for prefix in extract_field_prefixes(list(dict(note.items()).values())):
        queries.append(f'"{prefix}:*"')
    for tag in note.tags[:5]:
        queries.append(f'tag:"{tag}"')
    if deck_name:
        queries.append(f'deck:"{deck_name}"')

    seen: dict[int, dict[str, Any]] = {}
    per_query: list[dict[str, Any]] = []
    for query in queries:
        try:
            ids = list(ctx.col.find_notes(query))
        except Exception as exc:
            per_query.append({"query": query, "error": str(exc)})
            continue
        per_query.append({"query": query, "total": len(ids)})
        for nid in ids:
            if nid == note.id or nid in seen:
                continue
            if len(seen) < limit:
                seen[nid] = _note_summary(ctx.col, nid)
    return {
        "source_note_id": note.id,
        "queries": per_query,
        "related": list(seen.values()),
    }


def build_registry() -> ToolRegistry:
    registry = ToolRegistry()
    specs = [
        ToolSpec(
            "search_notes",
            "Search the collection with Anki search syntax (e.g. deck:\"X\", "
            "tag:foo, field content words). Returns note summaries.",
            {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Anki search query"},
                    "limit": {"type": "integer", "default": DEFAULT_SEARCH_LIMIT},
                },
                "required": ["query"],
            },
            search_notes,
        ),
        ToolSpec(
            "get_note",
            "Fetch one note: all fields, tags, note type, and its cards.",
            {
                "type": "object",
                "properties": {"note_id": {"type": "integer"}},
                "required": ["note_id"],
            },
            get_note,
        ),
        ToolSpec(
            "get_card",
            "Fetch one card: fields, deck, template, tags, and scheduling state.",
            {
                "type": "object",
                "properties": {"card_id": {"type": "integer"}},
                "required": ["card_id"],
            },
            get_card,
        ),
        ToolSpec(
            "deck_tree",
            "Full deck list annotated with note/card counts and review time "
            "(cached). Optional name prefix filter, e.g. 'Spanish::'.",
            {
                "type": "object",
                "properties": {"prefix": {"type": "string"}},
            },
            deck_tree,
        ),
        ToolSpec(
            "tag_tree",
            "Full tag list annotated with note counts (cached). Optional "
            "prefix filter.",
            {
                "type": "object",
                "properties": {"prefix": {"type": "string"}},
            },
            tag_tree,
        ),
        ToolSpec(
            "collection_stats",
            "Collection totals and note-type counts from the cached stats "
            "snapshot, with its computed_at timestamp.",
            {"type": "object", "properties": {}},
            collection_stats,
        ),
        ToolSpec(
            "list_note_types",
            "List all note type names.",
            {"type": "object", "properties": {}},
            list_note_types,
        ),
        ToolSpec(
            "get_note_type",
            "Field names and card template names for one note type.",
            {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
            get_note_type,
        ),
        ToolSpec(
            "find_related",
            "Find cards related to a note/card using clues: field prefixes "
            "(like 'Analysis:'), shared tags, and the deck. Merged and "
            "deduplicated; also reports which clue queries were run.",
            {
                "type": "object",
                "properties": {
                    "note_id": {"type": "integer"},
                    "card_id": {"type": "integer"},
                    "limit": {"type": "integer", "default": 10},
                },
            },
            find_related,
        ),
    ]
    for spec in specs:
        registry.register(spec)
    from .documents import register_document_tools
    from .learning import register_learning_tools
    from .media import register_media_tools
    from .proposals import register_proposal_tools

    register_media_tools(registry)
    register_document_tools(registry)
    register_proposal_tools(registry)
    register_learning_tools(registry)
    return registry
