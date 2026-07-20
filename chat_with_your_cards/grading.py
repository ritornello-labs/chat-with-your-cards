"""Safe native grading for cards outside the current review queue.

Anki's Browser exposes a native ``Grade Now`` operation.  It is the right
primitive for applying an honest scheduler answer to an arbitrary card: it
builds the card's current scheduling state, records a revlog entry, and lets
the active scheduler (including FSRS) choose the next state.

The one non-obvious case is a card in a filtered deck with rescheduling off.
There, ``Again`` means "repeat this preview" and deliberately leaves the real
schedule untouched.  Emptying/rebuilding the filtered deck to work around
that is unsafe: a limited or randomly ordered deck may gather different
cards.  We instead finish *only that card's* preview with native ``Easy``, then
apply native ``Again`` after Anki has returned it to its home deck.

This module deliberately contains no raw scheduling writes.  Callers must run
it on Anki's main thread, like every other collection operation in the add-on.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import IntEnum
from typing import Any, Iterable, Sequence

from . import invariants


CURSOR_CONFIG_KEY = "cwycGradingCursorV1"
QUEUE_SUSPENDED = -1


class GradingError(RuntimeError):
    """The grading event could not be applied without violating a guard."""


class GradeRating(IntEnum):
    """Values from ``scheduler.CardAnswer.Rating`` in Anki's protobuf."""

    AGAIN = 0
    HARD = 1
    GOOD = 2
    EASY = 3


@dataclass(frozen=True)
class GradingEventRef:
    """An immutable position in one server-side grading-event stream.

    A monotonic stream cursor is smaller and safer than retaining an unbounded
    set of UUIDs in Anki's collection config (which is sent on every sync).
    The cursor update is committed in the same SQLite transaction as the
    scheduler answers, so a retry after a crash is a deterministic no-op.
    """

    stream_id: str
    sequence: int
    event_id: str

    def validate(self) -> None:
        if not self.stream_id.strip() or len(self.stream_id) > 128:
            raise GradingError("grading stream_id must be 1-128 characters")
        if self.sequence < 1:
            raise GradingError("grading event sequence must be positive")
        if not self.event_id.strip() or len(self.event_id) > 128:
            raise GradingError("grading event_id must be 1-128 characters")


@dataclass(frozen=True)
class GradingTarget:
    """A resolved local card plus its stable note identity.

    ``note_guid`` is optional only for low-level/manual callers.  Production
    grading events must provide it, preventing a stale server event from
    grading an unrelated card after an export/re-import or graph remap.
    """

    card_id: int
    note_guid: str | None = None


@dataclass(frozen=True)
class GradingResult:
    card_ids: tuple[int, ...]
    already_applied: bool = False
    preview_exits: tuple[int, ...] = ()
    rescheduling_filtered: tuple[int, ...] = ()
    restored_suspended: tuple[int, ...] = ()
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class _CardBefore:
    card_id: int
    note_id: int
    reps: int
    queue: int
    current_deck_id: int
    home_deck_id: int
    preview_filtered: bool
    rescheduling_filtered: bool


def _unique_targets(targets: Sequence[GradingTarget | int]) -> list[GradingTarget]:
    unique: list[GradingTarget] = []
    seen: dict[int, str | None] = {}
    for raw in targets:
        target = raw if isinstance(raw, GradingTarget) else GradingTarget(int(raw))
        cid = int(target.card_id)
        if cid <= 0:
            raise GradingError(f"invalid card id {cid}")
        if cid in seen:
            if target.note_guid and seen[cid] and target.note_guid != seen[cid]:
                raise GradingError(f"card {cid} was supplied with conflicting note GUIDs")
            if seen[cid] is None and target.note_guid:
                seen[cid] = target.note_guid
                for idx, existing in enumerate(unique):
                    if existing.card_id == cid:
                        unique[idx] = target
                        break
            continue
        seen[cid] = target.note_guid
        unique.append(target)
    if not unique:
        raise GradingError("at least one card is required")
    return unique


def _get_deck(col: Any, deck_id: int) -> Any:
    try:
        deck = col.decks.get(int(deck_id), default=False)
    except TypeError:  # older collection doubles / Anki APIs
        deck = col.decks.get(int(deck_id))
    if not deck:
        raise GradingError(f"card points at missing deck {int(deck_id)}")
    return deck


def _note_guid(col: Any, note_id: int) -> str:
    note = col.get_note(int(note_id))
    return str(getattr(note, "guid", "") or "")


