# Capability gaps vs. Anki

What Anki can do that **Chat With Your Cards cannot**, and how badly we need each
thing. Audited 2026-07-23 and reconciled 2026-07-24 against the official manual
(docs.ankiweb.net) —
Editing/Note Types/Templates/Media/Import-Export, Studying/Deck Options/FSRS/
Filtered Decks/Stats, and Browsing/Searching/Maintenance/Preferences/Syncing —
cross-checked against the live tool registry and the proposal layer, so the
"already covered" claims reflect code rather than tool names.

Every theme below is filed in the working backlog (2026-07-23). This document
stays the durable record — the sourcing, the risk notes and the reasoning — so
it is worth updating here first when something is built or reprioritised.
Nothing here is committed to a schedule.

Prompted by a dogfood failure: asked to *"remove the red flag from that card"*,
the agent correctly answered that it had no flag tool at all.

## Already covered (so we don't re-litigate these)

Note create/edit + per-note tags, delete notes, find & replace (regex, field and
query scoped), change deck, deck create/rename, deck-options **preset** values
(dot-path edits, so leech threshold/action, burying flags, learning steps, daily
limits, display order, desired retention and FSRS `params` are all reachable),
filtered decks (create/reconfigure/rebuild/empty — which also covers most of
Custom Study), rename tag collection-wide (including `::` re-parenting), tag and
deck trees, note-type **reads** including full template source and CSS, card
inspection, exact card search, native Again grading of arbitrary exact cards,
post-grading suspension/burial removal, audio attachment on proposals, and
arbitrary Anki search.

**Search syntax is fully available at both identity levels.** `search_notes`
hands the raw string to `col.find_notes`; `find_cards` uses `col.find_cards` and
therefore preserves the exact sibling card that matched a card-level predicate.
Everything documented parses (`is:`, `prop:`, `flag:`, `rated:`, `added:`,
`introduced:`, `re:`, `nc:`, `preset:`, field searches, boolean/wildcards).
`find_cards` returns template, prompt preview, current/home deck, filtered and
hidden state, scheduling summary, and user flag. It is paged (`offset`, up to
100 per call) and explicitly treats results as candidates that must be selected
before a write. *Mark* is just a tag (`marked`), so single-note marking already
works.

**Exact-card grading is covered.** `fail_cards_now` records a native Again on
reviewed exact IDs, including future, hidden, and filtered-deck cards.
`make_cards_available` is the separate post-grading removal of suspension or
burial and never rewrites the failure. This does not yet provide general flag,
suspend, bury, due-date, forget, or reposition operations.

## Cross-cutting constraints

Two structural issues cut across most remaining gaps and should be settled before
building any of them:

1. **`search_notes` caps at 100 results.** Any bulk operation over a real
   collection ("tag these 400 notes") cannot even enumerate its targets.
   Card search no longer shares this dead end: `find_cards` has explicit
   offset-based pagination.
2. **Some writes force a full sync.** Anki calls these schema modifications and
   warns before them. From this audit the list is: adding a field, removing a
   card template, Change Note Type, and deleting a deck-options preset. Everything
   else below (including every card-state action) is a normal, syncable,
   undoable operation. See DESIGN.md task #33 — the decision is to gate these on
   Anki's own `mw.confirm_schema_modification()` rather than write our own
   warning.

## Gaps, by theme

Priority is "how likely is a card-authoring/study assistant to hit this",
not engineering cost.

### 1. Card state & scheduling — **highest value per unit of risk**

All three audits put this first. Every item is card-level, reversible, and
requires no schema modification.

| Feature | Unblocks | Pri | Risk |
|---|---|---|---|
| **Flags** (7 colours + none, one per card) | "remove the red flag from that card"; agent flags what it rewrote for review — a handoff channel that doesn't pollute tags | High | Cosmetic, trivially reversible |
| **Suspend / unsuspend** | "suspend everything tagged chapter-9 until next month" | High | Reversible. Note: filtered decks **cannot pull suspended cards**, so this gates our own filtered-deck tools |
| **Set Due Date** (`n`, `n-m`, `n!`) | "push these 40 cards to after my exam"; "spread this backlog over 14 days" | High | Touches scheduling; `!` also overwrites the interval; new→review conversion is not practically reversible |
| **Forget / Reset** | "I've forgotten this deck, start it over" | Medium-High | Destroys interval/ease and FSRS memory state; review log is preserved |
| **Reposition** (new-card queue) | "study the basics before the vocab" | Medium | New cards only; can renumber unrelated cards |
| **Bury / unbury** | "bury the siblings of what I just failed" | Medium | Self-expires at rollover |

**Sharp edges for the approval card:** Set Due Date with `!`, Forget with
counter reset, and FSRS "Reschedule cards on change" all rewrite scheduling in
bulk. These deserve a louder diff than a note edit — before/after interval and
due date for a sample, plus the total affected count.

### 2. Bulk tagging

We can add/remove tags **only one note at a time** (via an edit proposal) and
rename collection-wide. Anki's Browse offers bulk **Add Tags** / **Remove Tags**
over a selection (wildcards allowed), **delete a tag** outright, and **Clear
Unused Tags**. Combined with the 100-result cap this makes "tag every note
matching this search" effectively impossible today. **High.** Reversible.

### 3. Note-type write path — **CLOSED 2026-08-01 (task #7)**

