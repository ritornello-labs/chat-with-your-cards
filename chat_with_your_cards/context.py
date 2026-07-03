"""Context assembly: system prompt, card block, clues (DESIGN.md section 4).

Pure functions over plain data; the controller extracts card info from
aqt state and feeds it here.
"""

from __future__ import annotations

from typing import Any

from .tools.collection import extract_field_prefixes


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


def build_system_prompt(
    overview: str | None,
    *,
    permission_mode: str = "default",
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
        "get_note_type, and find_related (clue-based: field prefixes like "
        "\"Analysis:\", shared tags, same deck). Reads are allowed without "
        "asking. When looking for related material, prefer find_related "
        "first, then refine with search_notes.",
        "",
        "When a <current-card> block is present in a message, that is the "
        "card the user is looking at right now; treat it as the default "
        "subject of the conversation.",
    ]
    if permission_mode == "read-only":
        parts.append(
            "\nThis session is read-only: do not attempt any modification."
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
