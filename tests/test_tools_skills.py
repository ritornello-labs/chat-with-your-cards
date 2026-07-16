"""propose_new_skill tool wrapper (tools/skills.py, workspace task #20).

The tool itself is a thin delegate to ProposalManager.submit_skill_create
(exercised in depth in test_proposals.py's SkillCreateTests); these tests
pin the registry wiring and the delegation contract.
"""

from __future__ import annotations

import unittest
from typing import Any

from chat_with_your_cards.tools import build_registry
from chat_with_your_cards.tools.skills import propose_new_skill


class _Ctx:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

        class _Proposals:
            def submit_skill_create(_self, args: dict[str, Any]) -> dict[str, Any]:
                self.calls.append(args)
                return {"status": "pending_user_review", "proposal_id": "p1"}

        self.proposals = _Proposals()


class PropposeNewSkillToolTests(unittest.TestCase):
    def test_registered_in_default_registry_and_marked_as_a_write(self) -> None:
        specs = {spec.name: spec for spec in build_registry().specs(include_trusted=True)}
        self.assertIn("propose_new_skill", specs)
        self.assertTrue(specs["propose_new_skill"].writes)
        schema = specs["propose_new_skill"].input_schema
        self.assertEqual(
            {"name", "description", "markdown", "rationale"},
            set(schema["required"]),
        )

    def test_delegates_args_verbatim_to_proposal_manager(self) -> None:
        ctx = _Ctx()
        args = {
            "name": "tts-audio-workflow",
            "description": "Generate TTS audio via the user's API.",
            "markdown": "# steps\n",
            "rationale": "worked it out, will need it again",
        }
        result = propose_new_skill(ctx, args)
        self.assertEqual([args], ctx.calls)
        self.assertEqual("pending_user_review", result["status"])

    def test_description_mentions_when_to_use_it(self) -> None:
        # Since context.py (system prompt) is off-limits for this feature,
        # the tool's own description is the only channel telling the agent
        # this exists and when to reach for it.
        specs = {spec.name: spec for spec in build_registry().specs(include_trusted=True)}
        description = specs["propose_new_skill"].description.lower()
        self.assertIn("reusable workflow", description)
        self.assertIn("kebab-case", description)


if __name__ == "__main__":
    unittest.main()
