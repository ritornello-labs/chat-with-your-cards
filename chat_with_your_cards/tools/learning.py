"""Learning tools: edit observations + skill-update proposals (DESIGN.md
section 15). The reflection chat reads the accumulated observations with
get_edit_observations and proposes a revision of the card-authoring skill
with propose_skill_update; the user confirms every skill change on a
proposal card, in every permission mode."""

from __future__ import annotations

from typing import Any

from .registry import ToolContext, ToolRegistry, ToolSpec


def get_edit_observations(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    store = ctx.learning
    if store is None:
        raise RuntimeError("the learning store is not available")
    pending = store.pending()
    return {
        "pending_count": len(pending),
        "observations": pending,
        "skill_path": str(store.skill_path),
        "skill_markdown": store.read_skill(),
        "note": (
            "Observation kinds: 'reviewed' = the user edited the proposal "
            "card before accepting it; 'edited_later' = an AI-written note "
            "changed afterwards (Anki editor, Browser, or another device "
            "after sync); 'deleted_later' = an AI-written note was deleted. "
            "Look for patterns ACROSS observations, then call "
            "propose_skill_update."
        ),
    }


def propose_skill_update(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    store = ctx.learning
    if store is None:
        raise RuntimeError("the learning store is not available")
    return ctx.proposals.submit_skill_update(
        args,
        old_content=store.read_skill(),
        observation_ids=store.pending_ids(),
    )


def register_learning_tools(registry: ToolRegistry) -> None:
    registry.register(
        ToolSpec(
            "get_edit_observations",
            "How the user has been changing AI-written cards since the last "
            "skill review: proposal-card edits before accepting, later edits "
            "to accepted notes, and deletions. Also returns the current "
            "card-authoring skill. Use when asked to review edit patterns or "
            "update the skill.",
            {"type": "object", "properties": {}},
            get_edit_observations,
        )
    )
    registry.register(
        ToolSpec(
            "propose_skill_update",
            "Propose a revision of the card-authoring skill based on observed "
            "edit patterns. Pass the FULL revised skill markdown; the user "
            "sees your pattern summary plus a diff and confirms. ALWAYS "
            "user-confirmed, in every permission mode. Accepting consumes the "
            "pending observations.",
            {
                "type": "object",
                "properties": {
                    "summary": {
                        "type": "string",
                        "description": "One or two plain sentences: what this "
                        "update teaches the skill and why",
                    },
                    "patterns": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "The recurring patterns you noticed, one "
                        "short plain-language statement each",
                    },
                    "new_content": {
                        "type": "string",
                        "description": "The complete revised SKILL.md content "
                        "(frontmatter included)",
                    },
                },
                "required": ["summary", "patterns", "new_content"],
            },
            propose_skill_update,
            writes=True,
        )
    )
