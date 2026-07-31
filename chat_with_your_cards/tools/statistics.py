"""Read-only statistics tools: the collection's actual numbers (#5).

Before these, the agent could see structure (decks, tags, note types) but
not performance: "what's my true retention", "what's due this week",
"how has this card behaved" all came back as guesses or apologies. Every
tool here is a read; nothing goes through the proposal flow.

All SQL runs against the real collection DB on Anki's main thread (the
MCP layer marshals there). Measured on a 287k-card / 1.06M-revlog
collection (2026-07-30): retention SQL 5ms, forecast SQL <1ms,
deck_due_tree 47ms, card_stats_data 18ms, interval percentiles 13ms,
FSRS json_extract aggregates ~107ms - all fine for on-demand calls.
media.check() is the one slow call (~1.5s+); its description says so.
"""

from __future__ import annotations

import re
from typing import Any

from .registry import ToolContext, ToolRegistry, ToolSpec

# revlog.type / RevlogEntry.ReviewKind
REVLOG_KINDS = {
    0: "learning",
    1: "review",
    2: "relearning",
    3: "filtered",
    4: "manual",
    5: "rescheduled",
}
BUTTON_NAMES = {1: "again", 2: "hard", 3: "good", 4: "easy"}
CARD_TYPES = {0: "new", 1: "learning", 2: "review", 3: "relearning"}
MATURE_IVL = 21  # Anki's own young/mature boundary, in days

MAX_DECK_ROWS = 200
MAX_DUPE_GROUPS = 50
MAX_DUPE_NOTES_PER_GROUP = 20
MAX_MEDIA_FILES = 100
MAX_REVLOG_ENTRIES = 200

# Cards sitting in a filtered deck carry their home deck in odid; every
# deck-scoped query must group them under home, or a cram session makes
# its cards vanish from their real deck's numbers.
_HOME_DID = "coalesce(nullif(c.odid, 0), c.did)"

_TAG_STRIP = re.compile(r"<[^>]+>")


def _snippet(text: str, limit: int = 120) -> str:
    text = _TAG_STRIP.sub(" ", str(text))
    text = re.sub(r"\s+", " ", text).strip()
    return text[: limit - 1] + "…" if len(text) > limit else text


def _resolve_deck_scope(col: Any, deck: str | None) -> tuple[list[int] | None, str | None]:
    """Deck name -> that deck plus all children, or None for whole collection.

    A misspelled deck must raise, not silently mean 'everything': stats for
    the wrong scope read exactly like stats for the right one.
    """
    if not deck:
        return None, None
    name = str(deck).strip()
    try:
        did = int(col.decks.id_for_name(name))
    except Exception:
        did = 0
    if not did:
        all_names = [d.name for d in col.decks.all_names_and_ids()]
        similar = [n for n in all_names if name.lower() in n.lower()][:10]
        hint = f" Similar names: {similar}" if similar else ""
        raise ValueError(f"deck not found: {name!r}.{hint}")
    try:
        dids = [int(x) for x in col.decks.deck_and_child_ids(did)]
    except Exception:
        prefix = name + "::"
        dids = [
            int(d.id)
            for d in col.decks.all_names_and_ids()
            if d.name == name or d.name.startswith(prefix)
        ]
    return dids, name


def _did_clause(dids: list[int] | None) -> str:
    if dids is None:
        return "1=1"
    return f"{_HOME_DID} in ({','.join(str(d) for d in dids)})"


def _rate(passed: int, failed: int) -> float | None:
    total = passed + failed
    return round(passed / total, 4) if total else None


