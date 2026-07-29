"""Defer a card to later in the session, without burying or rescheduling it.

The need (user, 2026-07-27): mid-review, ask the assistant about the card on
screen and get it out of the way while it thinks; when the answer lands,
finish the card in front of you and then see the deferred one NEXT. Anki's own
Bury is the wrong tool - it means *tomorrow*.

Two halves, deliberately different in kind:

* **Deprioritise** - "not now". Persisted in the card's `custom_data`, so the
  decision survives a sync and a restart. It changes NOTHING the scheduler
  reads: queue, due, type and interval are untouched, the card stays due, and
  a client without this add-on simply shows it as normal. It is advisory, and
  self-expiring - the marker records the day it was made and means nothing on
  any later day, so there is no cleanup pass to forget to run.

* **Show next** - session-only, in memory, never written. The user said they
  expect exactly this: the "not now" should survive, the "show it next" should
  not.

Neither writes scheduling state, so nothing here can corrupt a review history.

HOW THE ORDERING WORKS. The v3 reviewer's only source of cards is
`Reviewer._get_next_v3_card()`, which calls `col.sched.get_queued_cards()`
(documented "Idempotent", `fetch_limit` defaulting to 1) and hands
`V3CardInfo.from_queue(output)` the result. `from_queue` takes `cards[0]` AND
reads its `states`/`context` from that same entry, so REORDERING the queue
before it is built yields a valid card with its own correct scheduling states.
Verified against Anki 25.09: fetching 5, reordering in place, and re-fetching
returns the original order with every card's `queue` byte-identical - the
backend is not mutated, only what we hand the reviewer.

The cost: aqt exposes no hook here, so this wraps a private method. Every
failure path falls through to stock behaviour rather than breaking review.
"""

from __future__ import annotations

import json
from typing import Any, Callable

# `custom_data` keys are capped at 8 bytes by the backend (verified: a longer
# key raises InvalidInput). Distinctive enough not to collide with FSRS's
# own short keys (`s`, `d`, ...).
MARKER_KEY = "cwycDfr"

# How deep to look for a card that is not deferred. A horizon, not infinity:
# if everything in the window is deferred there is nothing to swap to, and the
# honest move is to show the deferred card rather than loop or blank the
# reviewer.
FETCH_LIMIT = 20


