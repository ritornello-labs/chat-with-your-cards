"""Settings the assistant may read and propose changing through chat.

This is deliberately an allowlist, not an arbitrary add-on config editor.
Values are validated before a review card is created and again at apply time.
"""

from __future__ import annotations

from typing import Any


# (kind, allowed values or inclusive integer bounds). Keep secrets, arbitrary
# prompt text, executable paths, and MCP server definitions in Anki's config
# editor; a card can contain untrusted instructions aimed at the assistant.
SETTING_SPECS: dict[str, tuple[str, Any]] = {
    "theme": ("choice", ("teal", "indigo", "evergreen")),
    "dock_side": ("choice", ("left", "right")),
    "vim_mode": ("bool", None),
    "restore_last_chat": ("bool", None),
    "suggested_questions": ("bool", None),
    "defer_button": ("bool", None),
    "defer_on_send": ("bool", None),
    "widget_rendering": ("bool", None),
    "open_in_claude_target": ("choice", ("terminal", "desktop")),
    "model": ("model", None),
    "effort": ("choice", ("", "low", "medium", "high", "xhigh", "max")),
    "fast_mode": ("bool", None),
    "agent_tools": ("choice", ("sandbox", "acceptEdits", "auto", "full")),
    "permission_mode": (
        "choice",
        ("default", "ask-each-read", "read-only", "auto-accept", "trusted-writes", "full-collection"),
    ),
    "web_access": ("bool", None),
    "mcp_inherit_user": ("bool", None),
    "context_token_budget": ("int", (1_000, 100_000)),
    "auto_accept_cap": ("int", (0, 10_000)),
    "write_budget": ("int", (0, 100_000)),
    "learning_nudge_threshold": ("int", (1, 10_000)),
    "learning_nudge_days": ("int", (1, 3_650)),
    "learning_run_mode": ("choice", ("chat", "background")),
    "skill_update_policy": ("choice", ("review", "automatic")),
}

NEW_CHAT_KEYS = {"web_access", "mcp_inherit_user"}


def normalize_changes(raw: Any, current: dict[str, Any]) -> dict[str, Any]:
    """Return strict, effective changes or raise a user-readable ValueError."""
    if not isinstance(raw, dict) or not raw:
        raise ValueError("settings must be a non-empty object of setting names to values")
    unknown = sorted(set(raw) - SETTING_SPECS.keys())
    if unknown:
        raise ValueError(
            f"unsupported setting(s): {unknown}; editable: {sorted(SETTING_SPECS)}"
        )
    changes: dict[str, Any] = {}
    for name, value in raw.items():
        kind, constraint = SETTING_SPECS[name]
        if kind == "bool":
            if not isinstance(value, bool):
                raise ValueError(f"{name} must be true or false")
        elif kind == "int":
            low, high = constraint
            if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
                raise ValueError(f"{name} must be a whole number from {low} to {high}")
        elif kind == "choice":
            if not isinstance(value, str) or value not in constraint:
                raise ValueError(f"{name} must be one of {list(constraint)}")
        elif kind == "model":
            if (
                not isinstance(value, str)
                or len(value) > 120
                or any(ch.isspace() for ch in value)
            ):
                raise ValueError("model must be an alias or model ID without spaces")
        if current.get(name) != value:
            changes[name] = value
    if not changes:
        raise ValueError("no effective change: those settings already have these values")
    effective = {**current, **changes}
    if effective.get("agent_tools") == "auto" and "haiku" in str(effective.get("model", "")).lower():
        raise ValueError("Computer tools Auto is unavailable with Haiku; choose Sandbox or another model")
    return changes
