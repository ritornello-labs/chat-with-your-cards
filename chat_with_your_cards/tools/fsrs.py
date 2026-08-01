"""FSRS compute tools (#13): optimize, evaluate, simulate, optimal retention.

We could already *write* FSRS `params` (via `set_deck_options`) but never
*compute* them, so the one number that actually matters was the one number the
agent had to ask the user to go and fetch by hand.

Two things make this family unlike every other read tool, both found by probing
the real backend on 25.x before writing any of it:

1. **An under-specified request panics Rust.** `simulate_fsrs_review` with the
   protobuf's own defaults (`desired_retention=0.0`, `max_interval=0`) panicked
   with `min > max, or either was NaN`. A panic while holding the backend mutex
   **poisons it and kills the collection for the whole process** (SAFETY.md
   hazard 19) - an unrecoverable, restart-Anki failure caused by one bad float.
   So every field is populated and every value range-checked *here*, in Python,
   before the call crosses into Rust. `_number`'s range check is not
   politeness; it is the safety barrier.

2. **`compute_fsrs_params` reports insufficient data by returning an EMPTY
   parameter list, not by raising.** Handing that straight back would invite
   the agent to propose `params: []` - silently resetting the preset's
   scheduling to defaults while reporting success. Empty is converted to a
   loud error here. (`evaluate_params`, by contrast, raises a clean
   `InvalidInput: Insufficient review history` - the two sibling calls disagree
   about how to say the same thing.)

Everything here is READ-ONLY: it computes and reports. Applying the result is
`set_deck_options`, which goes through the proposal flow like every other
write - so the user reviews the numbers before their scheduling changes.
"""

from __future__ import annotations

from typing import Any

from .registry import ToolContext, ToolRegistry, ToolSpec

# Ranges accepted before anything reaches the backend. Sources: Anki's own deck
# options UI limits, and the FSRS docs. Deliberately conservative - the cost of
# rejecting a legal-but-odd value is an error message; the cost of passing an
# illegal one is a dead collection.
LIMITS: dict[str, tuple[float, float]] = {
    "desired_retention": (0.70, 0.99),
    "historical_retention": (0.50, 0.99),
    "days_to_simulate": (1, 3650),
    "new_limit": (0, 9999),
    "review_limit": (0, 9999),
    "max_interval": (1, 36500),
    "deck_size": (1, 100_000),
    "learning_step_count": (0, 10),
    "relearning_step_count": (0, 10),
    "suspend_after_lapse_count": (0, 100),
}
# FSRS-5 has 19 parameters, FSRS-6 has 21; older presets may still hold 17.
VALID_PARAM_COUNTS = (17, 19, 21)
MAX_SIMULATE_DAYS = 3650


def _number(args: dict[str, Any], key: str, default: float) -> float:
    raw = args.get(key)
    if raw is None:
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise ValueError(f"{key} must be a number, got {raw!r}") from None
    low, high = LIMITS[key]
    if not low <= value <= high:
        raise ValueError(f"{key} must be between {low} and {high}, got {value}")
    return value


def _params(raw: Any, label: str = "params") -> list[float]:
    if raw is None:
        return []
    try:
        values = [float(p) for p in raw]
    except (TypeError, ValueError):
        raise ValueError(f"{label} must be a list of numbers") from None
    if values and len(values) not in VALID_PARAM_COUNTS:
        raise ValueError(
            f"{label} has {len(values)} entries; FSRS expects one of "
            f"{list(VALID_PARAM_COUNTS)}"
        )
    for value in values:
        if value != value or value in (float("inf"), float("-inf")):  # NaN/inf
            raise ValueError(f"{label} contains a non-finite value")
    return values


def _validated_search(ctx: ToolContext, args: dict[str, Any]) -> str:
    """An invalid search must be OUR error, not a backend surprise. Also the
    agent's only lever on runtime: less history, less compute."""
    search = str(args.get("search") or "").strip()
    if not search:
        return ""
    try:
        ctx.col.find_cards(search)
    except Exception as exc:
        raise ValueError(f"invalid search {search!r}: {exc}") from None
    return search


def _preset_params(ctx: ToolContext, deck: str | None) -> list[float]:
    """The preset's current FSRS params, so evaluate/simulate default to what
    the user is actually scheduling with rather than to stock values."""
    if not deck:
        return []
    try:
        did = ctx.col.decks.id_for_name(deck)
        if did is None:
            return []
        conf = ctx.col.decks.config_dict_for_deck_id(did)
        return [float(p) for p in (conf.get("fsrsParams6") or conf.get("fsrsWeights") or [])]
    except Exception:
        return []


