"""Read and propose changes to Chat With Your Cards' own preferences."""

from __future__ import annotations

from typing import Any

from ..addon_settings import NEW_CHAT_KEYS, SETTING_SPECS
from .registry import ToolContext, ToolRegistry, ToolSpec


def get_addon_settings(ctx: ToolContext, _args: dict[str, Any]) -> dict[str, Any]:
    return {
        "settings": {name: ctx.config.get(name) for name in SETTING_SPECS},
        "editable": {
            name: (
                {"type": kind, "choices": list(rule)}
                if kind == "choice"
                else {"type": kind, "range": list(rule)}
                if kind == "int"
                else {"type": kind}
            )
            for name, (kind, rule) in SETTING_SPECS.items()
        },
        "new_chat_required": sorted(NEW_CHAT_KEYS),
        "note": "Use set_addon_settings for changes. It always asks the user to review and apply them. Advanced config, secrets, paths, and custom instructions stay in Anki's add-on Config editor.",
    }


def set_addon_settings(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    return ctx.proposals.submit_set_addon_settings(args)


def register_addon_setting_tools(registry: ToolRegistry) -> None:
    registry.register(
        ToolSpec(
            "get_addon_settings",
            "Read this add-on's chat-editable settings and valid values. Does not disclose custom instructions, secrets, executable paths, or MCP server definitions.",
            {"type": "object", "properties": {}},
            get_addon_settings,
        )
    )
    registry.register(
        ToolSpec(
            "set_addon_settings",
            "Propose changing Chat With Your Cards' own settings. Call get_addon_settings first. Every change requires the user's review, even in Trusted or Full collection mode. Advanced config and secrets are excluded.",
            {
                "type": "object",
                "properties": {
                    "settings": {"type": "object", "description": "Setting name -> new value, using names and allowed values from get_addon_settings"},
                    "rationale": {"type": "string"},
                },
                "required": ["settings"],
            },
            set_addon_settings,
            writes=True,
            available_read_only=True,
        )
    )
