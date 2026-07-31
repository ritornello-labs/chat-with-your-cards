"""Deck management tools: deck CRUD, options presets, filtered decks.

Reads answer directly; writes submit to the ProposalManager like every
other write tool (one confirmation card per operation; direct apply
under trusted-writes within the session budget).
"""

from __future__ import annotations

from typing import Any

from .registry import ToolContext, ToolRegistry, ToolSpec

ORDER_LEGEND = (
    "order codes: 0 oldest seen first, 1 random, 2 increasing intervals, "
    "3 decreasing intervals, 4 most lapses, 5 order added, 6 order due, "
    "7 latest added first, 8 relative overdueness"
)


def get_deck_info(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    col = ctx.col
    name = str(args["deck"]).strip()
    try:
        did = int(col.decks.id_for_name(name))
    except Exception:
        did = 0
    if not did:
        all_names = [d.name for d in col.decks.all_names_and_ids()]
        similar = [n for n in all_names if name.lower() in n.lower()][:10]
        return {"exists": False, "deck": name, "similar_names": similar}
    deck = col.decks.get(did)
    all_names = [d.name for d in col.decks.all_names_and_ids()]
    escaped = name.replace('"', '\\"')
    info: dict[str, Any] = {
        "exists": True,
        "deck": name,
        "deck_id": did,
        "filtered": bool(deck.get("dyn")),
        "card_count": len(list(col.find_cards(f'deck:"{escaped}"'))),
        "subdecks": [n for n in all_names if n.startswith(name + "::")],
    }
    if not deck.get("dyn"):
        # Per-deck limits (#25) live on the deck, not the preset; today-only
        # entries carry the day they apply to and expire at rollover.
        today = int(col.sched.today)

        def limit_value(raw: Any) -> Any:
            if isinstance(raw, dict):
                active = int(raw.get("today", -1)) == today
                return {"limit": raw.get("limit"), "active_today": active}
            return raw

        info["deck_limits"] = {
            "new_limit": deck.get("newLimit"),
            "review_limit": deck.get("reviewLimit"),
            "new_limit_today": limit_value(deck.get("newLimitToday")),
            "review_limit_today": limit_value(deck.get("reviewLimitToday")),
            "note": "per-deck overrides (set_deck_limits); null = preset "
            "value applies",
        }
    if info["filtered"]:
        info["terms"] = [
            {"search": t[0], "limit": t[1], "order": t[2]}
            for t in (deck.get("terms") or [])
        ]
        info["reschedule"] = bool(deck.get("resched", True))
        info["order_legend"] = ORDER_LEGEND
    else:
        conf = col.decks.config_dict_for_deck_id(did)
        try:
            shared = sum(
                1 for d in col.decks.all() if d.get("conf") == conf.get("id")
            )
        except Exception:
            shared = 1
        info["options_preset"] = {
            "name": conf.get("name", ""),
            "shared_by_decks": shared,
            "config": conf,
            "note": "use dot paths from 'config' with set_deck_options, "
            "e.g. 'new.perDay' or 'rev.perDay'",
        }
    return info


def set_deck_limits(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    return ctx.proposals.submit_set_deck_limits(args)


def create_deck(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    return ctx.proposals.submit_create_deck(args)


def rename_deck(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    return ctx.proposals.submit_rename_deck(args)


def set_deck_options(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    return ctx.proposals.submit_set_deck_options(args)


def create_filtered_deck(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    return ctx.proposals.submit_create_filtered_deck(args)


def update_filtered_deck(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    return ctx.proposals.submit_update_filtered_deck(args)


def filtered_deck_action(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    return ctx.proposals.submit_filtered_deck_action(args)


_TERMS_SCHEMA = {
    "type": "array",
    "minItems": 1,
    "maxItems": 2,
    "description": "1-2 gather terms. " + ORDER_LEGEND,
    "items": {
        "type": "object",
        "properties": {
            "search": {"type": "string", "description": "Anki search query"},
            "limit": {"type": "integer", "default": 100},
            "order": {"type": "integer", "default": 6},
        },
        "required": ["search"],
    },
}


def register_deck_tools(registry: ToolRegistry) -> None:
    registry.register(
        ToolSpec(
            "get_deck_info",
            "One deck in detail: card count, subdecks, and either its "
            "options preset (full config with dot paths, plus how many decks "
            "share it) or, for filtered decks, its search terms and "
            "reschedule flag. Check this before set_deck_options or "
            "update_filtered_deck.",
            {
                "type": "object",
                "properties": {"deck": {"type": "string"}},
                "required": ["deck"],
            },
            get_deck_info,
        )
    )
    registry.register(
        ToolSpec(
            "create_deck",
            "Create a new (empty) deck. Missing parents in the path are "
            "created too. One confirmation. Note: proposing a note into a "
            "nonexistent deck already creates it - use this only when the "
            "user wants the deck itself.",
            {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Full path, e.g. 'Math::Series'"},
                    "rationale": {"type": "string"},
                },
                "required": ["name"],
            },
            create_deck,
            writes=True,
        )
    )
    registry.register(
        ToolSpec(
            "rename_deck",
            "Rename or move a deck (subdecks follow). Renaming to a path "
            "under another deck moves it there, e.g. 'Verbs' -> "
            "'Spanish::Verbs'. One confirmation; revertible.",
            {
                "type": "object",
                "properties": {
                    "deck": {"type": "string", "description": "Current full path"},
                    "new_name": {"type": "string", "description": "New full path"},
                    "rationale": {"type": "string"},
                },
                "required": ["deck", "new_name"],
            },
            rename_deck,
            writes=True,
        )
    )
    registry.register(
        ToolSpec(
            "set_deck_options",
            "Change a deck's options (new/day, reviews/day, learning steps, "
            "max interval, ...). Options live in a PRESET that may be shared "
            "by several decks - the proposal warns when it is, and "
            "get_deck_info shows the current config. Keys are dot paths into "
            "that config, e.g. {'new.perDay': 40, 'rev.perDay': 300}.",
            {
                "type": "object",
                "properties": {
                    "deck": {"type": "string"},
                    "options": {
                        "type": "object",
                        "description": "Dot path -> new value; only existing "
                        "keys are accepted",
                        "additionalProperties": True,
                    },
                    "rationale": {"type": "string"},
                },
                "required": ["deck", "options"],
            },
            set_deck_options,
            writes=True,
        )
    )
    registry.register(
        ToolSpec(
            "set_deck_limits",
            "Per-DECK study limits, separate from the options preset: "
            "today-only overrides that expire at the next day rollover, and "
            "permanent per-deck caps. THE tool for 'no new cards just for "
            "today' (new_limit_today=0) and 'let me do 60 extra reviews "
            "today' (review_limit_today above the preset). -1 clears an "
            "override. v3 note: a parent's limit caps its whole subtree, so "
            "zeroing the parent alone silences it; RAISING limits may need "
            "include_subdecks since each child's own limit still applies. "
            "Revertible.",
            {
                "type": "object",
                "properties": {
                    "deck": {"type": "string", "description": "Full deck path"},
                    "new_limit_today": {
                        "type": "integer",
                        "minimum": -1,
                        "description": "Today's new-card cap; 0 = no new "
                        "cards today; -1 clears",
                    },
                    "review_limit_today": {
                        "type": "integer",
                        "minimum": -1,
                        "description": "Today's review cap; -1 clears",
                    },
                    "new_limit": {
                        "type": "integer",
                        "minimum": -1,
                        "description": "Permanent per-deck new cap "
                        "(overrides the preset); -1 clears",
                    },
                    "review_limit": {
                        "type": "integer",
                        "minimum": -1,
                        "description": "Permanent per-deck review cap; -1 clears",
                    },
                    "include_subdecks": {"type": "boolean", "default": False},
                    "rationale": {"type": "string"},
                },
                "required": ["deck"],
            },
            set_deck_limits,
            writes=True,
        )
    )
    registry.register(
        ToolSpec(
            "create_filtered_deck",
            "Create a filtered (dynamic) deck from Anki searches and build "
            "it immediately. Good for cram sessions, catching up a backlog, "
            "or studying a slice (tag, due status) across decks. Suspended "
            "cards and cards already in another filtered deck are never "
            "gathered.",
            {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "terms": _TERMS_SCHEMA,
                    "reschedule": {
                        "type": "boolean",
                        "default": True,
                        "description": "false = reviews here do not affect "
                        "normal scheduling (pure cramming)",
                    },
                    "rationale": {"type": "string"},
                },
                "required": ["name", "terms"],
            },
            create_filtered_deck,
            writes=True,
        )
    )
    registry.register(
        ToolSpec(
            "update_filtered_deck",
            "Change an existing filtered deck's search terms and/or "
            "reschedule flag, then rebuild it. Revertible (restores the "
            "previous configuration and rebuilds).",
            {
                "type": "object",
                "properties": {
                    "deck": {"type": "string"},
                    "terms": _TERMS_SCHEMA,
                    "reschedule": {"type": "boolean"},
                    "rationale": {"type": "string"},
                },
                "required": ["deck"],
            },
            update_filtered_deck,
            writes=True,
        )
    )
    registry.register(
        ToolSpec(
            "filtered_deck_action",
            "Rebuild filtered decks (re-gather with current terms) or empty "
            "them (cards return home). Batch-friendly: pass one `deck`, a "
            "`decks` list, or a `pattern` glob over filtered-deck names "
            "(e.g. 'Cram::*') - ONE review card either way, with per-deck "
            "gather counts on the resolved card.",
            {
                "type": "object",
                "properties": {
                    "deck": {"type": "string", "description": "One filtered deck"},
                    "decks": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Several filtered decks; exactly one "
                        "of deck / decks / pattern",
                    },
                    "pattern": {
                        "type": "string",
                        "description": "Glob over filtered-deck names, e.g. "
                        "'Cram::*' or '*Backlog*'; normal decks it sweeps up "
                        "are skipped",
                    },
                    "action": {"type": "string", "enum": ["rebuild", "empty"]},
                    "rationale": {"type": "string"},
                },
                "required": ["action"],
            },
            filtered_deck_action,
            writes=True,
        )
    )