def _require_fsrs_items(response: Any) -> list[float]:
    """See this module's docstring: an empty parameter list is how the backend
    says "not enough review history", and passing it on would let the agent
    propose wiping a preset's scheduling.

    The distinction in the message is load-bearing and was measured, not
    guessed: FSRS counts *items* (a card's review sequence anchored at its
    first LEARNING review), not revlog rows. A fixture with 3080 review-type
    rows and no learning entries yielded `fsrs_items: 0` and zero params;
    making the first review of each card type 0 yielded 2860 items and 21
    params from the same 3080 rows. So "you have plenty of reviews" and "FSRS
    can use them" are different claims, and a user whose history was imported
    without learning entries deserves to be told which one failed.
    """
    params = [float(p) for p in response.params]
    if params:
        return params
    items = int(getattr(response, "fsrs_items", 0))
    if items == 0:
        raise ValueError(
            "FSRS found no usable review sequences in scope. It builds each "
            "item from a card's history starting at its first LEARNING review, "
            "so reviews imported or rescheduled without a learning entry are "
            "invisible to it - a collection can hold thousands of review rows "
            "and still yield zero items. Try a wider search, or check whether "
            "this history came from an import. Nothing was changed."
        )
    raise ValueError(
        f"only {items} usable review(s) in scope - not enough to optimize "
        "FSRS parameters (it wants roughly 400+). Widen the search, or drop "
        "it to cover the whole collection. Nothing was changed."
    )


