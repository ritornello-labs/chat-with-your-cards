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
        rust-backend db_rollback() behaves the same way. Also records
        col.undo_status() before/after so we learn whether SAFETY.md's "Known
        wart on the rollback path" (a dangling undo entry left by the inner
        backend op that ran before the postcondition failed) manifests on
        real Anki - that part is an OBSERVATION, not an assertion, since
        finding out is the point."""
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
        "(real col.db.transact) + undo_status observation",
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
        if dock is None or state.dock is not dock:
            raise AssertionError("chat dock not found on the main window")
        if dock.isVisible():
            raise AssertionError("dock should start hidden")
        return dock

    check("dock exists and starts hidden", _dock_exists)

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
        for expected in ("Open / focus chat", "New chat"):
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
                "(function() { return {"
                "  pycmd: typeof pycmd,"
                "  chatUI: typeof window.chatUI,"
                "  marked: typeof window.marked,"
                "  stylesheets: document.styleSheets.length,"
                "  root: document.getElementById('cwyc-root') ? true : false,"
                "  title: document.title"
                "}; })();",
                DOM_TIMEOUT_MS,
                "ready-timeout diagnostics",
            )
            raise AssertionError(f"webview ready ping never arrived; page state: {diagnostics}")

    check("webview ready ping received", _web_ready)

    def _toggle_shows_dock() -> None:
        addon.toggle_chat_focus()
        _wait_until(dock.isVisible, 3_000, "dock to become visible after toggle")

    check("toggle shows dock", _toggle_shows_dock)

    def _scripted_chat() -> dict[str, Any]:
        controller = state.controller
        script = (
            "(function() {"
            f"  var input = document.getElementById('cwyc-input');"
            f"  input.value = {json.dumps(DEMO_MESSAGE)};"
            "  document.getElementById('cwyc-send').click();"
            "  return true;"
            "})();"
        )
        result = _eval_js(dock.web, script, DOM_TIMEOUT_MS, "demo send click")
        if result is not True:
            raise AssertionError(f"send click eval returned {result!r}")

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
            "(function() { return {"
            "  user: document.querySelectorAll('.msg-user').length,"
            "  assistant: document.querySelectorAll('.msg-assistant').length,"
            "  chips: document.querySelectorAll('.tool-chip').length,"
            "  chips_ok: document.querySelectorAll('.cwyc-tool-ok').length,"
            "  streaming: document.querySelectorAll('.cwyc-streaming').length,"
            "  input_style_height: document.getElementById('cwyc-input').style.height,"
            "  input_rect: document.getElementById('cwyc-input')"
            "    .getBoundingClientRect().height,"
            "  input_scroll: document.getElementById('cwyc-input').scrollHeight,"
            "  input_box_sizing: getComputedStyle("
            "    document.getElementById('cwyc-input')).boxSizing,"
            "  composer_rect: document.getElementById('cwyc-composer')"
            "    .getBoundingClientRect().height"
            "}; })();",
            DOM_TIMEOUT_MS,
            "DOM state query",
        )
        if dom.get("composer_rect", 0) > 120:
            raise AssertionError(f"composer blew up: {dom}")
        if dom["user"] < 1 or dom["assistant"] < 1 or dom["chips"] < 1:
            raise AssertionError(f"transcript DOM incomplete: {dom}")
        if dom["chips_ok"] != dom["chips"]:
            raise AssertionError(f"tool chip did not finish: {dom}")
        if dom["streaming"] != 0:
            raise AssertionError(f"assistant message still marked streaming: {dom}")
        return dom

    dom_info = check("transcript DOM rendered", _dom_rendered)

    def _focus_toggle_returns() -> None:
        dock.web.setFocus()
        QTest.qWait(100)
        addon.toggle_chat_focus()
        QTest.qWait(200)
        focused = mw.app.focusWidget()
        if focused is not None and dock.isAncestorOf(focused):
            raise AssertionError("focus stayed inside the dock after second toggle")

    check("second toggle returns focus", _focus_toggle_returns)

    def _proposal_round_trip() -> dict[str, Any]:
        """Scripted propose -> proposal card -> accept -> real note + ledger."""
        script = (
            "(function() {"
            "  var input = document.getElementById('cwyc-input');"
            f"  input.value = {json.dumps(PROPOSE_MESSAGE)};"
            "  document.getElementById('cwyc-send').click();"
            "  return true;"
            "})();"
        )
        if _eval_js(dock.web, script, DOM_TIMEOUT_MS, "propose send click") is not True:
            raise AssertionError("propose send click failed")

        def _card_rendered() -> bool:
            return bool(
                _eval_js(
                    dock.web,
                    "document.querySelectorAll('.cwyc-proposal').length",
                    DOM_TIMEOUT_MS,
                    "proposal card count",
                )
            )

        _wait_until(_card_rendered, STREAM_TIMEOUT_MS, "proposal card to render")
        card = _eval_js(
            dock.web,
            "(function() {"
            "  var p = document.querySelector('.cwyc-proposal');"
            "  return {"
            "    kind: p.querySelector('.cwyc-proposal-kind').textContent,"
            "    status: p.querySelector('.cwyc-proposal-status').textContent,"
            "    fields: p.querySelectorAll('.cwyc-field').length,"
            "    accept: p.querySelectorAll('.cwyc-btn-accept').length,"
            "    preview_tabs: p.querySelectorAll('.cwyc-preview-tab').length"
            "  };"
            "})();",
            DOM_TIMEOUT_MS,
            "proposal card state",
        )
        if card["kind"] != "New note" or card["fields"] < 2 or card["accept"] != 1:
            raise AssertionError(f"proposal card malformed: {card}")
        if card["preview_tabs"] != 2:
            raise AssertionError(f"expected Front/Back preview tabs: {card}")

        before_ids = set(mw.col.find_notes('tag:"ai-created"'))
        _eval_js(
            dock.web,
            "(function() { document.querySelector('.cwyc-btn-accept').click(); "
            "return true; })();",
            DOM_TIMEOUT_MS,
            "proposal accept click",
        )

        def _note_created() -> bool:
            return len(set(mw.col.find_notes('tag:"ai-created"')) - before_ids) == 1

        _wait_until(_note_created, DOM_TIMEOUT_MS, "accepted note to appear")
        (note_id,) = set(mw.col.find_notes('tag:"ai-created"')) - before_ids
        note = mw.col.get_note(note_id)
        session_tag = state.proposals.session_tag
        if session_tag not in note.tags:
            raise AssertionError(f"session tag missing: {note.tags}")

        QTest.qWait(300)
        resolved = _eval_js(
            dock.web,
            "(function() {"
            "  var p = document.querySelector('.cwyc-proposal');"
            "  var ledger = document.getElementById('cwyc-ledger');"
            "  return {"
            "    status: p.querySelector('.cwyc-proposal-status').textContent,"
            "    revert: p.querySelectorAll('.cwyc-btn-revert').length,"
            "    ledger_visible: !ledger.hidden,"
            "    ledger_text: document.getElementById('cwyc-ledger-label').textContent"
            "  };"
            "})();",
            DOM_TIMEOUT_MS,
            "resolved proposal state",
        )
        if resolved["status"] != "Accepted" or not resolved["ledger_visible"]:
            raise AssertionError(f"proposal did not resolve in the UI: {resolved}")
        return {"note_id": note_id, "card": card, "resolved": resolved}

    proposal_info = check("proposal accept round-trip", _proposal_round_trip)

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


def _save_screenshots(result: dict[str, Any]) -> None:
    path = os.environ.get("ANKI_ADDON_WORKBENCH_SCREENSHOT")
    if not path or mw is None:
        return
    light_path = Path(path)
    light_path.parent.mkdir(parents=True, exist_ok=True)
    QTest.qWait(200)
    if not mw.grab().save(str(light_path), "PNG"):
        raise RuntimeError(f"failed to save screenshot to {light_path}")
    result["screenshot"] = str(light_path)

    try:
        from aqt.theme import Theme, theme_manager

        mw.pm.set_theme(Theme.DARK)
        theme_manager.apply_style()
        QTest.qWait(700)
        addon = importlib.import_module(ADDON_PACKAGE)
        result["dark_page_state"] = _eval_js(
            addon.state.dock.web,
            "(function() { return {"
            "  htmlClass: document.documentElement.className,"
            "  canvas: getComputedStyle(document.documentElement)"
            "    .getPropertyValue('--canvas').trim()"
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
            "  return true;"
            "})();",
            DOM_TIMEOUT_MS,
            "dark repaint force",
        )
        QTest.qWait(500)
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
