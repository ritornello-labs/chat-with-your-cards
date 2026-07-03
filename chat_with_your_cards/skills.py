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
