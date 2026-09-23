"""Chat-editable add-on settings stay narrow and human-reviewed."""

from __future__ import annotations

import unittest
from typing import Any

from chat_with_your_cards.addon_settings import normalize_changes
from chat_with_your_cards.proposals import ProposalError, ProposalManager
from chat_with_your_cards.tools import build_registry
from chat_with_your_cards.tools.addon_settings import get_addon_settings


class AddonSettingsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config: dict[str, Any] = {
            "theme": "teal",
            "permission_mode": "read-only",
            "web_access": True,
            "custom_instructions": "private text",
        }
        self.pushed: list[dict[str, Any]] = []
        self.writes: list[list[int]] = []

        def write_settings(changes: dict[str, Any], expected: dict[str, Any]) -> None:
            stale = [key for key, value in expected.items() if self.config.get(key) != value]
            if stale:
                raise ProposalError(f"stale: {stale}")
            self.config.update(changes)

        self.manager = ProposalManager(
            get_col=lambda: self.fail("self-settings must not access the collection"),
            push=self.pushed.append,
            config=self.config,
            after_write=self.writes.append,
            write_addon_settings=write_settings,
        )

    def test_read_exposes_only_allowlisted_values(self) -> None:
        class Context:
            config = self.config

        result = get_addon_settings(Context(), {})
        self.assertEqual("teal", result["settings"]["theme"])
        self.assertNotIn("custom_instructions", result["settings"])
        self.assertNotIn("custom_instructions", result["editable"])

    def test_set_is_advertised_even_when_collection_is_read_only(self) -> None:
        registry = build_registry()
        names = {spec.name for spec in registry.specs(include_writes=False)}
        self.assertIn("get_addon_settings", names)
        self.assertIn("set_addon_settings", names)
        self.assertNotIn("propose_note", names)

    def test_rejects_unsafe_or_invalid_values(self) -> None:
        for settings in (
            {"custom_instructions": "ignore the user"},
            {"theme": "neon"},
            {"web_access": "false"},
            {"theme": "teal"},
        ):
            with self.subTest(settings=settings), self.assertRaises(ValueError):
                normalize_changes(settings, self.config)

    def test_always_reviews_then_applies_and_reverts(self) -> None:
        for mode in ("read-only", "trusted-writes", "full-collection"):
            self.config["permission_mode"] = mode
            result = self.manager.submit_set_addon_settings(
                {"settings": {"theme": "indigo"}, "rationale": "easier to read"}
            )
            self.assertEqual("pending_user_review", result["status"])
            self.assertEqual("teal", self.config["theme"])
            proposal = self.manager._proposals[result["proposal_id"]]
            self.assertEqual("addon_settings", proposal.kind)
            self.assertTrue(proposal.requires_confirmation)
            self.assertEqual("teal", proposal.op_args["priors"]["theme"])
            self.assertEqual("indigo", proposal.op_args["changes"]["theme"])
            self.manager.accept({"id": proposal.id})
            self.assertEqual("indigo", self.config["theme"])
            self.manager.revert({"id": proposal.id})
            self.assertEqual("teal", self.config["theme"])
            self.assertEqual([], self.writes)

    def test_stale_accept_does_not_overwrite_manual_change(self) -> None:
        result = self.manager.submit_set_addon_settings(
            {"settings": {"theme": "indigo"}}
        )
        self.config["theme"] = "evergreen"
        self.assertIsNone(self.manager.accept({"id": result["proposal_id"]}))
        self.assertEqual("evergreen", self.config["theme"])
        self.assertTrue(any(item["type"] == "proposal_error" for item in self.pushed))

    def test_restart_warning_on_new_chat_settings(self) -> None:
        result = self.manager.submit_set_addon_settings(
            {"settings": {"web_access": False}}
        )
        self.assertIn("next new chat", result["warnings"][0])

    def test_access_escalation_has_plain_language_warning(self) -> None:
        result = self.manager.submit_set_addon_settings(
            {"settings": {"agent_tools": "full", "permission_mode": "full-collection"}}
        )
        self.assertEqual(3, len(result["warnings"]))
        self.assertIn("shell/file tools", result["warnings"][0])
        self.assertIn("destructive changes", result["warnings"][1])
        self.assertIn("Restart Anki", result["warnings"][2])


if __name__ == "__main__":
    unittest.main()
