"""Precondition validators for the proposal write path (SAFETY.md Part 2).

Pure functions that run BEFORE any mutation. Each validator rejects a class
of silent corruption that Anki either performs quietly or cannot repair, and
raises ``ContractError`` with a message written for the *agent* to read and
correct itself -- not for an end user. Wiring into ``ProposalManager`` happens
elsewhere; this module never mutates and never imports ``aqt``.

Constraints: stdlib only (ships in an AnkiWeb add-on, no pip deps, no compiled
extensions), no ``aqt`` import, collection and proposal are duck-typed (``Any``)
so the whole module is unit-testable against a fake collection.
"""

from __future__ import annotations

import html
import os
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import unquote

# Legacy notetype kind codes (pylib ``anki.consts``): 0 = standard, 1 = cloze.
MODEL_STD = 0
MODEL_CLOZE = 1

# Anki's cloze marker (rslib/src/cloze.rs:29-30): ``{{c1::...}}`` / ``{{c1,2::...}}``.
_CLOZE_RE = re.compile(r"\{\{c[\d,]+::.*?\}\}", re.DOTALL)

# Tag separators Anki splits on, with no escaping (rslib/src/tags/mod.rs:55):
# an ASCII space or the ideographic space U+3000. Nothing else splits a tag.
_TAG_SEPARATORS = (" ", "　")

# Media references we can resolve to a local file. Remote (``://``) and inline
# ``data:`` sources are skipped: there is no local file to look for.
_IMG_SRC_RE = re.compile(r"""<img\b[^>]*?\bsrc\s*=\s*["']([^"']+)["']""", re.IGNORECASE)
_SOUND_RE = re.compile(r"\[sound:([^\]]+)\]", re.IGNORECASE)

# HTML stripping that mirrors rslib ``strip_html_preserving_media_filenames``
# (text.rs:366): media tags collapse to their filename so an image-only first
# field still carries identifying content; everything else is removed.
_MEDIA_TAG_RE = re.compile(
    r"""<\s*(?:img|audio|video|object|source)\b[^>]*?"""
    r"""\b(?:src|data)\s*=\s*["']([^"']+)["'][^>]*>""",
    re.IGNORECASE | re.DOTALL,
)
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_STYLE_SCRIPT_RE = re.compile(r"<(style|script)\b[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]*>", re.DOTALL)


class ContractError(Exception):
    """A precondition failed. The message is addressed to the agent, telling it
    what is wrong and how to fix the proposal so a retry can succeed."""


class _NewDeckSentinel:
    """Marker returned by ``resolve_writable_deck`` when the deck does not exist
    and is safe to create as a new *normal* deck."""

    def __repr__(self) -> str:  # pragma: no cover - debugging aid only
        return "WILL_CREATE"


# Singleton: "does not exist, will be created as a normal home deck".
WILL_CREATE = _NewDeckSentinel()


@dataclass
class CheckResult:
    """Outcome of ``check_create`` / ``check_edit``: errors and warnings kept
    apart. ``errors`` block the write; ``warnings`` are surfaced to the user but
    do not. ``deck_id`` / ``tags`` carry values a caller can reuse once ``ok``."""

    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    deck_id: Any = None  # int, WILL_CREATE, or None when unresolved/not applicable
    tags: list[str] | None = None  # NFC-normalized tags, set when tag check passed

    @property
    def ok(self) -> bool:
        return not self.errors

    def raise_if_failed(self) -> None:
        """Raise a single ``ContractError`` joining every collected error, or do
        nothing if the proposal is clean. Warnings never raise."""
        if self.errors:
            raise ContractError("\n".join(self.errors))


# --------------------------------------------------------------------------- #
# Individual validators (each raises ContractError; all are pure).             #
# --------------------------------------------------------------------------- #