class DeferralManager:
    """Session pin + the persisted marker, and the reviewer wrap that uses
    both. aqt-free apart from `install()`, so the policy is unit-testable."""

    def __init__(self, get_col: Callable[[], Any]) -> None:
        self._get_col = get_col
        # Session-only: the card to show NEXT. Never written to the collection.
        self._pinned: int | None = None
        self._original: Callable[[Any], None] | None = None

    # ---- the persisted "not now" marker ----

    def defer(self, card_id: int) -> None:
        """Mark a card as not-now for today. Touches no scheduling field."""
        col = self._get_col()
        card = col.get_card(int(card_id))
        data = _load(card)
        data[MARKER_KEY] = int(col.sched.today)
        card.custom_data = json.dumps(data, separators=(",", ":"))
        col.update_card(card)
        if self._pinned == int(card_id):
            self._pinned = None

    def undefer(self, card_id: int) -> None:
        """Drop the marker. Safe on a card that was never deferred."""
        col = self._get_col()
        card = col.get_card(int(card_id))
        data = _load(card)
        if data.pop(MARKER_KEY, None) is None:
            return
        card.custom_data = json.dumps(data, separators=(",", ":")) if data else ""
        col.update_card(card)

    def is_deferred(self, card: Any) -> bool:
        """True only for a marker made TODAY - it expires by meaning, not by a
        cleanup job, so a stale one from last week is simply ignored."""
        day = _load(card).get(MARKER_KEY)
        if day is None:
            return False
        try:
            return int(day) == int(self._get_col().sched.today)
        except Exception:
            return False

    def deferred_card_ids(self) -> list[int]:
        col = self._get_col()
        out: list[int] = []
        for card_id in col.find_cards("is:due"):
            try:
                if self.is_deferred(col.get_card(card_id)):
                    out.append(int(card_id))
            except Exception:
                continue
        return out

    # ---- the session-only "show this next" ----

    def show_next(self, card_id: int) -> None:
        """Bring a deferred card back as the next card. Clears the marker too,
        so it does not get skipped the moment it is served."""
        self.undefer(card_id)
        self._pinned = int(card_id)

    @property
    def pinned(self) -> int | None:
        return self._pinned

    def clear_session(self) -> None:
        self._pinned = None

    # ---- ordering policy (pure: a queue in, a queue order out) ----

    def choose(self, entries: list[Any], is_deferred: Callable[[Any], bool]) -> int:
        """Index of the entry to show. The pinned card wins; otherwise the
        first that is not deferred; otherwise 0 - if every card in the window
        is deferred there is nothing to swap to, and showing the deferred card
        beats showing nothing."""
        if self._pinned is not None:
            for index, entry in enumerate(entries):
                if int(entry.card.id) == self._pinned:
                    return index
        for index, entry in enumerate(entries):
            if not is_deferred(entry):
                return index
        return 0

    # ---- the reviewer wrap ----

    def install(self, reviewer_cls: Any) -> None:
        """Wrap Reviewer._get_next_v3_card. No hook exists for this path, so
        the private method is the only seam; every failure falls through to
        the original so a broken assumption degrades to stock Anki."""
        if self._original is not None:
            return
        original = reviewer_cls._get_next_v3_card
        self._original = original
        manager = self

        def patched(reviewer: Any) -> None:
            try:
                if not manager._reorder(reviewer):
                    original(reviewer)
            except Exception:
                original(reviewer)

        reviewer_cls._get_next_v3_card = patched

    def uninstall(self, reviewer_cls: Any) -> None:
        if self._original is not None:
            reviewer_cls._get_next_v3_card = self._original
            self._original = None

    def _reorder(self, reviewer: Any) -> bool:
        """Serve a chosen card. Returns False to mean "not our business", so
        the caller runs stock Anki."""
        from anki.cards import Card
        from anki.scheduler.v3 import Scheduler as V3Scheduler
        from aqt.reviewer import V3CardInfo

        col = self._get_col()
        if not isinstance(col.sched, V3Scheduler):
            return False  # v1/v2 have no queue to reorder
        if self._pinned is None and not self._any_marker_today(col):
            return False  # nothing deferred: do not touch the normal path

        output = col.sched.get_queued_cards(fetch_limit=FETCH_LIMIT)
        entries = list(output.cards)
        if len(entries) < 2:
            # One card (or none) left: nothing to swap to, and a pin cannot be
            # honoured either. Stock behaviour, so the last card still shows.
            self._pinned = None if not entries else self._pinned
            return False

        index = self.choose(entries, lambda e: self.is_deferred(_card_of(col, e)))
        if index == 0 and self._pinned is None:
            return False  # the natural top card is fine; leave the queue alone
        if self._pinned is not None and int(entries[index].card.id) == self._pinned:
            self._pinned = None  # a pin is spent the moment it is served

        reordered = [entries[index]] + entries[:index] + entries[index + 1 :]
        del output.cards[:]
        output.cards.extend(reordered)
        reviewer._v3 = V3CardInfo.from_queue(output)
        reviewer.card = Card(col, backend_card=reviewer._v3.top_card().card)
        reviewer.card.start_timer()
        return True

    def _any_marker_today(self, col: Any) -> bool:
        """Cheap gate so an ordinary review session never pays for this."""
        try:
            return bool(col.find_cards(f'prop:cdn:{MARKER_KEY}={col.sched.today}'))
        except Exception:
            # The prop:cdn: search needs a recent Anki; fall back to trusting
            # the session pin only.
            return False


def _load(card: Any) -> dict[str, Any]:
    raw = getattr(card, "custom_data", "") or ""
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _card_of(col: Any, entry: Any) -> Any:
    return col.get_card(int(entry.card.id))
