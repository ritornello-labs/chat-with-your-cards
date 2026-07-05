"""BYOK key resolution, web-access CLI flags, compact tool advertising."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from chat_with_your_cards import keys  # noqa: E402
from chat_with_your_cards.backends.claude_cli import build_cli_args  # noqa: E402
from chat_with_your_cards.mcp_server import tool_specs_for_mcp  # noqa: E402
from chat_with_your_cards.tools import build_registry  # noqa: E402


class ResolveAgentEnvTests(unittest.TestCase):
    def test_plain_key_used(self) -> None:
        env, problems = keys.resolve_agent_env({"anthropic_api_key": "sk-x"})
        self.assertEqual({"ANTHROPIC_API_KEY": "sk-x"}, env)
        self.assertEqual([], problems)

    def test_no_keys_no_env(self) -> None:
        env, problems = keys.resolve_agent_env({})
        self.assertEqual({}, env)
        self.assertEqual([], problems)

    def test_op_reference_wins_over_plain(self) -> None:
        fake = mock.Mock(returncode=0, stdout="sk-from-op\n", stderr="")
        with mock.patch.object(keys.subprocess, "run", return_value=fake), \
             mock.patch.object(keys.shutil, "which", return_value="/usr/bin/op"):
            env, problems = keys.resolve_agent_env(
                {
                    "anthropic_api_key": "sk-plain",
                    "anthropic_api_key_op": "op://Vault/Anthropic/key",
                }
            )
        self.assertEqual({"ANTHROPIC_API_KEY": "sk-from-op"}, env)
        self.assertEqual([], problems)

    def test_op_failure_falls_back_to_plain_and_reports(self) -> None:
        fake = mock.Mock(returncode=1, stdout="", stderr="not signed in")
        with mock.patch.object(keys.subprocess, "run", return_value=fake), \
             mock.patch.object(keys.shutil, "which", return_value="/usr/bin/op"):
            env, problems = keys.resolve_agent_env(
                {
                    "anthropic_api_key": "sk-plain",
                    "anthropic_api_key_op": "op://Vault/Anthropic/key",
                }
            )
        self.assertEqual({"ANTHROPIC_API_KEY": "sk-plain"}, env)
        self.assertTrue(problems and "op read failed" in problems[0])

    def test_missing_op_cli_reported(self) -> None:
        with mock.patch.object(keys.shutil, "which", return_value=None):
            env, problems = keys.resolve_agent_env(
                {"anthropic_api_key_op": "op://Vault/Anthropic/key"}
            )
        self.assertEqual({}, env)
        self.assertTrue(problems and "op) not found" in problems[0])

    def test_openai_key_supported(self) -> None:
        env, _ = keys.resolve_agent_env({"openai_api_key": "sk-oai"})
        self.assertEqual({"OPENAI_API_KEY": "sk-oai"}, env)


class WebAccessArgsTests(unittest.TestCase):
    def _args(self, **kwargs) -> list[str]:
        return build_cli_args(
            cli_path="claude", system_prompt="S", mcp_config_path="cfg", **kwargs
        )

    def test_web_on_by_default(self) -> None:
        args = self._args()
        allowed = args[args.index("--allowedTools") + 1]
        disallowed = args[args.index("--disallowedTools") + 1]
        self.assertIn("WebSearch", allowed)
        self.assertIn("WebFetch", allowed)
        self.assertIn("Skill", allowed)  # user/system skills must work
        self.assertNotIn("WebSearch", disallowed)
        self.assertIn("Bash", disallowed)

    def test_web_off(self) -> None:
        args = self._args(web_access=False)
        allowed = args[args.index("--allowedTools") + 1]
        disallowed = args[args.index("--disallowedTools") + 1]
        self.assertNotIn("WebSearch", allowed)
        self.assertIn("WebSearch", disallowed)
        self.assertIn("Skill", allowed)


class ToolAdvertisingTests(unittest.TestCase):
    def test_full_descriptions_and_schemas_served(self) -> None:
        # Compact advertising was dropped 2026-07-05: the ~380-token saving
        # is outweighed by the extra tool_help round-trip and cache churn.
        registry = build_registry()
        specs = tool_specs_for_mcp(registry.specs())
        names = {t["name"] for t in specs}
        self.assertNotIn("tool_help", names)
        by_name = {s.name: s for s in registry.specs()}
        for t in specs:
            self.assertEqual(by_name[t["name"]].description, t["description"])
            self.assertIn("inputSchema", t)


if __name__ == "__main__":
    unittest.main()
