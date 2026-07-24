"""GUI smoke probe for Chat With Your Cards.

Runs inside a disposable Anki profile next to the add-on under test.
Checks that the add-on loads, the dock and shortcuts register, the
webview boots, and a full scripted chat round-trip (JS send button ->
pycmd bridge -> ScriptedBackend -> streamed events -> DOM) works.
Captures light and dark screenshots via mw.grab() (no OS permissions
needed), writes JSON to $ANKI_ADDON_WORKBENCH_RESULT, then exits.
"""

from __future__ import annotations

import importlib
import json
import os
import time
import traceback
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

# Force the deterministic demo backend before the add-on builds one: the
# smoke must not depend on (or spend money through) a real claude CLI.
os.environ["CWYC_BACKEND"] = "scripted"

from aqt import mw
from aqt.qt import QDockWidget, QKeySequence, QShortcut, QTimer

try:
    from aqt.qt import QTest
except ImportError:  # pragma: no cover
    from PyQt6.QtTest import QTest

ADDON_PACKAGE = "chat_with_your_cards"
DOCK_OBJECT_NAME = "chat_with_your_cards_dock"
MENU_LABEL = "Chat With Your Cards"

WEB_READY_TIMEOUT_MS = 20_000
STREAM_TIMEOUT_MS = 15_000
DOM_TIMEOUT_MS = 5_000

DEMO_MESSAGE = "please run a tool demo"
PROPOSE_MESSAGE = "propose a note about this"
PUBLIC_EXPLAIN_MESSAGE = "Explain this card in plain language"
PUBLIC_RELATED_MESSAGE = "Which prerequisite cards should I review before this?"
PUBLIC_PROPOSE_MESSAGE = "Turn my confusion about the quantifiers into a focused card"


def _wait_until(predicate: Callable[[], bool], timeout_ms: int, description: str) -> None:
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        if predicate():
            return
        QTest.qWait(50)
    raise AssertionError(f"timed out waiting for {description}")


def _eval_js(web: Any, script: str, timeout_ms: int, description: str) -> Any:
    holder: dict[str, Any] = {}
    web.evalWithCallback(script, lambda value: holder.__setitem__("value", value))
    _wait_until(lambda: "value" in holder, timeout_ms, description)
    return holder["value"]


def _send_message(web: Any, message: str) -> None:
    """Type `message` into the assistant-ui composer and click Send, driving
    the same real send path a user does (composer-input -> send data-testid ->
    pycmd bridge). ComposerPrimitive.Input is a React-controlled textarea, so
    the DOM `.value` must be set through the native value setter plus a bubbling
    `input` event (React tracks the native setter, not direct `.value` writes) -
    otherwise onChange never fires and the Send button stays disabled (the
    workaround documented in ui/README.md). After the input event React
    re-renders asynchronously, so we poll for the Send button to enable before
    clicking rather than assuming a synchronous DOM."""
    typed = _eval_js(
        web,
        "(function() {"
        "  var input = document.querySelector('[data-testid=composer-input]');"
        "  if (!input) return 'no-input';"
        "  var proto = window.HTMLTextAreaElement.prototype;"
        "  var setter = Object.getOwnPropertyDescriptor(proto, 'value');"
        "  if (setter && setter.set) { setter.set.call(input, " + json.dumps(message) + "); }"
        "  else { input.value = " + json.dumps(message) + "; }"
        "  input.dispatchEvent(new Event('input', {bubbles: true}));"
        "  return 'typed';"
        "})();",
        DOM_TIMEOUT_MS,
        "type into composer",
    )
    if typed != "typed":
        raise AssertionError(f"could not type into composer-input: {typed!r}")

    def _send_enabled() -> bool:
        return bool(
            _eval_js(
                web,
                "(function() {"
                "  var b = document.querySelector('[data-testid=send]');"
                "  return !!b && !b.disabled;"
                "})();",
                DOM_TIMEOUT_MS,
                "send button enabled",
            )
        )

    _wait_until(_send_enabled, DOM_TIMEOUT_MS, "send button to enable after typing")
    clicked = _eval_js(
        web,
        "(function() {"
        "  var b = document.querySelector('[data-testid=send]');"
        "  if (!b || b.disabled) return false;"
        "  b.click();"
        "  return true;"
        "})();",
        DOM_TIMEOUT_MS,
        "send click",
    )
    if clicked is not True:
        raise AssertionError(f"send click did not fire: {clicked!r}")


def _shortcut_keys() -> list[str]:
    assert mw is not None
    return [
        shortcut.key().toString(QKeySequence.SequenceFormat.PortableText)
        for shortcut in mw.findChildren(QShortcut)
    ]


_PNG_1PX = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108020000009077"
    "53de0000000c4944415478da63f8cfc000000301010018dd8db00000000049"
    "454e44ae426082"
)


def _new_note(front: str, tags: Any = None, deck: str = "Default", back: str = "b") -> int:
    """Seed a real note in the disposable collection."""
    assert mw is not None
    model = mw.col.models.by_name("Basic")
    note = mw.col.new_note(model)
    note["Front"] = front
    note["Back"] = back
    if tags:
        note.tags = list(tags)
    mw.col.add_note(note, mw.col.decks.id(deck))
    return int(note.id)


def _backup_count() -> int:
    assert mw is not None
    try:
        folder = Path(mw.pm.backupFolder())
        return len(list(folder.glob("*.colpkg")))
    except Exception:
        return -1


@contextmanager
def _capture_pushes(proposals: Any) -> Iterator[list[dict[str, Any]]]:
    """Temporarily wrap ProposalManager._push to record every pushed payload
    (proposal cards, proposal_error, ledger updates, ...) so a check can
    assert on what got surfaced to the user without going through the
    webview DOM. Restores the original push callable afterward, mirroring
    how _trusted_writes above restores state.config in a finally block."""
    captured: list[dict[str, Any]] = []
    original_push = proposals._push

    def _record(payload: dict[str, Any]) -> None:
        captured.append(payload)
        original_push(payload)

    proposals._push = _record
    try:
        yield captured
    finally:
        proposals._push = original_push


def _undo_status_snapshot() -> dict[str, Any]:
    """Read col.undo_status() into a JSON-safe dict. Used to OBSERVE
    (SAFETY.md's "Known wart on the rollback path": db_rollback reverts the
    SQL but does not pop the undo entry an inner backend op already pushed)
    - never asserted on, since whether the wart manifests is exactly what we
    are trying to learn, not something we already know the answer to."""
    assert mw is not None
    status = mw.col.undo_status()
    return {
        "undo": getattr(status, "undo", "") or None,
        "redo": getattr(status, "redo", "") or None,
        "last_step": int(getattr(status, "last_step", 0)),
    }


