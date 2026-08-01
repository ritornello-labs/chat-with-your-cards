"""GUI handoff tools (#12): open Browse at a query, saved searches.

Handing off to Anki's real Browse window beats pasting text summaries
when the user says "show me those". Saved searches are collection
config (a write, so proposal-gated) - and per this workspace's APKG
lesson they are the shipping vehicle for curricula, since .apkg export
drops filtered decks.
"""

from __future__ import annotations

from typing import Any

from .registry import ToolContext, ToolRegistry, ToolSpec

SAVED_FILTERS_KEY = "savedFilters"


def open_browse(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    query = str(args.get("query", "")).strip()
    if not query:
        raise ValueError("open_browse needs a query")
    # Validate BEFORE opening a window on the user's screen: a typo'd search
    # should be a tool error, not a Browse window showing an Anki error.
    try:
        total = len(list(ctx.col.find_cards(query)))
    except Exception as exc:
        raise ValueError(f"invalid search {query!r}: {exc}") from None
    try:
        import aqt
        from aqt import mw
    except Exception:
        return {"opened": False, "reason": "Browse requires the Anki app"}
    aqt.dialogs.open("Browser", mw, search=(query,))
    return {
        "opened": True,
        "query": query,
        "matches": total,
        "note": "Anki's Browse window is now showing these cards",
    }


def list_saved_searches(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    saved = ctx.col.get_config(SAVED_FILTERS_KEY, {}) or {}
    return {
        "saved_searches": [
            {"name": name, "query": query} for name, query in sorted(saved.items())
        ],
        "note": "shown in Browse's sidebar; manage with manage_saved_search",
    }


def manage_saved_search(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    return ctx.proposals.submit_manage_saved_search(args)


def register_gui_tools(registry: ToolRegistry) -> None:
    registry.register(
        ToolSpec(
            "open_browse",
            "Open Anki's Browse window at a search - the real-UI handoff "
            "for 'show me those cards'. Validates the query first and "
            "reports the match count. Opens a window on the user's screen.",
            {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Anki search"},
                },
                "required": ["query"],
            },
            open_browse,
        )
    )
    registry.register(
        ToolSpec(
            "list_saved_searches",
            "The saved searches in Browse's sidebar (name + query).",
            {"type": "object", "properties": {}},
            list_saved_searches,
        )
    )
    registry.register(
        ToolSpec(
            "manage_saved_search",
            "Save a named search into Browse's sidebar, or delete one. THE "
            "shipping vehicle for curricula slices (filtered decks do not "
            "survive .apkg export; saved searches do, as collection "
            "config). One confirmation; revertible.",
            {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["save", "delete"]},
                    "name": {"type": "string"},
                    "query": {
                        "type": "string",
                        "description": "Anki search (save only)",
                    },
                    "rationale": {"type": "string"},
                },
                "required": ["action", "name"],
            },
            manage_saved_search,
            writes=True,
        )
    )