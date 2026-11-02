"""Profile settings are separate from collection and assistant preferences."""

from __future__ import annotations

from typing import Any

from ..anki_settings import SPECS
from .registry import ToolContext, ToolRegistry, ToolSpec


def get_anki_settings(ctx: ToolContext, _args: dict[str, Any]) -> dict[str, Any]:
    return ctx.proposals.get_anki_settings()


def set_anki_settings(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    return ctx.proposals.submit_set_anki_settings(args)


def register_anki_setting_tools(registry: ToolRegistry) -> None:
    registry.register(
        ToolSpec(
            "get_anki_settings",
            "Read current-profile Anki sync settings and editable values: custom sync URL, automatic sync, media sync, background media interval and network timeout. No account credentials. Collection review/edit preferences use get_preferences; CWYC's own settings use get_addon_settings.",
            {"type": "object", "properties": {}},
            get_anki_settings,
        )
    )
    registry.register(
        ToolSpec(
            "set_anki_settings",
            "Propose changing current-profile Anki sync settings. Read get_anki_settings first. Always requires user review, including in Full collection mode. Changing sync destination requires signing out in Anki Preferences first. Applies settings only; never starts sync or selects full-upload/download direction.",
            {
                "type": "object",
                "properties": {
                    "settings": {
                        "type": "object",
                        "properties": SPECS,
                        "additionalProperties": False,
                        "description": "One or more setting names and values from get_anki_settings.",
                    },
                    "rationale": {"type": "string"},
                },
                "required": ["settings"],
            },
            set_anki_settings,
            writes=True,
            available_read_only=True,
        )
    )
