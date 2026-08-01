"""Native scheduler write tools routed through CWYC's grading review flow."""

from __future__ import annotations

from typing import Any

from ..grading import MAX_CARDS_PER_OPERATION
from .registry import ToolContext, ToolRegistry, ToolSpec


def fail_cards_now(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    return ctx.grading.submit_fail(args)


def make_cards_available(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    return ctx.grading.submit_make_available(args)


def register_grading_tools(registry: ToolRegistry) -> None:
    card_ids = {
        "type": "array",
        "minItems": 1,
        "maxItems": MAX_CARDS_PER_OPERATION,
        "items": {"type": "integer"},
        "description": (
            "Exact Anki card IDs. Resolve note siblings first; do not pass every "
            "card from a broad search unless each one was intentionally judged wrong."
        ),
    }
    registry.register(
        ToolSpec(
            "fail_cards_now",
            "Record native reviews on exact existing cards, even if they are "
            "not due, suspended, buried, or already in a filtered deck. "
            "Defaults to Again; pass `rating` for hard/good/easy. Never "
            "changes scheduling rows directly. Existing suspension/burial is "
            "preserved and shown to the user, with a separate option to make those "
            "cards available. Depending on the user's permission mode, this either "
            "shows a dedicated confirmation chip or applies immediately under the "
            "session cap.",
            {
                "type": "object",
                "properties": {
                    "card_ids": card_ids,
                    "rating": {
                        "type": "string",
                        "enum": ["again", "hard", "good", "easy"],
                        "description": (
                            "Which button to record (default again). This writes "
                            "real review history, so it must reflect a judgement "
                            "the user actually made or asked for - never a guess "
                            "at how well they would have done."
                        ),
                    },
                    "rationale": {
                        "type": "string",
                        "description": "Why these exact cards should receive this rating.",
                    },
                },
                "required": ["card_ids"],
                "additionalProperties": False,
            },
            fail_cards_now,
            writes=True,
        )
    )
    registry.register(
        ToolSpec(
            "make_cards_available",
            "Remove suspension or burial from exact cards through Anki's native "
            "scheduler without removing or rewriting any recorded failure. Use "
            "only when the user asks to reveal cards that a grading result reported "
            "as still hidden.",
            {
                "type": "object",
                "properties": {
                    "card_ids": card_ids,
                    "rationale": {
                        "type": "string",
                        "description": "Why the user wants these cards available now.",
                    },
                },
                "required": ["card_ids"],
                "additionalProperties": False,
            },
            make_cards_available,
            writes=True,
        )
    )
