"""Set a card aside for later in the session ("defer", task #32).

REDESIGNED 2026-07-29 after a dogfood failure. The first version reordered
only what the reviewer DISPLAYED and left the backend queue untouched; but the
backend refuses to answer any card except its own queue top (InvalidInput
"not at top of queue" - verified against Anki 25.09, and there is no skip/pop
API on the scheduler service at all). So the swapped-in card could be viewed
but never answered, and the reviewer wedged: every answer failed, every
re-fetch swapped again.

The rule this file now obeys everywhere: **the reviewer is only ever handed
the backend's own top card.** Anything we want served later must genuinely
leave the queue, and the only sanctioned way out without touching scheduling
is a bury. Hence:

* **Defer** = a synced `custom_data` marker (records the day; expires by
  meaning at rollover) + a manual bury, wrapped in ONE named undo entry -
  so Anki's own Cmd+Z ("Undo Set Card Aside") reverts both at once.
  Scheduling state - due date, interval, ease, history - is untouched; the
  card self-restores at the next day rollover via Anki's normal unbury even
  if we never run again. The honest cost, which the first design pretended
  away: while set aside the card IS out of today's counts, on every device.

* **Recall** ("show it next") = unbury + a session-only pin. To make the
  pinned card the backend's true top (so answering it is valid), the wrap
  transiently PARKS the few entries ahead of it - same marker+bury, second
  key - and releases them on the very next fetch, after the pinned card was
  answered. Parked cards carry their own synced marker so a crash mid-park
  is healed by the same-day sweep (or by tomorrow's auto-unbury).

aqt-free except install(); every failure path falls through to stock Anki.
"""

from __future__ import annotations

import json
from typing import Any, Callable

# custom_data keys are capped at 8 bytes by the backend.
MARKER_KEY = "cwycDfr"  # deliberately set aside today
PARK_KEY = "cwycPrk"  # transiently parked so a recalled card can be top

# How far into the queue recall will look for the pinned card. Beyond this the
# pin is dropped rather than parking half the session.
FETCH_LIMIT = 20


class DeferralManager:
    def __init__(self, get_col: Callable[[], Any]) -> None:
        self._get_col = get_col
        self._pinned: int | None = None
        # Cards we buried to float a recalled card; released on the next
        # fetch. Session memory only - the PARK_KEY marker is the durable
        # record that heals a crash.
        self._parked: list[int] = []
        self._original: Callable[[Any], None] | None = None

    # ---- marker helpers ----

    def _mark(self, col: Any, card_id: int, key: str) -> None:
        card = col.get_card(int(card_id))
        data = _load(card)
        data[key] = int(col.sched.today)
        card.custom_data = json.dumps(data, separators=(",", ":"))
        col.update_card(card)

    def _unmark(self, col: Any, card_id: int, key: str) -> bool:
        card = col.get_card(int(card_id))
        data = _load(card)
        if data.pop(key, None) is None:
            return False
        card.custom_data = json.dumps(data, separators=(",", ":")) if data else ""
        col.update_card(card)
        return True

    def _marked_today(self, card: Any, key: str) -> bool:
        day = _load(card).get(key)
        try:
            return day is not None and int(day) == int(self._get_col().sched.today)
        except Exception:
            return False

    # ---- the public verbs ----

    def defer(self, card_id: int) -> None:
        """Marker + manual bury, as ONE undo entry so native Cmd+Z reverts it
        (user request 2026-07-29: undo set-aside with the ordinary undo key).
        Scheduling fields are untouched; tomorrow Anki unburies it itself."""
        col = self._get_col()
        target = col.add_custom_undo_entry("Set Card Aside")
        self._mark(col, card_id, MARKER_KEY)
        col.merge_undo_entries(target)
        col.sched.bury_cards([int(card_id)], manual=True)
        col.merge_undo_entries(target)
        if self._pinned == int(card_id):
            self._pinned = None

    def undefer(self, card_id: int) -> None:
        """Drop the marker and unbury. Safe on a card that was never deferred
        (or was deferred by the old marker-only build: unbury is a no-op)."""
        col = self._get_col()
        target = col.add_custom_undo_entry("Bring Card Back")
        if self._unmark(col, card_id, MARKER_KEY):
            col.merge_undo_entries(target)
        col.sched.unbury_cards([int(card_id)])
        col.merge_undo_entries(target)

    def show_next(self, card_id: int) -> None:
        self.undefer(card_id)
        self._pinned = int(card_id)

    def is_deferred(self, card: Any) -> bool:
        return self._marked_today(card, MARKER_KEY)

    def deferred_card_ids(self) -> list[int]:
        col = self._get_col()
        try:
            return [
                int(cid)
                for cid in col.find_cards(
                    f"prop:cdn:{MARKER_KEY}={col.sched.today}"
                )
            ]
        except Exception:
            return []

    @property
    def pinned(self) -> int | None:
        return self._pinned

    def clear_session(self) -> None:
        self._pinned = None
        try:
            self._release_parked(self._get_col())
        except Exception:
            pass

    # ---- parked-card bookkeeping ----

    def _release_parked(self, col: Any) -> None:
        """Unbury everything we parked - the in-memory list, plus (crash
        healing) anything still carrying a same-day park marker."""
        ids = set(self._parked)
        self._parked = []
        try:
            ids.update(
                int(cid)
                for cid in col.find_cards(f"prop:cdn:{PARK_KEY}={col.sched.today}")
            )
        except Exception:
            pass
        if not ids:
            return
        col.sched.unbury_cards(sorted(ids))
        for cid in ids:
            try:
                self._unmark(col, cid, PARK_KEY)
            except Exception:
                continue

    # ---- the reviewer wrap ----

    def install(self, reviewer_cls: Any) -> None:
        if self._original is not None:
            return
        original = reviewer_cls._get_next_v3_card
        self._original = original
        manager = self

        def patched(reviewer: Any) -> None:
            try:
                manager._before_fetch()
            except Exception:
                pass
            original(reviewer)

        reviewer_cls._get_next_v3_card = patched

    def uninstall(self, reviewer_cls: Any) -> None:
        if self._original is not None:
            reviewer_cls._get_next_v3_card = self._original
            self._original = None

    def _before_fetch(self) -> None:
        """Runs before every stock fetch. Releases parked cards, and if a
        recall pin is waiting, parks whatever sits ahead of the pinned card so
        the STOCK fetch serves it as the genuine backend top - which is what
        keeps answering valid."""
        col = self._get_col()
        self._release_parked(col)
        if self._pinned is None:
            return
        pinned = self._pinned
        output = col.sched.get_queued_cards(fetch_limit=FETCH_LIMIT)
        ids = [int(e.card.id) for e in output.cards]
        if pinned not in ids:
            # Not reachable within the window (answered meanwhile, suspended,
            # different deck selected...). Drop the pin rather than hunting.
            self._pinned = None
            return
        ahead = ids[: ids.index(pinned)]
        self._pinned = None  # spent, whether or not anything was ahead
        if not ahead:
            return
        target = col.add_custom_undo_entry("Bring Card Back")
        for cid in ahead:
            self._mark(col, cid, PARK_KEY)
            col.merge_undo_entries(target)
        col.sched.bury_cards(ahead, manual=True)
        col.merge_undo_entries(target)
        self._parked = ahead


def _load(card: Any) -> dict[str, Any]:
    raw = getattr(card, "custom_data", "") or ""
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}
