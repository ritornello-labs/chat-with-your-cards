"""Context assembly: system prompt, card block, clues (DESIGN.md section 4).

Pure functions over plain data; the controller extracts card info from
aqt state and feeds it here.
"""

from __future__ import annotations

from typing import Any

from .tools.collection import extract_field_prefixes
from .tools.media import image_filenames


def extract_card_info(col: Any, card: Any) -> dict[str, Any]:
    """Pull the current-card context out of a live anki Card."""
    note = card.note()
    return {
        "card_id": card.id,
        "note_id": note.id,
        "deck": col.decks.name(card.did),
        "note_type": note.note_type()["name"],
        "template": card.template().get("name"),
        "tags": list(note.tags),
        "fields": dict(note.items()),
        "scheduling": {
            "interval_days": card.ivl,
            "reps": card.reps,
            "lapses": card.lapses,
        },
    }


def build_card_block(info: dict[str, Any]) -> str:
    lines = [
        "<current-card>",
        f"Deck: {info['deck']}",
        f"Note type: {info['note_type']} (template: {info['template']})",
        f"Tags: {', '.join(info['tags']) if info['tags'] else '(none)'}",
        f"Card id: {info['card_id']} · Note id: {info['note_id']}",
        (
            "Scheduling: interval {interval_days}d, {reps} reps, "
            "{lapses} lapses".format(**info["scheduling"])
        ),
        "Fields:",
    ]
    for name, value in info["fields"].items():
        lines.append(f"  {name}: {value}")
    images = image_filenames(list(info["fields"].values()))
    if images:
        lines.append(
            f"Images: this card embeds {len(images)} image(s) "
            + "(" + ", ".join(images[:4]) + ("…" if len(images) > 4 else "") + "). "
            + "You see the field HTML, not the pixels — call get_card_images "
            + f"with card id {info['card_id']} to actually view them when the "
            + "visual matters."
        )
    clues = extract_field_prefixes(list(info["fields"].values()))
    if clues:
        lines.append(
            "Clues: field prefix(es) "
            + ", ".join(f'"{c}:"' for c in clues)
            + ' — cards in this collection often share prefixes; try searches like '
            + f'"{clues[0]}:*" or the find_related tool.'
        )
    lines.append("</current-card>")
    return "\n".join(lines)


def build_pins_block(pins: dict[str, Any] | None) -> str | None:
    """Render user pins as constraints the agent must not fight (DESIGN.md 8)."""
    pins = pins or {}
    lines = []
    if pins.get("deck"):
        lines.append(f"- Deck is pinned to {pins['deck']!r}: every proposal goes there.")
    if pins.get("note_type"):
        lines.append(
            f"- Note type is pinned to {pins['note_type']!r}: propose_note only "
            "accepts this type."
        )
    if pins.get("tags"):
        lines.append(
            "- Pinned tags added to every proposal: " + ", ".join(pins["tags"]) + "."
        )
    if pins.get("fields"):
        lines.append(
            "- Prefilled field defaults (keep unless you have strong reason, and "
            "say so when you override): "
            + "; ".join(f"{k} = {v!r}" for k, v in pins["fields"].items())
        )
    if not lines:
        return None
    return (
        "The user pinned these note-creation constraints in the dock. Do not "
        "fight them:\n" + "\n".join(lines)
    )