Shipped as `set_note_type_styling`, `set_card_template`,
`manage_note_type_fields`, `manage_card_templates`, `create_note_type`,
`change_note_type`, and `remove_empty_cards` — all proposal-gated, with
per-proposal revertibility and a critical backup on the destructive ones. See
DESIGN.md §5 for the probed Anki semantics behind them (notably: removing a
field silently rewrites the templates that referenced it, and can generate a
card on every note). The original gap text follows.

### 3. Note-type write path — **our biggest asymmetry** (historical)

Since we started returning template source and CSS verbatim so the agent can
*debug* rendering, it can diagnose a broken `<iframe>` in a template and then
not propose the fix. Wanted: edit `qfmt`/`afmt` and styling CSS; add/rename/
reposition/delete fields; create or clone a note type; **Change Note Type**
(convert notes between types with field/template mapping).

**High**, but the most dangerous family here: a note type is shared across every
deck that uses it, deleting a field destroys content collection-wide, unmapped
fields and templates are dropped on conversion, and several of these force a
full sync. **Empty Cards** becomes mandatory the moment templates are writable —
Anki offers no other way to delete a card whose front renders blank.

### 4. Read-only blind spots — **zero risk, cheap, badly missing**

| Feature | Unblocks | Pri |
|---|---|---|
| **Real statistics** (true retention, forecast/future due, card counts by state, answer buttons, intervals, hourly breakdown, FSRS stability/difficulty/retrievability) | "how am I actually doing?", "how big is next week's backlog?" — `collection_stats` today is note/card totals plus review seconds | High |
| **Per-deck due counts** (New / Learning / To Review) | "what should I study first today?" — `get_deck_info` returns only a total card count | High |
| **Card Info / review history** (per-review dates, ratings, intervals, time taken) | "why do I keep failing this card?" — `get_card` has only aggregate reps/lapses | High |
| **Find Duplicates** | "did I already make this card?" before every proposal — not expressible as a search | Medium |
| **Check Media** (missing + unused files) | "why is this image broken?" Caveat: Anki's own check does **not** scan templates, so template-referenced media is falsely reported unused | Medium-High |

### 5. Safety net & housekeeping

**Undo** is the conspicuous one: several tools advertise being revertible, but
the agent has no undo tool, and after any intervening GUI action the head of the
undo queue may not be ours — so it must inspect before firing. Also: **Check
Database**, **Empty Cards**, **on-demand backup** (we already checkpoint before
`delete_notes`; generalising it before any bulk write is cheap insurance), and
**sync status / sync now** — an agent proposing a schema change should be able
to say "this needs a full upload".

### 6. Smaller, conspicuous holes

- **Delete deck** — we create and rename but cannot delete (destructive: cards die with the deck).
- **Deck-options preset lifecycle** — add/clone/rename/delete a preset and assign one to a deck. Today `set_deck_options` can only mutate a *shared* preset in place; we warn about that but can't offer the correct fix (clone + reassign). Deleting a preset forces a one-way sync.
- **Per-deck "this deck" / "today only" limits** — these live on the *deck*, not the preset, so `set_deck_options` structurally cannot reach them. "Let me do 60 new cards today" is a very common ask.
- **Image/video attachment** — proposal `media[]` is audio-only, which blocks every visual deck (maps, diagrams, occlusion) and any `@font-face`/CSS asset a real styling change needs.
- **CSV/text import** with Anki's real options (notetype/deck columns, field mapping, Update / Preserve / Import as new, match scope) — turns bulk authoring from N approval round-trips into one. `Update` mode is the sharp edge: it rewrites existing notes matched on the first field.
- **Export** (.apkg / plain text / .colpkg) — read-only w.r.t. the collection.
- **Saved searches** — trivial, and per a hard-won APKG lesson, saved searches are how curricula ship (`.apkg` drops filtered decks).
- **Open Browse at a query** — hand off to the real UI instead of pasting text summaries.
- **Preferences** (next-day-starts-at, learn-ahead, timebox, ignore-accents-in-search — the last directly changes how `search_notes` behaves, so at minimum we should *read* it).
- **FSRS optimize / evaluate / simulate** — we can *write* `params` but cannot *compute* them.

### Probably should stay human-only

`.colpkg` import (replaces the entire collection and forces a one-way sync),
restoring from a backup (irreversible loss since the backup; backups exclude
media), and a forced-direction sync. LaTeX preamble editing is security-sensitive
— Anki notes LaTeX can contain malicious commands and disables it by default.

## Suggested sequencing

1. **Card-state write family** (flags, general suspend/unsuspend,
   bury/unbury) — exact card discovery is now shipped; these reversible writes
   are the remaining half of the former `find_cards` milestone.
2. **Bulk tagging** + raise/parameterise the search result cap.
3. **Read-only blind spots** — stats, due counts, card review history. No risk,
   high answer-quality gain.
4. **Scheduling writes** — Set Due Date, Forget, Reposition, with the louder
   diff described above.
5. **Note-type write path**, gated on `confirm_schema_modification()` (DESIGN.md
   task #33), with Empty Cards landing alongside it. **Done 2026-08-01** — the
   gate landed as the proposal card itself (blast radius, full-sync notice, and
   an explicit irreversibility line) rather than a modal, and Empty Cards
   shipped in the same pass as `remove_empty_cards`.
6. **Undo + maintenance** as the safety net under all of the above.
