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
    agent_skill_names,
    load_conventions,
    materialize_agent_environment,
    materialize_agent_skills,
    materialize_conventions_agent_skill,
    materialize_conventions_skill,
    write_new_skill,
)


class MaterializeAgentEnvironmentTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.agent_home = Path(self._tmp.name) / "agent-home"

    def test_writes_claude_md_at_agent_home_root(self) -> None:
        # CLAUDE.md must be at the cwd root (agent-home), not under .claude, so
        # Claude Code loads it as project memory for the agent AND subagents.
        path = materialize_agent_environment(self.agent_home)
        self.assertEqual(path, self.agent_home / "CLAUDE.md")
        self.assertTrue(path.exists())

    def test_states_the_hard_limits(self) -> None:
        # Default (sandbox) posture states the hard limits.
        body = materialize_agent_environment(self.agent_home).read_text(encoding="utf-8")
        for token in ("Bash", "Write", "Edit", "subagent", "read-only", "propose_note"):
            self.assertIn(token, body)
        self.assertIn("No shell", body)

    def test_sandbox_variant_explicit(self) -> None:
        body = materialize_agent_environment(
            self.agent_home, "sandbox"
        ).read_text(encoding="utf-8")
        self.assertIn("No shell", body)
        self.assertNotIn("full agent-tools mode", body)

    def test_full_variant_states_shell_and_injection_risk(self) -> None:
        # Full mode: the CLAUDE.md must say the agent HAS shell/write, warn that
        # card content is untrusted (injection), and steer to proposals +
        # AnkiConnect - it must not still claim shell is off.
        body = materialize_agent_environment(self.agent_home, "full").read_text(
            encoding="utf-8"
        )
        self.assertIn("full agent-tools mode", body)
        self.assertIn("untrusted", body)
        self.assertIn("AnkiConnect", body)
        self.assertIn("propose_note", body)
        self.assertNotIn("No shell", body)

    def test_regenerated_each_run_and_switches_variant(self) -> None:
        path = materialize_agent_environment(self.agent_home, "sandbox")
        path.write_text("stale user edit", encoding="utf-8")
        materialize_agent_environment(self.agent_home, "full")  # add-on truth wins
        body = path.read_text(encoding="utf-8")
        self.assertIn("full agent-tools mode", body)
        # Switching back rewrites it to the sandbox variant.
        materialize_agent_environment(self.agent_home, "sandbox")
        self.assertIn("No shell", path.read_text(encoding="utf-8"))


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


class AgentSkillNamesTest(unittest.TestCase):
    """agent_skill_names feeds ProposalManager.submit_skill_create's
    collision check (workspace task #20)."""

    def test_empty_when_no_skills_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(set(), agent_skill_names(Path(tmp) / "agent-home"))

    def test_lists_existing_skill_directories(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            agent_home = Path(tmp) / "agent-home"
            materialize_agent_skills(agent_home)
            names = agent_skill_names(agent_home)
            self.assertIn("anki-card-authoring", names)
            self.assertIn("skill-maintenance", names)

    def test_ignores_stray_files_not_directories(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            agent_home = Path(tmp) / "agent-home"
            skills_root = agent_home / ".claude" / "skills"
            skills_root.mkdir(parents=True)
            (skills_root / "not-a-skill.txt").write_text("stray file", encoding="utf-8")
            self.assertEqual(set(), agent_skill_names(agent_home))


class WriteNewSkillTest(unittest.TestCase):
    """write_new_skill is the ONLY path that ever writes an agent-proposed
    new skill to disk (proposals.py kind "skill_create", accepted via
    __init__.py's _apply_new_skill). Security-critical: must never
    overwrite an existing skill, even under a same-name race."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.agent_home = Path(self._tmp.name) / "agent-home"

    def test_writes_frontmatter_and_body(self) -> None:
        path = write_new_skill(
            self.agent_home,
            "tts-audio-workflow",
            "Generate TTS audio via the user's API.",
            "# TTS workflow\n\nDo the thing.\n",
        )
        self.assertEqual(
            path,
            self.agent_home / ".claude" / "skills" / "tts-audio-workflow" / "SKILL.md",
        )
        body = path.read_text(encoding="utf-8")
        self.assertTrue(body.startswith("---\nname: tts-audio-workflow\n"))
        self.assertIn('description: "Generate TTS audio', body)
        self.assertIn("# TTS workflow", body)
        self.assertIn("Do the thing.", body)

    def test_tags_the_file_as_agent_authored(self) -> None:
        path = write_new_skill(self.agent_home, "foo-bar", "Foo the bar.", "Body.\n")
        body = path.read_text(encoding="utf-8")
        self.assertIn("Agent-authored skill", body)
        self.assertIn("propose_new_skill", body)

    def test_description_with_colon_and_quote_stays_valid_yaml(self) -> None:
        # A raw colon or double-quote in the description would otherwise
        # break the frontmatter's `key: value` line or the fence structure.
        path = write_new_skill(
            self.agent_home,
            "quoting-test",
            'Uses a colon: and a "quoted" word.',
            "Body.\n",
        )
        lines = path.read_text(encoding="utf-8").splitlines()
        self.assertEqual("---", lines[0])
        self.assertEqual("name: quoting-test", lines[1])
        self.assertTrue(lines[2].startswith("description: "))
        self.assertEqual("---", lines[3])
        self.assertIn('\\"quoted\\"', lines[2])
        self.assertIn("colon:", lines[2])

    def test_raises_file_exists_error_without_overwriting(self) -> None:
        write_new_skill(self.agent_home, "dup-name", "First version.", "Original body.\n")
        with self.assertRaises(FileExistsError):
            write_new_skill(self.agent_home, "dup-name", "Second version.", "New body.\n")
        path = self.agent_home / ".claude" / "skills" / "dup-name" / "SKILL.md"
        self.assertIn("Original body.", path.read_text(encoding="utf-8"))
        self.assertNotIn("New body.", path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