def build_system_prompt(
    *,
    permission_mode: str = "default",
    pins: dict[str, Any] | None = None,
) -> str:
    """Assemble the string passed to `--append-system-prompt`.

    COMPLIANCE.md rule 3: this must stay lean and roughly constant-size. The ceiling test asserts under 4,000 chars in the worst case. The two unbounded inputs that used to be
    inlined here - the collection overview and the user's note conventions -
    are gone from this function's signature on purpose:
      - the overview travels in the FIRST user message of the session as a
        <collection-overview> block (see controller.wrap_user_message);
      - conventions are a skill the harness loads on demand (see
        skills.materialize_conventions_agent_skill), pointed at below by a
        fixed sentence rather than inlined.
    Only base instructions, the permission-mode paragraphs, and the (small,
    bounded) pins block belong here. Do not reinline bulk/unbounded content;
    test_context_and_stats.py's length-ceiling test guards this.
    """
    parts = [
        "You are the assistant inside \"Chat With Your Cards\", a chat dock in "
        "the Anki desktop app. The user is studying; be concise and direct. "
        "Answers render as markdown in a narrow sidebar - prefer short "
        "paragraphs and tight lists.",
        "",
        # Tight pointer only (COMPLIANCE rule 3 length ceiling); the full
        # version is agent-home/CLAUDE.md (materialize_agent_environment),
        # which subagents also load. Without this the agent spawned subagents
        # to try writing files, then flip-flopped (dogfood 2026-07-12).
        "No shell/file-writing (Bash/Write/Edit off, incl. subagents; Read is "
        "read-only). If a task needs running code or a generated file, tell "
        "the user how - don't try.",
        "",
        "You have MCP tools (server \"anki\") over the user's collection: "
        "search_notes (full Anki search syntax), get_note, get_card, "
        "deck_tree, tag_tree, collection_stats, list_note_types, "
        "get_note_type, find_related (clue-based: field prefixes like "
        "\"Analysis:\", shared tags, same deck), get_card_images (actual "
        "images, not just filenames). Reads need no confirmation; prefer "
        "find_related before search_notes for related material. Cards may "
        "carry source URIs in their fields - get_card_sources finds them "
        "(with position metadata when present): WebFetch for web sources, "
        "Read for local PDFs (meta.page), read_epub for EPUBs, to ground "
        "explanations and propose more cards from the same source. When "
        "creating a card from a source, record a rich anchor: "
        "<a href=\"URI#page=N\" data-source='{\"chapter\": \"...\", "
        "\"section\": \"...\"}'>title, p.N</a> so future sessions jump "
        "straight to the spot.",
        "",
        "When a <current-card> block is present in a message, that is the "
        "card the user is looking at right now; treat it as the default "
        "subject of the conversation.",
    ]
    if permission_mode == "read-only":
        parts.append(
            "\nThis session is read-only: do not attempt any modification."
        )
    else:
        parts.append(
            "\nTo create or change notes, use propose_note and "
            "propose_note_edit - never direct writes: the user reviews a "
            "proposal card (diffs plus a rendered preview) and accepts, "
            "edits, or rejects it. Check the note type's fields with "
            "get_note_type before proposing; match the user's existing style "
            "(look at similar notes first). Propose one focused note per "
            "concept, not a batch. For edits, only include fields that "
            "actually change. Revising a still-pending proposal (e.g. the "
            "user asks for a change before it's accepted)? Pass "
            "supersedes=<that proposal_id> so the old card is set aside "
            "instead of piling up."
        )
        parts.append(
            "\nFor many-note operations, don't loop propose_note_edit: use "
            "rename_tag / find_replace / move_cards for mechanical ops (one "
            "confirmation, with an affected count), and a change set "
            "(open_change_set -> add_to_change_set per note -> "
            "close_change_set) for edits needing per-note judgment - the "
            "user reviews the whole batch with sampled diffs. Notes changed "
            "while a batch is open are skipped, never overwritten blind."
        )
        parts.append(
            "\nYou can also manage decks: create_deck, rename_deck (subdecks "
            "follow), set_deck_options (presets may be shared - "
            "get_deck_info shows the config and who shares it), and "
            "filtered decks via create_filtered_deck / update_filtered_deck / "
            "filtered_deck_action (rebuild or empty) - one confirmation card "
            "each, like the bulk tools."
        )
        if permission_mode == "trusted-writes":
            parts.append(
                "\nTrusted-writes is on: creations, edits, bulk operations "
                "and change sets apply immediately (an Anki backup checkpoint "
                "runs before bulk applies), up to a per-session write "
                "budget - past that, changes queue for manual review. "
                "Deleting notes ALWAYS needs the user's explicit "
                "confirmation. Work carefully: everything is ledgered and "
                "revertible, but the user is trusting you not to need it."
            )
        if permission_mode == "auto-accept":
            parts.append(
                "\nAuto-accept is on: your note creations apply immediately "
                "(up to a session cap) and are tagged ai-created. Be "
                "conservative - only create notes the user clearly asked for. "
                "Edits still require manual review."
            )
        pins_block = build_pins_block(pins)
        if pins_block:
            parts.append("\n" + pins_block)
        parts.append(
            "\nYour note-authoring conventions live in the note-conventions "
            "skill - load it before proposing or editing any card."
        )
    parts.append(
        "\nThe collection overview (when available) arrives as a "
        "<collection-overview> block on your first message; until then, use "
        "deck_tree, tag_tree, and collection_stats."
    )
    return "\n".join(parts)


def wrap_user_message(
    text: str, card_block: str | None, overview_block: str | None = None
) -> str:
    """Prefix the user text with context blocks.

    `overview_block` (the <collection-overview>...</collection-overview>
    text, when the controller decides this is the first message of the
    session - COMPLIANCE.md rule 3) goes first, then `card_block` when the
    current card changed since the last send. Either, both, or neither may
    be present.
    """
    prefix = "\n\n".join(block for block in (overview_block, card_block) if block)
    if not prefix:
        return text
    return f"{prefix}\n\n{text}"
