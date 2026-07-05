"""Note-conventions skill, prompt tier (DESIGN.md section 7).

The user writes a conventions prompt in the add-on config; we wrap it
into a minimal SKILL.md under user_files/ (the on-disk shape the full-
skill tier will share in M3) and inject its content into the system
prompt for note proposals.
"""

from __future__ import annotations

from pathlib import Path

SKILL_HEADER = """---
name: note-conventions
description: The user's Anki note-authoring conventions for proposed notes.
---

"""


def materialize_conventions_skill(user_files: Path, prompt: str) -> str | None:
    """Write SKILL.md from the config prompt; returns the prompt text or None."""
    text = prompt.strip()
    skill_dir = user_files / "skills" / "note-conventions"
    skill_path = skill_dir / "SKILL.md"
    if not text:
        return None
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_path.write_text(SKILL_HEADER + text + "\n", encoding="utf-8")
    return text


CARD_SKILL_TEMPLATE = """---
name: anki-card-authoring
description: How to write flashcards for this user - style, granularity, and \
which proposal tools to use. Load whenever creating or editing cards.
---

# Card authoring for this collection

Edit this file to teach the assistant YOUR card taste. It ships as a
starting template; everything below is yours to change.

## Style

- One focused fact per card; prefer several small cards over one dense card.
- Front asks exactly one question; the shortest unambiguous phrasing wins.
- Back leads with the answer, then at most one line of context.
- Match the deck's existing conventions before inventing new ones - read a
  few similar notes first (search_notes / get_note).

## Workflow

- Use propose_note for single cards; check the note type's fields with
  get_note_type first.
- For many similar cards, still propose them individually unless the user
  asked for a sweep - then use a change set.
- Respect the user's pinned deck, note type, tags, and field defaults.

## Formatting

- Plain HTML in fields: <b> for the key term, no inline styles.
- Cloze cards: one {{c1::...}} per card unless the facts are inseparable.
"""


def materialize_agent_skills(agent_home: Path) -> Path:
    """Seed the conventional skills directory the harness picks up.

    The agent runs with cwd = agent_home, so project-level skills live in
    agent_home/.claude/skills/ (system-wide user skills load as usual from
    the user's home). The card-authoring template is written once and then
    left alone - it is the user's file to edit to their taste.
    """
    skill_dir = agent_home / ".claude" / "skills" / "anki-card-authoring"
    skill_path = skill_dir / "SKILL.md"
    if not skill_path.exists():
        skill_dir.mkdir(parents=True, exist_ok=True)
        skill_path.write_text(CARD_SKILL_TEMPLATE, encoding="utf-8")
    return skill_path


def load_conventions(user_files: Path, config_prompt: str) -> str | None:
    """Config prompt wins; otherwise reuse a SKILL.md left by a previous run
    (or dropped in by hand - a preview of the M3 full-skill tier)."""
    text = materialize_conventions_skill(user_files, config_prompt)
    if text is not None:
        return text
    skill_path = user_files / "skills" / "note-conventions" / "SKILL.md"
    if skill_path.exists():
        body = skill_path.read_text(encoding="utf-8")
        if body.startswith("---"):
            closing = body.find("---", 3)
            if closing != -1:
                body = body[closing + 3 :]
        body = body.strip()
        return body or None
    return None
