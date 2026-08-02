"""Postcondition invariants: cheap, scoped checks run inside the write
transaction after a mutation (SAFETY.md Part 2, rule 3).

A violation must raise so ``col.transact()`` rolls the savepoint back and the
proposal is rejected instead of committing corruption. Checks are scoped to the
touched decks/notes/cards so cost stays bounded; only the two collection-wide
COUNTs (a single indexed count each) are unscoped, because the blast-radius
family a template add can trigger touches the notetype, not the notes we
listed, and must still be caught.

No ``aqt`` import: the collection is a parameter. Reads only, always through the
DBProxy (``col.db.scalar/list``) or the decks API; this module never writes and
never issues a non-SELECT statement (SAFETY.md rule 4).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable


@dataclass(frozen=True)
class Scope:
    """The rows a mutation touched. Empty means nothing of that kind changed;
    a scoped check over an empty list is a no-op.

    Two distinct concerns, deliberately separated: ``deck_ids``/``note_ids``/
    ``card_ids`` are the rows to *inspect* for corruption (filtered-home,
    dangling-odid) — inspecting an unchanged row is harmless. ``written_*`` are
    the rows the op actually *stamped* ``usn = -1``, and only those are checked
    for pending-sync. A field edit (``update_note``) stamps the note but NOT its
    existing cards; a card move (``set_deck``) stamps the cards but NOT their
    notes — so conflating the two made pending-sync fire on rows the op never
    wrote. Default-empty ``written_*`` means an undeclared path skips the check
    (safe: it only guards against raw-SQL writes, which we never do) rather than
    false-positiving on already-synced rows."""

    deck_ids: tuple[int, ...] = ()
    note_ids: tuple[int, ...] = ()
    card_ids: tuple[int, ...] = ()
    written_note_ids: tuple[int, ...] = ()
    written_card_ids: tuple[int, ...] = ()


@dataclass(frozen=True)
class Snapshot:
    """Pre-mutation state captured inside the transaction. Counts are
    collection-wide; ``scope`` carries the touched ids the scoped checks reuse."""

    note_count: int
    card_count: int
    scm: int
    scope: Scope


@dataclass(frozen=True)
class Expectation:
    """What the proposal declared it would change. Deltas are collection-wide
    count deltas; ``changes_schema`` waives the scm check for ops that must
    bump it (field/template add/remove, notetype edits — SAFETY.md hazard 17)."""

    note_delta: int = 0
    card_delta: int = 0
    changes_schema: bool = False


class InvariantViolation(Exception):
    """A postcondition failed. The caller must let this propagate so the
    surrounding ``col.transact()`` rolls back; the message names the invariant."""


def _ids2str(ids: Iterable[int]) -> str:
    """Inline an int id list as a SQL tuple, mirroring Anki's ``ids2str``.
    Ids come from the collection, never from user input, so inlining is safe."""
    return "(" + ",".join(str(int(i)) for i in ids) + ")"


def snapshot(col: Any, scope: Scope) -> Snapshot:
    """Capture the before-state for the delta and schema checks."""
    return Snapshot(
        note_count=int(col.db.scalar("select count() from notes")),
        card_count=int(col.db.scalar("select count() from cards")),
        scm=int(col.db.scalar("select scm from col")),
        scope=scope,
    )


def no_homeless_filtered_cards(col: Any, deck_ids: Iterable[int]) -> str | None:
    """No card whose ``did`` is a filtered deck with ``odid == 0``.

    This is the state that breaks review with "home deck is filtered" and that
    Check Database cannot detect or repair (SAFETY.md Part 1, hazard 2). Scoped
    to the touched decks; ``get(default=False)`` avoids the legacy fallback to
    the Default deck that would hide a just-deleted deck.
    """
    for did in deck_ids:
        deck = col.decks.get(int(did), default=False)
        if not deck or not deck.get("dyn"):
            continue
        homeless = col.db.list(
            "select id from cards where did = ? and odid = 0", int(did)
        )
        if homeless:
            return (
                f"{len(homeless)} card(s) sit in filtered deck {int(did)} with no "
                f"home (odid=0), e.g. card {homeless[0]}; review breaks with 'home "
                "deck is filtered' and Check Database cannot repair it"
            )
    return None


def corruption_free_collection(col: Any) -> str | None:
    """The two filtered-deck corruptions, checked COLLECTION-WIDE.

    The scoped `no_homeless_filtered_cards` / `no_dangling_odid` above take an
    explicit id list, which is right for content writes: they know exactly
    which rows they touched. Deck operations do not - a filtered rebuild
    gathers an unknown set, and a deck deletion removes cards rather than
    listing them. Handing those a scoped check means handing them an empty
    list, and a scoped check over an empty list passes unconditionally: a
    postcondition that cannot fail.

    Both queries are unindexed scans of `cards`, so this is deliberately NOT
    used on the per-note write path. Deck ops are rare and human-paced; paying
    two scans to catch the corruption class they can actually cause is a good
    trade.
    """
    # Filtered-ness comes from the API, not SQL: modern Anki stores decks as
    # protobuf blobs and the `decks` table has no `dyn` column any more.
    live = [int(entry.id) for entry in col.decks.all_names_and_ids()]
    filtered = [did for did in live if col.decks.is_filtered(did)]
    if filtered:
        homeless = col.db.scalar(
            f"select count() from cards where odid = 0 and did in {_ids2str(filtered)}"
        )
        if homeless:
            return f"{homeless} card(s) sit in a filtered deck with no home deck"
    dangling = col.db.scalar(
        f"select count() from cards where odid != 0 and odid not in {_ids2str(live)}"
    )
    if dangling:
        return f"{dangling} card(s) point at a home deck that no longer exists"
    return None


def no_dangling_odid(col: Any, card_ids: Iterable[int]) -> str | None:
    """No touched card whose ``odid != 0`` points at a deck that no longer
    exists (home deck deleted out of band; never validated by Anki —
    SAFETY.md hazard 3). Scoped to the touched cards."""
    ids = [int(c) for c in card_ids]
    if not ids:
        return None
    live = {int(d.id) for d in col.decks.all_names_and_ids()}
    rows = col.db.all(
        f"select id, odid from cards where odid != 0 and id in {_ids2str(ids)}"
    )
    dangling = [
        (int(cid), int(odid)) for cid, odid in rows if int(odid) not in live
    ]
    if dangling:
        cid, odid = dangling[0]
        return (
            f"{len(dangling)} card(s) have odid pointing at a deleted deck, e.g. "
            f"card {cid} -> deck {odid}; the home deck is gone and review cannot "
            "restore them"
        )
    return None


def count_delta_matches(
    before: Snapshot, after: Snapshot, expected: Expectation
) -> str | None:
    """Card- and note-count deltas equal what the proposal declared. Catches
    the blast-radius family: a proposal saying "1 note, 2 cards" that produces
    4,000 cards is rolled back (SAFETY.md rule 3, hazard 11)."""
    note_delta = after.note_count - before.note_count
    card_delta = after.card_count - before.card_count
    problems: list[str] = []
    if note_delta != expected.note_delta:
        problems.append(f"notes {note_delta:+d} (declared {expected.note_delta:+d})")
    if card_delta != expected.card_delta:
        problems.append(f"cards {card_delta:+d} (declared {expected.card_delta:+d})")
    if problems:
        return "count delta disagrees with the proposal: " + "; ".join(problems)
    return None


def schema_unchanged(before: Snapshot, after: Snapshot) -> str | None:
    """The schema mark (``scm``) is unchanged. A bump forces a one-way full
    sync with a data-losing direction prompt (SAFETY.md hazard 17); callers
    waive this via ``Expectation.changes_schema`` for ops that must bump it."""
    if before.scm != after.scm:
        return (
            f"schema mark changed ({before.scm} -> {after.scm}); this forces a "
            "one-way full sync and the proposal did not declare it"
        )
    return None


def touched_rows_pending_sync(
    col: Any, note_ids: Iterable[int], card_ids: Iterable[int]
) -> str | None:
    """Every touched note and card row is marked pending sync (``usn == -1``).

    A row left with a real usn (the tell-tale of a raw SQL write) never gets
    gathered as pending and the change silently never reaches another device
    (SAFETY.md hazard 14). Scoped to the touched rows."""
    nids = [int(n) for n in note_ids]
    if nids:
        pending = col.db.list(
            f"select id from notes where usn != -1 and id in {_ids2str(nids)}"
        )
        if pending:
            return (
                f"{len(pending)} touched note(s) not pending sync (usn != -1), e.g. "
                f"note {pending[0]}; the write will never reach another device"
            )
    cids = [int(c) for c in card_ids]
    if cids:
        pending = col.db.list(
            f"select id from cards where usn != -1 and id in {_ids2str(cids)}"
        )
        if pending:
            return (
                f"{len(pending)} touched card(s) not pending sync (usn != -1), e.g. "
                f"card {pending[0]}; the write will never reach another device"
            )
    return None


def assert_all(
    col: Any, before: Snapshot, proposal_expectation: Expectation
) -> None:
    """Run every postcondition against the post-mutation state; raise
    ``InvariantViolation`` naming the first that fails so the transaction rolls
    back. Cheap: two COUNTs, one scm read, and id-scoped lookups."""
    after = snapshot(col, before.scope)
    scope = before.scope

    violation = count_delta_matches(before, after, proposal_expectation)
    if violation is not None:
        raise InvariantViolation(f"count_delta_matches: {violation}")

    if not proposal_expectation.changes_schema:
        violation = schema_unchanged(before, after)
        if violation is not None:
            raise InvariantViolation(f"schema_unchanged: {violation}")

    violation = no_homeless_filtered_cards(col, scope.deck_ids)
    if violation is not None:
        raise InvariantViolation(f"no_homeless_filtered_cards: {violation}")

    violation = no_dangling_odid(col, scope.card_ids)
    if violation is not None:
        raise InvariantViolation(f"no_dangling_odid: {violation}")

    violation = touched_rows_pending_sync(
        col, scope.written_note_ids, scope.written_card_ids
    )
    if violation is not None:
        raise InvariantViolation(f"touched_rows_pending_sync: {violation}")
