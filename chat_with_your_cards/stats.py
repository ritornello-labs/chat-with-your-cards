"""Collection stats snapshot: computation, cache, and overview text.

compute_stats/serialize_overview are pure enough for unit tests (they
take a collection object / plain dicts). The StatsCache glue at the
bottom owns persistence, the periodic refresh timer, and QueryOp-based
background computation inside Anki.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable

SCHEMA_VERSION = 1

DAY_SECONDS = 86_400
BUCKETS = (("today", 1), ("7d", 7), ("90d", 90))


def compute_stats(col: Any) -> dict[str, Any]:
    """One pass over the collection; runs in a QueryOp background op."""
    day_cutoff = int(col.sched.day_cutoff)

    per_deck: dict[int, dict[str, Any]] = {}
    for did, cards, notes in col.db.all(
        "select did, count(), count(distinct nid) from cards group by did"
    ):
        per_deck[did] = {"cards": cards, "notes": notes}

    review_ms: dict[int, dict[str, int]] = {}
    cutoffs = {
        name: (day_cutoff - days * DAY_SECONDS) * 1000 for name, days in BUCKETS
    }
    rows = col.db.all(
        """
        select c.did,
               sum(case when r.id > ? then r.time else 0 end),
               sum(case when r.id > ? then r.time else 0 end),
               sum(case when r.id > ? then r.time else 0 end),
               sum(r.time)
        from revlog r join cards c on c.id = r.cid
        group by c.did
        """,
        cutoffs["today"],
        cutoffs["7d"],
        cutoffs["90d"],
    )
    for did, today, seven, ninety, ever in rows:
        review_ms[did] = {
            "today": today or 0,
            "7d": seven or 0,
            "90d": ninety or 0,
            "ever": ever or 0,
        }

    decks = []
    for deck in col.decks.all_names_and_ids(skip_empty_default=False):
        counts = per_deck.get(deck.id, {"cards": 0, "notes": 0})
        ms = review_ms.get(deck.id, {})
        decks.append(
            {
                "name": deck.name,
                "notes": counts["notes"],
                "cards": counts["cards"],
                "review_secs": {
                    key: ms.get(key, 0) // 1000 for key in ("today", "7d", "90d", "ever")
                },
            }
        )
    decks.sort(key=lambda d: d["name"])

    tag_counts: dict[str, int] = {}
    for (tags_str,) in col.db.all("select tags from notes"):
        for tag in tags_str.split():
            tag_counts[tag] = tag_counts.get(tag, 0) + 1
    tags = [{"name": tag, "notes": count} for tag, count in sorted(tag_counts.items())]

    note_types = []
    for nt in col.models.all_names_and_ids():
        count = col.db.scalar("select count() from notes where mid = ?", nt.id) or 0
        note_types.append({"name": nt.name, "notes": count})

    total_notes = col.db.scalar("select count() from notes") or 0
    total_cards = col.db.scalar("select count() from cards") or 0

    return {
        "schema": SCHEMA_VERSION,
        "computed_at": int(time.time()),
        "totals": {"notes": total_notes, "cards": total_cards},
        "decks": decks,
        "tags": tags,
        "note_types": note_types,
    }


def _fmt_secs(secs: int) -> str:
    if secs <= 0:
        return "0"
    if secs < 3600:
        return f"{secs // 60}m" if secs >= 60 else f"{secs}s"
    return f"{secs / 3600:.1f}h"


def _deck_line(deck: dict[str, Any]) -> str:
    rev = deck["review_secs"]
    return (
        f"- {deck['name']} — {deck['notes']} notes / {deck['cards']} cards — "
        f"rev today {_fmt_secs(rev['today'])} · 7d {_fmt_secs(rev['7d'])} · "
        f"90d {_fmt_secs(rev['90d'])} · ever {_fmt_secs(rev['ever'])}"
    )


def estimate_tokens(text: str) -> int:
    return len(text) // 4


def serialize_overview(stats: dict[str, Any], budget_tokens: int = 8000) -> str:
    """Annotated deck+tag overview; full when it fits, folded when not.

    Measure-then-decide (DESIGN.md section 4): render the full trees,
    estimate tokens, and only fold when over budget - never silently.
    """
    age_min = max(0, int(time.time()) - stats["computed_at"]) // 60
    header = (
        f"Collection: {stats['totals']['notes']} notes, "
        f"{stats['totals']['cards']} cards (stats as of {age_min} min ago).\n"
    )
    deck_lines = [_deck_line(d) for d in stats["decks"]]
    tag_lines = [f"- {t['name']} ({t['notes']} notes)" for t in stats["tags"]]
    full = (
        header
        + "\nDecks:\n" + "\n".join(deck_lines)
        + "\n\nTags:\n" + ("\n".join(tag_lines) if tag_lines else "(none)")
    )
    if estimate_tokens(full) <= budget_tokens:
        return full

    # Over budget: shallow decks first, then enforce the budget hard -
    # ~70% of it for decks, the rest for tags - with explicit fold notes;
    # the deck_tree/tag_tree tools always have the complete trees.
    char_budget = budget_tokens * 4
    deck_chars = int(char_budget * 0.7)
    ordered = sorted(
        zip(stats["decks"], deck_lines), key=lambda item: item[0]["name"].count("::")
    )
    kept: list[tuple[str, str]] = []
    used = 0
    for deck, line in ordered:
        if used + len(line) > deck_chars:
            break
        kept.append((deck["name"], line))
        used += len(line)
    kept.sort(key=lambda item: item[0])
    folded = len(deck_lines) - len(kept)

    tag_chars = char_budget - used
    shown_tags: list[str] = []
    for line in tag_lines:
        if tag_chars - len(line) < 0:
            break
        shown_tags.append(line)
        tag_chars -= len(line)

    parts = [header, "\nDecks:\n" + "\n".join(line for _, line in kept)]
    if folded:
        parts.append(
            f"\n… {folded} more decks folded (full tree via the deck_tree tool)."
        )
    parts.append("\n\nTags:\n" + "\n".join(shown_tags))
    if len(tag_lines) > len(shown_tags):
        parts.append(
            f"\n… {len(tag_lines) - len(shown_tags)} more tags "
            "(full list via the tag_tree tool)."
        )
    return "".join(parts)


class StatsCache:
    """Add-on glue: cached snapshot + background refresh. aqt used lazily."""

    def __init__(self, cache_path: Path, refresh_minutes: int = 30) -> None:
        self._path = cache_path
        self._refresh_minutes = max(5, refresh_minutes)
        self._stats: dict[str, Any] | None = None
        self._timer: Any = None
        self._load()

    @property
    def stats(self) -> dict[str, Any] | None:
        return self._stats

    def overview(self, budget_tokens: int = 8000) -> str | None:
        if self._stats is None:
            return None
        return serialize_overview(self._stats, budget_tokens)

    def _load(self) -> None:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            if data.get("schema") == SCHEMA_VERSION:
                self._stats = data
        except (OSError, ValueError):
            self._stats = None

    def _store(self, stats: dict[str, Any]) -> None:
        self._stats = stats
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps(stats), encoding="utf-8")
        except OSError:
            pass  # cache persistence is best-effort

    def start(self, on_refreshed: Callable[[], None] | None = None) -> None:
        """Schedule periodic refresh inside Anki. Call from the main thread."""
        from aqt import mw
        from aqt.qt import QTimer

        def refresh() -> None:
            from aqt.operations import QueryOp

            if mw.col is None:
                return

            def done(stats: dict[str, Any]) -> None:
                self._store(stats)
                if on_refreshed is not None:
                    on_refreshed()

            QueryOp(parent=mw, op=compute_stats, success=done).run_in_background()

        self._timer = QTimer(mw)
        self._timer.setInterval(self._refresh_minutes * 60 * 1000)
        self._timer.timeout.connect(refresh)
        self._timer.start()
        QTimer.singleShot(5_000, refresh)  # initial refresh shortly after startup