def _collection_flow_checks(state: Any, check: Callable[[str, Callable[[], Any]], Any]) -> None:
    """Drive the REAL ProposalManager and collection tools against the real
    disposable collection - the real-Anki equivalents of the fake-collection
    unit tests (col.tags.rename, col.set_deck, col.remove_notes,
    col.create_backup, col.update_note, media dir)."""
    assert mw is not None
    proposals = state.proposals

    def _rename_tag() -> dict[str, Any]:
        ids = [_new_note(f"rename {i}", tags=["probe-old"]) for i in range(2)]
        result = proposals.submit_rename_tag({"old_tag": "probe-old", "new_tag": "probe-new"})
        proposals.accept({"id": result["proposal_id"]})
        if len(mw.col.find_notes('tag:"probe-new"')) != 2:
            raise AssertionError("rename_tag did not apply on the real collection")
        if mw.col.find_notes('tag:"probe-old"'):
            raise AssertionError("old tag still present after rename")
        proposals.revert({"id": result["proposal_id"]})
        if len(mw.col.find_notes('tag:"probe-old"')) != 2:
            raise AssertionError("rename_tag revert did not restore the old tag")
        return {"notes": ids}

    check("bulk rename_tag apply+revert (real col.tags.rename)", _rename_tag)

    def _move_cards() -> dict[str, Any]:
        nid = _new_note("move me", deck="Default")
        cid = mw.col.get_note(nid).cards()[0].id
        result = proposals.submit_move_cards({"query": f"nid:{nid}", "deck": "ProbeArchive"})
        proposals.accept({"id": result["proposal_id"]})
        archive = mw.col.decks.id("ProbeArchive")
        if mw.col.get_card(cid).did != archive:
            raise AssertionError("move_cards did not change the deck (real col.set_deck)")
        proposals.revert({"id": result["proposal_id"]})
        if mw.col.get_card(cid).did != mw.col.decks.id("Default"):
            raise AssertionError("move_cards revert did not restore the deck")
        return {"card": cid}

    check("bulk move_cards apply+revert (real col.set_deck)", _move_cards)

    def _change_set() -> dict[str, Any]:
        ids = [_new_note(f"cs {i}", back="orig") for i in range(3)]
        cs = proposals.open_change_set({"title": "probe sweep"})
        cs_id = cs["change_set_id"]
        for i, nid in enumerate(ids):
            proposals.add_to_change_set(
                {"change_set_id": cs_id, "note_id": nid, "field_changes": {"Back": f"swept {i}"}}
            )
        proposals.close_change_set({"change_set_id": cs_id, "summary": "s"})
        proposals.accept({"id": cs_id})
        for i, nid in enumerate(ids):
            if mw.col.get_note(nid)["Back"] != f"swept {i}":
                raise AssertionError("change set did not apply on the real collection")
        proposals.revert({"id": cs_id})
        if mw.col.get_note(ids[0])["Back"] != "orig":
            raise AssertionError("change set revert did not restore fields")
        return {"notes": len(ids)}

    check("change set apply+revert (real col.update_note)", _change_set)

    def _create_happy_path() -> dict[str, Any]:
        """Baseline for the rollback checks below: submit + accept a create
        proposal through the real ProposalManager chokepoint (SAFETY.md Part
        2 rule 1) and confirm the note actually landed with the declared
        deck/fields/tags and the note count moved by exactly +1."""
        col = mw.col
        before_notes = int(col.db.scalar("select count() from notes"))
        result = proposals.submit_create(
            {
                "note_type": "Basic",
                "deck": "Default",
                "tags": ["probe-happy"],
                "fields": {"Front": "probe happy front", "Back": "probe happy back"},
                "rationale": "probe happy-path create",
            }
        )
        if result.get("status") != "pending_user_review":
            raise AssertionError(f"expected pending_user_review, got {result}")
        proposal_id = result["proposal_id"]
        proposals.accept({"id": proposal_id})

        after_notes = int(col.db.scalar("select count() from notes"))
        if after_notes - before_notes != 1:
            raise AssertionError(
                f"note count delta {after_notes - before_notes}, expected +1"
            )
        from chat_with_your_cards.proposals import ACCEPTED

        proposal = proposals._proposals[proposal_id]
        if proposal.status != ACCEPTED:
            raise AssertionError(f"proposal did not resolve accepted: {proposal.status}")
        if proposal.note_id is None:
            raise AssertionError("accepted proposal has no note_id")
        note = col.get_note(proposal.note_id)
        if note["Front"] != "probe happy front" or note["Back"] != "probe happy back":
            raise AssertionError(f"note fields wrong: {dict(note.items())}")
        deck_name = col.decks.name(note.cards()[0].did)
        if deck_name != "Default":
            raise AssertionError(f"note landed in the wrong deck: {deck_name}")
        if "probe-happy" not in note.tags or "ai-created" not in note.tags:
            raise AssertionError(f"tags missing: {note.tags}")
        if not col.db.list("select id from cards where nid = ? and usn = -1", note.id):
            raise AssertionError("new card not marked pending sync (usn != -1)")
        return {
            "note_id": int(note.id),
            "note_count_delta": after_notes - before_notes,
            "deck": deck_name,
        }

    check("create: happy path submit+accept via real ProposalManager", _create_happy_path)

    def _postcondition_rollback() -> dict[str, Any]:
        """SAFETY.md Part 2 rule 1's backstop: force invariants.assert_all to
        raise AFTER col.add_note has already run, and confirm col.db.transact
        really rolls the SQL back in a REAL Anki collection - the fake
        collection in tests/test_invariants.py hand-simulates this with a
        Python dict snapshot/restore, which cannot tell us whether the real
        rust-backend db_rollback() behaves the same way. Also ASSERTS on
        col.undo_status() before/after: SAFETY.md's "Known wart on the
        rollback path" (a dangling undo entry left by the inner backend op
        that ran before the postcondition failed - db_rollback reverts the
        SQL but not the in-memory undo queue a separate, already-completed
        Rust-level Collection::transact pushed to) is now FIXED by
        ProposalManager._discard_dangling_undo (proposals.py): after a
        postcondition failure, it calls col.undo() exactly as many times as
        the write's execute() issued backend RPCs (ground truth, never a
        guess), consuming the dangling entry/entries. This check is the
        empirical confirmation that a real col.undo() call against a real,
        already-SQL-rolled-back collection is a safe no-op rather than an
        error or a misbehavior (candidate A from the fix's decision list -
        candidate B, a dedicated backend "clear undo" API, does not exist;
        see rslib/src/undo/mod.rs and proto/anki/collection.proto)."""
        from chat_with_your_cards import invariants as invariants_mod
        from chat_with_your_cards.proposals import PENDING

        col = mw.col
        before_notes = int(col.db.scalar("select count() from notes"))
        ledger_before = len(proposals._ledger)
        undo_before = _undo_status_snapshot()

        result = proposals.submit_create(
            {
                "note_type": "Basic",
                "deck": "Default",
                "tags": ["probe-postcondition-rollback"],
                "fields": {
                    "Front": "probe rollback front",
                    "Back": "probe rollback back",
                },
                "rationale": "probe forced postcondition failure",
            }
        )
        proposal_id = result["proposal_id"]

        original_assert_all = invariants_mod.assert_all

        def _boom(*_a: Any, **_kw: Any) -> None:
            raise invariants_mod.InvariantViolation(
                "probe-forced postcondition failure (real-Anki rollback check)"
            )

        invariants_mod.assert_all = _boom
        try:
            with _capture_pushes(proposals) as captured:
                proposals.accept({"id": proposal_id})
        finally:
            invariants_mod.assert_all = original_assert_all

        after_notes = int(col.db.scalar("select count() from notes"))
        if after_notes != before_notes:
            raise AssertionError(
                "note count changed despite forced postcondition failure: "
                f"{before_notes} -> {after_notes} (real-Anki SQL rollback did "
                "not revert col.add_note)"
            )
        if col.find_notes('tag:"probe-postcondition-rollback"'):
            raise AssertionError(
                "a note tagged probe-postcondition-rollback exists after the "
                "forced rollback; col.db.transact did not revert the mutation"
            )
        if len(proposals._ledger) != ledger_before:
            raise AssertionError(
                "ledger grew despite the rolled-back write (phantom ledger entry)"
            )

        errors = [
            p
            for p in captured
            if p.get("type") == "proposal_error" and p.get("id") == proposal_id
        ]
        if not errors:
            raise AssertionError(
                "forced InvariantViolation did not surface as a proposal_error "
                f"(ProposalError); captured pushes: {captured}"
            )
        proposal = proposals._proposals[proposal_id]
        if proposal.status != PENDING:
            raise AssertionError(
                f"proposal should remain pending after a failed accept, got "
                f"{proposal.status!r}"
            )

        undo_after = _undo_status_snapshot()

        # The fix: exactly one dangling entry (one add_note RPC) was pushed
        # before the postcondition rolled the SQL back; _discard_dangling_undo
        # must have popped it, so the queue's top reads back to whatever was
        # there before this whole check started - never left dangling, and
        # never over-popped into older, unrelated real history.
        if undo_after["undo"] != undo_before["undo"]:
            raise AssertionError(
                "dangling undo entry survived the forced rollback: expected "
                f"the undo queue's top to read back to {undo_before['undo']!r} "
                f"(its state before this check ran), got {undo_after['undo']!r} "
                "- _discard_dangling_undo (proposals.py) did not clean it up"
            )

        # The collection must still be usable: a normal accept right after
        # the forced rollback must still succeed cleanly.
        followup = proposals.submit_create(
            {
                "note_type": "Basic",
                "deck": "Default",
                "tags": ["probe-postcondition-followup"],
                "fields": {
                    "Front": "probe followup front",
                    "Back": "probe followup back",
                },
                "rationale": "confirm collection usable after forced rollback",
            }
        )
        proposals.accept({"id": followup["proposal_id"]})
        if not col.find_notes('tag:"probe-postcondition-followup"'):
            raise AssertionError(
                "collection unusable after forced rollback: follow-up accept "
                "failed"
            )

        return {
            "proposal_error_message": errors[0].get("message"),
            "undo_status_before": undo_before,
            "undo_status_after_forced_rollback": undo_after,
            "followup_accept_ok": True,
        }

    check(
        "create: postcondition InvariantViolation rolls back real SQL "
        "(real col.db.transact) + dangling undo entry cleaned up",
        _postcondition_rollback,
    )

    def _change_set_mid_batch_rollback() -> dict[str, Any]:
        """Mid-batch all-or-nothing: a 3-item change set where the SECOND
        item's write is doomed to fail partway through _apply_items. Confirms
        the FIRST item's already-applied col.update_note is rolled back too
        when a later item's write raises inside the same col.db.transact.

        Method note: SAFETY.md's suggested trigger ("delete the note out from
        under it") does NOT reach this code path on the real collection -
        _apply_items wraps col.get_note in its own try/except and treats a
        missing note as a skipped/stale item, not a raised error (the
        staleness guard, by design - confirmed by reading proposals.py, not
        assumed). To actually exercise the all-or-nothing rollback we
        monkeypatch col.update_note to raise for the doomed note's id,
        simulating a genuine backend failure partway through the batch."""
        col = mw.col
        ids = [_new_note(f"f3 batch {i}", back="orig") for i in range(3)]
        cs = proposals.open_change_set({"title": "probe doomed batch"})
        cs_id = cs["change_set_id"]
        for i, nid in enumerate(ids):
            proposals.add_to_change_set(
                {
                    "change_set_id": cs_id,
                    "note_id": nid,
                    "field_changes": {"Back": f"batch swept {i}"},
                }
            )
        proposals.close_change_set({"change_set_id": cs_id, "summary": "doomed batch"})

        doomed_nid = ids[1]
        real_update_note = col.update_note

        def _doomed_update_note(note: Any, *a: Any, **kw: Any) -> Any:
            if int(note.id) == int(doomed_nid):
                raise Exception("probe-forced backend failure mid-batch (F3)")
            return real_update_note(note, *a, **kw)

        col.update_note = _doomed_update_note
        ledger_before = len(proposals._ledger)
        try:
            with _capture_pushes(proposals) as captured:
                proposals.accept({"id": cs_id})
        finally:
            col.update_note = real_update_note

        notes = {nid: col.get_note(nid) for nid in ids}
        if notes[ids[0]]["Back"] != "orig":
            raise AssertionError(
                f"FIRST item's edit was NOT rolled back: {notes[ids[0]]['Back']!r} "
                "(all-or-nothing violated - a mid-batch failure left a partial "
                "write committed)"
            )
        if notes[ids[1]]["Back"] != "orig":
            raise AssertionError(
                f"doomed item unexpectedly changed: {notes[ids[1]]['Back']!r}"
            )
        if notes[ids[2]]["Back"] != "orig":
            raise AssertionError(
                "THIRD (never-reached) item unexpectedly changed: "
                f"{notes[ids[2]]['Back']!r}"
            )
        if len(proposals._ledger) != ledger_before:
            raise AssertionError(
                "ledger grew despite the rolled-back batch (phantom ledger entry)"
            )

        errors = [
            p
            for p in captured
            if p.get("type") == "proposal_error" and p.get("id") == cs_id
        ]
        if not errors:
            raise AssertionError(
                "doomed mid-batch write did not surface as a proposal_error "
                f"(ProposalError); captured pushes: {captured}"
            )

        followup = proposals.submit_create(
            {
                "note_type": "Basic",
                "deck": "Default",
                "tags": ["probe-f3-followup"],
                "fields": {"Front": "f3 followup", "Back": "b"},
                "rationale": "confirm collection usable after mid-batch rollback",
            }
        )
        proposals.accept({"id": followup["proposal_id"]})
        if not col.find_notes('tag:"probe-f3-followup"'):
            raise AssertionError(
                "collection unusable after mid-batch rollback: follow-up accept "
                "failed"
            )

        return {
            "proposal_error_message": errors[0].get("message"),
            "all_or_nothing_confirmed": True,
            "undo_status_after": _undo_status_snapshot(),
        }

    check(
        "change_set: mid-batch backend failure rolls back the WHOLE batch "
        "(all-or-nothing, real col.db.transact)",
        _change_set_mid_batch_rollback,
    )

    def _delete_with_backup() -> dict[str, Any]:
        ids = [_new_note(f"del {i}", tags=["probe-del"]) for i in range(2)]
        before = _backup_count()
        result = proposals.submit_delete_notes({"note_ids": ids})
        proposals.accept({"id": result["proposal_id"]})
        if any(nid in mw.col.find_notes('tag:"probe-del"') for nid in ids):
            raise AssertionError("delete_notes did not remove notes")
        # The delete is irreversible, so its checkpoint is SYNCHRONOUS: by the
        # time accept() returns, the real create_backup has run. Assert on its
        # captured result (deterministic - no file-timing races) that the real
        # Anki backup actually succeeded.
        cp = getattr(state, "last_checkpoint", None)
        if not cp or cp.get("error"):
            raise AssertionError(f"delete checkpoint failed: {cp}")
        if cp.get("critical") is not True:
            raise AssertionError(f"delete checkpoint was not synchronous: {cp}")
        if cp.get("created") is not True:
            raise AssertionError(
                f"real create_backup did not create a backup before delete: {cp}"
            )
        # create_backup returning True is the authoritative signal. The file
        # COUNT is not a reliable check: Anki rotates backups, so a forced
        # backup within the same time bucket replaces the previous one rather
        # than adding (observed: was 1, still 1, created=True). Just assert a
        # backup file exists on disk.
        after = _backup_count()
        if after == 0:
            raise AssertionError("checkpoint reported success but no .colpkg on disk")
        # Delete is not ledger-revertible.
        proposals.revert({"id": result["proposal_id"]})
        if any(_note_exists(nid) for nid in ids):
            raise AssertionError("delete revert unexpectedly restored notes")
        return {"checkpoint": cp, "backups_before": before, "backups_after": after}

    check("delete notes + real synchronous backup checkpoint", _delete_with_backup)

    def _delete_aborts_when_real_backup_fails() -> dict[str, Any]:
        """Job 2's abort contract, against the REAL col.create_backup (not a
        fake): when the checkpoint before a critical (delete) write fails,
        _apply_write must raise before col.remove_notes ever runs - the
        destructive op never proceeds without a safety net on disk."""
        ids = [_new_note(f"del-fail {i}", tags=["probe-del-fail"]) for i in range(2)]

        real_create_backup = mw.col.create_backup

        def failing_create_backup(*_a: Any, **_kw: Any) -> bool:
            raise RuntimeError("probe-forced backup failure (disk full, say)")

        mw.col.create_backup = failing_create_backup
        try:
            result = proposals.submit_delete_notes({"note_ids": ids})
            with _capture_pushes(proposals) as captured:
                proposals.accept({"id": result["proposal_id"]})
        finally:
            mw.col.create_backup = real_create_backup

        if any(nid not in mw.col.find_notes('tag:"probe-del-fail"') for nid in ids):
            raise AssertionError(
                "notes were deleted despite the backup checkpoint failing - "
                "the critical-backup abort did not block the write"
            )
        errors = [
            p
            for p in captured
            if p.get("type") == "proposal_error" and p.get("id") == result["proposal_id"]
        ]
        if not errors:
            raise AssertionError(
                f"backup failure did not surface as a proposal_error; captured: {captured}"
            )
        message = str(errors[0].get("message", ""))
        if "backup failed" not in message:
            raise AssertionError(f"unexpected abort message: {message!r}")

        # The collection must still be usable, and a real (succeeding)
        # backup + delete right after must still work cleanly.
        result2 = proposals.submit_delete_notes({"note_ids": ids})
        proposals.accept({"id": result2["proposal_id"]})
        if any(nid in mw.col.find_notes('tag:"probe-del-fail"') for nid in ids):
            raise AssertionError(
                "collection unusable after aborted delete: follow-up delete failed"
            )
        return {"abort_message": message}

    check(
        "delete aborts (no backend call) when the real backup checkpoint fails",
        _delete_aborts_when_real_backup_fails,
    )

    def _trusted_writes() -> dict[str, Any]:
        old_mode = state.config.get("permission_mode", "default")
        state.config["permission_mode"] = "trusted-writes"
        proposals.new_session()  # reset the write budget
        try:
            result = proposals.submit_create(
                {"note_type": "Basic", "deck": "Default", "tags": ["probe-trusted"],
                 "fields": {"Front": "trusted create", "Back": "b"}, "rationale": "t"}
            )
            if result.get("status") != "created":
                raise AssertionError(f"trusted create not applied directly: {result}")
            if not mw.col.find_notes('tag:"probe-trusted"'):
                raise AssertionError("trusted create left no note in the collection")
        finally:
            state.config["permission_mode"] = old_mode
            proposals.new_session()
        return {"status": result.get("status")}

    check("trusted-writes direct apply", _trusted_writes)

    def _find_cards() -> dict[str, Any]:
        """The agent-facing read must preserve the exact card that matched.

        search_notes intentionally collapses card-level matches to a note, so
        it cannot safely select a sibling for grading. Exercise the real
        col.find_cards path and the serialized card-level context here.
        """
        addon = importlib.import_module(ADDON_PACKAGE)
        nid = _new_note("find this exact card", tags=["probe-find-card"])
        cid = int(mw.col.get_note(nid).cards()[0].id)
        from chat_with_your_cards.tools import build_registry

        result = build_registry().call(
            addon._ToolCtx(),
            "find_cards",
            {"query": f"cid:{cid}", "limit": 20},
        )
        cards = result.get("cards") or []
        if result.get("total") != 1 or len(cards) != 1:
            raise AssertionError(f"find_cards did not preserve the exact match: {result}")
        card = cards[0]
        if int(card.get("card_id", 0)) != cid or int(card.get("note_id", 0)) != nid:
            raise AssertionError(f"find_cards returned the wrong identity: {card}")
        if card.get("template") != "Card 1":
            raise AssertionError(f"find_cards omitted the real template: {card}")
        if "Front" not in (card.get("fields_preview") or {}):
            raise AssertionError(f"find_cards omitted the prompt preview: {card}")
        return {"card": cid, "total": result["total"]}

    check("find_cards preserves exact real-Anki card matches", _find_cards)

    def _card_images() -> dict[str, Any]:
        addon = importlib.import_module(ADDON_PACKAGE)
        media_dir = Path(mw.col.media.dir())
        (media_dir / "probe.png").write_bytes(_PNG_1PX)
        nid = _new_note("has image", back='<img src="probe.png">')
        from chat_with_your_cards.tools import build_registry

        registry = build_registry()
        blocks = registry.call(addon._ToolCtx(), "get_card_images", {"note_id": nid})
        images = [b for b in blocks if b.get("type") == "image"]
        if len(images) != 1 or images[0]["mimeType"] != "image/png":
            raise AssertionError(f"get_card_images failed on real media: {blocks}")
        return {"images": len(images)}

    check("get_card_images against real media dir", _card_images)

    def _note_type_templates() -> dict[str, Any]:
        """get_note_type must return the REAL template source from Anki's own
        model dict (qfmt/afmt/css), not just names - otherwise the agent cannot
        see how a card renders and reports that an <iframe> in the template
        does not exist (dogfood 2026-07-23). Verifies the keys exist on a real
        25.09 note type, which only real Anki can prove."""
        addon = importlib.import_module(ADDON_PACKAGE)
        from chat_with_your_cards.tools import build_registry

        result = build_registry().call(addon._ToolCtx(), "get_note_type", {"name": "Basic"})
        templates = result.get("templates") or []
        if not templates or not isinstance(templates[0], dict):
            raise AssertionError(f"templates are not objects: {result}")
        first = templates[0]
        if "qfmt" not in first or "afmt" not in first:
            raise AssertionError(f"template source missing: {first}")
        if "{{" not in str(first["qfmt"]):
            raise AssertionError(f"qfmt does not look like template source: {first}")
        if "css" not in result:
            raise AssertionError(f"note-type css missing: {list(result)}")
        return {"template": first["name"], "qfmt_chars": len(str(first["qfmt"]))}

    check("get_note_type returns real template source", _note_type_templates)

    def _learning_flow() -> dict[str, Any]:
        # The edit-pattern learning loop end-to-end on the real collection:
        # accept-time diff -> snapshot -> direct col edit found by scan (real
        # notes.mod bulk query) -> skill-update proposal writes the real
        # SKILL.md, archives the old one, consumes the observations.
        store = state.learning
        if store is None:
            raise AssertionError("learning store missing from add-on state")
        store.consume(store.pending_ids())  # isolate from earlier checks
        result = proposals.submit_create(
            {"note_type": "Basic", "deck": "Default", "tags": [],
             "fields": {"Front": "learn front", "Back": "agent answer"},
             "rationale": "t"}
        )
        proposals.accept(
            {"id": result["proposal_id"],
             "fields": {"Front": "learn front", "Back": "user answer"}}
        )
        reviewed = [o for o in store.pending() if o.get("kind") == "reviewed"]
        if not reviewed:
            raise AssertionError("accept-time edit produced no reviewed observation")
        nid = proposals._proposals[result["proposal_id"]].note_id
        if str(nid) not in store._snapshots:
            raise AssertionError("accepted creation was not snapshotted")
        # notes.mod has 1s granularity; cross the boundary so the real edit
        # is visible to the bulk-mod fast path (real users edit much later).
        QTest.qWait(1100)
        note = mw.col.get_note(nid)
        note["Back"] = "user improved this later"
        mw.col.update_note(note)
        found = store.scan(mw.col)
        if found < 1:
            raise AssertionError("scan missed a direct collection edit")
        edited = [o for o in store.pending() if o.get("kind") == "edited_later"]
        if not edited or edited[-1]["note_id"] != nid:
            raise AssertionError(f"edited_later observation wrong: {edited}")

        old_skill = store.read_skill()
        if not old_skill:
            raise AssertionError("card-authoring skill not materialized on disk")
        upd = proposals.submit_skill_update(
            {"summary": "Probe learned preferences.",
             "patterns": ["probe pattern"],
             "new_content": old_skill + "\n- probe learned rule\n"},
            old_content=old_skill,
            observation_ids=store.pending_ids(),
        )
        proposals.accept({"id": upd["proposal_id"]})
        if "- probe learned rule" not in store.read_skill():
            raise AssertionError("accepted skill update did not write SKILL.md")
        if store.pending():
            raise AssertionError("observations not consumed after skill update")
        if not list(store._backups_dir.glob("*.md")):
            raise AssertionError("previous skill version was not archived")
        store.write_skill(old_skill)  # leave the skill as we found it
        return {"reviewed": len(reviewed), "scan_found": found}

    check("learning loop: capture -> scan -> skill update (real col)", _learning_flow)

    def _deck_ops_flow() -> dict[str, Any]:
        # Deck management on the real backend - this is where the modern
        # decks/sched API surface the deck ops assume gets validated:
        # decks.save renames children, config_dict_for_deck_id/update_config
        # round-trip, new_filtered + terms + rebuild gather, removal of a
        # filtered deck returns cards home.
        col = mw.col

        def _did(name: str) -> Any:
            try:
                return col.decks.id_for_name(name)
            except Exception:
                return None

        res = proposals.submit_create_deck({"name": "ZZProbeDecks"})
        create_pid = res["proposal_id"]
        proposals.accept({"id": create_pid})
        if not _did("ZZProbeDecks"):
            raise AssertionError("create_deck did not create the deck")
        col.decks.id("ZZProbeDecks::Child")

        res = proposals.submit_rename_deck(
            {"deck": "ZZProbeDecks", "new_name": "ZZProbeDecksR"}
        )
        proposals.accept({"id": res["proposal_id"]})
        if not _did("ZZProbeDecksR::Child"):
            raise AssertionError(
                "rename_deck did not carry the child (real col.decks.save)"
            )
        if _did("ZZProbeDecks"):
            raise AssertionError("old deck name still present after rename")
        proposals.revert({"id": res["proposal_id"]})
        if not _did("ZZProbeDecks::Child"):
            raise AssertionError("rename revert did not restore the child deck")

        did = _did("ZZProbeDecks")
        conf = col.decks.config_dict_for_deck_id(did)
        old_per_day = int(conf["new"]["perDay"])
        res = proposals.submit_set_deck_options(
            {"deck": "ZZProbeDecks", "options": {"new.perDay": old_per_day + 7}}
        )
        proposals.accept({"id": res["proposal_id"]})
        now = int(col.decks.config_dict_for_deck_id(did)["new"]["perDay"])
        if now != old_per_day + 7:
            raise AssertionError(
                f"set_deck_options did not apply (real update_config): {now}"
            )
        proposals.revert({"id": res["proposal_id"]})
        now = int(col.decks.config_dict_for_deck_id(did)["new"]["perDay"])
        if now != old_per_day:
            raise AssertionError(f"options revert did not restore the value: {now}")

        nids = [_new_note(f"deckops {i}", deck="ZZProbeDecks") for i in range(2)]
        res = proposals.submit_create_filtered_deck(
            {
                "name": "ZZProbeCram",
                "terms": [
                    {"search": 'deck:"ZZProbeDecks"', "limit": 10, "order": 5}
                ],
            }
        )
        filter_pid = res["proposal_id"]
        proposals.accept({"id": filter_pid})
        cram_did = _did("ZZProbeCram")
        if not cram_did or not col.decks.get(cram_did).get("dyn"):
            raise AssertionError("create_filtered_deck did not create a dyn deck")
        gathered = len(col.find_cards('deck:"ZZProbeCram"'))
        if gathered != 2:
            raise AssertionError(f"filtered rebuild gathered {gathered}, expected 2")

        res = proposals.submit_filtered_deck_action(
            {"deck": "ZZProbeCram", "action": "empty"}
        )
        proposals.accept({"id": res["proposal_id"]})
        if col.find_cards('deck:"ZZProbeCram"'):
            raise AssertionError("empty action left cards in the filtered deck")

        res = proposals.submit_update_filtered_deck(
            {
                "deck": "ZZProbeCram",
                "terms": [
                    {"search": 'deck:"ZZProbeDecks"', "limit": 1, "order": 5}
                ],
            }
        )
        proposals.accept({"id": res["proposal_id"]})
        regathered = len(col.find_cards('deck:"ZZProbeCram"'))
        if regathered != 1:
            raise AssertionError(
                f"update_filtered_deck re-gathered {regathered}, expected 1"
            )

        proposals.revert({"id": filter_pid})
        if _did("ZZProbeCram"):
            raise AssertionError("reverting create_filtered_deck did not remove it")
        parent_did = _did("ZZProbeDecks")
        home = [
            cid
            for cid in col.find_cards('deck:"ZZProbeDecks"')
            if col.get_card(cid).did == parent_did
        ]
        if len(home) != 2:
            raise AssertionError(
                f"cards did not return home after filtered removal: {len(home)}"
            )

        # The created deck now holds cards: the ledger revert must REFUSE.
        proposals.revert({"id": create_pid})
        if not _did("ZZProbeDecks"):
            raise AssertionError("create_deck revert removed a deck holding cards")

        col.remove_notes(nids)
        col.decks.remove([_did("ZZProbeDecks::Child"), _did("ZZProbeDecks")])
        return {"gathered": gathered, "per_day": old_per_day}

    check(
        "deck ops: create/rename/options/filtered lifecycle + reverts (real col)",
        _deck_ops_flow,
    )

    def _native_grading_flow() -> dict[str, Any]:
        # Prove the arbitrary-card grading path against Anki's real 25.09 Rust
        # scheduler.  The key regression is preview filtered decks: native
        # Grade Now/Again alone repeats the preview and does NOT lapse the
        # underlying card, while grading.fail_cards_now exits only that card's
        # preview and then records a real Again without rebuilding the deck.
        col = mw.col
        grading = importlib.import_module(
            f"{ADDON_PACKAGE}._vendor.safe_collection_operations"
        )

        def _review_card(front: str) -> tuple[int, int]:
            nid = _new_note(front, deck="ZZProbeGrading")
            cid = int(col.get_note(nid).cards()[0].id)
            col.sched.set_due_date([cid], "30")
            return nid, cid

        def _filtered(name: str, card_ids: list[int], *, resched: bool) -> int:
            did = int(col.decks.new_filtered(name))
            deck = col.decks.get(did)
            search = " or ".join(f"cid:{cid}" for cid in card_ids)
            deck["terms"] = [[search, 100, 5]]
            deck["resched"] = resched
            col.decks.save(deck)
            col.sched.rebuild_filtered_deck(did)
            return did

        # Establish why the preview branch exists using the unwrapped native
        # operation first.
        _, naive_cid = _review_card("grade preview naive")
        naive_did = _filtered("ZZProbeGradeNaive", [naive_cid], resched=False)
        naive_before = col.get_card(naive_cid)
        naive_reps = int(naive_before.reps)
        naive_lapses = int(naive_before.lapses)
        naive_revlogs = int(
            col.db.scalar("select count() from revlog where cid = ?", naive_cid)
        )
        col._backend.grade_now(
            card_ids=[naive_cid], rating=grading.Rating.AGAIN
        )
        naive_after = col.get_card(naive_cid)
        if int(naive_after.did) != naive_did or int(naive_after.reps) != naive_reps:
            raise AssertionError("preview Grade Now/Again unexpectedly changed real scheduling")
        if int(naive_after.lapses) != naive_lapses:
            raise AssertionError("preview Grade Now/Again unexpectedly added a lapse")
        naive_revlogs_after = int(
            col.db.scalar("select count() from revlog where cid = ?", naive_cid)
        )
        if naive_revlogs_after != naive_revlogs + 1:
            raise AssertionError("preview Grade Now/Again did not log its preview answer")

        # Normal future-due review + event cursor idempotency.
        normal_nid, normal_cid = _review_card("grade future normal")
        normal_guid = str(col.get_note(normal_nid).guid)
        normal_before = col.get_card(normal_cid)
        normal_reps = int(normal_before.reps)
        event = grading.EventRef("gui-smoke-stream", 1, "gui-smoke-event-1")
        target = grading.Target(normal_cid, normal_guid)
        first = grading.fail_cards_now(col, [target], event=event)
        retry = grading.fail_cards_now(col, [target], event=event)
        if first.already_applied or not retry.already_applied:
            raise AssertionError("grading event cursor did not make the retry a no-op")
        if int(col.get_card(normal_cid).reps) != normal_reps + 1:
            raise AssertionError("future-due card did not receive exactly one real answer")

        # Preview filtered target: its companion must remain byte-for-byte in
        # the deck; no empty/rebuild operation is allowed to reshuffle it.
        _, preview_cid = _review_card("grade preview target")
        _, companion_cid = _review_card("grade preview companion")
        preview_did = _filtered(
            "ZZProbeGradePreview", [preview_cid, companion_cid], resched=False
        )
        companion_before = col.get_card(companion_cid)
        companion_state = (
            int(companion_before.did),
            int(companion_before.odid),
            int(companion_before.queue),
            int(companion_before.due),
            int(companion_before.reps),
        )
        preview_before = col.get_card(preview_cid)
        preview_reps = int(preview_before.reps)
        preview_logs = int(
            col.db.scalar("select count() from revlog where cid = ?", preview_cid)
        )
        preview_result = grading.fail_cards_now(col, [preview_cid])
        preview_after = col.get_card(preview_cid)
        companion_after = col.get_card(companion_cid)
        if preview_result.preview_exits != (preview_cid,):
            raise AssertionError(f"preview branch was not reported: {preview_result}")
        if (int(preview_after.did), int(preview_after.odid)) != (
            int(col.decks.id_for_name("ZZProbeGrading")),
            0,
        ):
            raise AssertionError("preview target did not return to its normal home deck")
        if int(preview_after.reps) != preview_reps + 1:
            raise AssertionError("preview target did not receive one real Again")
        preview_logs_after = int(
            col.db.scalar("select count() from revlog where cid = ?", preview_cid)
        )
        if preview_logs_after != preview_logs + 2:
            raise AssertionError("preview target should have one preview exit + one real Again")
        if (
            int(companion_after.did),
            int(companion_after.odid),
            int(companion_after.queue),
            int(companion_after.due),
            int(companion_after.reps),
        ) != companion_state:
            raise AssertionError("preview companion changed; the deck was likely rebuilt")
        if int(companion_after.did) != preview_did:
            raise AssertionError("preview companion left its filtered deck")

        # Rescheduling filtered cards are already real reviews; one native
        # Again is enough, whether the resulting relearning state remains in
        # the filtered deck or returns home under the active deck preset.
        _, resched_cid = _review_card("grade rescheduling filtered")
        resched_did = _filtered("ZZProbeGradeResched", [resched_cid], resched=True)
        resched_before = col.get_card(resched_cid)
        resched_reps = int(resched_before.reps)
        resched_logs = int(
            col.db.scalar("select count() from revlog where cid = ?", resched_cid)
        )
        resched_result = grading.fail_cards_now(col, [resched_cid])
        resched_after = col.get_card(resched_cid)
        if resched_result.rescheduling_filtered != (resched_cid,):
            raise AssertionError("rescheduling filtered branch was not reported")
        if int(resched_after.reps) != resched_reps + 1:
            raise AssertionError("rescheduling filtered card did not receive one Again")
        resched_logs_after = int(
            col.db.scalar("select count() from revlog where cid = ?", resched_cid)
        )
        if resched_logs_after != resched_logs + 1:
            raise AssertionError("rescheduling filtered card wrote the wrong revlog count")
        if int(resched_after.odid) and int(resched_after.did) != resched_did:
            raise AssertionError("rescheduling filtered card has inconsistent did/odid")

        # Explicit suspension is durable policy, so record the failure and
        # restore suspension with Anki's native operation.
        _, suspended_cid = _review_card("grade suspended")
        col.sched.suspend_cards([suspended_cid])
        suspended_before = col.get_card(suspended_cid)
        suspended_reps = int(suspended_before.reps)
        suspended_result = grading.fail_cards_now(col, [suspended_cid])
        suspended_after = col.get_card(suspended_cid)
        if suspended_result.preserved_suspended != (suspended_cid,):
            raise AssertionError("suspension restore was not reported")
        if int(suspended_after.queue) != -1:
            raise AssertionError("grading unexpectedly unsuspended an explicit suspension")
        if int(suspended_after.reps) != suspended_reps + 1:
            raise AssertionError("suspended card did not record the real failure")

        # CWYC's policy layer must preflight and wait in Propose mode, then
        # execute the same vendored core only after the confirmation action.
        _, workflow_cid = _review_card("grade through CWYC chip")
        workflow_before = int(col.get_card(workflow_cid).reps)
        workflow = state.grading.submit_fail(
            {
                "card_ids": [workflow_cid],
                "rationale": "real-Anki integration probe",
            }
        )
        if workflow.get("status") != "pending_user_confirmation":
            raise AssertionError(f"grading workflow did not pause: {workflow}")
        if int(col.get_card(workflow_cid).reps) != workflow_before:
            raise AssertionError("pending grading mutated the card before confirmation")
        state.grading.accept({"id": workflow["grading_id"]})
        request = state.grading._requests[workflow["grading_id"]]
        if request.status != "accepted":
            raise AssertionError(f"grading chip did not resolve accepted: {request.status}")
        if int(col.get_card(workflow_cid).reps) != workflow_before + 1:
            raise AssertionError("confirmed grading chip did not record native Again")

        return {
            "normal": normal_cid,
            "preview": preview_cid,
            "preview_companion": companion_cid,
            "rescheduling": resched_cid,
            "suspended": suspended_cid,
            "workflow": workflow_cid,
        }

    check(
        "native arbitrary-card grading + CWYC confirmation workflow",
        _native_grading_flow,
    )

    def _reviewer_refresh() -> dict[str, Any]:
        # Highest-value real-Anki path: edit the note under review and confirm
        # the reviewer re-renders in place (private _showQuestion API).
        nid = _new_note("review q", deck="ProbeReview", back="old back")
        mw.col.decks.select(mw.col.decks.id("ProbeReview"))
        mw.moveToState("review")
        QTest.qWait(400)
        reviewer = getattr(mw, "reviewer", None)
        card = getattr(reviewer, "card", None) if reviewer else None
        if card is None or card.nid != nid:
            return {"attempted": True, "in_review": False, "note": "could not enter review"}
        result = proposals.submit_edit({"note_id": nid, "field_changes": {"Front": "review q EDITED"}})
        proposals.accept({"id": result["proposal_id"]})
        QTest.qWait(300)
        fresh = mw.reviewer.card.note()["Front"] if mw.reviewer.card else ""
        mw.moveToState("deckBrowser")
        if fresh != "review q EDITED":
            raise AssertionError(f"reviewer did not refresh after edit: {fresh!r}")
        return {"attempted": True, "in_review": True, "refreshed": True}

    check("reviewer refresh after edit (real review state)", _reviewer_refresh)


