"""FSRS compute tools (#13) against a REAL, throwaway Anki collection.

Same reasoning as the note-type lane: these tools are thin wrappers over a
Rust backend whose failure modes are not guessable, and a fake would only
encode what we already believed. Two probed facts drive the whole design and
are pinned here:

  * a `SimulateFsrsReviewRequest` left at its protobuf defaults **panics** the
    backend (`min > max, or either was NaN`), and a panic poisons the backend
    mutex and kills the collection for the whole process (SAFETY.md hazard 19);
  * `compute_fsrs_params` signals "not enough review history" by returning an
    **empty parameter list**, not by raising - while its sibling
    `evaluate_params` raises `InvalidInput` for the same condition.

Skipped when `anki` is missing. To run locally:

    "$HOME/Library/Application Support/AnkiProgramFiles/.venv/bin/python" \
        -m unittest tests.test_fsrs_real_anki -v
"""

from __future__ import annotations

import os
import random
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:  # pragma: no cover - environment probe
    from anki.collection import Collection

    HAVE_ANKI = True
except Exception:  # pragma: no cover
    Collection = None  # type: ignore[assignment,misc]
    HAVE_ANKI = False

from chat_with_your_cards.tools import fsrs as fsrs_tools  # noqa: E402
from chat_with_your_cards.tools.registry import ToolSpec  # noqa: E402


class _Ctx:
    """The slice of ToolContext these tools touch."""

    def __init__(self, col: Any) -> None:
        self.col = col
        self.config: dict[str, Any] = {}

    def push_ui(self, payload: dict[str, Any]) -> None:  # pragma: no cover
        pass


