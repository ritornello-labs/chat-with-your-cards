"""Note-type write path (#7) — the most dangerous tool family in the add-on.

We have returned template source and CSS verbatim since 2026-07-23 so the agent
could *debug* rendering; until now it could diagnose a broken template and then
not propose the fix. These tools close that asymmetry.

Everything here is collection-wide by construction: a note type is shared by
every deck that uses it. Nothing writes directly — every call submits to the
ProposalManager, which puts the blast radius (notes, decks, cards created or
destroyed, whether a full sync follows) on the review card before the user
commits. `remove_empty_cards` ships alongside deliberately: the moment
templates are writable, Anki offers no other way to delete a card whose front
renders blank.
"""

from __future__ import annotations

from typing import Any

from .registry import ToolContext, ToolRegistry, ToolSpec


def set_note_type_styling(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    return ctx.proposals.submit_set_note_type_styling(args)


def set_card_template(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    return ctx.proposals.submit_set_card_template(args)


def manage_note_type_fields(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    return ctx.proposals.submit_manage_note_type_fields(args)


def manage_card_templates(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    return ctx.proposals.submit_manage_card_templates(args)


def create_note_type(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    return ctx.proposals.submit_create_note_type(args)


def change_note_type(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    return ctx.proposals.submit_change_note_type(args)


def remove_empty_cards(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    return ctx.proposals.submit_remove_empty_cards(args)


_RATIONALE = {
    "type": "string",
    "description": "One sentence: why this change is worth its blast radius",
}


def register_note_type_tools(registry: ToolRegistry) -> None:
    registry.register(
        ToolSpec(
            "set_note_type_styling",
            "Replace a note type's CSS (the styling shared by all its card "
            "templates). Read the current CSS with get_note_type first and "
            "pass the FULL new stylesheet - this replaces, it does not merge.",
            {
                "type": "object",
                "properties": {
                    "note_type": {"type": "string"},
                    "css": {
                        "type": "string",
                        "description": "The complete new stylesheet",
                    },
                    "rationale": _RATIONALE,
                },
                "required": ["note_type", "css"],
            },
            set_note_type_styling,
            writes=True,
        )
    )
    registry.register(
        ToolSpec(
            "set_card_template",
            "Rewrite one card template's front (qfmt) and/or back (afmt). Read "
            "the current source with get_note_type first and pass the FULL new "
            "source for whichever side you change - this replaces, it does not "
            "merge. Does not force a full sync.",
            {
                "type": "object",
                "properties": {
                    "note_type": {"type": "string"},
                    "template": {
                        "type": "string",
                        "description": "The card template's name, e.g. 'Card 1'",
                    },
                    "qfmt": {
                        "type": "string",
                        "description": "Complete new front source; omit to keep it",
                    },
                    "afmt": {
                        "type": "string",
                        "description": "Complete new back source; omit to keep it",
                    },
                    "rationale": _RATIONALE,
                },
                "required": ["note_type", "template"],
            },
            set_card_template,
            writes=True,
        )
    )
    registry.register(
        ToolSpec(
            "manage_note_type_fields",
            "Add, rename, reposition, or remove a field on a note type. "
            "`rename` is safe: Anki rewrites template references and note "
            "content follows. `add`/`reposition` force a full sync. `remove` "
            "destroys that field's content on every note collection-wide, "
            "cannot be undone from the chat, and makes Anki silently rewrite "
            "any template that referenced the field - propose it only when the "
            "user has asked for exactly that.",
            {
                "type": "object",
                "properties": {
                    "note_type": {"type": "string"},
                    "op": {
                        "type": "string",
                        "enum": ["add", "rename", "reposition", "remove"],
                    },
                    "field": {
                        "type": "string",
                        "description": "The field to act on (its new name for `add`)",
                    },
                    "new_name": {"type": "string", "description": "For `rename`"},
                    "position": {
                        "type": "integer",
                        "description": "For `reposition`: the 0-based target index",
                    },
                    "rationale": _RATIONALE,
                },
                "required": ["note_type", "op", "field"],
            },
            manage_note_type_fields,
            writes=True,
        )
    )
    registry.register(
        ToolSpec(
            "manage_card_templates",
            "Add, rename, reposition, or remove a card template on a note "
            "type. `add` generates a new card on every note that fills its "
            "front; `remove` deletes those cards and their entire review "
            "history and cannot be undone from the chat. Both force a full "
            "sync. To change an existing template's source use "
            "set_card_template instead.",
            {
                "type": "object",
                "properties": {
                    "note_type": {"type": "string"},
                    "op": {
                        "type": "string",
                        "enum": ["add", "rename", "reposition", "remove"],
                    },
                    "template": {
                        "type": "string",
                        "description": "The template to act on (its new name for `add`)",
                    },
                    "qfmt": {"type": "string", "description": "Front source, for `add`"},
                    "afmt": {"type": "string", "description": "Back source, for `add`"},
                    "new_name": {"type": "string", "description": "For `rename`"},
                    "position": {
                        "type": "integer",
                        "description": "For `reposition`: the 0-based target index",
                    },
                    "rationale": _RATIONALE,
                },
                "required": ["note_type", "op", "template"],
            },
            manage_card_templates,
            writes=True,
        )
    )
    registry.register(
        ToolSpec(
            "create_note_type",
            "Create a new note type as a copy of an existing one (fields, "
            "templates, and CSS included). The copy starts empty, so this "
            "touches no existing note - it is the safe way to try a new card "
            "design, and the correct first step before change_note_type.",
            {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Name for the new note type"},
                    "clone_from": {
                        "type": "string",
                        "description": "Existing note type to copy",
                    },
                    "rationale": _RATIONALE,
                },
                "required": ["name", "clone_from"],
            },
            create_note_type,
            writes=True,
        )
    )
    registry.register(
        ToolSpec(
            "change_note_type",
            "Convert notes from one note type to another, mapping fields and "
            "card templates BY NAME. Anything left unmapped is dropped: field "
            "content is erased and cards (with their review history) are "
            "deleted. Not undoable from the chat - a backup is taken first. "
            "With neither note_ids nor query, every note of the source type is "
            "converted.",
            {
                "type": "object",
                "properties": {
                    "note_type": {"type": "string", "description": "Source note type"},
                    "new_note_type": {"type": "string", "description": "Target note type"},
                    "note_ids": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "Specific notes; omit with `query` for all of them",
                    },
                    "query": {
                        "type": "string",
                        "description": "Anki search selecting the notes to convert",
                    },
                    "field_map": {
                        "type": "object",
                        "description": "Old field name -> new field name. Omitted "
                        "entirely means match by name. An old field you leave out "
                        "(or map to null) has its content DESTROYED.",
                        "additionalProperties": {"type": "string"},
                    },
                    "template_map": {
                        "type": "object",
                        "description": "Old template name -> new template name. Same "
                        "default and same consequence: an unmapped template's cards "
                        "are deleted with their review history.",
                        "additionalProperties": {"type": "string"},
                    },
                    "rationale": _RATIONALE,
                },
                "required": ["note_type", "new_note_type"],
            },
            change_note_type,
            writes=True,
        )
    )
    registry.register(
        ToolSpec(
            "remove_empty_cards",
            "Find and delete cards whose front renders blank - Anki's Empty "
            "Cards. The necessary companion to template edits: a conditional "
            "front that stops matching leaves a card that nothing else can "
            "remove. Notes left with no cards at all are deleted outright. Not "
            "undoable from the chat - a backup is taken first.",
            {
                "type": "object",
                "properties": {"rationale": _RATIONALE},
            },
            remove_empty_cards,
            writes=True,
        )
    )
