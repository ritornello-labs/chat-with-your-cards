"""Bulk authoring tools (#11): CSV/text import and export.

Import goes through Anki's REAL pipeline (get_csv_metadata /
import_csv) behind one review card - the difference between proposing
200 vocab notes and importing them. Export writes files only, so those
tools are read-class like create_backup_now. .colpkg import is
deliberately absent: it replaces the whole collection and stays
human-only.
"""

from __future__ import annotations

import os
from typing import Any

from .registry import ToolContext, ToolRegistry, ToolSpec

DELIMITER_NAMES = {1: "comma", 2: "semicolon", 3: "tab", 4: "space", 5: "pipe", 6: "colon"}
DUPE_NAMES = {0: "update", 1: "preserve", 2: "duplicate"}
EXPORT_MAX_PREVIEW = 5


def preview_csv_import(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """What Anki detected about the file - inspect BEFORE import_csv_file."""
    path = os.path.expanduser(str(args.get("path", "")).strip())
    if not os.path.isfile(path):
        raise ValueError(f"no file at {path}")
    meta = ctx.col.get_csv_metadata(path, None)
    notetype = None
    which = getattr(meta, "WhichOneof", lambda _n: None)("notetype")
    if which == "global_notetype":
        model = ctx.col.models.get(int(meta.global_notetype.id))
        notetype = {"name": model["name"] if model else None, "column": None}
    elif which == "notetype_column":
        notetype = {"name": None, "column": int(meta.notetype_column)}
    preview_rows = [
        list(getattr(row, "vals", []))[:6]
        for row in list(getattr(meta, "preview", []))[:EXPORT_MAX_PREVIEW]
    ]
    return {
        "path": path,
        "delimiter": DELIMITER_NAMES.get(int(getattr(meta, "delimiter", 0)), "unknown"),
        "is_html": bool(getattr(meta, "is_html", False)),
        "notetype": notetype,
        "deck_id": int(getattr(meta, "deck_id", 0) or 0) or None,
        "existing_notes_default": DUPE_NAMES.get(
            int(getattr(meta, "dupe_resolution", 0)), "unknown"
        ),
        "preview_rows": preview_rows,
        "note": "import with import_csv_file; existing_notes defaults to "
        "'preserve' there (safer than Anki's own Update default)",
    }


def import_csv_file(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    return ctx.proposals.submit_import_csv(args)


def _export_limit(ctx: ToolContext, args: dict[str, Any]) -> tuple[Any, str]:
    """ExportLimit from deck or query (or the whole collection)."""
    from anki.import_export_pb2 import ExportLimit

    deck = str(args.get("deck", "") or "").strip()
    query = str(args.get("query", "") or "").strip()
    if deck and query:
        raise ValueError("pass deck OR query, not both")
    if deck:
        try:
            did = int(ctx.col.decks.id_for_name(deck))
        except Exception:
            did = 0
        if not did:
            raise ValueError(f"deck {deck!r} not found")
        return ExportLimit(deck_id=did), f'deck "{deck}"'
    if query:
        note_ids = [int(n) for n in ctx.col.find_notes(query)]
        if not note_ids:
            raise ValueError(f"no notes match {query!r}")
        limit = ExportLimit()
        limit.note_ids.note_ids.extend(note_ids)
        return limit, f"{len(note_ids)} note(s) matching {query!r}"
    return ExportLimit(whole_collection={}), "the whole collection"


def _resolve_out_path(args: dict[str, Any], extension: str) -> str:
    out = os.path.expanduser(str(args.get("out_path", "")).strip())
    if not out:
        raise ValueError("needs out_path")
    if not out.endswith(extension):
        out += extension
    if os.path.exists(out) and not bool(args.get("overwrite", False)):
        raise ValueError(f"{out} exists; pass overwrite=true to replace it")
    return out


def export_csv(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    out = _resolve_out_path(args, ".txt")
    limit, scope = _export_limit(ctx, args)
    count = ctx.col.export_note_csv(
        out_path=out,
        limit=limit,
        with_html=bool(args.get("with_html", True)),
        with_tags=bool(args.get("with_tags", True)),
        with_deck=bool(args.get("with_deck", True)),
        with_notetype=bool(args.get("with_notetype", True)),
        with_guid=bool(args.get("with_guid", False)),
    )
    return {"exported": count, "scope": scope, "path": out}


def export_apkg(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    from anki.import_export_pb2 import ExportAnkiPackageOptions

    out = _resolve_out_path(args, ".apkg")
    limit, scope = _export_limit(ctx, args)
    count = ctx.col.export_anki_package(
        out_path=out,
        options=ExportAnkiPackageOptions(
            with_scheduling=bool(args.get("with_scheduling", False)),
            with_media=bool(args.get("with_media", True)),
            legacy=True,
        ),
        limit=limit,
    )
    return {
        "exported_cards": count,
        "scope": scope,
        "path": out,
        "caveat": "filtered decks do not survive .apkg export (workspace "
        "lesson) - ship curricula as saved searches + tags instead",
    }


def register_authoring_tools(registry: ToolRegistry) -> None:
    registry.register(
        ToolSpec(
            "preview_csv_import",
            "Inspect a CSV/text file the way Anki's import dialog would: "
            "detected delimiter, HTML flag, note type / deck mapping, and "
            "the first rows. Always preview before import_csv_file.",
            {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Absolute file path"},
                },
                "required": ["path"],
            },
            preview_csv_import,
        )
    )
    registry.register(
        ToolSpec(
            "import_csv_file",
            "Import a text/CSV file through Anki's real pipeline - one "
            "review card instead of N proposals; the resolved card reports "
            "exactly what was created vs updated. existing_notes defaults "
            "to 'preserve' (SAFER than Anki's Update default); 'update' "
            "rewrites notes matched on the first field and is warned in "
            "capitals. Revert removes the CREATED notes (studied ones are "
            "kept and reported); updated notes only come back via the "
            "backup.",
            {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "deck": {"type": "string", "description": "Target deck "
                             "(must exist; overrides the file's own column)"},
                    "note_type": {"type": "string"},
                    "existing_notes": {
                        "type": "string",
                        "enum": ["preserve", "update", "duplicate"],
                        "default": "preserve",
                    },
                    "delimiter": {
                        "type": "string",
                        "enum": ["TAB", "SPACE", "COMMA", "SEMICOLON", "PIPE", "COLON"],
                        "description": "Override autodetection",
                    },
                    "tags": {"type": "array", "items": {"type": "string"}},
                    "rationale": {"type": "string"},
                },
                "required": ["path"],
            },
            import_csv_file,
            writes=True,
        )
    )
    registry.register(
        ToolSpec(
            "export_csv",
            "Export notes as tab-separated text (Anki's own format, "
            "re-importable). Scope with deck OR query, else the whole "
            "collection. Writes a file; the collection is untouched.",
            {
                "type": "object",
                "properties": {
                    "out_path": {"type": "string"},
                    "deck": {"type": "string"},
                    "query": {"type": "string"},
                    "with_html": {"type": "boolean", "default": True},
                    "with_tags": {"type": "boolean", "default": True},
                    "with_deck": {"type": "boolean", "default": True},
                    "with_notetype": {"type": "boolean", "default": True},
                    "with_guid": {"type": "boolean", "default": False},
                    "overwrite": {"type": "boolean", "default": False},
                },
                "required": ["out_path"],
            },
            export_csv,
        )
    )
    registry.register(
        ToolSpec(
            "export_apkg",
            "Export an .apkg (deck package) for sharing or backup. Scope "
            "with deck OR query, else the whole collection. NOTE: filtered "
            "decks do not survive .apkg export - ship curricula as saved "
            "searches + tags. Writes a file; the collection is untouched.",
            {
                "type": "object",
                "properties": {
                    "out_path": {"type": "string"},
                    "deck": {"type": "string"},
                    "query": {"type": "string"},
                    "with_scheduling": {"type": "boolean", "default": False},
                    "with_media": {"type": "boolean", "default": True},
                    "overwrite": {"type": "boolean", "default": False},
                },
                "required": ["out_path"],
            },
            export_apkg,
        )
    )