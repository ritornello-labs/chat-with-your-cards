"""MCP UI settings must preserve private values and reject stale/destructive edits."""

from __future__ import annotations

import copy
import json
import unittest
from typing import Any

from chat_with_your_cards.mcp_settings import editor, handle, mutate, snapshot


class McpSettingsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config: dict[str, Any] = {
            "mcp_servers": {
                "reference": {
                    "type": "http",
                    "url": "https://example.com/mcp",
                    "headers": {"Authorization": "synthetic-private-header"},
                    "timeout": 30000,
                    "oauth": {"clientId": "synthetic-client"},
                },
                "local": {
                    "command": "example-mcp",
                    "args": ["--folder", "path with spaces"],
                    "env": {"EXAMPLE_TOKEN": "synthetic-private-env"},
                },
            },
            "mcp_disabled": ["reference", "inherited"],
            "mcp_inherit_user": False,
            "unrelated": "preserve me",
        }

    def action(self, action: str, **kwargs: Any) -> dict[str, Any]:
        return mutate(
            self.config, {"action": action, "revision": snapshot(self.config)["revision"], **kwargs}
        )

    def test_snapshot_and_editor_do_not_send_saved_header_or_env_values(self) -> None:
        for data in (
            snapshot(self.config),
            editor(self.config, "reference"),
            editor(self.config, "local"),
        ):
            encoded = json.dumps(data)
            self.assertNotIn("synthetic-private-header", encoded)
            self.assertNotIn("synthetic-private-env", encoded)
        self.assertNotIn("url", json.dumps(snapshot(self.config)))
        self.assertEqual(["inherited"], snapshot(self.config)["blocked"])

    def test_edit_preserves_credentials_and_advanced_options(self) -> None:
        draft = editor(self.config, "reference")
        draft["url"] = "https://example.org/mcp"
        result = self.action("save", name="reference", existing=True, server=draft)
        spec = result["mcp_servers"]["reference"]
        self.assertEqual("synthetic-private-header", spec["headers"]["Authorization"])
        self.assertEqual(30000, spec["timeout"])
        self.assertEqual({"clientId": "synthetic-client"}, spec["oauth"])
        self.assertEqual("https://example.org/mcp", spec["url"])
        self.assertEqual("preserve me", result["unrelated"])
        self.assertEqual("https://example.com/mcp", self.config["mcp_servers"]["reference"]["url"])

    def test_stdio_round_trip_preserves_argument_boundaries_and_env(self) -> None:
        draft = editor(self.config, "local")
        result = self.action("save", name="local", existing=True, server=draft)
        self.assertEqual(
            self.config["mcp_servers"]["local"]["args"], result["mcp_servers"]["local"]["args"]
        )
        self.assertEqual(
            self.config["mcp_servers"]["local"]["env"], result["mcp_servers"]["local"]["env"]
        )

    def test_replace_and_remove_private_values(self) -> None:
        draft = editor(self.config, "reference")
        draft["headers"] = [{"key": "Authorization", "value": "replacement", "keep": False}]
        result = self.action("save", name="reference", existing=True, server=draft)
        self.assertEqual(
            {"Authorization": "replacement"}, result["mcp_servers"]["reference"]["headers"]
        )
        draft["headers"] = []
        result = self.action("save", name="reference", existing=True, server=draft)
        self.assertEqual({}, result["mcp_servers"]["reference"]["headers"])

    def test_invalid_save_is_atomic(self) -> None:
        before = copy.deepcopy(self.config)
        draft = editor(self.config, "reference")
        draft["headers"] = [
            {"key": "Authorization", "value": "bad\r\nInjected: true", "keep": False}
        ]
        with self.assertRaises(ValueError):
            self.action("save", name="reference", existing=True, server=draft)
        self.assertEqual(before, self.config)

    def test_stale_edit_cannot_clobber_another_change(self) -> None:
        revision = snapshot(self.config)["revision"]
        self.config["mcp_servers"]["reference"]["headers"]["Authorization"] = "changed"
        with self.assertRaisesRegex(ValueError, "changed elsewhere"):
            mutate(self.config, {"action": "remove", "name": "reference", "revision": revision})

    def test_builtin_cannot_be_modified(self) -> None:
        for action in ("save", "remove", "toggle"):
            with self.subTest(action=action), self.assertRaisesRegex(ValueError, "built-in"):
                self.action(action, name="anki", enabled=False)

    def test_disabling_and_enabling_preserves_server_spec(self) -> None:
        result = self.action("toggle", name="local", enabled=False)
        self.assertEqual(self.config["mcp_servers"], result["mcp_servers"])
        self.assertIn("local", result["mcp_disabled"])
        result = self.action("toggle", name="inherited", enabled=True)
        self.assertNotIn("inherited", result["mcp_disabled"])

    def test_remove_does_not_enable_an_inherited_server_of_the_same_name(self) -> None:
        result = self.action("remove", name="local")
        self.assertNotIn("local", result["mcp_servers"])
        self.assertIn("local", result["mcp_disabled"])

    def test_duplicate_add_never_replaces_an_existing_server(self) -> None:
        with self.assertRaises(ValueError):
            self.action("save", name="local", server=editor(self.config, "local"))

    def test_server_names_cannot_inject_tool_deny_patterns(self) -> None:
        for name in ("*", "mcp__*", "foo,bar", "foo\nbar", ""):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.action("toggle", name=name, enabled=False)

    def test_invalid_or_incomplete_spec_never_writes(self) -> None:
        for server in (
            {"transport": "http", "url": "file:///example"},
            {"transport": "http", "url": None},
            {"transport": "stdio", "command": "", "args": []},
            {"transport": "stdio", "command": "echo", "args": "hello"},
        ):
            with self.subTest(server=server), self.assertRaises(ValueError):
                self.action("save", name="new", server=server)

    def test_type_switch_removes_previous_transport_specific_settings(self) -> None:
        result = self.action(
            "save",
            name="reference",
            existing=True,
            server={"transport": "stdio", "command": "example", "args": [], "env": []},
        )
        self.assertNotIn("headers", result["mcp_servers"]["reference"])
        self.assertNotIn("url", result["mcp_servers"]["reference"])

    def test_bridge_persists_only_mcp_keys_and_reports_write_failure_without_values(self) -> None:
        pushed: list[dict[str, Any]] = []
        writes: list[dict[str, Any]] = []
        message = {
            "action": "inherit",
            "enabled": True,
            "revision": snapshot(self.config)["revision"],
            "request_id": "request",
        }
        handle(message, read=lambda: self.config, write=writes.append, push=pushed.append)
        self.assertEqual([{"mcp_inherit_user": True}], writes)
        self.assertEqual("request", pushed[-1]["request_id"])

        def failed_write(_changes: Any) -> None:
            raise OSError("synthetic-private-header")

        handle(message, read=lambda: self.config, write=failed_write, push=pushed.append)
        self.assertNotIn("synthetic-private-header", json.dumps(pushed))
        self.assertIn("error", pushed[-1])


if __name__ == "__main__":
    unittest.main()