def _preflight_cards(
    col: Any,
    targets: Sequence[GradingTarget],
    *,
    require_note_guids: bool,
) -> list[_CardBefore]:
    snapshots: list[_CardBefore] = []
    for target in targets:
        try:
            card = col.get_card(int(target.card_id))
        except Exception as exc:
            raise GradingError(f"card {target.card_id} no longer exists") from exc

        note_id = int(card.nid)
        if require_note_guids and not target.note_guid:
            raise GradingError(
                f"grading event target {target.card_id} is missing its note GUID"
            )
        if target.note_guid:
            actual_guid = _note_guid(col, note_id)
            if actual_guid != target.note_guid:
                raise GradingError(
                    f"card {target.card_id} no longer belongs to the expected note "
                    f"(GUID {target.note_guid!r} != {actual_guid!r})"
                )

        current_did = int(card.did)
        original_did = int(getattr(card, "odid", 0) or 0)
        current_deck = _get_deck(col, current_did)
        current_is_filtered = bool(current_deck.get("dyn"))

        if current_is_filtered and not original_did:
            raise GradingError(
                f"card {target.card_id} is homeless in filtered deck {current_did} "
                "(odid=0); refusing to grade corrupted scheduling state"
            )
        if original_did and not current_is_filtered:
            raise GradingError(
                f"card {target.card_id} has odid={original_did} but its current "
                f"deck {current_did} is not filtered"
            )

        home_did = original_did or current_did
        home_deck = _get_deck(col, home_did)
        if home_deck.get("dyn"):
            raise GradingError(
                f"card {target.card_id}'s home deck {home_did} is filtered"
            )

        preview = current_is_filtered and not bool(current_deck.get("resched", True))
        snapshots.append(
            _CardBefore(
                card_id=int(target.card_id),
                note_id=note_id,
                reps=int(card.reps),
                queue=int(card.queue),
                current_deck_id=current_did,
                home_deck_id=home_did,
                preview_filtered=preview,
                rescheduling_filtered=current_is_filtered and not preview,
            )
        )
    return snapshots


def _read_cursor(col: Any) -> dict[str, Any] | None:
    raw = col.get_config(CURSOR_CONFIG_KEY, None)
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise GradingError("the collection's grading cursor is malformed")
    try:
        return {
            "stream_id": str(raw["stream_id"]),
            "sequence": int(raw["sequence"]),
            "event_id": str(raw["event_id"]),
        }
    except (KeyError, TypeError, ValueError) as exc:
        raise GradingError("the collection's grading cursor is malformed") from exc


def _event_is_already_applied(col: Any, event: GradingEventRef) -> bool:
    cursor = _read_cursor(col)
    if cursor is None:
        if event.sequence != 1:
            raise GradingError(
                f"grading event stream starts with a gap: got {event.sequence}, expected 1"
            )
        return False
    if cursor["stream_id"] != event.stream_id:
        raise GradingError(
            "grading stream changed; explicit reprovisioning is required before "
            "events from the new stream can be applied"
        )
    current = int(cursor["sequence"])
    if event.sequence < current:
        return True
    if event.sequence == current:
        if cursor["event_id"] != event.event_id:
            raise GradingError(
                f"grading sequence {event.sequence} was reused with a different event id"
            )
        return True
    if event.sequence != current + 1:
        raise GradingError(
            f"grading event stream has a gap: got {event.sequence}, expected {current + 1}"
        )
    return False


def _revlog_count(col: Any, card_ids: Iterable[int]) -> int:
    ids = tuple(int(cid) for cid in card_ids)
    id_sql = "(" + ",".join(str(cid) for cid in ids) + ")"
    return int(col.db.scalar(f"select count() from revlog where cid in {id_sql}"))


def _discard_dangling_undo(col: Any, count: int) -> None:
    """Pop undo entries whose SQL changes were rolled back by DBProxy.

    Each successfully returned native operation pushed exactly one undo step.
    The enclosing DBProxy rollback restores the rows but not Anki's in-memory
    undo queue, so the matching entries must be consumed.  This mirrors the
    proposal chokepoint's proven rollback cleanup.
    """

    for _ in range(count):
        try:
            col.undo()
        except Exception:
            return


