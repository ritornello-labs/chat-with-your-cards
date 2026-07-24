from __future__ import annotations

import unittest
from types import SimpleNamespace
from typing import Any

from chat_with_your_cards.tools import build_registry


class _FakeGrading:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def submit_fail(self, args: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(("fail", args))
        return {"status": "pending_user_confirmation"}

    def submit_make_available(self, args: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(("available", args))
        return {"status": "pending_user_confirmation"}


class GradingToolTests(unittest.TestCase):
    def test_tools_are_registered_as_writes_with_bounded_exact_ids(self) -> None:
        specs = {spec.name: spec for spec in build_registry().specs()}

        for name in ("fail_cards_now", "make_cards_available"):
            with self.subTest(name=name):
                spec = specs[name]
                self.assertTrue(spec.writes)
                ids = spec.input_schema["properties"]["card_ids"]
                self.assertEqual(ids["minItems"], 1)
                self.assertEqual(ids["maxItems"], 50)
                self.assertFalse(spec.input_schema["additionalProperties"])

    def test_tools_delegate_to_grading_manager(self) -> None:
        grading = _FakeGrading()
        ctx = SimpleNamespace(grading=grading)
        registry = build_registry()

        first = registry.call(ctx, "fail_cards_now", {"card_ids": [1]})
        second = registry.call(ctx, "make_cards_available", {"card_ids": [1]})

        self.assertEqual(first["status"], "pending_user_confirmation")
        self.assertEqual(second["status"], "pending_user_confirmation")
        self.assertEqual(
            grading.calls,
            [
                ("fail", {"card_ids": [1]}),
                ("available", {"card_ids": [1]}),
            ],
        )


if __name__ == "__main__":
    unittest.main()