def _note_exists(note_id: int) -> bool:
    assert mw is not None
    try:
        mw.col.get_note(note_id)
        return True
    except Exception:
        return False


def _run_checks() -> dict[str, Any]:
    assert mw is not None
    checks: list[dict[str, Any]] = []

    def check(name: str, fn: Callable[[], Any]) -> Any:
        value = fn()
        entry: dict[str, Any] = {"name": name, "ok": True}
        # Most checks return None or a live object (module, QDockWidget) for
        # the caller's own use; only a JSON-safe dict is worth echoing into
        # the result file (e.g. the undo_status observations below need to
        # reach $ANKI_ADDON_WORKBENCH_RESULT, not just pass/fail).
        if isinstance(value, dict):
            entry["detail"] = value
        checks.append(entry)
        return value

    addon = check(
        "module imported",
        lambda: importlib.import_module(ADDON_PACKAGE),
    )
    state = addon.state

    dock = mw.findChild(QDockWidget, DOCK_OBJECT_NAME)

    def _dock_exists() -> Any:
        # Shell redesign (2026-07-13): the dock is ALWAYS visible; it starts
        # as the collapsed rail (config default dock_collapsed=True), pinned
        # at the rail width, with the native Qt title bar replaced by an
        # empty widget (the webview header is the chrome).
        from chat_with_your_cards.dock import RAIL_WIDTH

        if dock is None or state.dock is not dock:
            raise AssertionError("chat dock not found on the main window")
        if not dock.isVisible():
            raise AssertionError("dock should start visible (as the rail)")
        if dock.expanded:
            raise AssertionError("dock should start collapsed (rail)")
        if dock.width() != RAIL_WIDTH:
            raise AssertionError(f"rail width {dock.width()}, expected {RAIL_WIDTH}")
        title_bar = dock.titleBarWidget()
        if title_bar is None or title_bar.height() > 0:
            raise AssertionError("native title bar should be replaced by an empty widget")
        return dock

    check("dock starts visible as the collapsed rail, no native title bar", _dock_exists)

    def _tools_action() -> None:
        actions = mw.form.menuTools.actions()
        entry = next(
            (a for a in actions if a.text().replace("&", "") == MENU_LABEL), None
        )
        if entry is None:
            texts = [a.text().replace("&", "") for a in actions]
            raise AssertionError(f"Tools menu is missing {MENU_LABEL!r}: {texts}")
        submenu = entry.menu()
        if submenu is None:
            raise AssertionError("Tools entry should be a submenu, not a bare toggle")
        items = [a.text().replace("&", "").split("\t")[0] for a in submenu.actions()]
        for expected in ("Toggle chat", "New chat"):
            if expected not in items:
                raise AssertionError(f"submenu missing {expected!r}: {items}")

    check("Tools submenu present with labeled actions", _tools_action)

    def _shortcuts_registered() -> None:
        keys = _shortcut_keys()
        for expected in (state.config["toggle_shortcut"], state.config["new_chat_shortcut"]):
            normalized = QKeySequence(expected).toString(
                QKeySequence.SequenceFormat.PortableText
            )
            if normalized not in keys:
                raise AssertionError(f"shortcut {expected!r} not registered (found {keys})")
        if len(state.shortcuts) != 2:
            raise AssertionError(f"expected 2 tracked shortcuts, got {len(state.shortcuts)}")

    check("shortcuts registered", _shortcuts_registered)

    def _web_ready() -> None:
        try:
            _wait_until(lambda: state.web_ready, WEB_READY_TIMEOUT_MS, "webview ready ping")
        except AssertionError:
            diagnostics = _eval_js(
                dock.web,
                "(function() {"
                "  var root = document.getElementById('cwyc-root');"
                "  return {"
                "  pycmd: typeof pycmd,"
                "  chatUI: typeof window.chatUI,"
                "  stylesheets: document.styleSheets.length,"
                "  root: root ? true : false,"
                "  root_children: root ? root.childElementCount : -1,"
                "  composer: document.querySelector('[data-testid=composer-input]') ? true : false,"
                "  title: document.title"
                "}; })();",
                DOM_TIMEOUT_MS,
                "ready-timeout diagnostics",
            )
            raise AssertionError(f"webview ready ping never arrived; page state: {diagnostics}")

    check("webview ready ping received", _web_ready)

    # Focused, opt-in lane for collection/scheduler work.  It keeps the same
    # real disposable Anki profile and add-on load, but skips unrelated dock
    # animation/hover assertions that can be timing-sensitive on the host.
    # The normal smoke never sets this and still runs the complete suite.
    if os.environ.get("CWYC_SMOKE_COLLECTION_ONLY"):
        _collection_flow_checks(state, check)
        return {
            "ok": True,
            "checks": checks,
            "collection_only": True,
            "anki_version": getattr(mw, "appVersion", None),
        }

    def _toggle_expands_dock() -> None:
        # expand_target(), not MIN_DOCK_WIDTH: in a small window the target
        # is clamped below the nominal floor so the dock never gets laid out
        # wider than the window (which Qt "solves" by clipping it).
        addon.toggle_chat_focus()
        _wait_until(
            lambda: dock.expanded and dock._anim is None
            and dock.width() >= dock.expand_target(),
            3_000,
            "dock to finish expanding after toggle",
        )

    check("toggle expands the rail to the full dock", _toggle_expands_dock)

    def _scripted_chat() -> dict[str, Any]:
        controller = state.controller
        _send_message(dock.web, DEMO_MESSAGE)

        def _stream_finished() -> bool:
            return any(type(e).__name__ == "Done" for e in controller.event_log)

        _wait_until(_stream_finished, STREAM_TIMEOUT_MS, "scripted stream to finish")

        names = [type(e).__name__ for e in controller.event_log]
        deltas = names.count("TextDelta")
        if deltas < 2:
            raise AssertionError(f"expected several TextDelta events, got {deltas}")
        started = [e for e in controller.event_log if type(e).__name__ == "ToolCallStarted"]
        finished = [e for e in controller.event_log if type(e).__name__ == "ToolCallFinished"]
        if len(started) != 1 or len(finished) != 1 or started[0].call_id != finished[0].call_id:
            raise AssertionError(f"expected one matched tool call pair, got {names}")
        return {"events": len(names), "text_deltas": deltas}

    stream_info = check("scripted chat streams end-to-end", _scripted_chat)

    def _dom_rendered() -> dict[str, Any]:
        QTest.qWait(300)  # allow the final render to settle
        dom = _eval_js(
            dock.web,
            "(function() {"
            "  var compInput = document.querySelector('[data-testid=composer-input]');"
            "  var thinkSummary = document.querySelector('[data-testid=thinking-summary]');"
            "  return {"
            "  user: document.querySelectorAll('[data-testid=user-message]').length,"
            "  assistant: document.querySelectorAll('[data-testid=assistant-message]').length,"
            "  chips: document.querySelectorAll('[data-testid=tool-chip]').length,"
            "  chips_ok: document.querySelectorAll('[data-testid=tool-chip].cwyc-tool-ok').length,"
            "  streaming: document.querySelectorAll('.cwyc-streaming').length,"
            "  markdown: document.querySelectorAll('[data-testid=assistant-message] .cwyc-markdown').length,"
            "  composer_input_rect: compInput ? compInput.getBoundingClientRect().height : -1,"
            "  send: document.querySelector('[data-testid=send]') ? true : false,"
            "  send_overflow: (function() {"
            "    var b = document.querySelector('[data-testid=send]');"
            "    if (!b) return 0;"
            "    return Math.max(0, Math.round(b.getBoundingClientRect().right"
            "      - document.documentElement.clientWidth));"
            "  })(),"
            "  stop: document.querySelector('[data-testid=stop]') ? true : false,"
            "  thinking_indicator: document.querySelectorAll('[data-testid=thinking-indicator]').length,"
            "  thinking_summary: document.querySelectorAll('[data-testid=thinking-summary]').length,"
            "  thinking_text: thinkSummary ? thinkSummary.textContent : '',"
            # Did the bundled IBM Plex actually load in Anki's webview, or did
            # it silently fall back to a system sans? document.fonts.check is
            # the ground truth. If false, the dock renders in a fallback face
            # and every browser-preview measurement is off (dogfood 2026-07-12).
            "  plex_sans_loaded: document.fonts.check('13px \"IBM Plex Sans\"'),"
            "  plex_mono_loaded: document.fonts.check('11px \"IBM Plex Mono\"'),"
            "  body_font: compInput ? getComputedStyle(compInput).fontFamily : ''"
            "}; })();",
            DOM_TIMEOUT_MS,
            "DOM state query",
        )
        # The single-row composer input must not blow up (the classic content-box
        # regression this guard originally caught): a one-line textarea is ~20-40px.
        if dom.get("composer_input_rect", 0) > 120:
            raise AssertionError(f"composer input blew up: {dom}")
        if dom["user"] < 1 or dom["assistant"] < 1 or dom["chips"] < 1:
            raise AssertionError(f"transcript DOM incomplete: {dom}")
        if dom["chips_ok"] != dom["chips"]:
            raise AssertionError(f"tool chip did not finish: {dom}")
        if dom["streaming"] != 0:
            raise AssertionError(f"assistant message still marked streaming: {dom}")
        # Assistant text renders through the markdown pipeline (TextPart.tsx ->
        # markdown.ts: marked + DOMPurify). At least one .cwyc-markdown container
        # must exist inside an assistant message, proving markdown rendering is live.
        if dom["markdown"] < 1:
            raise AssertionError(f"assistant text did not render as markdown: {dom}")
        # The stream is done: the composer must be back to Send (not the streaming
        # Stop button).
        if not dom["send"] or dom["stop"]:
            raise AssertionError(f"composer not back to Send after done: {dom}")
        # The whole composer row must sit INSIDE the visible viewport: a
        # too-wide layout (e.g. a dock width the window couldn't honor - Qt
        # clips the dock at the window edge rather than failing) cuts the
        # send button in half (real Anki, stock ~670px window, 2026-07-13).
        if dom.get("send_overflow", 0) > 0:
            raise AssertionError(
                f"send button overflows the viewport by {dom['send_overflow']}px: {dom}"
            )
        # TOOL_SCRIPT (backends/fixtures.py, DEMO_MESSAGE selects it via the
        # "tool" keyword) opens with a thinking phase (empty-text,
        # growing-estimated_tokens ThinkingDelta beats - see scripted.py). By
        # the time the stream is done and this settles, ReasoningBlock.tsx must
        # have collapsed the live thinking-indicator into the static
        # thinking-summary "Thought for ~N tokens" one-liner - never left
        # mid-rotation (no live indicator lingering) and never silently dropped.
        if dom["thinking_summary"] < 1:
            raise AssertionError(f"thinking summary missing after done: {dom}")
        if dom["thinking_indicator"] != 0:
            raise AssertionError(f"live thinking indicator lingered after done: {dom}")
        if "Thought for ~" not in dom["thinking_text"] or "tokens" not in dom["thinking_text"]:
            raise AssertionError(f"thinking indicator did not collapse to a token count: {dom}")
        return dom

    dom_info = check("transcript DOM rendered", _dom_rendered)

    def _control_surface() -> dict[str, Any]:
        """Parity rebuild (dogfood 2026-07-11): the header (new-chat, history,
        open-in-Claude-Code split button, doctor cog) and the composer control
        row (permission-mode chip, Pins, model/effort picker) must render, and
        the mode chip must reflect the authoritative "agent" push (the probe
        profile runs permission_mode=default -> label "Propose")."""
        controls = _eval_js(
            dock.web,
            "(function() {"
            "  var ids = ['header','new-chat','history-button','open-cc',"
            "             'open-cc-caret','settings','mode-chip','pins-button',"
            "             'model-picker','tools-chip','collapse','rail'];"
            "  var out = {};"
            "  ids.forEach(function(id) {"
            "    out[id.replace(/-/g, '_')] ="
            "      document.querySelectorAll('[data-testid=' + id + ']').length;"
            "  });"
            "  var chip = document.querySelector('[data-testid=mode-chip]');"
            "  out.mode_label = chip ? chip.textContent : '';"
            "  var cc = document.querySelector('[data-testid=open-cc]');"
            "  out.open_cc_text = cc ? cc.textContent : '';"
            "  return out;"
            "})();",
            DOM_TIMEOUT_MS,
            "control surface query",
        )
        missing = [key for key, count in controls.items() if isinstance(count, int) and count != 1]
        if missing:
            raise AssertionError(f"control surface incomplete ({missing}): {controls}")
        if controls["mode_label"] != "Propose":
            raise AssertionError(f"mode chip does not reflect agent state: {controls}")
        if "Claude Code" not in controls["open_cc_text"]:
            raise AssertionError(f"open-in-Claude-Code button mislabeled: {controls}")
        return controls

    check("control surface present (header + composer row)", _control_surface)

    def _inline_image_data_uri_loads() -> dict[str, Any]:
        """show_image renders images as base64 data: URIs (InlineImage in
        Thread.tsx). The render path is preview-verified; the ONLY real-Anki
        risk is QtWebEngine refusing to LOAD a data: image under Anki's CSP,
        which a browser preview can't reveal (dogfood 2026-07-15). Probe that
        in isolation - inject one <img> and read naturalWidth - so it can't
        disturb the store/other checks."""
        png = (
            "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ"
            "AAAAC0lEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
        )
        _eval_js(
            dock.web,
            "(function(){var i=document.createElement('img');"
            "i.id='__cwyc_img_probe';i.style.position='fixed';i.style.left='-9999px';"
            "i.src='" + png + "';document.body.appendChild(i);return true;})();",
            DOM_TIMEOUT_MS,
            "inject data: image probe",
        )
        QTest.qWait(300)  # let QtWebEngine decode it
        info = _eval_js(
            dock.web,
            "(function(){var i=document.getElementById('__cwyc_img_probe');"
            "if(!i)return {missing:true};var r={complete:i.complete,"
            "naturalWidth:i.naturalWidth};i.remove();return r;})();",
            DOM_TIMEOUT_MS,
            "data: image load state",
        )
        if not isinstance(info, dict) or not info.get("naturalWidth"):
            raise AssertionError(
                f"Anki webview did not load a data: image (CSP blocking data:?): {info}"
            )
        return info

    check("data: URI images load in the Anki webview (show_image)", _inline_image_data_uri_loads)

    def _widget_sandbox_holds() -> dict[str, Any]:
        """render_widget end-to-end THROUGH THE REAL PATH (tool -> record_push
        -> bridge -> store -> WidgetCard iframe), then adversarial sandbox
        verification from BOTH sides:
        - inside: the injected widget's own script tries to reach
          window.parent.document (must throw: opaque origin) and to fetch()
          the network (must reject: CSP default-src 'none'), reporting via
          postMessage - the one channel a sandboxed frame legitimately has,
          which the app itself deliberately never listens on (display-only).
        - outside: the iframe must carry exactly sandbox="allow-scripts"
          (no allow-same-origin) and parent JS must NOT be able to reach
          iframe.contentDocument.
        A leak on any of these is a security regression, not a flake."""
        import chat_with_your_cards as cwyc
        from chat_with_your_cards.tools.widgets import render_widget

        # Probe-scoped message listener (the app has none, by design).
        _eval_js(
            dock.web,
            "(function(){window.__cwycWidgetProbe=null;"
            "window.addEventListener('message',function(e){window.__cwycWidgetProbe=e.data;});"
            "return true;})();",
            DOM_TIMEOUT_MS,
            "install widget probe listener",
        )
        widget_html = (
            "<div id='ok'>widget alive</div><script>"
            "var out={alive:true};"
            "try{void window.parent.document;out.parentBlocked=false;}"
            "catch(e){out.parentBlocked=true;}"
            "fetch('https://example.com/').then(function(){out.fetchBlocked=false;})"
            ".catch(function(){out.fetchBlocked=true;})"
            ".then(function(){window.parent.postMessage(out,'*');});"
            "</script>"
        )
        prior = bool(cwyc.state.config.get("widget_rendering", False))
        cwyc.state.config["widget_rendering"] = True
        try:
            off_result = None
            if not prior:
                # Also pin the consent gate: with the flag restored off, the
                # tool must refuse and surface the offer chip instead.
                cwyc.state.config["widget_rendering"] = False
                off_result = render_widget(
                    cwyc._ToolCtx(), {"html": "<b>x</b>", "title": "gate probe"}
                )
                if off_result.get("status") != "disabled_pending_user":
                    raise AssertionError(f"consent gate did not hold: {off_result}")
                cwyc.state.config["widget_rendering"] = True
            result = render_widget(
                cwyc._ToolCtx(), {"html": widget_html, "title": "sandbox probe"}
            )
            if result.get("status") != "displayed":
                raise AssertionError(f"render_widget (enabled) failed: {result}")
            QTest.qWait(900)  # let the iframe boot, run its script, postMessage
            info = _eval_js(
                dock.web,
                "(function(){"
                "var f=document.querySelector('[data-testid=inline-widget] iframe');"
                "if(!f)return {missing:true};"
                "var r={sandbox:f.getAttribute('sandbox'),"
                "cspInSrcdoc:(f.getAttribute('srcdoc')||'').indexOf(\"default-src 'none'\")>=0,"
                "report:window.__cwycWidgetProbe};"
                "try{r.contentReachable=!!(f.contentDocument&&f.contentDocument.body);}"
                "catch(e){r.contentReachable=false;}"
                "return r;})();",
                DOM_TIMEOUT_MS,
                "widget sandbox verdicts",
            )
            if not isinstance(info, dict) or info.get("missing"):
                raise AssertionError(f"widget iframe never rendered: {info}")
            if info.get("sandbox") != "allow-scripts":
                raise AssertionError(f"SANDBOX WEAKENED: sandbox={info.get('sandbox')!r}")
            if not info.get("cspInSrcdoc"):
                raise AssertionError("no-network CSP missing from widget srcdoc")
            if info.get("contentReachable"):
                raise AssertionError("SANDBOX LEAK: parent can reach iframe document")
            report = info.get("report")
            if not isinstance(report, dict) or not report.get("alive"):
                raise AssertionError(
                    f"widget script did not run / report (allow-scripts broken?): {info}"
                )
            if not report.get("parentBlocked"):
                raise AssertionError("SANDBOX LEAK: widget reached window.parent.document")
            if not report.get("fetchBlocked"):
                raise AssertionError("CSP LEAK: widget fetch() reached the network")
            return {"gate": off_result, "verdicts": info}
        finally:
            cwyc.state.config["widget_rendering"] = prior

    check("widget sandbox holds (opaque origin + no-network CSP)", _widget_sandbox_holds)

    def _mermaid_chunk_renders() -> dict[str, Any]:
        """Mermaid ships as a SEPARATE runtime-fetched ES chunk
        (web/next/mermaid.bundle.js; ui/src/mermaid.ts + vite.mermaid.config.ts,
        2026-07-16), which only a real Anki session can certify: the dynamic
        import() URL derives from document.currentScript.src of the stdHtml-
        injected bundle, and the chunk must come back over Anki's media server
        with a module-loadable response. Drive the REAL path end-to-end:
        dispatch a streamed ```mermaid fence into the store exactly as Python
        would, then wait for a sanitized SVG whose node labels survived
        (htmlLabels:false regression guard). A chunk 404/MIME failure shows up
        as the plain-code-block fallback, i.e. a timeout here."""
        fence = "```mermaid\\nflowchart LR\\n  A[Alpha] --> B[Beta]\\n```\\n"
        _eval_js(
            dock.web,
            "(function(){"
            "window.chatUI.dispatch({type:'text_delta', text:'A diagram:\\n\\n'});"
            f"window.chatUI.dispatch({{type:'text_delta', text:'{fence}'}});"
            "window.chatUI.dispatch({type:'text_delta', text:'\\nDone.'});"
            "window.chatUI.dispatch({type:'done'});"
            "return true;})();",
            DOM_TIMEOUT_MS,
            "dispatch mermaid fence",
        )

        def _svg_rendered() -> bool:
            state = _eval_js(
                dock.web,
                "(function(){var w=document.querySelector('.cwyc-mermaid');"
                "if(!w)return 'no-wrapper';"
                "var s=w.querySelector('svg');if(!s)return 'no-svg';"
                "return (w.textContent||'');})();",
                DOM_TIMEOUT_MS,
                "mermaid render state",
            )
            return isinstance(state, str) and "Alpha" in state and "Beta" in state

        _wait_until(_svg_rendered, STREAM_TIMEOUT_MS, "mermaid chunk to fetch and render SVG")
        info = _eval_js(
            dock.web,
            "(function(){var w=document.querySelector('.cwyc-mermaid');"
            "var s=w.querySelector('svg');"
            "return {labels:(w.textContent||'').trim().slice(0,80),"
            "scripts:w.querySelectorAll('script').length,"
            "foreign:w.querySelectorAll('foreignObject').length,"
            "svg:!!s};})();",
            DOM_TIMEOUT_MS,
            "mermaid sanitization state",
        )
        # DOMPurify SVG-profile invariants: no script, no foreignObject.
        if not isinstance(info, dict) or not info.get("svg"):
            raise AssertionError(f"mermaid SVG missing after wait: {info}")
        if info.get("scripts") or info.get("foreign"):
            raise AssertionError(f"UNSANITIZED mermaid output reached the DOM: {info}")
        return info

    check("mermaid chunk fetches and renders in real Anki", _mermaid_chunk_renders)

    def _collapse_expand_cycle() -> dict[str, Any]:
        """Drive the shell round-trip through the real webview controls: the
        header's collapse chevron shrinks the dock to the rail (animated,
        width pinned at RAIL_WIDTH), the rail click grows it back to the
        expanded width. Also asserts the settings panel opens (the cog is
        Settings now, with the Setup check folded inside - dogfood
        2026-07-13)."""
        from chat_with_your_cards.dock import RAIL_WIDTH

        _eval_js(
            dock.web,
            "(function() { document.querySelector('[data-testid=collapse]').click();"
            " return true; })();",
            DOM_TIMEOUT_MS,
            "collapse click",
        )
        _wait_until(
            lambda: not dock.expanded and dock._anim is None and dock.width() == RAIL_WIDTH,
            3_000,
            "dock to collapse to the rail",
        )
        rail_visible = _eval_js(
            dock.web,
            "(function() {"
            "  var rail = document.querySelector('[data-testid=rail]');"
            "  return rail ? getComputedStyle(rail).visibility : 'missing';"
            "})();",
            DOM_TIMEOUT_MS,
            "rail visibility",
        )
        if rail_visible != "visible":
            raise AssertionError(f"rail layer not visible when collapsed: {rail_visible}")
        _eval_js(
            dock.web,
            "(function() { document.querySelector('[data-testid=rail]').click();"
            " return true; })();",
            DOM_TIMEOUT_MS,
            "rail click",
        )
        _wait_until(
            lambda: dock.expanded and dock._anim is None
            and dock.width() >= dock.expand_target(),
            3_000,
            "dock to expand back",
        )
        _eval_js(
            dock.web,
            "(function() { document.querySelector('[data-testid=settings]').click();"
            " return true; })();",
            DOM_TIMEOUT_MS,
            "settings cog click",
        )

        def _panel_state() -> Any:
            # React renders the panel a tick after the click lands, so poll
            # instead of querying in the same eval (that raced and lost).
            return _eval_js(
                dock.web,
                "(function() {"
                "  var panel = document.querySelector('[data-testid=settings-panel]');"
                "  if (!panel) return null;"
                "  return {"
                "    panel: true,"
                "    restore: !!panel.querySelector('[data-testid=setting-restore-last-chat]'),"
                "    dock_side: !!panel.querySelector('[data-testid=setting-dock-right]'),"
                "    theme: !!panel.querySelector('[data-testid=setting-theme-teal]')"
                "        && !!panel.querySelector('[data-testid=setting-theme-indigo]')"
                "        && !!panel.querySelector('[data-testid=setting-theme-evergreen]'),"
                "    doctor: !!panel.querySelector('[data-testid=run-doctor]')"
                "  };"
                "})();",
                DOM_TIMEOUT_MS,
                "settings panel state",
            )

        _wait_until(lambda: bool(_panel_state()), DOM_TIMEOUT_MS, "settings panel to open")
        settings = _panel_state()
        if not (settings and settings.get("restore")
                and settings.get("dock_side") and settings.get("theme")
                and settings.get("doctor")):
            raise AssertionError(f"settings panel incomplete: {settings}")
        # The theme picker must actually swap the palette: clicking Evergreen
        # puts cwyc-theme-evergreen on <html>; Teal restores the default.
        _eval_js(
            dock.web,
            "(function(){document.querySelector"
            "('[data-testid=setting-theme-evergreen]').click();return true;})();",
            DOM_TIMEOUT_MS,
            "pick evergreen theme",
        )
        _wait_until(
            lambda: bool(_eval_js(
                dock.web,
                "document.documentElement.classList.contains('cwyc-theme-evergreen')",
                DOM_TIMEOUT_MS,
                "evergreen theme applied",
            )),
            DOM_TIMEOUT_MS,
            "evergreen theme class on <html>",
        )
        _eval_js(
            dock.web,
            "(function(){document.querySelector"
            "('[data-testid=setting-theme-teal]').click();return true;})();",
            DOM_TIMEOUT_MS,
            "restore teal theme",
        )
        _eval_js(
            dock.web,
            "(function() { document.querySelector('[data-testid=settings]').click();"
            " return true; })();",
            DOM_TIMEOUT_MS,
            "settings cog close click",
        )
        return {"rail_round_trip": True, "settings": settings}

    check("collapse/expand round-trip + settings panel", _collapse_expand_cycle)

    def _vim_mode_round_trip() -> dict[str, Any]:
        """The Settings vim toggle swaps the composer textarea for the
        CodeMirror vim editor and back - exercised inside real Anki's
        QtWebEngine (the CM bundle, fat-cursor CSS, and the settings
        round-trip through set_setting -> writeConfig -> settings push)."""

        def _kinds() -> Any:
            return _eval_js(
                dock.web,
                "(function() { return {"
                "  cm: document.querySelectorAll("
                "    '[data-testid=composer-input-vim] .cm-editor').length,"
                "  ta: document.querySelectorAll("
                "    '[data-testid=composer-input]').length"
                "}; })();",
                DOM_TIMEOUT_MS,
                "composer kind counts",
            )

        def _set_vim(on: bool) -> None:
            _eval_js(
                dock.web,
                "(function() { document.querySelector('[data-testid=settings]').click();"
                " return true; })();",
                DOM_TIMEOUT_MS,
                "settings cog click (vim)",
            )
            _wait_until(
                lambda: bool(
                    _eval_js(
                        dock.web,
                        "!!document.querySelector('[data-testid=setting-vim-mode]')",
                        DOM_TIMEOUT_MS,
                        "vim toggle present",
                    )
                ),
                DOM_TIMEOUT_MS,
                "settings panel with vim toggle",
            )
            desired = "true" if on else "false"
            _eval_js(
                dock.web,
                "(function() {"
                "  var box = document.querySelector('[data-testid=setting-vim-mode]');"
                "  if (box.checked !== " + desired + ") box.click();"
                "  return true; })();",
                DOM_TIMEOUT_MS,
                "vim toggle click",
            )
            _eval_js(
                dock.web,
                "(function() { document.querySelector('[data-testid=settings]').click();"
                " return true; })();",
                DOM_TIMEOUT_MS,
                "settings cog close (vim)",
            )

        start = _kinds()
        if start["ta"] != 1 or start["cm"] != 0:
            raise AssertionError(f"expected the plain textarea composer to start: {start}")
        _set_vim(True)
        _wait_until(
            lambda: _kinds()["cm"] == 1, DOM_TIMEOUT_MS, "CodeMirror vim composer to mount"
        )
        vim_on = _kinds()
        if vim_on["ta"] != 0:
            raise AssertionError(f"textarea still present with vim mode on: {vim_on}")
        _set_vim(False)
        _wait_until(
            lambda: _kinds()["ta"] == 1, DOM_TIMEOUT_MS, "textarea composer to return"
        )
        end = _kinds()
        if end["cm"] != 0:
            raise AssertionError(f"vim editor still present after toggling off: {end}")
        return {"vim_on": vim_on, "end": end}

    check("vim composer toggles on/off via Settings", _vim_mode_round_trip)

    def _vim_mode_persists() -> dict[str, Any]:
        """Close+reopen reloads the add-on config from disk, so the Settings vim
        toggle only survives a restart if its writeConfig actually lands in
        meta.json - not just runtime state. Drive the real handler ON, run the
        profile-close writer (which must not clobber it), then read the config
        back the three ways a cold start would: getConfig, the DEFAULT_CONFIG
        merge startup does, and straight off meta.json on disk."""
        import json as _json
        from pathlib import Path as _Path

        try:
            addon._set_setting({"key": "vim_mode", "value": True})
            QTest.qWait(50)
            # Profile-close persists dock geometry; it reads getConfig +
            # writes, so a stale snapshot here would silently drop vim_mode.
            addon._save_dock_width()
            QTest.qWait(20)

            live = bool((mw.addonManager.getConfig(ADDON_PACKAGE) or {}).get("vim_mode"))
            fresh = dict(addon.DEFAULT_CONFIG)  # same merge as add-on startup
            fresh.update(mw.addonManager.getConfig(ADDON_PACKAGE) or {})
            reload_on = bool(fresh.get("vim_mode"))
            meta_path = _Path(mw.addonManager.addonsFolder(ADDON_PACKAGE)) / "meta.json"
            disk = _json.loads(meta_path.read_text()) if meta_path.exists() else {}
            disk_on = bool((disk.get("config") or {}).get("vim_mode"))
        finally:
            # Leave the profile stock for later checks/screenshots.
            addon._set_setting({"key": "vim_mode", "value": False})
            QTest.qWait(50)

        if not (live and reload_on and disk_on):
            raise AssertionError(
                "vim_mode did not persist across a simulated restart: "
                f"getConfig={live} startup-merge={reload_on} meta.json={disk_on}"
            )
        return {"getConfig": live, "startup_merge": reload_on, "meta_json": disk_on}

    check("vim_mode persists across a simulated restart", _vim_mode_persists)

    # From a wrapped line, measure whether a bare `0` moves by VISUAL line
    # (start of the current screen row) or LOGICAL line (start of the whole
    # line). Requires window.cwycVimView (exposed in every build).
    _VIM_0_PROBE = r"""(function(){
      var v = window.cwycVimView;
      if(!v) return {err:'no view'};
      function press(k,code,kc,shift){ v.contentDOM.dispatchEvent(
        new KeyboardEvent('keydown',{key:k,code:code,keyCode:kc,which:kc,shiftKey:!!shift,bubbles:true,cancelable:true})); }
      var text='alpha bravo charlie delta echo foxtrot golf hotel india juliet kilo lima mike november oscar papa quebec';
      v.dispatch({changes:{from:0,to:v.state.doc.length,insert:text}, selection:{anchor:0}});
      v.focus();
      var r0=Math.round(v.coordsAtPos(0).top), mid=null;
      for(var i=0;i<=v.state.doc.length;i++){ if(Math.round(v.coordsAtPos(i).top)>r0){ mid=i+3; break; } }
      if(mid===null) return {err:'line did not wrap', width: v.contentDOM.clientWidth};
      press('Escape','Escape',27,false);
      v.dispatch({selection:{anchor:mid}}); v.focus();
      var midTop=Math.round(v.coordsAtPos(mid).top);
      press('0','Digit0',48,false);
      var h=v.state.selection.main.head, top=Math.round(v.coordsAtPos(h).top);
      return {mid:mid, midTop:midTop, row0Top:r0, head:h, headTop:top,
              visual:(top===midTop && h<mid), logical:(top===r0 && h===0)};
    })();"""

    def _vim_mappings_take_effect() -> dict[str, Any]:
        """Dogfood 2026-07-14: (a) `0`/`$` remaps set in config seemed not to
        work in real Anki, and (b) "does changing vimrc need a restart?". This
        verifies BOTH: mappings applied through the real config path move by
        visual line, AND they now reload LIVE via _on_config_updated
        (setConfigUpdatedAction) - removing a mapping takes effect with no Anki
        restart and no editor remount (Vim.mapclear + re-apply)."""

        def reload_with(maps: list[list[str]]) -> None:
            cfg = mw.addonManager.getConfig(ADDON_PACKAGE) or {}
            cfg["vim_mappings"] = maps
            cfg["vim_mode"] = True
            mw.addonManager.writeConfig(ADDON_PACKAGE, cfg)
            addon._on_config_updated()  # the live path: reload + re-push, no restart
            QTest.qWait(180)

        try:
            reload_with([
                ["0", "g0", "normal"], ["0", "g0", "visual"],
                ["$", "g$", "normal"], ["$", "g$", "visual"],
            ])
            _wait_until(
                lambda: bool(
                    _eval_js(dock.web, "!!window.cwycVimView", DOM_TIMEOUT_MS, "vim view present")
                ),
                DOM_TIMEOUT_MS,
                "vim editor view to mount",
            )
            QTest.qWait(120)
            on = _eval_js(dock.web, _VIM_0_PROBE, DOM_TIMEOUT_MS, "0 with mapping")
            # LIVE reload with the mapping removed: Vim.mapclear must drop it
            # from the SAME editor instance (no remount, no restart).
            reload_with([])
            off = _eval_js(dock.web, _VIM_0_PROBE, DOM_TIMEOUT_MS, "0 after live removal")
        finally:
            cfg = mw.addonManager.getConfig(ADDON_PACKAGE) or {}
            cfg["vim_mode"] = False
            cfg["vim_mappings"] = []
            mw.addonManager.writeConfig(ADDON_PACKAGE, cfg)
            addon._on_config_updated()
            QTest.qWait(80)

        if not isinstance(on, dict) or on.get("err"):
            raise AssertionError(f"could not measure mapped 0: {on}")
        if not on.get("visual"):
            raise AssertionError(
                f"config `0`->`g0` did NOT apply in real Anki (bare 0 went logical): {on}"
            )
        if not isinstance(off, dict) or off.get("err"):
            raise AssertionError(f"could not measure 0 after live removal: {off}")
        if not off.get("logical"):
            raise AssertionError(
                "live config reload failed: after removing `0`->`g0` via "
                f"_on_config_updated (no restart), bare 0 was still visual: {off}"
            )
        return {"with_mapping": on, "after_live_removal": off}

    check(
        "vim key-mappings apply in real Anki + reload live (no restart)",
        _vim_mappings_take_effect,
    )

    def _thin_window_grows_and_restores() -> dict[str, Any]:
        """A window too thin to give the dock a usable column grows to fit on
        expand and shrinks back on collapse - unless the user resized it, whose
        size then wins. Defensive: if Anki's own minimum window width won't let
        us make the window thin enough to need a grow, the grow assertions are
        skipped (the path is a safe no-op on wide windows)."""
        from aqt.qt import QRect

        from chat_with_your_cards.dock import CENTRAL_MIN, MIN_DOCK_WIDTH

        def _set_window_width(w: int) -> int:
            geo = QRect(mw.geometry())
            geo.setWidth(w)
            mw.setGeometry(geo)
            QTest.qWait(60)
            return mw.width()

        dock.set_expanded(False, animate=False)
        QTest.qWait(60)
        # Too thin: room for the central column plus far less than the dock floor.
        thin = _set_window_width(CENTRAL_MIN + 80)
        result: dict[str, Any] = {"thin_window": thin}
        if thin - CENTRAL_MIN >= MIN_DOCK_WIDTH:
            # The window would not go thin enough (Anki min size); can't exercise
            # the grow. Still assert expand doesn't break, then restore state.
            dock.set_expanded(True, animate=False)
            QTest.qWait(80)
            result["skipped"] = "window min width too large to force a thin state"
            return result

        dock.set_expanded(True, animate=False)
        QTest.qWait(100)
        grew = mw.width()
        dock_w = dock.width()
        if grew <= thin:
            raise AssertionError(f"window did not grow on expand: {thin} -> {grew}")
        if dock_w < MIN_DOCK_WIDTH:
            raise AssertionError(f"dock still below its floor after grow: {dock_w}")

        dock.set_expanded(False, animate=False)
        QTest.qWait(100)
        restored = mw.width()
        if abs(restored - thin) > 6:
            raise AssertionError(f"window did not restore on collapse: {thin} -> {restored}")

        # A manual resize between grow and collapse must survive the collapse.
        dock.set_expanded(True, animate=False)
        QTest.qWait(100)
        user_w = _set_window_width(mw.width() + 140)
        dock.set_expanded(False, animate=False)
        QTest.qWait(100)
        if abs(mw.width() - user_w) > 6:
            raise AssertionError(f"collapse clobbered a manual resize: {user_w} -> {mw.width()}")

        # Leave a comfortable window + expanded dock for later checks/screenshots.
        _set_window_width(CENTRAL_MIN + MIN_DOCK_WIDTH + 240)
        dock.set_expanded(True, animate=False)
        QTest.qWait(80)
        result.update({"grew": grew, "dock": dock_w, "restored": restored, "manual_kept": user_w})
        return result

    check("thin window grows on expand and restores on collapse", _thin_window_grows_and_restores)

    def _focus_toggle_cycles() -> None:
        # Ctrl+J cycles the shell (2026-07-13): with focus in the chat, the
        # chord collapses to the rail and hands focus back; from the rail it
        # expands + focuses again.
        from chat_with_your_cards.dock import RAIL_WIDTH

        dock.web.setFocus()
        QTest.qWait(100)
        addon.toggle_chat_focus()
        _wait_until(
            lambda: not dock.expanded and dock._anim is None and dock.width() == RAIL_WIDTH,
            3_000,
            "dock to collapse on second toggle",
        )
        focused = mw.app.focusWidget()
        if focused is not None and dock.isAncestorOf(focused):
            raise AssertionError("focus stayed inside the dock after collapse toggle")
        addon.toggle_chat_focus()
        _wait_until(
            lambda: dock.expanded and dock._anim is None
            and dock.width() >= dock.expand_target(),
            3_000,
            "dock to expand again on third toggle",
        )

    check("toggle cycles: collapse from chat focus, expand back", _focus_toggle_cycles)

    def _collapse_closes_open_menu() -> dict[str, Any]:
        """Regression (dogfood 2026-07-14): a menu left open must not survive a
        dock collapse - it used to reappear on re-expand, letting you toggle the
        dock with Settings still showing. Collapsing now broadcasts a dismiss
        (store.ts dock_state -> dismissAllPopovers), so the panel vanishes."""
        dock.set_expanded(True)
        _wait_until(lambda: dock.expanded and dock._anim is None, 3_000, "dock expanded")
        _eval_js(
            dock.web,
            "(function(){document.querySelector('[data-testid=settings]').click();"
            "return true;})();",
            DOM_TIMEOUT_MS,
            "open settings before collapse",
        )
        _wait_until(
            lambda: bool(_eval_js(
                dock.web,
                "document.querySelectorAll('[data-testid=settings-panel]').length",
                DOM_TIMEOUT_MS,
                "settings panel present",
            )),
            DOM_TIMEOUT_MS,
            "settings panel to open",
        )
        dock.set_expanded(False)
        _wait_until(
            lambda: _eval_js(
                dock.web,
                "document.querySelectorAll('[data-testid=settings-panel]').length",
                DOM_TIMEOUT_MS,
                "settings panel gone",
            ) == 0,
            3_000,
            "open menu to close when the dock collapses",
        )
        dock.set_expanded(True)
        _wait_until(lambda: dock.expanded and dock._anim is None, 3_000, "dock re-expanded")
        return {"menu_dismissed_on_collapse": True}

    check("collapsing the dock closes any open menu", _collapse_closes_open_menu)

    def _proposal_round_trip() -> dict[str, Any]:
        """Scripted propose -> interaction card -> approve -> real note in the
        collection, and the card flips to the resolved read-only state.

        Selector history: `create` proposals render through the shared
        interaction renderer since 1c73311 (data-testid=interaction-card,
        action buttons by data-action-id, badge in .eui-status) - this check
        was red from then until the interaction-ui-react package swap
        (2026-07-16) because it still probed the pre-1c73311
        `proposal-card`/`proposal-approve` testids. Status labels come from
        CWYC's own adapter now (interactionAdapter.ts BADGES): "Pending
        review" while actionable, "Completed" once accepted; resolved-ness =
        the host passes no actions, so the footer (and every
        [data-action-id]) disappears."""
        _send_message(dock.web, PROPOSE_MESSAGE)

        def _card_rendered() -> bool:
            return bool(
                _eval_js(
                    dock.web,
                    "document.querySelectorAll('[data-testid=interaction-card]').length",
                    DOM_TIMEOUT_MS,
                    "interaction card count",
                )
            )

        _wait_until(_card_rendered, STREAM_TIMEOUT_MS, "proposal card to render")
        card = _eval_js(
            dock.web,
            "(function() {"
            "  var p = document.querySelector('[data-testid=interaction-card]');"
            "  return {"
            "    title: (p.querySelector('h2') || {}).textContent,"
            "    eyebrow: (p.querySelector('.eui-eyebrow') || {}).textContent,"
            "    status: (p.querySelector('.eui-status') || {}).textContent,"
            "    fields: p.querySelectorAll('.eui-field').length,"
            "    approve: p.querySelectorAll('[data-action-id=approve]').length,"
            "    edit_button: p.querySelectorAll('[data-action-id=revise]').length,"
            "    editable_fields: p.querySelectorAll('.eui-field-value-editable').length,"
            "    reject: p.querySelectorAll('[data-action-id=reject]').length,"
            "    preview_tabs: p.querySelectorAll('.cwyc-preview-tab').length"
            "  };"
            "})();",
            DOM_TIMEOUT_MS,
            "proposal card state",
        )
        if card["title"] != "Create Anki note" or card["fields"] < 2 or card["approve"] != 1:
            raise AssertionError(f"proposal card malformed: {card}")
        if card["status"] != "Pending review":
            raise AssertionError(f"unexpected pending badge: {card}")
        if card["reject"] != 1:
            raise AssertionError(f"proposal card missing reject control: {card}")
        # Click-to-edit, not a separate Edit button: no revise action, and each
        # field value is its own editable button (task #27).
        if card["edit_button"] != 0:
            raise AssertionError(f"unexpected Edit button (should be click-to-edit): {card}")
        if card["editable_fields"] < 2:
            raise AssertionError(f"click-to-edit field buttons missing: {card}")
        if card["preview_tabs"] != 2:
            raise AssertionError(f"expected Front/Back preview tabs: {card}")

        before_ids = set(mw.col.find_notes('tag:"ai-created"'))
        _eval_js(
            dock.web,
            "(function() { document.querySelector('[data-action-id=approve]').click(); "
            "return true; })();",
            DOM_TIMEOUT_MS,
            "proposal approve click",
        )

        def _note_created() -> bool:
            return len(set(mw.col.find_notes('tag:"ai-created"')) - before_ids) == 1

        _wait_until(_note_created, DOM_TIMEOUT_MS, "accepted note to appear")
        (note_id,) = set(mw.col.find_notes('tag:"ai-created"')) - before_ids
        note = mw.col.get_note(note_id)
        session_tag = state.proposals.session_tag
        if session_tag not in note.tags:
            raise AssertionError(f"session tag missing: {note.tags}")

        # The card must flip to the resolved read-only state: badge
        # "Completed" (the adapter's accepted label, same string the old
        # combined renderer showed) and EVERY action button gone - the host
        # passes an empty actions list once the proposal leaves `pending`, so
        # the renderer drops the whole footer. That proves the same accepted
        # transition the classic check asserted, on the surface that actually
        # exists now.
        def _resolved_state() -> Any:
            return _eval_js(
                dock.web,
                "(function() {"
                "  var p = document.querySelector('[data-testid=interaction-card]');"
                "  if (!p) return null;"
                "  return {"
                "    status: (p.querySelector('.eui-status') || {}).textContent,"
                "    actions: p.querySelectorAll('[data-action-id]').length"
                "  };"
                "})();",
                DOM_TIMEOUT_MS,
                "resolved proposal state",
            )

        def _resolved_ok() -> bool:
            r = _resolved_state()
            return bool(r and r.get("status") == "Completed" and r.get("actions") == 0)

        _wait_until(_resolved_ok, DOM_TIMEOUT_MS, "proposal card to resolve to Completed")
        resolved = _resolved_state()
        if not resolved or resolved["status"] != "Completed" or resolved["actions"] != 0:
            raise AssertionError(f"proposal did not resolve in the UI: {resolved}")
        return {"note_id": note_id, "card": card, "resolved": resolved}

    proposal_info = check("proposal accept round-trip", _proposal_round_trip)

    def _proposal_media_round_trip() -> dict[str, Any]:
        """Task #21 end-to-end in real Anki: a REAL wav staged through
        ProposalManager.submit_create's media arg -> the schema-1.1 player
        strip renders on the review card (.eui-media audio, data: src) and
        QtWebEngine actually DECODES it (duration > 0 - the one claim only a
        real webview can prove) -> approve through the real button -> the
        file lands in collection.media and the note's field keeps its
        [sound:] marker. Synthesizes the wav with the stdlib so the probe
        needs no fixture assets."""
        import io
        import math
        import struct
        import tempfile
        import wave

        buf = io.BytesIO()
        with wave.open(buf, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(8000)
            frames = b"".join(
                struct.pack("<h", int(12000 * math.sin(2 * math.pi * 440 * i / 8000)))
                for i in range(1600)  # 0.2s of A4
            )
            wav.writeframes(frames)
        tone_path = os.path.join(tempfile.gettempdir(), "cwyc-probe-tone.wav")
        with open(tone_path, "wb") as handle:
            handle.write(buf.getvalue())

        result = state.proposals.submit_create(
            {
                "note_type": "Basic",
                "deck": "Default",
                "fields": {
                    "Front": "probe audio [sound:cwyc-probe-tone.wav]",
                    "Back": "hears a beep",
                },
                "rationale": "gui-smoke media round-trip",
                "media": [{"path": tone_path, "filename": "cwyc-probe-tone.wav"}],
            }
        )
        if result.get("status") != "pending_user_review":
            raise AssertionError(f"media proposal did not stage: {result}")
        pid = result["proposal_id"]

        def _strip_state() -> Any:
            return _eval_js(
                dock.web,
                "(function() {"
                "  var cards = document.querySelectorAll('[data-testid=interaction-card]');"
                "  var p = cards[cards.length - 1];"
                "  if (!p) return null;"
                "  var a = p.querySelector('.eui-media audio');"
                "  if (!a) return {audio: 0};"
                "  return {audio: 1, src: (a.getAttribute('src') || '').slice(0, 15),"
                "          duration: a.duration || 0, label: a.getAttribute('aria-label')};"
                "})();",
                DOM_TIMEOUT_MS,
                "media strip state",
            )

        def _strip_decodable() -> bool:
            s = _strip_state()
            return bool(s and s.get("audio") and s.get("duration", 0) > 0)

        _wait_until(_strip_decodable, STREAM_TIMEOUT_MS, "player strip with decodable audio")
        strip = _strip_state()
        if not str(strip.get("src", "")).startswith("data:audio/"):
            raise AssertionError(f"player src is not a data: URI: {strip}")
        if strip.get("label") != "cwyc-probe-tone.wav":
            raise AssertionError(f"player accessibility label wrong: {strip}")

        _eval_js(
            dock.web,
            "(function() {"
            "  var cards = document.querySelectorAll('[data-testid=interaction-card]');"
            "  cards[cards.length - 1].querySelector('[data-action-id=approve]').click();"
            "  return true; })();",
            DOM_TIMEOUT_MS,
            "media proposal approve click",
        )

        def _media_imported() -> bool:
            return bool(mw.col.media.have("cwyc-probe-tone.wav"))

        _wait_until(_media_imported, DOM_TIMEOUT_MS, "audio to land in collection.media")
        found = mw.col.find_notes('"probe audio"')
        if len(found) != 1:
            raise AssertionError(f"media note not found: {found}")
        note = mw.col.get_note(found[0])
        if "[sound:cwyc-probe-tone.wav]" not in note["Front"]:
            raise AssertionError(f"sound marker missing/rewritten unexpectedly: {note['Front']}")
        from chat_with_your_cards import USER_FILES as _uf

        staging_dir = _uf / "staging" / pid
        if staging_dir.exists():
            raise AssertionError("staging dir not freed after import")
        return {"strip": strip, "note_id": int(found[0])}

    check("proposal media: staged wav -> player strip -> collection.media",
          _proposal_media_round_trip)

    def _existing_media_preview() -> dict[str, Any]:
        """Task #25 in real Anki: a proposal that only REFERENCES media
        already in the collection ([sound:cwyc-probe-tone.wav], imported by
        the round-trip check above) - with NO staged attachment - still shows
        a playable strip. _attach_preview_media reads the real col.media dir
        into a data: URI and QtWebEngine must decode it (duration > 0)."""
        if not mw.col.media.have("cwyc-probe-tone.wav"):
            raise AssertionError("precondition: tone not in collection.media")
        result = state.proposals.submit_create(
            {
                "note_type": "Basic",
                "deck": "Default",
                "fields": {
                    "Front": "reuse existing [sound:cwyc-probe-tone.wav]",
                    "Back": "plays the imported tone",
                },
                "rationale": "gui-smoke existing-media preview",
            }
        )
        if result.get("status") != "pending_user_review":
            raise AssertionError(f"existing-media proposal did not stage: {result}")

        def _strip() -> Any:
            return _eval_js(
                dock.web,
                "(function() {"
                "  var cards = document.querySelectorAll('[data-testid=interaction-card]');"
                "  var p = cards[cards.length - 1];"
                "  if (!p) return null;"
                "  var a = p.querySelector('.eui-media audio');"
                "  if (!a) return {audio: 0};"
                "  return {audio: 1, src: (a.getAttribute('src') || '').slice(0, 15),"
                "          duration: a.duration || 0, label: a.getAttribute('aria-label')};"
                "})();",
                DOM_TIMEOUT_MS,
                "existing-media strip state",
            )

        def _decodable() -> bool:
            s = _strip()
            return bool(s and s.get("audio") and s.get("duration", 0) > 0)

        _wait_until(_decodable, STREAM_TIMEOUT_MS, "existing-media strip with decodable audio")
        strip = _strip()
        if not str(strip.get("src", "")).startswith("data:audio/"):
            raise AssertionError(f"existing-media player src not a data: URI: {strip}")
        if strip.get("label") != "cwyc-probe-tone.wav":
            raise AssertionError(f"existing-media label wrong: {strip}")
        _eval_js(
            dock.web,
            "(function() {"
            "  var cards = document.querySelectorAll('[data-testid=interaction-card]');"
            "  cards[cards.length - 1].querySelector('[data-action-id=reject]').click();"
            "  return true; })();",
            DOM_TIMEOUT_MS,
            "existing-media proposal reject click",
        )
        return {"strip": strip}

    check("proposal media: existing [sound:] ref -> preview strip (task #25)",
          _existing_media_preview)

    def _hover_geometry_stable() -> dict[str, Any]:
        """Hovering a header button must change ONLY its background - never
        padding, font, size, or radius. A geometry change on hover shrinks the
        control out from under the cursor and starts a flicker loop (the
        `#cwyc-root button:hover` specificity trap, real Anki, 2026-07-12).
        This drives the REAL mouse and compares computed styles + rect."""
        from aqt.qt import QPoint

        web = state.dock.web
        target = web.focusProxy() or web
        diffs = {}
        for tid in ("open-cc", "new-chat", "settings"):
            QTest.mouseMove(target, QPoint(2, 2))
            QTest.qWait(150)
            rest = _measure(web, tid)
            r = _eval_js(
                web,
                "(function(){var b=document.querySelector('[data-testid=" + tid + "]');"
                "var q=b.getBoundingClientRect();return {x:Math.round(q.left+q.width/2),"
                "y:Math.round(q.top+q.height/2)};})();",
                DOM_TIMEOUT_MS,
                f"rect {tid}",
            )
            QTest.mouseMove(target, QPoint(int(r["x"]), int(r["y"])))
            QTest.qWait(250)
            hover = _measure(web, tid)
            changed = {k for k in rest if rest.get(k) != hover.get(k)}
            geom = changed - {"backgroundColor"}
            if geom:
                raise AssertionError(
                    f"{tid} changes {sorted(geom)} on hover (not just background): "
                    f"{ {k:[rest[k],hover[k]] for k in geom} } - hover flicker risk"
                )
            diffs[tid] = sorted(changed)
        return diffs

    check("hover changes background only (no geometry)", _hover_geometry_stable)

    # Real-collection stress tests: the collection-mutation paths driven
    # against the real disposable collection, not the unit tests' fakes.
    _collection_flow_checks(state, check)

    return {
        "ok": True,
        "checks": checks,
        "stream": stream_info,
        "dom": dom_info,
        "proposal": proposal_info,
        "anki_version": getattr(mw, "appVersion", None),
    }


_MEASURE_JS = """
(function(tid){
  var b = document.querySelector('[data-testid='+tid+']');
  if(!b) return null;
  var cs = getComputedStyle(b), r = b.getBoundingClientRect();
  var p = ['width','height','paddingTop','paddingRight','paddingBottom','paddingLeft',
    'marginTop','marginRight','marginBottom','marginLeft','borderTopWidth','borderRightWidth',
    'borderBottomWidth','borderLeftWidth','fontSize','fontFamily','fontWeight','lineHeight',
    'boxSizing','boxShadow','transform','transition','minWidth','minHeight','borderRadius',
    'backgroundColor'];
  var o = {rectW: +r.width.toFixed(2), rectH: +r.height.toFixed(2)};
  p.forEach(function(k){ o[k] = cs[k]; });
  return o;
})(%s);
"""


def _measure(web: Any, testid: str) -> Any:
    return _eval_js(web, _MEASURE_JS % json.dumps(testid), DOM_TIMEOUT_MS, f"measure {testid}")


def _hover_grab(result: dict[str, Any], light_path: Path, testid: str) -> None:
    """Reproduce hover-ONLY behavior in real Anki and MEASURE it. A static grab
    can't show flicker or a geometry hover-loop; the computed-style + rect DIFF
    between rest and hover names the exact property that changes (dogfood
    2026-07-12: 'changes size, shape, font on hover; flickers on the divider')."""
    import importlib

    from aqt.qt import QPoint

    addon = importlib.import_module(ADDON_PACKAGE)
    web = addon.state.dock.web

    # 1. rest snapshot (mouse parked far away)
    target = web.focusProxy() or web
    QTest.mouseMove(target, QPoint(2, 2))
    QTest.qWait(200)
    rest = _measure(web, testid)
    if not rest:
        result[f"hover_{testid}_error"] = "control not found"
        return

    # 2. hover snapshot
    rect = _eval_js(
        web,
        "(function(){var b=document.querySelector('[data-testid=" + testid + "]');"
        "var r=b.getBoundingClientRect();"
        "return {x:Math.round(r.left+r.width/2),y:Math.round(r.top+r.height/2)};})();",
        DOM_TIMEOUT_MS,
        f"hover rect {testid}",
    )
    QTest.mouseMove(target, QPoint(int(rect["x"]), int(rect["y"])))
    QTest.qWait(400)
    hover = _measure(web, testid)

    # 3. the diff IS the diagnosis
    diff = {k: [rest.get(k), hover.get(k)] for k in rest if rest.get(k) != hover.get(k)}
    result[f"hoverdiff_{testid}"] = diff or "identical (geometry stable)"

    hover_path = light_path.with_name(f"hover-{testid}.png")
    if mw.grab().save(str(hover_path), "PNG"):
        result[f"hover_{testid}"] = str(hover_path)


def _stage_public_demo_collection() -> dict[str, Any]:
    """Build the synthetic collection used by the public user-story captures."""
    assert mw is not None
    current_deck = "CWYC Demo::Current"
    related_deck = "CWYC Demo::Related"
    current_nid = _new_note(
        "Why does uniform continuity require one delta that works everywhere?",
        tags=["analysis", "continuity", "quantifiers"],
        deck=current_deck,
        back=(
            "Pointwise continuity may choose a different delta at each point. "
            "Uniform continuity chooses delta before the point, so that one "
            "delta must work across the whole domain."
        ),
    )
    related = [
        (
            "Continuity at a point: the epsilon–delta game",
            "For every epsilon and every chosen point, there exists a delta.",
            ["analysis", "continuity"],
        ),
        (
            "Open covers and finite subcovers",
            "A space is compact when every open cover has a finite subcover.",
            ["analysis", "compactness"],
        ),
        (
            "Heine–Cantor theorem",
            "A continuous function on a compact space is uniformly continuous.",
            ["analysis", "continuity", "compactness"],
        ),
        (
            "Why (0, 1) is not compact",
            "The cover (1/n, 1) has no finite subcover of the interval.",
            ["analysis", "compactness"],
        ),
    ]
    related_ids = [
        _new_note(front, tags=tags, deck=related_deck, back=back)
        for front, back, tags in related
    ]

    mw.col.decks.select(mw.col.decks.id(current_deck))
    mw.reset()
    mw.onOverview()
    QTest.qWait(250)
    mw.moveToState("review")
    _wait_until(
        lambda: getattr(mw, "state", "") == "review"
        and getattr(getattr(mw, "reviewer", None), "card", None) is not None,
        3_000,
        "synthetic current card to open in reviewer",
    )
    reviewer = getattr(mw, "reviewer", None)
    card = getattr(reviewer, "card", None) if reviewer else None
    if card is None or int(card.nid) != current_nid:
        raise AssertionError("could not open the synthetic current card in the reviewer")
    reviewer._showQuestion()
    reviewer.web.repaint()
    mw.repaint()
    QTest.qWait(800)

    mw.resize(1280, 760)
    QTest.qWait(250)
    return {
        "current_note": current_nid,
        "related_notes": related_ids,
        "current_deck": current_deck,
        "related_deck": related_deck,
        "anki_state": mw.state,
    }


def _frame_public_story(web: Any, *, bottom: bool) -> None:
    _eval_js(
        web,
        "(function() {"
        "  var vp = document.querySelector('.cwyc-viewport');"
        "  if (!vp) vp = document.querySelector('[class*=viewport]');"
        "  if (vp) {"
        + (
            "    var card = document.querySelector('[data-testid=proposal-card]');"
            "    if (card) {"
            "      var cr = card.getBoundingClientRect();"
            "      var vr = vp.getBoundingClientRect();"
            "      vp.scrollTop += cr.top - vr.top - 120;"
            "    } else { vp.scrollTop = vp.scrollHeight; }"
            if bottom
            else "    vp.scrollTop = 0;"
        )
        + "  }"
        "  return true;"
        "})();",
        DOM_TIMEOUT_MS,
        "frame public screenshot transcript",
    )
    QTest.qWait(350)


def _capture_public_story(
    controller: Any,
    web: Any,
    *,
    message: str,
    path: Path,
    bottom: bool = False,
) -> None:
    controller.new_chat()
    controller.push_context_chip()
    _send_message(web, message)
    _wait_until(
        lambda: not controller.streaming
        and any(type(event).__name__ == "Done" for event in controller.event_log),
        STREAM_TIMEOUT_MS,
        f"public story stream for {path.name}",
    )
    QTest.qWait(300)
    _frame_public_story(web, bottom=bottom)
    if not mw.grab().save(str(path), "PNG"):
        raise RuntimeError(f"failed to save public story screenshot to {path}")


def _save_public_story_screenshots(result: dict[str, Any], light_path: Path) -> None:
    addon = importlib.import_module(ADDON_PACKAGE)
    dock = addon.state.dock
    dock.set_expanded(True, animate=False)
    _wait_until(lambda: dock.expanded and dock._anim is None, 3_000, "public dock expand")
    collection = _stage_public_demo_collection()
    # Earlier destructive tests intentionally surface notices. Let the UI's
    # six-second notice timer expire before taking publicity captures.
    QTest.qWait(6_500)
    light_path.parent.mkdir(parents=True, exist_ok=True)

    explain_path = light_path.with_name(light_path.stem + "-explain.png")
    proposal_path = light_path.with_name(light_path.stem + "-proposal.png")
    _capture_public_story(
        addon.state.controller,
        dock.web,
        message=PUBLIC_EXPLAIN_MESSAGE,
        path=explain_path,
    )
    _capture_public_story(
        addon.state.controller,
        dock.web,
        message=PUBLIC_RELATED_MESSAGE,
        path=light_path,
    )
    _capture_public_story(
        addon.state.controller,
        dock.web,
        message=PUBLIC_PROPOSE_MESSAGE,
        path=proposal_path,
        bottom=True,
    )
    result["public_demo_collection"] = collection
    result["screenshot_explain"] = str(explain_path)
    result["screenshot"] = str(light_path)
    result["screenshot_proposal"] = str(proposal_path)


def _save_screenshots(result: dict[str, Any]) -> None:
    path = os.environ.get("ANKI_ADDON_WORKBENCH_SCREENSHOT")
    if not path or mw is None:
        return

    # README/publicity captures recreate three user stories in a synthetic
    # collection after the destructive smoke assertions finish. The early
    # return avoids producing diagnostic dark/rail variants of the final story.
    if os.environ.get("CWYC_PUBLIC_SCREENSHOT"):
        _save_public_story_screenshots(result, Path(path))
        return

    light_path = Path(path)
    light_path.parent.mkdir(parents=True, exist_ok=True)
    QTest.qWait(200)
    if not mw.grab().save(str(light_path), "PNG"):
        raise RuntimeError(f"failed to save screenshot to {light_path}")
    result["screenshot"] = str(light_path)

    # The collapsed rail is a first-class UI state now: grab it too.
    try:
        addon = importlib.import_module(ADDON_PACKAGE)
        chat_dock = addon.state.dock
        chat_dock.set_expanded(False)
        _wait_until(lambda: chat_dock._anim is None, 3_000, "rail collapse for screenshot")
        QTest.qWait(400)
        rail_path = light_path.with_name(light_path.stem + "-rail.png")
        if mw.grab().save(str(rail_path), "PNG"):
            result["screenshot_rail"] = str(rail_path)
        chat_dock.set_expanded(True)
        _wait_until(lambda: chat_dock._anim is None, 3_000, "re-expand after rail screenshot")
        QTest.qWait(300)
    except Exception as exc:
        result["screenshot_rail_error"] = str(exc)

    if os.environ.get("CWYC_SMOKE_HOVER"):
        for tid in ("open-cc", "new-chat", "settings"):
            try:
                _hover_grab(result, light_path, tid)
            except Exception as exc:
                result[f"hover_{tid}_error"] = str(exc)

    try:
        from aqt.theme import Theme, theme_manager

        mw.pm.set_theme(Theme.DARK)
        theme_manager.apply_style()
        QTest.qWait(700)
        addon = importlib.import_module(ADDON_PACKAGE)
        result["dark_page_state"] = _eval_js(
            addon.state.dock.web,
            # appBg is the tokens verdict: dark charcoal here + light pixels
            # in the grab = stale QtWebEngine tiles; light here = OUR css bug.
            "(function() { var app = document.querySelector('.cwyc-app');"
            " return {"
            "  htmlClass: document.documentElement.className,"
            "  bodyClass: document.body.className,"
            "  canvas: getComputedStyle(document.documentElement)"
            "    .getPropertyValue('--canvas').trim(),"
            "  appBg: app ? getComputedStyle(app).backgroundColor : null"
            "}; })();",
            DOM_TIMEOUT_MS,
            "dark page state",
        )
        # Force a full recomposite: QtWebEngine leaves stale (light) tiles
        # after a pure CSS-class flip, and mw.grab() would photograph them.
        _eval_js(
            addon.state.dock.web,
            "(function() {"
            "  document.body.style.display = 'none';"
            "  void document.body.offsetHeight;"
            "  document.body.style.display = '';"
            # Display-toggling alone left stale light tiles in the large,
            # lazily-rasterized message list (observed on Anki 25.09,
            # 2026-07-12); jiggle the scroll viewport to invalidate them too.
            "  var vp = document.querySelector("
            "    '.cwyc-thread, .cwyc-messages, [class*=viewport]');"
            "  if (vp) { vp.scrollTop += 1; vp.scrollTop -= 1; }"
            "  return true;"
            "})();",
            DOM_TIMEOUT_MS,
            "dark repaint force",
        )
        QTest.qWait(1500)
        dark_path = light_path.with_name(light_path.stem + "-dark.png")
        if not mw.grab().save(str(dark_path), "PNG"):
            raise RuntimeError(f"failed to save screenshot to {dark_path}")
        result["screenshot_dark"] = str(dark_path)
    except Exception as exc:  # dark theme is best-effort
        result["screenshot_dark_error"] = str(exc)


def _write_result(payload: dict[str, Any]) -> None:
    path = os.environ.get("ANKI_ADDON_WORKBENCH_RESULT")
    if not path:
        return
    Path(path).write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _finish() -> None:
    if mw is not None:
        mw.unloadProfileAndExit()


def _run_and_quit() -> None:
    # CWYC_SMOKE_THEME=dark: switch theme BEFORE any chat content renders, so
    # the final grab shows dark pixels that were never light. The mid-session
    # flip further down stays as a state assertion, but its mw.grab() is NOT
    # pixel-truth: QWidget::grab photographs the GPU compositor's tiles, and
    # after a live theme flip those stay stale-light no matter how hard we
    # force reflows (verified 2026-07-12: computed .cwyc-app background was
    # dark charcoal while the grab stayed light). Fresh-render grabs are
    # honest; post-flip grabs are not.
    if os.environ.get("CWYC_SMOKE_THEME") == "dark":
        try:
            from aqt.theme import Theme, theme_manager

            mw.pm.set_theme(Theme.DARK)
            theme_manager.apply_style()
            QTest.qWait(400)
        except Exception:
            pass
    try:
        result = _run_checks()
    except Exception as exc:
        result = {"ok": False, "error": str(exc), "traceback": traceback.format_exc()}

    try:
        _save_screenshots(result)
    except Exception as exc:
        result["screenshot_error"] = str(exc)

    _write_result(result)
    QTimer.singleShot(100, _finish)


def _schedule() -> None:
    QTimer.singleShot(800, _run_and_quit)


from aqt import gui_hooks  # noqa: E402

gui_hooks.main_window_did_init.append(_schedule)
