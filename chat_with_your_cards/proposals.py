"""Note proposals: validation, review, ledger, auto-accept (DESIGN.md section 8).

The ProposalManager is the single write path to the collection. Agent
tools (propose_note / propose_note_edit) submit here; the webview's
proposal cards accept/reject here; the session ledger and undo/revert
live here. Every method runs on Anki's main thread (tool execution is
marshaled there by the add-on glue; bridge handlers already are).

No aqt imports: the collection is an injected callable, so the whole
flow is unit-testable against a fake collection.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import secrets
from dataclasses import dataclass, field, replace
from typing import Any, Callable

from . import contract, invariants

AI_TAG = "ai-created"
AI_EDIT_TAG = "ai-edited"
SESSION_TAG_PREFIX = "ai-chat-dock::session-"
DEFAULT_AUTO_ACCEPT_CAP = 20
DEFAULT_WRITE_BUDGET = 200
MAX_SAMPLES = 5

# Card-state bulk ops (#3): op name -> proposal-card verb. Suspend/bury are
# scheduler queue changes; flags are the cards' 3-bit user flag.
CARD_STATE_OPS = {
    "suspend_cards": "Suspend",
    "unsuspend_cards": "Unsuspend",
    "bury_cards": "Bury",
    "unbury_cards": "Unbury",
    "set_card_flag": "Flag",
}
FLAG_NAMES = {
    0: "no flag",
    1: "Red",
    2: "Orange",
    3: "Green",
    4: "Blue",
    5: "Pink",
    6: "Turquoise",
    7: "Purple",
}
# Explicit id lists ride in op_args, which ships verbatim to the UI payload -
# a query is the right vehicle for anything bigger.
MAX_EXPLICIT_CARD_IDS = 2000

# Bulk tag ops (#4): Anki separates tags with spaces, so a "tag with spaces"
# is actually several tags - reject it instead of silently splitting.
MAX_TAGS_PER_OP = 10

# Scheduling writes (#6). Anki's set_due_date grammar: "n" days from today,
# "n-m" a random day in that range, trailing "!" also sets the interval.
SCHEDULING_OPS = {"set_due_date", "forget_cards", "reposition_new_cards"}

# Per-deck study limits (#25). These live on the DECK object, not the options
# preset, so set_deck_options structurally cannot reach them. Tool arg name ->
# deck-dict key; the *_today values self-expire at the next day rollover
# (stored as {"limit": n, "today": day}).
DECK_LIMIT_KEYS = {
    "new_limit_today": "newLimitToday",
    "review_limit_today": "reviewLimitToday",
    "new_limit": "newLimit",
    "review_limit": "reviewLimit",
}
MAX_DECK_LIMIT = 100_000

# ---- note-type write path (#7) ----
#
# The most dangerous family in the add-on: a note type is shared by every deck
# that uses it, so every op here is collection-wide by construction. The
# metadata below is what the review card shows BEFORE the user commits, and
# what _kind_revertible / _accept_note_type_op read at apply time.
#
# `full_sync` and `card_effect` are empirical, probed against Anki 25.x in a
# throwaway collection (2026-08-01) rather than taken from the manual:
#   edit qfmt/afmt, edit css, rename field ....... scm untouched, 0 cards
#   add field, reposition field .................. scm BUMPED,    0 cards
#   add template ................................. scm BUMPED,    +1 card/note
#                                                  (0 if its front is
#                                                   conditional and unfilled)
#   remove template .............................. scm BUMPED,    -1 card/note
#   remove field ................................. scm sometimes bumped, and
#                                                  CAN CREATE CARDS - see below
#   clone note type .............................. scm untouched, 0 cards
#   change note type ............................. scm BUMPED,    0 cards
#
# The remove-field result is the one nobody expects and the reason this family
# reports an actual before/after card delta instead of trusting a prediction:
# Anki REWRITES every template that referenced the removed field, remapping the
# reference to a different field by ordinal. A `{{#Extra}}c:{{Extra}}{{/Extra}}`
# front became a bare `c:{{Front}}` - no longer conditional - and Anki generated
# a card per note for it. (When the rewrite instead makes two fronts identical,
# Anki refuses the whole update with CardTypeError, which we surface verbatim.)
NOTE_TYPE_OPS: dict[str, dict[str, Any]] = {
    "set_note_type_styling": {
        "label": "note-type CSS",
        "risk": "affects every card of this note type, in every deck",
        "revert": "restores the previous CSS",
        "revertible": True,
        "full_sync": False,
        "backup": False,
    },
    "set_card_template": {
        "label": "card template",
        "risk": "changes how every card of this template renders",
        "revert": "restores the previous front/back source",
        "revertible": True,
        "full_sync": False,
        "backup": False,
    },
    "manage_note_type_fields": {
        "label": "note-type fields",
        "risk": "structural: adding or moving a field forces a full sync; "
        "removing one destroys its content on every note",
        "revert": "restores the previous field list (not removed content)",
        "revertible": True,  # narrowed to False for `remove` at submit time
        "full_sync": True,
        "backup": True,
    },
    "manage_card_templates": {
        "label": "card templates",
        "risk": "adding a template creates a card on every note; removing one "
        "destroys those cards and their review history",
        "revert": "restores the previous template list (not deleted cards)",
        "revertible": True,  # narrowed to False for `remove` at submit time
        "full_sync": True,
        "backup": True,
    },
    "create_note_type": {
        "label": "new note type",
        "risk": "additive - no existing note is touched",
        "revert": "removes the new note type again",
        "revertible": True,
        "full_sync": False,
        "backup": False,
    },
    "change_note_type": {
        "label": "change note type",
        "risk": "converts notes; unmapped fields and templates are dropped, "
        "and their cards' review history goes with them",
        "revert": "not revertible - restore from the backup",
        "revertible": False,
        "full_sync": True,
        "backup": True,
    },
    "remove_empty_cards": {
        "label": "empty cards",
        "risk": "deletes cards whose front renders blank, and any note left "
        "with no cards at all",
        "revert": "not revertible - restore from the backup",
        "revertible": False,
        "full_sync": False,
        "backup": True,
    },
}

# Field/template sub-operations whose effects cannot be undone by restoring the
# note type dict, because the payload they destroy lives outside it (note field
# content; cards and their review history).
DESTRUCTIVE_SUBOPS = {"remove"}

FIELD_SUBOPS = ("add", "rename", "reposition", "remove")
TEMPLATE_SUBOPS = ("add", "rename", "reposition", "remove")
MAX_NOTE_TYPE_SOURCE_CHARS = 50_000
# A front with no field reference renders identically for every note, which is
# exactly the shape Anki rejects with CardTypeError - caught here so the error
# names the real problem instead of an ordinal.
_FIELD_REF_RE = re.compile(r"\{\{[^}]*\}\}")

# ---- batchable operations (#27): change sets beyond note edits ----
#
# Curated allowlist. Each entry declares the RISK CLASS the review card
# surfaces (the batch inherits the highest class present), an HONEST
# revertibility label shown up front (undo-UX decision 2026-07-23: never
# promise batch-wide undo the op class cannot deliver), and a light add-time
# validator; full validation still happens through the op's own submit path
# at apply, with an explicit per-item outcome.
_RISK_ORDER = ("note edits", "note tags", "card state", "scheduling", "deck & structure")


def _batch_v_cards(args: dict[str, Any]) -> None:
    if bool(args.get("card_ids")) == bool(str(args.get("query") or "").strip()):
        raise ProposalError("needs exactly one of card_ids or query")


def _batch_v_flag(args: dict[str, Any]) -> None:
    _batch_v_cards(args)
    flag = int(args.get("flag", -99))
    if not 0 <= flag <= 7:
        raise ProposalError("flag must be 0-7")


def _batch_v_tags(args: dict[str, Any]) -> None:
    if not [t for t in (args.get("tags") or []) if str(t).strip()]:
        raise ProposalError("needs a non-empty tags list")
    if bool(args.get("note_ids")) == bool(str(args.get("query") or "").strip()):
        raise ProposalError("needs exactly one of note_ids or query")


def _batch_v_due(args: dict[str, Any]) -> None:
    _batch_v_cards(args)
    if not _DUE_DATE_RE.match(str(args.get("days", "")).strip()):
        raise ProposalError("days must be 'n', 'n-m', or with trailing '!'")


def _batch_v_limits(args: dict[str, Any]) -> None:
    if not str(args.get("deck", "")).strip():
        raise ProposalError("needs a deck")
    if not any(k in args and args[k] is not None for k in DECK_LIMIT_KEYS):
        raise ProposalError("needs at least one limit value")


def _batch_v_filtered(args: dict[str, Any]) -> None:
    if str(args.get("action", "")).strip() not in ("rebuild", "empty"):
        raise ProposalError("action must be 'rebuild' or 'empty'")


_CLEAN_REVERT = "reverts cleanly from the ledger"
BATCHABLE_OPS: dict[str, dict[str, Any]] = {
    "suspend_cards": {"risk": "card state", "revert": _CLEAN_REVERT, "validate": _batch_v_cards},
    "unsuspend_cards": {"risk": "card state", "revert": _CLEAN_REVERT, "validate": _batch_v_cards},
    "bury_cards": {"risk": "card state", "revert": _CLEAN_REVERT + " (also expires at rollover)", "validate": _batch_v_cards},
    "unbury_cards": {"risk": "card state", "revert": _CLEAN_REVERT, "validate": _batch_v_cards},
    "set_card_flag": {"risk": "card state", "revert": _CLEAN_REVERT, "validate": _batch_v_flag},
    "add_tags": {"risk": "note tags", "revert": _CLEAN_REVERT + " (exact prior tag lists)", "validate": _batch_v_tags},
    "remove_tags": {"risk": "note tags", "revert": _CLEAN_REVERT + " (exact prior tag lists)", "validate": _batch_v_tags},
    "set_due_date": {"risk": "scheduling", "revert": _CLEAN_REVERT + " (exact scheduling restore)", "validate": _batch_v_due},
    "forget_cards": {"risk": "scheduling", "revert": _CLEAN_REVERT + " (exact scheduling restore)", "validate": _batch_v_cards},
    "reposition_new_cards": {"risk": "scheduling", "revert": _CLEAN_REVERT, "validate": _batch_v_cards},
    "set_deck_limits": {"risk": "deck & structure", "revert": _CLEAN_REVERT + " (today-limits also self-expire)", "validate": _batch_v_limits},
    "filtered_deck_action": {"risk": "deck & structure", "revert": "NOT revertible: the previous gathered set is not stored anywhere", "validate": _batch_v_filtered},
}
# submit_* dispatch for the internal apply path; deck ops go through
# _accept_deck_op, bulk ops through _accept_bulk.
_BATCH_SUBMIT = {
    "suspend_cards": "submit_card_state",
    "unsuspend_cards": "submit_card_state",
    "bury_cards": "submit_card_state",
    "unbury_cards": "submit_card_state",
    "set_card_flag": "submit_card_state",
    "add_tags": "submit_bulk_tags",
    "remove_tags": "submit_bulk_tags",
    "set_due_date": "submit_scheduling",
    "forget_cards": "submit_scheduling",
    "reposition_new_cards": "submit_scheduling",
    "set_deck_limits": "submit_set_deck_limits",
    "filtered_deck_action": "submit_filtered_deck_action",
}
_DUE_DATE_RE = re.compile(r"^\d+(-\d+)?!?$")
# Every field the three ops can touch, captured per card at apply time so
# revert is an exact update_card restore - including the new->review
# conversion Anki's own UI calls not-practically-reversible, and the FSRS
# memory state forget destroys.
_SCHED_FIELDS = (
    "type",
    "queue",
    "due",
    "ivl",
    "factor",
    "left",
    "odue",
    "reps",
    "lapses",
    "custom_data",
    "memory_state",
)
# Per-card inspection cap at submit (no-op / filtered-deck warnings). Purely
# advisory: accept captures true prior state per card regardless of size.
CARD_STATE_INSPECT_MAX = 500

# New-skill proposals (workspace task #20): kebab-case only (the harness
# discovers skills by directory name) and generous-but-bounded size caps -
# large enough for a real workflow write-up, small enough that a runaway or
# adversarial generation can't dump megabytes into agent-home.
SKILL_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
MAX_SKILL_NAME_CHARS = 64
MAX_SKILL_DESCRIPTION_CHARS = 1024
MAX_SKILL_MARKDOWN_CHARS = 50_000

# Accepted top-level arguments per proposal tool. Unknown keys are rejected
# rather than silently dropped: a misnamed `fields` on an edit used to vanish
# and surface as a bogus "all proposed values match the note" (dogfood
# 2026-07-23), which talked the agent out of a valid edit. `fields` is listed
# for edit because it is accepted there as an alias for `field_changes`.
_EDIT_ARGS = {
    "note_id",
    "field_changes",
    "fields",
    "add_tags",
    "remove_tags",
    "rationale",
    "media",
    "supersedes",
}
_CREATE_ARGS = {
    "note_type",
    "deck",
    "tags",
    "fields",
    "rationale",
    "media",
    "supersedes",
}

PENDING = "pending"
ACCEPTED = "accepted"

# Filtered-deck gather order codes (Anki's DYN_* constants).
FILTERED_ORDER_NAMES = {
    0: "oldest seen first",
    1: "random",
    2: "increasing intervals",
    3: "decreasing intervals",
    4: "most lapses",
    5: "order added",
    6: "order due",
    7: "latest added first",
    8: "relative overdueness",
}
AUTO_ACCEPTED = "auto-accepted"
REJECTED = "rejected"
UNDONE = "undone"
SUPERSEDED = "superseded"


class ProposalError(Exception):
    """Validation failure reported back to the agent as a tool error."""


class StaleRevert(ProposalError):
    """A revert would discard a change made AFTER the proposal was applied.

    Distinct from an ordinary refusal because it is the one case the user can
    legitimately override: they may decide the newer change should lose. The
    UI surfaces it on the card with an explicit "undo anyway", so the override
    is a decision rather than a retry.
    """


# rslib's UNDO_LIMIT (undo/mod.rs): the in-memory undo queue never holds more
# than this many steps, so it is a safe absolute cap on how many times
# _discard_dangling_undo will ever call col.undo() - see that method.
UNDO_DISCARD_MAX = 30


@dataclass
class _WriteResult:
    """What a chokepoint ``execute`` reports back: the value to return to the
    caller, the collection-wide count-delta ``Expectation`` to assert, and the
    touched-row ``Scope`` the scoped postconditions run over.

    ``undo_steps`` is the exact number of backend RPCs ``execute`` issued
    when it ran to completion (default 1: almost every execute() makes one
    mutating call - add_note, tags.rename, set_deck, remove_notes, a single
    update_note). The two execute()s that loop over _apply_items (bulk
    find_replace, change_set) call update_note once per applied item and set
    this to that count. Used only to discard exactly that many dangling undo
    entries if a postcondition rejects the write after execute() already
    fully applied it (SAFETY.md's "Known wart", see _discard_dangling_undo)."""

    value: Any
    expectation: invariants.Expectation
    scope: invariants.Scope
    undo_steps: int = 1


@dataclass
class Proposal:
    id: str
    kind: str  # "create" | "edit" | "bulk" | "delete" | "change_set" | "deck_op" | "note_type_op" | "skill_update" | "skill_create"
    note_type: str
    deck: str
    tags: list[str]
    fields: dict[str, str]  # create: all values; edit: changed fields (new values)
    rationale: str
    status: str = PENDING
    note_id: int | None = None  # edit target; set on create once applied
    base_fields: dict[str, str] = field(default_factory=dict)  # edit staleness guard
    add_tags: list[str] = field(default_factory=list)
    remove_tags: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    previews: dict[str, Any] | None = None
    # bulk / delete / change_set extras
    op: str = ""  # bulk: "rename_tag" | "find_replace" | "move_cards"
    op_args: dict[str, Any] = field(default_factory=dict)
    title: str = ""  # change_set title
    count: int = 0  # affected notes/cards/items
    samples: list[dict[str, Any]] = field(default_factory=list)
    items: list[dict[str, Any]] = field(default_factory=list)  # change_set entries
    open: bool = False  # change_set still collecting
    revision: int = 1  # immutable review revision; edits create a new revision
    # Staged media attachments (task #21; media_staging.py): payload dicts
    # {id, kind, name, mime, bytes, src} where src is a self-contained data:
    # URI for the review card's playable preview. Files live in the staging
    # dir until accept (imported via col.media.add_file) or reject/supersede
    # (discarded). create and edit proposals both stage here (#24b).
    media: list[dict[str, Any]] = field(default_factory=list)
    # Whether accepting this can be undone from the dock (#7). Set at submit
    # time by families where the SAME tool is revertible or not depending on
    # its arguments (rename a field vs remove one), so the review card can say
    # so BEFORE the click rather than the ledger only after it. None = the
    # kind-level default in _kind_revertible decides.
    revertible: bool | None = None
    # Edit only (#24a): the note's OTHER fields, unchanged by this proposal.
    # Never written - context so the reviewer can see the rest of the note
    # (does the new value duplicate something already there?) without the
    # diff itself growing. The card keeps them collapsed by default.
    context_fields: dict[str, str] = field(default_factory=dict)
    # Preview-only audio: [sound:...] markers in `fields` that resolve to media
    # ALREADY in the collection, rendered as playable data: URIs so the review
    # card can replay them (same player strip as `media`). Distinct from
    # `media` because these are never imported on accept - they already live in
    # collection.media - so _import_staged_media must not touch them.
    preview_media: list[dict[str, Any]] = field(default_factory=list)

    def operation_digest(self) -> str:
        operation = {
            "type": "anki.note.create" if self.kind == "create" else f"anki.proposal.{self.kind}",
            "deck": self.deck,
            "note_type": self.note_type,
            "fields": self.fields,
            "tags": self.tags,
            "note_id": self.note_id,
            "op": self.op,
            "op_args": self.op_args,
        }
        canonical = json.dumps(operation, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        return hashlib.sha256(canonical.encode()).hexdigest()

    def to_payload(self) -> dict[str, Any]:
        fields_payload = []
        for name, new in self.fields.items():
            entry: dict[str, Any] = {"name": name, "new": new}
            if self.kind == "edit":
                entry["old"] = self.base_fields.get(name, "")
            fields_payload.append(entry)
        return {
            "id": self.id,
            "revision": self.revision,
            "operation_digest": self.operation_digest(),
            "kind": self.kind,
            "status": self.status,
            "note_type": self.note_type,
            "deck": self.deck,
            "tags": self.tags,
            "note_id": self.note_id,
            "fields": fields_payload,
            "add_tags": self.add_tags,
            "remove_tags": self.remove_tags,
            "rationale": self.rationale,
            "warnings": self.warnings,
            "previews": self.previews,
            "op": self.op,
            "op_args": self.op_args,
            "title": self.title,
            "count": self.count,
            "samples": self.samples,
            "media": self.media,
            "preview_media": self.preview_media,
            # Omitted (not null) when unknown: the UI's `revertible` is also
            # written by proposal_resolved, and a null here would clobber a
            # real answer with "we don't know".
            **({"revertible": self.revertible} if self.revertible is not None else {}),
            "context_fields": [
                {"name": name, "value": value}
                for name, value in self.context_fields.items()
            ],
            "items": [
                (
                    {
                        # Generic-op item (#27): index lets the review card
                        # exclude it on accept; risk/revert are shown up front.
                        "index": index,
                        "op": item["op"],
                        "label": item.get("label", ""),
                        "risk": item.get("risk", ""),
                        "revert": item.get("revert", ""),
                    }
                    if "op" in item
                    else {
                        "index": index,
                        "note_id": item["note_id"],
                        "label": item.get("label", ""),
                        "fields": sorted(item.get("field_changes", {})),
                    }
                )
                for index, item in enumerate(self.items)
            ],
            "open": self.open,
        }


@dataclass
class LedgerEntry:
    id: str
    kind: str  # "create" | "edit" | "bulk" | "delete" | "change_set"
    note_id: int
    label: str
    prior_fields: dict[str, str] = field(default_factory=dict)  # edit revert data
    prior_tags: list[str] | None = None
    # What the proposal actually WROTE. Prior state alone cannot tell "nobody
    # touched this since" from "someone edited it after us" - both look like
    # `current != prior`. Without this, revert blind-wrote the prior values
    # over whatever was there and silently destroyed any later edit, including
    # one synced in from another device (user-found 2026-07-23).
    written_fields: dict[str, str] = field(default_factory=dict)
    written_tags: list[str] | None = None
    undone: bool = False
    # bulk/change_set revert data: op-specific priors
    # rename_tag: {"old_tag","new_tag"}; move_cards: {"card_decks":{cid:did}};
    # find_replace/change_set: {"items":[{note_id, base_fields, prior_tags}]}
    data: dict[str, Any] = field(default_factory=dict)
    revertible: bool = True

    def to_payload(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "note_id": self.note_id,
            "label": self.label,
            "undone": self.undone,
            "revertible": self.revertible,
        }


class ProposalManager:
    def __init__(
        self,
        *,
        get_col: Callable[[], Any],
        push: Callable[[dict[str, Any]], None],
        config: dict[str, Any],
        save_pins: Callable[[dict[str, Any]], None] | None = None,
        after_write: Callable[[list[int]], None] | None = None,
        checkpoint: Callable[[str, bool], bool] | None = None,
        observe: Callable[[dict[str, Any]], None] | None = None,
        apply_skill: Callable[["Proposal"], list[str]] | None = None,
        after_deck_change: Callable[[], None] | None = None,
        apply_skill_create: Callable[["Proposal"], list[str]] | None = None,
        list_skill_names: Callable[[], set[str]] | None = None,
        media_staging: Any | None = None,
        sync_now: Any | None = None,
    ) -> None:
        self._get_col = get_col
        # Quiet mode (#27): while a batch applies its items through their
        # ops' own submit/accept paths, per-item pushes and checkpoints are
        # suppressed - the batch owns the card, the ledger row and the ONE
        # backup checkpoint.
        self._quiet = False

        def _gated_push(payload: dict[str, Any]) -> None:
            if self._quiet:
                return
            push(payload)

        self._push = _gated_push
        self._config = config
        self._save_pins = save_pins
        # Called with the affected note ids after any collection write, so the
        # add-on can refresh the reviewer if it is showing one of them.
        self._after_write = after_write or (lambda _ids: None)
        # Called before bulk/delete/change-set applies; the add-on glue makes
        # an Anki backup so even non-ledger-revertible ops have a way back.
        # Second arg = critical: True forces a SYNCHRONOUS backup (on disk
        # before the op proceeds) for irreversible operations like delete.
        # Returns True when the checkpoint is safe to proceed on (a backup
        # exists or was just made - Anki's create_backup(force=True) can
        # legitimately return False for "nothing changed since the last
        # backup", which is NOT a failure) and False only when it actually
        # failed (exception raised while creating it). _apply_write ABORTS
        # a critical=True write on False (no destructive op ever proceeds
        # without a safety net); a non-critical False leaves
        # self._checkpoint_warning set for the caller to surface.
        self._checkpoint = checkpoint or (lambda _reason, _critical: True)
        self._checkpoint_warning: str | None = None
        # Learning capture (DESIGN.md section 15): "applied" after content
        # writes (snapshot what the system wrote), "resync" after reverts
        # (refresh already-tracked notes only), "reviewed" with the diff
        # between what the agent proposed and what the user accepted.
        self._observe = observe or (lambda _event: None)
        # Applies an accepted skill-update proposal (writes the skill file,
        # archives the prior version, consumes observations); returns
        # warnings/notes to show on the resolved card.
        self._apply_skill = apply_skill
        # Called after a deck operation applies or reverts, so the add-on can
        # refresh the deck browser / overview (deck ops touch no note ids, so
        # the after_write reviewer-refresh path never fires for them).
        self._after_deck_change = after_deck_change or (lambda: None)
        # Applies an accepted skill-CREATE proposal (writes the brand-new
        # SKILL.md under agent-home; see skills.write_new_skill's security
        # note - this only ever runs from an accepted proposal). Returns
        # warnings/notes to show on the resolved card.
        self._apply_skill_create = apply_skill_create
        # Lists the names of skills that already exist under agent-home, so
        # submit_skill_create can reject a colliding name before a proposal
        # card is even shown. None (e.g. in tests that don't care) means no
        # collision is ever reported.
        self._list_skill_names = list_skill_names
        # Staged media for create proposals (task #21; media_staging.py).
        # None (tests that don't care / minimal setups) disables the media
        # arg on propose_note with a clear error instead of silent drops.
        self._media = media_staging
        # Starts Anki's own sync flow (#8); injected by the add-on glue so
        # this module stays aqt-free. None = sync tool unavailable.
        self._sync_now = sync_now
        if self._media is not None:
            # Clear staging dirs abandoned by crashes/never-resolved
            # proposals; bounded work at startup, best-effort.
            try:
                self._media.sweep()
            except OSError:
                pass
        self._proposals: dict[str, Proposal] = {}
        self._ledger: list[LedgerEntry] = []
        self._counter = 0
        self._auto_accepted = 0
        self._auto_accept_pause_notified = False
        self._written = 0  # trusted-writes budget consumed (notes touched)
        self._budget_pause_notified = False
        self.session_id = secrets.token_hex(4)

    # ---- session lifecycle ----

    def new_session(self) -> None:
        self._proposals.clear()
        self._ledger.clear()
        self._counter = 0
        self._auto_accepted = 0
        self._auto_accept_pause_notified = False
        self._written = 0
        self._budget_pause_notified = False
        self.session_id = secrets.token_hex(4)

    @property
    def session_tag(self) -> str:
        prefix = str(self._config.get("session_tag_prefix", SESSION_TAG_PREFIX))
        return (prefix + self.session_id) if prefix else ""

    @property
    def created_tag(self) -> str:
        return str(self._config.get("created_tag", AI_TAG))

    @property
    def edited_tag(self) -> str:
        return str(self._config.get("edited_tag", AI_EDIT_TAG))

    def _tag_edit(self, note: Any) -> None:
        """Stamp the configured 'edited by AI' tag, if any, on an edited note."""
        tag = self.edited_tag
        if tag and tag not in note.tags:
            note.tags.append(tag)

    def _col(self) -> Any:
        col = self._get_col()
        if col is None:
            raise ProposalError("collection is not open")
        return col

    def _next_id(self) -> str:
        self._counter += 1
        # Session-scoped, NOT just a counter. Restoring a chat replays its
        # saved proposal cards into the UI with their original ids while this
        # manager starts a fresh session at p1 - so a plain counter handed the
        # next live proposal an id that was already on screen, and the UI's
        # upsert quietly REPLACED that old card somewhere up the scrollback
        # instead of appending a new one. The tool returned ok, the assistant
        # said "proposed as p2", and no card ever appeared (dogfood
        # 2026-07-27).
        return f"p{self.session_id}-{self._counter}"

    # ---- pins ----

    @property
    def pins(self) -> dict[str, Any]:
        pins = self._config.get("pins") or {}
        return pins if isinstance(pins, dict) else {}

    def set_pins(self, pins: dict[str, Any]) -> None:
        cleaned = {
            "deck": str(pins.get("deck") or "").strip(),
            "note_type": str(pins.get("note_type") or "").strip(),
            "tags": [t for t in (pins.get("tags") or []) if str(t).strip()],
            "fields": {
                str(k): str(v)
                for k, v in (pins.get("fields") or {}).items()
                if str(k).strip()
            },
        }
        self._config["pins"] = cleaned
        if self._save_pins is not None:
            self._save_pins(cleaned)
        self._push({"type": "pins", "pins": cleaned})

    def push_ui_state(self) -> None:
        """Initial state for the webview: pins and the current ledger."""
        self._push({"type": "pins", "pins": self.pins})
        self._push_ledger()

    def _maybe_supersede(self, supersedes: Any) -> None:
        """Deactivate a still-pending proposal that a new one revises, so the
        old card is set aside in favor of the new version (DESIGN.md 8)."""
        prev = self._proposals.get(str(supersedes or ""))
        if prev is not None and prev.status == PENDING:
            prev.status = SUPERSEDED
            # Staged media is deliberately NOT discarded here (nor on reject):
            # restore() can bring this proposal back to pending, and its accept
            # would then need the files. Staging is freed on successful import
            # (accept) or by the startup sweep for proposals that never resolve.
            self._push(
                {"type": "proposal_resolved", "id": prev.id, "status": SUPERSEDED}
            )

    def supersede(self, args: dict[str, Any]) -> None:
        """Set a pending proposal aside because the user asked for a revision
        ("Suggest change" + send). Restorable like any superseded card. A no-op
        if it was already resolved, so a stale click can't corrupt state."""
        self._maybe_supersede(args.get("id"))

    def revise(self, msg: dict[str, Any]) -> None:
        """Validate and preview a new immutable revision of a create proposal.

        The old review revision is never accepted implicitly: the shared UI
        must save edits first, receive the incremented revision, and then
        approve that exact revision. Other proposal kinds keep their existing
        review flow until their protocol adapters are implemented.
        """
        proposal = self._proposals.get(str(msg.get("id", "")))
        if proposal is None or proposal.status != PENDING:
            return
        try:
            expected_revision = int(msg.get("expected_revision", 0))
        except (TypeError, ValueError):
            expected_revision = 0
        if expected_revision != proposal.revision:
            self._push(
                {
                    "type": "proposal_error",
                    "id": proposal.id,
                    "message": f"stale proposal revision {expected_revision}; current revision is {proposal.revision}",
                }
            )
            return
        if proposal.kind != "create":
            self._push(
                {
                    "type": "proposal_error",
                    "id": proposal.id,
                    "message": "saved revisions are currently available for new-note proposals only",
                }
            )
            return
        fields = {str(key): str(value) for key, value in (msg.get("fields") or {}).items()}
        try:
            col = self._col()
            model = self._validate_note_type_and_fields(col, proposal.note_type, fields)
            field_names = [field["name"] for field in model["flds"]]
            first_name = field_names[0]
            if not fields.get(first_name, "").strip():
                raise ProposalError(f"first field {first_name!r} must not be empty")
            proposal.fields = {name: fields.get(name, "") for name in field_names}
            proposal.previews = self._render_create_preview(col, model, proposal)
            self._attach_preview_media(col, proposal)
            proposal.revision += 1
        except ProposalError as exc:
            self._push({"type": "proposal_error", "id": proposal.id, "message": str(exc)})
            return
        self._push({"type": "proposal", "proposal": proposal.to_payload()})

    # ---- agent-facing submission (tool entry points) ----

    def submit_create(self, args: dict[str, Any]) -> dict[str, Any]:
        col = self._col()
        unknown_args = sorted(set(args) - _CREATE_ARGS)
        if unknown_args:
            raise ProposalError(
                f"unknown argument(s) {unknown_args}; valid: {sorted(_CREATE_ARGS)}"
            )
        pins = self.pins
        note_type = str(args.get("note_type", "")).strip()
        deck = str(args.get("deck", "")).strip()
        tags = [str(t).strip() for t in (args.get("tags") or []) if str(t).strip()]
        fields = _coerce_field_map(args.get("fields"), "fields")
        rationale = str(args.get("rationale", ""))
        warnings: list[str] = []

        if pins.get("note_type"):
            if note_type and note_type != pins["note_type"]:
                raise ProposalError(
                    f"note type is pinned to {pins['note_type']!r}; "
                    "propose with the pinned note type"
                )
            note_type = pins["note_type"]
        if pins.get("deck"):
            if deck and deck != pins["deck"]:
                warnings.append(f"deck overridden by pin: {pins['deck']}")
            deck = pins["deck"]
        for tag in pins.get("tags") or []:
            if tag not in tags:
                tags.append(tag)
        for name, value in (pins.get("fields") or {}).items():
            if not fields.get(name):
                fields[name] = value

        model = self._validate_note_type_and_fields(col, note_type, fields)
        if not deck:
            raise ProposalError("deck is required (no deck pinned)")
        deck_id = self._find_deck_id(col, deck)
        if deck_id is None:
            warnings.append(f"deck {deck!r} does not exist yet; it will be created")
        else:
            self._require_normal_deck(col, deck, deck_id)

        field_names = [f["name"] for f in model["flds"]]
        first = fields.get(field_names[0], "").strip()
        if not first:
            raise ProposalError(f"first field {field_names[0]!r} must not be empty")
        if self._looks_duplicate(col, model, field_names[0], first):
            warnings.append("possible duplicate: a note with the same first field exists")

        proposal = Proposal(
            id=self._next_id(),
            kind="create",
            note_type=note_type,
            deck=deck,
            tags=tags,
            fields={name: fields.get(name, "") for name in field_names},
            rationale=rationale,
            warnings=warnings,
        )
        media_items = list(args.get("media") or [])
        if media_items:
            self._stage_media(proposal, media_items)
        proposal.previews = self._render_create_preview(col, model, proposal)
        self._attach_preview_media(col, proposal)

        if self._trusted_enabled() and self._budget_take(1):
            note_id = self._apply_create(col, model, proposal)
            proposal.status = AUTO_ACCEPTED
            proposal.note_id = note_id
            self._proposals[proposal.id] = proposal
            self._push({"type": "proposal", "proposal": proposal.to_payload()})
            self._maybe_supersede(args.get("supersedes"))
            self._push_ledger()
            self._after_write([note_id])
            return {
                "status": "created",
                "note_id": note_id,
                "auto_accepted": True,
                "warnings": warnings,
            }

        if self._auto_accept_enabled():
            cap = int(self._config.get("auto_accept_cap", DEFAULT_AUTO_ACCEPT_CAP))
            if self._auto_accepted < cap:
                note_id = self._apply_create(col, model, proposal)
                self._auto_accepted += 1
                proposal.status = AUTO_ACCEPTED
                proposal.note_id = note_id
                self._proposals[proposal.id] = proposal
                self._push({"type": "proposal", "proposal": proposal.to_payload()})
                self._maybe_supersede(args.get("supersedes"))
                self._push_ledger()
                self._after_write([note_id])
                return {
                    "status": "created",
                    "note_id": note_id,
                    "auto_accepted": True,
                    "warnings": warnings,
                }
            if not self._auto_accept_pause_notified:
                self._auto_accept_pause_notified = True
                self._push(
                    {
                        "type": "notice",
                        "text": f"Auto-accept paused after {cap} notes this session; "
                        "further proposals need manual review.",
                    }
                )
            warnings.append("auto-accept cap reached; queued for manual review")

        self._proposals[proposal.id] = proposal
        self._push({"type": "proposal", "proposal": proposal.to_payload()})
        self._maybe_supersede(args.get("supersedes"))
        return {
            "status": "pending_user_review",
            "proposal_id": proposal.id,
            "warnings": warnings,
            "note": "The user sees a proposal card and will accept, edit, or reject it.",
        }

    def submit_edit(self, args: dict[str, Any]) -> dict[str, Any]:
        col = self._col()
        unknown_args = sorted(set(args) - _EDIT_ARGS)
        if unknown_args:
            raise ProposalError(
                f"unknown argument(s) {unknown_args}; valid: {sorted(_EDIT_ARGS)}"
            )
        note_id = int(args.get("note_id", 0))
        # `fields` is accepted as an alias for `field_changes`: propose_note
        # (create) takes `fields`, so reaching for the same name on edit is the
        # natural slip - and silently dropping it produced a bogus "all
        # proposed values match the note" (dogfood 2026-07-23).
        raw_changes = args.get("field_changes")
        if raw_changes is None:
            raw_changes = args.get("fields")
        submitted = _coerce_field_map(raw_changes, "field_changes")
        changes = dict(submitted)
        add_tags = [str(t).strip() for t in (args.get("add_tags") or []) if str(t).strip()]
        remove_tags = [
            str(t).strip() for t in (args.get("remove_tags") or []) if str(t).strip()
        ]
        rationale = str(args.get("rationale", ""))

        try:
            note = col.get_note(note_id)
        except Exception:
            raise ProposalError(f"note {note_id} not found") from None
        current = dict(note.items())
        unknown = [name for name in changes if name not in current]
        if unknown:
            raise ProposalError(
                f"unknown field(s) {unknown} for this note; valid: {list(current)}"
            )
        changes = {k: v for k, v in changes.items() if v != current[k]}
        if not changes and not add_tags and not remove_tags:
            # Two very different failures; conflating them sent the agent
            # chasing a phantom "the tool compares by text" theory.
            if not submitted:
                raise ProposalError(
                    "no field changes provided: pass `field_changes` as an object "
                    "mapping field name -> full new value (add_tags/remove_tags "
                    "also count as changes)"
                )
            raise ProposalError("no effective changes: all proposed values match the note")

        deck = ""
        cards = list(note.cards())
        if cards:
            deck = col.decks.name(cards[0].did)
        proposal = Proposal(
            id=self._next_id(),
            kind="edit",
            note_type=note.note_type()["name"],
            deck=deck,
            tags=list(note.tags),
            fields=changes,
            rationale=rationale,
            note_id=note_id,
            base_fields={name: current[name] for name in changes},
            add_tags=add_tags,
            remove_tags=remove_tags,
            context_fields={
                name: value for name, value in current.items() if name not in changes
            },
        )
        media_items = list(args.get("media") or [])
        if media_items:
            self._stage_media(proposal, media_items)
        proposal.previews = self._render_edit_preview(col, note_id, changes)
        # Same player strip create proposals get (#24b): staged attachments
        # plus any [sound:...] the NEW values point at that already lives in
        # collection.media. Scanned over the changed values only - the whole
        # note's audio would drown the diff being reviewed.
        self._attach_preview_media(col, proposal)
        self._proposals[proposal.id] = proposal
        self._push({"type": "proposal", "proposal": proposal.to_payload()})
        self._maybe_supersede(args.get("supersedes"))
        if self._trusted_enabled() and self._budget_take(1):
            self.accept({"id": proposal.id, "_direct": True})
            if proposal.status in (ACCEPTED, AUTO_ACCEPTED):
                return {
                    "status": "applied",
                    "note_id": note_id,
                    "auto_accepted": True,
                }
        return {
            "status": "pending_user_review",
            "proposal_id": proposal.id,
            "note": "The user sees a proposal card with field diffs and will decide.",
        }

    # ---- bulk single-op tools (one semantic operation, one proposal) ----

    def _finish_submission(self, proposal: Proposal, writes: int) -> dict[str, Any]:
        """Common tail for bulk submissions: apply directly under
        trusted-writes (within budget), otherwise render a proposal card."""
        self._proposals[proposal.id] = proposal
        if self._quiet:
            # Internal batch item (#27): the caller drives accept itself.
            return {
                "status": "pending_user_review",
                "proposal_id": proposal.id,
                "affected": proposal.count,
                "warnings": proposal.warnings,
            }
        if (
            self._trusted_enabled()
            and proposal.kind != "delete"
            and self._budget_take(writes)
        ):
            self._push({"type": "proposal", "proposal": proposal.to_payload()})
            try:
                self.accept({"id": proposal.id, "_direct": True})
            except ProposalError as exc:  # pragma: no cover - accept() reports
                return {"status": "error", "error": str(exc)}
            return {
                "status": "applied",
                "proposal_id": proposal.id,
                "affected": proposal.count,
                "warnings": proposal.warnings,
            }
        self._push({"type": "proposal", "proposal": proposal.to_payload()})
        return {
            "status": "pending_user_review",
            "proposal_id": proposal.id,
            "affected": proposal.count,
            "warnings": proposal.warnings,
        }

    def submit_rename_tag(self, args: dict[str, Any]) -> dict[str, Any]:
        col = self._col()
        old = str(args.get("old_tag", "")).strip()
        new = str(args.get("new_tag", "")).strip()
        if not old or not new:
            raise ProposalError("rename_tag needs old_tag and new_tag")
        affected = list(col.find_notes(f'tag:"{old}"'))
        if not affected:
            raise ProposalError(f"no notes carry tag {old!r}")
        warnings = []
        if list(col.find_notes(f'tag:"{new}"')):
            warnings.append(
                f"some notes already carry {new!r}; the rename merges into it "
                "and a ledger revert cannot fully separate them again"
            )
        proposal = Proposal(
            id=self._next_id(),
            kind="bulk",
            op="rename_tag",
            op_args={"old_tag": old, "new_tag": new},
            note_type="",
            deck="",
            tags=[],
            fields={},
            rationale=str(args.get("rationale", "")),
            count=len(affected),
            samples=[{"text": f"{old} → {new}"}],
            warnings=warnings,
        )
        return self._finish_submission(proposal, len(affected))

    def submit_find_replace(self, args: dict[str, Any]) -> dict[str, Any]:
        import re as _re

        col = self._col()
        search = str(args.get("search", ""))
        replacement = str(args.get("replacement", ""))
        field_name = str(args.get("field", "")).strip()
        query = str(args.get("query", "")).strip()
        use_regex = bool(args.get("regex", False))
        if not search:
            raise ProposalError("find_replace needs a non-empty search")
        if use_regex:
            try:
                pattern = _re.compile(search)
            except _re.error as exc:
                raise ProposalError(f"invalid regex: {exc}") from None

        def transform(value: str) -> str:
            if use_regex:
                return pattern.sub(replacement, value)
            return value.replace(search, replacement)

        candidate_query = query or "deck:*"
        try:
            candidates = list(col.find_notes(candidate_query))
        except Exception as exc:
            raise ProposalError(f"bad query {candidate_query!r}: {exc}") from None

        items: list[dict[str, Any]] = []
        samples: list[dict[str, Any]] = []
        for nid in candidates:
            note = col.get_note(nid)
            fields = dict(note.items())
            changes = {}
            for name, value in fields.items():
                if field_name and name != field_name:
                    continue
                new_value = transform(value)
                if new_value != value:
                    changes[name] = new_value
            if not changes:
                continue
            items.append(
                {
                    "note_id": nid,
                    "field_changes": changes,
                    "base_fields": {n: fields[n] for n in changes},
                    "add_tags": [],
                    "remove_tags": [],
                    "label": _short_label(next(iter(fields.values()), "")),
                }
            )
            if len(samples) < MAX_SAMPLES:
                name = next(iter(changes))
                samples.append(
                    {"label": items[-1]["label"], "old": fields[name], "new": changes[name]}
                )
        if not items:
            raise ProposalError(
                self._empty_query_error(
                    col, query, "no notes match that search/replacement"
                )
            )
        scope = f" in field {field_name!r}" if field_name else ""
        proposal = Proposal(
            id=self._next_id(),
            kind="bulk",
            op="find_replace",
            op_args={
                "search": search,
                "replacement": replacement,
                "field": field_name,
                "query": query,
                "regex": use_regex,
            },
            note_type="",
            deck="",
            tags=[],
            fields={},
            rationale=str(args.get("rationale", ""))
            or f"Replace {search!r} with {replacement!r}{scope}",
            count=len(items),
            samples=samples,
            items=items,
        )
        return self._finish_submission(proposal, len(items))

    def submit_move_cards(self, args: dict[str, Any]) -> dict[str, Any]:
        col = self._col()
        query = str(args.get("query", "")).strip()
        deck = str(args.get("deck", "")).strip()
        if not query or not deck:
            raise ProposalError("move_cards needs query and deck")
        try:
            card_ids = list(col.find_cards(query))
        except Exception as exc:
            raise ProposalError(f"bad query {query!r}: {exc}") from None
        if not card_ids:
            raise ProposalError(
                self._empty_query_error(col, query, f"no cards match {query!r}")
            )
        deck_id = self._find_deck_id(col, deck)
        if deck_id is not None:
            self._require_normal_deck(col, deck, deck_id)
        # F2 / SAFETY.md hazard 8: set_deck calls clear_fsrs_data, so a move
        # silently wipes each card's learned memory state, and revert (another
        # set_deck) cannot restore it. Surface the loss on the proposal card.
        warnings: list[str] = []
        fsrs = self._fsrs_memory_count(col, card_ids)
        if fsrs:
            warnings.append(
                f"moving discards FSRS memory (stability/difficulty) for {fsrs} "
                "card(s); this cannot be restored by undo."
            )
        proposal = Proposal(
            id=self._next_id(),
            kind="bulk",
            op="move_cards",
            op_args={"query": query, "deck": deck},
            note_type="",
            deck=deck,
            tags=[],
            fields={},
            rationale=str(args.get("rationale", "")),
            count=len(card_ids),
            samples=[{"text": f'{len(card_ids)} card(s) matching {query!r} → "{deck}"'}],
            warnings=warnings,
        )
        return self._finish_submission(proposal, len(card_ids))

    def submit_bulk_tags(self, args: dict[str, Any]) -> dict[str, Any]:
        """Bulk tag add/remove (#4): Anki Browse's Add Tags / Remove Tags.

        Same selection contract as the card-state ops - exactly one of
        note_ids or query - one proposal card with an honest count. No
        wildcards on either op (probed against the real backend 2026-07-31:
        bulk_remove silently ignores '*'); removal instead follows Anki's
        hierarchy semantics - removing "X" also removes "X::child" tags.
        `remove_tags` over query 'tag:"X"' with tags ["X"] is the
        delete-a-tag idiom.
        """
        col = self._col()
        op = str(args.get("op", ""))
        if op not in ("add_tags", "remove_tags"):
            raise ProposalError(f"unknown tag op {op!r}")
        tags = [str(t).strip() for t in (args.get("tags") or []) if str(t).strip()]
        if not tags:
            raise ProposalError(f"{op} needs a non-empty tags list")
        if len(tags) > MAX_TAGS_PER_OP:
            raise ProposalError(f"at most {MAX_TAGS_PER_OP} tags per operation")
        for tag in tags:
            if " " in tag or '"' in tag:
                raise ProposalError(
                    f"invalid tag {tag!r}: spaces separate tags in Anki - "
                    "use :: for hierarchy or _ inside a name"
                )
            if "*" in tag:
                # The backend silently ignores '*' (verified on 25.09), so a
                # wildcard here would no-op while looking accepted.
                hint = (
                    " - removing a parent tag also removes its ::children"
                    if op == "remove_tags"
                    else ""
                )
                raise ProposalError(
                    f"invalid tag {tag!r}: wildcards are not supported{hint}"
                )
        raw_ids = args.get("note_ids") or []
        query = str(args.get("query") or "").strip()
        if bool(raw_ids) == bool(query):
            raise ProposalError(f"{op} needs exactly one of note_ids or query")
        warnings: list[str] = []
        if raw_ids:
            ids = list(dict.fromkeys(int(n) for n in raw_ids))
            if len(ids) > MAX_EXPLICIT_CARD_IDS:
                raise ProposalError(
                    f"{len(ids)} explicit note_ids is too many "
                    f"(max {MAX_EXPLICIT_CARD_IDS}); pass a query instead"
                )
            live = []
            missing = 0
            for nid in ids:
                try:
                    col.get_note(nid)
                except Exception:
                    missing += 1
                    continue
                live.append(nid)
            if not live:
                raise ProposalError("none of those notes exist")
            if missing:
                warnings.append(f"{missing} note id(s) do not exist and were dropped")
            scope_text = f"{len(live)} selected note(s)"
        else:
            try:
                live = [int(n) for n in col.find_notes(query)]
            except Exception as exc:
                raise ProposalError(f"bad query {query!r}: {exc}") from None
            if not live:
                raise ProposalError(
                    self._empty_query_error(col, query, f"no notes match {query!r}")
                )
            scope_text = f"{len(live)} note(s) matching {query!r}"
        # Advisory no-op accounting, bounded like the card-state ops. Removal
        # mirrors the backend's hierarchy semantics: "X" also hits "X::child".
        if len(live) <= CARD_STATE_INSPECT_MAX:
            wanted = {t.lower() for t in tags}
            noop = 0
            for nid in live:
                have = {t.lower() for t in col.get_note(nid).tags}
                if op == "add_tags" and wanted <= have:
                    noop += 1
                elif op == "remove_tags" and not any(
                    h == w or h.startswith(w + "::") for h in have for w in wanted
                ):
                    noop += 1
            if noop:
                already = (
                    "already carry all of those tags"
                    if op == "add_tags"
                    else "carry none of those tags"
                )
                warnings.append(f"{noop} of these note(s) {already} (no change)")
        verb = "Add" if op == "add_tags" else "Remove"
        joined = ", ".join(tags)
        samples = [{"text": f"{verb} {joined} — {scope_text}"}]
        for nid in live[:MAX_SAMPLES]:
            try:
                note = col.get_note(nid)
                samples.append(
                    {"text": _short_label(next(iter(dict(note.items()).values()), ""))}
                )
            except Exception:
                continue
        op_args: dict[str, Any] = {"tags": tags}
        if raw_ids:
            op_args["note_ids"] = live
        else:
            op_args["query"] = query
        proposal = Proposal(
            id=self._next_id(),
            kind="bulk",
            op=op,
            op_args=op_args,
            note_type="",
            deck="",
            tags=tags,
            fields={},
            rationale=str(args.get("rationale", "")),
            count=len(live),
            samples=samples,
            warnings=warnings,
        )
        return self._finish_submission(proposal, len(live))

    def submit_clear_unused_tags(self, args: dict[str, Any]) -> dict[str, Any]:
        """Anki's Clear Unused Tags: drop registry entries no note carries."""
        col = self._col()
        used: set[str] = set()
        for (tags_str,) in col.db.all("select distinct tags from notes"):
            for tag in str(tags_str).split():
                used.add(tag.lower())
        unused = [t for t in col.tags.all() if t.lower() not in used]
        if not unused:
            raise ProposalError("no unused tags - the tag list is already clean")
        samples = [{"text": t} for t in unused[:MAX_SAMPLES]]
        if len(unused) > MAX_SAMPLES:
            samples.append({"text": f"… and {len(unused) - MAX_SAMPLES} more"})
        proposal = Proposal(
            id=self._next_id(),
            kind="bulk",
            op="clear_unused_tags",
            op_args={},
            note_type="",
            deck="",
            tags=[],
            fields={},
            rationale=str(args.get("rationale", "")),
            count=len(unused),
            samples=samples,
            warnings=[
                "Removes tag-list entries only; no note changes. Not "
                "revertible from the chat (a tag reappears the moment a note "
                "uses it again)."
            ],
        )
        return self._finish_submission(proposal, len(unused))

    def submit_store_media_asset(self, args: dict[str, Any]) -> dict[str, Any]:
        """Place a non-note asset in collection.media (#10): fonts and CSS
        for styling, shared images templates reference by name. Staged like
        note media so the review card can PREVIEW the file; imported through
        col.media.add_file on accept; revert moves it to Anki's media trash.
        """
        if self._media is None:
            raise ProposalError("media staging is not available in this session")
        from .media_staging import MediaError

        proposal_id = self._next_id()
        try:
            staged = self._media.stage(
                proposal_id,
                [
                    {
                        "path": str(args.get("path", "")),
                        "filename": str(args.get("filename", "") or ""),
                    }
                ],
            )
        except MediaError as exc:
            raise ProposalError(str(exc)) from None
        item = staged[0]
        proposal = Proposal(
            id=proposal_id,
            kind="bulk",
            op="store_media_asset",
            op_args={"filename": item.filename},
            note_type="",
            deck="",
            tags=[],
            fields={},
            rationale=str(args.get("rationale", "")),
            count=1,
            samples=[
                {
                    "text": f'Store "{item.filename}" '
                    f"({item.size // 1024} KB {item.kind}) in the media folder"
                }
            ],
            media=[item.to_payload()],
        )
        return self._finish_submission(proposal, 1)

    def _accept_store_media(self, proposal: Proposal) -> list[int]:
        col = self._col()
        name = str(proposal.op_args["filename"])
        staged_path = self._media.staged_path(proposal.id, name) if self._media else None
        if staged_path is None or not staged_path.is_file():
            raise ProposalError(f"staged file {name!r} is gone (cleaned up?)")
        try:
            final = str(col.media.add_file(str(staged_path)))
        except Exception as exc:
            raise ProposalError(str(exc)) from None
        if final != name:
            proposal.warnings.append(
                f"renamed on import (name already taken): {name} -> {final}"
            )
        self._ledger.append(
            LedgerEntry(
                id=proposal.id,
                kind="bulk",
                note_id=0,
                label=f'store media "{final}"',
                data={"op": "store_media_asset", "final": final},
            )
        )
        if self._media is not None:
            self._media.discard(proposal.id)
        proposal.status = ACCEPTED
        return []

    def submit_delete_deck(self, args: dict[str, Any]) -> dict[str, Any]:
        """Delete a deck (#9a). Destructive for a normal deck - its cards
        die with it - so that case is ALWAYS user-confirmed (even under
        trusted-writes) with a critical backup at apply; a filtered deck is
        the mild case (cards return home)."""
        col = self._col()
        name = str(args.get("deck", "")).strip()
        did, deck = self._deck_by_name(col, name)
        filtered = bool(deck.get("dyn"))
        subdecks = [n for n in self._deck_names(col) if n.startswith(name + "::")]
        escaped = name.replace('"', '\\"')
        cards = len(list(col.find_cards(f'deck:"{escaped}"')))
        warnings: list[str] = []
        if filtered:
            samples = [
                f'Delete filtered deck "{name}" - its {cards} card(s) return '
                "to their home decks with scheduling intact"
            ]
        else:
            samples = [f'Delete deck "{name}"']
            if subdecks:
                samples.append(f"{len(subdecks)} subdeck(s) are deleted with it")
            if cards:
                warnings.append(
                    f"THIS DELETES {cards} CARD(S) and their notes' review "
                    "history with it - not revertible from the chat; a backup "
                    "checkpoint is written first (File > Switch Profile "
                    "restores it)"
                )
        proposal = Proposal(
            id=self._next_id(),
            kind="deck_op",
            op="delete_deck",
            op_args={"deck": name, "filtered": filtered, "cards": cards},
            note_type="",
            deck=name,
            tags=[],
            fields={},
            rationale=str(args.get("rationale", "")),
            count=cards,
            samples=[{"text": line} for line in samples],
            warnings=warnings,
        )
        if not filtered and cards:
            # Destructive: never auto-applies, mirroring delete_notes.
            self._proposals[proposal.id] = proposal
            self._push({"type": "proposal", "proposal": proposal.to_payload()})
            return {
                "status": "pending_user_review",
                "proposal_id": proposal.id,
                "affected": cards,
                "note": "Deleting a deck with cards always requires explicit "
                "user confirmation.",
            }
        return self._finish_submission(proposal, 1)

    def submit_manage_preset(self, args: dict[str, Any]) -> dict[str, Any]:
        """Options-preset lifecycle (#9b): create / clone / rename / delete."""
        col = self._col()
        action = str(args.get("action", "")).strip()
        if action not in ("create", "clone", "rename", "delete"):
            raise ProposalError("action must be create, clone, rename, or delete")
        configs = {str(c.get("name", "")): c for c in col.decks.all_config()}
        name = str(args.get("name", "")).strip()
        preset = str(args.get("preset", "")).strip()
        warnings: list[str] = []
        if action in ("create", "clone"):
            if not name:
                raise ProposalError(f"{action} needs the new preset's `name`")
            if name in configs:
                raise ProposalError(f"a preset named {name!r} already exists")
            if action == "clone":
                if preset not in configs:
                    raise ProposalError(
                        f"no preset named {preset!r} to clone; presets: "
                        + ", ".join(sorted(configs))
                    )
                samples = [f'Clone preset "{preset}" as "{name}"']
            else:
                samples = [f'Create preset "{name}" (Anki defaults)']
            op_args = {"action": action, "name": name, "clone_from": preset}
        elif action == "rename":
            if preset not in configs:
                raise ProposalError(f"no preset named {preset!r}")
            if not name:
                raise ProposalError("rename needs `name` (the new name)")
            if name in configs:
                raise ProposalError(f"a preset named {name!r} already exists")
            samples = [f'Rename preset "{preset}" → "{name}"']
            op_args = {"action": action, "preset": preset, "name": name}
        else:  # delete
            if preset not in configs:
                raise ProposalError(f"no preset named {preset!r}")
            if int(configs[preset].get("id", 0)) == 1:
                raise ProposalError("the Default preset cannot be deleted")
            using = self._decks_sharing_config(col, configs[preset].get("id"))
            samples = [f'Delete preset "{preset}"']
            warnings.append(
                f"{using} deck(s) using it fall back to the Default preset; "
                "deleting a preset forces a one-way sync (check "
                "get_sync_status first). Revert recreates the preset with "
                "the same values and reassigns those decks, but the forced "
                "sync stands."
            )
            op_args = {"action": action, "preset": preset}
        return self._deck_op_proposal(
            op="manage_preset",
            op_args=op_args,
            deck="",
            rationale=str(args.get("rationale", "")),
            samples=samples,
            warnings=warnings,
        )

    def submit_assign_preset(self, args: dict[str, Any]) -> dict[str, Any]:
        """Assign an options preset to a deck (#9b) - with include_subdecks,
        Anki's "Save to All Subdecks". THE correct fix when set_deck_options
        warns about a shared preset: clone, then assign the clone here."""
        col = self._col()
        deck_name = str(args.get("deck", "")).strip()
        preset = str(args.get("preset", "")).strip()
        _did, deck = self._deck_by_name(col, deck_name)
        if deck.get("dyn"):
            raise ProposalError("filtered decks have no options preset")
        configs = {str(c.get("name", "")): c for c in col.decks.all_config()}
        if preset not in configs:
            raise ProposalError(
                f"no preset named {preset!r}; presets: " + ", ".join(sorted(configs))
            )
        include_subdecks = bool(args.get("include_subdecks", False))
        names = [deck_name]
        if include_subdecks:
            for n in self._deck_names(col):
                if n.startswith(deck_name + "::"):
                    _d, child = self._deck_by_name(col, n)
                    if not child.get("dyn"):
                        names.append(n)
        samples = [
            f'Assign preset "{preset}" to "{deck_name}"'
            + (f" and {len(names) - 1} subdeck(s)" if len(names) > 1 else "")
        ]
        return self._deck_op_proposal(
            op="assign_preset",
            op_args={"decks": names, "preset": preset},
            deck=deck_name,
            rationale=str(args.get("rationale", "")),
            samples=samples,
            count=len(names),
        )

    def submit_set_deck_description(self, args: dict[str, Any]) -> dict[str, Any]:
        """Deck description (#9c) - shown on the deck's congrats/overview."""
        col = self._col()
        deck_name = str(args.get("deck", "")).strip()
        _did, deck = self._deck_by_name(col, deck_name)
        description = str(args.get("description", ""))
        old = str(deck.get("desc", ""))
        if description == old:
            raise ProposalError("the description already reads exactly that")
        preview = " ".join(description.split())[:80] or "(cleared)"
        return self._deck_op_proposal(
            op="set_deck_description",
            op_args={"deck": deck_name, "description": description},
            deck=deck_name,
            rationale=str(args.get("rationale", "")),
            samples=[f'Description for "{deck_name}": {preview}'],
        )

    @staticmethod
    def _csv_metadata_for(col: Any, args: dict[str, Any]) -> Any:
        """CSV metadata with the proposal's overrides applied (#11). Proto
        enum names resolve lazily; the unit-test fakes return plain
        namespaces and skip enum mapping entirely."""
        import os

        path = os.path.expanduser(str(args.get("path", "")).strip())
        delimiter = None
        delim_name = str(args.get("delimiter", "") or "").strip().upper()
        if delim_name:
            try:
                from anki.import_export_pb2 import CsvMetadata

                delimiter = CsvMetadata.Delimiter.Value(delim_name)
            except ImportError:
                delimiter = delim_name  # fake path
            except ValueError:
                raise ProposalError(
                    f"unknown delimiter {delim_name!r}; use TAB, SPACE, COMMA, "
                    "SEMICOLON, PIPE or COLON"
                ) from None
        meta = col.get_csv_metadata(path, delimiter)
        deck_name = str(args.get("deck", "") or "").strip()
        if deck_name:
            try:
                did = int(col.decks.id_for_name(deck_name))
            except Exception:
                did = 0
            if not did:
                raise ProposalError(
                    f"deck {deck_name!r} not found - create_deck it first"
                )
            meta.deck_id = did
        notetype_name = str(args.get("note_type", "") or "").strip()
        if notetype_name:
            model = col.models.by_name(notetype_name)
            if model is None:
                raise ProposalError(f"note type {notetype_name!r} not found")
            meta.global_notetype.id = int(model["id"])
        existing = str(args.get("existing_notes", "preserve")).strip().upper()
        try:
            from anki.import_export_pb2 import CsvMetadata

            meta.dupe_resolution = CsvMetadata.DupeResolution.Value(existing)
        except ImportError:
            meta.dupe_resolution = existing  # fake path
        except ValueError:
            raise ProposalError(
                f"existing_notes must be UPDATE, PRESERVE or DUPLICATE; "
                f"got {existing!r}"
            ) from None
        tags = [str(t).strip() for t in (args.get("tags") or []) if str(t).strip()]
        if tags:
            meta.global_tags.extend(tags)
        return meta

    def submit_import_csv(self, args: dict[str, Any]) -> dict[str, Any]:
        """Import text/CSV through Anki's real pipeline (#11) - one review
        card instead of N proposal round-trips. SAFE default: existing_notes
        = preserve (Anki's own default is Update, which rewrites notes
        matched on the first field - here that mode is opt-in and warned in
        capitals)."""
        import os

        col = self._col()
        raw_path = str(args.get("path", "")).strip()
        if not raw_path:
            raise ProposalError("import_csv needs a file path")
        path = os.path.expanduser(raw_path)
        if not os.path.isfile(path):
            raise ProposalError(f"no file at {path}")
        try:
            meta = self._csv_metadata_for(col, args)
        except ProposalError:
            raise
        except Exception as exc:
            raise ProposalError(f"could not read the file's structure: {exc}") from None
        with open(path, encoding="utf-8", errors="replace") as handle:
            data_rows = sum(
                1 for line in handle if line.strip() and not line.startswith("#")
            )
        preview = list(getattr(meta, "preview", []))[:3]
        samples = [f"≈{data_rows} data row(s) from {os.path.basename(path)}"]
        for row in preview:
            values = list(getattr(row, "vals", row if isinstance(row, list) else []))
            samples.append(" · ".join(str(v) for v in values[:4])[:80])
        existing = str(args.get("existing_notes", "preserve")).lower()
        warnings: list[str] = []
        if existing == "update":
            warnings.append(
                "UPDATE MODE: rows matching an existing note's first field "
                "REWRITE that note - a bad column mapping can overwrite the "
                "wrong notes en masse. The resolved card reports exactly what "
                "was updated vs created."
            )
        proposal = Proposal(
            id=self._next_id(),
            kind="bulk",
            op="import_csv",
            op_args={
                "path": path,
                "deck": str(args.get("deck", "") or ""),
                "note_type": str(args.get("note_type", "") or ""),
                "existing_notes": existing,
                "delimiter": str(args.get("delimiter", "") or ""),
                "tags": [str(t) for t in (args.get("tags") or [])],
            },
            note_type=str(args.get("note_type", "") or ""),
            deck=str(args.get("deck", "") or ""),
            tags=[],
            fields={},
            rationale=str(args.get("rationale", "")),
            count=data_rows,
            samples=[{"text": line} for line in samples],
            warnings=warnings,
        )
        return self._finish_submission(proposal, data_rows)

    def _accept_import_csv(self, proposal: Proposal) -> list[int]:
        col = self._col()
        a = proposal.op_args
        if not self._checkpoint("csv import", False):
            proposal.warnings.append(
                "backup checkpoint failed (csv import); proceeding without a "
                "fresh backup — check disk space / permissions"
            )
        try:
            meta = self._csv_metadata_for(col, a)
        except ProposalError:
            raise
        except Exception as exc:
            raise ProposalError(f"file unreadable at apply time: {exc}") from None
        try:
            try:
                from anki.import_export_pb2 import ImportCsvRequest

                request: Any = ImportCsvRequest(path=a["path"], metadata=meta)
            except ImportError:
                from types import SimpleNamespace

                request = SimpleNamespace(path=a["path"], metadata=meta)
            log = col.import_csv(request).log
        except ProposalError:
            raise
        except Exception as exc:
            raise ProposalError(f"import failed: {exc}") from None
        created = [int(entry.id.nid) for entry in getattr(log, "new", [])]
        updated = len(list(getattr(log, "updated", [])))
        # Probed on 25.09: preserve-mode matches land in first_field_match,
        # NOT duplicate (which holds exact-content dupes).
        skipped = len(list(getattr(log, "duplicate", []))) + len(
            list(getattr(log, "first_field_match", []))
        )
        conflicting = len(list(getattr(log, "conflicting", [])))
        outcome = (
            f"imported: {len(created)} new, {updated} updated, "
            f"{skipped} existing note(s) left untouched"
        )
        if conflicting:
            outcome += f", {conflicting} conflicting"
        proposal.warnings = list(proposal.warnings) + [outcome]
        if updated:
            proposal.warnings.append(
                f"the {updated} updated note(s) cannot be restored from the "
                "chat ledger - the backup checkpoint is the way back"
            )
        self._ledger.append(
            LedgerEntry(
                id=proposal.id,
                kind="bulk",
                note_id=0,
                label=f"csv import ({len(created)} new, {updated} updated)",
                data={"op": "import_csv", "created": created},
            )
        )
        proposal.status = ACCEPTED
        self._after_deck_change()
        return created

    # ---- note-type write path (#7) ----

    def _note_type_by_name(self, col: Any, name: Any) -> Any:
        """Resolve a note type by exact name, with the available names in the
        error - the agent has no way to guess a name it got slightly wrong."""
        clean = str(name or "").strip()
        if not clean:
            raise ProposalError("note_type is required")
        model = col.models.by_name(clean)
        if model is None:
            names = sorted(nt.name for nt in col.models.all_names_and_ids())
            raise ProposalError(f"note type {clean!r} not found; available: {names}")
        return model

    def _note_type_blast_radius(self, col: Any, model: Any) -> list[str]:
        """Every op in this family is collection-wide by construction. Say how
        wide, in notes and decks, before the user commits."""
        try:
            note_ids = list(col.models.nids(model["id"]))
        except Exception:
            note_ids = []
        if not note_ids:
            return ["no notes use this note type yet"]
        decks: set[str] = set()
        try:
            ids_sql = "(" + ",".join(str(int(n)) for n in note_ids) + ")"
            for did in col.db.list(
                "select distinct coalesce(nullif(c.odid, 0), c.did) from cards c "
                f"where c.nid in {ids_sql}"
            ):
                decks.add(col.decks.name(did))
        except Exception:
            pass
        where = f" across {len(decks)} deck(s)" if decks else ""
        return [f"used by {len(note_ids)} note(s){where}"]

    def _note_type_op_proposal(
        self,
        *,
        op: str,
        op_args: dict[str, Any],
        note_type: str,
        rationale: str,
        samples: list[str],
        count: int = 0,
        warnings: list[str] | None = None,
        revertible: bool | None = None,
    ) -> dict[str, Any]:
        meta = NOTE_TYPE_OPS[op]
        lines = list(warnings or [])
        if meta["full_sync"]:
            lines.append(
                "structural change: your next sync will be a full upload "
                "(Anki will ask which side wins - choose this device)"
            )
        can_revert = meta["revertible"] if revertible is None else revertible
        proposal = Proposal(
            id=self._next_id(),
            kind="note_type_op",
            op=op,
            op_args=dict(op_args),
            revertible=can_revert,
            note_type=note_type,
            deck="",
            tags=[],
            fields={},
            rationale=rationale,
            count=count,
            samples=[{"text": line} for line in samples],
            warnings=lines,
        )
        return self._finish_submission(proposal, 1)

    @staticmethod
    def _require_source(value: Any, label: str) -> str:
        text = "" if value is None else str(value)
        if len(text) > MAX_NOTE_TYPE_SOURCE_CHARS:
            raise ProposalError(
                f"{label} is {len(text)} chars; the cap is "
                f"{MAX_NOTE_TYPE_SOURCE_CHARS}"
            )
        return text

    def submit_set_note_type_styling(self, args: dict[str, Any]) -> dict[str, Any]:
        col = self._col()
        model = self._note_type_by_name(col, args.get("note_type"))
        css = self._require_source(args.get("css"), "css")
        if not css.strip():
            raise ProposalError("css must not be empty (pass the full stylesheet)")
        current = str(model.get("css", ""))
        if css == current:
            raise ProposalError("no effective change: the CSS already matches")
        return self._note_type_op_proposal(
            op="set_note_type_styling",
            op_args={"note_type": model["name"], "css": css},
            note_type=model["name"],
            rationale=str(args.get("rationale", "")),
            samples=[
                f'Replace the CSS of "{model["name"]}" '
                f"({len(current)} → {len(css)} chars)"
            ],
            warnings=self._note_type_blast_radius(col, model),
        )

    def submit_set_card_template(self, args: dict[str, Any]) -> dict[str, Any]:
        col = self._col()
        model = self._note_type_by_name(col, args.get("note_type"))
        name = str(args.get("template", "")).strip()
        if not name:
            raise ProposalError("template is required (the card type's name)")
        match = next((t for t in model["tmpls"] if t["name"] == name), None)
        if match is None:
            raise ProposalError(
                f"card template {name!r} not found on {model['name']!r}; "
                f"available: {[t['name'] for t in model['tmpls']]}"
            )
        qfmt = args.get("qfmt")
        afmt = args.get("afmt")
        if qfmt is None and afmt is None:
            raise ProposalError("pass qfmt and/or afmt (the full new source)")
        new_q = self._require_source(qfmt, "qfmt") if qfmt is not None else match["qfmt"]
        new_a = self._require_source(afmt, "afmt") if afmt is not None else match["afmt"]
        if new_q == match["qfmt"] and new_a == match["afmt"]:
            raise ProposalError("no effective change: the template already matches")
        if not new_q.strip():
            raise ProposalError("the front (qfmt) must not be empty")
        if not _FIELD_REF_RE.search(new_q):
            raise ProposalError(
                "the front references no field, so every note would render the "
                "same card; include at least one {{Field}}"
            )
        changed = []
        if new_q != match["qfmt"]:
            changed.append("front")
        if new_a != match["afmt"]:
            changed.append("back")
        return self._note_type_op_proposal(
            op="set_card_template",
            op_args={
                "note_type": model["name"],
                "template": name,
                "qfmt": new_q,
                "afmt": new_a,
            },
            note_type=model["name"],
            rationale=str(args.get("rationale", "")),
            samples=[
                f'Rewrite the {" and ".join(changed)} of "{name}" '
                f'on "{model["name"]}"'
            ],
            warnings=self._note_type_blast_radius(col, model),
        )

    def submit_manage_note_type_fields(self, args: dict[str, Any]) -> dict[str, Any]:
        col = self._col()
        model = self._note_type_by_name(col, args.get("note_type"))
        sub = str(args.get("op", "")).strip().lower()
        if sub not in FIELD_SUBOPS:
            raise ProposalError(f"op must be one of {list(FIELD_SUBOPS)}")
        field = str(args.get("field", "")).strip()
        if not field:
            raise ProposalError("field is required")
        names = [f["name"] for f in model["flds"]]
        warnings = self._note_type_blast_radius(col, model)
        samples: list[str] = []
        op_args: dict[str, Any] = {
            "note_type": model["name"],
            "op": sub,
            "field": field,
        }
        revertible: bool | None = None

        if sub == "add":
            if field in names:
                raise ProposalError(f"{model['name']!r} already has a field {field!r}")
            samples.append(f'Add field "{field}" to "{model["name"]}"')
            warnings.append("existing notes get the new field empty")
        else:
            if field not in names:
                raise ProposalError(
                    f"{model['name']!r} has no field {field!r}; available: {names}"
                )
            index = names.index(field)
            if sub == "rename":
                new_name = str(args.get("new_name", "")).strip()
                if not new_name:
                    raise ProposalError("rename needs new_name")
                if new_name == field:
                    raise ProposalError("new_name matches the current name")
                if new_name in names:
                    raise ProposalError(f"a field named {new_name!r} already exists")
                op_args["new_name"] = new_name
                samples.append(f'Rename field "{field}" → "{new_name}"')
                warnings.append(
                    "Anki rewrites every template reference to this field, and "
                    "note content follows the rename"
                )
            elif sub == "reposition":
                position = args.get("position")
                if position is None:
                    raise ProposalError("reposition needs position (0-based)")
                try:
                    position = int(position)
                except (TypeError, ValueError):
                    raise ProposalError("position must be an integer") from None
                if not 0 <= position < len(names):
                    raise ProposalError(
                        f"position must be between 0 and {len(names) - 1}"
                    )
                if position == index:
                    raise ProposalError("the field is already at that position")
                op_args["position"] = position
                samples.append(
                    f'Move field "{field}" from position {index} to {position}'
                )
                if position == 0 or index == 0:
                    warnings.append(
                        "this changes the FIRST field, which Anki uses for "
                        "duplicate detection and browser sorting"
                    )
            else:  # remove
                if len(names) == 1:
                    raise ProposalError("a note type must keep at least one field")
                if index == 0:
                    raise ProposalError(
                        "the first field is the duplicate/sort key; reposition "
                        "another field to the front before removing this one"
                    )
                filled = self._field_content_count(col, model, field)
                op_args["filled"] = filled
                revertible = False
                samples.append(f'Remove field "{field}" from "{model["name"]}"')
                if filled:
                    warnings.append(
                        f"{filled} note(s) have content in this field - it is "
                        "destroyed collection-wide and CANNOT be undone from "
                        "the dock (a backup is taken first)"
                    )
                else:
                    warnings.append(
                        "no note has content in this field, but the removal "
                        "still cannot be undone from the dock"
                    )
                users = self._templates_referencing(model, field)
                if users:
                    warnings.append(
                        "template(s) " + ", ".join(repr(u) for u in users) + " "
                        "reference this field: Anki SILENTLY REWRITES them to "
                        "point at a different field, which can turn a "
                        "conditional front unconditional and generate a card on "
                        "every note (probed on 25.x). The applied card is "
                        "reported after the change."
                    )

        return self._note_type_op_proposal(
            op="manage_note_type_fields",
            op_args=op_args,
            note_type=model["name"],
            rationale=str(args.get("rationale", "")),
            samples=samples,
            warnings=warnings,
            revertible=revertible,
        )

    @staticmethod
    def _field_content_count(col: Any, model: Any, field: str) -> int:
        """How many notes actually have something in this field - the number
        that makes 'this destroys content' concrete instead of theoretical."""
        names = [f["name"] for f in model["flds"]]
        index = names.index(field)
        filled = 0
        try:
            for (flds,) in col.db.all(
                "select flds from notes where mid = ?", model["id"]
            ):
                parts = flds.split("\x1f")
                if index < len(parts) and parts[index].strip():
                    filled += 1
        except Exception:
            return 0
        return filled

    @staticmethod
    def _templates_referencing(model: Any, field: str) -> list[str]:
        """Templates whose source mentions this field, in any of Anki's three
        reference forms: plain, conditional, negated-conditional."""
        forms = ("{{" + field + "}}", "{{#" + field + "}}", "{{^" + field + "}}")
        hits = []
        for tmpl in model["tmpls"]:
            source = str(tmpl.get("qfmt", "")) + str(tmpl.get("afmt", ""))
            if any(form in source for form in forms):
                hits.append(tmpl["name"])
        return hits

    def _resolve_note_selection(
        self, col: Any, args: dict[str, Any], op: str, *, default_all_of: Any = None
    ) -> tuple[list[int], str, list[str], dict[str, Any]]:
        """Note-level twin of _resolve_card_selection. At most one of note_ids
        or query; with neither, `default_all_of` means every note of that note
        type - the overwhelmingly common intent for a conversion, and stating
        it explicitly beats making the agent construct a `note:` search."""
        raw_ids = args.get("note_ids") or []
        query = str(args.get("query") or "").strip()
        if raw_ids and query:
            raise ProposalError(f"{op} takes at most one of note_ids or query")
        warnings: list[str] = []
        if raw_ids:
            ids = list(dict.fromkeys(int(n) for n in raw_ids))
            if len(ids) > MAX_EXPLICIT_CARD_IDS:
                raise ProposalError(
                    f"{len(ids)} explicit note_ids is too many "
                    f"(max {MAX_EXPLICIT_CARD_IDS}); pass a query instead"
                )
            live = []
            for nid in ids:
                try:
                    col.get_note(nid)
                except Exception:
                    continue
                live.append(nid)
            missing = len(ids) - len(live)
            if missing:
                warnings.append(f"{missing} note id(s) no longer exist and are skipped")
            return live, f"{len(live)} selected note(s)", warnings, {"note_ids": ids}
        if query:
            try:
                live = [int(n) for n in col.find_notes(query)]
            except Exception as exc:
                raise ProposalError(f"invalid search {query!r}: {exc}") from None
            return live, f"search {query!r}", warnings, {"query": query}
        if default_all_of is None:
            raise ProposalError(f"{op} needs note_ids or query")
        live = [int(n) for n in col.models.nids(default_all_of["id"])]
        return (
            live,
            f'every note of "{default_all_of["name"]}"',
            warnings,
            {"all_of_note_type": default_all_of["name"]},
        )

    def submit_manage_card_templates(self, args: dict[str, Any]) -> dict[str, Any]:
        col = self._col()
        model = self._note_type_by_name(col, args.get("note_type"))
        sub = str(args.get("op", "")).strip().lower()
        if sub not in TEMPLATE_SUBOPS:
            raise ProposalError(f"op must be one of {list(TEMPLATE_SUBOPS)}")
        name = str(args.get("template", "")).strip()
        if not name:
            raise ProposalError("template is required")
        names = [t["name"] for t in model["tmpls"]]
        note_count = len(list(col.models.nids(model["id"])))
        warnings = self._note_type_blast_radius(col, model)
        samples: list[str] = []
        op_args: dict[str, Any] = {
            "note_type": model["name"],
            "op": sub,
            "template": name,
        }
        revertible: bool | None = None

        if sub == "add":
            if name in names:
                raise ProposalError(f"a card template named {name!r} already exists")
            qfmt = self._require_source(args.get("qfmt"), "qfmt")
            afmt = self._require_source(args.get("afmt"), "afmt")
            if not qfmt.strip() or not afmt.strip():
                raise ProposalError("a new template needs both qfmt and afmt")
            if not _FIELD_REF_RE.search(qfmt):
                raise ProposalError(
                    "the front references no field, so every note would render "
                    "the same card; include at least one {{Field}}"
                )
            op_args["qfmt"] = qfmt
            op_args["afmt"] = afmt
            samples.append(f'Add card template "{name}" to "{model["name"]}"')
            conditional = qfmt.lstrip().startswith("{{#") or "{{#" in qfmt
            warnings.append(
                f"this generates up to {note_count} new card(s) - one per note"
                + (
                    " (fewer if the front is conditional and the field is empty)"
                    if conditional
                    else ""
                )
            )
        else:
            if name not in names:
                raise ProposalError(
                    f"{model['name']!r} has no card template {name!r}; "
                    f"available: {names}"
                )
            index = names.index(name)
            if sub == "rename":
                new_name = str(args.get("new_name", "")).strip()
                if not new_name:
                    raise ProposalError("rename needs new_name")
                if new_name == name:
                    raise ProposalError("new_name matches the current name")
                if new_name in names:
                    raise ProposalError(f"a template named {new_name!r} already exists")
                op_args["new_name"] = new_name
                samples.append(f'Rename card template "{name}" → "{new_name}"')
            elif sub == "reposition":
                position = args.get("position")
                if position is None:
                    raise ProposalError("reposition needs position (0-based)")
                try:
                    position = int(position)
                except (TypeError, ValueError):
                    raise ProposalError("position must be an integer") from None
                if not 0 <= position < len(names):
                    raise ProposalError(
                        f"position must be between 0 and {len(names) - 1}"
                    )
                if position == index:
                    raise ProposalError("the template is already at that position")
                op_args["position"] = position
                samples.append(
                    f'Move card template "{name}" from {index} to {position}'
                )
            else:  # remove
                if len(names) == 1:
                    raise ProposalError(
                        "a note type must keep at least one card template"
                    )
                try:
                    doomed = int(col.models.template_use_count(model["id"], index))
                except Exception:
                    doomed = 0
                op_args["cards"] = doomed
                revertible = False
                samples.append(
                    f'Remove card template "{name}" from "{model["name"]}"'
                )
                warnings.append(
                    f"{doomed} card(s) and their entire review history are "
                    "deleted - this CANNOT be undone from the dock (a backup is "
                    "taken first)"
                )

        return self._note_type_op_proposal(
            op="manage_card_templates",
            op_args=op_args,
            note_type=model["name"],
            rationale=str(args.get("rationale", "")),
            samples=samples,
            count=note_count,
            warnings=warnings,
            revertible=revertible,
        )

    def submit_create_note_type(self, args: dict[str, Any]) -> dict[str, Any]:
        col = self._col()
        name = str(args.get("name", "")).strip()
        if not name:
            raise ProposalError("create_note_type needs a name")
        if col.models.by_name(name) is not None:
            raise ProposalError(f"a note type named {name!r} already exists")
        source = self._note_type_by_name(col, args.get("clone_from"))
        return self._note_type_op_proposal(
            op="create_note_type",
            op_args={"name": name, "clone_from": source["name"]},
            note_type=name,
            rationale=str(args.get("rationale", "")),
            samples=[
                f'Create note type "{name}" as a copy of "{source["name"]}" '
                f"({len(source['flds'])} field(s), {len(source['tmpls'])} "
                "card template(s))"
            ],
            warnings=[
                "the copy starts with no notes; existing notes are untouched"
            ],
        )

    def submit_change_note_type(self, args: dict[str, Any]) -> dict[str, Any]:
        col = self._col()
        old = self._note_type_by_name(col, args.get("note_type"))
        new = self._note_type_by_name(col, args.get("new_note_type"))
        if old["id"] == new["id"]:
            raise ProposalError("the notes already use that note type")
        live, scope_text, warnings, selection = self._resolve_note_selection(
            col, args, "change_note_type", default_all_of=old
        )
        note_ids = [
            nid for nid in live if col.get_note(nid).note_type()["id"] == old["id"]
        ]
        if not note_ids:
            raise ProposalError(f"no note in {scope_text} uses {old['name']!r}")
        skipped = len(live) - len(note_ids)
        if skipped:
            warnings.append(
                f"{skipped} selected note(s) do not use {old['name']!r} and are "
                "left alone"
            )

        old_fields = [f["name"] for f in old["flds"]]
        new_fields = [f["name"] for f in new["flds"]]
        old_templates = [t["name"] for t in old["tmpls"]]
        new_templates = [t["name"] for t in new["tmpls"]]

        field_map = self._coerce_map(
            args.get("field_map"), old_fields, new_fields, "field_map"
        )
        template_map = self._coerce_map(
            args.get("template_map"), old_templates, new_templates, "template_map"
        )
        dropped_fields = [name for name in old_fields if name not in field_map]
        dropped_templates = [
            name for name in old_templates if name not in template_map
        ]
        samples = [
            f'Convert {len(note_ids)} note(s) from "{old["name"]}" to '
            f'"{new["name"]}"'
        ]
        samples += [f"{src} → {dst}" for src, dst in sorted(field_map.items())]
        if dropped_fields:
            warnings.append(
                "field content DESTROYED (mapped nowhere): "
                + ", ".join(dropped_fields)
            )
        if dropped_templates:
            warnings.append(
                "card(s) and their review history DESTROYED (template mapped "
                "nowhere): " + ", ".join(dropped_templates)
            )
        warnings.append("this cannot be undone from the dock (a backup is taken first)")
        return self._note_type_op_proposal(
            op="change_note_type",
            op_args={
                "note_type": old["name"],
                "new_note_type": new["name"],
                "note_ids": note_ids,
                "field_map": field_map,
                "template_map": template_map,
                "selection": selection,
            },
            note_type=old["name"],
            rationale=str(args.get("rationale", "")),
            samples=samples,
            count=len(note_ids),
            warnings=warnings,
        )

    @staticmethod
    def _coerce_map(
        raw: Any, old_names: list[str], new_names: list[str], label: str
    ) -> dict[str, str]:
        """`{old name: new name}`, defaulting to same-name pairs. Anything left
        unmapped is dropped on conversion, which is why the default is
        name-matching rather than positional: a positional default silently
        moves content between unrelated fields."""
        if raw is None:
            return {name: name for name in old_names if name in new_names}
        if not isinstance(raw, dict):
            raise ProposalError(f"{label} must be an object of old name -> new name")
        mapping: dict[str, str] = {}
        used: set[str] = set()
        for src, dst in raw.items():
            src = str(src).strip()
            if src not in old_names:
                raise ProposalError(
                    f"{label}: {src!r} is not on the source note type; "
                    f"available: {old_names}"
                )
            if dst is None or not str(dst).strip():
                continue  # explicitly dropped
            dst = str(dst).strip()
            if dst not in new_names:
                raise ProposalError(
                    f"{label}: {dst!r} is not on the target note type; "
                    f"available: {new_names}"
                )
            if dst in used:
                raise ProposalError(
                    f"{label}: two sources both map onto {dst!r}; one would win "
                    "silently"
                )
            used.add(dst)
            mapping[src] = dst
        return mapping

    def submit_remove_empty_cards(self, args: dict[str, Any]) -> dict[str, Any]:
        col = self._col()
        report = col.get_empty_cards()
        entries: list[dict[str, Any]] = [
            {
                "note_id": int(entry.note_id),
                "card_ids": [int(cid) for cid in entry.card_ids],
                "will_delete_note": bool(entry.will_delete_note),
            }
            for entry in report.notes
        ]
        if not entries:
            raise ProposalError(
                "no empty cards found - nothing to remove"
            )
        card_total = sum(len(list(e["card_ids"])) for e in entries)
        doomed_notes = [e for e in entries if e["will_delete_note"]]
        samples = [
            f"{card_total} empty card(s) across {len(entries)} note(s)"
        ]
        for entry in entries[:10]:
            try:
                note = col.get_note(entry["note_id"])
                label = _short_label(next(iter(note.values()), ""))
            except Exception:
                label = f"note {entry['note_id']}"
            samples.append(
                f"{label} - {len(list(entry['card_ids']))} card(s)"
                + (" (the note goes too)" if entry["will_delete_note"] else "")
            )
        if len(entries) > 10:
            samples.append(f"… and {len(entries) - 10} more note(s)")
        warnings = []
        if doomed_notes:
            warnings.append(
                f"{len(doomed_notes)} note(s) lose their LAST card and are "
                "deleted outright, content and all"
            )
        warnings.append("this cannot be undone from the dock (a backup is taken first)")
        return self._note_type_op_proposal(
            op="remove_empty_cards",
            op_args={"entries": entries},
            note_type="",
            rationale=str(args.get("rationale", "")),
            samples=samples,
            count=card_total,
            warnings=warnings,
        )

    def submit_manage_saved_search(self, args: dict[str, Any]) -> dict[str, Any]:
        """Saved searches (#12b): Browse-sidebar entries in collection
        config - the curricula shipping vehicle (filtered decks do not
        survive .apkg export; saved searches do)."""
        col = self._col()
        action = str(args.get("action", "")).strip()
        if action not in ("save", "delete"):
            raise ProposalError("action must be 'save' or 'delete'")
        name = str(args.get("name", "")).strip()
        if not name:
            raise ProposalError("a saved search needs a name")
        saved = dict(col.get_config("savedFilters", {}) or {})
        if action == "save":
            query = str(args.get("query", "")).strip()
            if not query:
                raise ProposalError("save needs a query")
            try:
                col.find_cards(query)
            except Exception as exc:
                raise ProposalError(f"invalid search {query!r}: {exc}") from None
            verb = "Update" if name in saved else "Save"
            samples = [f'{verb} saved search "{name}": {query}']
            op_args = {"action": action, "name": name, "query": query}
        else:
            if name not in saved:
                raise ProposalError(
                    f"no saved search named {name!r}; saved: "
                    + (", ".join(sorted(saved)) or "(none)")
                )
            samples = [f'Delete saved search "{name}" ({saved[name]})']
            op_args = {"action": action, "name": name}
        return self._deck_op_proposal(
            op="saved_search",
            op_args=op_args,
            deck="",
            rationale=str(args.get("rationale", "")),
            samples=samples,
        )

    def submit_undo_change(self, args: dict[str, Any]) -> dict[str, Any]:
        """Undo Anki's queue head (#8) - INSPECTED, never blind.

        After any intervening GUI action the head may not be ours (it could
        be the user's own review answer), so the card names exactly what
        would be undone, and apply re-inspects: a moved queue is a clean
        error, never a misfire.
        """
        col = self._col()
        try:
            status = col.undo_status()
        except Exception as exc:
            raise ProposalError(f"undo status unavailable: {exc}") from None
        label = str(getattr(status, "undo", "") or "")
        if not label:
            raise ProposalError("there is nothing to undo right now")
        return self._deck_op_proposal(
            op="undo_change",
            op_args={"expected": label, "step": int(getattr(status, "last_step", 0))},
            deck="",
            rationale=str(args.get("rationale", "")),
            samples=[f'Undo "{label}" (the current head of Anki\'s undo queue)'],
            warnings=[
                "this may be the user's own most recent action - the label "
                "above is exactly what will be undone"
            ],
        )

    def submit_check_database(self, args: dict[str, Any]) -> dict[str, Any]:
        """Anki's Check Database (#8): integrity check + rebuild."""
        return self._deck_op_proposal(
            op="check_database",
            op_args={},
            deck="",
            rationale=str(args.get("rationale", "")),
            samples=["Run Anki's full database integrity check"],
            warnings=[
                "the collection is unresponsive while it runs (seconds to "
                "minutes on large collections); a backup checkpoint is taken "
                "first; not revertible - it is a repair"
            ],
        )

    def submit_sync_now(self, args: dict[str, Any]) -> dict[str, Any]:
        if self._sync_now is None:
            raise ProposalError("sync is not available in this session")
        return self._deck_op_proposal(
            op="sync_now",
            op_args={},
            deck="",
            rationale=str(args.get("rationale", "")),
            samples=["Start an AnkiWeb sync (Anki's own sync window takes over)"],
            warnings=[
                "not revertible from here; if a full one-way sync is needed, "
                "Anki will ask for the direction itself"
            ],
        )

    def submit_card_state(self, args: dict[str, Any]) -> dict[str, Any]:
        """Card-state ops (#3): suspend/unsuspend, bury/unbury, flags.

        One shared submit path because the five ops have the same shape:
        select cards (explicit ids OR a query - exactly one), freeze an
        honest count, warn about no-ops and filtered-deck side effects,
        and stash enough in op_args for accept to re-resolve.
        """
        col = self._col()
        op = str(args.get("op", ""))
        if op not in CARD_STATE_OPS:
            raise ProposalError(f"unknown card-state op {op!r}")
        flag: int | None = None
        if op == "set_card_flag":
            if "flag" not in args:
                raise ProposalError(
                    "set_card_flag needs flag 0-7 (0 clears; "
                    + ", ".join(f"{n} {FLAG_NAMES[n]}" for n in range(1, 8))
                    + ")"
                )
            flag = int(args["flag"])
            if not 0 <= flag <= 7:
                raise ProposalError(f"flag must be 0-7, got {flag}")
        live, scope_text, warnings, selection = self._resolve_card_selection(
            col, args, op
        )
        # Honest accounting, bounded: inspecting every card of a huge query
        # would stall submit, and the warnings are advisory - accept captures
        # true prior state per card regardless.
        if len(live) <= CARD_STATE_INSPECT_MAX:
            noop = 0
            in_filtered = 0
            for cid in live:
                card = col.get_card(cid)
                queue = int(card.queue)
                if op == "suspend_cards" and queue == -1:
                    noop += 1
                elif op == "unsuspend_cards" and queue != -1:
                    noop += 1
                elif op == "bury_cards" and queue < 0:
                    noop += 1
                elif op == "unbury_cards" and queue not in (-2, -3):
                    noop += 1
                elif op == "set_card_flag" and (int(card.flags) & 0x7) == flag:
                    noop += 1
                in_filtered_deck = int(getattr(card, "odid", 0) or 0) != 0
                if op in ("suspend_cards", "bury_cards") and in_filtered_deck:
                    in_filtered += 1
            if noop:
                already = {
                    "suspend_cards": "already suspended",
                    "unsuspend_cards": "not suspended",
                    "bury_cards": "already buried or suspended",
                    "unbury_cards": "not buried",
                    "set_card_flag": f"already flagged {FLAG_NAMES[flag or 0]}",
                }[op]
                warnings.append(f"{noop} of these card(s) are {already} (no change)")
            if in_filtered:
                warnings.append(
                    f"{in_filtered} card(s) sit in a filtered deck; this returns "
                    "them to their home deck, and undo will not re-add them to "
                    "the filtered deck"
                )
        verb = CARD_STATE_OPS[op]
        if op == "set_card_flag":
            verb = (
                f"Flag {FLAG_NAMES[flag or 0]}" if flag else "Clear the flag on"
            )
        samples = [{"text": f"{verb} {scope_text}"}]
        for cid in live[:MAX_SAMPLES]:
            try:
                note = col.get_card(cid).note()
                samples.append(
                    {"text": _short_label(next(iter(dict(note.items()).values()), ""))}
                )
            except Exception:
                continue
        op_args: dict[str, Any] = dict(selection)
        if flag is not None:
            op_args["flag"] = flag
        proposal = Proposal(
            id=self._next_id(),
            kind="bulk",
            op=op,
            op_args=op_args,
            note_type="",
            deck="",
            tags=[],
            fields={},
            # No fallback rationale: samples[0] already carries "{verb} {scope}",
            # and echoing it as the rationale rendered the same sentence twice
            # on the card (seen in the dev preview).
            rationale=str(args.get("rationale", "")),
            count=len(live),
            samples=samples,
            warnings=warnings,
        )
        return self._finish_submission(proposal, len(live))

    def _resolve_card_selection(
        self, col: Any, args: dict[str, Any], op: str
    ) -> tuple[list[int], str, list[str], dict[str, Any]]:
        """Shared card-selection contract (#3/#6): exactly one of card_ids or
        query; returns (live ids, human scope text, warnings, the op_args
        fragment recording how the selection was made)."""
        raw_ids = args.get("card_ids") or []
        query = str(args.get("query") or "").strip()
        if bool(raw_ids) == bool(query):
            raise ProposalError(f"{op} needs exactly one of card_ids or query")
        warnings: list[str] = []
        if raw_ids:
            ids = list(dict.fromkeys(int(c) for c in raw_ids))
            if len(ids) > MAX_EXPLICIT_CARD_IDS:
                raise ProposalError(
                    f"{len(ids)} explicit card_ids is too many "
                    f"(max {MAX_EXPLICIT_CARD_IDS}); pass a query instead"
                )
            live = []
            missing = 0
            for cid in ids:
                try:
                    col.get_card(cid)
                except Exception:
                    missing += 1
                    continue
                live.append(cid)
            if not live:
                raise ProposalError("none of those cards exist")
            if missing:
                warnings.append(f"{missing} card id(s) do not exist and were dropped")
            return live, f"{len(live)} selected card(s)", warnings, {"card_ids": live}
        try:
            live = [int(c) for c in col.find_cards(query)]
        except Exception as exc:
            raise ProposalError(f"bad query {query!r}: {exc}") from None
        if not live:
            raise ProposalError(
                self._empty_query_error(col, query, f"no cards match {query!r}")
            )
        return (
            live,
            f"{len(live)} card(s) matching {query!r}",
            warnings,
            {"query": query},
        )

    @staticmethod
    def _sched_summary(card: Any, today: int) -> str:
        """One-line scheduling state for the proposal card's before/after diff."""
        ctype = int(card.type)
        if ctype == 0:
            return f"new · position {int(card.due)}"
        if int(card.queue) == -1:
            state = "suspended"
        elif ctype in (1, 3):
            state = "learning"
        else:
            state = "review"
        due = int(card.odue or card.due) if int(getattr(card, "odid", 0)) else int(card.due)
        parts = [state]
        if ctype in (2, 3):
            delta = due - today
            parts.append(f"due in {delta}d" if delta >= 0 else f"overdue {-delta}d")
            parts.append(f"ivl {int(card.ivl)}d")
        return " · ".join(parts)

    def submit_scheduling(self, args: dict[str, Any]) -> dict[str, Any]:
        """Scheduling writes (#6): Set Due Date / Forget / Reposition.

        These rewrite scheduling state in bulk, so the proposal card gets a
        LOUDER diff than a note edit: per-sample before/after scheduling
        lines plus the total count. Revert is an exact update_card restore
        of every captured field (_SCHED_FIELDS) - including the new->review
        conversion and the FSRS memory state forget destroys.
        """
        col = self._col()
        op = str(args.get("op", ""))
        if op not in SCHEDULING_OPS:
            raise ProposalError(f"unknown scheduling op {op!r}")
        op_params: dict[str, Any] = {}
        if op == "set_due_date":
            days = str(args.get("days", "")).strip()
            if not _DUE_DATE_RE.match(days):
                raise ProposalError(
                    f"invalid days {days!r}: use 'n' (days from today), 'n-m' "
                    "(random in range), optionally ending in '!' to also set "
                    "the interval"
                )
            op_params["days"] = days
        elif op == "forget_cards":
            op_params["restore_position"] = bool(args.get("restore_position", False))
            op_params["reset_counts"] = bool(args.get("reset_counts", False))
        else:
            op_params["starting_from"] = max(0, int(args.get("starting_from", 0)))
            op_params["step_size"] = max(1, int(args.get("step_size", 1)))
            op_params["randomize"] = bool(args.get("randomize", False))
            op_params["shift_existing"] = bool(args.get("shift_existing", False))

        live, scope_text, warnings, selection = self._resolve_card_selection(
            col, args, op
        )
        today = int(col.sched.today)

        headline = {
            "set_due_date": f"Set due date to {op_params.get('days')} — {scope_text}",
            "forget_cards": f"Forget (reset to new) — {scope_text}",
            "reposition_new_cards": (
                f"Reposition from {op_params.get('starting_from')} "
                f"step {op_params.get('step_size')} — {scope_text}"
            ),
        }[op]
        samples: list[dict[str, Any]] = [{"text": headline}]
        if len(live) <= CARD_STATE_INSPECT_MAX:
            new_count = 0
            position = op_params.get("starting_from", 0)
            for index, cid in enumerate(live):
                card = col.get_card(cid)
                if int(card.type) == 0:
                    new_count += 1
                if index < MAX_SAMPLES:
                    try:
                        note = card.note()
                        label = _short_label(
                            next(iter(dict(note.items()).values()), "")
                        )
                    except Exception:
                        label = f"card {cid}"
                    samples.append(
                        {
                            "label": label,
                            "old": self._sched_summary(card, today),
                            "new": self._sched_after_text(op, op_params, card, index, position),
                        }
                    )
            if op == "set_due_date" and new_count:
                warnings.append(
                    f"{new_count} new card(s) become review cards (revert "
                    "restores their exact new-card state)"
                )
            if op == "reposition_new_cards" and new_count < len(live):
                warnings.append(
                    f"{len(live) - new_count} card(s) are not new and are "
                    "unaffected by repositioning"
                )
        if op == "set_due_date" and str(op_params.get("days", "")).endswith("!"):
            warnings.append(
                "the '!' form also overwrites each card's interval"
            )
        if op == "forget_cards":
            warnings.append(
                "clears interval, ease and FSRS memory state; the review log "
                "is preserved, and revert restores the captured state"
            )
        if op_params.get("shift_existing"):
            warnings.append(
                "shift_existing renumbers OTHER new cards too; revert "
                "restores only the selected cards"
            )
        proposal = Proposal(
            id=self._next_id(),
            kind="bulk",
            op=op,
            op_args={**selection, **op_params},
            note_type="",
            deck="",
            tags=[],
            fields={},
            rationale=str(args.get("rationale", "")),
            count=len(live),
            samples=samples,
            warnings=warnings,
        )
        return self._finish_submission(proposal, len(live))

    @staticmethod
    def _sched_after_text(
        op: str, params: dict[str, Any], card: Any, index: int, start: int
    ) -> str:
        if op == "set_due_date":
            days = str(params["days"]).rstrip("!")
            base = f"review · due in {days}d"
            if str(params["days"]).endswith("!"):
                base += f" · ivl := {days}d"
            return base
        if op == "forget_cards":
            return "new" + (
                " · original position" if params.get("restore_position") else ""
            )
        if int(card.type) != 0:
            return "(not a new card - unaffected)"
        if params.get("randomize"):
            return "new · random position"
        return f"new · position {start + index * int(params.get('step_size', 1))}"

    def submit_delete_notes(self, args: dict[str, Any]) -> dict[str, Any]:
        col = self._col()
        note_ids = [int(n) for n in (args.get("note_ids") or [])]
        if not note_ids:
            raise ProposalError("delete_notes needs note_ids")
        samples: list[dict[str, Any]] = []
        missing = 0
        for nid in note_ids:
            try:
                note = col.get_note(nid)
            except Exception:
                missing += 1
                continue
            if len(samples) < MAX_SAMPLES:
                samples.append(
                    {"text": _short_label(next(iter(dict(note.items()).values()), ""))}
                )
        if missing == len(note_ids):
            raise ProposalError("none of those notes exist")
        proposal = Proposal(
            id=self._next_id(),
            kind="delete",
            note_type="",
            deck="",
            tags=[],
            fields={},
            rationale=str(args.get("rationale", "")),
            note_id=None,
            count=len(note_ids) - missing,
            samples=samples,
            op_args={"note_ids": note_ids},
            warnings=[
                "Deleting notes cannot be undone from the chat ledger. A backup "
                "checkpoint is created first (File > Switch Profile restores it)."
            ],
        )
        # Deletes are ALWAYS user-confirmed, even under trusted-writes.
        self._proposals[proposal.id] = proposal
        self._push({"type": "proposal", "proposal": proposal.to_payload()})
        return {
            "status": "pending_user_review",
            "proposal_id": proposal.id,
            "affected": proposal.count,
            "note": "Deletion always requires explicit user confirmation.",
        }

    # ---- deck operations (create/rename/options + filtered decks) ----

    @staticmethod
    def _empty_query_error(col: Any, query: str, fallback: str) -> str:
        """`fallback`, plus WHY the query is empty when a term names nothing.

        A bulk write over zero rows is a dead end either way; the difference is
        whether the assistant learns the deck name was wrong or concludes the
        collection has nothing to change (search_terms.py)."""
        from .search_terms import diagnose_collection

        detail = diagnose_collection(col, query)
        return f"{fallback}. {detail}" if detail else fallback

    def _deck_by_name(self, col: Any, name: str) -> tuple[int, dict[str, Any]]:
        did = self._find_deck_id(col, name)
        deck = col.decks.get(did) if did is not None else None
        if did is None or deck is None:
            raise ProposalError(f"deck {name!r} not found")
        return int(did), deck

    @staticmethod
    def _deck_names(col: Any) -> list[str]:
        return [d.name for d in col.decks.all_names_and_ids()]

    @staticmethod
    def _decks_sharing_config(col: Any, conf_id: Any) -> int:
        try:
            return sum(1 for d in col.decks.all() if d.get("conf") == conf_id)
        except Exception:
            return 1

    @staticmethod
    def _resolve_option(conf: dict[str, Any], path: str) -> tuple[dict[str, Any], str]:
        """Resolve a dot path like 'new.perDay' inside a deck-options dict.
        Only existing keys are addressable, so a typo cannot plant a garbage
        key that Anki silently ignores."""
        node: Any = conf
        parts = path.split(".")
        for part in parts[:-1]:
            node = node.get(part) if isinstance(node, dict) else None
            if node is None:
                break
        leaf = parts[-1]
        if not isinstance(node, dict) or leaf not in node:
            raise ProposalError(
                f"unknown option {path!r}; check get_deck_info for the "
                "preset's actual keys (dot paths like 'new.perDay')"
            )
        return node, leaf

    @staticmethod
    def _check_option_type(path: str, old: Any, new: Any) -> None:
        ok = (
            (isinstance(old, bool) and isinstance(new, bool))
            or (
                isinstance(old, (int, float))
                and not isinstance(old, bool)
                and isinstance(new, (int, float))
                and not isinstance(new, bool)
            )
            or (isinstance(old, str) and isinstance(new, str))
            or (isinstance(old, list) and isinstance(new, list))
        )
        if not ok:
            raise ProposalError(
                f"option {path!r} holds {type(old).__name__} "
                f"({old!r}); got {type(new).__name__} ({new!r})"
            )

    def _parse_filtered_terms(
        self, col: Any, raw: Any
    ) -> tuple[list[list[Any]], list[str]]:
        """Validate filtered-deck search terms; returns (terms, sample lines)."""
        if not isinstance(raw, list) or not 1 <= len(raw) <= 2:
            raise ProposalError("terms must be a list of 1 or 2 search terms")
        terms: list[list[Any]] = []
        lines: list[str] = []
        for t in raw:
            if not isinstance(t, dict):
                raise ProposalError("each term is an object {search, limit, order}")
            search = str(t.get("search", "")).strip()
            if not search:
                raise ProposalError("each term needs a non-empty search")
            limit = int(t.get("limit", 100))
            if limit < 1:
                raise ProposalError("term limit must be at least 1")
            order = int(t.get("order", 6))
            if order not in FILTERED_ORDER_NAMES:
                raise ProposalError(
                    f"order must be 0-8: {FILTERED_ORDER_NAMES}"
                )
            try:
                approx = len(list(col.find_cards(search)))
            except Exception as exc:
                raise ProposalError(f"bad search {search!r}: {exc}") from None
            terms.append([search, limit, order])
            lines.append(
                f"Gather: {search} (limit {limit}, {FILTERED_ORDER_NAMES[order]}) "
                f"— ≈{approx} card(s) match now"
            )
        return terms, lines

    def _deck_op_proposal(
        self,
        *,
        op: str,
        op_args: dict[str, Any],
        deck: str,
        rationale: str,
        samples: list[str],
        count: int = 0,
        warnings: list[str] | None = None,
    ) -> dict[str, Any]:
        proposal = Proposal(
            id=self._next_id(),
            kind="deck_op",
            op=op,
            op_args=op_args,
            note_type="",
            deck=deck,
            tags=[],
            fields={},
            rationale=rationale,
            count=count,
            samples=[{"text": line} for line in samples],
            warnings=warnings or [],
        )
        return self._finish_submission(proposal, 1)

    def submit_create_deck(self, args: dict[str, Any]) -> dict[str, Any]:
        col = self._col()
        name = str(args.get("name", "")).strip()
        if not name:
            raise ProposalError("create_deck needs a name")
        if self._find_deck_id(col, name) is not None:
            raise ProposalError(f"deck {name!r} already exists")
        return self._deck_op_proposal(
            op="create_deck",
            op_args={"name": name},
            deck=name,
            rationale=str(args.get("rationale", "")),
            samples=[f'Create deck "{name}"'],
        )

    def submit_rename_deck(self, args: dict[str, Any]) -> dict[str, Any]:
        col = self._col()
        old = str(args.get("deck", "")).strip()
        new = str(args.get("new_name", "")).strip()
        if not old or not new:
            raise ProposalError("rename_deck needs deck and new_name")
        if old == new:
            raise ProposalError("new_name matches the current name")
        self._deck_by_name(col, old)
        if new.startswith(old + "::"):
            raise ProposalError("cannot move a deck under itself")
        if self._find_deck_id(col, new) is not None:
            raise ProposalError(f"deck {new!r} already exists")
        children = [n for n in self._deck_names(col) if n.startswith(old + "::")]
        warnings = (
            [f"{len(children)} subdeck(s) are renamed with it"] if children else []
        )
        return self._deck_op_proposal(
            op="rename_deck",
            op_args={"old": old, "new": new},
            deck=old,
            rationale=str(args.get("rationale", "")),
            samples=[f'Rename deck "{old}" → "{new}"'],
            warnings=warnings,
        )

    def submit_set_deck_options(self, args: dict[str, Any]) -> dict[str, Any]:
        col = self._col()
        deck_name = str(args.get("deck", "")).strip()
        did, deck = self._deck_by_name(col, deck_name)
        if deck.get("dyn"):
            raise ProposalError(
                "filtered decks have no options preset; use update_filtered_deck"
            )
        options = args.get("options")
        if not isinstance(options, dict) or not options:
            raise ProposalError(
                "options must map dot paths (e.g. 'new.perDay') to new values"
            )
        conf = col.decks.config_dict_for_deck_id(did)
        changes: dict[str, Any] = {}
        samples: list[str] = []
        for path, new in options.items():
            path = str(path)
            node, leaf = self._resolve_option(conf, path)
            old = node[leaf]
            self._check_option_type(path, old, new)
            if old == new:
                continue
            changes[path] = new
            samples.append(f"{path}: {old!r} → {new!r}")
        if not changes:
            raise ProposalError(
                "no effective changes: every value matches the current options"
            )
        shared = self._decks_sharing_config(col, conf.get("id"))
        warnings = []
        if shared > 1:
            warnings.append(
                f'options preset "{conf.get("name", "")}" is shared by '
                f"{shared} decks - changes affect all of them. To change "
                "just this deck: manage_options_preset action=clone, then "
                "assign_options_preset."
            )
        return self._deck_op_proposal(
            op="set_deck_options",
            op_args={"deck": deck_name, "options": changes},
            deck=deck_name,
            rationale=str(args.get("rationale", "")),
            samples=samples,
            count=len(changes),
            warnings=warnings,
        )

    @staticmethod
    def _limit_display(raw: Any, today: int) -> str:
        """Human text for a deck-limit value: today-only dicts show expiry."""
        if raw is None:
            return "(none)"
        if isinstance(raw, dict):
            limit = raw.get("limit")
            return (
                f"{limit} (today)"
                if int(raw.get("today", -1)) == today
                else f"{limit} (expired)"
            )
        return str(raw)

    def submit_set_deck_limits(self, args: dict[str, Any]) -> dict[str, Any]:
        """Per-deck limits (#25): today-only overrides + permanent caps.

        The one Study Triage action worth having ("Set Today's New Cards to
        0") plus Custom Study's increase-limits. Today-only values expire at
        the next rollover, so risk is low; -1 clears an override.
        """
        col = self._col()
        deck_name = str(args.get("deck", "")).strip()
        did, deck = self._deck_by_name(col, deck_name)
        if deck.get("dyn"):
            raise ProposalError("filtered decks have no per-deck limits")
        changes: dict[str, int] = {}
        for limit_field in DECK_LIMIT_KEYS:
            if limit_field not in args or args[limit_field] is None:
                continue
            value = int(args[limit_field])
            if value < -1 or value > MAX_DECK_LIMIT:
                raise ProposalError(
                    f"{limit_field} must be 0-{MAX_DECK_LIMIT}, or -1 to clear "
                    f"the override; got {value}"
                )
            changes[limit_field] = value
        if not changes:
            raise ProposalError(
                "nothing to change - pass at least one of "
                + ", ".join(DECK_LIMIT_KEYS)
            )
        include_subdecks = bool(args.get("include_subdecks", False))
        names = [deck_name]
        skipped_filtered = 0
        if include_subdecks:
            for name in self._deck_names(col):
                if not name.startswith(deck_name + "::"):
                    continue
                _did, child = self._deck_by_name(col, name)
                if child.get("dyn"):
                    skipped_filtered += 1
                    continue
                names.append(name)
        today = int(col.sched.today)
        samples: list[str] = []
        for name in names[: MAX_SAMPLES + 1]:
            _d, target = self._deck_by_name(col, name)
            for limit_field, value in changes.items():
                old = self._limit_display(target.get(DECK_LIMIT_KEYS[limit_field]), today)
                new = "(none)" if value == -1 else str(value)
                if limit_field.endswith("_today") and value != -1:
                    new += " (today only)"
                samples.append(f'"{name}" {limit_field}: {old} → {new}')
        if len(names) > MAX_SAMPLES + 1:
            samples.append(f"… and {len(names) - MAX_SAMPLES - 1} more deck(s)")
        warnings: list[str] = []
        if any(f.endswith("_today") for f in changes):
            warnings.append(
                "today-only limits expire on their own at the next day rollover"
            )
        children_exist = any(
            n.startswith(deck_name + "::") for n in self._deck_names(col)
        )
        if children_exist and not include_subdecks:
            raising = any(
                not f.endswith("_today") or v > 0 for f, v in changes.items()
            )
            note = (
                "v3 scheduler: a parent deck's limit caps its whole subtree, "
                "so 0 here silences the subdecks too"
            )
            if raising:
                note += (
                    "; RAISING limits may still be capped by each subdeck's "
                    "own limit (include_subdecks raises those as well)"
                )
            warnings.append(note)
        if skipped_filtered:
            warnings.append(
                f"{skipped_filtered} filtered subdeck(s) have no limits and "
                "were skipped"
            )
        return self._deck_op_proposal(
            op="set_deck_limits",
            op_args={"decks": names, "changes": changes},
            deck=deck_name,
            rationale=str(args.get("rationale", "")),
            samples=samples,
            count=len(names) * len(changes),
            warnings=warnings,
        )

    def submit_create_filtered_deck(self, args: dict[str, Any]) -> dict[str, Any]:
        col = self._col()
        name = str(args.get("name", "")).strip()
        if not name:
            raise ProposalError("create_filtered_deck needs a name")
        if self._find_deck_id(col, name) is not None:
            raise ProposalError(f"deck {name!r} already exists")
        terms, lines = self._parse_filtered_terms(col, args.get("terms"))
        resched = bool(args.get("reschedule", True))
        lines.append(
            "Reviews reschedule cards normally"
            if resched
            else "Reviews do NOT affect normal scheduling"
        )
        return self._deck_op_proposal(
            op="create_filtered_deck",
            op_args={"name": name, "terms": terms, "resched": resched},
            deck=name,
            rationale=str(args.get("rationale", "")),
            samples=[f'Create filtered deck "{name}"'] + lines,
        )

    def submit_update_filtered_deck(self, args: dict[str, Any]) -> dict[str, Any]:
        col = self._col()
        deck_name = str(args.get("deck", "")).strip()
        did, deck = self._deck_by_name(col, deck_name)
        if not deck.get("dyn"):
            raise ProposalError(f"{deck_name!r} is not a filtered deck")
        op_args: dict[str, Any] = {"deck": deck_name}
        lines: list[str] = []
        if args.get("terms") is not None:
            terms, lines = self._parse_filtered_terms(col, args.get("terms"))
            op_args["terms"] = terms
        if "reschedule" in args and args.get("reschedule") is not None:
            resched = bool(args.get("reschedule"))
            op_args["resched"] = resched
            lines.append(
                "Reviews reschedule cards normally"
                if resched
                else "Reviews do NOT affect normal scheduling"
            )
        if "terms" not in op_args and "resched" not in op_args:
            raise ProposalError("nothing to change: pass terms and/or reschedule")
        return self._deck_op_proposal(
            op="update_filtered_deck",
            op_args=op_args,
            deck=deck_name,
            rationale=str(args.get("rationale", "")),
            samples=[f'Reconfigure and rebuild filtered deck "{deck_name}"'] + lines,
        )

    def submit_filtered_deck_action(self, args: dict[str, Any]) -> dict[str, Any]:
        """Rebuild/empty filtered decks - ONE review card for many decks.

        #27's quick win (user hit live 2026-07-23: 50 rebuilds meant 50
        review cards). Selection: `deck` (one name), `decks` (list), or
        `pattern` (glob over FILTERED deck names, e.g. 'Cram::*') - exactly
        one of the three.
        """
        import fnmatch

        col = self._col()
        action = str(args.get("action", "")).strip()
        if action not in ("rebuild", "empty"):
            raise ProposalError("action must be 'rebuild' or 'empty'")
        deck_name = str(args.get("deck", "")).strip()
        deck_list = [str(d).strip() for d in (args.get("decks") or []) if str(d).strip()]
        pattern = str(args.get("pattern", "")).strip()
        selectors = sum(1 for s in (deck_name, deck_list, pattern) if s)
        if selectors != 1:
            raise ProposalError(
                "filtered_deck_action needs exactly one of deck, decks, or pattern"
            )
        if deck_name:
            names = [deck_name]
        elif deck_list:
            names = list(dict.fromkeys(deck_list))
        else:
            names = [
                n
                for n in self._deck_names(col)
                if fnmatch.fnmatchcase(n, pattern)
            ]
            # Pattern selection filters to filtered decks below; a pattern
            # matching nothing filtered is a clean error, not a no-op.
        resolved: list[str] = []
        not_filtered: list[str] = []
        for name in names:
            _did, deck = self._deck_by_name(col, name)
            if deck.get("dyn"):
                resolved.append(name)
            elif not pattern:
                not_filtered.append(name)
            # pattern mode: silently skip normal decks the glob swept up
        if not_filtered:
            raise ProposalError(
                f"not filtered deck(s): {', '.join(repr(n) for n in not_filtered)}"
            )
        if not resolved:
            raise ProposalError(
                f"no filtered decks match {pattern!r}"
                if pattern
                else "no filtered decks selected"
            )
        verb = "Rebuild" if action == "rebuild" else "Empty"
        samples = [
            f'{verb} filtered deck "{name}"' for name in resolved[: MAX_SAMPLES + 1]
        ]
        if len(resolved) > MAX_SAMPLES + 1:
            samples = samples[: MAX_SAMPLES + 1]
            samples.append(f"… and {len(resolved) - MAX_SAMPLES - 1} more")
        return self._deck_op_proposal(
            op="filtered_deck_action",
            op_args={"decks": resolved, "action": action},
            deck=resolved[0] if len(resolved) == 1 else f"{len(resolved)} filtered decks",
            rationale=str(args.get("rationale", "")),
            samples=samples,
            count=len(resolved),
        )

    def submit_skill_update(
        self, args: dict[str, Any], *, old_content: str, observation_ids: list[str]
    ) -> dict[str, Any]:
        """Propose an update to the card-authoring skill from observed edit
        patterns. ALWAYS user-confirmed, in every permission mode: a skill
        change alters all future agent behavior, so the blast radius is high
        and the confirmation is cheap."""
        import difflib

        summary = str(args.get("summary", "")).strip()
        new_content = str(args.get("new_content", ""))
        patterns = [str(p).strip() for p in (args.get("patterns") or []) if str(p).strip()]
        if not summary or not patterns:
            raise ProposalError(
                "summary and patterns are required: the user reads them to "
                "decide whether the update reflects their actual preferences"
            )
        if not new_content.strip():
            raise ProposalError("new_content must be the full revised skill markdown")
        if new_content.strip() == old_content.strip():
            raise ProposalError("new_content is identical to the current skill")
        diff = "".join(
            difflib.unified_diff(
                old_content.splitlines(keepends=True),
                new_content.splitlines(keepends=True),
                fromfile="skill (current)",
                tofile="skill (proposed)",
            )
        )
        proposal = Proposal(
            id=self._next_id(),
            kind="skill_update",
            note_type="",
            deck="",
            tags=[],
            fields={},
            title="Update the card-authoring skill",
            rationale=summary,
            samples=[{"text": p} for p in patterns],
            count=len(observation_ids),
            op_args={
                "new_content": new_content,
                "diff": diff,
                "observation_ids": list(observation_ids),
            },
        )
        self._proposals[proposal.id] = proposal
        self._push({"type": "proposal", "proposal": proposal.to_payload()})
        return {
            "status": "pending_user_review",
            "proposal_id": proposal.id,
            "note": "Skill updates always require explicit user confirmation.",
        }

    def submit_skill_create(self, args: dict[str, Any]) -> dict[str, Any]:
        """Propose CREATING a brand-new skill (workspace task #20) - a
        reusable workflow the agent and user worked out together, e.g.
        "generate TTS audio via the user's API". Nothing here is TTS- or
        task-specific: any workflow can be proposed this way.

        SECURITY (see skills.py's matching note): a skill is standing
        instructions loaded into every future session, and this add-on feeds
        the agent untrusted card content, so a booby-trapped deck could try
        to get a malicious skill planted here. That is why this method only
        ever stages a proposal - ALWAYS user-confirmed, in every permission
        mode, exactly like submit_skill_update - and the actual file write
        happens solely on accept, via the injected apply_skill_create
        callable (never here, never from a direct tool write)."""
        name = str(args.get("name", "")).strip()
        description = str(args.get("description", "")).strip()
        markdown = str(args.get("markdown", ""))
        rationale = str(args.get("rationale", "")).strip()

        if not name or not SKILL_NAME_RE.match(name):
            raise ProposalError(
                "name must be kebab-case (lowercase letters, digits, and "
                "hyphens only, e.g. 'tts-audio-workflow')"
            )
        if len(name) > MAX_SKILL_NAME_CHARS:
            raise ProposalError(f"name is too long (max {MAX_SKILL_NAME_CHARS} chars)")
        if not description:
            raise ProposalError(
                "description is required: it goes in the SKILL.md frontmatter "
                "and is what future sessions read to decide whether to load it"
            )
        if len(description) > MAX_SKILL_DESCRIPTION_CHARS:
            raise ProposalError(
                f"description is too long (max {MAX_SKILL_DESCRIPTION_CHARS} chars)"
            )
        if not markdown.strip():
            raise ProposalError("markdown must not be empty")
        if len(markdown) > MAX_SKILL_MARKDOWN_CHARS:
            raise ProposalError(
                f"markdown is too long (max {MAX_SKILL_MARKDOWN_CHARS} chars); "
                "keep a skill focused - split unrelated workflows into "
                "separate skills"
            )
        if not rationale:
            raise ProposalError(
                "rationale is required: explain why this is worth saving as a "
                "reusable skill, for the user reviewing the proposal card"
            )

        existing = set()
        if self._list_skill_names is not None:
            existing = {n.lower() for n in self._list_skill_names()}
        if name.lower() in existing:
            raise ProposalError(
                f"a skill named {name!r} already exists; pick a different "
                "name, or use propose_skill_update if you mean to revise the "
                "existing card-authoring skill"
            )

        proposal = Proposal(
            id=self._next_id(),
            kind="skill_create",
            note_type="",
            deck="",
            tags=[],
            fields={},
            title=f"Create new skill: {name}",
            rationale=rationale,
            samples=[{"text": description}],
            op_args={"name": name, "description": description, "markdown": markdown},
        )
        self._proposals[proposal.id] = proposal
        self._push({"type": "proposal", "proposal": proposal.to_payload()})
        return {
            "status": "pending_user_review",
            "proposal_id": proposal.id,
            "note": "Creating a new skill always requires explicit user "
            "confirmation: a skill is standing instructions loaded into "
            "every future session.",
        }

    # ---- change sets: many small edits reviewed as one unit ----

    def open_change_set(self, args: dict[str, Any]) -> dict[str, Any]:
        title = str(args.get("title", "")).strip() or "Change set"
        proposal = Proposal(
            id=self._next_id(),
            kind="change_set",
            note_type="",
            deck="",
            tags=[],
            fields={},
            title=title,
            rationale=str(args.get("description", "")),
            open=True,
        )
        self._proposals[proposal.id] = proposal
        self._push({"type": "proposal", "proposal": proposal.to_payload()})
        return {
            "status": "open",
            "change_set_id": proposal.id,
            "note": "Add edits with add_to_change_set, then close_change_set to "
            "hand it to the user for review.",
        }

    def add_to_change_set(self, args: dict[str, Any]) -> dict[str, Any]:
        col = self._col()
        proposal = self._proposals.get(str(args.get("change_set_id", "")))
        if proposal is None or proposal.kind != "change_set" or not proposal.open:
            raise ProposalError("no open change set with that id")
        if args.get("op"):
            return self._add_operation_to_change_set(proposal, args)
        note_id = int(args.get("note_id", 0))
        if not note_id:
            raise ProposalError(
                "each item needs either note_id (a note edit) or op + args "
                "(a batchable operation)"
            )
        changes = {str(k): str(v) for k, v in (args.get("field_changes") or {}).items()}
        add_tags = [str(t).strip() for t in (args.get("add_tags") or []) if str(t).strip()]
        remove_tags = [
            str(t).strip() for t in (args.get("remove_tags") or []) if str(t).strip()
        ]
        try:
            note = col.get_note(note_id)
        except Exception:
            raise ProposalError(f"note {note_id} not found") from None
        current = dict(note.items())
        unknown = [name for name in changes if name not in current]
        if unknown:
            raise ProposalError(f"unknown field(s) {unknown}; valid: {list(current)}")
        changes = {k: v for k, v in changes.items() if v != current[k]}
        if not changes and not add_tags and not remove_tags:
            raise ProposalError("no effective changes for this note")
        # One entry per note: a second add for the same note merges into it.
        entry = next((i for i in proposal.items if i["note_id"] == note_id), None)
        if entry is None:
            entry = {
                "note_id": note_id,
                "field_changes": {},
                "base_fields": {},
                "add_tags": [],
                "remove_tags": [],
                "label": _short_label(next(iter(current.values()), "")),
            }
            proposal.items.append(entry)
        for name, value in changes.items():
            if name not in entry["base_fields"]:
                entry["base_fields"][name] = current[name]
            entry["field_changes"][name] = value
        entry["add_tags"] = sorted(set(entry["add_tags"]) | set(add_tags))
        entry["remove_tags"] = sorted(set(entry["remove_tags"]) | set(remove_tags))
        proposal.count = len(proposal.items)
        # Keep the UI's counter fresh without spamming a re-render per note.
        if proposal.count <= 3 or proposal.count % 25 == 0:
            self._push({"type": "proposal", "proposal": proposal.to_payload()})
        return {"status": "added", "notes_in_set": proposal.count}

    def _add_operation_to_change_set(
        self, proposal: Proposal, args: dict[str, Any]
    ) -> dict[str, Any]:
        """Generic-op item (#27): {op, args} instead of note edits."""
        op = str(args.get("op", ""))
        spec = BATCHABLE_OPS.get(op)
        if spec is None:
            raise ProposalError(
                f"op {op!r} is not batchable; batchable ops: "
                + ", ".join(sorted(BATCHABLE_OPS))
            )
        op_args = args.get("args")
        if not isinstance(op_args, dict) or not op_args:
            raise ProposalError(f"{op} needs an `args` object")
        try:
            spec["validate"](op_args)
        except ProposalError as exc:
            raise ProposalError(f"{op}: {exc}") from None
        selector = (
            str(op_args.get("query") or op_args.get("deck") or op_args.get("pattern") or "")
            or f"{len(op_args.get('card_ids') or op_args.get('note_ids') or op_args.get('decks') or [])} selected"
        )
        proposal.items.append(
            {
                "op": op,
                "args": dict(op_args),
                "label": f"{op.replace('_', ' ')} — {selector}"[:80],
                "risk": spec["risk"],
                "revert": spec["revert"],
            }
        )
        proposal.count = len(proposal.items)
        if proposal.count <= 3 or proposal.count % 25 == 0:
            self._push({"type": "proposal", "proposal": proposal.to_payload()})
        return {
            "status": "added",
            "notes_in_set": proposal.count,
            "revertibility": spec["revert"],
        }

    def close_change_set(self, args: dict[str, Any]) -> dict[str, Any]:
        proposal = self._proposals.get(str(args.get("change_set_id", "")))
        if proposal is None or proposal.kind != "change_set" or not proposal.open:
            raise ProposalError("no open change set with that id")
        if not proposal.items:
            proposal.status = REJECTED
            self._push(
                {"type": "proposal_resolved", "id": proposal.id, "status": REJECTED}
            )
            return {"status": "discarded", "note": "change set was empty"}
        proposal.open = False
        summary = str(args.get("summary", "")).strip()
        if summary:
            proposal.rationale = summary
        proposal.samples = []
        for item in proposal.items[:MAX_SAMPLES]:
            if "op" in item:
                proposal.samples.append(
                    {"text": f"{item['label']} · {item['revert']}"}
                )
                continue
            name = next(iter(item["field_changes"]), None)
            if name is None:
                continue
            proposal.samples.append(
                {
                    "label": item["label"],
                    "old": item["base_fields"].get(name, ""),
                    "new": item["field_changes"][name],
                }
            )
        # The batch inherits its HIGHEST risk class (#27), and revertibility
        # variance is declared before accept, never discovered after.
        op_items = [item for item in proposal.items if "op" in item]
        if op_items:
            risks = {item["risk"] for item in op_items}
            highest = max(risks, key=_RISK_ORDER.index)
            proposal.warnings.append(
                f"this batch includes {highest} operations"
                + (f" (and {len(risks) - 1} lighter class(es))" if len(risks) > 1 else "")
            )
            irreversible = [i for i in op_items if i["revert"].startswith("NOT revertible")]
            if irreversible:
                proposal.warnings.append(
                    f"{len(irreversible)} operation(s) can NOT be reverted from "
                    "the ledger: " + "; ".join(i["label"] for i in irreversible[:3])
                )
        if self._trusted_enabled() and self._budget_take(len(proposal.items)):
            self._push({"type": "proposal", "proposal": proposal.to_payload()})
            self.accept({"id": proposal.id, "_direct": True})
            return {
                "status": "applied",
                "notes_in_set": proposal.count,
                "warnings": proposal.warnings,
            }
        self._push({"type": "proposal", "proposal": proposal.to_payload()})
        return {
            "status": "pending_user_review",
            "change_set_id": proposal.id,
            "notes_in_set": proposal.count,
        }

    # ---- the write chokepoint (SAFETY.md Part 2, rule 1) ----

    @staticmethod
    def _run_in_transaction(col: Any, op: Callable[[], None]) -> None:
        """Run ``op`` inside the collection's write transaction so any raise
        rolls the whole thing back (SAFETY.md rule 1).

        SAFETY: SAFETY.md's pseudocode ``with col.transact():`` is aspirational;
        pylib exposes no ``Collection.transact``. The real primitive is
        ``col.db.transact(op)`` (anki/dbproxy.py:34) which does
        ``db_begin(); op(); db_commit()`` and ``db_rollback()`` on any
        exception. We call that. If a collection double has no ``db.transact``
        we degrade to a plain call (no atomicity), which only happens in
        environments that cannot roll back anyway.
        """
        db = getattr(col, "db", None)
        transact = getattr(db, "transact", None)
        if callable(transact):
            transact(op)
        else:  # pragma: no cover - only when no transactional backend exists
            op()

    def _apply_write(
        self,
        *,
        execute: Callable[[Any, invariants.Snapshot], _WriteResult],
        precheck: Callable[[Any], None] | None = None,
        backup_reason: str | None = None,
        critical_backup: bool = False,
        lenient_cards: bool = False,
    ) -> Any:
        """The single guarded apply path every note/card mutation flows through
        (SAFETY.md rule 1): backup (if risky) -> transaction -> snapshot ->
        PRECONDITIONS -> execute (backend API only) -> POSTCONDITIONS.

        A precondition failure, a postcondition (invariant) violation, or any
        backend exception rolls the transaction back and surfaces as a clean
        ``ProposalError``; the ledger is truncated to its pre-write length so a
        rolled-back write never leaves a phantom ledger entry. ``lenient_cards``
        relaxes only the card-count delta to whatever actually happened (field
        edits may legitimately activate/deactivate conditional cards, AGENTS.md;
        the drift is still surfaced as a warning by ``_stats_drift_warnings``).

        ``backup_reason`` checkpoints before the transaction opens.
        ``critical_backup=True`` (irreversible ops like delete) makes a FAILED
        checkpoint ABORT the whole write with a ``ProposalError`` before
        anything is touched - a destructive op must never proceed without a
        safety net. A non-critical checkpoint failure does not block the
        write; it is recorded in ``self._checkpoint_warning`` for the caller
        to surface on the resolved proposal.
        """
        col = self._col()
        self._checkpoint_warning = None
        if backup_reason is not None and self._quiet:
            backup_reason = None  # the batch took ONE checkpoint up front (#27)
        if backup_reason is not None and not self._checkpoint(backup_reason, critical_backup):
            if critical_backup:
                raise ProposalError(
                    "backup failed; delete not performed — check disk space / "
                    "permissions"
                )
            self._checkpoint_warning = (
                f"backup checkpoint failed ({backup_reason}); proceeding "
                "without a fresh backup — check disk space / permissions"
            )
        ledger_mark = len(self._ledger)
        box: dict[str, Any] = {}

        def op() -> None:
            before = invariants.snapshot(col, invariants.Scope())
            if precheck is not None:
                precheck(col)
            result = execute(col, before)
            box["result"] = result
            expectation = result.expectation
            if lenient_cards:
                after_cards = int(col.db.scalar("select count() from cards"))
                expectation = replace(
                    expectation, card_delta=after_cards - before.card_count
                )
            invariants.assert_all(col, replace(before, scope=result.scope), expectation)

        try:
            self._run_in_transaction(col, op)
        except ProposalError:
            del self._ledger[ledger_mark:]
            raise
        except invariants.InvariantViolation as exc:
            del self._ledger[ledger_mark:]
            # execute() is the line right before assert_all, so it always ran
            # to completion here: box["result"].undo_steps is the exact count
            # of backend RPCs that already pushed a real (now dangling, see
            # _discard_dangling_undo) undo entry before the postcondition
            # caught the problem and rolled the SQL back.
            result = box.get("result")
            if result is not None:
                self._discard_dangling_undo(col, result.undo_steps)
            raise ProposalError(str(exc)) from None
        except Exception as exc:  # backend error raised mid-execute: how many
            # of a multi-item write's RPCs already ran is not knowable here
            # (F3 mid-batch), so we do not guess at undo cleanup - guessing
            # risks popping a genuinely older, unrelated undo entry instead
            # of one of ours. SAFETY.md documents this narrower residual.
            del self._ledger[ledger_mark:]
            raise ProposalError(str(exc)) from None
        return box["result"].value

    @staticmethod
    def _discard_dangling_undo(col: Any, steps: int) -> None:
        """Consume ``steps`` dangling in-memory undo entries left behind by a
        chokepoint write whose SQL was already reverted by ``col.db.transact``
        (SAFETY.md's "Known wart on the rollback path", fixed here).

        Why this is correct: each of the ``steps`` backend RPCs that ran
        before the postcondition rejected the write already completed its own
        Rust-level ``Collection::transact`` (rslib/src/collection/
        transact.rs) successfully, which pushes one real entry onto the
        in-memory undo queue (``end_undoable_operation``) - a push that is
        NOT part of the SQL transaction ``col.db.transact`` rolls back, so it
        survives as a "dangling" entry describing a change that no longer
        exists in the DB. This is only called after an ``InvariantViolation``
        (see callers), which fires strictly after every one of those RPCs
        already returned - so ``steps`` is a ground-truth count, never a
        guess, and popping exactly that many can never reach into
        genuinely older, unrelated undo history.

        Why popping is safe: ``col.undo()`` pops the front (LIFO - exactly
        the order the dangling entries were pushed in) and replays its
        recorded reverse-change. Every reverse-change our proposals can dangle
        is a no-op against the already-rolled-back state: undoing an add
        issues a plain ``DELETE ... WHERE id = ?`` with no existence check
        (rslib/src/notes/undo.rs ``remove_note_without_grave`` ->
        storage/note/mod.rs ``remove_note``; storage/card/mod.rs
        ``remove_card`` likewise), and undoing an update re-writes the very
        fields the SQL rollback already restored (rslib/src/notes/undo.rs
        ``update_note_undoable``). Behaviorally confirmed on a real
        collection via the gui_smoke probe's forced-rollback check.

        Best-effort: any failure (including a collection double with no
        ``undo_status``/``undo``, as in the unit tests' FakeCol) is swallowed
        so cleanup can never mask the real ``ProposalError`` already being
        raised, and never raise a new one of its own.
        """
        for _ in range(min(steps, UNDO_DISCARD_MAX)):
            try:
                col.undo()
            except Exception:
                return

    def _revert_write(
        self,
        col: Any,
        mutate: Callable[[], None],
        scope: invariants.Scope,
        *,
        steps: Callable[[], int] | int = 1,
    ) -> None:
        """Guarded revert path: run ``mutate`` inside the transaction, convert
        any backend exception to a reported ``ProposalError`` (F1: a filtered
        ``set_deck`` hard-errors with ``CanNotMoveCardsInto`` and must never
        escape as an unhandled pycmd exception), and run the corruption
        postconditions over the touched rows so a revert that would recreate the
        original corruption is rejected instead of committed.

        ``steps`` is the exact number of backend RPCs ``mutate`` issues when it
        runs to completion (default 1; callers whose ``mutate`` loops over
        several set_deck/update_note calls pass the real count - as a plain
        int when it is known upfront, or a zero-arg callable when ``mutate``
        itself decides how many it actually issued, e.g. skipping missing
        notes, and the count is only known after it runs). Used to discard
        exactly that many dangling undo entries if the corruption
        postconditions below reject the revert after ``mutate`` already fully
        applied it (SAFETY.md's "Known wart", see _discard_dangling_undo)."""

        def op() -> None:
            mutate()
            for check in (
                invariants.no_homeless_filtered_cards(col, scope.deck_ids),
                invariants.no_dangling_odid(col, scope.card_ids),
            ):
                if check is not None:
                    raise invariants.InvariantViolation(check)

        try:
            self._run_in_transaction(col, op)
        except ProposalError:
            raise
        except invariants.InvariantViolation as exc:
            # mutate() ran to completion (these checks run strictly after it),
            # so exactly `steps` real undo entries are now dangling. Resolved
            # lazily (after mutate() ran) so a callable `steps` can reflect
            # how many of its calls actually happened, e.g. change-set revert
            # skipping notes that no longer exist.
            count = steps() if callable(steps) else steps
            self._discard_dangling_undo(col, count)
            raise ProposalError(str(exc)) from None
        except Exception as exc:
            raise ProposalError(str(exc)) from None

    def _precheck_create(self, proposal: Proposal) -> Callable[[Any], None]:
        def precheck(col: Any) -> None:
            result = contract.check_create(col, proposal)
            self._merge_contract_warnings(proposal, result.warnings)
            if not result.ok:
                raise ProposalError("\n".join(result.errors))

        return precheck

    def _precheck_edit(self, proposal: Proposal) -> Callable[[Any], None]:
        def precheck(col: Any) -> None:
            result = contract.check_edit(col, proposal)
            self._merge_contract_warnings(proposal, result.warnings)
            if not result.ok:
                raise ProposalError("\n".join(result.errors))

        return precheck

    @staticmethod
    def _merge_contract_warnings(proposal: Proposal, warnings: list[str]) -> None:
        for warning in warnings:
            if warning not in proposal.warnings:
                proposal.warnings.append(warning)

    @staticmethod
    def _fsrs_memory_count(col: Any, card_ids: list[int]) -> int:
        """How many of ``card_ids`` carry FSRS memory that a move would discard
        (SAFETY.md hazard 8: ``set_deck`` calls ``clear_fsrs_data``). Prefers
        ``card.memory_state``; falls back to the ``s``/``d`` in ``card.data``;
        if neither is inspectable, counts every card, because a move wipes FSRS
        regardless and the warning must not understate the loss."""
        import json as _json

        count = 0
        inspectable = False
        for cid in card_ids:
            try:
                card = col.get_card(cid)
            except Exception:
                continue
            state = getattr(card, "memory_state", "unknown")
            if state != "unknown":
                inspectable = True
                if state is not None:
                    count += 1
                continue
            data = getattr(card, "data", "") or ""
            if data:
                try:
                    blob = _json.loads(data)
                except Exception:
                    blob = None
                if isinstance(blob, dict) and ("s" in blob or "d" in blob):
                    inspectable = True
                    count += 1
        return count if inspectable else len(list(card_ids))

    # ---- user-facing decisions (bridge entry points) ----

    def accept(self, msg: dict[str, Any]) -> None:
        proposal = self._proposals.get(str(msg.get("id", "")))
        if proposal is None or proposal.status != PENDING:
            return
        if "revision" in msg:
            try:
                accepted_revision = int(msg.get("revision", 0))
            except (TypeError, ValueError):
                accepted_revision = 0
            if accepted_revision != proposal.revision:
                self._push(
                    {
                        "type": "proposal_error",
                        "id": proposal.id,
                        "message": f"stale proposal revision {msg.get('revision')}; current revision is {proposal.revision}",
                    }
                )
                return
        if proposal.kind == "change_set" and proposal.open:
            self._push(
                {
                    "type": "proposal_error",
                    "id": proposal.id,
                    "message": "the assistant is still adding to this change set",
                }
            )
            return
        direct = bool(msg.get("_direct"))
        # The user may have edited values / narrowed the accepted field set
        # in the proposal card before accepting.
        final_fields = {
            str(k): str(v) for k, v in (msg.get("fields") or proposal.fields).items()
        }
        # Per-item reject (#27): the review card can exclude batch items at
        # accept time; indices refer to the payload's item order.
        excluded = msg.get("excluded_items")
        if (
            proposal.kind == "change_set"
            and isinstance(excluded, list)
            and excluded
        ):
            drop = {int(i) for i in excluded}
            kept = [
                item
                for index, item in enumerate(proposal.items)
                if index not in drop
            ]
            if not kept:
                self._push(
                    {
                        "type": "proposal_error",
                        "id": proposal.id,
                        "message": "every item was excluded - reject the batch "
                        "instead if none of it should apply",
                    }
                )
                return
            removed = len(proposal.items) - len(kept)
            if removed:
                proposal.items = kept
                proposal.count = len(kept)
                proposal.warnings.append(
                    f"{removed} item(s) excluded by you at review"
                )

        touched: list[int] = []
        try:
            # create/edit/bulk/delete/change_set all flow through the write
            # chokepoint (_apply_write: transaction + preconditions + invariant
            # postconditions). SAFETY: deck_op and skill_update do NOT yet — see
            # the comments on those branches for why and what remains.
            if proposal.kind == "create":
                self._accept_create(proposal, msg, final_fields)
                touched = [proposal.note_id] if proposal.note_id else []
            elif proposal.kind == "edit":
                self._accept_edit(proposal, msg, final_fields)
                touched = [proposal.note_id] if proposal.note_id else []
            elif proposal.kind == "bulk":
                touched = self._accept_bulk(proposal)
            elif proposal.kind == "delete":
                touched = self._accept_delete(proposal)
            elif proposal.kind == "change_set":
                touched = self._accept_change_set(proposal)
            elif proposal.kind == "deck_op":
                # SAFETY: not yet unified through _apply_write. Deck ops change
                # no note/card counts (create/rename/options/filtered rebuild),
                # already convert backend errors to ProposalError, resolve decks
                # by name, and refresh the deck browser. What remains to unify:
                # wrap _accept_deck_op in a transaction and add a filtered-deck
                # corruption postcondition for create_filtered_deck/rebuild.
                touched = self._accept_deck_op(proposal)
            elif proposal.kind == "note_type_op":
                touched = self._accept_note_type_op(proposal)
            elif proposal.kind == "skill_update":
                # SAFETY: not a collection write at all - _apply_skill writes the
                # skill markdown file on disk, so the col transaction/invariants
                # do not apply. Always user-confirmed and never ledger-reverted.
                if self._apply_skill is None:
                    raise ProposalError("skill updates are not available")
                proposal.warnings = self._apply_skill(proposal)
                proposal.status = ACCEPTED
            elif proposal.kind == "skill_create":
                # SAFETY: same posture as skill_update - not a collection write,
                # so no transaction/invariants apply. _apply_skill_create writes
                # a brand-new SKILL.md (skills.write_new_skill), re-checking
                # existence at write time so this can never overwrite a name
                # another accepted proposal already claimed. Always
                # user-confirmed and never ledger-reverted.
                if self._apply_skill_create is None:
                    raise ProposalError("creating new skills is not available")
                proposal.warnings = self._apply_skill_create(proposal)
                proposal.status = ACCEPTED
            else:
                raise ProposalError(f"unknown proposal kind {proposal.kind!r}")
            proposal.status = AUTO_ACCEPTED if direct else proposal.status
        except ProposalError as exc:
            self._push(
                {"type": "proposal_error", "id": proposal.id, "message": str(exc)}
            )
            return
        except Exception as exc:  # backend error outside the chokepoint's reach
            self._push(
                {"type": "proposal_error", "id": proposal.id, "message": str(exc)}
            )
            return
        self._push(
            {
                "type": "proposal_resolved",
                "id": proposal.id,
                "status": proposal.status,
                "note_id": proposal.note_id,
                "warnings": proposal.warnings,
                "revertible": self._kind_revertible(proposal),
            }
        )
        self._push_ledger()
        if touched:
            self._after_write(touched)

    def _accept_bulk(self, proposal: Proposal) -> list[int]:
        if proposal.op == "rename_tag":
            old = proposal.op_args["old_tag"]
            new = proposal.op_args["new_tag"]

            def execute_rename(col: Any, before: invariants.Snapshot) -> _WriteResult:
                note_ids = list(col.find_notes(f'tag:"{old}"'))
                col.tags.rename(old, new)
                self._ledger.append(
                    LedgerEntry(
                        id=proposal.id,
                        kind="bulk",
                        note_id=0,
                        label=f"rename tag {old} → {new} ({len(note_ids)} notes)",
                        data={"op": "rename_tag", "old_tag": old, "new_tag": new},
                    )
                )
                return _WriteResult(
                    note_ids,
                    invariants.Expectation(),
                    invariants.Scope(
                        note_ids=tuple(int(n) for n in note_ids),
                        # tags.rename stamps every matching note
                        written_note_ids=tuple(int(n) for n in note_ids),
                    ),
                )

            note_ids = self._apply_write(
                execute=execute_rename, backup_reason=f"bulk {proposal.op}"
            )
            proposal.status = ACCEPTED
            if self._checkpoint_warning:
                proposal.warnings.append(self._checkpoint_warning)
            return note_ids
        if proposal.op == "move_cards":
            query = proposal.op_args["query"]
            deck = proposal.op_args["deck"]

            def execute_move(col: Any, before: invariants.Snapshot) -> _WriteResult:
                card_ids = list(col.find_cards(query))
                if not card_ids:
                    raise ProposalError(f"no cards match {query!r} anymore")
                # F1: capture each card's HOME deck, never a raw filtered `did`.
                # odid != 0 means the card currently sits in a filtered deck and
                # its home is odid; storing the filtered did would make revert
                # call set_deck into a filtered deck and hard-error with
                # CanNotMoveCardsInto (SAFETY.md Part 1).
                prior: dict[int, int] = {}
                for cid in card_ids:
                    c = col.get_card(cid)
                    prior[int(cid)] = int(getattr(c, "odid", 0) or c.did)
                # Single deck-resolution point: rejects a filtered destination.
                deck_id = contract.resolve_writable_deck(col, deck)
                if deck_id is contract.WILL_CREATE:
                    deck_id = col.decks.id(deck)
                col.set_deck(card_ids, deck_id)
                self._ledger.append(
                    LedgerEntry(
                        id=proposal.id,
                        kind="bulk",
                        note_id=0,
                        label=f'move {len(card_ids)} card(s) → "{deck}"',
                        data={"op": "move_cards", "card_decks": prior},
                    )
                )
                note_ids = [int(col.get_card(cid).nid) for cid in card_ids[:50]]
                return _WriteResult(
                    note_ids,
                    invariants.Expectation(),
                    invariants.Scope(
                        deck_ids=(int(deck_id),),
                        card_ids=tuple(int(c) for c in card_ids),
                        # set_deck stamps the cards, not their notes
                        written_card_ids=tuple(int(c) for c in card_ids),
                    ),
                )

            note_ids = self._apply_write(
                execute=execute_move, backup_reason=f"bulk {proposal.op}"
            )
            proposal.status = ACCEPTED
            if self._checkpoint_warning:
                proposal.warnings.append(self._checkpoint_warning)
            return note_ids
        if proposal.op == "find_replace":

            def execute_replace(col: Any, before: invariants.Snapshot) -> _WriteResult:
                applied, skipped = self._apply_items(col, proposal)
                return _WriteResult(
                    (applied, skipped),
                    invariants.Expectation(),
                    invariants.Scope(
                    note_ids=tuple(int(n) for n in applied),
                    # update_note stamps each edited note
                    written_note_ids=tuple(int(n) for n in applied),
                ),
                    undo_steps=len(applied),
                )

            applied, skipped = self._apply_write(
                execute=execute_replace,
                backup_reason=f"bulk {proposal.op}",
                lenient_cards=True,
            )
            proposal.status = ACCEPTED
            if self._checkpoint_warning:
                proposal.warnings.append(self._checkpoint_warning)
            if skipped:
                proposal.warnings.append(
                    f"{len(skipped)} note(s) changed since the preview and were "
                    "skipped: " + ", ".join(skipped[:3])
                )
            return applied
        if proposal.op in CARD_STATE_OPS:
            return self._accept_card_state(proposal)
        if proposal.op in ("add_tags", "remove_tags"):
            return self._accept_bulk_tags(proposal)
        if proposal.op in SCHEDULING_OPS:
            return self._accept_scheduling(proposal)
        if proposal.op == "clear_unused_tags":
            return self._accept_clear_unused(proposal)
        if proposal.op == "store_media_asset":
            return self._accept_store_media(proposal)
        if proposal.op == "import_csv":
            return self._accept_import_csv(proposal)
        raise ProposalError(f"unknown bulk op {proposal.op!r}")

    def _accept_bulk_tags(self, proposal: Proposal) -> list[int]:
        """Apply a bulk tag add/remove.

        Prior state is each note's FULL tag list at apply time - removal may
        use wildcards whose expansion is the backend's business, so recording
        "what to reverse" would mean reimplementing its matching; recording
        the whole list makes revert an exact restore instead (same shape as
        change-set revert: one update_note per surviving note).
        """
        op = proposal.op
        tags_str = " ".join(proposal.op_args["tags"])

        def execute(col: Any, before: invariants.Snapshot) -> _WriteResult:
            if "note_ids" in proposal.op_args:
                candidates = [int(n) for n in proposal.op_args["note_ids"]]
            else:
                candidates = [int(n) for n in col.find_notes(proposal.op_args["query"])]
            prior: dict[int, list[str]] = {}
            for nid in candidates:
                try:
                    prior[nid] = list(col.get_note(nid).tags)
                except Exception:
                    continue
            if not prior:
                raise ProposalError("none of those notes exist anymore")
            note_ids = list(prior)
            if op == "add_tags":
                col.tags.bulk_add(note_ids, tags_str)
            else:
                col.tags.bulk_remove(note_ids, tags_str)
            verb = "add" if op == "add_tags" else "remove"
            self._ledger.append(
                LedgerEntry(
                    id=proposal.id,
                    kind="bulk",
                    note_id=0,
                    label=f"{verb} tags {tags_str} ({len(note_ids)} notes)",
                    data={"op": op, "tags": proposal.op_args["tags"], "prior": prior},
                )
            )
            return _WriteResult(
                note_ids,
                invariants.Expectation(),
                invariants.Scope(
                    note_ids=tuple(note_ids),
                    # bulk_add/bulk_remove stamp the notes they touch
                    written_note_ids=tuple(note_ids),
                ),
            )

        note_ids = self._apply_write(
            execute=execute, backup_reason=f"bulk {proposal.op}"
        )
        proposal.status = ACCEPTED
        if self._checkpoint_warning:
            proposal.warnings.append(self._checkpoint_warning)
        return note_ids

    def _accept_scheduling(self, proposal: Proposal) -> list[int]:
        """Apply a set_due_date / forget / reposition proposal.

        Prior state per card = every field the ops can touch (_SCHED_FIELDS,
        including memory_state and custom_data as live objects - the ledger
        is in-process, never serialized), captured at apply time so revert is
        an exact update_card restore regardless of what the backend did.
        """
        op = proposal.op
        params = proposal.op_args

        def execute(col: Any, before: invariants.Snapshot) -> _WriteResult:
            if "card_ids" in params:
                candidates = [int(c) for c in params["card_ids"]]
            else:
                candidates = [int(c) for c in col.find_cards(params["query"])]
            prior: dict[int, dict[str, Any]] = {}
            cards: list[int] = []
            new_ids: list[int] = []
            for cid in candidates:
                try:
                    card = col.get_card(cid)
                except Exception:
                    continue
                prior[cid] = {name: getattr(card, name) for name in _SCHED_FIELDS}
                cards.append(cid)
                if int(card.type) == 0:
                    new_ids.append(cid)
            if not cards:
                raise ProposalError("none of those cards exist anymore")
            if op == "set_due_date":
                col.sched.set_due_date(cards, str(params["days"]))
                label = f"set due date {params['days']} ({len(cards)} cards)"
                written = cards
            elif op == "forget_cards":
                col.sched.schedule_cards_as_new(
                    cards,
                    restore_position=bool(params.get("restore_position")),
                    reset_counts=bool(params.get("reset_counts")),
                )
                label = f"forget {len(cards)} card(s)"
                written = cards
            else:
                if not new_ids:
                    raise ProposalError(
                        "none of those cards are new; repositioning only "
                        "affects the new-card queue"
                    )
                col.sched.reposition_new_cards(
                    new_ids,
                    starting_from=int(params.get("starting_from", 0)),
                    step_size=int(params.get("step_size", 1)),
                    randomize=bool(params.get("randomize", False)),
                    shift_existing=bool(params.get("shift_existing", False)),
                )
                label = f"reposition {len(new_ids)} new card(s)"
                # Only the new cards are stamped; claiming the rest would
                # fail the written-rows invariant.
                written = new_ids
            self._ledger.append(
                LedgerEntry(
                    id=proposal.id,
                    kind="bulk",
                    note_id=0,
                    label=label,
                    data={"op": op, "prior": prior},
                )
            )
            note_ids = [int(col.get_card(cid).nid) for cid in cards[:50]]
            return _WriteResult(
                note_ids,
                invariants.Expectation(),
                invariants.Scope(
                    card_ids=tuple(cards),
                    written_card_ids=tuple(written),
                ),
            )

        note_ids = self._apply_write(
            execute=execute, backup_reason=f"bulk {proposal.op}"
        )
        proposal.status = ACCEPTED
        if self._checkpoint_warning:
            proposal.warnings.append(self._checkpoint_warning)
        return note_ids

    def _accept_clear_unused(self, proposal: Proposal) -> list[int]:
        def execute(col: Any, before: invariants.Snapshot) -> _WriteResult:
            col.tags.clear_unused_tags()
            self._ledger.append(
                LedgerEntry(
                    id=proposal.id,
                    kind="bulk",
                    note_id=0,
                    label=f"clear {proposal.count} unused tag(s)",
                    data={"op": "clear_unused_tags"},
                    revertible=False,
                )
            )
            return _WriteResult([], invariants.Expectation(), invariants.Scope())

        note_ids = self._apply_write(
            execute=execute, backup_reason=f"bulk {proposal.op}"
        )
        proposal.status = ACCEPTED
        if self._checkpoint_warning:
            proposal.warnings.append(self._checkpoint_warning)
        return note_ids

    def _accept_card_state(self, proposal: Proposal) -> list[int]:
        """Apply a suspend/unsuspend/bury/unbury/flag proposal.

        Prior state is captured per card AT APPLY TIME (queue for the queue
        ops, the 3-bit flag for set_card_flag), because the collection may
        have moved since submit; revert restores exactly what was recorded.
        """
        op = proposal.op
        flag = proposal.op_args.get("flag")

        def execute(col: Any, before: invariants.Snapshot) -> _WriteResult:
            if "card_ids" in proposal.op_args:
                candidates = [int(c) for c in proposal.op_args["card_ids"]]
            else:
                candidates = [int(c) for c in col.find_cards(proposal.op_args["query"])]
            prior: dict[int, int] = {}
            cards: list[int] = []
            for cid in candidates:
                try:
                    card = col.get_card(cid)
                except Exception:
                    continue
                if op == "set_card_flag":
                    prior[cid] = int(getattr(card, "flags", 0)) & 0x7
                else:
                    prior[cid] = int(card.queue)
                cards.append(cid)
            if not cards:
                raise ProposalError("none of those cards exist anymore")
            if op == "suspend_cards":
                col.sched.suspend_cards(cards)
            elif op == "unsuspend_cards":
                col.sched.unsuspend_cards(cards)
            elif op == "bury_cards":
                col.sched.bury_cards(cards, manual=True)
            elif op == "unbury_cards":
                col.sched.unbury_cards(cards)
            else:
                col.set_user_flag_for_cards(int(flag or 0), cards)
            label = f"{CARD_STATE_OPS[op].lower()} {len(cards)} card(s)"
            if op == "set_card_flag":
                label = (
                    f"flag {len(cards)} card(s) {FLAG_NAMES[int(flag or 0)]}"
                    if flag
                    else f"clear the flag on {len(cards)} card(s)"
                )
            self._ledger.append(
                LedgerEntry(
                    id=proposal.id,
                    kind="bulk",
                    note_id=0,
                    label=label,
                    data={"op": op, "prior": prior, "flag": flag},
                )
            )
            note_ids = [int(col.get_card(cid).nid) for cid in cards[:50]]
            return _WriteResult(
                note_ids,
                invariants.Expectation(),
                invariants.Scope(
                    card_ids=tuple(cards),
                    # suspend/bury/unbury/flag all stamp the card rows only
                    written_card_ids=tuple(cards),
                ),
            )

        note_ids = self._apply_write(
            execute=execute, backup_reason=f"bulk {proposal.op}"
        )
        proposal.status = ACCEPTED
        if self._checkpoint_warning:
            proposal.warnings.append(self._checkpoint_warning)
        return note_ids

    @staticmethod
    def _kind_revertible(proposal: Proposal) -> bool:
        if proposal.kind in ("delete", "skill_update", "skill_create"):
            return False
        # Rebuild/empty leave nothing to restore: the previous queue content
        # is not stored anywhere, and re-running them is one chat message.
        if proposal.kind == "deck_op" and proposal.op == "filtered_deck_action":
            return False
        # Set at submit time where the kind alone cannot answer it (#7: the
        # same tool is revertible or not depending on its sub-op).
        if proposal.revertible is not None:
            return proposal.revertible
        # Safety-net ops (#8): undo's inverse is Anki's own redo; a database
        # repair and a sync have no meaningful inverse here.
        if proposal.op in ("undo_change", "check_database", "sync_now"):
            return False
        # Deleting a deck deletes its cards (#9a); the backup is the way back.
        if proposal.op == "delete_deck":
            return False
        # Registry-only cleanup: a tag reappears the moment a note uses it.
        if proposal.op == "clear_unused_tags":
            return False
        return True

    @staticmethod
    def _rebuild_filtered(col: Any, did: int) -> int | None:
        """Rebuild a filtered deck; returns the gathered card count when the
        scheduler reports one (int in older APIs, .count on newer OpChanges)."""
        result = col.sched.rebuild_filtered_deck(did)
        if isinstance(result, bool):
            return None
        if isinstance(result, int):
            return result
        return getattr(result, "count", None)

    def _gather_note(self, count: int | None) -> list[str]:
        if count is None:
            return []
        note = f"gathered {count} card(s)"
        if count == 0:
            note += (
                " - nothing matched; suspended cards and cards already in "
                "another filtered deck are never gathered"
            )
        return [note]

    def _accept_deck_op(self, proposal: Proposal) -> list[int]:
        col = self._col()
        a = proposal.op_args
        try:
            if proposal.op == "create_deck":
                name = a["name"]
                if self._find_deck_id(col, name) is not None:
                    raise ProposalError(f"deck {name!r} already exists")
                col.decks.id(name)
                self._ledger.append(
                    LedgerEntry(
                        id=proposal.id,
                        kind="deck_op",
                        note_id=0,
                        label=f'create deck "{name}"',
                        data={"op": "create_deck", "name": name},
                    )
                )
            elif proposal.op == "rename_deck":
                old, new = a["old"], a["new"]
                did, deck = self._deck_by_name(col, old)
                if self._find_deck_id(col, new) is not None:
                    raise ProposalError(f"deck {new!r} already exists")
                deck["name"] = new
                col.decks.save(deck)
                self._ledger.append(
                    LedgerEntry(
                        id=proposal.id,
                        kind="deck_op",
                        note_id=0,
                        label=f'rename deck "{old}" → "{new}"',
                        data={"op": "rename_deck", "old": old, "new": new},
                    )
                )
            elif proposal.op == "set_deck_options":
                did, deck = self._deck_by_name(col, a["deck"])
                if deck.get("dyn"):
                    raise ProposalError("filtered decks have no options preset")
                conf = col.decks.config_dict_for_deck_id(did)
                # Shared presets have collection-wide blast radius: checkpoint.
                # Non-critical (reversible via the ledger below): a failed
                # checkpoint does not block the change, just warns.
                if not self._checkpoint("deck options", False):
                    proposal.warnings.append(
                        "backup checkpoint failed (deck options); proceeding "
                        "without a fresh backup — check disk space / permissions"
                    )
                priors: dict[str, Any] = {}
                for path, new in a["options"].items():
                    node, leaf = self._resolve_option(conf, path)
                    priors[path] = node[leaf]
                    node[leaf] = new
                col.decks.update_config(conf)
                self._ledger.append(
                    LedgerEntry(
                        id=proposal.id,
                        kind="deck_op",
                        note_id=0,
                        label=f'deck options for "{a["deck"]}" '
                        f"({len(priors)} change(s))",
                        data={
                            "op": "set_deck_options",
                            "deck": a["deck"],
                            "priors": priors,
                        },
                    )
                )
            elif proposal.op == "set_deck_limits":
                today = int(col.sched.today)
                limit_priors: dict[str, dict[str, Any]] = {}
                for name in a["decks"]:
                    try:
                        _did, deck = self._deck_by_name(col, name)
                    except ProposalError:
                        continue  # deck renamed/removed since submit
                    if deck.get("dyn"):
                        continue
                    prior: dict[str, Any] = {}
                    for field, value in a["changes"].items():
                        key = DECK_LIMIT_KEYS[field]
                        prior[field] = deck.get(key)
                        if int(value) == -1:
                            deck[key] = None
                        elif field.endswith("_today"):
                            deck[key] = {"limit": int(value), "today": today}
                        else:
                            deck[key] = int(value)
                    col.decks.save(deck)
                    limit_priors[name] = prior
                if not limit_priors:
                    raise ProposalError("none of those decks exist anymore")
                self._ledger.append(
                    LedgerEntry(
                        id=proposal.id,
                        kind="deck_op",
                        note_id=0,
                        label=f'deck limits for "{a["decks"][0]}"'
                        + (f" +{len(limit_priors) - 1} subdecks" if len(limit_priors) > 1 else ""),
                        data={"op": "set_deck_limits", "priors": limit_priors},
                    )
                )
            elif proposal.op == "create_filtered_deck":
                name = a["name"]
                if self._find_deck_id(col, name) is not None:
                    raise ProposalError(f"deck {name!r} already exists")
                did = int(col.decks.new_filtered(name))
                deck = col.decks.get(did)
                deck["terms"] = [list(t) for t in a["terms"]]
                deck["resched"] = bool(a["resched"])
                col.decks.save(deck)
                proposal.warnings = self._gather_note(
                    self._rebuild_filtered(col, did)
                )
                self._ledger.append(
                    LedgerEntry(
                        id=proposal.id,
                        kind="deck_op",
                        note_id=0,
                        label=f'create filtered deck "{name}"',
                        data={"op": "create_filtered_deck", "name": name},
                    )
                )
            elif proposal.op == "update_filtered_deck":
                did, deck = self._deck_by_name(col, a["deck"])
                if not deck.get("dyn"):
                    raise ProposalError(f'{a["deck"]!r} is not a filtered deck')
                priors = {
                    "terms": [list(t) for t in deck.get("terms") or []],
                    "resched": bool(deck.get("resched", True)),
                }
                if a.get("terms") is not None:
                    deck["terms"] = [list(t) for t in a["terms"]]
                if a.get("resched") is not None:
                    deck["resched"] = bool(a["resched"])
                col.decks.save(deck)
                proposal.warnings = self._gather_note(
                    self._rebuild_filtered(col, did)
                )
                self._ledger.append(
                    LedgerEntry(
                        id=proposal.id,
                        kind="deck_op",
                        note_id=0,
                        label=f'reconfigure filtered deck "{a["deck"]}"',
                        data={
                            "op": "update_filtered_deck",
                            "deck": a["deck"],
                            "priors": priors,
                        },
                    )
                )
            elif proposal.op == "saved_search":
                saved = dict(col.get_config("savedFilters", {}) or {})
                name = a["name"]
                prior_query = saved.get(name)
                if a["action"] == "save":
                    saved[name] = a["query"]
                else:
                    if name not in saved:
                        raise ProposalError(f"saved search {name!r} is already gone")
                    del saved[name]
                col.set_config("savedFilters", saved)
                self._ledger.append(
                    LedgerEntry(
                        id=proposal.id,
                        kind="deck_op",
                        note_id=0,
                        label=f'{a["action"]} saved search "{name}"',
                        data={
                            "op": "saved_search",
                            "name": name,
                            "prior": prior_query,
                        },
                    )
                )
            elif proposal.op == "delete_deck":
                did, deck = self._deck_by_name(col, a["deck"])
                destructive = not a.get("filtered") and int(a.get("cards", 0)) > 0
                if destructive and not self._checkpoint("delete deck", True):
                    raise ProposalError(
                        "backup failed; deck not deleted — check disk space / "
                        "permissions"
                    )
                col.decks.remove([did])
                proposal.warnings = list(proposal.warnings) + [
                    (
                        f'removed filtered deck "{a["deck"]}"; its cards are '
                        "back in their home decks"
                        if a.get("filtered")
                        else f'removed deck "{a["deck"]}" and {a.get("cards", 0)} '
                        "card(s)"
                    )
                ]
                # No ledger entry: not revertible (backup is the way back).
            elif proposal.op == "manage_preset":
                action = a["action"]
                configs = {
                    str(c.get("name", "")): c for c in col.decks.all_config()
                }
                if action in ("create", "clone"):
                    if a["name"] in configs:
                        raise ProposalError(f"a preset named {a['name']!r} exists now")
                    clone_from = (
                        configs.get(a.get("clone_from", "")) if action == "clone" else None
                    )
                    if action == "clone" and clone_from is None:
                        raise ProposalError(
                            f"preset {a.get('clone_from')!r} is gone; cannot clone"
                        )
                    conf = col.decks.add_config(a["name"], clone_from=clone_from)
                    self._ledger.append(
                        LedgerEntry(
                            id=proposal.id,
                            kind="deck_op",
                            note_id=0,
                            label=f'{action} preset "{a["name"]}"',
                            data={
                                "op": "manage_preset",
                                "action": action,
                                "conf_id": int(conf["id"]),
                                "name": a["name"],
                            },
                        )
                    )
                elif action == "rename":
                    conf = configs.get(a["preset"])
                    if conf is None:
                        raise ProposalError(f"preset {a['preset']!r} is gone")
                    conf["name"] = a["name"]
                    col.decks.update_config(conf)
                    self._ledger.append(
                        LedgerEntry(
                            id=proposal.id,
                            kind="deck_op",
                            note_id=0,
                            label=f'rename preset "{a["preset"]}" → "{a["name"]}"',
                            data={
                                "op": "manage_preset",
                                "action": "rename",
                                "old": a["preset"],
                                "new": a["name"],
                            },
                        )
                    )
                else:  # delete
                    conf = configs.get(a["preset"])
                    if conf is None:
                        raise ProposalError(f"preset {a['preset']!r} is already gone")
                    conf_id = int(conf["id"])
                    using = [
                        d["name"]
                        for d in col.decks.all()
                        if int(d.get("conf", 1)) == conf_id and not d.get("dyn")
                    ]
                    import copy as _copy

                    snapshot = _copy.deepcopy(conf)
                    col.decks.remove_config(conf_id)
                    self._ledger.append(
                        LedgerEntry(
                            id=proposal.id,
                            kind="deck_op",
                            note_id=0,
                            label=f'delete preset "{a["preset"]}"',
                            data={
                                "op": "manage_preset",
                                "action": "delete",
                                "config": snapshot,
                                "decks": using,
                            },
                        )
                    )
                    proposal.warnings = list(proposal.warnings) + [
                        f"{len(using)} deck(s) now use the Default preset"
                    ]
            elif proposal.op == "assign_preset":
                configs = {
                    str(c.get("name", "")): c for c in col.decks.all_config()
                }
                conf = configs.get(a["preset"])
                if conf is None:
                    raise ProposalError(f"preset {a['preset']!r} is gone")
                assign_priors: dict[str, int] = {}
                for deck_name in a["decks"]:
                    try:
                        _did, deck = self._deck_by_name(col, deck_name)
                    except ProposalError:
                        continue
                    if deck.get("dyn"):
                        continue
                    assign_priors[deck_name] = int(deck.get("conf", 1))
                    col.decks.set_config_id_for_deck_dict(deck, conf["id"])
                if not assign_priors:
                    raise ProposalError("none of those decks exist anymore")
                self._ledger.append(
                    LedgerEntry(
                        id=proposal.id,
                        kind="deck_op",
                        note_id=0,
                        label=f'assign preset "{a["preset"]}" '
                        f"({len(assign_priors)} deck(s))",
                        data={"op": "assign_preset", "priors": assign_priors},
                    )
                )
            elif proposal.op == "set_deck_description":
                did, deck = self._deck_by_name(col, a["deck"])
                prior_desc = str(deck.get("desc", ""))
                deck["desc"] = str(a["description"])
                col.decks.save(deck)
                self._ledger.append(
                    LedgerEntry(
                        id=proposal.id,
                        kind="deck_op",
                        note_id=0,
                        label=f'description for "{a["deck"]}"',
                        data={
                            "op": "set_deck_description",
                            "deck": a["deck"],
                            "prior": prior_desc,
                        },
                    )
                )
            elif proposal.op == "undo_change":
                # Re-inspect (#8): the queue may have moved since review, and
                # firing blind could undo the user's own latest action.
                status = col.undo_status()
                current = str(getattr(status, "undo", "") or "")
                expected = str(a.get("expected", ""))
                if not current:
                    raise ProposalError("there is nothing to undo anymore")
                if current != expected or int(
                    getattr(status, "last_step", 0)
                ) != int(a.get("step", 0)):
                    raise ProposalError(
                        f"the undo queue moved since review: it would now undo "
                        f"{current!r} instead of {expected!r} - ask again for a "
                        "fresh card"
                    )
                col.undo()
                proposal.warnings = [
                    f'undid "{expected}"; Anki\'s redo (Edit menu) can bring '
                    "it back while nothing else changes the queue"
                ]
                # No ledger entry: the inverse IS Anki's redo.
            elif proposal.op == "check_database":
                if not self._checkpoint("check database", False):
                    proposal.warnings.append(
                        "backup checkpoint failed before Check Database — "
                        "check disk space / permissions"
                    )
                report, ok = col.fix_integrity()
                summary = " ".join(str(report).split())
                proposal.warnings = list(proposal.warnings) + [
                    ("integrity OK: " if ok else "PROBLEMS FOUND: ")
                    + (summary[:400] or "(no report)")
                ]
                # No ledger entry: a repair has no meaningful inverse.
            elif proposal.op == "sync_now":
                if self._sync_now is None:
                    raise ProposalError("sync is not available in this session")
                self._sync_now()
                proposal.warnings = [
                    "sync started - Anki's own sync window takes over from here"
                ]
            elif proposal.op == "filtered_deck_action":
                # One card may carry many decks (#27 quick win). Per-deck
                # outcomes surface as warnings so a batch never reads as one
                # opaque success.
                names = [
                    str(n)
                    for n in (
                        a.get("decks") or ([a["deck"]] if a.get("deck") else [])
                    )
                ]
                outcomes: list[str] = []
                for name in names:
                    did, deck = self._deck_by_name(col, name)
                    if not deck.get("dyn"):
                        raise ProposalError(f"{name!r} is not a filtered deck")
                    if a["action"] == "rebuild":
                        note = self._gather_note(self._rebuild_filtered(col, did))
                        if note:
                            outcomes.append(
                                f'"{name}": {note[0]}' if len(names) > 1 else note[0]
                            )
                    else:
                        col.sched.empty_filtered_deck(did)
                proposal.warnings = outcomes
                # No ledger entry: nothing restorable (see _kind_revertible).
            else:
                raise ProposalError(f"unknown deck op {proposal.op!r}")
        except ProposalError:
            raise
        except Exception as exc:
            # Real backend errors (invalid name, backend invariants) surface
            # as a proposal error on the card instead of a stuck UI.
            raise ProposalError(str(exc)) from None
        proposal.status = ACCEPTED
        self._after_deck_change()
        return []

    def _accept_delete(self, proposal: Proposal) -> list[int]:
        note_ids = [int(n) for n in proposal.op_args.get("note_ids", [])]

        def execute(col: Any, before: invariants.Snapshot) -> _WriteResult:
            existing: list[int] = []
            card_total = 0
            for nid in note_ids:
                try:
                    note = col.get_note(nid)
                except Exception:
                    continue
                existing.append(nid)
                card_total += len(list(note.cards()))
            if not existing:
                raise ProposalError("those notes no longer exist")
            col.remove_notes(existing)
            self._ledger.append(
                LedgerEntry(
                    id=proposal.id,
                    kind="delete",
                    note_id=0,
                    label=f"deleted {len(existing)} note(s)",
                    revertible=False,
                )
            )
            return _WriteResult(
                existing,
                invariants.Expectation(
                    note_delta=-len(existing), card_delta=-card_total
                ),
                invariants.Scope(),
            )

        existing = self._apply_write(
            execute=execute, backup_reason="delete notes", critical_backup=True
        )
        proposal.status = ACCEPTED
        return existing

    def _internal_apply_op(self, op: str, op_args: dict[str, Any]) -> Proposal:
        """Run ONE batch item through its op's own submit+accept path (#27),
        quietly: no UI pushes, no per-item checkpoint, no trusted auto-apply.
        Full validation therefore still happens exactly where the single-op
        tools do it. Raises ProposalError on any failure; the caller records
        the per-item outcome either way."""
        submit = getattr(self, _BATCH_SUBMIT[op])
        proposal_id: str | None = None
        self._quiet = True
        try:
            result = submit({**op_args, "op": op})
            proposal_id = str(result.get("proposal_id") or "")
            internal = self._proposals.get(proposal_id)
            if internal is None:
                raise ProposalError(f"internal submission failed: {result}")
            if internal.kind == "deck_op":
                self._accept_deck_op(internal)
            else:
                self._accept_bulk(internal)
            return internal
        finally:
            self._quiet = False
            if proposal_id:
                # Internal proposals never reach the UI's map.
                self._proposals.pop(proposal_id, None)

    def _accept_change_set(self, proposal: Proposal) -> list[int]:
        col = self._col()
        note_items = [i for i in proposal.items if "op" not in i]
        op_items = [i for i in proposal.items if "op" in i]
        if not op_items:
            return self._accept_change_set_notes(proposal)

        # Generic batch (#27): ONE checkpoint up front, then every item
        # applies with an explicit outcome - never a silent half-applied
        # batch. Native undo entries are merged best-effort so the whole
        # batch is one Cmd+Z where the backend allows it.
        if not self._checkpoint(f"change set: {proposal.title}", False):
            proposal.warnings.append(
                "backup checkpoint failed (change set); proceeding without a "
                "fresh backup — check disk space / permissions"
            )
        ledger_mark = len(self._ledger)
        outcomes: list[str] = []
        applied_notes: list[int] = []
        undo_box: dict[str, Any] = {"target": None, "warned": False}

        def merge_undo() -> None:
            try:
                status = col.undo_status()
                step = int(getattr(status, "last_step", 0) or 0)
                if not step:
                    return
                if undo_box["target"] is None:
                    undo_box["target"] = step
                elif step != undo_box["target"]:
                    col.merge_undo_entries(undo_box["target"])
            except Exception:
                if not undo_box["warned"]:
                    undo_box["warned"] = True
                    proposal.warnings.append(
                        "items remain separate steps in Anki's native undo "
                        "(merge unavailable); the ledger still reverts the "
                        "whole batch"
                    )

        if note_items:

            def execute(col: Any, snap: invariants.Snapshot) -> _WriteResult:
                applied, skipped = self._apply_items(col, proposal)
                return _WriteResult(
                    (applied, skipped),
                    invariants.Expectation(),
                    invariants.Scope(
                        note_ids=tuple(int(n) for n in applied),
                        written_note_ids=tuple(int(n) for n in applied),
                    ),
                    undo_steps=len(applied),
                )

            try:
                applied_notes, skipped = self._apply_write(
                    execute=execute, backup_reason=None, lenient_cards=True
                )
                line = f"note edits: {len(applied_notes)} applied"
                if skipped:
                    line += f", {len(skipped)} skipped (changed since planning)"
                outcomes.append(line)
                merge_undo()
            except ProposalError as exc:
                outcomes.append(f"note edits: FAILED — {exc}")

        for item in op_items:
            before_len = len(self._ledger)
            try:
                internal = self._internal_apply_op(item["op"], item["args"])
                line = f"{item['label']}: applied"
                if internal.warnings:
                    line += f" ({'; '.join(internal.warnings[:2])})"
                outcomes.append(line)
                if len(self._ledger) == before_len:
                    # Applied but left no revert data (e.g. filtered rebuild):
                    # a tombstone sub so the batch REVERT names it too, not
                    # only the review card.
                    self._ledger.append(
                        LedgerEntry(
                            id=proposal.id,
                            kind="batch_item",
                            note_id=0,
                            label=item["label"],
                            data={},
                            revertible=False,
                        )
                    )
                merge_undo()
            except ProposalError as exc:
                outcomes.append(f"{item['label']}: FAILED — {exc}")

        # One ledger row for the batch; per-item revert data rides inside,
        # each sub keeping its own revertibility (declared up front at close).
        subs = list(self._ledger[ledger_mark:])
        del self._ledger[ledger_mark:]
        self._ledger.append(
            LedgerEntry(
                id=proposal.id,
                kind="batch",
                note_id=0,
                label=(proposal.title or "batch") + f" ({len(proposal.items)} items)",
                data={"sub": subs},
                revertible=any(s.revertible for s in subs),
            )
        )
        failed = sum(1 for line in outcomes if "FAILED" in line)
        if failed:
            proposal.warnings.append(
                f"{failed} of {len(outcomes)} item(s) FAILED — outcomes below"
            )
        proposal.warnings.extend(outcomes[:12])
        if len(outcomes) > 12:
            proposal.warnings.append(f"… and {len(outcomes) - 12} more outcome(s)")
        proposal.status = ACCEPTED
        return applied_notes

    def _accept_change_set_notes(self, proposal: Proposal) -> list[int]:
        col = self._col()
        before = self._counts(col)

        def execute(col: Any, snap: invariants.Snapshot) -> _WriteResult:
            applied, skipped = self._apply_items(col, proposal)
            return _WriteResult(
                (applied, skipped),
                invariants.Expectation(),
                invariants.Scope(
                    note_ids=tuple(int(n) for n in applied),
                    # update_note stamps each edited note
                    written_note_ids=tuple(int(n) for n in applied),
                ),
                undo_steps=len(applied),
            )

        applied, skipped = self._apply_write(
            execute=execute,
            backup_reason=f"change set: {proposal.title}",
            lenient_cards=True,
        )
        after = self._counts(col)
        proposal.status = ACCEPTED
        warnings = list(proposal.warnings)
        if self._checkpoint_warning:
            warnings.append(self._checkpoint_warning)
        warnings.extend(self._stats_drift_warnings(before, after))
        if skipped:
            warnings.append(
                f"{len(skipped)} note(s) changed since they were added and were "
                "skipped: " + ", ".join(skipped[:3])
            )
        proposal.warnings = warnings
        return applied

    def _apply_items(self, col: Any, proposal: Proposal) -> tuple[list[int], list[str]]:
        """Apply a proposal's per-note items; returns (applied ids, skipped labels).

        Staleness (the pushedHash pattern from the workspace's AnkiConnect
        scripts): each item carries the field values seen when it was
        planned; a note that changed in the meantime is skipped and
        reported, never overwritten blind.
        """
        applied: list[int] = []
        skipped: list[str] = []
        priors: list[dict[str, Any]] = []
        for item in proposal.items:
            if "op" in item:
                continue  # generic-op items apply through their own path (#27)
            try:
                note = col.get_note(item["note_id"])
            except Exception:
                skipped.append(item.get("label", str(item["note_id"])))
                continue
            current = dict(note.items())
            if any(
                current.get(name, "") != base
                for name, base in item["base_fields"].items()
            ):
                skipped.append(item.get("label", str(item["note_id"])))
                continue
            prior_entry = {
                "note_id": item["note_id"],
                "base_fields": {
                    name: current[name] for name in item["field_changes"]
                },
                "prior_tags": list(note.tags),
            }
            priors.append(prior_entry)
            for name, value in item["field_changes"].items():
                note[name] = value
            for tag in item.get("add_tags", []):
                if tag not in note.tags:
                    note.tags.append(tag)
            note.tags = [t for t in note.tags if t not in item.get("remove_tags", [])]
            self._tag_edit(note)
            col.update_note(note)
            # Same reason as the single-note edit: revert needs to know what we
            # wrote, or it cannot tell an untouched note from an edited one.
            stored = col.get_note(item["note_id"])
            prior_entry["written_fields"] = {
                name: stored[name] for name in item["field_changes"]
            }
            prior_entry["written_tags"] = list(stored.tags)
            applied.append(item["note_id"])
        if not applied:
            raise ProposalError(
                "nothing could be applied: every note changed since planning"
            )
        self._ledger.append(
            LedgerEntry(
                id=proposal.id,
                kind="change_set",
                note_id=0,
                label=(proposal.title or proposal.op or "bulk edit")
                + f" ({len(applied)} notes)",
                data={"items": priors},
            )
        )
        self._observe({"event": "applied", "note_ids": list(applied)})
        return applied, skipped

    def _accept_create(
        self, proposal: Proposal, msg: dict[str, Any], final_fields: dict[str, str]
    ) -> None:
        col = self._col()
        orig_fields = dict(proposal.fields)
        orig_tags = list(proposal.tags)
        orig_deck = proposal.deck
        proposal.deck = str(msg.get("deck") or proposal.deck)
        # `None` = the card sent no opinion; `[]` = the user cleared every tag
        # in the editor, which must strip them rather than silently restore the
        # assistant's choice.
        tags_override = msg.get("tags")
        chosen_tags = proposal.tags if tags_override is None else tags_override
        proposal.tags = [str(t).strip() for t in chosen_tags if str(t).strip()]
        model = self._validate_note_type_and_fields(col, proposal.note_type, final_fields)
        field_names = [f["name"] for f in model["flds"]]
        if not final_fields.get(field_names[0], "").strip():
            raise ProposalError(f"first field {field_names[0]!r} must not be empty")
        proposal.fields = {name: final_fields.get(name, "") for name in field_names}
        proposal.note_id = self._apply_create(col, model, proposal)
        proposal.status = ACCEPTED
        self._observe(
            {
                "event": "reviewed",
                "proposal_kind": "create",
                "note_type": proposal.note_type,
                "deck_before": orig_deck,
                "deck_after": proposal.deck,
                "tags_before": orig_tags,
                "tags_after": list(proposal.tags),
                "fields_before": orig_fields,
                "fields_after": dict(proposal.fields),
            }
        )

    def _accept_edit(
        self, proposal: Proposal, msg: dict[str, Any], final_fields: dict[str, str]
    ) -> None:
        accepted = msg.get("accepted_fields")
        names = [str(n) for n in accepted] if accepted is not None else list(final_fields)
        apply_fields = {n: final_fields[n] for n in names if n in final_fields}
        if not apply_fields and not proposal.add_tags and not proposal.remove_tags:
            raise ProposalError("nothing selected to apply")
        # Media before the write, exactly like create: the [sound:] markers
        # must already point at their final collection names (#24b). The
        # rewrite has to land on the REVIEWER's values too - final_fields is
        # what an edit writes, not proposal.fields.
        renames = self._import_staged_media(self._col(), proposal)
        if renames:
            from .media_staging import rewrite_media_markers

            apply_fields = rewrite_media_markers(apply_fields, renames)
        assert proposal.note_id is not None
        note_id = int(proposal.note_id)
        contract_precheck = self._precheck_edit(proposal)

        def precheck(col: Any) -> None:
            contract_precheck(col)
            try:
                note = col.get_note(note_id)
            except Exception:
                raise ProposalError("the note no longer exists") from None
            current = dict(note.items())
            stale = [
                name
                for name in apply_fields
                if current.get(name, "") != proposal.base_fields.get(name, "")
            ]
            if stale:
                # Staleness guard: the note changed underneath the proposal
                # (user edit mid-chat, sync). Refresh the baseline and ask for
                # re-review instead of applying blind (DESIGN.md section 5).
                for name in proposal.fields:
                    proposal.base_fields[name] = current.get(name, "")
                proposal.warnings = [
                    "This note changed while the proposal was open (fields: "
                    + ", ".join(stale)
                    + "). Diffs refreshed - please re-review."
                ]
                self._push({"type": "proposal", "proposal": proposal.to_payload()})
                raise ProposalError(
                    "note changed underneath; proposal refreshed for re-review"
                )

        def execute(col: Any, before: invariants.Snapshot) -> _WriteResult:
            note = col.get_note(note_id)
            current = dict(note.items())
            prior_fields = {name: current[name] for name in apply_fields}
            prior_tags = list(note.tags)
            for name, value in apply_fields.items():
                note[name] = value
            for tag in proposal.add_tags:
                if tag not in note.tags:
                    note.tags.append(tag)
            note.tags = [t for t in note.tags if t not in proposal.remove_tags]
            self._tag_edit(note)
            col.update_note(note)
            first = next(iter(prior_fields.values()), "")
            # Re-read so `written_*` is what Anki STORED, not what we handed
            # it: a revert compares against the stored value, and any
            # normalisation would otherwise read as divergence.
            stored = col.get_note(note_id)
            self._ledger.append(
                LedgerEntry(
                    id=proposal.id,
                    kind="edit",
                    note_id=note_id,
                    label=_short_label(next(iter(apply_fields.values()), first)),
                    prior_fields=prior_fields,
                    prior_tags=prior_tags,
                    written_fields={n: stored[n] for n in apply_fields},
                    written_tags=list(stored.tags),
                )
            )
            cards = tuple(int(c.id) for c in note.cards())
            return _WriteResult(
                None,
                invariants.Expectation(),
                invariants.Scope(
                    note_ids=(note_id,),
                    card_ids=cards,
                    # update_note stamps the note, not its existing cards
                    written_note_ids=(note_id,),
                ),
            )

        self._apply_write(execute=execute, precheck=precheck, lenient_cards=True)
        proposal.status = ACCEPTED
        self._observe({"event": "applied", "note_ids": [note_id]})
        self._observe(
            {
                "event": "reviewed",
                "proposal_kind": "edit",
                "note_type": proposal.note_type,
                "fields_before": dict(proposal.fields),
                "fields_after": dict(apply_fields),
                "declined_fields": [n for n in proposal.fields if n not in apply_fields],
                # Per-field reject-with-comment (#24d): the reason the user
                # typed on the skipped row, so the learning record says WHY.
                "declined_field_comments": {
                    str(name): str(text)
                    for name, text in (msg.get("field_comments") or {}).items()
                },
            }
        )

    def reject(self, msg: dict[str, Any]) -> None:
        proposal = self._proposals.get(str(msg.get("id", "")))
        if proposal is None or proposal.status != PENDING:
            return
        proposal.status = REJECTED
        self._push(
            {"type": "proposal_resolved", "id": proposal.id, "status": REJECTED}
        )

    def restore(self, msg: dict[str, Any]) -> None:
        """Reactivate a superseded or rejected proposal back to pending review."""
        proposal = self._proposals.get(str(msg.get("id", "")))
        if proposal is None or proposal.status not in (SUPERSEDED, REJECTED):
            return
        proposal.status = PENDING
        proposal.warnings = []
        self._push({"type": "proposal", "proposal": proposal.to_payload()})

    def readd(self, msg: dict[str, Any]) -> None:
        """Re-apply a proposal that was undone (re-create the note, or
        re-apply the edit), so an accidental undo is one click to reverse."""
        proposal = self._proposals.get(str(msg.get("id", "")))
        if proposal is None or proposal.status != UNDONE:
            return
        if proposal.kind not in ("create", "edit"):
            self._push(
                {
                    "type": "proposal_error",
                    "id": proposal.id,
                    "message": "re-apply this by asking the assistant again",
                }
            )
            return
        col = self._col()
        try:
            if proposal.kind == "create":
                model = self._validate_note_type_and_fields(
                    col, proposal.note_type, proposal.fields
                )
                # Re-create flows through the chokepoint (_apply_create).
                proposal.note_id = self._apply_create(col, model, proposal)
            else:
                # SAFETY: the readd EDIT path is not yet routed through
                # _apply_write; it re-applies an undone edit inline and is
                # already guarded by this method's broad try/except. What
                # remains: fold it into the edit chokepoint like _accept_edit.
                assert proposal.note_id is not None
                note = col.get_note(proposal.note_id)
                current = dict(note.items())
                prior_fields = {
                    name: current[name] for name in proposal.fields if name in current
                }
                prior_tags = list(note.tags)
                for name, value in proposal.fields.items():
                    if name in current:
                        note[name] = value
                for tag in proposal.add_tags:
                    if tag not in note.tags:
                        note.tags.append(tag)
                note.tags = [t for t in note.tags if t not in proposal.remove_tags]
                self._tag_edit(note)
                col.update_note(note)
                stored = col.get_note(proposal.note_id)
                self._ledger.append(
                    LedgerEntry(
                        id=proposal.id,
                        kind="edit",
                        note_id=proposal.note_id,
                        label=_short_label(next(iter(proposal.fields.values()), "")),
                        prior_fields=prior_fields,
                        prior_tags=prior_tags,
                        written_fields={n: stored[n] for n in prior_fields},
                        written_tags=list(stored.tags),
                    )
                )
                self._observe(
                    {"event": "applied", "note_ids": [int(proposal.note_id)]}
                )
        except Exception as exc:  # ProposalError or collection trouble
            self._push(
                {"type": "proposal_error", "id": proposal.id, "message": str(exc)}
            )
            return
        proposal.status = ACCEPTED
        self._push(
            {
                "type": "proposal_resolved",
                "id": proposal.id,
                "status": proposal.status,
                "note_id": proposal.note_id,
            }
        )
        self._push_ledger()
        if proposal.note_id is not None:
            self._after_write([proposal.note_id])

    def render_for_window(
        self, proposal_id: str, fields: dict[str, Any] | None = None
    ) -> dict[str, Any] | None:
        """Previews for the large preview window (#2, route B).

        Same renderers as preview_request, but RETURNED instead of pushed:
        the caller hands them to preview_window.show_preview on the main
        thread. `fields` carries the card's in-progress draft so the big
        window shows what the user is editing, not the stale submission;
        without a draft it falls back to the proposal's own values.
        """
        proposal = self._proposals.get(str(proposal_id))
        if proposal is None:
            return None
        edited = {str(k): str(v) for k, v in (fields or {}).items()}
        try:
            col = self._col()
            if proposal.kind == "create":
                model = col.models.by_name(proposal.note_type)
                if model is None:
                    return None
                return self._render_create_fields(
                    col, model, edited or dict(proposal.fields)
                )
            if proposal.kind == "edit":
                return self._render_edit_live(
                    col, proposal, edited or dict(proposal.fields)
                )
        except Exception:
            return None
        return None  # bulk/deck/skill kinds have no card to render

    def preview_request(self, msg: dict[str, Any]) -> None:
        """Re-render a proposal's card preview from the user's in-progress
        edits, so the live card reflects what they are typing."""
        proposal = self._proposals.get(str(msg.get("id", "")))
        if proposal is None:
            return
        edited = {str(k): str(v) for k, v in (msg.get("fields") or {}).items()}
        try:
            col = self._col()
            if proposal.kind == "create":
                model = col.models.by_name(proposal.note_type)
                previews = (
                    self._render_create_fields(col, model, edited) if model else None
                )
            else:
                previews = self._render_edit_live(col, proposal, edited)
        except Exception:
            return
        self._push(
            {"type": "preview_update", "id": proposal.id, "previews": previews}
        )

    # ---- ledger: revert / undo ----

    def revert(self, msg: dict[str, Any]) -> None:
        entry_id = str(msg.get("id", ""))
        entry = next(
            (e for e in self._ledger if e.id == entry_id and not e.undone), None
        )
        if entry is None:
            return
        try:
            self._revert_entry(entry, force=bool(msg.get("force")))
        except StaleRevert as exc:
            # Onto the CARD, flagged, so it can offer "undo anyway" right where
            # the user clicked. A floating notice would explain the refusal and
            # leave them no way to act on it.
            self._push(
                {
                    "type": "proposal_error",
                    "id": entry.id,
                    "message": str(exc),
                    "conflict": True,
                }
            )
            return
        except ProposalError as exc:
            self._push({"type": "notice", "text": f"Could not revert: {exc}"})
            return
        except Exception as exc:  # backend error the chokepoint did not convert
            self._push({"type": "notice", "text": f"Could not revert: {exc}"})
            return
        proposal = self._proposals.get(entry.id)
        if proposal is not None:
            proposal.status = UNDONE
            self._push(
                {"type": "proposal_resolved", "id": proposal.id, "status": UNDONE}
            )
        self._push_ledger()
        self._after_write([entry.note_id])

    def undo_session(self) -> None:
        errors = 0
        for entry in reversed(self._ledger):
            if entry.undone:
                continue
            try:
                self._revert_entry(entry)
            except ProposalError:
                errors += 1
                continue
            except Exception:  # backend error the chokepoint did not convert
                errors += 1
                continue
            proposal = self._proposals.get(entry.id)
            if proposal is not None:
                proposal.status = UNDONE
                self._push(
                    {"type": "proposal_resolved", "id": proposal.id, "status": UNDONE}
                )
        if errors:
            self._push(
                {
                    "type": "notice",
                    # Session undo never forces: a bulk sweep is exactly where
                    # silently overwriting someone's later edit would do the
                    # most damage. Diverged entries stay applied and revertible
                    # one at a time, where the conflict can be read.
                    "text": f"Session undo finished; {errors} change(s) could not be "
                    "reverted and were left as they are (studied or deleted "
                    "notes, or notes edited since).",
                }
            )
        self._push_ledger()
        self._after_write([e.note_id for e in self._ledger])

    @staticmethod
    def _diverged(
        note: Any, written_fields: dict[str, str], written_tags: list[str] | None
    ) -> list[str]:
        """Which parts of the note no longer hold what the proposal wrote.

        A revert is a COMPENSATION, not a plain write: it may only undo its own
        change. Comparing against the prior value cannot detect interference -
        `current != prior` is equally true whether nobody touched the note or
        somebody rewrote it. Comparing against what we WROTE distinguishes the
        two, so a later edit (yours, another proposal's, or one synced in from
        another device) is seen instead of silently overwritten.
        """
        # dict(note.items()), not `name in note`: a Note defines __getitem__
        # for FIELD NAMES but no __contains__, so `in` falls back to the
        # sequence protocol and probes note[0], note[1], ... - which raises.
        current = dict(note.items())
        names = [
            name
            for name, value in written_fields.items()
            if name in current and current[name] != value
        ]
        if written_tags is not None and sorted(note.tags) != sorted(written_tags):
            names.append("tags")
        return names

    def _guard_stale(
        self,
        note: Any,
        written_fields: dict[str, str],
        written_tags: list[str] | None,
        force: bool,
        label: str = "",
    ) -> None:
        """Refuse a revert that would clobber a change made after ours."""
        if force:
            return
        names = self._diverged(note, written_fields, written_tags)
        if not names:
            return
        where = f" on {label}" if label else ""
        raise StaleRevert(
            f"{', '.join(names)}{where} changed after this was applied. "
            "Undoing now would discard that newer change."
        )

    def _revert_entry(self, entry: LedgerEntry, force: bool = False) -> None:
        col = self._col()
        if not entry.revertible:
            raise ProposalError(
                "this change cannot be reverted from the ledger; restore the "
                "backup checkpoint instead (File > Switch Profile > Open Backup)"
            )
        # A revert is the system writing, not the user editing: resync the
        # learning snapshots of already-tracked notes so the post-revert state
        # is the new baseline (a create-revert drops its snapshot).
        resync: list[int] = [entry.note_id] if entry.note_id else []
        if entry.kind == "change_set":
            resync = [int(i["note_id"]) for i in entry.data.get("items", [])]
        elif entry.kind == "bulk" and entry.data.get("op") in ("add_tags", "remove_tags"):
            # Tag revert rewrites notes via update_note; refresh tracked
            # learning snapshots so the restore is not read as a user edit.
            resync = [int(n) for n in entry.data.get("prior", {})]

        if entry.kind == "batch":
            # Generic batch (#27): revert items in REVERSE apply order, each
            # through its own op-class revert path. Per-item revertibility was
            # declared up front; a non-revertible sub is reported, never a
            # silent skip, and one failure does not strand the rest.
            failures: list[str] = []
            reverted = 0
            for sub in reversed(entry.data.get("sub", [])):
                if sub.undone:
                    continue
                if not sub.revertible:
                    failures.append(f"{sub.label}: not revertible")
                    continue
                try:
                    self._revert_entry(sub, force=force)
                    reverted += 1
                except ProposalError as exc:
                    failures.append(f"{sub.label}: {exc}")
            if failures:
                self._push(
                    {
                        "type": "notice",
                        "text": f"Batch revert: {reverted} item(s) restored; "
                        + "; ".join(failures[:3])
                        + ("" if len(failures) <= 3 else f"; +{len(failures) - 3} more"),
                    }
                )
            if reverted == 0 and failures:
                raise ProposalError(
                    "nothing in this batch could be reverted: " + failures[0]
                )
            entry.undone = True
            return

        if entry.kind == "note_type_op":
            # Restoring the snapshot can move card counts (re-adding a template
            # regenerates its cards), so this is the one revert that must not
            # assert a zero delta.
            self._revert_note_type_op(col, entry)
            entry.undone = True
            self._after_deck_change()
            return

        if entry.kind == "deck_op":
            # SAFETY: deck-op reverts flow through _revert_deck_op, which looks
            # decks up by name, converts backend errors to ProposalError, and
            # refreshes the deck browser. Deck ops move no note/card counts, so
            # they are not yet unified through _revert_write's invariant sandwich.
            self._revert_deck_op(col, entry)
            entry.undone = True
            return

        if entry.kind == "create":
            # Preconditions (already-deleted / studied) BEFORE any mutation.
            try:
                note = col.get_note(entry.note_id)
            except Exception:
                raise ProposalError("note already deleted") from None
            if any(getattr(card, "reps", 0) > 0 for card in note.cards()):
                raise ProposalError("note has been studied; delete it in the Browser")

            def mutate_create() -> None:
                col.remove_notes([entry.note_id])

            self._revert_write(col, mutate_create, invariants.Scope())
        elif entry.kind == "bulk" and entry.data.get("op") == "rename_tag":

            def mutate_rename() -> None:
                # Reverse rename; if the target tag pre-existed the merge cannot
                # be fully separated (warned at proposal time).
                col.tags.rename(entry.data["new_tag"], entry.data["old_tag"])

            self._revert_write(col, mutate_rename, invariants.Scope())
        elif entry.kind == "bulk" and entry.data.get("op") == "move_cards":
            card_decks = entry.data.get("card_decks", {})
            by_deck: dict[int, list[int]] = {}
            for cid, did in card_decks.items():
                by_deck.setdefault(int(did), []).append(int(cid))

            def mutate_move() -> None:
                # F1: card_decks holds each card's HOME (normal) deck, never a
                # filtered did, so set_deck can't hit CanNotMoveCardsInto. Any
                # backend error is still converted to a clean "could not revert".
                for did, cids in by_deck.items():
                    col.set_deck(cids, did)

            self._revert_write(
                col,
                mutate_move,
                invariants.Scope(
                    deck_ids=tuple(by_deck),
                    card_ids=tuple(int(c) for c in card_decks),
                ),
                # One set_deck RPC per distinct prior home deck, not per card.
                steps=len(by_deck),
            )
        elif entry.kind == "bulk" and entry.data.get("op") == "import_csv":
            # Partial by design (declared on the resolved card): CREATED notes
            # come back out; UPDATED notes stay (the backup is their way back).
            created_ids = [int(n) for n in entry.data.get("created", [])]
            removable: list[int] = []
            studied = 0
            for nid in created_ids:
                try:
                    note = col.get_note(nid)
                except Exception:
                    continue  # already gone
                if any(getattr(card, "reps", 0) > 0 for card in note.cards()):
                    studied += 1
                    continue
                removable.append(nid)
            if studied:
                self._push(
                    {
                        "type": "notice",
                        "text": f"Import revert: {studied} imported note(s) have "
                        "been studied and were kept; delete them in the Browser "
                        "if intended.",
                    }
                )

            def mutate_import() -> None:
                if removable:
                    col.remove_notes(removable)

            self._revert_write(
                col,
                mutate_import,
                invariants.Scope(note_ids=tuple(removable)),
                steps=1 if removable else 0,
            )
        elif entry.kind == "bulk" and entry.data.get("op") == "store_media_asset":
            final = str(entry.data.get("final", ""))

            def mutate_media() -> None:
                # Anki's media trash, not deletion: recoverable from the
                # Check Media screen if the revert was itself a mistake.
                col.media.trash_files([final])

            self._revert_write(col, mutate_media, invariants.Scope(), steps=0)
        elif entry.kind == "bulk" and entry.data.get("op") in ("add_tags", "remove_tags"):
            tag_prior = {
                int(n): list(v) for n, v in entry.data.get("prior", {}).items()
            }
            done_box = {"n": 0}

            def mutate_tags() -> None:
                restored = 0
                for nid, old_tags in tag_prior.items():
                    try:
                        note = col.get_note(nid)
                    except Exception:
                        continue  # note deleted since; nothing to restore
                    if list(note.tags) == old_tags:
                        continue
                    note.tags = list(old_tags)
                    col.update_note(note)
                    restored += 1
                done_box["n"] = restored

            self._revert_write(
                col,
                mutate_tags,
                invariants.Scope(note_ids=tuple(tag_prior)),
                steps=lambda: done_box["n"],
            )
        elif entry.kind == "bulk" and entry.data.get("op") in SCHEDULING_OPS:
            sched_prior = {
                int(c): dict(v) for c, v in entry.data.get("prior", {}).items()
            }
            restored_box = {"n": 0}

            def mutate_sched() -> None:
                restored = 0
                for cid, fields in sched_prior.items():
                    try:
                        card = col.get_card(cid)
                    except Exception:
                        continue  # card deleted since; nothing to restore
                    if all(
                        getattr(card, name) == value for name, value in fields.items()
                    ):
                        continue
                    for name, value in fields.items():
                        setattr(card, name, value)
                    col.update_card(card)
                    restored += 1
                restored_box["n"] = restored

            self._revert_write(
                col,
                mutate_sched,
                invariants.Scope(card_ids=tuple(sched_prior)),
                steps=lambda: restored_box["n"],
            )
        elif entry.kind == "bulk" and entry.data.get("op") in CARD_STATE_OPS:
            op = entry.data["op"]
            prior = {int(c): int(v) for c, v in entry.data.get("prior", {}).items()}
            if op == "set_card_flag":
                # Group by prior flag: one backend call per distinct value.
                by_flag: dict[int, list[int]] = {}
                for cid, old_flag in prior.items():
                    by_flag.setdefault(old_flag, []).append(cid)

                def mutate_flags() -> None:
                    for old_flag, cids in by_flag.items():
                        col.set_user_flag_for_cards(old_flag, cids)

                self._revert_write(
                    col,
                    mutate_flags,
                    invariants.Scope(card_ids=tuple(prior)),
                    steps=len(by_flag),
                )
            else:
                # Queue states drift on their own (bury expires at rollover,
                # the user may have unsuspended in the Browser), so the revert
                # reads each card's CURRENT queue and only issues the
                # transitions still needed to reach the recorded prior state.
                calls_box = {"n": 0}

                def mutate_queues() -> None:
                    calls_box["n"] = _restore_card_queues(col, prior)

                self._revert_write(
                    col,
                    mutate_queues,
                    invariants.Scope(card_ids=tuple(prior)),
                    steps=lambda: calls_box["n"],
                )
        elif entry.kind == "change_set":
            missing_box = {"n": 0}

            # Whole-batch precondition: check every item BEFORE writing any of
            # them, so a conflict on note 40 cannot leave notes 1-39 reverted.
            if not force:
                for item in entry.data.get("items", []):
                    try:
                        note = col.get_note(item["note_id"])
                    except Exception:
                        continue  # missing notes are skipped below, not a conflict
                    self._guard_stale(
                        note,
                        item.get("written_fields", {}),
                        item.get("written_tags"),
                        force,
                        label=str(item.get("label") or f"note {item['note_id']}"),
                    )

            def mutate_change_set() -> None:
                missing = 0
                for item in entry.data.get("items", []):
                    try:
                        note = col.get_note(item["note_id"])
                    except Exception:
                        missing += 1
                        continue
                    for name, value in item["base_fields"].items():
                        note[name] = value
                    if item.get("prior_tags") is not None:
                        note.tags = list(item["prior_tags"])
                    col.update_note(note)
                missing_box["n"] = missing

            total_items = len(entry.data.get("items", []))
            self._revert_write(
                col,
                mutate_change_set,
                invariants.Scope(),
                # One update_note RPC per item, minus the ones mutate_change_set
                # skipped because the note no longer exists; only known once
                # mutate_change_set has actually run.
                steps=lambda: total_items - missing_box["n"],
            )
            if missing_box["n"]:
                self._push(
                    {
                        "type": "notice",
                        "text": f"Revert: {missing_box['n']} note(s) no longer exist "
                        "and were skipped.",
                    }
                )
        else:
            try:
                col.get_note(entry.note_id)
            except Exception:
                raise ProposalError("note no longer exists") from None

            self._guard_stale(
                col.get_note(entry.note_id),
                entry.written_fields,
                entry.written_tags,
                force,
            )

            def mutate_edit() -> None:
                note = col.get_note(entry.note_id)
                for name, value in entry.prior_fields.items():
                    note[name] = value
                if entry.prior_tags is not None:
                    note.tags = list(entry.prior_tags)
                col.update_note(note)

            self._revert_write(col, mutate_edit, invariants.Scope())
        entry.undone = True
        if resync:
            self._observe({"event": "resync", "note_ids": resync})

    def _accept_note_type_op(self, proposal: Proposal) -> list[int]:
        """Apply a note-type write (#7) through the shared chokepoint.

        Card counts legitimately move here (a new template generates cards, a
        removed one destroys them, and a removed FIELD can generate them - see
        NOTE_TYPE_OPS), so the write runs with ``lenient_cards`` and reports
        the delta that ACTUALLY happened as an outcome on the resolved card,
        rather than asserting a prediction that Anki is entitled to disagree
        with. Every branch snapshots the whole note type dict first: for the
        revertible ops that snapshot IS the undo.
        """
        col = self._col()
        op = proposal.op
        meta = NOTE_TYPE_OPS[op]
        args = proposal.op_args
        outcomes: list[str] = []

        def apply(mutate: Callable[[Any], None], *, model_name: str = "") -> None:
            prior = (
                copy.deepcopy(col.models.by_name(model_name)) if model_name else None
            )

            def execute(col: Any, before: invariants.Snapshot) -> _WriteResult:
                cards_before = int(col.db.scalar("select count() from cards"))
                notes_before = int(col.db.scalar("select count() from notes"))
                templates_before = (
                    {t["name"]: (t.get("qfmt", ""), t.get("afmt", "")) for t in prior["tmpls"]}
                    if prior
                    else {}
                )
                mutate(col)
                cards_delta = int(col.db.scalar("select count() from cards")) - cards_before
                notes_delta = int(col.db.scalar("select count() from notes")) - notes_before
                if cards_delta:
                    outcomes.append(
                        f"{abs(cards_delta)} card(s) "
                        + ("created" if cards_delta > 0 else "deleted")
                    )
                if notes_delta:
                    outcomes.append(
                        f"{abs(notes_delta)} note(s) "
                        + ("created" if notes_delta > 0 else "deleted")
                    )
                # The silent-template-rewrite check. Anki remaps field
                # references when a field is removed, which is how a
                # conditional front becomes unconditional (probed on 25.x);
                # nothing in Anki's UI tells you it happened.
                if prior:
                    after = col.models.by_name(args.get("new_name") or model_name)
                    if after is not None:
                        for tmpl in after["tmpls"]:
                            was = templates_before.get(tmpl["name"])
                            if was is None:
                                continue
                            if was != (tmpl.get("qfmt", ""), tmpl.get("afmt", "")):
                                outcomes.append(
                                    f'Anki rewrote card template "{tmpl["name"]}" '
                                    "to keep its field references valid"
                                )
                self._ledger.append(
                    LedgerEntry(
                        id=proposal.id,
                        kind="note_type_op",
                        note_id=0,
                        label=f'{meta["label"]}: {proposal.samples[0]["text"]}'
                        if proposal.samples
                        else meta["label"],
                        data={
                            "op": op,
                            "note_type": model_name,
                            "prior": prior,
                            "created": args.get("name") if op == "create_note_type" else None,
                        },
                    )
                )
                return _WriteResult(
                    None,
                    invariants.Expectation(changes_schema=True),
                    invariants.Scope(),
                )

            self._apply_write(
                execute=execute,
                backup_reason=meta["label"] if meta["backup"] else None,
                # An op we cannot undo must never proceed without a backup.
                critical_backup=proposal.revertible is False,
                lenient_cards=True,
            )

        if op == "set_note_type_styling":
            name = args["note_type"]

            def mutate(col: Any) -> None:
                model = self._note_type_by_name(col, name)
                model["css"] = args["css"]
                col.models.update_dict(model)

            apply(mutate, model_name=name)

        elif op == "set_card_template":
            name = args["note_type"]

            def mutate(col: Any) -> None:
                model = self._note_type_by_name(col, name)
                tmpl = next(
                    (t for t in model["tmpls"] if t["name"] == args["template"]), None
                )
                if tmpl is None:
                    raise ProposalError(
                        f'card template {args["template"]!r} no longer exists'
                    )
                tmpl["qfmt"] = args["qfmt"]
                tmpl["afmt"] = args["afmt"]
                col.models.update_dict(model)

            apply(mutate, model_name=name)

        elif op == "manage_note_type_fields":
            name = args["note_type"]

            def mutate(col: Any) -> None:
                model = self._note_type_by_name(col, name)
                sub = args["op"]
                existing = {f["name"]: f for f in model["flds"]}
                if sub == "add":
                    if args["field"] in existing:
                        raise ProposalError(
                            f'a field named {args["field"]!r} already exists'
                        )
                    col.models.add_field(model, col.models.new_field(args["field"]))
                else:
                    target = existing.get(args["field"])
                    if target is None:
                        raise ProposalError(
                            f'field {args["field"]!r} no longer exists'
                        )
                    if sub == "rename":
                        if args["new_name"] in existing:
                            raise ProposalError(
                                f'a field named {args["new_name"]!r} already exists'
                            )
                        col.models.rename_field(model, target, args["new_name"])
                    elif sub == "reposition":
                        col.models.reposition_field(model, target, args["position"])
                    else:
                        col.models.remove_field(model, target)
                col.models.update_dict(model)

            apply(mutate, model_name=name)

        elif op == "manage_card_templates":
            name = args["note_type"]

            def mutate(col: Any) -> None:
                model = self._note_type_by_name(col, name)
                sub = args["op"]
                existing = {t["name"]: t for t in model["tmpls"]}
                if sub == "add":
                    if args["template"] in existing:
                        raise ProposalError(
                            f'a card template named {args["template"]!r} already exists'
                        )
                    tmpl = col.models.new_template(args["template"])
                    tmpl["qfmt"] = args["qfmt"]
                    tmpl["afmt"] = args["afmt"]
                    col.models.add_template(model, tmpl)
                else:
                    target = existing.get(args["template"])
                    if target is None:
                        raise ProposalError(
                            f'card template {args["template"]!r} no longer exists'
                        )
                    if sub == "rename":
                        if args["new_name"] in existing:
                            raise ProposalError(
                                f'a template named {args["new_name"]!r} already exists'
                            )
                        target["name"] = args["new_name"]
                    elif sub == "reposition":
                        col.models.reposition_template(model, target, args["position"])
                    else:
                        col.models.remove_template(model, target)
                col.models.update_dict(model)

            apply(mutate, model_name=name)

        elif op == "create_note_type":

            def mutate(col: Any) -> None:
                if col.models.by_name(args["name"]) is not None:
                    raise ProposalError(
                        f'a note type named {args["name"]!r} already exists'
                    )
                source = self._note_type_by_name(col, args["clone_from"])
                clone = copy.deepcopy(source)
                clone["id"] = 0
                clone["name"] = args["name"]
                col.models.add_dict(clone)

            apply(mutate)

        elif op == "change_note_type":

            def mutate(col: Any) -> None:
                old = self._note_type_by_name(col, args["note_type"])
                new = self._note_type_by_name(col, args["new_note_type"])
                request = self._change_notetype_request(
                    col, old, new, args["field_map"], args["template_map"]
                )
                # Re-resolve: notes may have been converted or deleted while
                # the card sat pending, and handing a stale id to the backend
                # is how a conversion hits the wrong note.
                live = [
                    nid
                    for nid in args["note_ids"]
                    if self._note_uses(col, nid, old["id"])
                ]
                if not live:
                    raise ProposalError(
                        "none of these notes still use that note type"
                    )
                if len(live) != len(args["note_ids"]):
                    outcomes.append(
                        f"{len(args['note_ids']) - len(live)} note(s) had already "
                        "changed and were skipped"
                    )
                request.note_ids.extend(live)
                col.models.change_notetype_of_notes(request)
                outcomes.append(f"{len(live)} note(s) converted")

            apply(mutate)

        elif op == "remove_empty_cards":

            def mutate(col: Any) -> None:
                # Recompute rather than trusting the ids captured at submit:
                # any edit since then may have filled a field and made a
                # previously-empty card real. Only cards that are STILL empty
                # are deleted.
                report = col.get_empty_cards()
                current = {
                    int(cid) for entry in report.notes for cid in entry.card_ids
                }
                proposed = {
                    int(cid) for entry in args["entries"] for cid in entry["card_ids"]
                }
                doomed = sorted(current & proposed)
                stale = len(proposed) - len(doomed)
                if stale:
                    outcomes.append(
                        f"{stale} card(s) are no longer empty and were kept"
                    )
                if not doomed:
                    raise ProposalError(
                        "none of those cards are empty any more - nothing removed"
                    )
                col.remove_cards_and_orphaned_notes(doomed)

            apply(mutate)

        else:  # pragma: no cover - the table and this dispatch move together
            raise ProposalError(f"unknown note-type op {op!r}")

        proposal.status = ACCEPTED
        if outcomes:
            proposal.warnings = list(proposal.warnings) + [
                "applied: " + "; ".join(outcomes)
            ]
        if self._checkpoint_warning:
            proposal.warnings.append(self._checkpoint_warning)
        self._push_ledger()
        self._after_deck_change()
        return []

    @staticmethod
    def _note_uses(col: Any, note_id: int, notetype_id: Any) -> bool:
        try:
            return col.get_note(note_id).note_type()["id"] == notetype_id
        except Exception:
            return False

    @staticmethod
    def _change_notetype_request(
        col: Any,
        old: Any,
        new: Any,
        field_map: dict[str, str],
        template_map: dict[str, str],
    ) -> Any:
        """Turn our name->name maps into Anki's positional request.

        The backend wants ``new_fields[i] = index of the OLD field that feeds
        NEW field i`` (``-1`` = leave it empty), and the same shape for
        templates - probed on 25.x. Names are the contract at our boundary
        precisely because positions are not stable across note types: a
        positional API with a name-shaped mental model is how content lands in
        the wrong field.
        """
        request = col.models.change_notetype_info(
            old_notetype_id=old["id"], new_notetype_id=new["id"]
        ).input
        old_fields = [f["name"] for f in old["flds"]]
        new_fields = [f["name"] for f in new["flds"]]
        old_templates = [t["name"] for t in old["tmpls"]]
        new_templates = [t["name"] for t in new["tmpls"]]
        reverse_fields = {dst: src for src, dst in field_map.items()}
        reverse_templates = {dst: src for src, dst in template_map.items()}
        del request.new_fields[:]
        request.new_fields.extend(
            old_fields.index(reverse_fields[name]) if name in reverse_fields else -1
            for name in new_fields
        )
        del request.new_templates[:]
        request.new_templates.extend(
            old_templates.index(reverse_templates[name])
            if name in reverse_templates
            else -1
            for name in new_templates
        )
        return request

    def _revert_note_type_op(self, col: Any, entry: LedgerEntry) -> None:
        """Undo a note-type write by restoring the snapshot taken at apply.

        Only ever reached for ops _kind_revertible allows: the destructive ones
        (field/template removal, conversion, empty-card deletion) destroy
        payload that lives OUTSIDE the note type dict - note content, cards,
        review history - which no snapshot of the dict could bring back. Those
        are marked non-revertible up front and the backup is the way back.
        """
        op = entry.data.get("op")
        if op == "create_note_type":
            name = entry.data.get("created")
            model = col.models.by_name(name) if name else None
            if model is None:
                raise ProposalError("that note type is already gone")
            if list(col.models.nids(model["id"])):
                raise ProposalError(
                    "notes now use this note type; removing it would delete them "
                    "- do it in Anki if that is really the intent"
                )
            col.models.remove(model["id"])
            return
        prior = entry.data.get("prior")
        if not prior:
            raise ProposalError("no snapshot to restore")
        live = col.models.get(prior["id"])
        if live is None:
            raise ProposalError("that note type no longer exists")
        col.models.update_dict(copy.deepcopy(prior))

    def _revert_deck_op(self, col: Any, entry: LedgerEntry) -> None:
        """Deck-op reverts always look decks up BY NAME, never by stored id:
        legacy DeckManager.get(did) falls back to the Default deck for a
        missing id, and a revert must never touch the wrong deck."""
        op = entry.data.get("op")
        try:
            if op == "create_deck":
                name = entry.data["name"]
                did = self._find_deck_id(col, name)
                if did is None:
                    raise ProposalError("deck already removed")
                if any(
                    n.startswith(name + "::") for n in self._deck_names(col)
                ):
                    raise ProposalError(
                        "deck now has subdecks; remove it in Anki if intended"
                    )
                if list(col.find_cards(f'deck:"{name}"')):
                    raise ProposalError(
                        "deck now contains cards; remove it in Anki if intended"
                    )
                col.decks.remove([did])
            elif op == "rename_deck":
                old, new = entry.data["old"], entry.data["new"]
                did, deck = self._deck_by_name(col, new)
                if self._find_deck_id(col, old) is not None:
                    raise ProposalError(f"a deck named {old!r} exists again")
                deck["name"] = old
                col.decks.save(deck)
            elif op == "set_deck_options":
                did, _deck = self._deck_by_name(col, entry.data["deck"])
                conf = col.decks.config_dict_for_deck_id(did)
                for path, prior in entry.data["priors"].items():
                    node, leaf = self._resolve_option(conf, path)
                    node[leaf] = prior
                col.decks.update_config(conf)
            elif op == "saved_search":
                saved = dict(col.get_config("savedFilters", {}) or {})
                prior = entry.data.get("prior")
                if prior is None:
                    saved.pop(entry.data["name"], None)
                else:
                    saved[entry.data["name"]] = prior
                col.set_config("savedFilters", saved)
            elif op == "manage_preset":
                action = entry.data["action"]
                if action in ("create", "clone"):
                    conf_id = int(entry.data["conf_id"])
                    used_by = [
                        d["name"]
                        for d in col.decks.all()
                        if int(d.get("conf", 1)) == conf_id
                    ]
                    if used_by:
                        raise ProposalError(
                            f"preset {entry.data['name']!r} is now used by "
                            f"{len(used_by)} deck(s); reassign them first"
                        )
                    col.decks.remove_config(conf_id)
                elif action == "rename":
                    configs = {
                        str(c.get("name", "")): c for c in col.decks.all_config()
                    }
                    conf = configs.get(entry.data["new"])
                    if conf is None:
                        raise ProposalError(
                            f"preset {entry.data['new']!r} is gone; nothing to rename"
                        )
                    conf["name"] = entry.data["old"]
                    col.decks.update_config(conf)
                else:  # delete: recreate with the same values, reassign decks
                    configs = {
                        str(c.get("name", "")): c for c in col.decks.all_config()
                    }
                    if entry.data["config"]["name"] in configs:
                        raise ProposalError(
                            f"a preset named {entry.data['config']['name']!r} "
                            "exists again; nothing restored"
                        )
                    conf = col.decks.add_config(
                        entry.data["config"]["name"],
                        clone_from=entry.data["config"],
                    )
                    for deck_name in entry.data.get("decks", []):
                        try:
                            _did, deck = self._deck_by_name(col, deck_name)
                        except ProposalError:
                            continue
                        col.decks.set_config_id_for_deck_dict(deck, conf["id"])
            elif op == "assign_preset":
                for deck_name, prior_conf in entry.data.get("priors", {}).items():
                    try:
                        _did, deck = self._deck_by_name(col, deck_name)
                    except ProposalError:
                        continue
                    deck["conf"] = int(prior_conf)
                    col.decks.save(deck)
            elif op == "set_deck_description":
                _did, deck = self._deck_by_name(col, entry.data["deck"])
                deck["desc"] = str(entry.data.get("prior", ""))
                col.decks.save(deck)
            elif op == "set_deck_limits":
                for name, prior in entry.data["priors"].items():
                    try:
                        _did, deck = self._deck_by_name(col, name)
                    except ProposalError:
                        continue  # deck gone; nothing to restore
                    for field, raw in prior.items():
                        deck[DECK_LIMIT_KEYS[field]] = raw
                    col.decks.save(deck)
            elif op == "create_filtered_deck":
                # Removing a filtered deck returns its cards to their home
                # decks with scheduling intact - the safe inverse of create.
                did, _deck = self._deck_by_name(col, entry.data["name"])
                col.decks.remove([did])
            elif op == "update_filtered_deck":
                did, deck = self._deck_by_name(col, entry.data["deck"])
                priors = entry.data["priors"]
                deck["terms"] = [list(t) for t in priors["terms"]]
                deck["resched"] = bool(priors["resched"])
                col.decks.save(deck)
                self._rebuild_filtered(col, did)
            else:
                raise ProposalError(f"unknown deck op {op!r}")
        except ProposalError:
            raise
        except Exception as exc:
            raise ProposalError(str(exc)) from None
        self._after_deck_change()

    def _push_ledger(self) -> None:
        self._push(
            {
                "type": "ledger",
                "session_id": self.session_id,
                "session_tag": self.session_tag,
                "entries": [e.to_payload() for e in self._ledger],
            }
        )

    # ---- helpers ----

    def _auto_accept_enabled(self) -> bool:
        return str(self._config.get("permission_mode", "default")) == "auto-accept"

    def _trusted_enabled(self) -> bool:
        return str(self._config.get("permission_mode", "default")) == "trusted-writes"

    def _budget_take(self, n: int) -> bool:
        """Consume n note-writes from the trusted-writes budget. When the
        budget runs out, direct writes pause and everything falls back to
        gated proposals (the safety valve for a runaway agent)."""
        budget = int(self._config.get("write_budget", DEFAULT_WRITE_BUDGET))
        if self._written + n > budget:
            if not self._budget_pause_notified:
                self._budget_pause_notified = True
                self._push(
                    {
                        "type": "notice",
                        "text": f"Trusted-writes budget reached ({budget} notes this "
                        "session); further changes need manual review. Start a new "
                        "chat to reset the budget.",
                    }
                )
            return False
        self._written += n
        return True

    def _counts(self, col: Any) -> tuple[int, int] | None:
        """Collection note/card counts for the before/after sanity check
        (ported from the workspace's AnkiConnect stats-comparison scripts)."""
        try:
            return int(col.note_count()), int(col.card_count())
        except Exception:
            return None

    def _stats_drift_warnings(
        self,
        before: tuple[int, int] | None,
        after: tuple[int, int] | None,
        *,
        expect_note_delta: int = 0,
    ) -> list[str]:
        if before is None or after is None:
            return []
        warnings = []
        note_delta = after[0] - before[0]
        card_delta = after[1] - before[1]
        if note_delta != expect_note_delta:
            warnings.append(
                f"note count changed by {note_delta:+d} (expected "
                f"{expect_note_delta:+d}) - please verify in the Browser"
            )
        # Field edits can legitimately activate/deactivate conditional cards;
        # surface it so the user can confirm it was intended (AGENTS.md lesson).
        if expect_note_delta == 0 and card_delta != 0:
            warnings.append(
                f"card count changed by {card_delta:+d} - conditional card "
                "templates may have been activated or deactivated"
            )
        return warnings

    def _validate_note_type_and_fields(
        self, col: Any, note_type: str, fields: dict[str, str]
    ) -> Any:
        if not note_type:
            raise ProposalError("note_type is required (none pinned)")
        model = col.models.by_name(note_type)
        if model is None:
            names = [nt.name for nt in col.models.all_names_and_ids()]
            raise ProposalError(f"note type {note_type!r} not found; available: {names}")
        valid = {f["name"] for f in model["flds"]}
        unknown = [name for name in fields if name not in valid]
        if unknown:
            raise ProposalError(
                f"unknown field(s) {unknown} for {note_type!r}; valid: {sorted(valid)}"
            )
        return model

    @staticmethod
    def _find_deck_id(col: Any, name: str) -> Any:
        try:
            return col.decks.id_for_name(name)
        except Exception:
            return None

    @staticmethod
    def _require_normal_deck(col: Any, name: str, deck_id: Any) -> None:
        """Reject filtered decks as persistent card destinations.

        Cards belong to a normal home deck. Anki only places them in a
        filtered deck through a rebuild, which records that home in ``odid``.
        Adding or moving cards directly into a filtered deck leaves ``odid=0``
        and later fails in review with "No such deck: '0'".
        """
        deck = col.decks.get(deck_id)
        if deck and deck.get("dyn"):
            raise ProposalError(
                f"{name!r} is a filtered deck; create or move cards into a "
                "normal home deck, then rebuild the filtered deck"
            )

    @staticmethod
    def _looks_duplicate(col: Any, model: Any, first_name: str, value: str) -> bool:
        try:
            query = '"{}:{}" note:"{}"'.format(
                first_name, value.replace('"', '\\"'), model["name"]
            )
            return bool(col.find_notes(query))
        except Exception:
            return False

    # ---- staged media (task #21; media_staging.py) ----

    def _stage_media(self, proposal: Proposal, items: list[dict[str, Any]]) -> None:
        """Validate + copy the agent's audio files into the proposal's staging
        dir and attach playable data-URI payload entries to the proposal.
        Raises ProposalError (surfaced as the tool error) on any problem -
        all-or-nothing, nothing half-staged."""
        from .media_staging import MediaError, media_references

        if self._media is None:
            raise ProposalError(
                "media attachments are not available in this session"
            )
        try:
            staged = self._media.stage(
                proposal.id, items, kinds={"audio", "image", "video"}
            )
        except MediaError as exc:
            raise ProposalError(str(exc)) from None
        proposal.media = [item.to_payload() for item in staged]
        # Cross-check [sound:...] markers against what was staged: not errors
        # (the field may reference media already in the collection), but the
        # mismatches the agent most plausibly made by accident are surfaced
        # on the review card.
        referenced = media_references(proposal.fields)
        for item in staged:
            if item.filename not in referenced:
                how = (
                    "<img src=...> reference"
                    if item.kind == "image"
                    else "[sound:...] marker"
                )
                proposal.warnings.append(
                    f"attached {item.kind} {item.filename!r} is not referenced "
                    f"by any field ({how} missing) - it would be imported but "
                    "never shown"
                )

    def _attach_preview_media(self, col: Any, proposal: Proposal) -> None:
        """Resolve [sound:...] markers pointing at media ALREADY in the
        collection into playable data: URIs for the review card's player
        strip. Preview-only and best-effort: skips anything not present, not
        audio, oversized, or already covered by a staged attachment (which has
        its own strip entry). Never imported on accept - these files already
        live in collection.media - so they stay out of `proposal.media`."""
        import base64
        import os
        from pathlib import Path

        from .media_staging import (
            AUDIO_MIME_BY_EXT,
            MAX_MEDIA_FILE_BYTES,
            _FILENAME_RE,
            sound_markers,
        )

        proposal.preview_media = []
        media = getattr(col, "media", None)
        if media is None:
            return
        try:
            media_dir = Path(media.dir())
        except Exception:
            return
        staged_names = {str(entry.get("name", "")).lower() for entry in proposal.media}
        for name in sorted(sound_markers(proposal.fields)):
            if name.lower() in staged_names:
                continue  # the staged-media strip already plays this one
            if not _FILENAME_RE.match(name):
                continue  # separators/brackets - not a bare media filename
            mime = AUDIO_MIME_BY_EXT.get(os.path.splitext(name)[1].lower())
            if mime is None:
                continue  # non-audio [sound:] (unusual) - no player for it
            path = media_dir / name
            try:
                if not path.is_file():
                    continue
                size = path.stat().st_size
                if size == 0 or size > MAX_MEDIA_FILE_BYTES:
                    continue
                data = base64.b64encode(path.read_bytes()).decode("ascii")
            except OSError:
                continue
            proposal.preview_media.append(
                {
                    "id": f"pv-{hashlib.sha256(name.encode()).hexdigest()[:8]}",
                    "kind": "audio",
                    "name": name,
                    "mime": mime,
                    "bytes": size,
                    "src": f"data:{mime};base64,{data}",
                }
            )

    def _import_staged_media(self, col: Any, proposal: Proposal) -> dict[str, str]:
        """On accept: import staged files through col.media.add_file (Anki's
        own API - it de-duplicates and renames on content collision) and
        rewrite [sound:] markers to the final names. Idempotent: entries that
        already carry final_name (a re-add after undo) are skipped. Returns
        the applied renames so an edit can rewrite the reviewer's own field
        values too (proposal.fields is not what an edit writes - #24b)."""
        from .media_staging import rewrite_media_markers

        if not proposal.media or self._media is None:
            return {}
        renames: dict[str, str] = {}
        for entry in proposal.media:
            if entry.get("final_name"):
                continue  # already imported (re-add path)
            staged_path = self._media.staged_path(proposal.id, entry["name"])
            if not staged_path.is_file():
                proposal.warnings.append(
                    f"staged media {entry['name']!r} is gone (cleaned up?); "
                    "the note was saved without importing it"
                )
                continue
            final = str(col.media.add_file(str(staged_path)))
            entry["final_name"] = final
            if final != entry["name"]:
                renames[entry["name"]] = final
        if renames:
            proposal.fields = rewrite_media_markers(proposal.fields, renames)
            proposal.warnings.append(
                "media renamed on import (name already taken in your media "
                "folder): "
                + ", ".join(f"{old} -> {new}" for old, new in renames.items())
            )
        self._media.discard(proposal.id)
        return renames

    def _apply_create(self, col: Any, model: Any, proposal: Proposal) -> int:
        # Media first: markers must point at their FINAL collection names
        # before the note's fields are written.
        self._import_staged_media(col, proposal)
        precheck = self._precheck_create(proposal)

        def execute(col: Any, before: invariants.Snapshot) -> _WriteResult:
            note = col.new_note(model)
            for name, value in proposal.fields.items():
                note[name] = value
            tags = list(proposal.tags)
            for tag in (self.created_tag, self.session_tag):
                if tag and tag not in tags:
                    tags.append(tag)
            note.tags = tags
            # Single deck-resolution point (rejects a filtered home deck);
            # create it as a normal deck when it does not exist yet.
            deck_id = contract.resolve_writable_deck(col, proposal.deck)
            if deck_id is contract.WILL_CREATE:
                deck_id = col.decks.id(proposal.deck)
            col.add_note(note, deck_id)
            self._ledger.append(
                LedgerEntry(
                    id=proposal.id,
                    kind="create",
                    note_id=note.id,
                    label=_short_label(next(iter(proposal.fields.values()), "")),
                )
            )
            card_ids = tuple(int(c.id) for c in note.cards())
            return _WriteResult(
                int(note.id),
                invariants.Expectation(note_delta=1, card_delta=len(card_ids)),
                invariants.Scope(
                    deck_ids=(int(deck_id),),
                    note_ids=(int(note.id),),
                    card_ids=card_ids,
                    # add_note stamps the note AND its freshly created cards
                    written_note_ids=(int(note.id),),
                    written_card_ids=card_ids,
                ),
            )

        note_id = self._apply_write(execute=execute, precheck=precheck)
        self._observe({"event": "applied", "note_ids": [note_id]})
        return note_id

    # ---- card previews (best-effort; None when rendering unavailable) ----

    def _render_create_preview(
        self, col: Any, model: Any, proposal: Proposal
    ) -> dict[str, Any] | None:
        def build(note: Any) -> Any:
            for name, value in proposal.fields.items():
                note[name] = value
            return note

        after = _render_ephemeral(col, lambda: build(col.new_note(model)), model)
        return {"before": None, "after": after} if after else None

    def _render_create_fields(
        self, col: Any, model: Any, fields: dict[str, str]
    ) -> dict[str, Any] | None:
        valid = {f["name"] for f in model["flds"]}

        def build(note: Any) -> Any:
            for name, value in fields.items():
                if name in valid:
                    note[name] = value
            return note

        after = _render_ephemeral(col, lambda: build(col.new_note(model)), model)
        return {"before": None, "after": after} if after else None

    def _render_edit_live(
        self, col: Any, proposal: Proposal, edited: dict[str, str]
    ) -> dict[str, Any] | None:
        note_id = proposal.note_id
        if note_id is None:
            return None
        model_of = lambda n: n.note_type()  # noqa: E731
        before = _render_ephemeral(
            col, lambda: col.get_note(note_id), model_of, ord_from_note=True
        )

        def mutated() -> Any:
            note = col.get_note(note_id)
            fields = dict(note.items())
            for name, value in edited.items():
                if name in fields:
                    note[name] = value
            return note

        after = _render_ephemeral(col, mutated, model_of, ord_from_note=True)
        if before is None and after is None:
            return None
        return {"before": before, "after": after}

    def _render_edit_preview(
        self, col: Any, note_id: int, changes: dict[str, str]
    ) -> dict[str, Any] | None:
        model_of = lambda n: n.note_type()  # noqa: E731
        before = _render_ephemeral(
            col, lambda: col.get_note(note_id), model_of, ord_from_note=True
        )

        def mutated() -> Any:
            note = col.get_note(note_id)
            for name, value in changes.items():
                note[name] = value
            return note

        after = _render_ephemeral(col, mutated, model_of, ord_from_note=True)
        if before is None and after is None:
            return None
        return {"before": before, "after": after}


def _render_ephemeral(
    col: Any,
    make_note: Callable[[], Any],
    model_or_getter: Any,
    *,
    ord_from_note: bool = False,
) -> dict[str, Any] | None:
    """Render a note through its real card template via Note.ephemeral_card()."""
    try:
        note = make_note()
        model = model_or_getter(note) if callable(model_or_getter) else model_or_getter
        ord_ = 0
        if ord_from_note:
            cards = list(note.cards())
            if cards:
                ord_ = cards[0].ord
        card = note.ephemeral_card(ord_)
        output = card.render_output()
        return {
            "question": output.question_text,
            "answer": output.answer_text,
            "css": model.get("css", ""),
        }
    except Exception:
        return None


def _coerce_field_map(value: Any, key: str) -> dict[str, str]:
    """Field maps arrive as {name: value}, but models routinely stringify
    nested JSON (`"{\\"Front\\": \\"...\\"}"`). Accept that spelling instead of
    dying on `.items()` with an AttributeError the agent can't act on; anything
    genuinely wrong becomes a clear tool error naming the argument."""
    if value is None:
        return {}
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return {}
        try:
            value = json.loads(text)
        except ValueError:
            raise ProposalError(
                f"`{key}` must be an object mapping field name -> full new value; "
                "got a string that is not JSON"
            ) from None
    if not isinstance(value, dict):
        raise ProposalError(
            f"`{key}` must be an object mapping field name -> full new value; "
            f"got {type(value).__name__}"
        )
    return {str(k): str(v) for k, v in value.items()}


def _restore_card_queues(col: Any, prior: dict[int, int]) -> int:
    """Return each card to its recorded queue state; returns backend calls made.

    Compares against the CURRENT queue rather than assuming the post-apply
    state still holds: manual buries expire at the day rollover and the user
    can flip states in the Browser between apply and revert, and a blind
    reverse-operation would then corrupt cards that no longer need it.
    Order matters: leave the wrong hidden state first (unsuspend/unbury),
    re-bury second, suspend last (suspend wins from any state).
    """
    unsuspend_ids: list[int] = []
    unbury_ids: list[int] = []
    bury_manual: list[int] = []
    bury_sched: list[int] = []
    suspend_ids: list[int] = []
    for cid, want in prior.items():
        try:
            cur = int(col.get_card(cid).queue)
        except Exception:
            continue
        if cur == want:
            continue
        if want == -1:
            suspend_ids.append(cid)
            continue
        if cur == -1:
            unsuspend_ids.append(cid)
        elif cur in (-2, -3):
            unbury_ids.append(cid)
        if want == -3:
            bury_manual.append(cid)
        elif want == -2:
            bury_sched.append(cid)
    calls = 0
    if unsuspend_ids:
        col.sched.unsuspend_cards(unsuspend_ids)
        calls += 1
    if unbury_ids:
        col.sched.unbury_cards(unbury_ids)
        calls += 1
    if bury_manual:
        col.sched.bury_cards(bury_manual, manual=True)
        calls += 1
    if bury_sched:
        col.sched.bury_cards(bury_sched, manual=False)
        calls += 1
    if suspend_ids:
        col.sched.suspend_cards(suspend_ids)
        calls += 1
    return calls


def _short_label(text: str, limit: int = 60) -> str:
    import re

    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[: limit - 1] + "…" if len(text) > limit else text