def fail_cards_now(
    col: Any,
    targets: Sequence[GradingTarget | int],
    *,
    event: GradingEventRef | None = None,
) -> GradingResult:
    """Apply native ``Again`` ratings to arbitrary cards, atomically.

    ``event`` is required for the production server/reconciler path.  Omitting
    it is useful for an explicit local/manual action and for tests, but provides
    no cross-process idempotency.  Production event targets must carry stable
    note GUIDs as a stale-card guard.

    Durable suspension is preserved: the failure is recorded and then Anki's
    native suspend operation is reapplied.  A one-day burial is intentionally
    consumed by the answer, matching Anki's Browser ``Grade Now`` behavior.
    The caller must marshal this operation onto Anki's main thread.
    """

    normalized = _unique_targets(targets)
    if event is not None:
        event.validate()

    backend_grade = getattr(getattr(col, "_backend", None), "grade_now", None)
    if not callable(backend_grade):
        raise GradingError(
            "this Anki version does not expose the native Grade Now operation"
        )
    db_transact = getattr(getattr(col, "db", None), "transact", None)
    if not callable(db_transact):
        raise GradingError("collection transaction support is unavailable")
    if not all(
        callable(getattr(col, name, None))
        for name in ("add_custom_undo_entry", "merge_undo_entries", "undo")
    ):
        raise GradingError("collection undo support is unavailable")

    card_ids = tuple(target.card_id for target in normalized)
    box: dict[str, Any] = {}
    undo_steps = 0
    undo_target: int | None = None

    def native_op(call: Any) -> Any:
        nonlocal undo_steps
        result = call()
        # Both callers only invoke this for guaranteed writes: Grade Now always
        # adds a revlog, and suspend is skipped unless at least one target is
        # currently unsuspended. A successfully returned native write therefore
        # owns exactly one new undo entry.
        undo_steps += 1
        return result

    def apply() -> None:
        nonlocal undo_steps, undo_target
        if event is not None and _event_is_already_applied(col, event):
            box["result"] = GradingResult(card_ids=card_ids, already_applied=True)
            return

        before = invariants.snapshot(col, invariants.Scope())
        cards = _preflight_cards(
            col, normalized, require_note_guids=event is not None
        )
        preview_ids = tuple(c.card_id for c in cards if c.preview_filtered)
        rescheduling_ids = tuple(
            c.card_id for c in cards if c.rescheduling_filtered
        )
        suspended_ids = tuple(c.card_id for c in cards if c.queue == QUEUE_SUSPENDED)
        revlogs_before = _revlog_count(col, card_ids)

        # Establish an unambiguous boundary before any scheduler answer.  It
        # gives the user one meaningful undo action after success and lets the
        # rollback path pop only entries owned by this operation.
        undo_target = int(col.add_custom_undo_entry("Apply AI grading"))
        undo_steps = 1

        # Preview Again is not a real lapse.  Easy always has finished=true,
        # which returns only this card home without rebuilding its filtered deck.
        if preview_ids:
            native_op(
                lambda: backend_grade(
                    card_ids=list(preview_ids), rating=GradeRating.EASY
                )
            )

        native_op(
            lambda: backend_grade(card_ids=list(card_ids), rating=GradeRating.AGAIN)
        )

        needs_resuspend = tuple(
            cid for cid in suspended_ids if int(col.get_card(cid).queue) != QUEUE_SUSPENDED
        )
        if needs_resuspend:
            native_op(lambda: col.sched.suspend_cards(list(needs_resuspend)))

        expected_revlogs = len(card_ids) + len(preview_ids)
        actual_revlogs = _revlog_count(col, card_ids) - revlogs_before
        if actual_revlogs != expected_revlogs:
            raise GradingError(
                f"native grading wrote {actual_revlogs} revlog entries; "
                f"expected {expected_revlogs}"
            )

        for snapshot in cards:
            card = col.get_card(snapshot.card_id)
            if int(card.reps) != snapshot.reps + 1:
                raise GradingError(
                    f"card {snapshot.card_id} reps changed by "
                    f"{int(card.reps) - snapshot.reps}, expected +1"
                )
            if snapshot.queue == QUEUE_SUSPENDED and int(card.queue) != QUEUE_SUSPENDED:
                raise GradingError(
                    f"card {snapshot.card_id} lost its suspended state while grading"
                )
            if snapshot.preview_filtered:
                if int(getattr(card, "odid", 0) or 0) != 0:
                    raise GradingError(
                        f"preview card {snapshot.card_id} did not return home"
                    )
                if int(card.did) != snapshot.home_deck_id:
                    raise GradingError(
                        f"preview card {snapshot.card_id} returned to deck "
                        f"{int(card.did)}, expected {snapshot.home_deck_id}"
                    )

        if event is not None:
            # Non-undoable on purpose: an explicit Anki Undo is a human override,
            # not permission for the reconciler to double-apply the same event.
            col.set_config(
                CURSOR_CONFIG_KEY,
                {
                    "stream_id": event.stream_id,
                    "sequence": event.sequence,
                    "event_id": event.event_id,
                },
                undoable=False,
            )
            cursor = _read_cursor(col)
            if cursor is None or int(cursor["sequence"]) != event.sequence:
                raise GradingError("grading cursor did not persist")

        scope = invariants.Scope(
            deck_ids=tuple(
                sorted(
                    {
                        c.current_deck_id
                        for c in cards
                        if c.preview_filtered or c.rescheduling_filtered
                    }
                )
            ),
            card_ids=card_ids,
            written_card_ids=card_ids,
        )
        invariants.assert_all(
            col,
            replace(before, scope=scope),
            invariants.Expectation(),
        )
        box["result"] = GradingResult(
            card_ids=card_ids,
            preview_exits=preview_ids,
            rescheduling_filtered=rescheduling_ids,
            restored_suspended=suspended_ids,
        )

    try:
        db_transact(apply)
    except GradingError:
        _discard_dangling_undo(col, undo_steps)
        raise
    except invariants.InvariantViolation as exc:
        _discard_dangling_undo(col, undo_steps)
        raise GradingError(str(exc)) from None
    except Exception as exc:
        _discard_dangling_undo(col, undo_steps)
        raise GradingError(str(exc)) from None

    result = box["result"]
    if result.already_applied or undo_target is None:
        return result

    try:
        col.merge_undo_entries(undo_target)
    except Exception as exc:
        return replace(
            result,
            warnings=(
                "grading applied safely, but Anki could not merge its native "
                f"undo steps: {exc}",
            ),
        )
    return result
