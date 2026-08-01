"""Learning from the user's edits to AI-written notes (DESIGN.md section 15).

The store keeps one snapshot per AI-touched note: the last state the
system (agent write or user-reviewed accept) left it in. Diffing the
live note against its snapshot detects *any* later user edit - in the
Anki editor, the Browser, or on AnkiDroid/AnkiMobile after a sync -
without editor hooks. Edits found this way, plus the diffs the user
makes on proposal cards before accepting, accumulate as observations;
a reflection chat later turns them into a user-confirmed update of the
card-authoring skill.

Size is structurally bounded: one snapshot per AI-touched note is a
constant factor of the AI-touched slice of the collection itself, so
there is deliberately no cap (decision 2026-07-05).

No aqt imports; the collection is duck-typed like in proposals.py.
"""

from __future__ import annotations

import json
import re
import secrets
import time
from pathlib import Path
from typing import Any

OBS_REVIEWED = "reviewed"  # user edited the proposal card before accepting
OBS_EDITED = "edited_later"  # note changed after the system last wrote it
OBS_DELETED = "deleted_later"  # AI-touched note was deleted outside the chat

DEFAULT_NUDGE_THRESHOLD = 10
DEFAULT_NUDGE_DAYS = 7


def _norm(value: str) -> str:
    """Whitespace-insensitive comparison; everything else is signal."""
    return re.sub(r"\s+", " ", str(value)).strip()