def resolve_writable_deck(col: Any, name: str) -> Any:
    """Resolve ``name`` to a writable (normal) home-deck id -- the single deck
    chokepoint (SAFETY.md rule 2).

    Rejects a filtered (``dyn``) deck, and rejects any name whose *existing*
    ancestor chain contains a filtered deck: Anki forbids a filtered deck from
    having children and raises ``MustBeLeafNode`` (decks/addupdate.rs:114-119),
    but a clear message first beats a backend panic. Cards added to a filtered
    deck silently land in Default with ``odid=0`` (cardgen.rs:333-352), a state
    Check Database cannot even see (dbcheck.rs:208-231).

    Returns the deck id (int) for an existing normal deck, or ``WILL_CREATE``
    when the deck does not exist and its parents are all normal.
    """
    name = (name or "").strip()
    if not name:
        raise ContractError(
            "no deck was given. Name a normal deck to hold the card, e.g. "
            "'Spanish::Verbs'."
        )

    parts = name.split("::")
    for depth in range(1, len(parts)):
        ancestor = "::".join(parts[:depth])
        did = _find_deck_id(col, ancestor)
        if did is not None and _is_filtered(col, did):
            raise ContractError(
                f"deck {name!r} cannot be used: its ancestor {ancestor!r} is a "
                "filtered deck, and Anki forbids a filtered deck from having "
                "child decks (MustBeLeafNode). Choose a destination whose parent "
                "decks are all normal decks."
            )

    did = _find_deck_id(col, name)
    if did is None:
        return WILL_CREATE
    if _is_filtered(col, did):
        raise ContractError(
            f"deck {name!r} is a filtered deck; cards must live in a normal home "
            "deck. Pick or create a normal deck as the destination, then build a "
            "filtered deck from it if you need one. Adding cards to a filtered "
            "deck directly corrupts them (they lose their home deck)."
        )
    return int(did)


def check_fields(col: Any, notetype: str, fields: dict[str, str]) -> dict[str, Any]:
    """Resolve the note type by name and require the field set to match it
    EXACTLY (SAFETY.md hazard 4).

    ``notetype`` is the note-type *name*. Returns the resolved note-type dict so
    the model-based validators (``check_first_field`` / ``check_cloze``) can run
    without a second lookup. A field-count mismatch is never padded or dropped:
    Anki silently merges surplus fields into the last one joined by ``"; "``
    (rslib/src/notes/mod.rs:264-275), so any extra or missing field is rejected.
    """
    model = _resolve_notetype(col, notetype)
    expected = _field_names(model)
    expected_set = set(expected)
    missing = [n for n in expected if n not in fields]
    surplus = [n for n in fields if n not in expected_set]
    if missing or surplus:
        detail = ""
        if missing:
            detail += f" Missing field(s): {missing}."
        if surplus:
            detail += f" Field(s) not in this note type: {surplus}."
        raise ContractError(
            f"fields do not match note type {notetype!r}.{detail} Provide exactly "
            f"these {len(expected)} field(s), no more and no fewer: {expected}. "
            "Anki does not pad or drop mismatched fields; it silently merges "
            'surplus values into the last field joined by "; ", so this is rejected.'
        )
    return model


def check_first_field(notetype: dict[str, Any], fields: dict[str, str]) -> None:
    """Require the first field (ord 0) to be non-empty after HTML stripping
    (SAFETY.md; notes/mod.rs:206-207).

    The first field is the note's duplicate key (``csum``) and, unless overridden,
    its sort field -- both hard-wired to ord 0. An empty first field collides
    with every other empty-first-field note, silently breaking dedup and sort.
    ``notetype`` is the resolved note-type dict.
    """
    names = _field_names(notetype)
    if not names:
        raise ContractError(
            f"note type {notetype.get('name', '?')!r} has no fields; it cannot "
            "hold a note."
        )
    first = names[0]
    if not _strip_html_preserving_media(fields.get(first, "")).strip():
        raise ContractError(
            f"first field {first!r} is empty after stripping HTML. It is the "
            "note's duplicate key (csum) and sort key, hard-wired to the first "
            "field, so an empty value collides with every other empty-first-field "
            f"note. Put the card's identifying content in {first!r}."
        )


