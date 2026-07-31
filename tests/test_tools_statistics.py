"""Statistics tools (#5): true retention, due forecasts, card history.

The SQL-backed tools run against a REAL in-memory SQLite database with
the Anki cards/revlog schema subset, so the queries themselves are
executed, not pattern-matched. Protobuf-shaped results (deck_due_tree,
card_stats_data, media check) use fakes mirroring the field names probed
against Anki 25.09 on 2026-07-30.
"""

from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from chat_with_your_cards.tools import build_registry  # noqa: E402
from chat_with_your_cards.tools.statistics import (  # noqa: E402
    check_media,
    find_duplicates,
    get_card_history,
    get_deck_due_counts,
    get_due_forecast,
    get_study_stats,
)

TODAY = 3000
DAY_CUTOFF = 1_700_000_000  # end of TODAY, epoch seconds
DAY = 86_400


class FakeDB:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def all(self, sql: str, *args: Any) -> list[Any]:
        return [list(row) for row in self._conn.execute(sql, args).fetchall()]

    def list(self, sql: str, *args: Any) -> list[Any]:
        return [row[0] for row in self._conn.execute(sql, args).fetchall()]

    def scalar(self, sql: str, *args: Any) -> Any:
        row = self._conn.execute(sql, args).fetchone()
        return row[0] if row else None


class FakeNamed:
    def __init__(self, deck_id: int, name: str) -> None:
        self.id = deck_id
        self.name = name


class FakeDecks:
    NAMES = {1: "Default", 2: "Spanish", 3: "Spanish::Verbs", 99: "Cram"}
    CHILDREN = {2: [2, 3]}

    def id_for_name(self, name: str) -> int | None:
        for did, deck_name in self.NAMES.items():
            if deck_name == name:
                return did
        return None

    def deck_and_child_ids(self, did: int) -> list[int]:
        return self.CHILDREN.get(did, [did])

    def all_names_and_ids(self) -> list[FakeNamed]:
        return [FakeNamed(did, name) for did, name in self.NAMES.items()]


