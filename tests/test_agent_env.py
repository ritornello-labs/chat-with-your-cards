"""BYOK key resolution, web-access CLI flags, compact tool advertising, and
MCP widening (DESIGN.md section 5's config-file tier, shipped 2026-07-10)."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from chat_with_your_cards import keys  # noqa: E402
from chat_with_your_cards.backends.claude_cli import (  # noqa: E402
    build_cli_args,
    write_mcp_config,
)
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


class McpWideningArgsTests(unittest.TestCase):
    """build_cli_args's half of DESIGN.md section 5's config-file MCP-widening
    tier: --strict-mcp-config is the default, dropped only for
    mcp_inherit_user; mcp_disabled names become mcp__<name> disallowedTools
    entries; 'anki' can never be disabled that way."""

    def _args(self, **kwargs) -> list[str]:
        return build_cli_args(
            cli_path="claude", system_prompt="S", mcp_config_path="cfg", **kwargs
        )

    def test_strict_mcp_config_on_by_default(self) -> None:
        self.assertIn("--strict-mcp-config", self._args())

    def test_strict_mcp_config_dropped_when_inherit_user(self) -> None:
        args = self._args(mcp_inherit_user=True)
        self.assertNotIn("--strict-mcp-config", args)

    def test_disabled_servers_become_disallowed_tools(self) -> None:
        args = self._args(mcp_disabled=["github", "filesystem"])
        disallowed = args[args.index("--disallowedTools") + 1]
        self.assertIn("mcp__github", disallowed)
        self.assertIn("mcp__filesystem", disallowed)

    def test_disabling_anki_is_ignored_and_logged(self) -> None:
        logged: list[str] = []
        args = self._args(mcp_disabled=["anki", "github"], log=logged.append)
        disallowed = args[args.index("--disallowedTools") + 1]
        self.assertNotIn("mcp__anki", disallowed)
        self.assertIn("mcp__github", disallowed)
        self.assertTrue(any("anki" in line for line in logged))

    def test_disabling_anki_without_a_logger_still_ignored(self) -> None:
        # log is optional; the guard itself must not depend on it.
        args = self._args(mcp_disabled=["anki"])
        disallowed = args[args.index("--disallowedTools") + 1]
        self.assertNotIn("mcp__anki", disallowed)

    def test_allowedTools_still_advertises_the_builtin_server(self) -> None:
        # mcp__anki stays in --allowedTools regardless of widening flags -
        # widening only ever ADDS reachable servers, never narrows ours.
        args = self._args(mcp_inherit_user=True, mcp_disabled=["github"])
        allowed = args[args.index("--allowedTools") + 1]
        self.assertIn("mcp__anki", allowed)


class McpConfigMergeTests(unittest.TestCase):
    """write_mcp_config's half of the same tier: extra servers merge
    verbatim into the CLI's --mcp-config JSON, and a user-supplied 'anki'
    entry can never shadow the built-in one."""

    def _write(self, **kwargs) -> dict:
        with tempfile.TemporaryDirectory() as tmp:
            path = write_mcp_config(Path(tmp), "http://x", "tok", **kwargs)
            return json.loads(path.read_text(encoding="utf-8"))

    def test_no_extras_just_the_builtin_server(self) -> None:
        config = self._write()
        self.assertEqual(["anki"], list(config["mcpServers"]))
        self.assertEqual("http://x", config["mcpServers"]["anki"]["url"])

    def test_extra_servers_merged_verbatim(self) -> None:
        extra = {
            "github": {"command": "npx", "args": ["-y", "@modelcontextprotocol/github"]}
        }
        config = self._write(extra_servers=extra)
        self.assertEqual(extra["github"], config["mcpServers"]["github"])
        self.assertIn("anki", config["mcpServers"])

    def test_user_supplied_anki_server_is_dropped_and_logged(self) -> None:
        logged: list[str] = []
        extra = {"anki": {"command": "evil"}, "github": {"command": "npx"}}
        config = self._write(extra_servers=extra, log=logged.append)
        # The built-in wins, never the user-supplied spec.
        self.assertEqual("http://x", config["mcpServers"]["anki"]["url"])
        self.assertNotIn("command", config["mcpServers"]["anki"])
        self.assertIn("github", config["mcpServers"])
        self.assertTrue(any("anki" in line for line in logged))

    def test_user_supplied_anki_dropped_without_a_logger(self) -> None:
        config = self._write(extra_servers={"anki": {"command": "evil"}})
        self.assertEqual("http://x", config["mcpServers"]["anki"]["url"])


if __name__ == "__main__":
    unittest.main()
