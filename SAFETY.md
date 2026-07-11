# Collection safety: design and hazard taxonomy

Every claim below is cited against the Anki **25.09.2** source vendored at
`_vendor/anki-src`. Read `rslib/src/...:LINE` as a real line number, not a gesture.

Written after an agent-proposed note ended up leaving the collection in an inconsistent
state involving a filtered deck. The incident is the occasion; the point is that our
architecture made it *possible*, and would have made a dozen similar incidents possible.

---

## Part 1 — Why the existing guard failed, structurally

We already had `ProposalManager._require_normal_deck()`. It is correct. It was called at
four of the mutation sites and not at the others — notably not on the **revert** path
(`proposals.py:1777`, `col.set_deck(cids, did)` restoring raw `did` values captured from
cards that may themselves have been in a filtered deck).

That is the whole lesson: **a guard you must remember to call is a guard you will forget
to call.** The fix is not "call it in more places." The fix is to make it impossible not
to call it.

Compounding it, Anki's own enforcement is *uneven*, so intuition about which operations
are safe is worthless:

| Operation | Anki's behaviour |
|---|---|
| `set_deck(cards, filtered_did)` | **Hard error.** `FilteredDeckError::CanNotMoveCardsInto` — `rslib/src/card/mod.rs:371-373` |
| `add_note(note, filtered_did)` | **Silently succeeds**, cards land in **Default**. `deck_for_adding` falls back when `config_id()` is `None` — `rslib/src/notetype/cardgen.rs:333-352` |
| creating `Filtered::Child` | **Hard error.** `MustBeLeafNode` — `rslib/src/decks/addupdate.rs:114-119` |
| `col.db.execute("update cards …")` | **Silently succeeds**, and wipes the user's undo history |

Three of those four look like the same kind of operation. They are not.

### The two states Check Database cannot see

This is the part that should change how we think about the blast radius.

`did = <filtered deck>` **and** `odid = 0` is unreachable through Anki's own UI, and
`Check Database` does not detect or repair it. `check_filtered_cards` iterates only rows
where `odid > 0` (`rslib/src/dbcheck.rs:208-231`); `missing_decks` inspects only `did`
(`rslib/src/storage/deck/missing-decks.sql`). Nothing looks for a card whose `did` points
at a filtered deck with no recorded home.

The card then explodes at review time — `home deck is filtered`
(`rslib/src/scheduler/answering/mod.rs:456`), or under FSRS fuzz the dynamically
constructed `No such deck: '0'` (`rslib/src/error/not_found.rs:26-36`).

A **dangling `odid`** (home deck deleted out of band) is likewise never validated.

So: *Anki's repair tool will not save us from the exact corruption our agent can cause.*
We are the last line of defence, not the first.

---

## Part 2 — The design

Five rules. Each one removes a class of incident rather than an instance.

### 1. One chokepoint. No guarded call sites.

Every mutation flows through a single `apply(proposal)` that cannot be bypassed:

```
apply(proposal):
    backup_if_risky(proposal)          # scm-bumping or destructive ops only
    col.db.transact(lambda:            # pylib dbproxy.py:transact — begin/commit, rollback on raise
        run_sandwich(proposal))
    return result

run_sandwich(proposal):
    before = invariants.snapshot(col, scope=proposal.scope())
    contract.check_preconditions(col, proposal)     # raises → nothing mutated yet
    result = proposal.execute(col)                  # backend API only, never raw SQL
    invariants.assert_unchanged(col, before, expect=proposal.expected_delta())
```

**API note (verified against the vendored source):** there is no `col.transact()` in pylib.
The real primitive is `col.db.transact(op)` — `db_begin` / run `op` / `db_commit`, and
`db_rollback` on any raised exception (`pylib/anki/dbproxy.py`). Its docstring explicitly
sanctions wrapping several backend calls "when you are making other changes at the same time
and want to ensure they are applied completely or not at all" — exactly our case. Inner
backend ops nest as savepoints under the outer transaction, so the SQL composes correctly.

**Known wart on the rollback path — FIXED via `col.undo()`, ground-truth-counted, verified on
real Anki 2026-07-11.** On `db_rollback`, `rslib/src/backend/dbproxy.rs` does `clear_caches()` +
`rollback_trx()` — it reverts the *SQL* but does **not** pop the in-memory undo entries that the
inner backend ops already pushed: each backend RPC (e.g. `add_note`) runs its own Rust-level
`Collection::transact` (`rslib/src/collection/transact.rs`), which on success calls
`end_undoable_operation` and pushes a real entry into the in-memory undo queue — a push that
lives entirely outside the SQL transaction `col.db.transact` later rolls back. Behaviorally
confirmed 2026-07-10: a forced `InvariantViolation` after `col.add_note` rolled the SQL back
cleanly (note count unchanged, no phantom ledger entry, collection usable, follow-up accept
succeeded), while `col.undo_status()` showed a dangling "Add Note" entry with no surviving data
behind it.