def get_study_stats(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """Real revlog-backed statistics: true retention, answer buttons,
    review counts/time, card states, interval spread, FSRS averages."""
    col = ctx.col
    days = max(1, min(int(args.get("days", 30)), 36_500))
    dids, deck_name = _resolve_deck_scope(col, args.get("deck"))
    day_cutoff = int(col.sched.day_cutoff)
    cutoff_ms = (day_cutoff - days * 86_400) * 1000
    scope = _did_clause(dids)
    # Revlog rows join cards for deck scoping only; unscoped stays joinless
    # so reviews of since-deleted cards still count, like Anki's own stats.
    joined_from = f"from revlog r join cards c on c.id = r.cid where {scope} and"
    plain_from = "from revlog r where"
    rev_from = plain_from if dids is None else joined_from

    by_kind: dict[str, int] = {}
    total_reviews = 0
    for kind, count in col.db.all(
        f"select r.type, count() {rev_from} r.id > ? group by r.type", cutoff_ms
    ):
        by_kind[REVLOG_KINDS.get(kind, f"unknown-{kind}")] = count
        total_reviews += count

    time_ms, study_days = col.db.all(
        f"select sum(r.time), count(distinct (? - r.id/1000 - 1) / 86400) "
        f"{rev_from} r.id > ?",
        day_cutoff,
        cutoff_ms,
    )[0]
    time_secs = int((time_ms or 0) // 1000)
    counted = total_reviews - by_kind.get("manual", 0) - by_kind.get("rescheduled", 0)

    retention: dict[str, Any] = {
        "young": {"pass": 0, "fail": 0},
        "mature": {"pass": 0, "fail": 0},
    }
    for maturity, passed, failed in col.db.all(
        f"""
        select case when r.lastIvl >= {MATURE_IVL} then 'mature' else 'young' end,
               sum(r.ease > 1), sum(r.ease = 1)
        {rev_from} r.id > ? and r.type = 1 and r.ease between 1 and 4
        group by 1
        """,
        cutoff_ms,
    ):
        retention[maturity] = {"pass": passed or 0, "fail": failed or 0}
    for bucket in retention.values():
        bucket["rate"] = _rate(bucket["pass"], bucket["fail"])
    total_pass = retention["young"]["pass"] + retention["mature"]["pass"]
    total_fail = retention["young"]["fail"] + retention["mature"]["fail"]
    retention["total"] = {
        "pass": total_pass,
        "fail": total_fail,
        "rate": _rate(total_pass, total_fail),
    }
    retention["definition"] = (
        "normal review-kind answers only (learning, relearning, cram and "
        f"manual entries excluded); pass = anything above Again; mature = "
        f"previous interval >= {MATURE_IVL} days"
    )

    buttons: dict[str, dict[str, int]] = {}
    for bucket, ease, count in col.db.all(
        f"""
        select case when r.type in (0, 2) then 'learning'
                    when r.type = 3 then 'filtered'
                    when r.lastIvl >= {MATURE_IVL} then 'mature'
                    else 'young' end,
               r.ease, count()
        {rev_from} r.id > ? and r.type in (0, 1, 2, 3) and r.ease between 1 and 4
        group by 1, 2
        """,
        cutoff_ms,
    ):
        buttons.setdefault(bucket, {})[BUTTON_NAMES[ease]] = count

    states = {name: 0 for name in CARD_TYPES.values()}
    for ctype, count in col.db.all(
        f"select c.type, count() from cards c where {scope} group by c.type"
    ):
        states[CARD_TYPES.get(ctype, f"unknown-{ctype}")] = count
    states["suspended"] = 0
    states["buried"] = 0
    for queue, count in col.db.all(
        f"select c.queue, count() from cards c where {scope} and c.queue < 0 group by c.queue"
    ):
        if queue == -1:
            states["suspended"] = count
        elif queue in (-2, -3):
            states["buried"] += count
    states["total"] = sum(
        states[name] for name in CARD_TYPES.values()
    )

    ivls = col.db.list(
        f"select c.ivl from cards c where {scope} and c.type in (2, 3) "
        "and c.queue != -1 order by c.ivl"
    )
    if ivls:
        n = len(ivls)
        intervals: dict[str, Any] = {
            "count": n,
            "p25": ivls[n // 4],
            "p50": ivls[n // 2],
            "p75": ivls[(n * 3) // 4],
            "p90": ivls[min(n - 1, (n * 9) // 10)],
            "max": ivls[-1],
            "avg": round(sum(ivls) / n, 1),
        }
    else:
        intervals = {"count": 0}
    intervals["note"] = "days; review/relearning cards, suspended excluded"

    try:
        fsrs_count, avg_s, avg_d = col.db.all(
            f"""
            select count(), avg(json_extract(c.data, '$.s')),
                   avg(json_extract(c.data, '$.d'))
            from cards c where {scope} and c.queue != -1 and c.data like '%"s"%'
            """
        )[0]
        fsrs: dict[str, Any] = (
            {
                "cards_with_memory_state": fsrs_count,
                "avg_stability_days": round(avg_s, 1),
                "avg_difficulty": round(avg_d, 2),
            }
            if fsrs_count
            else {"cards_with_memory_state": 0}
        )
    except Exception:
        fsrs = {"available": False}

    return {
        "scope": {"deck": deck_name, "days": days},
        "reviews": {
            "total": total_reviews,
            "by_kind": by_kind,
            "study_days": study_days or 0,
            "avg_reviews_per_study_day": (
                round(counted / study_days, 1) if study_days else 0
            ),
            "time_secs": time_secs,
            "avg_secs_per_review": (
                round(time_secs / counted, 1) if counted else 0
            ),
        },
        "true_retention": retention,
        "answer_buttons": buttons,
        "card_states": states,
        "intervals": intervals,
        "fsrs": fsrs,
    }


def _flatten_due_tree(node: Any, parents: list[str], rows: list[dict[str, Any]]) -> None:
    name = "::".join(parents + [str(node.name)]) if str(node.name) else ""
    if name:
        rows.append(
            {
                "deck": name,
                "new": int(node.new_count),
                "learn": int(node.learn_count),
                "review": int(node.review_count),
                "total_today": int(node.new_count + node.learn_count + node.review_count),
                "uncapped": {
                    "new": int(node.new_uncapped),
                    "review": int(node.review_uncapped),
                    "interday_learning": int(node.interday_learning_uncapped),
                },
                "cards_in_deck": int(node.total_in_deck),
                "filtered": bool(node.filtered),
            }
        )
        next_parents = parents + [str(node.name)]
    else:
        next_parents = parents
    for child in node.children:
        _flatten_due_tree(child, next_parents, rows)


def get_deck_due_counts(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """What the deck browser shows: per-deck remaining counts for today."""
    tree = ctx.col.sched.deck_due_tree()
    rows: list[dict[str, Any]] = []
    _flatten_due_tree(tree, [], rows)
    prefix = str(args.get("prefix") or "")
    if prefix:
        rows = [r for r in rows if r["deck"].startswith(prefix)]
    if not bool(args.get("include_empty", False)):
        rows = [r for r in rows if r["total_today"] or r["cards_in_deck"]]
    total = len(rows)
    truncated = total > MAX_DECK_ROWS
    result: dict[str, Any] = {
        "decks": rows[:MAX_DECK_ROWS],
        "total_decks": total,
        "note": (
            "counts are what remains TODAY after deck limits (what the deck "
            "browser shows); a parent row already includes its children"
        ),
    }
    if truncated:
        result["truncated"] = (
            f"showing {MAX_DECK_ROWS} of {total} decks - narrow with `prefix`"
        )
    return result


def get_due_forecast(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """Review load ahead: raw due dates per day plus the overdue backlog."""
    col = ctx.col
    days = max(1, min(int(args.get("days", 14)), 365))
    dids, deck_name = _resolve_deck_scope(col, args.get("deck"))
    scope = _did_clause(dids)
    today = int(col.sched.today)

    backlog = col.db.scalar(
        f"select count() from cards c where {scope} and c.queue in (2, 3) and c.due < ?",
        today,
    ) or 0
    per_day = {
        day: count
        for day, count in col.db.all(
            f"select c.due - ?, count() from cards c where {scope} "
            "and c.queue in (2, 3) and c.due between ? and ? group by c.due",
            today,
            today,
            today + days - 1,
        )
    }
    daily = [
        {"days_from_today": i, "due": per_day.get(i, 0)} for i in range(days)
    ]
    total_due = sum(row["due"] for row in daily)
    return {
        "scope": {"deck": deck_name, "days": days},
        "backlog_overdue": backlog,
        "daily": daily,
        "total_in_window": total_due,
        "avg_per_day": round(total_due / days, 1),
        "note": (
            "raw card due dates ignoring daily limits (day 0 = today's "
            "remaining reviews); intraday learning steps are not included - "
            "get_deck_due_counts has today's limit-aware picture"
        ),
    }


def _has_field(msg: Any, field: str) -> bool:
    try:
        return bool(msg.HasField(field))
    except Exception:
        return getattr(msg, field, None) is not None


def get_card_history(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """One card's full performance record via col.card_stats_data - the
    same numbers Anki's card info dialog shows, including FSRS state."""
    col = ctx.col
    card_id = int(args["card_id"])
    col.get_card(card_id)  # existence check with a clean error
    stats = col.card_stats_data(card_id)
    limit = max(1, min(int(args.get("limit", 50)), MAX_REVLOG_ENTRIES))

    entries = []
    for entry in list(stats.revlog)[:limit]:  # backend returns newest first
        row: dict[str, Any] = {
            "time": int(entry.time),
            "kind": REVLOG_KINDS.get(int(entry.review_kind), str(entry.review_kind)),
            "button": BUTTON_NAMES.get(int(entry.button_chosen)),
            "interval_days": round(int(entry.interval) / 86_400, 2),
            "taken_secs": round(float(entry.taken_secs), 1),
        }
        if _has_field(entry, "memory_state"):
            row["fsrs"] = {
                "stability_days": round(float(entry.memory_state.stability), 1),
                "difficulty": round(float(entry.memory_state.difficulty), 2),
            }
        entries.append(row)

    result: dict[str, Any] = {
        "card_id": int(stats.card_id),
        "note_id": int(stats.note_id),
        "deck": str(stats.deck),
        "note_type": str(stats.notetype),
        "template": str(stats.card_type),
        "preset": str(stats.preset),
        "added": int(stats.added),
        "first_review": int(stats.first_review) or None,
        "latest_review": int(stats.latest_review) or None,
        "due": (
            {"date": int(stats.due_date)}
            if int(stats.due_date)
            else {"new_queue_position": int(stats.due_position)}
        ),
        "interval_days": int(stats.interval),
        "ease_factor": int(stats.ease) or None,
        "reviews": int(stats.reviews),
        "lapses": int(stats.lapses),
        "average_secs": round(float(stats.average_secs), 1),
        "total_secs": round(float(stats.total_secs), 1),
        "revlog": entries,
        "revlog_total": len(stats.revlog),
        "revlog_shown": len(entries),
        "times_are": "unix epoch seconds",
    }
    if _has_field(stats, "memory_state"):
        result["fsrs"] = {
            "stability_days": round(float(stats.memory_state.stability), 1),
            "difficulty": round(float(stats.memory_state.difficulty), 2),
            "retrievability": round(float(stats.fsrs_retrievability), 4),
            "desired_retention": round(float(stats.desired_retention), 2),
        }
    return result


def find_duplicates(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """Anki's Find Duplicates: notes sharing a field value."""
    field = str(args["field"]).strip()
    search = str(args.get("search") or "")
    groups = ctx.col.find_dupes(field, search)
    shown = []
    for value, note_ids in groups[:MAX_DUPE_GROUPS]:
        ids = [int(n) for n in note_ids]
        shown.append(
            {
                "value": _snippet(value),
                "note_count": len(ids),
                "note_ids": ids[:MAX_DUPE_NOTES_PER_GROUP],
            }
        )
    result: dict[str, Any] = {
        "field": field,
        "search": search or None,
        "groups_total": len(groups),
        "notes_total": sum(len(nids) for _, nids in groups),
        "groups": shown,
    }
    if len(groups) > MAX_DUPE_GROUPS:
        result["truncated"] = (
            f"showing {MAX_DUPE_GROUPS} of {len(groups)} groups - "
            "narrow with `search`"
        )
    return result


def check_media(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """Anki's Check Media: files referenced but absent, and vice versa."""
    out = ctx.col.media.check()
    missing = [str(f) for f in out.missing]
    unused = [str(f) for f in out.unused]
    result: dict[str, Any] = {
        "missing_count": len(missing),
        "unused_count": len(unused),
        "missing": missing[:MAX_MEDIA_FILES],
        "unused": unused[:MAX_MEDIA_FILES],
        "have_trash": bool(out.have_trash),
        "caveats": (
            "'unused' only proves no note FIELD references the file: names "
            "built dynamically (in templates or JavaScript) look unused here. "
            "Never propose deleting media from this list without the user "
            "confirming each file."
        ),
    }
    if len(missing) > MAX_MEDIA_FILES or len(unused) > MAX_MEDIA_FILES:
        result["truncated"] = f"file lists capped at {MAX_MEDIA_FILES} entries each"
    notes_field = getattr(out, "missing_media_notes", None)
    if notes_field:
        result["notes_with_missing_media"] = [int(n) for n in list(notes_field)[:50]]
    return result


def register_statistics_tools(registry: ToolRegistry) -> None:
    registry.register(
        ToolSpec(
            "get_study_stats",
            "Real study statistics from the review log: true retention "
            "(young/mature pass rates), answer-button breakdown, review "
            "counts and time, card counts by state, interval spread, and "
            "average FSRS stability/difficulty. Scope to one deck (children "
            "included) and a trailing window of days. Use this instead of "
            "guessing performance numbers.",
            {
                "type": "object",
                "properties": {
                    "days": {
                        "type": "integer",
                        "default": 30,
                        "description": "Trailing window for revlog-based "
                        "numbers (retention, buttons, time). Card states, "
                        "intervals and FSRS describe the present regardless.",
                    },
                    "deck": {
                        "type": "string",
                        "description": "Full deck path; includes subdecks. "
                        "Omit for the whole collection.",
                    },
                },
            },
            get_study_stats,
        )
    )
    registry.register(
        ToolSpec(
            "get_deck_due_counts",
            "Per-deck new/learn/review counts remaining TODAY, exactly what "
            "the deck browser shows (daily limits applied; parents include "
            "children). Also each deck's uncapped totals and card count. "
            "Decks with nothing due and no cards are omitted unless "
            "include_empty.",
            {
                "type": "object",
                "properties": {
                    "prefix": {
                        "type": "string",
                        "description": "Deck name or path prefix filter, "
                        "e.g. 'Spanish' or 'Spanish::Verbs'.",
                    },
                    "include_empty": {"type": "boolean", "default": False},
                },
            },
            get_deck_due_counts,
        )
    )
    registry.register(
        ToolSpec(
            "get_due_forecast",
            "Review load ahead: cards becoming due on each of the next N "
            "days (raw due dates, ignoring daily limits) plus the overdue "
            "backlog. Scope to one deck (children included) or the whole "
            "collection.",
            {
                "type": "object",
                "properties": {
                    "days": {"type": "integer", "default": 14, "maximum": 365},
                    "deck": {
                        "type": "string",
                        "description": "Full deck path; includes subdecks. "
                        "Omit for the whole collection.",
                    },
                },
            },
            get_due_forecast,
        )
    )
    registry.register(
        ToolSpec(
            "get_card_history",
            "One card's complete performance record (Anki's card info): "
            "every review with button pressed, interval and time taken, "
            "plus totals, lapses, and FSRS stability/difficulty/"
            "retrievability when available.",
            {
                "type": "object",
                "properties": {
                    "card_id": {"type": "integer"},
                    "limit": {
                        "type": "integer",
                        "default": 50,
                        "maximum": MAX_REVLOG_ENTRIES,
                        "description": "Max review-log entries (newest first).",
                    },
                },
                "required": ["card_id"],
            },
            get_card_history,
        )
    )
    registry.register(
        ToolSpec(
            "find_duplicates",
            "Anki's Find Duplicates: groups of notes whose given field has "
            "the same value. Optionally narrow with an Anki search (e.g. "
            "deck:\"X\") - a whole-collection scan on a very large "
            "collection can take a second or two.",
            {
                "type": "object",
                "properties": {
                    "field": {
                        "type": "string",
                        "description": "Field name to compare, e.g. 'Front'.",
                    },
                    "search": {
                        "type": "string",
                        "description": "Anki search limiting which notes are "
                        "checked. Empty = all notes having that field.",
                    },
                },
                "required": ["field"],
            },
            find_duplicates,
        )
    )
    registry.register(
        ToolSpec(
            "check_media",
            "Anki's Check Media: files referenced by notes but missing from "
            "the media folder, and files present but referenced by no note "
            "field. SLOW on large collections (seconds; Anki is briefly "
            "unresponsive) - run it when the user asks about media health, "
            "not routinely. Treat 'unused' as candidates only, never as "
            "safe to delete.",
            {"type": "object", "properties": {}},
            check_media,
        )
    )
