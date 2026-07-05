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
AI_EDIT_TAG = "ai-edited"
SESSION_TAG_PREFIX = "ai-chat-dock::session-"
DEFAULT_AUTO_ACCEPT_CAP = 20
DEFAULT_WRITE_BUDGET = 200
MAX_SAMPLES = 5

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
    kind: str  # "create" | "edit" | "bulk" | "delete" | "change_set"
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
            "op": self.op,
            "op_args": self.op_args,
            "title": self.title,
            "count": self.count,
            "samples": self.samples,
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
        checkpoint: Callable[[str, bool], None] | None = None,
        observe: Callable[[dict[str, Any]], None] | None = None,
        apply_skill: Callable[["Proposal"], list[str]] | None = None,
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
        self._checkpoint = checkpoint or (lambda _reason, _critical: None)
        # Learning capture (DESIGN.md section 15): "applied" after content
        # writes (snapshot what the system wrote), "resync" after reverts
        # (refresh already-tracked notes only), "reviewed" with the diff
        # between what the agent proposed and what the user accepted.
        self._observe = observe or (lambda _event: None)
        # Applies an accepted skill-update proposal (writes the skill file,
        # archives the prior version, consumes observations); returns
        # warnings/notes to show on the resolved card.
        self._apply_skill = apply_skill
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

    # ---- user-facing decisions (bridge entry points) ----

    def accept(self, msg: dict[str, Any]) -> None:
        proposal = self._proposals.get(str(msg.get("id", "")))
        if proposal is None or proposal.status != PENDING:
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
            elif proposal.kind == "skill_update":
                if self._apply_skill is None:
                    raise ProposalError("skill updates are not available")
                proposal.warnings = self._apply_skill(proposal)
                proposal.status = ACCEPTED
            else:
                raise ProposalError(f"unknown proposal kind {proposal.kind!r}")
            proposal.status = AUTO_ACCEPTED if direct else proposal.status
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
                "warnings": proposal.warnings,
                "revertible": proposal.kind not in ("delete", "skill_update"),
            }
        )
        self._push_ledger()
        if touched:
            self._after_write(touched)

    def _accept_bulk(self, proposal: Proposal) -> list[int]:
        col = self._col()
        self._checkpoint(f"bulk {proposal.op}", False)  # ledger-revertible
        if proposal.op == "rename_tag":
            old = proposal.op_args["old_tag"]
            new = proposal.op_args["new_tag"]
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
            proposal.status = ACCEPTED
            return note_ids
        if proposal.op == "move_cards":
            query = proposal.op_args["query"]
            deck = proposal.op_args["deck"]
            card_ids = list(col.find_cards(query))
            if not card_ids:
                raise ProposalError(f"no cards match {query!r} anymore")
            prior = {int(cid): int(col.get_card(cid).did) for cid in card_ids}
            col.set_deck(card_ids, col.decks.id(deck))
            self._ledger.append(
                LedgerEntry(
                    id=proposal.id,
                    kind="bulk",
                    note_id=0,
                    label=f'move {len(card_ids)} card(s) → "{deck}"',
                    data={"op": "move_cards", "card_decks": prior},
                )
            )
            proposal.status = ACCEPTED
            return [int(col.get_card(cid).nid) for cid in card_ids[:50]]
        if proposal.op == "find_replace":
            applied, skipped = self._apply_items(col, proposal)
            proposal.status = ACCEPTED
            if skipped:
                proposal.warnings = [
                    f"{len(skipped)} note(s) changed since the preview and were "
                    "skipped: " + ", ".join(skipped[:3])
                ]
            return applied
        raise ProposalError(f"unknown bulk op {proposal.op!r}")

    def _accept_delete(self, proposal: Proposal) -> list[int]:
        col = self._col()
        self._checkpoint("delete notes", True)  # irreversible: sync backup
        note_ids = [int(n) for n in proposal.op_args.get("note_ids", [])]
        existing = []
        for nid in note_ids:
            try:
                col.get_note(nid)
                existing.append(nid)
            except Exception:
                continue
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
        proposal.status = ACCEPTED
        return existing

    def _accept_change_set(self, proposal: Proposal) -> list[int]:
        col = self._col()
        self._checkpoint(f"change set: {proposal.title}", False)
        before = self._counts(col)
        applied, skipped = self._apply_items(col, proposal)
        after = self._counts(col)
        proposal.status = ACCEPTED
        warnings = self._stats_drift_warnings(before, after)
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
        self._tag_edit(note)
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
        self._observe({"event": "applied", "note_ids": [int(proposal.note_id)]})
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
        if entry.kind == "create":
            try:
                note = col.get_note(entry.note_id)
            except Exception:
                raise ProposalError("note already deleted") from None
            if any(getattr(card, "reps", 0) > 0 for card in note.cards()):
                raise ProposalError("note has been studied; delete it in the Browser")
            col.remove_notes([entry.note_id])
        elif entry.kind == "bulk" and entry.data.get("op") == "rename_tag":
            # Reverse rename; if the target tag pre-existed the merge cannot be
            # fully separated (warned at proposal time).
            col.tags.rename(entry.data["new_tag"], entry.data["old_tag"])
        elif entry.kind == "bulk" and entry.data.get("op") == "move_cards":
            by_deck: dict[int, list[int]] = {}
            for cid, did in entry.data.get("card_decks", {}).items():
                by_deck.setdefault(int(did), []).append(int(cid))
            for did, cids in by_deck.items():
                col.set_deck(cids, did)
        elif entry.kind == "change_set":
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
            if missing:
                self._push(
                    {
                        "type": "notice",
                        "text": f"Revert: {missing} note(s) no longer exist and "
                        "were skipped.",
                    }
                )
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
        if resync:
            self._observe({"event": "resync", "note_ids": resync})

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
        for tag in (self.created_tag, self.session_tag):
            if tag and tag not in tags:
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
        self._observe({"event": "applied", "note_ids": [int(note.id)]})
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
