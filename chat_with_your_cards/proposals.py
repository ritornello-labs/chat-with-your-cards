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

# New-skill proposals (workspace task #20): kebab-case only (the harness
# discovers skills by directory name) and generous-but-bounded size caps -
# large enough for a real workflow write-up, small enough that a runaway or
# adversarial generation can't dump megabytes into agent-home.
SKILL_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
MAX_SKILL_NAME_CHARS = 64
MAX_SKILL_DESCRIPTION_CHARS = 1024
MAX_SKILL_MARKDOWN_CHARS = 50_000

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
    kind: str  # "create" | "edit" | "bulk" | "delete" | "change_set" | "deck_op" | "skill_update" | "skill_create"
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
    # (discarded). create-kind only for now.
    media: list[dict[str, Any]] = field(default_factory=list)
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
            "items": [
                {
                    "note_id": item["note_id"],
                    "label": item.get("label", ""),
                    "fields": sorted(item.get("field_changes", {})),
                }
                for item in self.items
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
    ) -> None:
        self._get_col = get_col
        self._push = push
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
        return f"p{self._counter}"

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
        pins = self.pins
        note_type = str(args.get("note_type", "")).strip()
        deck = str(args.get("deck", "")).strip()
        tags = [str(t).strip() for t in (args.get("tags") or []) if str(t).strip()]
        fields = {str(k): str(v) for k, v in (args.get("fields") or {}).items()}
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
        note_id = int(args.get("note_id", 0))
        changes = {str(k): str(v) for k, v in (args.get("field_changes") or {}).items()}
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
        )
        proposal.previews = self._render_edit_preview(col, note_id, changes)
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
            raise ProposalError("no notes match that search/replacement")
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
            raise ProposalError(f"no cards match {query!r}")
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
                f"{shared} decks - changes affect all of them"
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
        col = self._col()
        deck_name = str(args.get("deck", "")).strip()
        action = str(args.get("action", "")).strip()
        if action not in ("rebuild", "empty"):
            raise ProposalError("action must be 'rebuild' or 'empty'")
        _did, deck = self._deck_by_name(col, deck_name)
        if not deck.get("dyn"):
            raise ProposalError(f"{deck_name!r} is not a filtered deck")
        verb = "Rebuild" if action == "rebuild" else "Empty"
        return self._deck_op_proposal(
            op="filtered_deck_action",
            op_args={"deck": deck_name, "action": action},
            deck=deck_name,
            rationale=str(args.get("rationale", "")),
            samples=[f'{verb} filtered deck "{deck_name}"'],
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
        note_id = int(args.get("note_id", 0))
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
        raise ProposalError(f"unknown bulk op {proposal.op!r}")

    @staticmethod
    def _kind_revertible(proposal: Proposal) -> bool:
        if proposal.kind in ("delete", "skill_update", "skill_create"):
            return False
        # Rebuild/empty leave nothing to restore: the previous queue content
        # is not stored anywhere, and re-running them is one chat message.
        if proposal.kind == "deck_op" and proposal.op == "filtered_deck_action":
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
            elif proposal.op == "filtered_deck_action":
                did, deck = self._deck_by_name(col, a["deck"])
                if not deck.get("dyn"):
                    raise ProposalError(f'{a["deck"]!r} is not a filtered deck')
                if a["action"] == "rebuild":
                    proposal.warnings = self._gather_note(
                        self._rebuild_filtered(col, did)
                    )
                else:
                    col.sched.empty_filtered_deck(did)
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

    def _accept_change_set(self, proposal: Proposal) -> list[int]:
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
            priors.append(
                {
                    "note_id": item["note_id"],
                    "base_fields": {
                        name: current[name] for name in item["field_changes"]
                    },
                    "prior_tags": list(note.tags),
                }
            )
            for name, value in item["field_changes"].items():
                note[name] = value
            for tag in item.get("add_tags", []):
                if tag not in note.tags:
                    note.tags.append(tag)
            note.tags = [t for t in note.tags if t not in item.get("remove_tags", [])]
            self._tag_edit(note)
            col.update_note(note)
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
        proposal.tags = [
            str(t).strip() for t in (msg.get("tags") or proposal.tags) if str(t).strip()
        ]
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
            self._ledger.append(
                LedgerEntry(
                    id=proposal.id,
                    kind="edit",
                    note_id=note_id,
                    label=_short_label(next(iter(apply_fields.values()), first)),
                    prior_fields=prior_fields,
                    prior_tags=prior_tags,
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
                self._ledger.append(
                    LedgerEntry(
                        id=proposal.id,
                        kind="edit",
                        note_id=proposal.note_id,
                        label=_short_label(next(iter(proposal.fields.values()), "")),
                        prior_fields=prior_fields,
                        prior_tags=prior_tags,
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
            self._revert_entry(entry)
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
                    "text": f"Session undo finished; {errors} change(s) could not be "
                    "reverted (studied or missing notes are kept).",
                }
            )
        self._push_ledger()
        self._after_write([e.note_id for e in self._ledger])

    def _revert_entry(self, entry: LedgerEntry) -> None:
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
        elif entry.kind == "change_set":
            missing_box = {"n": 0}

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
        from .media_staging import MediaError, sound_markers

        if self._media is None:
            raise ProposalError(
                "media attachments are not available in this session"
            )
        try:
            staged = self._media.stage(proposal.id, items)
        except MediaError as exc:
            raise ProposalError(str(exc)) from None
        proposal.media = [item.to_payload() for item in staged]
        # Cross-check [sound:...] markers against what was staged: not errors
        # (the field may reference media already in the collection), but the
        # mismatches the agent most plausibly made by accident are surfaced
        # on the review card.
        referenced = sound_markers(proposal.fields)
        for item in staged:
            if item.filename not in referenced:
                proposal.warnings.append(
                    f"attached audio {item.filename!r} is not referenced by any "
                    "field ([sound:...] marker missing) - it would be imported "
                    "but never played"
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

    def _import_staged_media(self, col: Any, proposal: Proposal) -> None:
        """On accept: import staged files through col.media.add_file (Anki's
        own API - it de-duplicates and renames on content collision) and
        rewrite [sound:] markers to the final names. Idempotent: entries that
        already carry final_name (a re-add after undo) are skipped."""
        from .media_staging import rewrite_sound_markers

        if not proposal.media or self._media is None:
            return
        renames: dict[str, str] = {}
        for entry in proposal.media:
            if entry.get("final_name"):
                continue  # already imported (re-add path)
            staged_path = self._media.staged_path(proposal.id, entry["name"])
            if not staged_path.is_file():
                proposal.warnings.append(
                    f"staged audio {entry['name']!r} is gone (cleaned up?); "
                    "the note was created without importing it"
                )
                continue
            final = str(col.media.add_file(str(staged_path)))
            entry["final_name"] = final
            if final != entry["name"]:
                renames[entry["name"]] = final
        if renames:
            proposal.fields = rewrite_sound_markers(proposal.fields, renames)
            proposal.warnings.append(
                "audio renamed on import (name already taken in your media "
                "folder): "
                + ", ".join(f"{old} -> {new}" for old, new in renames.items())
            )
        self._media.discard(proposal.id)

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


def _short_label(text: str, limit: int = 60) -> str:
    import re

    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[: limit - 1] + "…" if len(text) > limit else text
