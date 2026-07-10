"""Note-conventions skill, prompt tier (DESIGN.md section 7).

The user writes a conventions prompt in the add-on config; we wrap it
into a minimal SKILL.md under user_files/ (the on-disk shape the full-
skill tier will share in M3) - the per-profile source of truth, kept for
back-compat and for a user who drops in a hand-authored full-skill
directory instead of using the config prompt.

COMPLIANCE.md rule 3: conventions used to also be inlined into
`--append-system-prompt` (see context.build_system_prompt's history).
They no longer are - `materialize_conventions_agent_skill` below mirrors
the same text into agent_home/.claude/skills/note-conventions/SKILL.md,
the conventional directory `materialize_agent_skills` already seeds
(agent cwd = agent_home), so the harness auto-discovers and loads it
like any other skill instead of it being pasted into every session.
"""

from __future__ import annotations

from pathlib import Path

SKILL_HEADER = """---
name: note-conventions
description: The user's Anki note-authoring conventions - style, phrasing, \
field usage, and formatting rules for this collection. Load before \
proposing a new note or editing an existing one (propose_note, \
propose_note_edit).
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


def materialize_conventions_agent_skill(agent_home: Path, text: str | None) -> Path | None:
    """Mirror the resolved conventions text into the agent-home skills dir
    (same directory `materialize_agent_skills` seeds) so the harness
    auto-discovers it like any other skill, instead of it being inlined
    into `--append-system-prompt` (COMPLIANCE.md rule 3).

    `text` is whatever `load_conventions` resolved (config prompt wins,
    else a previous run's - or a hand-dropped full-skill tier's -
    user_files/skills/note-conventions/SKILL.md body). Regenerated every
    run: unlike the anki-card-authoring template, this file's source of
    truth is user_files/ (or the config), not something the user is
    expected to hand-edit inside agent_home directly.
    """
    skill_path = agent_home / ".claude" / "skills" / "note-conventions" / "SKILL.md"
    if not text or not text.strip():
        if skill_path.exists():
            try:
                skill_path.unlink()
            except OSError:
                pass
        return None
    skill_path.parent.mkdir(parents=True, exist_ok=True)
    skill_path.write_text(SKILL_HEADER + text.strip() + "\n", encoding="utf-8")
    return skill_path


CARD_SKILL_TEMPLATE = """---
name: anki-card-authoring
description: How to write flashcards for this user - style, granularity, and \
which proposal tools to use. Load whenever creating or editing cards.
---

# Card authoring for this collection

Edit this file to teach the assistant YOUR card taste. It ships as a
starting template aimed at an experienced Anki user; everything below is
yours to change, and the assistant treats it as the house rules.

## Before writing anything

1. **Calibrate against existing cards.** Search the target deck
   (search_notes, get_note) and read a handful of the user's own recent
   cards: note type, field usage, phrasing style, tag shape. Match what
   you find; do not invent a new style when the deck already has one.
2. **Check the note type's fields** with get_note_type before proposing.
3. **Respect pins** (deck, note type, tags, field defaults) when set.

## Card quality rules

- **One atomic fact per card.** Several small cards beat one dense card;
  never pack multiple independent facts into one note via c1/c2/c3.
- **Minimum information, maximum retrievability**: the front asks exactly
  one thing; the shortest unambiguous phrasing wins.
- **Answers are short.** Lead with the core answer; push nice-to-know
  context into the note's extra/back-context field, visually secondary.
- **No list-length hints in questions** ("name the X", not "name the
  three X"), and no yes/no questions without an appended "Explain."
- **Cloze cards**: one {{c1::...}} deletion per card unless the facts are
  genuinely inseparable; never cloze a single mid-sentence word that
  could be guessed from grammar alone.
- **Formatting**: plain HTML, <b> for the key term, <ul> for enumerations,
  italics only for untested context. No decorative styling.

## Workflow

- Single cards: propose_note, one focused proposal per concept.
- Reworking existing cards: propose_note_edit with only the fields that
  change.
- Sweeps across many notes: open_change_set / add_to_change_set /
  close_change_set so the user reviews one batch, not fifty cards.
- If a card has sources (URIs in fields - get_card_sources), ground your
  edits in the source rather than guessing: WebFetch for pages, Read for
  local PDFs (meta.page jumps you to the spot), read_epub for books.
- When creating a card FROM a source, record the source richly:
  <a href="URI#page=N" data-source='{"chapter": "...", "section": "..."}'>
  readable title, p.N</a> in the source/extra field.
"""


SKILL_MAINTENANCE_TEMPLATE = """---
name: skill-maintenance
description: How to review the user's edits to AI-written cards and update \
the anki-card-authoring skill from them. Load when reviewing edit \
observations or proposing a skill update.
---

# Learning from the user's edits

This file is the user's to edit: it controls HOW the assistant folds
observed preferences back into the card-authoring skill.

## Reading observations

- get_edit_observations returns everything the user changed since the
  last skill review: proposal-card edits made before accepting
  ('reviewed', including declined field changes), later edits to
  accepted notes from the editor/Browser/another device
  ('edited_later'), and deletions of AI-written notes ('deleted_later').
- Look for patterns ACROSS observations. A one-off factual correction is
  not a preference; the same kind of change made repeatedly is.
- Deletions are the strongest signal: the user rejected that card
  entirely - ask what property those cards shared.
- Weigh recent observations over old ones if they conflict.

## Updating the skill

- Propose ONE update via propose_skill_update with the full revised
  skill markdown. Never edit the skill file any other way.
- Integrate new guidance inline where it naturally belongs: sharpen an
  existing bullet, add one next to related rules, or delete guidance the
  user's edits contradict. Do not create a separate "learned
  preferences" section (unless the user rewrites this instruction).
- Keep the skill concise. Prefer revising and merging over appending;
  a skill that only ever grows stops being read.
- summary and patterns are what the user reads on the confirmation
  card: short, plain language, no tool jargon.
- If the observations show no real pattern, say so in chat and do not
  propose an update.
"""


def materialize_agent_skills(agent_home: Path) -> Path:
    """Seed the conventional skills directory the harness picks up.

    The agent runs with cwd = agent_home, so project-level skills live in
    agent_home/.claude/skills/ (system-wide user skills load as usual from
    the user's home). Templates are written once and then left alone -
    they are the user's files to edit to their taste.
    """
    skills_root = agent_home / ".claude" / "skills"
    skill_path = skills_root / "anki-card-authoring" / "SKILL.md"
    if not skill_path.exists():
        skill_path.parent.mkdir(parents=True, exist_ok=True)
        skill_path.write_text(CARD_SKILL_TEMPLATE, encoding="utf-8")
    maintenance_path = skills_root / "skill-maintenance" / "SKILL.md"
    if not maintenance_path.exists():
        maintenance_path.parent.mkdir(parents=True, exist_ok=True)
        maintenance_path.write_text(SKILL_MAINTENANCE_TEMPLATE, encoding="utf-8")
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
