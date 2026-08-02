"""Write tools: note proposals (DESIGN.md section 5).

These never write to the collection directly - they submit to the
ProposalManager, which validates, applies pins, and either renders a
proposal card for the user or (auto-accept mode, creations only)
applies immediately under the session cap.
"""

from __future__ import annotations

from typing import Any

from .registry import ToolContext, ToolRegistry, ToolSpec


def propose_note(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    return ctx.proposals.submit_create(args)


def propose_note_edit(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    return ctx.proposals.submit_edit(args)


def rename_tag(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    return ctx.proposals.submit_rename_tag(args)


def find_replace(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    return ctx.proposals.submit_find_replace(args)


def move_cards(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    return ctx.proposals.submit_move_cards(args)


def delete_notes(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    return ctx.proposals.submit_delete_notes(args)


def _bulk_tags(op: str):
    def call(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
        return ctx.proposals.submit_bulk_tags({**args, "op": op})

    return call


def clear_unused_tags(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    return ctx.proposals.submit_clear_unused_tags(args)


def _bulk_tags_schema(op: str) -> dict[str, Any]:
    tag_desc = (
        "Tags to add (use :: for hierarchy)"
        if op == "add_tags"
        else "Tags to remove. No wildcards - removing 'X' also removes its "
        "'X::child' tags (Anki's hierarchy semantics)."
    )
    return {
        "type": "object",
        "properties": {
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
                "description": tag_desc,
            },
            "note_ids": {
                "type": "array",
                "items": {"type": "integer"},
                "description": "Exact note ids (from search_notes detail='ids'). "
                "Exactly one of note_ids / query.",
            },
            "query": {
                "type": "string",
                "description": "Anki search selecting the notes. Exactly one "
                "of note_ids / query.",
            },
            "rationale": {"type": "string"},
        },
        "required": ["tags"],
    }


def _card_state(op: str):
    def call(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
        return ctx.proposals.submit_card_state({**args, "op": op})

    return call


def _scheduling(op: str):
    def call(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
        return ctx.proposals.submit_scheduling({**args, "op": op})

    return call


def _scheduling_schema(extra: dict[str, Any], required: list[str]) -> dict[str, Any]:
    properties: dict[str, Any] = {
        "card_ids": {
            "type": "array",
            "items": {"type": "integer"},
            "description": "Exact card ids (from find_cards). Exactly one of "
            "card_ids / query.",
        },
        "query": {
            "type": "string",
            "description": "Anki search selecting the cards. Exactly one of "
            "card_ids / query.",
        },
        "rationale": {"type": "string"},
    }
    properties.update(extra)
    return {"type": "object", "properties": properties, "required": required}


# Shared schema for the card-state ops: exactly one of card_ids / query.
def _card_state_schema(extra: dict[str, Any] | None = None) -> dict[str, Any]:
    properties: dict[str, Any] = {
        "card_ids": {
            "type": "array",
            "items": {"type": "integer"},
            "description": "Exact card ids (from find_cards). Use for a "
            "hand-picked selection; use `query` for everything matching a "
            "search. Exactly one of the two.",
        },
        "query": {
            "type": "string",
            "description": "Anki search selecting the cards, e.g. "
            "'deck:\"X\" tag:leech'. Exactly one of card_ids / query.",
        },
        "rationale": {"type": "string"},
    }
    if extra:
        properties.update(extra)
    return {"type": "object", "properties": properties, "required": list(extra or [])}


def open_change_set(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    return ctx.proposals.open_change_set(args)


def add_to_change_set(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    return ctx.proposals.add_to_change_set(args)


def close_change_set(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    return ctx.proposals.close_change_set(args)


def register_proposal_tools(registry: ToolRegistry) -> None:
    registry.register(
        ToolSpec(
            "propose_note",
            "Propose a new note for the user to review. Never writes directly: "
            "the user sees a proposal card (fields, deck, tags, rendered card "
            "preview) and accepts, edits, or rejects it. Respect pinned deck / "
            "note type / tags when the context mentions pins.",
            {
                "type": "object",
                "properties": {
                    "note_type": {
                        "type": "string",
                        "description": "Note type name (see list_note_types)",
                    },
                    "deck": {
                        "type": "string",
                        "description": "Full deck path, e.g. 'Math::Analysis'",
                    },
                    "tags": {"type": "array", "items": {"type": "string"}},
                    "fields": {
                        "type": "object",
                        "description": "Field name -> value (HTML allowed)",
                        "additionalProperties": {"type": "string"},
                    },
                    "rationale": {
                        "type": "string",
                        "description": "One sentence: why this note helps the user",
                    },
                    "supersedes": {
                        "type": "string",
                        "description": "If this revises a proposal the user has not "
                        "accepted yet, pass that proposal_id so the old card is set "
                        "aside in favor of this one.",
                    },
                    "media": {
                        "type": "array",
                        "description": "Media files to attach (TTS audio, a "
                        "map/diagram image, short video): each is staged now, "
                        "previewed on the review card, and imported into the "
                        "collection's media folder only when the user accepts. "
                        "Reference audio/video in a field as [sound:filename] "
                        "and images as <img src=\"filename\"> - unreferenced "
                        "attachments are flagged on the card. Audio "
                        "mp3/wav/ogg/opus/m4a/flac, images png/jpg/jpeg/gif/"
                        "webp/svg/avif, video mp4/webm; max 4 files.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "path": {
                                    "type": "string",
                                    "description": "Absolute local path to the media file",
                                },
                                "filename": {
                                    "type": "string",
                                    "description": "Name to import as (defaults to the "
                                    "file's basename); use this exact name in the "
                                    "[sound:...] marker",
                                },
                            },
                            "required": ["path"],
                        },
                    },
                },
                "required": ["fields"],
            },
            propose_note,
            writes=True,
        )
    )
    registry.register(
        ToolSpec(
            "propose_note_edit",
            "Propose changes to an existing note. The user reviews per-field "
            "diffs and a before/after card preview, then accepts or rejects. "
            "Only include fields that actually change.",
            {
                "type": "object",
                "properties": {
                    "note_id": {"type": "integer"},
                    "field_changes": {
                        "type": "object",
                        "description": "Field name -> full new value. NOTE: this "
                        "argument is `field_changes`, not `fields` (that is "
                        "propose_note's argument). Pass a real object, not a "
                        "JSON-encoded string.",
                        "additionalProperties": {"type": "string"},
                    },
                    "add_tags": {"type": "array", "items": {"type": "string"}},
                    "remove_tags": {"type": "array", "items": {"type": "string"}},
                    "rationale": {
                        "type": "string",
                        "description": "One sentence: why this edit improves the note",
                    },
                    "supersedes": {
                        "type": "string",
                        "description": "If this revises an earlier unaccepted proposal, "
                        "pass its proposal_id so the old card is set aside.",
                    },
                    "media": {
                        "type": "array",
                        "description": "Media files to attach to this edit (adding "
                        "TTS audio or a diagram to an existing note): staged now, "
                        "previewed on the review card, imported into the media "
                        "folder only when the user accepts. Put the reference in "
                        "the new field value - [sound:filename] for audio/video, "
                        "<img src=\"filename\"> for images - or it is flagged as "
                        "unreferenced. Audio mp3/wav/ogg/opus/m4a/flac, images "
                        "png/jpg/jpeg/gif/webp/svg/avif, video mp4/webm; max 4 "
                        "files.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "path": {
                                    "type": "string",
                                    "description": "Absolute local path to the media file",
                                },
                                "filename": {
                                    "type": "string",
                                    "description": "Name to import as (defaults to the "
                                    "file's basename); use this exact name in the "
                                    "reference marker",
                                },
                            },
                            "required": ["path"],
                        },
                    },
                },
                "required": ["note_id"],
            },
            propose_note_edit,
            writes=True,
            # Accepted as an alias for `field_changes` (proposals.py resolves
            # it), but deliberately unadvertised: the schema names one
            # argument so the model is not invited to pick between two.
            extra_args=frozenset({"fields"}),
        )
    )
    registry.register(
        ToolSpec(
            "rename_tag",
            "Rename a tag across the whole collection in one operation. Shows "
            "the user one confirmation with the affected note count.",
            {
                "type": "object",
                "properties": {
                    "old_tag": {"type": "string"},
                    "new_tag": {"type": "string"},
                    "rationale": {"type": "string"},
                },
                "required": ["old_tag", "new_tag"],
            },
            rename_tag,
            writes=True,
        )
    )
    registry.register(
        ToolSpec(
            "find_replace",
            "Find-and-replace across note fields (plain text, or regex with "
            "regex=true). Scope with an Anki search query and/or a field name. "
            "The user reviews one card with the affected count and sample "
            "diffs. Use for mechanical text fixes; for semantically judged "
            "per-note edits use a change set instead.",
            {
                "type": "object",
                "properties": {
                    "search": {"type": "string"},
                    "replacement": {"type": "string"},
                    "query": {
                        "type": "string",
                        "description": "Anki search to scope candidates (default: all)",
                    },
                    "field": {
                        "type": "string",
                        "description": "Restrict to one field name (default: all fields)",
                    },
                    "regex": {"type": "boolean", "default": False},
                    "rationale": {"type": "string"},
                },
                "required": ["search", "replacement"],
            },
            find_replace,
            writes=True,
        )
    )
    registry.register(
        ToolSpec(
            "move_cards",
            "Move all cards matching an Anki search into a deck. One "
            "confirmation with the affected count. CAUTION: moving cards "
            "discards their FSRS memory (stability/difficulty) - Anki wipes it "
            "on any deck move, and undo cannot restore it - so the proposal "
            "warns how many cards would lose memory. Prefer a filtered deck "
            "when you only need to study cards temporarily without rehoming them.",
            {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Anki search"},
                    "deck": {"type": "string", "description": "Target deck path"},
                    "rationale": {"type": "string"},
                },
                "required": ["query", "deck"],
            },
            move_cards,
            writes=True,
        )
    )
    registry.register(
        ToolSpec(
            "delete_notes",
            "Delete notes. ALWAYS requires explicit user confirmation (even in "
            "trusted-writes mode) and cannot be undone from the chat; a backup "
            "checkpoint is created first. Use sparingly.",
            {
                "type": "object",
                "properties": {
                    "note_ids": {"type": "array", "items": {"type": "integer"}},
                    "rationale": {"type": "string"},
                },
                "required": ["note_ids"],
            },
            delete_notes,
            writes=True,
            trusted_only=True,
        )
    )
    registry.register(
        ToolSpec(
            "set_due_date",
            "Set when cards come up next (Anki's Set Due Date). days='7' = "
            "due in a week; '0' = today; '3-10' = spread randomly across "
            "that range (the backlog-spreading idiom); '7!' also rewrites "
            "the interval. New cards become review cards. The proposal shows "
            "per-card before/after scheduling; revertible (exact restore, "
            "including new-card state and FSRS memory).",
            _scheduling_schema(
                {
                    "days": {
                        "type": "string",
                        "description": "'n', 'n-m', or with trailing '!' to "
                        "also set the interval, e.g. '7', '3-10', '14!'",
                    }
                },
                ["days"],
            ),
            _scheduling("set_due_date"),
            writes=True,
        )
    )
    registry.register(
        ToolSpec(
            "forget_cards",
            "Reset cards to new (Anki's Forget): clears interval, ease and "
            "FSRS memory state; the review log survives. The 'start this "
            "deck over' tool. Revertible (the captured scheduling state, "
            "including FSRS memory, is restored exactly).",
            _scheduling_schema(
                {
                    "restore_position": {
                        "type": "boolean",
                        "default": False,
                        "description": "Put cards back at their original "
                        "new-queue position instead of the end.",
                    },
                    "reset_counts": {
                        "type": "boolean",
                        "default": False,
                        "description": "Also zero the review/lapse counters.",
                    },
                },
                [],
            ),
            _scheduling("forget_cards"),
            writes=True,
        )
    )
    registry.register(
        ToolSpec(
            "reposition_new_cards",
            "Renumber NEW cards' queue positions (Anki's Reposition) - the "
            "'study the basics first' tool. Non-new cards in the selection "
            "are unaffected. shift_existing makes room by renumbering other "
            "new cards (warned; undo restores only the selected cards). "
            "Revertible.",
            _scheduling_schema(
                {
                    "starting_from": {"type": "integer", "default": 0},
                    "step_size": {"type": "integer", "default": 1},
                    "randomize": {"type": "boolean", "default": False},
                    "shift_existing": {"type": "boolean", "default": False},
                },
                [],
            ),
            _scheduling("reposition_new_cards"),
            writes=True,
        )
    )
    registry.register(
        ToolSpec(
            "add_tags",
            "Add tags to many notes at once (Anki Browse's Add Tags). Select "
            "with an Anki query or exact note_ids. One confirmation with the "
            "affected count; revertible (restores each note's exact prior "
            "tag list).",
            _bulk_tags_schema("add_tags"),
            _bulk_tags("add_tags"),
            writes=True,
        )
    )
    registry.register(
        ToolSpec(
            "remove_tags",
            "Remove tags from many notes at once (Anki Browse's Remove "
            "Tags). Removing a tag also removes its ::children. Deleting a "
            "tag outright = remove_tags(tags=['X'], query='tag:\"X\"'). One "
            "confirmation; revertible.",
            _bulk_tags_schema("remove_tags"),
            _bulk_tags("remove_tags"),
            writes=True,
        )
    )
    registry.register(
        ToolSpec(
            "clear_unused_tags",
            "Drop tag-list entries that no note carries (Anki's Clear Unused "
            "Tags). Touches the tag registry only, never notes. Not "
            "revertible from the chat, but harmless: a tag reappears the "
            "moment a note uses it.",
            {
                "type": "object",
                "properties": {"rationale": {"type": "string"}},
            },
            clear_unused_tags,
            writes=True,
        )
    )
    registry.register(
        ToolSpec(
            "suspend_cards",
            "Suspend cards: removed from study entirely until unsuspended, "
            "scheduling frozen. One confirmation with the affected count; "
            "revertible. Cards currently in a filtered deck return to their "
            "home deck (warned on the proposal).",
            _card_state_schema(),
            _card_state("suspend_cards"),
            writes=True,
        )
    )
    registry.register(
        ToolSpec(
            "unsuspend_cards",
            "Unsuspend cards so they return to normal scheduling. One "
            "confirmation; revertible.",
            _card_state_schema(),
            _card_state("unsuspend_cards"),
            writes=True,
        )
    )
    registry.register(
        ToolSpec(
            "bury_cards",
            "Bury cards until the next day rollover (Anki's manual bury). For "
            "'not this one right now' on the card being reviewed, prefer "
            "defer_card - the same manual bury, but tracked, so it shows in "
            "the user's buried-today tray and can be recalled as the next "
            "card. This tool is for bulk or off-queue burying. One "
            "confirmation; revertible.",
            _card_state_schema(),
            _card_state("bury_cards"),
            writes=True,
        )
    )
    registry.register(
        ToolSpec(
            "unbury_cards",
            "Unbury cards so they come back into today's queue. One "
            "confirmation; revertible.",
            _card_state_schema(),
            _card_state("unbury_cards"),
            writes=True,
        )
    )
    registry.register(
        ToolSpec(
            "set_card_flag",
            "Set or clear the colored flag on cards (visible in the Browser "
            "and reviewer; searchable as flag:N). One confirmation; "
            "revertible.",
            _card_state_schema(
                {
                    "flag": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": 7,
                        "description": "0 clears; 1 Red, 2 Orange, 3 Green, "
                        "4 Blue, 5 Pink, 6 Turquoise, 7 Purple",
                    }
                }
            ),
            _card_state("set_card_flag"),
            writes=True,
        )
    )
    registry.register(
        ToolSpec(
            "open_change_set",
            "Start a change set: a batch of per-note edits the user reviews as "
            "ONE unit (count + sampled diffs) instead of hundreds of separate "
            "proposals. Use for semantic sweeps over many notes. Returns a "
            "change_set_id; add edits with add_to_change_set, then "
            "close_change_set.",
            {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Short title, e.g. "
                              "'Fix pinyin tone marks'"},
                    "description": {"type": "string"},
                },
                "required": ["title"],
            },
            open_change_set,
            writes=True,
        )
    )
    registry.register(
        ToolSpec(
            "add_to_change_set",
            "Add ONE item to an open change set - either a note's edits "
            "(note_id + field_changes/tags, same shape as propose_note_edit) "
            "OR a generic operation (op + args): suspend/unsuspend/bury/"
            "unbury_cards, set_card_flag, add/remove_tags, set_due_date, "
            "forget_cards, reposition_new_cards, set_deck_limits, "
            "filtered_deck_action - args exactly as the standalone tool "
            "takes them. The whole set reviews as ONE card (items listed "
            "with risk class and per-item revertibility; the user can "
            "exclude single items), draws the budget per ITEM, and applies "
            "with explicit per-item outcomes.",
            {
                "type": "object",
                "properties": {
                    "change_set_id": {"type": "string"},
                    "note_id": {"type": "integer"},
                    "field_changes": {
                        "type": "object",
                        "additionalProperties": {"type": "string"},
                    },
                    "add_tags": {"type": "array", "items": {"type": "string"}},
                    "remove_tags": {"type": "array", "items": {"type": "string"}},
                    "op": {
                        "type": "string",
                        "description": "Batchable operation name (instead of "
                        "note_id); see the tool description for the list.",
                    },
                    "args": {
                        "type": "object",
                        "description": "The op's arguments, exactly as its "
                        "standalone tool takes them (minus rationale).",
                        "additionalProperties": True,
                    },
                },
                "required": ["change_set_id"],
            },
            add_to_change_set,
            writes=True,
        )
    )
    registry.register(
        ToolSpec(
            "close_change_set",
            "Close a change set and hand it to the user for review (or apply "
            "directly under trusted-writes). Call once all edits are added.",
            {
                "type": "object",
                "properties": {
                    "change_set_id": {"type": "string"},
                    "summary": {
                        "type": "string",
                        "description": "One-sentence summary of what the set does",
                    },
                },
                "required": ["change_set_id"],
            },
            close_change_set,
            writes=True,
        )
    )