Fix candidates considered:
- **(B) a dedicated backend "clear undo" API** — does not exist. `col.undo()` / `col.undo_status()`
  (`rslib/src/undo/mod.rs`) and the `Undo`/`Redo` RPCs (`proto/anki/collection.proto`) are the
  entire Python-exposed surface; there is no "pop without replaying" or "truncate the queue"
  primitive.
- **(C) trigger the dbproxy full-queue discard** (any non-SELECT via `col.db.execute`, e.g.
  `update col set id = id`) — wipes the whole 30-step queue (`discard_undo_and_study_queues`,
  `rslib/src/backend/dbproxy.rs:122-167`). Rejected as needlessly destructive: it would erase the
  user's real prior undo history over what is, for `create`/`edit`, a single dangling entry.
- **(A) `col.undo()`, called exactly as many times as the write's `execute()` issued backend
  RPCs — the ground truth (`ProposalManager._WriteResult.undo_steps`, `_revert_write`'s `steps`
  argument), never a guess.** Shipped. Every reverse-change our proposals can leave dangling is a
  no-op against the already-SQL-rolled-back state: undoing an add issues a plain
  `DELETE ... WHERE id = ?` with no existence check (`rslib/src/notes/undo.rs` →
  `storage/note/mod.rs::remove_note`; `storage/card/mod.rs::remove_card` likewise), and undoing an
  edit re-writes the very fields the SQL rollback already restored
  (`rslib/src/notes/undo.rs::update_note_undoable`). Exact-count popping (rather than "pop until
  the queue looks back to normal") matters because the queue's only Python-visible signals are a
  translated op-description string and a monotonically-increasing step counter that also ticks up
  on `col.undo()` itself — neither can reliably tell "mine" from "someone else's, coincidentally
  the same op type" apart, so guessing risks reaching into genuinely older, unrelated undo history.
  Tracking the real count instead sidesteps that entirely.

The gui_smoke probe's forced-rollback check (`make test-gui-smoke-docker`,
`_postcondition_rollback` in `tests/gui_smoke/probe_addon/__init__.py`) now ASSERTS on this
instead of merely observing it, and passed on real Anki 2026-07-11:

```
undo_status_before:                {"last_step": 19, "redo": null,       "undo": "Add Note"}
undo_status_after_forced_rollback: {"last_step": 21, "redo": "Add Note", "undo": "Add Note"}
```