def check_cloze(notetype: dict[str, Any], fields: dict[str, str]) -> None:
    """For a cloze note type, require at least one ``{{cN::...}}`` marker across
    the fields (SAFETY.md; notetype/mod.rs:441-445).

    A cloze note type with no marker silently generates a single card with no
    cloze deletion -- a useless card. A no-op for standard note types.
    ``notetype`` is the resolved note-type dict.
    """
    if int(notetype.get("type", MODEL_STD)) != MODEL_CLOZE:
        return
    if any(_CLOZE_RE.search(value or "") for value in fields.values()):
        return
    raise ContractError(
        f"note type {notetype.get('name', '?')!r} is a cloze type but no field "
        "contains a cloze marker such as {{c1::hidden text}}. Anki would generate "
        "a single card with nothing hidden. Add at least one {{cN::...}} marker."
    )


def check_tags(tags: list[str]) -> list[str]:
    """NFC-normalize tags and reject any that would silently split into two.

    A tag containing a space or U+3000 (IDEOGRAPHIC SPACE) becomes two tags:
    Anki stores tags space-separated in one column with no escaping
    (rslib/src/tags/mod.rs:46-56). Returns the NFC-normalized tag list.
    """
    normalized: list[str] = []
    for tag in tags:
        norm = unicodedata.normalize("NFC", str(tag))
        for sep in _TAG_SEPARATORS:
            if sep in norm:
                label = "a space" if sep == " " else "an ideographic space (U+3000)"
                raise ContractError(
                    f"tag {norm!r} contains {label}. Anki stores tags "
                    "space-separated with no escaping, so this would silently "
                    "split into separate tags. Use '::' for a hierarchy (a::b) or "
                    "'_' / '-' instead of a space."
                )
        normalized.append(norm)
    return normalized


def check_media_refs(col: Any, fields: dict[str, str]) -> list[str]:
    """WARN (never reject) for each local media file referenced but absent from
    the media folder (SAFETY.md hazard-adjacent: a broken asset).

    Covers ``<img src=...>`` and ``[sound:...]``. Remote (``://``) and ``data:``
    references are skipped. Returns warning strings; an empty list means every
    referenced file is present, or the media folder could not be inspected.
    """
    warnings: list[str] = []
    seen: set[str] = set()
    for value in fields.values():
        refs = _IMG_SRC_RE.findall(value or "") + _SOUND_RE.findall(value or "")
        for raw in refs:
            fname = raw.strip()
            if not fname or fname in seen:
                continue
            if "://" in fname or fname.lower().startswith("data:"):
                continue
            seen.add(fname)
            present = _media_has(col, fname)
            if present is False:
                warnings.append(
                    f"media file {fname!r} is referenced in the fields but is not "
                    "in the collection's media folder; the card will show a broken "
                    "image/sound until the file is added."
                )
    return warnings


# --------------------------------------------------------------------------- #
# Entry points: run the relevant validators, collect warnings from errors.     #
# --------------------------------------------------------------------------- #


def check_create(col: Any, proposal: Any) -> CheckResult:
    """Run every precondition relevant to creating a note. Errors and warnings
    are collected separately (never raised here) so the caller can show them all
    at once; every independent validator runs even if an earlier one failed."""
    result = CheckResult()
    fields = dict(getattr(proposal, "fields", {}) or {})

    _run_deck(col, getattr(proposal, "deck", ""), result)

    model: dict[str, Any] | None = None
    try:
        model = check_fields(col, getattr(proposal, "note_type", "") or "", fields)
    except ContractError as exc:
        result.errors.append(str(exc))
    if model is not None:
        _collect(lambda: check_first_field(model, fields), result)
        _collect(lambda: check_cloze(model, fields), result)

    _run_tags(list(getattr(proposal, "tags", []) or []), result)
    result.warnings.extend(check_media_refs(col, fields))
    return result