@unittest.skipUnless(HAVE_ANKI, "the anki library is not installed in this env")
class RealAnkiFsrsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="cwyc-fsrs-real-")
        self.addCleanup(self._tmp.cleanup)
        self.col = Collection(os.path.join(self._tmp.name, "throwaway.anki2"))
        self.addCleanup(self.col.close)
        self.ctx = _Ctx(self.col)

    def _seed_history(
        self, cards: int = 60, reviews: int = 10, first_type: int = 0
    ) -> int:
        """A synthetic revlog. Direct SQL is fine for a FIXTURE - it is never
        how the add-on writes.

        `first_type=0` makes each card's first entry a LEARNING review, which
        is what FSRS anchors an item on. With `first_type=1` the same rows
        yield zero usable items - see test_review_rows_without_learning_entries.
        """
        random.seed(11)
        model = self.col.models.by_name("Basic")
        day_ms = 86_400_000
        now_ms = int(time.time() * 1000)
        for i in range(cards):
            note = self.col.new_note(model)
            note["Front"] = f"q{i}"
            note["Back"] = f"a{i}"
            self.col.add_note(note, 1)
            cid = note.cards()[0].id
            # Per-card millisecond offset: revlog's primary key IS the
            # timestamp, so identical stamps across cards silently collide and
            # `insert or ignore` drops them - which is how the first version of
            # this fixture quietly produced a third of the rows it claimed.
            ivl, stamp = 1, now_ms - day_ms * 900 + i * 97
            for _index in range(reviews):
                ease = random.choices([1, 2, 3, 4], weights=[12, 20, 55, 13])[0]
                last = ivl
                # Capped growth keeps every review inside the window; an
                # uncapped 2.4x ran cards past `now` after ~6 reviews.
                ivl = max(1, min(int(ivl * (0.5 if ease == 1 else 2.0)) or 1, 21))
                stamp += day_ms * last
                if stamp > now_ms:
                    break
                self.col.db.execute(
                    "insert or ignore into revlog values (?,?,?,?,?,?,?,?,?)",
                    stamp, cid, -1, ease, ivl, last, 2500, 3000,
                    first_type if _index == 0 else 1,
                )
            card = self.col.get_card(cid)
            card.type, card.queue, card.ivl = 2, 2, ivl
            card.factor, card.reps, card.due = 2500, reviews, 1
            self.col.update_card(card)
        return int(self.col.db.scalar("select count() from revlog"))

    # ---- the two probed failure modes ----------------------------------

    def test_insufficient_history_is_a_clean_error_not_empty_params(self) -> None:
        """The trap: the backend returns [] rather than raising. Handing that
        back would invite proposing `params: []`, silently resetting a
        preset's scheduling while reporting success."""
        self._seed_history(cards=3, reviews=3)
        with self.assertRaises(ValueError) as ctx:
            fsrs_tools.fsrs_optimize(self.ctx, {})
        message = str(ctx.exception)
        self.assertIn("not enough to optimize", message)
        self.assertIn("Nothing was changed", message)

    def test_review_rows_without_learning_entries_yield_no_items(self) -> None:
        """Measured, not assumed: FSRS counts ITEMS (a card's history anchored
        at its first LEARNING review), not revlog rows. The same 3080 rows gave
        0 items with review-type first entries and 2860 with learning-type
        ones - so "you have thousands of reviews" and "FSRS can use them" are
        different claims, and the error has to say which one failed."""
        rows = self._seed_history(cards=220, reviews=14, first_type=1)
        self.assertGreater(rows, 400)
        with self.assertRaises(ValueError) as ctx:
            fsrs_tools.fsrs_optimize(self.ctx, {})
        message = str(ctx.exception)
        self.assertIn("no usable review sequences", message)
        self.assertIn("LEARNING review", message)

    def test_evaluate_insufficient_history_gets_the_same_wording(self) -> None:
        """Its sibling RAISES for the same condition. Two backend behaviours,
        one user-facing meaning."""
        self._seed_history(cards=2, reviews=2)
        with self.assertRaises(ValueError) as ctx:
            fsrs_tools.fsrs_evaluate(self.ctx, {})
        self.assertIn("not enough review history", str(ctx.exception))

    def test_simulate_never_reaches_the_backend_with_an_empty_request(self) -> None:
        """The panic guard. With no params anywhere, we must refuse in Python
        rather than send the zero-vector that panicked Rust and would kill the
        collection for the whole process."""
        with self.assertRaises(ValueError) as ctx:
            fsrs_tools.fsrs_simulate(self.ctx, {})
        self.assertIn("no FSRS parameters available", str(ctx.exception))

    def test_out_of_range_values_are_refused_before_the_backend(self) -> None:
        params = [0.4] * 19
        for args, needle in (
            ({"params": params, "desired_retention": 0.2}, "desired_retention"),
            ({"params": params, "desired_retention": 1.5}, "desired_retention"),
            ({"params": params, "days_to_simulate": 0}, "days_to_simulate"),
            ({"params": params, "days_to_simulate": 99_999}, "days_to_simulate"),
            ({"params": params, "max_interval": 0}, "max_interval"),
            ({"params": params, "review_limit": -1}, "review_limit"),
        ):
            with self.assertRaises(ValueError, msg=f"{args} should be refused") as ctx:
                fsrs_tools.fsrs_simulate(self.ctx, args)
            self.assertIn(needle, str(ctx.exception))

    def test_malformed_params_are_refused(self) -> None:
        for raw, needle in (
            ([0.1, 0.2], "FSRS expects one of"),
            ([float("nan")] * 19, "non-finite"),
            (["x"] * 19, "list of numbers"),
        ):
            with self.assertRaises(ValueError) as ctx:
                fsrs_tools.fsrs_simulate(self.ctx, {"params": raw})
            self.assertIn(needle, str(ctx.exception))

    def test_easy_days_must_be_seven_values(self) -> None:
        params = [0.4] * 19
        with self.assertRaises(ValueError) as ctx:
            fsrs_tools.fsrs_simulate(
                self.ctx, {"params": params, "easy_days_percentages": [1.0, 1.0]}
            )
        self.assertIn("exactly 7 values", str(ctx.exception))

    def test_invalid_search_is_our_error(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            fsrs_tools.fsrs_evaluate(self.ctx, {"search": "deck:("})
        self.assertIn("invalid search", str(ctx.exception))

    # ---- the happy paths -----------------------------------------------

    def test_simulate_returns_a_readable_workload_projection(self) -> None:
        result = fsrs_tools.fsrs_simulate(
            self.ctx,
            {"params": [0.4] * 19, "days_to_simulate": 28, "new_limit": 10,
             "review_limit": 100},
        )
        self.assertEqual(28, result["days_simulated"])
        self.assertEqual(4, len(result["weekly_review_counts"]))
        self.assertEqual(4, len(result["weekly_minutes"]))
        self.assertGreaterEqual(result["total_reviews"], 0)
        self.assertGreaterEqual(result["peak_daily_reviews"], 0)
        # Weekly series are per-day means, so they live on the daily scale.
        self.assertLessEqual(
            max(result["weekly_review_counts"]), result["peak_daily_reviews"]
        )

    def test_optimal_retention_lands_in_range(self) -> None:
        result = fsrs_tools.fsrs_optimal_retention(
            self.ctx, {"params": [0.4] * 19, "days_to_simulate": 60}
        )
        self.assertTrue(0.70 <= result["optimal_retention"] <= 0.99, result)
        self.assertIn("not applied", result["next_step"].lower())

    def test_optimize_reports_but_never_applies(self) -> None:
        """Enough history to actually optimize; the result must stay advisory."""
        rows = self._seed_history(cards=220, reviews=14)
        self.assertGreater(rows, 400, "fixture should clear FSRS's data floor")
        before = [
            float(p)
            for p in (
                self.col.decks.config_dict_for_deck_id(1).get("fsrsParams6") or []
            )
        ]
        result = fsrs_tools.fsrs_optimize(self.ctx, {})
        self.assertIn(len(result["params"]), fsrs_tools.VALID_PARAM_COUNTS)
        self.assertFalse(result["applied"])
        self.assertIn("set_deck_options", result["next_step"])
        after = [
            float(p)
            for p in (
                self.col.decks.config_dict_for_deck_id(1).get("fsrsParams6") or []
            )
        ]
        self.assertEqual(before, after, "optimize must not touch the preset")

    def test_evaluate_reports_both_metrics(self) -> None:
        self._seed_history(cards=220, reviews=14)
        result = fsrs_tools.fsrs_evaluate(self.ctx, {})
        self.assertIsInstance(result["log_loss"], float)
        self.assertIsInstance(result["rmse_bins"], float)
        self.assertIn("Lower is better", result["reading"])

    # ---- registry wiring -------------------------------------------------

    def test_every_fsrs_tool_is_long_running_and_read_only(self) -> None:
        """These are pure compute: they must never be marshalled onto the Qt
        main thread (they freeze Anki and blow the 15s tool timeout), and none
        of them writes."""
        from chat_with_your_cards.tools.registry import ToolRegistry

        registry = ToolRegistry()
        fsrs_tools.register_fsrs_tools(registry)
        specs = {spec.name: spec for spec in registry.specs()}
        self.assertEqual(4, len(specs))
        for name, spec in specs.items():
            self.assertIsInstance(spec, ToolSpec)
            self.assertTrue(spec.long_running, f"{name} must run off the main thread")
            self.assertFalse(spec.writes, f"{name} must be read-only")
            self.assertNotEqual("Working…", spec.progress_label, f"{name} needs a label")


@unittest.skipUnless(HAVE_ANKI, "the anki library is not installed in this env")
class RealAnkiPreferenceTests(unittest.TestCase):
    """Preferences (#14) against a real collection: the protobuf round-trip is
    the whole risk, and a fake would not exercise it."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="cwyc-prefs-real-")
        self.addCleanup(self._tmp.cleanup)
        self.col = Collection(os.path.join(self._tmp.name, "throwaway.anki2"))
        self.addCleanup(self.col.close)
        self.pushed: list[dict[str, Any]] = []
        from chat_with_your_cards.proposals import ProposalManager

        self.manager = ProposalManager(
            get_col=lambda: self.col,
            push=self.pushed.append,
            config={},
            checkpoint=lambda reason, critical: True,
        )

    def _accept(self, result: dict[str, Any]) -> Any:
        self.manager.accept({"id": result["proposal_id"]})
        return self.manager._proposals[result["proposal_id"]]

    def test_read_reports_the_accent_setting_in_plain_words(self) -> None:
        from chat_with_your_cards.tools.maintenance import get_preferences

        class Ctx:
            col = self.col
            config: dict[str, Any] = {}

            def push_ui(self, payload: dict[str, Any]) -> None:
                pass

        result = get_preferences(Ctx(), {})
        self.assertIn("rollover", result["scheduling"])
        self.assertIn("daily", result["backups"])
        joined = " ".join(result["notes"])
        self.assertIn("accents are SIGNIFICANT", joined)

    def test_change_applies_and_reverts(self) -> None:
        before = self.col.get_preferences().scheduling.rollover
        result = self.manager.submit_set_preferences(
            {"preferences": {"scheduling.rollover": (before + 3) % 24}}
        )
        proposal = self._accept(result)
        self.assertEqual(
            (before + 3) % 24, self.col.get_preferences().scheduling.rollover
        )
        self.manager.revert({"id": proposal.id})
        self.assertEqual(before, self.col.get_preferences().scheduling.rollover)

    def test_rollover_change_warns_about_due_shifting(self) -> None:
        self.manager.submit_set_preferences({"preferences": {"scheduling.rollover": 9}})
        warnings = " ".join(self.pushed[-1]["proposal"]["warnings"])
        self.assertIn("shifts what counts as due today", warnings)

    def test_backup_retention_is_refused_with_the_reason(self) -> None:
        """It is the safety net every destructive proposal here promises."""
        from chat_with_your_cards.proposals import ProposalError

        with self.assertRaises(ProposalError) as ctx:
            self.manager.submit_set_preferences({"preferences": {"backups.daily": 2}})
        self.assertIn("weaken that guarantee", str(ctx.exception))

    def test_out_of_range_and_unknown_paths_are_refused(self) -> None:
        from chat_with_your_cards.proposals import ProposalError

        for prefs, needle in (
            ({"scheduling.rollover": 99}, "between 0 and 23"),
            ({"scheduling.rollover": "four"}, "whole number"),
            ({"editing.ignore_accents_in_search": "yes"}, "true or false"),
            ({"nope.nope": 1}, "non-settable"),
        ):
            with self.assertRaises(ProposalError) as ctx:
                self.manager.submit_set_preferences({"preferences": prefs})
            self.assertIn(needle, str(ctx.exception))

    def test_noop_change_is_refused(self) -> None:
        from chat_with_your_cards.proposals import ProposalError

        current = self.col.get_preferences().scheduling.rollover
        with self.assertRaises(ProposalError) as ctx:
            self.manager.submit_set_preferences(
                {"preferences": {"scheduling.rollover": current}}
            )
        self.assertIn("already match", str(ctx.exception))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