`undo` reads back to exactly what it was before the check ran ("Add Note", left by an earlier,
unrelated check's real create) — the dangling entry from *this* check's own forced-rejected
`add_note` is gone. `last_step` (a monotonic counter, ticks up on every op attempt including
`col.undo()` itself, so it is not by itself proof of cleanliness) moved by exactly +2 - one for
the doomed `add_note` and one for our own single `_discard_dangling_undo` → `col.undo()` call -
independent, quantitative confirmation that cleanup fired exactly `undo_steps=1` time, not zero
and not more. `redo` flipping `null` → `"Add Note"` is the expected, harmless side effect of
actively calling `col.undo()` (it pushes what it just undid onto the redo queue); a real user
could in principle click Redo and re-create the rejected note, but nothing prompts them to (they
never saw it appear), and it is overwritten by the next real op like any other redo residue - not
asserted on, since it does not bear on the safety goal. A follow-up accept right after still
succeeded cleanly (`followup_accept_ok: true`), and the F3 mid-batch check (below) still passed
unaffected, confirming its own dangling residual (not cleaned up by design - see the next
paragraph) is unchanged and harmless. `ProposalManager._discard_dangling_undo` (proposals.py)
is called only from an `InvariantViolation` handler in `_apply_write`/`_revert_write` — the one
case where `execute()`/`mutate()` is *guaranteed* to have run to completion, so the ground-truth
count is trustworthy. It is deliberately **not** called for a precondition failure (nothing was
mutated yet, so there is nothing dangling) or for a bare backend exception raised mid-execute
(the F3 mid-batch case below: how many of a multi-item write's RPCs already ran before the raise
is not knowable from the outside, so cleanup is skipped rather than guessed at) — a narrower,
documented residual, not a regression from the pre-fix behavior.

The mid-batch (F3) all-or-nothing path is likewise real-Anki-verified: with a later item's
`update_note` forced to raise, the first item's already-applied edit rolled back too. Note
one probe finding: a *deleted* mid-batch note does NOT reach the rollback path — `_apply_items`
deliberately treats it as a skipped stale item (the staleness guard), which is correct
behavior, just a different path than a backend error. Unlike the postcondition-rollback case
above, this path's dangling undo entries (if the doomed item was not the first) are **not**
cleaned up: the bare `Exception` `_apply_write` catches here does not tell us how many of the
batch's `update_note` calls already succeeded before the raise, and guessing would risk popping
into real, unrelated undo history instead — a narrower residual than before the fix, kept
deliberately rather than guessed away.

The revert path is not special. It builds a proposal and calls `apply()` like everything
else. A revert that would recreate the original corruption is simply rejected.

### 2. Illegal states are unrepresentable in the tool schema

The agent did something dumb partly because we let it name any deck as a string and only
checked afterwards. Instead, deck listings carry the truth:

```json
{"name": "Chinese::Hanzi", "kind": "normal",   "writable": true}
{"name": "Cram: due today", "kind": "filtered", "writable": false,
 "reason": "filtered decks hold cards temporarily; cards must live in a normal home deck"}
```

`propose_note` resolves a deck through one function that rejects filtered decks *and*
filtered ancestors. The agent never sees a `did`. It cannot construct the illegal call.

Tool descriptions must teach the model the domain, not just the syntax. "Deck path" is
not a type; "normal deck that can be a card's permanent home" is.

### 3. Postconditions, scoped and cheap

Run inside the transaction, over the touched decks/notes only:

- no card where `deck.is_filtered() and odid == 0` ← *invisible to Check Database*
- no card where `odid != 0` and `odid` is not a live deck ← *also invisible*
- card-count delta equals the declared expectation
- note-count delta equals the declared expectation
- `scm` unchanged, unless the proposal declared `changes_schema = True`
- every touched row has `usn == -1`

Any violation raises, the savepoint rolls back, the proposal is marked failed, and the
user sees why. Corruption becomes an error message.

The declared-expectation rule catches the whole *blast radius* family: adding a template
generates one new card per existing note (`rslib/src/notetype/schemachange.rs:155-173`).
If a proposal says "1 note, 2 cards" and we observe 4,000, we roll back.

### 4. Never raw SQL. Ever.

`col.db.execute()` on a non-`SELECT` calls `discard_undo_and_study_queues()`
(`rslib/src/backend/dbproxy.rs:122-167`), which destroys the user's entire 30-step undo
history. It does not bump `col.mod`, so the change may never sync. It emits no
`OpChanges`, so open views do not refresh. It sets no `usn = -1`, so the row is never
gathered as pending (`rslib/src/storage/sync.rs:37-46`).

Deletions by raw SQL write no `graves` row (`rslib/src/storage/graves/mod.rs:13-19`), so
another device resurrects the notes — or the count mismatch trips the sync sanity check,
which calls `set_schema_modified()` and forces a one-way full sync with an Upload/Download
prompt where the wrong answer loses data (`rslib/src/sync/collection/normal.rs:95-110`).

There is no acceptable use of raw SQL in a write path. If the backend has no method for
what we want, we do not want it.

### 5. Backup before anything that bumps `scm` or destroys

`mw.create_backup_now()` blocks until complete and exists precisely for this
(`qt/aqt/main.py:1571-1579`). Required before: any schema-bumping op (see hazard 17),
`delete_notes`, notetype edits, template add/remove.

---

## Part 3 — Hazard taxonomy

Ordered by how quietly it hurts you.

### A. Silent corruption Anki will not repair

1. **`add_note` into a filtered deck** silently redirects cards to **Default**. No error.
   `cardgen.rs:333-352`. Our `_require_normal_deck` catches what Anki won't.
2. **`did = filtered, odid = 0`** — breaks review with `home deck is filtered`
   (`answering/mod.rs:456`); **Check Database cannot see it** (`dbcheck.rs:208-231`).
3. **Dangling `odid`** pointing at a deleted deck — never validated; `missing_decks`
   checks only `did`.

### B. Silent data mangling

4. **Wrong field count**: surplus fields are merged into the last field joined by `"; "`
   (`rslib/src/notes/mod.rs:264-275`). Silent.
5. **Hand-editing notetype `flds` ords.** Field data is remapped by each field's *previous*
   `ord` (`schemachange.rs:184-195`). "Normalizing" ords to `0..n` scrambles or drops every
   note's data. Only ever use `add_field` / `remove_field` / `reposition_field`.
6. **A tag containing a space** (or U+3000) silently becomes two tags. Tags are one
   space-separated column (`rslib/src/tags/mod.rs:46-56`). There is no escaping.
7. **Malformed `cards.data`** silently drops FSRS stability/difficulty and the new-queue
   position. Every field is `default_on_invalid`; the blob is unversioned
   (`rslib/src/storage/card/data.rs:115-124`).
8. **`set_deck` wipes FSRS memory** via `clear_fsrs_data()` (`card/mod.rs:190-201`).
   Moving cards costs the user their memory state. Our `move_cards` tool does this today
   and says nothing about it.
9. **Writing `ivl` / `factor` under FSRS** has no scheduling effect and desyncs the columns
   from the `s`/`d` stored in `data`.
10. **`due` means three different things.** New → queue position. Learn → **epoch seconds**.
    Review → **days since `col.crt`**. Never validated on read
    (`storage/card/mod.rs:84`). Writing `due` without branching on `type` silently
    misschedules.

### C. Blast radius

11. **Adding a template** generates one new card **per existing note**, all `New`, dumped
    into the queue at once (`schemachange.rs:155-173`). The single biggest one-edit,
    collection-wide hazard.
12. **Removing a template**, or Tools → Empty Cards, permanently deletes cards and their
    scheduling state. (Revlog rows survive, orphaned — `storage/card/mod.rs:245-250`.)
13. **Delayed action**: editing a note so a `{{#Field}}` conditional renders empty does not
    delete the card now; it makes it an "empty card" that the user destroys later, losing
    scheduling, believing it was their own cleanup.

### D. Sync-level — invisible until a second device

14. Raw write without `usn = -1` → the change **never syncs**. Counts still match, so
    nothing catches it.
15. Raw delete without a `graves` row → the other device **resurrects** the notes.
16. `col.db.execute` → **wipes 30 steps of the user's undo history**, no `col.mod` bump,
    no UI refresh.
17. **`scm` bump forces a one-way full sync** with a data-losing direction prompt.
    Triggered by: field add/remove/reorder, **sort-field change**, template add/remove/
    reorder, change-notetype, notetype delete, deck-config delete, scheduler upgrade —
    **and by Check Database repairing anything** (`dbcheck.rs:184…448`).
18. The collection is opened **WAL, `locking_mode = exclusive`, `busy_timeout = 0`**
    (`rslib/src/storage/sqlite.rs:53-72`). An external process must never touch
    `collection.anki2` while Anki runs: it will be refused, or it will read a stale main
    file whose committed pages still live in `-wal`. **This constrains any future
    "agent-as-device" writer.**

### E. Threading

19. The backend mutex serializes individual calls, not logical operations. A panic while
    holding it **poisons the mutex and kills the collection for the whole process**
    (`rslib/src/backend/mod.rs:115-126`). Anki's own collection thread pool is
    `max_workers=1` (`qt/aqt/taskman.py:33-34`). Never touch `mw.col` off it.

### F. Identity

20. `add_or_update_*` with a reused id **clobbers the existing row** silently.
21. A duplicated `guid` makes sync and `.apkg` import treat two notes as one — overwrite
    or skip, never duplicate.
22. `.apkg` import with a notetype schema mismatch **skips the note**; a cloze-vs-normal
    kind mismatch is a hard error.

### G. Deck names

23. Names are NFC-normalized, ASCII control chars stripped, each component trimmed of
    whitespace and `:`. An empty component becomes literally `"blank"` — `foo::::baz`
    → `foo::blank::baz` (`rslib/src/decks/name.rs:194-207`). Duplicates get `+` appended.
24. Deleting a normal deck also deletes cards **currently held in a filtered deck** whose
    `odid` points at it (`storage/deck/cards_for_deck.sql`).
25. The Default deck (id 1) cannot be deleted; it returns (`decks/remove.rs:27-49`).

---

## Part 4 — What this buys us

Rules 1 and 3 together mean the incident that prompted this document could not have
happened, and neither could #2, #5, #7, #10, #11, or #20 — not because we thought of each
one, but because the postcondition check runs on every write and the transaction rolls
back. That is the difference between a fix and a design.

The hazards that remain need explicit human confirmation, not validation: #8 (losing FSRS
memory on a move), #11 (queue flood), #12 (destroying scheduling), #17 (forced full sync).
These should be surfaced *in the proposal card itself* — "this will discard FSRS memory for
340 cards" — not buried in a mode setting.
