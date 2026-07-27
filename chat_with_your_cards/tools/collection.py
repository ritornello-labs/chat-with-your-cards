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
# Card templates / note-type CSS are returned VERBATIM (not stripped): the
# agent needs the real markup to diagnose rendering - an <iframe src=...>,
# a conditional section, a CSS rule. Generous per-string cap so a pathological
# note type can't flood the context, and truncation is always announced (a
# silent cut would hide the very line being debugged).
MAX_TEMPLATE_CHARS = 20_000
# Ceiling for an explicit `max_chars` override. The default keeps a routine
# read cheap; this bounds how much one call can ever return.
HARD_MAX_TEMPLATE_CHARS = 200_000
HIDDEN_QUEUE_NAMES = {
    -1: "suspended",
    -2: "scheduler-buried",
    -3: "manually buried",
}

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


def _deck_name(col: Any, deck_id: int) -> str:
    try:
        return str(col.decks.name(deck_id))
    except Exception:
        return f"[missing deck {deck_id}]"


def _card_summary(col: Any, card_id: int) -> dict[str, Any]:
    card = col.get_card(card_id)
    note = card.note()
    fields = dict(note.items())
    current_deck_id = int(card.did)
    original_deck_id = int(getattr(card, "odid", 0))
    home_deck_id = original_deck_id or current_deck_id
    queue = int(card.queue)
    try:
        template = str(card.template().get("name", ""))
    except Exception:
        template = ""
    return {
        "card_id": int(card.id),
        "note_id": int(note.id),
        "note_type": str(note.note_type()["name"]),
        "template": template,
        "template_ordinal": int(card.ord),
        "tags": list(note.tags),
        "fields_preview": {
            name: _plain(value) for name, value in list(fields.items())[:2]
        },
        "current_deck": _deck_name(col, current_deck_id),
        "current_deck_id": current_deck_id,
        "home_deck": _deck_name(col, home_deck_id),
        "home_deck_id": home_deck_id,
        "in_filtered_deck": bool(original_deck_id),
        "hidden_state": HIDDEN_QUEUE_NAMES.get(queue),
        "scheduling": {
            "type": int(card.type),
            "queue": queue,
            "due": int(card.due),
            "interval_days": int(card.ivl),
            "ease_factor": int(card.factor),
            "reps": int(card.reps),
            "lapses": int(card.lapses),
            "user_flag": int(getattr(card, "flags", 0)) & 0x7,
        },
    }


