"""Skill materialization unit tests (DESIGN.md section 7).

Covers the note-conventions skill: the per-profile source-of-truth copy
under user_files/skills/ (back-compat), and its mirror into the
agent-home skills dir that the harness actually auto-discovers
(COMPLIANCE.md rule 3 - conventions must not be inlined into
--append-system-prompt).
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from chat_with_your_cards.skills import (  # noqa: E402
    load_conventions,
    materialize_agent_skills,
    materialize_conventions_agent_skill,
    materialize_conventions_skill,
)


class MaterializeConventionsAgentSkillTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.agent_home = Path(self._tmp.name) / "agent-home"

    def _skill_path(self) -> Path:
        return self.agent_home / ".claude" / "skills" / "note-conventions" / "SKILL.md"

    def test_writes_skill_under_agent_home_claude_skills(self) -> None:
        path = materialize_conventions_agent_skill(self.agent_home, "Keep answers short.")
        self.assertEqual(path, self._skill_path())
        self.assertTrue(self._skill_path().exists())
        body = self._skill_path().read_text(encoding="utf-8")
        self.assertIn("Keep answers short.", body)

    def test_frontmatter_targets_card_writing_tasks(self) -> None:
        materialize_conventions_agent_skill(self.agent_home, "Keep answers short.")
        body = self._skill_path().read_text(encoding="utf-8")
        self.assertTrue(body.startswith("---\nname: note-conventions\n"))
        # The description is what the harness matches against to decide
        # whether to surface the skill - it must name the proposal tools
        # so a card-writing task actually triggers it.
        self.assertIn("propose_note", body.split("---", 2)[1])

    def test_none_or_blank_text_writes_nothing(self) -> None:
        self.assertIsNone(materialize_conventions_agent_skill(self.agent_home, None))
        self.assertIsNone(materialize_conventions_agent_skill(self.agent_home, "   "))
        self.assertFalse(self._skill_path().exists())

    def test_clears_stale_skill_when_conventions_removed(self) -> None:
        materialize_conventions_agent_skill(self.agent_home, "Keep answers short.")
        self.assertTrue(self._skill_path().exists())
        materialize_conventions_agent_skill(self.agent_home, None)
        self.assertFalse(self._skill_path().exists())

    def test_regenerated_each_call_unlike_the_card_authoring_template(self) -> None:
        materialize_conventions_agent_skill(self.agent_home, "First version.")
        materialize_conventions_agent_skill(self.agent_home, "Second version.")
        body = self._skill_path().read_text(encoding="utf-8")
        self.assertIn("Second version.", body)
        self.assertNotIn("First version.", body)


class LoadConventionsIntegrationTest(unittest.TestCase):
    """The full chain: config prompt -> user_files source of truth ->
    agent-home mirror, the same sequence __init__.py._setup runs."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.user_files = Path(self._tmp.name) / "user_files"
        self.agent_home = self.user_files / "agent-home"

    def test_config_prompt_flows_through_to_agent_home(self) -> None:
        text = load_conventions(self.user_files, "Prefer cloze for vocab.")
        materialize_conventions_agent_skill(self.agent_home, text)
        mirrored = (
            self.agent_home / ".claude" / "skills" / "note-conventions" / "SKILL.md"
        )
        self.assertIn("Prefer cloze for vocab.", mirrored.read_text(encoding="utf-8"))
        # Back-compat/source-of-truth copy still lands under user_files/.
        source_of_truth = self.user_files / "skills" / "note-conventions" / "SKILL.md"
        self.assertTrue(source_of_truth.exists())

    def test_previous_run_or_hand_dropped_skill_is_reused(self) -> None:
        # Empty config prompt: load_conventions falls back to whatever is
        # already sitting in user_files/skills/note-conventions/SKILL.md
        # (a previous run's file, or a hand-authored full-skill drop-in).
        materialize_conventions_skill(self.user_files, "Old preference text.")
        text = load_conventions(self.user_files, "")
        self.assertEqual(text, "Old preference text.")
        path = materialize_conventions_agent_skill(self.agent_home, text)
        self.assertIsNotNone(path)
        assert path is not None
        self.assertIn("Old preference text.", path.read_text(encoding="utf-8"))

    def test_no_conventions_configured_mirrors_nothing(self) -> None:
        text = load_conventions(self.user_files, "")
        self.assertIsNone(text)
        self.assertIsNone(materialize_conventions_agent_skill(self.agent_home, text))


class MaterializeAgentSkillsTest(unittest.TestCase):
    """The fresh-install skill set and its user-owned upgrade behavior."""

    def test_seeds_task_and_maintenance_skills(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            agent_home = Path(tmp) / "agent-home"
            path = materialize_agent_skills(agent_home)
            self.assertTrue(path.exists())
            skills_root = agent_home / ".claude" / "skills"
            for name in (
                "anki-card-authoring",
                "anki-curriculum-design",
                "anki-curriculum-delivery",
                "skill-maintenance",
            ):
                body = (skills_root / name / "SKILL.md").read_text(encoding="utf-8")
                self.assertTrue(body.startswith(f"---\nname: {name}\n"))

    def test_card_defaults_are_adaptive_not_personal_taste(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            agent_home = Path(tmp) / "agent-home"
            path = materialize_agent_skills(agent_home)
            body = path.read_text(encoding="utf-8")
            self.assertIn("`note-conventions` skill override", body)
            self.assertIn("one coherent grading target", body)
            self.assertIn("ordered lists only when order", body)
            self.assertNotIn("One atomic fact per card", body)
            self.assertNotIn("YOUR card taste", body)

    def test_existing_user_owned_skill_is_not_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            agent_home = Path(tmp) / "agent-home"
            path = agent_home / ".claude" / "skills" / "anki-curriculum-design" / "SKILL.md"
            path.parent.mkdir(parents=True)
            path.write_text("custom curriculum rules\n", encoding="utf-8")
            materialize_agent_skills(agent_home)
            self.assertEqual(path.read_text(encoding="utf-8"), "custom curriculum rules\n")


if __name__ == "__main__":
    unittest.main()
