"""Agent-tools axis (DESIGN.md section 5) at the controller boundary: the
`agent_tools` config key round-trips through set_agent_config /
push_agent_state as `"tools": "sandbox" | "full"`, orthogonal to the
permission mode.

ChatController imports aqt at module load, so this stubs a minimal aqt /
aqt.qt into sys.modules before importing it (the round-trip paths under test
touch neither mw nor QTimer).
"""

from __future__ import annotations

import sys
import tempfile
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# --- minimal aqt stubs so `from aqt import mw` / `from aqt.qt import QTimer`
#     succeed without a running Anki -------------------------------------------
if "aqt" not in sys.modules:
    aqt_stub = types.ModuleType("aqt")
    aqt_stub.mw = None  # type: ignore[attr-defined]
    sys.modules["aqt"] = aqt_stub
    aqt_qt_stub = types.ModuleType("aqt.qt")
    aqt_qt_stub.QTimer = object  # type: ignore[attr-defined]
    sys.modules["aqt.qt"] = aqt_qt_stub

from chat_with_your_cards.controller import (  # noqa: E402
    ChatController,
    _norm_agent_tools,
)


class NormAgentToolsTests(unittest.TestCase):
    def test_known_values_pass_through(self) -> None:
        self.assertEqual("sandbox", _norm_agent_tools("sandbox"))
        self.assertEqual("full", _norm_agent_tools("full"))

    def test_unknown_falls_back_to_sandbox(self) -> None:
        for value in (None, "", "bananas", "FULL", 3):
            self.assertEqual("sandbox", _norm_agent_tools(value))


class AgentStateRoundTripTests(unittest.TestCase):
    def _controller(self, **config_overrides) -> tuple[ChatController, list[dict]]:
        config = {
            "backend": "auto",
            "model": "",
            "effort": "",
            "fast_mode": False,
            "agent_tools": "sandbox",
            "permission_mode": "default",
        }
        config.update(config_overrides)
        pushed: list[dict] = []
        controller = ChatController(
            push=pushed.append,
            config=config,
            system_prompt_builder=lambda: "",
            ensure_mcp=lambda: ("", ""),
            workdir=Path(tempfile.mkdtemp(prefix="cwyc-tools-")),
        )
        return controller, pushed

    def _last_agent(self, pushed: list[dict]) -> dict:
        agents = [p for p in pushed if p.get("type") == "agent"]
        self.assertTrue(agents, "expected an 'agent' state push")
        return agents[-1]

    def test_push_agent_state_includes_tools(self) -> None:
        controller, pushed = self._controller()
        controller.push_agent_state()
        self.assertEqual("sandbox", self._last_agent(pushed)["tools"])

    def test_push_agent_state_reflects_full(self) -> None:
        controller, pushed = self._controller(agent_tools="full")
        controller.push_agent_state()
        self.assertEqual("full", self._last_agent(pushed)["tools"])

    def test_set_agent_config_persists_and_round_trips_full(self) -> None:
        controller, pushed = self._controller()
        controller.set_agent_config("opus", "high", False, "full")
        self.assertEqual("full", controller._config["agent_tools"])
        self.assertEqual("full", self._last_agent(pushed)["tools"])

    def test_set_agent_config_normalizes_bad_value_to_sandbox(self) -> None:
        controller, pushed = self._controller(agent_tools="full")
        controller.set_agent_config("", "", False, "bananas")
        self.assertEqual("sandbox", controller._config["agent_tools"])
        self.assertEqual("sandbox", self._last_agent(pushed)["tools"])

    def test_tools_is_orthogonal_to_permission_mode(self) -> None:
        # Switching agent_tools must not disturb the permission mode, and vice
        # versa: they are separate axes on the pushed agent state.
        controller, pushed = self._controller(permission_mode="read-only")
        controller.set_agent_config("", "", False, "full")
        agent = self._last_agent(pushed)
        self.assertEqual("full", agent["tools"])
        self.assertEqual("read-only", agent["mode"])

    def test_default_agent_tools_arg_is_sandbox(self) -> None:
        # A caller that omits agent_tools (legacy 3-arg call) gets the safe
        # default rather than raising.
        controller, pushed = self._controller(agent_tools="full")
        controller.set_agent_config("sonnet", "low")
        self.assertEqual("sandbox", controller._config["agent_tools"])


if __name__ == "__main__":
    unittest.main()