class FakeCol:
    def __init__(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.executescript(
            """
            create table cards (
                id integer primary key, nid int, did int, ord int,
                type int, queue int, due int, ivl int, factor int,
                reps int, lapses int, odue int, odid int, data text
            );
            create table revlog (
                id integer primary key, cid int, ease int, ivl int,
                lastIvl int, factor int, time int, type int
            );
            """
        )
        self.db = FakeDB(conn)
        self._conn = conn
        self.decks = FakeDecks()
        self.sched = SimpleNamespace(today=TODAY, day_cutoff=DAY_CUTOFF)

    def add_card(self, cid: int, did: int, *, ctype: int, queue: int, due: int = 0,
                 ivl: int = 0, odid: int = 0, data: str = "") -> None:
        self._conn.execute(
            "insert into cards values (?,?,?,0,?,?,?,?,2500,0,0,0,?,?)",
            (cid, cid, did, ctype, queue, due, ivl, odid, data),
        )

    def add_rev(self, cid: int, *, ts: int, rtype: int, ease: int,
                last_ivl: int = 0, time_ms: int = 0) -> None:
        rid = ts * 1000
        while self._conn.execute("select 1 from revlog where id=?", (rid,)).fetchone():
            rid += 1
        self._conn.execute(
            "insert into revlog values (?,?,?,0,?,2500,?,?)",
            (rid, cid, ease, last_ivl, time_ms, rtype),
        )

    def get_card(self, cid: int) -> Any:
        if not self._conn.execute("select 1 from cards where id=?", (cid,)).fetchone():
            raise KeyError(cid)
        return SimpleNamespace(id=cid)

    def __del__(self) -> None:
        self._conn.close()


def _ctx(col: FakeCol) -> Any:
    return SimpleNamespace(col=col)


def _seeded() -> FakeCol:
    col = FakeCol()
    # Cards: home decks 1/2/3, one card cramming in filtered deck 99 (home 2).
    col.add_card(101, 2, ctype=2, queue=2, due=TODAY, ivl=30, data='{"s":100.0,"d":5.0}')
    col.add_card(102, 3, ctype=2, queue=2, due=TODAY + 1, ivl=10, data='{"s":50.0,"d":7.0}')
    col.add_card(103, 99, ctype=2, queue=2, due=TODAY - 5, ivl=400, odid=2)
    col.add_card(104, 1, ctype=0, queue=0)
    col.add_card(105, 1, ctype=2, queue=-1, ivl=100, data='{"s":999.0,"d":9.0}')
    col.add_card(106, 2, ctype=1, queue=1, due=DAY_CUTOFF + 60)
    col.add_card(107, 1, ctype=3, queue=-2, ivl=4)
    # Reviews inside a 30-day window (two distinct study days) + one outside.
    col.add_rev(101, ts=DAY_CUTOFF - 3600, rtype=1, ease=3, last_ivl=30, time_ms=5000)
    col.add_rev(101, ts=DAY_CUTOFF - 7200, rtype=1, ease=1, last_ivl=25, time_ms=4000)
    col.add_rev(102, ts=DAY_CUTOFF - 90_000, rtype=1, ease=2, last_ivl=10, time_ms=3000)
    col.add_rev(103, ts=DAY_CUTOFF - 100_000, rtype=1, ease=3, last_ivl=400, time_ms=2000)
    col.add_rev(106, ts=DAY_CUTOFF - 3600, rtype=0, ease=3, last_ivl=0, time_ms=1000)
    col.add_rev(105, ts=DAY_CUTOFF - 3600, rtype=4, ease=0, last_ivl=0, time_ms=0)
    col.add_rev(101, ts=DAY_CUTOFF - 40 * DAY, rtype=1, ease=1, last_ivl=30, time_ms=9000)
    return col


class StudyStatsTest(unittest.TestCase):
    def test_whole_collection_numbers(self) -> None:
        result = get_study_stats(_ctx(_seeded()), {"days": 30})
        reviews = result["reviews"]
        self.assertEqual(reviews["total"], 6)
        self.assertEqual(reviews["by_kind"], {"review": 4, "learning": 1, "manual": 1})
        self.assertEqual(reviews["study_days"], 2)
        self.assertEqual(reviews["avg_reviews_per_study_day"], 2.5)  # manual excluded
        self.assertEqual(reviews["time_secs"], 15)
        self.assertEqual(reviews["avg_secs_per_review"], 3.0)

        retention = result["true_retention"]
        self.assertEqual(retention["mature"], {"pass": 2, "fail": 1, "rate": round(2 / 3, 4)})
        self.assertEqual(retention["young"], {"pass": 1, "fail": 0, "rate": 1.0})
        self.assertEqual(retention["total"], {"pass": 3, "fail": 1, "rate": 0.75})

        buttons = result["answer_buttons"]
        self.assertEqual(buttons["mature"], {"good": 2, "again": 1})
        self.assertEqual(buttons["young"], {"hard": 1})
        self.assertEqual(buttons["learning"], {"good": 1})

        states = result["card_states"]
        self.assertEqual(states["new"], 1)
        self.assertEqual(states["learning"], 1)
        self.assertEqual(states["review"], 4)
        self.assertEqual(states["relearning"], 1)
        self.assertEqual(states["suspended"], 1)
        self.assertEqual(states["buried"], 1)
        self.assertEqual(states["total"], 7)

        intervals = result["intervals"]
        self.assertEqual(intervals["count"], 4)  # suspended 105 excluded
        self.assertEqual(intervals["p50"], 30)
        self.assertEqual(intervals["max"], 400)
        self.assertEqual(intervals["avg"], 111.0)

        fsrs = result["fsrs"]
        self.assertEqual(fsrs["cards_with_memory_state"], 2)  # suspended excluded
        self.assertEqual(fsrs["avg_stability_days"], 75.0)
        self.assertEqual(fsrs["avg_difficulty"], 6.0)

    def test_deck_scope_follows_home_deck_for_filtered_cards(self) -> None:
        result = get_study_stats(_ctx(_seeded()), {"days": 30, "deck": "Spanish"})
        self.assertEqual(result["scope"]["deck"], "Spanish")
        # Card 103 crams in deck 99 but belongs to Spanish: its review counts.
        self.assertEqual(result["reviews"]["by_kind"], {"review": 4, "learning": 1})
        states = result["card_states"]
        self.assertEqual(states["review"], 3)  # 101, 102, 103
        self.assertEqual(states["learning"], 1)
        self.assertEqual(states["new"], 0)
        self.assertEqual(states["suspended"], 0)

    def test_window_excludes_old_reviews(self) -> None:
        result = get_study_stats(_ctx(_seeded()), {"days": 36_500})
        self.assertEqual(result["reviews"]["by_kind"]["review"], 5)
        self.assertEqual(result["true_retention"]["total"]["fail"], 2)

    def test_unknown_deck_raises_with_similar_names(self) -> None:
        with self.assertRaises(ValueError) as caught:
            get_study_stats(_ctx(_seeded()), {"deck": "Verbs"})
        self.assertIn("Spanish::Verbs", str(caught.exception))


class DueForecastTest(unittest.TestCase):
    def test_daily_counts_and_backlog(self) -> None:
        result = get_due_forecast(_ctx(_seeded()), {"days": 3})
        self.assertEqual(result["backlog_overdue"], 1)  # card 103 five days over
        self.assertEqual(
            [row["due"] for row in result["daily"]], [1, 1, 0]
        )
        self.assertEqual(result["total_in_window"], 2)
        self.assertEqual(result["avg_per_day"], 0.7)

    def test_deck_scope_includes_cramming_card(self) -> None:
        result = get_due_forecast(_ctx(_seeded()), {"days": 3, "deck": "Spanish"})
        self.assertEqual(result["backlog_overdue"], 1)
        self.assertEqual(result["scope"]["deck"], "Spanish")


def _tree_node(name: str, *, new: int = 0, learn: int = 0, review: int = 0,
               total: int = 0, filtered: bool = False,
               children: list[Any] | None = None) -> Any:
    return SimpleNamespace(
        name=name,
        new_count=new,
        learn_count=learn,
        review_count=review,
        new_uncapped=new * 10,
        review_uncapped=review * 10,
        interday_learning_uncapped=learn,
        total_in_deck=total,
        filtered=filtered,
        children=children or [],
    )


class DeckDueCountsTest(unittest.TestCase):
    def _col(self) -> Any:
        tree = _tree_node(
            "",
            children=[
                _tree_node(
                    "Spanish", new=2, learn=1, review=5, total=10,
                    children=[_tree_node("Verbs", review=3, total=4)],
                ),
                _tree_node("Empty"),
                _tree_node("Cram", review=1, total=1, filtered=True),
            ],
        )
        col = SimpleNamespace(sched=SimpleNamespace(deck_due_tree=lambda: tree))
        return SimpleNamespace(col=col)

    def test_flattens_with_full_paths_and_omits_empty(self) -> None:
        result = get_deck_due_counts(self._col(), {})
        names = [row["deck"] for row in result["decks"]]
        self.assertEqual(names, ["Spanish", "Spanish::Verbs", "Cram"])
        spanish = result["decks"][0]
        self.assertEqual(spanish["total_today"], 8)
        self.assertEqual(spanish["uncapped"]["new"], 20)
        self.assertTrue(result["decks"][2]["filtered"])

    def test_include_empty_and_prefix(self) -> None:
        result = get_deck_due_counts(self._col(), {"include_empty": True})
        self.assertIn("Empty", [row["deck"] for row in result["decks"]])
        result = get_deck_due_counts(self._col(), {"prefix": "Spanish::"})
        self.assertEqual([row["deck"] for row in result["decks"]], ["Spanish::Verbs"])


class FakeMsg(SimpleNamespace):
    def HasField(self, field: str) -> bool:  # noqa: N802 - protobuf casing
        return getattr(self, field, None) is not None


def _fake_stats(*, due_date: int, with_fsrs: bool, entries: int) -> FakeMsg:
    memory = FakeMsg(stability=284.404, difficulty=8.658)
    revlog = [
        FakeMsg(
            time=1_768_075_409 - i,
            review_kind=1,
            button_chosen=3,
            interval=25_920_000,
            ease=2650,
            taken_secs=4.0,
            memory_state=memory if with_fsrs else None,
        )
        for i in range(entries)
    ]
    return FakeMsg(
        card_id=555,
        note_id=556,
        deck="Spanish::Verbs",
        notetype="Basic",
        card_type="Forward",
        preset="Default",
        added=1_325_083_107,
        first_review=1_581_340_528,
        latest_review=1_768_075_409,
        due_date=due_date,
        due_position=7,
        interval=300,
        ease=2650,
        reviews=47,
        lapses=6,
        average_secs=4.1547,
        total_secs=195.27,
        memory_state=memory if with_fsrs else None,
        fsrs_retrievability=0.926094,
        desired_retention=0.9,
        revlog=revlog,
    )


class CardHistoryTest(unittest.TestCase):
    def _ctx(self, stats: FakeMsg) -> Any:
        col = SimpleNamespace(
            get_card=lambda cid: SimpleNamespace(id=cid),
            card_stats_data=lambda cid: stats,
        )
        return SimpleNamespace(col=col)

    def test_review_card_with_fsrs(self) -> None:
        result = get_card_history(
            self._ctx(_fake_stats(due_date=1_794_016_004, with_fsrs=True, entries=3)),
            {"card_id": 555, "limit": 2},
        )
        self.assertEqual(result["due"], {"date": 1_794_016_004})
        self.assertEqual(result["fsrs"]["stability_days"], 284.4)
        self.assertEqual(result["fsrs"]["retrievability"], 0.9261)
        self.assertEqual(result["revlog_total"], 3)
        self.assertEqual(result["revlog_shown"], 2)
        entry = result["revlog"][0]
        self.assertEqual(entry["kind"], "review")
        self.assertEqual(entry["button"], "good")
        self.assertEqual(entry["interval_days"], 300.0)
        self.assertEqual(entry["fsrs"]["difficulty"], 8.66)

    def test_new_card_without_fsrs(self) -> None:
        stats = _fake_stats(due_date=0, with_fsrs=False, entries=0)
        stats.ease = 0
        result = get_card_history(self._ctx(stats), {"card_id": 555})
        self.assertEqual(result["due"], {"new_queue_position": 7})
        self.assertIsNone(result["ease_factor"])
        self.assertNotIn("fsrs", result)
        self.assertEqual(result["revlog"], [])


class DuplicatesAndMediaTest(unittest.TestCase):
    def test_find_duplicates_strips_html_and_caps_groups(self) -> None:
        groups = [(f"<b>value {i}</b>", list(range(i, i + 3))) for i in range(60)]
        col = SimpleNamespace(find_dupes=lambda field, search: groups)
        result = find_duplicates(SimpleNamespace(col=col), {"field": "Front"})
        self.assertEqual(result["groups_total"], 60)
        self.assertEqual(result["notes_total"], 180)
        self.assertEqual(len(result["groups"]), 50)
        self.assertEqual(result["groups"][0]["value"], "value 0")
        self.assertIn("truncated", result)

    def test_check_media_caps_lists_and_warns(self) -> None:
        out = SimpleNamespace(
            missing=[f"m{i}.png" for i in range(150)],
            unused=["orphan.mp3"],
            have_trash=True,
            missing_media_notes=[11, 12],
            report="ignored",
        )
        col = SimpleNamespace(media=SimpleNamespace(check=lambda: out))
        result = check_media(SimpleNamespace(col=col), {})
        self.assertEqual(result["missing_count"], 150)
        self.assertEqual(len(result["missing"]), 100)
        self.assertEqual(result["unused"], ["orphan.mp3"])
        self.assertIn("truncated", result)
        self.assertIn("Never propose deleting", result["caveats"])
        self.assertEqual(result["notes_with_missing_media"], [11, 12])


class RegistryTest(unittest.TestCase):
    def test_statistics_tools_are_registered_reads(self) -> None:
        read_specs = {s.name for s in build_registry().specs(include_writes=False)}
        for name in (
            "get_study_stats",
            "get_deck_due_counts",
            "get_due_forecast",
            "get_card_history",
            "find_duplicates",
            "check_media",
        ):
            self.assertIn(name, read_specs)


if __name__ == "__main__":
    unittest.main()
