"""Sandboxed inline widgets: let the agent render bespoke HTML in the chat.

Security model (DESIGN.md "Inline widgets"): the HTML is agent output, and
the agent reads untrusted card content, so the payload is treated as HOSTILE
regardless of any user opt-in. The UI renders it inside
`<iframe sandbox="allow-scripts">` WITHOUT allow-same-origin (opaque origin:
no parent DOM, no pycmd bridge, no cookies) and prepends a CSP that blocks
all network. The opt-in config gate (`widget_rendering`) is a consent switch
for surface area, NOT the security boundary - the sandbox must hold even
when the gate is open.

State channel: the tool is always registered; the tool RESULT tells the
agent whether rendering is enabled. When disabled, the call pushes an
enable-offer chip into the chat (the user can flip the setting right there)
and returns `disabled_pending_user` - so "agent asks for permission" and
"agent discovers current state" are the same deterministic mechanism, and a
mid-conversation toggle is simply reflected in the next call's result.
"""

from __future__ import annotations

from typing import Any

from .registry import ToolContext, ToolRegistry, ToolSpec

# The HTML rides the bridge and the transcript; keep it bounded. Generous
# enough for a self-contained dashboard with inline data, far below anything
# that would stall the webview.
WIDGET_MAX_CHARS = 400_000


def render_widget(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    html = str(args.get("html", ""))
    title = str(args.get("title", "")).strip() or "Widget"
    if not html.strip():
        raise ValueError("render_widget needs non-empty `html`")
    if len(html) > WIDGET_MAX_CHARS:
        raise ValueError(
            f"widget HTML is {len(html)} chars; the cap is {WIDGET_MAX_CHARS}. "
            "Trim embedded data or split the view."
        )
    config = getattr(ctx, "config", None) or {}
    if not bool(config.get("widget_rendering", False)):
        # Surface the consent decision to the user as a chip; tell the agent
        # deterministically via the result (no schema push exists mid-session).
        ctx.push_ui({"type": "widget_offer", "title": title})
        return {
            "status": "disabled_pending_user",
            "hint": "Widget rendering is off. The user has just been shown an "
            "enable control in the chat; if they enable it (they may reply "
            "'(widget rendering enabled)'), call render_widget again. "
            "Meanwhile, continue in markdown - do not stall waiting.",
        }
    ctx.push_ui({"type": "inline_widget", "html": html, "title": title})
    return {"status": "displayed", "title": title, "chars": len(html)}


def register_widget_tools(registry: ToolRegistry) -> None:
    registry.register(
        ToolSpec(
            "render_widget",
            "Render a self-contained interactive HTML widget inline in the "
            "chat (charts, diagrams, mini-dashboards, small interactive "
            "visualizations). It runs in a hard sandbox: NO network access "
            "(no CDNs, no fetch - inline all script/style, embed images as "
            "data: URIs), no access to the page or the user's data, display "
            "only. Write plain inline JS/SVG/CSS; keep it usable in a narrow "
            "(~400px) column and readable in light and dark. May be disabled: "
            "the result tells you, and the user is offered an enable control "
            "- do not ask them to hunt through settings.",
            {
                "type": "object",
                "properties": {
                    "html": {
                        "type": "string",
                        "description": "Body HTML (no <html>/<head>; inline "
                        "<style>/<script> allowed). Fully self-contained.",
                    },
                    "title": {
                        "type": "string",
                        "description": "Short title shown above the widget.",
                    },
                },
                "required": ["html"],
            },
            render_widget,
        )
    )
