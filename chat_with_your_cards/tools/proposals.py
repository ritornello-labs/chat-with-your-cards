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
                },
                "required": ["note_id"],
            },
            propose_note_edit,
            writes=True,
        )
    )