def find_cards(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """Search without collapsing card-level matches to their parent notes."""
    query = str(args["query"])
    limit = max(1, min(int(args.get("limit", DEFAULT_SEARCH_LIMIT)), MAX_SEARCH_LIMIT))
    offset = max(0, int(args.get("offset", 0)))
    card_ids = [int(card_id) for card_id in ctx.col.find_cards(query)]
    page_ids = card_ids[offset : offset + limit]
    next_offset = offset + len(page_ids)
    return {
        "query": query,
        "total": len(card_ids),
        "offset": offset,
        "shown": len(page_ids),
        "next_offset": next_offset if next_offset < len(card_ids) else None,
        "cards": [_card_summary(ctx.col, card_id) for card_id in page_ids],
        "selection_note": (
            "These are exact card matches, not authorization to modify every result. "
            "Inspect and select only the intended card IDs before a card-level write."
        ),
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
    current_deck_id = int(card.did)
    original_deck_id = int(getattr(card, "odid", 0))
    home_deck_id = original_deck_id or current_deck_id
    queue = int(card.queue)
    return {
        "card_id": card.id,
        "note_id": note.id,
        "deck": _deck_name(ctx.col, current_deck_id),
        "current_deck": _deck_name(ctx.col, current_deck_id),
        "current_deck_id": current_deck_id,
        "home_deck": _deck_name(ctx.col, home_deck_id),
        "home_deck_id": home_deck_id,
        "in_filtered_deck": bool(original_deck_id),
        "note_type": note.note_type()["name"],
        "template": template.get("name"),
        "template_ordinal": int(card.ord),
        "tags": note.tags,
        "fields": dict(note.items()),
        "hidden_state": HIDDEN_QUEUE_NAMES.get(queue),
        "scheduling": {
            "type": card.type,
            "queue": queue,
            "interval_days": card.ivl,
            "ease_factor": card.factor,
            "reps": card.reps,
            "lapses": card.lapses,
            "due": card.due,
            "user_flag": int(getattr(card, "flags", 0)) & 0x7,
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


def get_collection_overview(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """The compact annotated deck+tag overview (stats.serialize_overview) on
    demand. This USED to be injected into the first user message of every
    session (~8k tokens on a large collection, paid whether or not the
    conversation needed it); it is a tool now (design change 2026-07-14) so
    a session only spends those tokens when the agent actually wants
    collection structure."""
    from ..stats import serialize_overview

    stats = ctx.stats
    if not stats:
        return {"available": False, "reason": "stats cache not computed yet"}
    default_budget = int(ctx.config.get("context_token_budget", 8000))
    budget = int(args.get("budget_tokens") or default_budget)
    return {
        "available": True,
        "computed_at": stats.get("computed_at"),
        "overview": serialize_overview(stats, budget),
    }


def list_note_types(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    models = []
    for nt in ctx.col.models.all_names_and_ids():
        models.append({"name": nt.name, "id": nt.id})
    return {"note_types": models}


def _template_source(text: str, limit: int, offset: int = 0) -> str:
    """Card-template / CSS source, verbatim but bounded. Truncation always
    says how to read the REST from here - telling the agent to open Anki
    would be a dead end, since opening Anki is the one thing it cannot do."""
    text = str(text or "")
    total = len(text)
    window = text[offset : offset + limit]
    notes = []
    if offset:
        notes.append(f"offset {offset} of {total}")
    remaining = total - (offset + len(window))
    if remaining > 0:
        notes.append(
            f"{remaining} more characters - call get_note_type again with "
            f"offset={offset + len(window)} (and max_chars up to "
            f"{HARD_MAX_TEMPLATE_CHARS}) to continue"
        )
    if not notes:
        return window
    return window + "\n<!-- chat-with-your-cards: " + "; ".join(notes) + " -->"


def get_note_type(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    model = ctx.col.models.by_name(str(args["name"]))
    if model is None:
        raise ValueError(f"note type not found: {args['name']!r}")
    # The agent can widen the window (up to a hard ceiling) or page through a
    # pathological template, so nothing is permanently out of reach.
    limit = max(1, min(int(args.get("max_chars", MAX_TEMPLATE_CHARS)), HARD_MAX_TEMPLATE_CHARS))
    offset = max(0, int(args.get("offset", 0)))
    return {
        "name": model["name"],
        "fields": [f["name"] for f in model["flds"]],
        # Full front/back template source + the note type's CSS. Without these
        # the agent is blind to how a note actually RENDERS: it could see the
        # fields but not the <iframe src="{{Wikipedia}}"> using them, so it
        # could not diagnose a rendering bug and had to ask the user to paste
        # the template (dogfood 2026-07-23).
        "templates": [
            {
                "name": t["name"],
                "qfmt": _template_source(t.get("qfmt", ""), limit, offset),
                "afmt": _template_source(t.get("afmt", ""), limit, offset),
            }
            for t in model["tmpls"]
        ],
        "css": _template_source(model.get("css", ""), limit, offset),
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
            "find_cards",
            "Search with Anki syntax and return the exact matching cards rather "
            "than collapsing matches to notes. Use this before any card-level "
            "operation when exact IDs are not already known. Results are "
            "candidates: inspect and select the intended cards instead of "
            "blindly modifying every broad-search match. Page with offset. "
            "COUNTING: `total` is always the full number of matches, whatever "
            "`limit` is - so for a count alone pass limit=1 and read `total`, "
            "rather than fetching a page you do not need.",
            {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Anki search query"},
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": MAX_SEARCH_LIMIT,
                        "default": DEFAULT_SEARCH_LIMIT,
                        "description": "How many cards to return in this page. "
                        "Does NOT affect `total`; use 1 when you only need the count.",
                    },
                    "offset": {
                        "type": "integer",
                        "minimum": 0,
                        "default": 0,
                    },
                },
                "required": ["query"],
                "additionalProperties": False,
            },
            find_cards,
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
            "get_collection_overview",
            "Compact human-readable overview of the whole collection: deck "
            "hierarchy and top tags annotated with counts and due/review "
            "load. START HERE when you need the collection's structure; "
            "deck_tree/tag_tree/collection_stats drill into details.",
            {
                "type": "object",
                "properties": {
                    "budget_tokens": {
                        "type": "integer",
                        "description": "Approximate size cap; the overview "
                        "folds deeper levels to fit (default from config).",
                    }
                },
            },
            get_collection_overview,
        ),
        ToolSpec(
            "list_note_types",
            "List all note type names.",
            {"type": "object", "properties": {}},
            list_note_types,
        ),
        ToolSpec(
            "get_note_type",
            "One note type's field names, its card templates INCLUDING the full "
            "front/back template source (qfmt/afmt), and the note-type CSS. Use "
            "this to see how a card actually renders - conditional sections, "
            "embedded <iframe>/<img>, styling - not just which fields exist.",
            {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "max_chars": {
                        "type": "integer",
                        "description": "Widen the per-string source window "
                        f"(default {MAX_TEMPLATE_CHARS}, max "
                        f"{HARD_MAX_TEMPLATE_CHARS}).",
                    },
                    "offset": {
                        "type": "integer",
                        "description": "Start reading each source string here; "
                        "use the offset named in a truncation note to page "
                        "through a long template or stylesheet.",
                    },
                },
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
    from .decks import register_deck_tools
    from .documents import register_document_tools
    from .grading import register_grading_tools
    from .learning import register_learning_tools
    from .media import register_media_tools
    from .proposals import register_proposal_tools
    from .skills import register_skill_tools
    from .widgets import register_widget_tools

    register_media_tools(registry)
    register_widget_tools(registry)
    register_document_tools(registry)
    register_proposal_tools(registry)
    register_grading_tools(registry)
    register_deck_tools(registry)
    register_learning_tools(registry)
    register_skill_tools(registry)
    return registry