def fsrs_optimize(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    search = _validated_search(ctx, args)
    current = _params(args.get("current_params"), "current_params")
    relearning_steps = int(_number(args, "relearning_step_count", 1))
    backend = ctx.col._backend
    response = backend.compute_fsrs_params(
        search=search,
        current_params=current,
        ignore_revlogs_before_ms=0,
        num_of_relearning_steps=relearning_steps,
        health_check=True,
    )
    params = _require_fsrs_items(response)
    health: bool | None = None
    try:
        if response.HasField("health_check_passed"):
            health = bool(response.health_check_passed)
    except Exception:
        health = None
    result: dict[str, Any] = {
        "params": [round(p, 4) for p in params],
        "reviews_used": int(getattr(response, "fsrs_items", 0)),
        "search": search or "(whole collection)",
        "applied": False,
        "next_step": (
            "These are computed, NOT applied. To use them, propose "
            "set_deck_options with options {\"fsrsParams6\": [...]} on the "
            "deck's preset - the user reviews that like any other change."
        ),
    }
    if health is not None:
        result["health_check_passed"] = health
        if not health:
            result["health_warning"] = (
                "FSRS's own health check did not pass: the review history is "
                "unusual enough that these parameters may not beat the current "
                "ones. Compare with fsrs_evaluate before proposing them."
            )
    return result


def fsrs_evaluate(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    search = _validated_search(ctx, args)
    relearning_steps = int(_number(args, "relearning_step_count", 1))
    try:
        response = ctx.col._backend.evaluate_params(
            search=search,
            ignore_revlogs_before_ms=0,
            num_of_relearning_steps=relearning_steps,
        )
    except Exception as exc:
        # The sibling of the empty-list case: this one raises. Same user-facing
        # meaning, so give it the same user-facing wording.
        if "Insufficient" in str(exc) or "insufficient" in str(exc):
            raise ValueError(
                "not enough review history to evaluate FSRS here; widen the "
                "search (or drop it to cover the whole collection)"
            ) from None
        raise
    return {
        "log_loss": round(float(response.log_loss), 4),
        "rmse_bins": round(float(response.rmse_bins), 4),
        "search": search or "(whole collection)",
        "reading": (
            "Lower is better for both. RMSE(bins) is the calibration error - "
            "how far predicted recall sits from observed recall; under ~0.05 is "
            "good. Compare the same metric before and after optimizing; the "
            "absolute number alone says little."
        ),
    }


def _simulate_request(ctx: ToolContext, args: dict[str, Any]) -> Any:
    """Build a FULLY populated request. Every field is set - see the module
    docstring for what happens when one is left at its protobuf default."""
    from anki import scheduler_pb2

    deck = str(args.get("deck") or "").strip() or None
    params = _params(args.get("params")) or _preset_params(ctx, deck)
    if not params:
        # No preset params to fall back on: optimize first rather than
        # simulating against a zero-vector (the shape that panicked Rust).
        raise ValueError(
            "no FSRS parameters available to simulate with: pass `params`, or "
            "pass a `deck` whose preset already has them, or run fsrs_optimize "
            "first"
        )
    easy_days = args.get("easy_days_percentages")
    if easy_days is None:
        easy_days = [1.0] * 7
    try:
        easy_days = [float(v) for v in easy_days]
    except (TypeError, ValueError):
        raise ValueError("easy_days_percentages must be 7 numbers") from None
    if len(easy_days) != 7 or any(not 0.0 <= v <= 1.0 for v in easy_days):
        raise ValueError(
            "easy_days_percentages must be exactly 7 values between 0 and 1 "
            "(Monday..Sunday), where 1 means a normal day"
        )

    request = scheduler_pb2.SimulateFsrsReviewRequest()
    request.params.extend(params)
    request.desired_retention = _number(args, "desired_retention", 0.9)
    request.deck_size = int(_number(args, "deck_size", 10_000))
    request.days_to_simulate = int(_number(args, "days_to_simulate", 365))
    request.new_limit = int(_number(args, "new_limit", 20))
    request.review_limit = int(_number(args, "review_limit", 200))
    request.max_interval = int(_number(args, "max_interval", 36500))
    request.search = _validated_search(ctx, args)
    request.new_cards_ignore_review_limit = bool(
        args.get("new_cards_ignore_review_limit", False)
    )
    request.easy_days_percentages.extend(easy_days)
    request.review_order = 0
    request.suspend_after_lapse_count = int(
        _number(args, "suspend_after_lapse_count", 8)
    )
    request.historical_retention = _number(args, "historical_retention", 0.9)
    request.learning_step_count = int(_number(args, "learning_step_count", 2))
    request.relearning_step_count = int(_number(args, "relearning_step_count", 1))
    return request


def fsrs_simulate(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    request = _simulate_request(ctx, args)
    response = ctx.col._backend.simulate_fsrs_review(request)
    reviews = [int(v) for v in response.daily_review_count]
    new_cards = [int(v) for v in response.daily_new_count]
    seconds = [float(v) for v in response.daily_time_cost]
    memorized = [float(v) for v in response.accumulated_knowledge_acquisition]
    days = len(reviews)

    def weekly(values: list[float]) -> list[float]:
        """Per-week means. A 365-entry array is noise in a chat reply; the
        shape of the workload is the answer the user actually wants."""
        out = []
        for start in range(0, len(values), 7):
            chunk = values[start : start + 7]
            out.append(round(sum(chunk) / len(chunk), 1))
        return out

    return {
        "days_simulated": days,
        "desired_retention": round(request.desired_retention, 3),
        "total_reviews": sum(reviews),
        "total_new_cards": sum(new_cards),
        "total_hours": round(sum(seconds) / 3600, 1),
        "peak_daily_reviews": max(reviews) if reviews else 0,
        "average_daily_reviews": round(sum(reviews) / days, 1) if days else 0,
        "average_daily_minutes": round(sum(seconds) / 60 / days, 1) if days else 0,
        "memorized_at_end": round(memorized[-1], 1) if memorized else 0,
        "weekly_review_counts": weekly([float(v) for v in reviews]),
        "weekly_minutes": [round(v / 60, 1) for v in weekly(seconds)],
        "reading": (
            "Weekly series are per-day averages within each week, so they are "
            "directly comparable to the daily numbers. Simulations assume the "
            "limits and retention passed here hold for the whole period."
        ),
    }


def fsrs_optimal_retention(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    request = _simulate_request(ctx, args)
    value = float(ctx.col._backend.compute_optimal_retention(request))
    return {
        "optimal_retention": round(value, 3),
        "days_simulated": request.days_to_simulate,
        "reading": (
            "The desired-retention setting that maximises material learned for "
            "the study time implied by these limits. It is a suggestion from a "
            "simulation, not a measurement: it moves with the limits and deck "
            "size passed in. Anki's own guidance is to leave retention alone "
            "unless there is a reason - propose it only if the user asks."
        ),
        "next_step": (
            "Not applied. To use it, propose set_deck_options with "
            "{\"desiredRetention\": <value>} on the deck's preset."
        ),
    }


_SEARCH = {
    "type": "string",
    "description": "Anki search limiting which cards' review history is used. "
    "Omit for the whole collection. This is also the runtime lever: less "
    "history, less compute.",
}
_DECK = {
    "type": "string",
    "description": "Deck whose options preset supplies the current FSRS "
    "parameters when `params` is omitted.",
}


def register_fsrs_tools(registry: ToolRegistry) -> None:
    registry.register(
        ToolSpec(
            "fsrs_optimize",
            "Compute optimal FSRS parameters from review history. Returns the "
            "numbers; does NOT apply them - propose set_deck_options to do "
            "that, so the user reviews the change. Needs roughly 400+ reviews "
            "in scope. Takes seconds to minutes on a large collection.",
            {
                "type": "object",
                "properties": {
                    "search": _SEARCH,
                    "current_params": {
                        "type": "array",
                        "items": {"type": "number"},
                        "description": "The preset's existing parameters, if "
                        "any, so the optimizer can start from them.",
                    },
                    "relearning_step_count": {
                        "type": "integer",
                        "description": "Relearning steps in the preset (default 1)",
                    },
                },
            },
            fsrs_optimize,
            long_running=True,
            progress_label="Optimizing FSRS parameters…",
        )
    )
    registry.register(
        ToolSpec(
            "fsrs_evaluate",
            "Measure how well the CURRENT FSRS parameters predict the user's "
            "actual review history (log loss and RMSE). Use it before and "
            "after fsrs_optimize to show whether new parameters are actually "
            "an improvement.",
            {
                "type": "object",
                "properties": {
                    "search": _SEARCH,
                    "relearning_step_count": {
                        "type": "integer",
                        "description": "Relearning steps in the preset (default 1)",
                    },
                },
            },
            fsrs_evaluate,
            long_running=True,
            progress_label="Evaluating FSRS parameters…",
        )
    )
    registry.register(
        ToolSpec(
            "fsrs_simulate",
            "Project future workload: reviews per day, time per day, and how "
            "much stays memorized, for a given set of limits and desired "
            "retention. Answers 'what happens to my workload if I raise new "
            "cards to 30?' without the user having to live through it.",
            {
                "type": "object",
                "properties": {
                    "search": _SEARCH,
                    "deck": _DECK,
                    "params": {
                        "type": "array",
                        "items": {"type": "number"},
                        "description": "FSRS parameters to simulate with; "
                        "defaults to the deck preset's.",
                    },
                    "desired_retention": {"type": "number", "description": "0.70-0.99 (default 0.9)"},
                    "days_to_simulate": {"type": "integer", "description": "1-3650 (default 365)"},
                    "new_limit": {"type": "integer", "description": "New cards/day (default 20)"},
                    "review_limit": {"type": "integer", "description": "Reviews/day (default 200)"},
                    "deck_size": {"type": "integer", "description": "Cards available to learn (default 10000)"},
                    "max_interval": {"type": "integer", "description": "Days (default 36500)"},
                    "historical_retention": {"type": "number", "description": "0.50-0.99 (default 0.9)"},
                    "learning_step_count": {"type": "integer", "description": "Default 2"},
                    "relearning_step_count": {"type": "integer", "description": "Default 1"},
                    "suspend_after_lapse_count": {"type": "integer", "description": "Default 8"},
                    "new_cards_ignore_review_limit": {"type": "boolean"},
                    "easy_days_percentages": {
                        "type": "array",
                        "items": {"type": "number"},
                        "description": "Exactly 7 values 0-1, Monday..Sunday; "
                        "1 = a normal study day.",
                    },
                },
            },
            fsrs_simulate,
            long_running=True,
            progress_label="Simulating future workload…",
        )
    )
    registry.register(
        ToolSpec(
            "fsrs_optimal_retention",
            "Compute the desired-retention setting that maximises material "
            "learned for the study time these limits imply. A simulation "
            "result, not a measurement - Anki's guidance is to leave retention "
            "alone without a reason, so report it rather than pushing it.",
            {
                "type": "object",
                "properties": {
                    "search": _SEARCH,
                    "deck": _DECK,
                    "params": {"type": "array", "items": {"type": "number"}},
                    "days_to_simulate": {"type": "integer", "description": "1-3650 (default 365)"},
                    "new_limit": {"type": "integer", "description": "New cards/day (default 20)"},
                    "review_limit": {"type": "integer", "description": "Reviews/day (default 200)"},
                    "deck_size": {"type": "integer", "description": "Cards available to learn (default 10000)"},
                    "max_interval": {"type": "integer"},
                    "historical_retention": {"type": "number"},
                    "learning_step_count": {"type": "integer"},
                    "relearning_step_count": {"type": "integer"},
                    "suspend_after_lapse_count": {"type": "integer"},
                },
            },
            fsrs_optimal_retention,
            long_running=True,
            progress_label="Computing optimal retention…",
        )
    )
