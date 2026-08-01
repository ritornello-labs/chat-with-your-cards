"""Safety-net tools (#8): undo, Check Database, backups, sync status.

The reads are direct; the writes go through the proposal flow like every
other write (undo is INSPECTED - the card names exactly what would be
undone, and apply re-checks the queue head). aqt is imported lazily and
only where a feature genuinely needs the running app (sync auth, backup
folder); outside Anki those tools answer honestly instead of crashing.
"""

from __future__ import annotations

from typing import Any

from .registry import ToolContext, ToolRegistry, ToolSpec

SYNC_REQUIRED_NAMES = {
    0: "no_changes",
    1: "normal_sync",
    2: "full_sync",
    3: "full_download",
    4: "full_upload",
}


def get_undo_status(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    status = ctx.col.undo_status()
    undo = str(getattr(status, "undo", "") or "")
    redo = str(getattr(status, "redo", "") or "")
    return {
        "undo": undo or None,
        "redo": redo or None,
        "note": (
            "`undo` is the head of Anki's queue - possibly the user's own "
            "latest action, not necessarily this chat's. undo_last_change "
            "shows it for confirmation before anything fires."
        ),
    }


def get_sync_status(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    try:
        from aqt import mw

        auth = mw.pm.sync_auth() if mw is not None else None
    except Exception:
        return {"available": False, "reason": "sync status requires the Anki app"}
    pending = {
        "notes": int(
            ctx.col.db.scalar("select count() from notes where usn = -1") or 0
        ),
        "cards": int(
            ctx.col.db.scalar("select count() from cards where usn = -1") or 0
        ),
    }
    if auth is None:
        return {
            "logged_in": False,
            "pending_changes": pending,
            "note": "not logged in to AnkiWeb; no sync is configured",
        }
    try:
        status = ctx.col.sync_status(auth)
    except Exception as exc:
        return {
            "logged_in": True,
            "pending_changes": pending,
            "available": False,
            "reason": f"sync status check failed: {exc}",
        }
    required = int(getattr(status, "required", -1))
    return {
        "logged_in": True,
        "required": SYNC_REQUIRED_NAMES.get(required, str(required)),
        "pending_changes": pending,
        "note": (
            "full_sync means the next sync must overwrite one side entirely "
            "- surface that to the user BEFORE proposing schema changes"
        ),
    }


def create_backup_now(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """A backup file write only - the collection itself is untouched, which
    is why this is available even in read-only mode."""
    try:
        from .. import _backup_checkpoint

        ok = _backup_checkpoint("requested via chat", critical=True)
    except ImportError:
        return {"created": False, "reason": "backup requires the Anki app"}
    return {
        "created": bool(ok),
        "note": (
            "restore via File > Switch Profile > Open Backup"
            if ok
            else "backup FAILED - check disk space / permissions"
        ),
    }


def undo_last_change(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    return ctx.proposals.submit_undo_change(args)


def check_database(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    return ctx.proposals.submit_check_database(args)


def sync_now(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    return ctx.proposals.submit_sync_now(args)


def get_preferences(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """Collection-wide preferences (#14), annotated.

    `ignore_accents_in_search` is the one that matters to me directly: it
    changes what my OWN searches match, so a search that found nothing may
    just be this setting.
    """
    prefs = ctx.col.get_preferences()

    def group(message: Any) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for field in message.DESCRIPTOR.fields:
            value = getattr(message, field.name)
            out[field.name] = value if not hasattr(value, "DESCRIPTOR") else group(value)
        return out

    scheduling = group(prefs.scheduling)
    editing = group(prefs.editing)
    return {
        "scheduling": scheduling,
        "reviewing": group(prefs.reviewing),
        "editing": editing,
        "backups": group(prefs.backups),
        "notes": [
            f"the day rolls over at {scheduling.get('rollover')}:00 - "
            "'due today' is measured from there, not midnight",
            (
                "accents are IGNORED in search, so 'cafe' matches 'café'"
                if editing.get("ignore_accents_in_search")
                else "accents are SIGNIFICANT in search, so 'cafe' does NOT "
                "match 'café' - worth remembering when a search finds nothing"
            ),
            "backup retention is readable here but not settable by me: it is "
            "the safety net the destructive proposals rely on",
        ],
    }


def set_preferences(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    return ctx.proposals.submit_set_preferences(args)


def register_maintenance_tools(registry: ToolRegistry) -> None:
    registry.register(
        ToolSpec(
            "get_preferences",
            "Read Anki's collection-wide preferences: day-rollover hour, "
            "learn-ahead window, timebox, review display, editing options, and "
            "backup retention. Check `ignore_accents_in_search` when a search "
            "surprises you - it changes what search_notes matches.",
            {"type": "object", "properties": {}},
            get_preferences,
        )
    )
    registry.register(
        ToolSpec(
            "set_preferences",
            "Propose changes to collection preferences, as dotted paths "
            "(e.g. {\"scheduling.rollover\": 4, \"reviewing.time_limit_secs\": 0}). "
            "Collection-wide and reviewed by the user like any other change. "
            "Backup retention is deliberately not settable here.",
            {
                "type": "object",
                "properties": {
                    "preferences": {
                        "type": "object",
                        "description": "Dotted path -> new value. Call "
                        "get_preferences first to see the current values and "
                        "the exact path names.",
                    },
                    "rationale": {
                        "type": "string",
                        "description": "One sentence: why this helps",
                    },
                },
                "required": ["preferences"],
            },
            set_preferences,
            writes=True,
        )
    )
    registry.register(
        ToolSpec(
            "get_undo_status",
            "What Anki's undo and redo would currently do (labels only, no "
            "changes). Check this before suggesting undo_last_change.",
            {"type": "object", "properties": {}},
            get_undo_status,
        )
    )
    registry.register(
        ToolSpec(
            "get_sync_status",
            "AnkiWeb sync state: logged in or not, whether the next sync is "
            "normal or a FULL one-way sync, and how many notes/cards have "
            "pending local changes. Check before proposing schema-changing "
            "operations so the user hears 'this forces a full upload and "
            "you have N pending changes' in advance.",
            {"type": "object", "properties": {}},
            get_sync_status,
        )
    )
    registry.register(
        ToolSpec(
            "create_backup_now",
            "Write a collection backup file right now (the collection itself "
            "is untouched). The bulk write paths already checkpoint "
            "automatically; use this for an explicit safety net before "
            "something risky.",
            {"type": "object", "properties": {}},
            create_backup_now,
        )
    )
    registry.register(
        ToolSpec(
            "undo_last_change",
            "Undo the head of Anki's undo queue - the review card names "
            "EXACTLY what would be undone (it may be the user's own latest "
            "action), and apply re-checks the queue so a moved head is a "
            "clean error, never a misfire. The inverse is Anki's own redo.",
            {
                "type": "object",
                "properties": {"rationale": {"type": "string"}},
            },
            undo_last_change,
            writes=True,
        )
    )
    registry.register(
        ToolSpec(
            "check_database",
            "Run Anki's Check Database (integrity check + rebuild, also "
            "rebuilds the tag list). One confirmation; a backup checkpoint "
            "is taken first; the full report lands on the resolved card. "
            "The collection is unresponsive while it runs.",
            {
                "type": "object",
                "properties": {"rationale": {"type": "string"}},
            },
            check_database,
            writes=True,
        )
    )
    registry.register(
        ToolSpec(
            "sync_now",
            "Start an AnkiWeb sync after one confirmation; Anki's own sync "
            "window takes over (it asks about full-sync direction itself). "
            "Check get_sync_status first.",
            {
                "type": "object",
                "properties": {"rationale": {"type": "string"}},
            },
            sync_now,
            writes=True,
        )
    )