"""render_widget: the consent gate + state channel (tools/widgets.py).

The iframe sandbox itself is UI-side and is exercised by the GUI smoke
probe (_widget_sandbox_holds); these tests pin the Python contract:
- always registered, never silently dropped
- disabled -> enable-offer chip pushed + deterministic "disabled" result
- enabled -> inline_widget pushed verbatim
- size/emptiness guards
"""

from __future__ import annotations

import unittest
from typing import Any

from chat_with_your_cards.tools import build_registry
from chat_with_your_cards.tools.widgets import WIDGET_MAX_CHARS, render_widget


class _Ctx:
    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self.pushed: list[dict[str, Any]] = []

    def push_ui(self, payload: dict[str, Any]) -> None:
        self.pushed.append(payload)


class WidgetToolTests(unittest.TestCase):
    def test_registered_in_default_registry(self) -> None:
        names = {spec.name for spec in build_registry().specs(include_trusted=True)}
        self.assertIn("render_widget", names)

    def test_disabled_pushes_offer_and_reports_state(self) -> None:
        ctx = _Ctx({"widget_rendering": False})
        result = render_widget(ctx, {"html": "<b>hi</b>", "title": "T"})
        self.assertEqual(result["status"], "disabled_pending_user")
        self.assertEqual(len(ctx.pushed), 1)
        self.assertEqual(ctx.pushed[0]["type"], "widget_offer")
        self.assertEqual(ctx.pushed[0]["title"], "T")
        # The hint must tell the agent how it will learn about a flip.
        self.assertIn("enable", result["hint"].lower())

    def test_missing_config_key_means_disabled(self) -> None:
        ctx = _Ctx({})
        result = render_widget(ctx, {"html": "<b>hi</b>"})
        self.assertEqual(result["status"], "disabled_pending_user")

    def test_enabled_pushes_widget_verbatim(self) -> None:
        ctx = _Ctx({"widget_rendering": True})
        html = "<div id='x'></div><script>1</script>"
        result = render_widget(ctx, {"html": html, "title": "Chart"})
        self.assertEqual(result["status"], "displayed")
        self.assertEqual(len(ctx.pushed), 1)
        self.assertEqual(ctx.pushed[0]["type"], "inline_widget")
        self.assertEqual(ctx.pushed[0]["html"], html)
        self.assertEqual(ctx.pushed[0]["title"], "Chart")

    def test_default_title(self) -> None:
        ctx = _Ctx({"widget_rendering": True})
        render_widget(ctx, {"html": "<i>x</i>"})
        self.assertEqual(ctx.pushed[0]["title"], "Widget")

    def test_empty_html_rejected(self) -> None:
        ctx = _Ctx({"widget_rendering": True})
        with self.assertRaises(ValueError):
            render_widget(ctx, {"html": "   "})
        self.assertEqual(ctx.pushed, [])

    def test_oversized_html_rejected_before_any_push(self) -> None:
        ctx = _Ctx({"widget_rendering": True})
        with self.assertRaises(ValueError):
            render_widget(ctx, {"html": "x" * (WIDGET_MAX_CHARS + 1)})
        self.assertEqual(ctx.pushed, [])


if __name__ == "__main__":
    unittest.main()