class LearningStore:
    """Snapshots + observations under user_files/learning/, plus the
    skill file the reflection flow updates. All methods run on Anki's
    main thread (tool calls are marshaled there by the add-on glue)."""

    def __init__(self, root: Path, skill_path: Path) -> None:
        self._root = root
        self._skill_path = skill_path
        self._snapshots_path = root / "snapshots.json"
        self._observations_path = root / "observations.jsonl"
        self._backups_dir = root / "skill-backups"
        self._snapshots: dict[str, dict[str, Any]] = {}
        self._observations: dict[str, dict[str, Any]] = {}
        self._load()

    # ---- persistence ----

    def _load(self) -> None:
        try:
            raw = json.loads(self._snapshots_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                self._snapshots = raw
        except Exception:
            self._snapshots = {}
        try:
            lines = self._observations_path.read_text(encoding="utf-8").splitlines()
        except Exception:
            return
        for line in lines:
            try:
                event = json.loads(line)
            except Exception:
                continue
            if event.get("event") == "observation":
                obs = event.get("observation") or {}
                if obs.get("id"):
                    self._observations[str(obs["id"])] = obs
            elif event.get("event") == "consumed":
                for oid in event.get("ids") or []:
                    self._observations.pop(str(oid), None)

    def _save_snapshots(self) -> None:
        try:
            self._root.mkdir(parents=True, exist_ok=True)
            self._snapshots_path.write_text(
                json.dumps(self._snapshots, ensure_ascii=False), encoding="utf-8"
            )
        except OSError:
            pass

    def _append(self, event: dict[str, Any]) -> None:
        try:
            self._root.mkdir(parents=True, exist_ok=True)
            with self._observations_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(event, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def _record(self, obs: dict[str, Any]) -> None:
        obs.setdefault("id", secrets.token_hex(4))
        obs.setdefault("ts", int(time.time()))
        self._observations[str(obs["id"])] = obs
        self._append({"event": "observation", "observation": obs})

    # ---- snapshots (what the system last wrote) ----

    def snapshot_notes(self, col: Any, note_ids: list[int], *, add: bool = True) -> None:
        """Upsert snapshots for notes the system just content-wrote (or, with
        add=False, resync only notes already tracked - used after reverts so
        e.g. a bulk tag rename does not put untracked notes under watch)."""
        changed = False
        for nid in note_ids:
            key = str(int(nid))
            if not add and key not in self._snapshots:
                continue
            try:
                note = col.get_note(int(nid))
            except Exception:
                if self._snapshots.pop(key, None) is not None:
                    changed = True  # system removed it (create-revert): no signal
                continue
            self._snapshots[key] = self._describe(col, note)
            changed = True
        if changed:
            self._save_snapshots()

    @staticmethod
    def _describe(col: Any, note: Any) -> dict[str, Any]:
        deck = ""
        note_type = ""
        try:
            note_type = note.note_type()["name"]
        except Exception:
            pass
        try:
            cards = list(note.cards())
            if cards:
                deck = col.decks.name(cards[0].did)
        except Exception:
            pass
        return {
            "fields": dict(note.items()),
            "tags": list(note.tags),
            "note_type": note_type,
            "deck": deck,
            "mod": int(getattr(note, "mod", 0) or 0),
        }

    # ---- capture channel 1: proposal-card edits at accept time ----

    def record_review(
        self,
        *,
        proposal_kind: str,
        note_type: str,
        deck_before: str = "",
        deck_after: str = "",
        tags_before: list[str] | None = None,
        tags_after: list[str] | None = None,
        fields_before: dict[str, str] | None = None,
        fields_after: dict[str, str] | None = None,
        declined_fields: list[str] | None = None,
        declined_field_comments: dict[str, str] | None = None,
    ) -> bool:
        """Diff what the agent proposed against what the user accepted;
        returns True if anything material was recorded."""
        before = fields_before or {}
        after = fields_after or {}
        changes = [
            {"name": name, "before": before.get(name, ""), "after": after.get(name, "")}
            for name in before
            if name in after and _norm(before[name]) != _norm(after[name])
        ]
        tb, ta = sorted(tags_before or []), sorted(tags_after or [])
        tags_changed = tb != ta
        deck_changed = bool(deck_before and deck_after and deck_before != deck_after)
        declined = list(declined_fields or [])
        if not changes and not tags_changed and not deck_changed and not declined:
            return False
        obs: dict[str, Any] = {
            "kind": OBS_REVIEWED,
            "proposal_kind": proposal_kind,
            "note_type": note_type,
            "changes": changes,
        }
        if tags_changed:
            obs["tags_before"], obs["tags_after"] = tb, ta
        if deck_changed:
            obs["deck_before"], obs["deck_after"] = deck_before, deck_after
        if declined:
            obs["declined_fields"] = declined
            # Why the user skipped it (#24d). A bare "declined Back" teaches
            # nothing; "Back - too wordy" is the whole point of the record.
            comments = {
                name: text.strip()
                for name, text in (declined_field_comments or {}).items()
                if name in declined and str(text).strip()
            }
            if comments:
                obs["declined_field_comments"] = comments
        self._record(obs)
        return True

    # ---- capture channel 2: later edits anywhere (snapshot diff scan) ----

    def scan(self, col: Any) -> int:
        """Compare each snapshot against the live note; record an observation
        per changed or deleted note and advance the snapshot to the current
        state (so the same edit is never re-reported). Returns how many new
        observations were recorded."""
        if not self._snapshots:
            return 0
        mods = self._bulk_mods(col)
        new = 0
        changed_snaps = False
        for key, snap in list(self._snapshots.items()):
            nid = int(key)
            if mods is not None and mods.get(nid) == snap.get("mod") and nid in mods:
                continue  # cheap skip: note untouched since we wrote it
            try:
                note = col.get_note(nid)
            except Exception:
                note = None
            if note is None:
                self._record(
                    {
                        "kind": OBS_DELETED,
                        "note_id": nid,
                        "note_type": snap.get("note_type", ""),
                        "deck": snap.get("deck", ""),
                        "fields": snap.get("fields", {}),
                        "tags": snap.get("tags", []),
                    }
                )
                self._snapshots.pop(key, None)
                new += 1
                changed_snaps = True
                continue
            fields = dict(note.items())
            old_fields = snap.get("fields", {})
            changes = [
                {"name": n, "before": old_fields.get(n, ""), "after": fields.get(n, "")}
                for n in set(old_fields) | set(fields)
                if _norm(old_fields.get(n, "")) != _norm(fields.get(n, ""))
            ]
            tb = sorted(snap.get("tags", []))
            ta = sorted(note.tags)
            if changes or tb != ta:
                obs: dict[str, Any] = {
                    "kind": OBS_EDITED,
                    "note_id": nid,
                    "note_type": snap.get("note_type", ""),
                    "deck": snap.get("deck", ""),
                    "changes": changes,
                }
                if tb != ta:
                    obs["tags_before"], obs["tags_after"] = tb, ta
                self._record(obs)
                new += 1
            current = self._describe(col, note)
            if current != snap:
                self._snapshots[key] = current
                changed_snaps = True
        if changed_snaps:
            self._save_snapshots()
        return new

    def _bulk_mods(self, col: Any) -> dict[int, int] | None:
        """One query for all tracked notes' mod stamps (real Anki); None on
        fake collections, which fall back to per-note field comparison."""
        try:
            ids = ",".join(self._snapshots.keys())
            rows = col.db.all(f"SELECT id, mod FROM notes WHERE id IN ({ids})")
            return {int(r[0]): int(r[1]) for r in rows}
        except Exception:
            return None

    # ---- observations: pending / consume ----

    def pending(self) -> list[dict[str, Any]]:
        return sorted(self._observations.values(), key=lambda o: o.get("ts", 0))

    def pending_ids(self) -> list[str]:
        return [str(o["id"]) for o in self.pending()]

    def consume(self, ids: list[str]) -> None:
        ids = [str(i) for i in ids if str(i) in self._observations]
        if not ids:
            return
        for oid in ids:
            self._observations.pop(oid, None)
        self._append({"event": "consumed", "ids": ids})

    def nudge_state(
        self,
        threshold: int = DEFAULT_NUDGE_THRESHOLD,
        days: int = DEFAULT_NUDGE_DAYS,
    ) -> dict[str, Any]:
        pending = self.pending()
        oldest = pending[0].get("ts", 0) if pending else None
        stale = (
            oldest is not None and (time.time() - float(oldest)) > days * 86400
        )
        return {
            "pending": len(pending),
            "nudge": len(pending) >= max(1, threshold) or (bool(pending) and stale),
        }

    # ---- the skill file the reflection flow maintains ----

    def read_skill(self) -> str:
        try:
            return self._skill_path.read_text(encoding="utf-8")
        except OSError:
            return ""

    @property
    def skill_path(self) -> Path:
        return self._skill_path

    def write_skill(self, new_content: str) -> Path | None:
        """Write the updated skill; the prior version is archived first so an
        accepted update always has a way back. Returns the backup path."""
        backup: Path | None = None
        old = self.read_skill()
        if old:
            self._backups_dir.mkdir(parents=True, exist_ok=True)
            backup = self._backups_dir / f"SKILL-{int(time.time())}.md"
            backup.write_text(old, encoding="utf-8")
        self._skill_path.parent.mkdir(parents=True, exist_ok=True)
        self._skill_path.write_text(new_content, encoding="utf-8")
        return backup

    # ---- doctor ----

    def stats(self) -> dict[str, Any]:
        size = 0
        for path in (self._snapshots_path, self._observations_path):
            try:
                size += path.stat().st_size
            except OSError:
                pass
        try:
            size += sum(p.stat().st_size for p in self._backups_dir.glob("*.md"))
        except OSError:
            pass
        return {
            "snapshots": len(self._snapshots),
            "pending": len(self._observations),
            "bytes": size,
        }
