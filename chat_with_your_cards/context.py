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
    overview: str | None,
    *,
    permission_mode: str = "default",
    pins: dict[str, Any] | None = None,
    conventions: str | None = None,
) -> str:
    parts = [
        "You are the assistant inside \"Chat With Your Cards\", a chat dock in "
        "the Anki desktop app. The user is studying; be concise and direct. "
        "Answers render as markdown in a narrow sidebar - prefer short "
        "paragraphs and tight lists.",
        "",
        "You have MCP tools (server \"anki\") to query the user's collection: "
        "search_notes (full Anki search syntax), get_note, get_card, "
        "deck_tree, tag_tree, collection_stats, list_note_types, "
        "get_note_type, find_related (clue-based: field prefixes like "
        "\"Analysis:\", shared tags, same deck), and get_card_images (view a "
        "card's actual images, not just their filenames). Reads are allowed "
        "without asking. When looking for related material, prefer "
        "find_related first, then refine with search_notes. Tool listings "
        "show one-line summaries; call tool_help before using an unfamiliar "
        "tool.",
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
            "propose_note_edit. They never write directly: the user reviews a "
            "proposal card (with diffs and a rendered card preview) and "
            "accepts, edits, or rejects it. Check the note type's fields with "
            "get_note_type before proposing; match the user's existing style "
            "(look at similar notes first). Propose one focused note per "
            "concept rather than a batch. For edits, only include fields that "
            "actually change. When you revise a proposal the user has NOT yet "
            "accepted (e.g. they asked for a change to a card still pending "
            "review), pass supersedes=<that proposal_id> so the old card is "
            "set aside in favor of your new one instead of piling up."
        )
        parts.append(
            "\nFor operations across many notes, do NOT loop propose_note_edit: "
            "use rename_tag / find_replace / move_cards for mechanical "
            "operations (each is one confirmation with an affected count), and "
            "a change set (open_change_set -> add_to_change_set per note -> "
            "close_change_set) when each note needs its own judged edit - the "
            "user reviews the whole batch as one unit with sampled diffs. "
            "Notes that change while a batch is open are skipped, never "
            "overwritten blind."
        )
        if permission_mode == "trusted-writes":
            parts.append(
                "\nTrusted-writes is on: your creations, edits, bulk operations "
                "and change sets apply immediately (an Anki backup checkpoint "
                "is created before bulk applies), up to a per-session write "
                "budget - after that, changes queue for manual review. "
                "Deleting notes ALWAYS requires the user's explicit "
                "confirmation. Work carefully: everything is ledgered and "
                "revertible, but the user is trusting you to not need it."
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
        if conventions:
            parts.append(
                "\n<note-conventions>\nThe user's note-authoring conventions - "
                "follow them for every proposal:\n"
                + conventions.strip()
                + "\n</note-conventions>"
            )
    if overview:
        parts.append("\n<collection-overview>\n" + overview + "\n</collection-overview>")
    else:
        parts.append(
            "\nThe collection overview is not computed yet; use deck_tree, "
            "tag_tree, and collection_stats to inspect the collection."
        )
    return "\n".join(parts)


def wrap_user_message(text: str, card_block: str | None) -> str:
    """Prefix the user text with card context when it changed since last send."""
    if card_block is None:
        return text
    return f"{card_block}\n\n{text}"