def check_edit(col: Any, proposal: Any) -> CheckResult:
    """Run the preconditions relevant to editing an existing note.

    An edit carries only the CHANGED fields, so the exact field-count rule does
    not apply; instead each changed field must be a real field of the note type,
    and the first field (if being edited) must not be blanked. Cloze/full-field
    checks need the whole note and are left to ``check_create``'s path.
    """
    result = CheckResult()
    fields = dict(getattr(proposal, "fields", {}) or {})

    model: dict[str, Any] | None = None
    try:
        model = _resolve_notetype(col, getattr(proposal, "note_type", "") or "")
    except ContractError as exc:
        result.errors.append(str(exc))
    if model is not None:
        valid = _field_names(model)
        unknown = [n for n in fields if n not in valid]
        if unknown:
            result.errors.append(
                f"field(s) {unknown} are not part of note type "
                f"{model.get('name', '?')!r}; its fields are {valid}. Only edit "
                "fields that exist on the note type."
            )
        if valid and valid[0] in fields:
            _collect(lambda: check_first_field(model, fields), result)

    _run_tags(list(getattr(proposal, "add_tags", []) or []), result)
    result.warnings.extend(check_media_refs(col, fields))
    return result


# --------------------------------------------------------------------------- #
# Internal helpers.                                                            #
# --------------------------------------------------------------------------- #


def _run_deck(col: Any, name: str, result: CheckResult) -> None:
    try:
        deck_id = resolve_writable_deck(col, name)
    except ContractError as exc:
        result.errors.append(str(exc))
        return
    result.deck_id = deck_id
    if deck_id is WILL_CREATE:
        result.warnings.append(
            f"deck {(name or '').strip()!r} does not exist yet; it will be created "
            "as a new normal deck."
        )


def _run_tags(tags: list[str], result: CheckResult) -> None:
    try:
        result.tags = check_tags(tags)
    except ContractError as exc:
        result.errors.append(str(exc))


def _collect(check: Any, result: CheckResult) -> None:
    try:
        check()
    except ContractError as exc:
        result.errors.append(str(exc))


def _resolve_notetype(col: Any, name: str) -> dict[str, Any]:
    if not name:
        raise ContractError("no note type was given; name the note type to use.")
    model = col.models.by_name(name)
    if model is None:
        available = sorted(nt.name for nt in col.models.all_names_and_ids())
        raise ContractError(
            f"note type {name!r} does not exist. Available note types: {available}. "
            "Use one of these exactly, or ask the user to create the note type first."
        )
    return model


def _field_names(notetype: dict[str, Any]) -> list[str]:
    return [f["name"] for f in notetype.get("flds", [])]


def _find_deck_id(col: Any, name: str) -> Any:
    try:
        return col.decks.id_for_name(name)
    except Exception:
        return None


def _is_filtered(col: Any, deck_id: Any) -> bool:
    try:
        deck = col.decks.get(deck_id)
    except Exception:
        return False
    return bool(deck and deck.get("dyn"))


def _media_has(col: Any, fname: str) -> bool | None:
    """True/False when the media folder can be inspected, else None (unknown).

    Tries the URL-decoded name too, since fields may percent-encode filenames.
    """
    media = getattr(col, "media", None)
    if media is None:
        return None
    candidates = [fname]
    decoded = unquote(fname)
    if decoded != fname:
        candidates.append(decoded)

    have = getattr(media, "have", None)
    if callable(have):
        try:
            return any(bool(have(c)) for c in candidates)
        except Exception:
            return None
    try:
        media_dir = media.dir()
    except Exception:
        return None
    if not media_dir:
        return None
    return any(os.path.exists(os.path.join(media_dir, c)) for c in candidates)


def _strip_html_preserving_media(text: str) -> str:
    """Approximate rslib ``strip_html_preserving_media_filenames`` (text.rs:366):
    keep media filenames, drop every other tag and comment, unescape entities."""
    text = _MEDIA_TAG_RE.sub(r" \1 ", text)
    text = _COMMENT_RE.sub(" ", text)
    text = _STYLE_SCRIPT_RE.sub(" ", text)
    text = _TAG_RE.sub(" ", text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()
