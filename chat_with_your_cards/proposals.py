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

import secrets
from dataclasses import dataclass, field
from typing import Any, Callable

AI_TAG = "ai-created"
SESSION_TAG_PREFIX = "ai-chat-dock::session-"
DEFAULT_AUTO_ACCEPT_CAP = 20

PENDING = "pending"
ACCEPTED = "accepted"
AUTO_ACCEPTED = "auto-accepted"
REJECTED = "rejected"
UNDONE = "undone"
SUPERSEDED = "superseded"


class ProposalError(Exception):
    """Validation failure reported back to the agent as a tool error."""


@dataclass
class Proposal:
    id: str
    kind: str  # "create" | "edit"
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

    def to_payload(self) -> dict[str, Any]:
        fields_payload = []
        for name, new in self.fields.items():
            entry: dict[str, Any] = {"name": name, "new": new}
            if self.kind == "edit":
                entry["old"] = self.base_fields.get(name, "")
            fields_payload.append(entry)
        return {
            "id": self.id,
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
        }


@dataclass
class LedgerEntry:
    id: str
    kind: str  # "create" | "edit"
    note_id: int
    label: str
    prior_fields: dict[str, str] = field(default_factory=dict)  # edit revert data
    prior_tags: list[str] | None = None
    undone: bool = False

    def to_payload(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "note_id": self.note_id,
            "label": self.label,
            "undone": self.undone,
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
    ) -> None:
        self._get_col = get_col
        self._push = push
        self._config = config
        self._save_pins = save_pins
        # Called with the affected note ids after any collection write, so the
        # add-on can refresh the reviewer if it is showing one of them.
        self._after_write = after_write or (lambda _ids: None)
        self._proposals: dict[str, Proposal] = {}
        self._ledger: list[LedgerEntry] = []
        self._counter = 0
        self._auto_accepted = 0
        self._auto_accept_pause_notified = False
        self.session_id = secrets.token_hex(4)

    # ---- session lifecycle ----

    def new_session(self) -> None:
        self._proposals.clear()
        self._ledger.clear()
        self._counter = 0
        self._auto_accepted = 0
        self._auto_accept_pause_notified = False
        self.session_id = secrets.token_hex(4)

    @property
    def session_tag(self) -> str:
        return SESSION_TAG_PREFIX + self.session_id

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
            self._push(
                {"type": "proposal_resolved", "id": prev.id, "status": SUPERSEDED}
            )

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
        if self._find_deck_id(col, deck) is None:
            warnings.append(f"deck {deck!r} does not exist yet; it will be created")

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
        proposal.previews = self._render_create_preview(col, model, proposal)

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
        return {
            "status": "pending_user_review",
            "proposal_id": proposal.id,
            "note": "The user sees a proposal card with field diffs and will decide.",
        }

    # ---- user-facing decisions (bridge entry points) ----

    def accept(self, msg: dict[str, Any]) -> None:
        proposal = self._proposals.get(str(msg.get("id", "")))
        if proposal is None or proposal.status != PENDING:
            return
        # The user may have edited values / narrowed the accepted field set
        # in the proposal card before accepting.
        final_fields = {
            str(k): str(v) for k, v in (msg.get("fields") or proposal.fields).items()
        }
        try:
            if proposal.kind == "create":
                self._accept_create(proposal, msg, final_fields)
            else:
                self._accept_edit(proposal, msg, final_fields)
        except ProposalError as exc:
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
            }
        )
        self._push_ledger()
        if proposal.note_id is not None:
            self._after_write([proposal.note_id])

    def _accept_create(
        self, proposal: Proposal, msg: dict[str, Any], final_fields: dict[str, str]
    ) -> None:
        col = self._col()
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

    def _accept_edit(
        self, proposal: Proposal, msg: dict[str, Any], final_fields: dict[str, str]
    ) -> None:
        col = self._col()
        accepted = msg.get("accepted_fields")
        names = [str(n) for n in accepted] if accepted is not None else list(final_fields)
        apply_fields = {n: final_fields[n] for n in names if n in final_fields}
        if not apply_fields and not proposal.add_tags and not proposal.remove_tags:
            raise ProposalError("nothing selected to apply")
        assert proposal.note_id is not None
        try:
            note = col.get_note(proposal.note_id)
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
            raise ProposalError("note changed underneath; proposal refreshed for re-review")

        prior_fields = {name: current[name] for name in apply_fields}
        prior_tags = list(note.tags)
        for name, value in apply_fields.items():
            note[name] = value
        for tag in proposal.add_tags:
            if tag not in note.tags:
                note.tags.append(tag)
        note.tags = [t for t in note.tags if t not in proposal.remove_tags]
        col.update_note(note)
        proposal.status = ACCEPTED
        first = next(iter(prior_fields.values()), "")
        self._ledger.append(
            LedgerEntry(
                id=proposal.id,
                kind="edit",
                note_id=proposal.note_id,
                label=_short_label(next(iter(apply_fields.values()), first)),
                prior_fields=prior_fields,
                prior_tags=prior_tags,
            )
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
        col = self._col()
        try:
            if proposal.kind == "create":
                model = self._validate_note_type_and_fields(
                    col, proposal.note_type, proposal.fields
                )
                proposal.note_id = self._apply_create(col, model, proposal)
            else:
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
        if entry.kind == "create":
            try:
                note = col.get_note(entry.note_id)
            except Exception:
                raise ProposalError("note already deleted") from None
            if any(getattr(card, "reps", 0) > 0 for card in note.cards()):
                raise ProposalError("note has been studied; delete it in the Browser")
            col.remove_notes([entry.note_id])
        else:
            try:
                note = col.get_note(entry.note_id)
            except Exception:
                raise ProposalError("note no longer exists") from None
            for name, value in entry.prior_fields.items():
                note[name] = value
            if entry.prior_tags is not None:
                note.tags = list(entry.prior_tags)
            col.update_note(note)
        entry.undone = True

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
    def _looks_duplicate(col: Any, model: Any, first_name: str, value: str) -> bool:
        try:
            query = '"{}:{}" note:"{}"'.format(
                first_name, value.replace('"', '\\"'), model["name"]
            )
            return bool(col.find_notes(query))
        except Exception:
            return False

    def _apply_create(self, col: Any, model: Any, proposal: Proposal) -> int:
        note = col.new_note(model)
        for name, value in proposal.fields.items():
            note[name] = value
        tags = list(proposal.tags)
        for tag in (AI_TAG, self.session_tag):
            if tag not in tags:
                tags.append(tag)
        note.tags = tags
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
        return int(note.id)

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
