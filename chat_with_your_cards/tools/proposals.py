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
                        "description": "Audio files to attach (e.g. TTS you "
                        "generated): each is staged now, playable on the review "
                        "card, and imported into the collection's media folder "
                        "only when the user accepts. Reference each file in a "
                        "field as [sound:filename] - unreferenced attachments "
                        "are flagged on the card. mp3/wav/ogg/opus/m4a/flac, "
                        "max 4 files.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "path": {
                                    "type": "string",
                                    "description": "Absolute local path to the audio file",
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
                        "description": "Field name -> full new value",
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
                },
                "required": ["note_id"],
            },
            propose_note_edit,
            writes=True,
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
            "Add one note's edits to an open change set. Same shape as "
            "propose_note_edit but batched for review as a unit.",
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
                },
                "required": ["change_set_id", "note_id"],
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
