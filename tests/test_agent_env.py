"""Web-access CLI flags, compact tool advertising, and MCP widening.

These cover DESIGN.md section 5's config-file tier, shipped 2026-07-10.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from chat_with_your_cards.backends.claude_cli import (  # noqa: E402
    augmented_path,
    build_cli_args,
    context_window_for,
    write_mcp_config,
)
from chat_with_your_cards.mcp_server import tool_specs_for_mcp  # noqa: E402
from chat_with_your_cards.tools import build_registry  # noqa: E402


def _disallowed(argv):
    """--disallowedTools value, or "" when the flag is omitted (full tools
    with no disabled MCP servers no longer passes an empty-string arg)."""
    if "--disallowedTools" not in argv:
        return ""
    return argv[argv.index("--disallowedTools") + 1]

class WebAccessArgsTests(unittest.TestCase):
    def _args(self, **kwargs) -> list[str]:
        return build_cli_args(
            cli_path="claude", system_prompt="S", mcp_config_path="cfg", **kwargs
        )

    def test_web_on_by_default(self) -> None:
        args = self._args()
        allowed = args[args.index("--allowedTools") + 1]
        disallowed = _disallowed(args)
        self.assertIn("WebSearch", allowed)
        self.assertIn("WebFetch", allowed)
        self.assertIn("Skill", allowed)  # user/system skills must work
        self.assertNotIn("WebSearch", disallowed)
        self.assertIn("Bash", disallowed)

    def test_web_off(self) -> None:
        args = self._args(web_access=False)
        allowed = args[args.index("--allowedTools") + 1]
        disallowed = _disallowed(args)
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


class FastModeArgsTests(unittest.TestCase):
    """Headless fast mode (claude CLI >= 2.1.205) has no flag/env - it is
    enabled ONLY via --settings '{"fastMode": true}', so build_cli_args must
    emit that exact minimal blob when on, and nothing at all when off."""

    def _args(self, **kwargs) -> list[str]:
        return build_cli_args(
            cli_path="claude", system_prompt="S", mcp_config_path="cfg", **kwargs
        )

    def test_fast_mode_off_by_default(self) -> None:
        args = self._args()
        self.assertNotIn("--settings", args)

    def test_fast_mode_on_emits_settings_flag(self) -> None:
        args = self._args(fast_mode=True)
        self.assertIn("--settings", args)
        settings = args[args.index("--settings") + 1]
        self.assertEqual({"fastMode": True}, json.loads(settings))

    def test_fast_mode_off_explicit(self) -> None:
        args = self._args(fast_mode=False)
        self.assertNotIn("--settings", args)

    def test_fast_mode_combines_with_model_and_effort(self) -> None:
        args = self._args(model="opus", effort="high", fast_mode=True)
        self.assertEqual("opus", args[args.index("--model") + 1])
        self.assertEqual("high", args[args.index("--effort") + 1])
        self.assertIn("--settings", args)


class AgentToolsArgsTests(unittest.TestCase):
    """The agent-tools axis (DESIGN.md section 5), orthogonal to the collection
    permission mode: 'sandbox' (default) hard-blocks the CLI's own shell/file
    tools; 'full' leaves them on and adds --permission-mode bypassPermissions."""

    def _args(self, **kwargs) -> list[str]:
        return build_cli_args(
            cli_path="claude", system_prompt="S", mcp_config_path="cfg", **kwargs
        )

    def test_sandbox_is_default(self) -> None:
        args = self._args()
        disallowed = _disallowed(args)
        for tool in ("Bash", "Edit", "Write", "NotebookEdit"):
            self.assertIn(tool, disallowed)
        self.assertNotIn("--permission-mode", args)

    def test_sandbox_explicit(self) -> None:
        args = self._args(agent_tools="sandbox")
        disallowed = _disallowed(args)
        self.assertIn("Bash", disallowed)
        self.assertNotIn("--permission-mode", args)

    def test_full_drops_shell_disallows_and_adds_bypass(self) -> None:
        args = self._args(agent_tools="full")
        disallowed = _disallowed(args)
        for tool in ("Bash", "Edit", "Write", "NotebookEdit"):
            self.assertNotIn(tool, disallowed)
        self.assertIn("--permission-mode", args)
        self.assertEqual(
            "bypassPermissions", args[args.index("--permission-mode") + 1]
        )

    def test_full_still_honors_web_off_and_mcp_disabled(self) -> None:
        # The other axes are independent of agent_tools: web-off and
        # mcp_disabled still land in --disallowedTools even in full mode.
        args = self._args(
            agent_tools="full", web_access=False, mcp_disabled=["github"]
        )
        disallowed = _disallowed(args)
        self.assertIn("WebSearch", disallowed)
        self.assertIn("mcp__github", disallowed)
        self.assertNotIn("Bash", disallowed)  # shell stays ON in full mode

    def test_full_keeps_model_effort_fast(self) -> None:
        args = self._args(agent_tools="full", model="opus", effort="high", fast_mode=True)
        self.assertEqual("opus", args[args.index("--model") + 1])
        self.assertEqual("high", args[args.index("--effort") + 1])
        self.assertIn("--settings", args)
        self.assertIn("--permission-mode", args)

    def test_unknown_agent_tools_treated_as_sandbox(self) -> None:
        # build_cli_args only special-cases the exact string "full"; anything
        # else (including a stray value) stays in the safe sandbox posture.
        args = self._args(agent_tools="bananas")
        disallowed = _disallowed(args)
        self.assertIn("Bash", disallowed)
        self.assertNotIn("--permission-mode", args)


class ContextWindowForTests(unittest.TestCase):
    """context_window_for's table (DESIGN.md section 9): the stream never
    carries a context-WINDOW size, only per-turn usage counts, so this is
    hardcoded - mirrored in TypeScript by ui/src/contextWindow.ts."""

    def test_opus_alias_is_1m(self) -> None:
        self.assertEqual(1_000_000, context_window_for("opus"))

    def test_sonnet_alias_is_1m(self) -> None:
        self.assertEqual(1_000_000, context_window_for("sonnet"))

    def test_fable_alias_is_1m(self) -> None:
        self.assertEqual(1_000_000, context_window_for("fable"))

    def test_haiku_alias_is_200k(self) -> None:
        self.assertEqual(200_000, context_window_for("haiku"))

    def test_empty_default_alias_is_200k(self) -> None:
        self.assertEqual(200_000, context_window_for(""))

    def test_unknown_model_defaults_to_200k(self) -> None:
        self.assertEqual(200_000, context_window_for("some-future-model-nobody-heard-of"))

    def test_full_model_id_opus_is_1m(self) -> None:
        self.assertEqual(1_000_000, context_window_for("claude-opus-4-6-20260315"))

    def test_old_sonnet_full_id_is_200k(self) -> None:
        self.assertEqual(200_000, context_window_for("claude-sonnet-4-5-20250929"))

    def test_current_sonnet_full_id_is_1m(self) -> None:
        self.assertEqual(1_000_000, context_window_for("claude-sonnet-4-6-20260101"))

    def test_case_insensitive(self) -> None:
        self.assertEqual(1_000_000, context_window_for("OPUS"))


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
        disallowed = _disallowed(args)
        self.assertIn("mcp__github", disallowed)
        self.assertIn("mcp__filesystem", disallowed)

    def test_disabling_anki_is_ignored_and_logged(self) -> None:
        logged: list[str] = []
        args = self._args(mcp_disabled=["anki", "github"], log=logged.append)
        disallowed = _disallowed(args)
        self.assertNotIn("mcp__anki", disallowed)
        self.assertIn("mcp__github", disallowed)
        self.assertTrue(any("anki" in line for line in logged))

    def test_disabling_anki_without_a_logger_still_ignored(self) -> None:
        # log is optional; the guard itself must not depend on it.
        args = self._args(mcp_disabled=["anki"])
        disallowed = _disallowed(args)
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


class AugmentedPathTests(unittest.TestCase):
    """Anki's GUI PATH omits /opt/homebrew/bin etc., so the claude subprocess
    can't find tools its built-ins shell out to (poppler's pdftoppm for PDFs).
    augmented_path restores a login-shell PATH (dogfood 2026-07-15)."""

    def test_prepends_existing_tool_dirs_and_preserves_current(self) -> None:
        with tempfile.TemporaryDirectory() as tool, tempfile.TemporaryDirectory() as cur:
            with mock.patch(
                "chat_with_your_cards.backends.claude_cli._TOOL_DIRS",
                [tool, "/does/not/exist/xyz"],
            ):
                result = augmented_path(cur).split(":")
        self.assertEqual(result[0], tool, "tool dir should come first")
        self.assertIn(cur, result, "existing PATH entries must be kept")
        self.assertNotIn("/does/not/exist/xyz", result, "non-existent dirs dropped")

    def test_idempotent_and_dedups(self) -> None:
        with tempfile.TemporaryDirectory() as tool:
            with mock.patch(
                "chat_with_your_cards.backends.claude_cli._TOOL_DIRS", [tool]
            ):
                once = augmented_path(tool)
                twice = augmented_path(once)
        self.assertEqual(once, twice)
        self.assertEqual(once.split(":").count(tool), 1)


if __name__ == "__main__":
    unittest.main()
