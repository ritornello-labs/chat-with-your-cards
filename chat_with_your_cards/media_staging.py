"""Staged media for note proposals (workspace task #21, DESIGN.md 8).

The agent (full agent-tools mode) can generate audio - e.g. a TTS mp3 for a
vocabulary card - but a proposal is reviewed asynchronously: the agent's file
sits in some /tmp path that may be gone by the time the user clicks Accept.
So propose_note copies each attachment into a per-proposal staging directory
under user_files/ at PROPOSE time (validated: audio-only, size/count caps),
and the proposal payload carries self-contained data: URIs so the review
card's preview can play the audio without touching the filesystem.

On ACCEPT the staged file is imported through col.media.add_file - Anki's
own API, which de-duplicates and RENAMES on content collision - and every
`[sound:original-name]` marker in the proposal's fields is rewritten to the
final name before the note is created. On reject/supersede the staging
directory is deleted. A startup sweep clears directories left behind by
crashes or never-resolved proposals.

Widened beyond audio (#10, 2026-07-31): images unblock every visual deck
(maps, diagrams, image occlusion) and video rides the same [sound:...]
marker Anki itself uses for it. The kind is derived from the extension.
"""

from __future__ import annotations

import base64
import os
import re
import shutil
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

AUDIO_MIME_BY_EXT = {
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".ogg": "audio/ogg",
    ".opus": "audio/ogg",
    ".m4a": "audio/mp4",
    ".flac": "audio/flac",
}
IMAGE_MIME_BY_EXT = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
    ".avif": "image/avif",
}
VIDEO_MIME_BY_EXT = {
    ".mp4": "video/mp4",
    ".webm": "video/webm",
}
# Documents (#15b): context material for the agent, not card media - note
# proposals reject the kind (a PDF cannot render on a card), composer
# attachments and store_media_asset accept it.
DOCUMENT_MIME_BY_EXT = {
    ".pdf": "application/pdf",
}
MIME_BY_EXT = {
    **AUDIO_MIME_BY_EXT,
    **IMAGE_MIME_BY_EXT,
    **VIDEO_MIME_BY_EXT,
    **DOCUMENT_MIME_BY_EXT,
}


def kind_for_ext(ext: str) -> str | None:
    if ext in AUDIO_MIME_BY_EXT:
        return "audio"
    if ext in IMAGE_MIME_BY_EXT:
        return "image"
    if ext in VIDEO_MIME_BY_EXT:
        return "video"
    if ext in DOCUMENT_MIME_BY_EXT:
        return "document"
    return None
MAX_MEDIA_FILE_BYTES = 8_000_000  # matches show_image's inline budget
MAX_MEDIA_PER_PROPOSAL = 4
SWEEP_MAX_AGE_DAYS = 7

# Anki's sound marker: [sound:filename.mp3] - used for video too.
SOUND_RE = re.compile(r"\[sound:([^\]]+)\]")
# <img src="filename.png"> in any quoting style; group 1 = the filename.
IMG_SRC_RE = re.compile(r"<img\b[^>]*?\bsrc=[\"']?([^\"'>\s]+)", re.I)

# Media filenames must be safe as a bare path component AND inside a
# [sound:...] marker: no separators, no brackets, nothing hidden.
_FILENAME_RE = re.compile(r"^[^/\\\[\]:\0]{1,120}$")


class MediaError(ValueError):
    """Validation failure surfaced to the agent as the tool error."""


@dataclass
class StagedItem:
    id: str
    filename: str
    mime: str
    size: int
    path: Path
    kind: str = "audio"

    def to_payload(self) -> dict[str, Any]:
        with self.path.open("rb") as handle:
            data = base64.b64encode(handle.read()).decode("ascii")
        return {
            "id": self.id,
            "kind": self.kind,
            "name": self.filename,
            "mime": self.mime,
            "bytes": self.size,
            "src": f"data:{self.mime};base64,{data}",
        }


class MediaStaging:
    """Per-proposal staging directories under `root` (user_files/staging)."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def _dir(self, proposal_id: str) -> Path:
        # proposal ids are ProposalManager-generated (p1, p2...), never user
        # or agent input - but be defensive anyway.
        if not re.match(r"^[A-Za-z0-9_-]{1,64}$", proposal_id):
            raise MediaError(f"bad proposal id {proposal_id!r}")
        return self._root / proposal_id

    def stage(
        self,
        proposal_id: str,
        items: list[dict[str, Any]],
        kinds: set[str] | None = None,
    ) -> list[StagedItem]:
        """Validate and copy the agent's files; all-or-nothing. ``kinds``
        restricts which media kinds this call site accepts (note proposals
        exclude documents; the composer takes everything)."""
        if len(items) > MAX_MEDIA_PER_PROPOSAL:
            raise MediaError(
                f"too many media files ({len(items)}); the cap is "
                f"{MAX_MEDIA_PER_PROPOSAL} per proposal"
            )
        staged: list[StagedItem] = []
        target = self._dir(proposal_id)
        try:
            seen_names: set[str] = set()
            for item in items:
                raw_path = str(item.get("path", "")).strip()
                if not raw_path:
                    raise MediaError("each media item needs a `path`")
                source = Path(os.path.expanduser(raw_path))
                if not source.is_file():
                    raise MediaError(f"no file at {source}")
                filename = str(item.get("filename", "")).strip() or source.name
                if not _FILENAME_RE.match(filename):
                    raise MediaError(
                        f"bad media filename {filename!r} (no path separators, "
                        "brackets, or colons; max 120 chars)"
                    )
                ext = os.path.splitext(filename)[1].lower()
                mime = MIME_BY_EXT.get(ext)
                kind = kind_for_ext(ext)
                if mime is None or kind is None:
                    raise MediaError(
                        f"unsupported media type {ext!r} for {filename!r} - "
                        "supported: " + ", ".join(sorted(MIME_BY_EXT))
                    )
                if kinds is not None and kind not in kinds:
                    raise MediaError(
                        f"{kind} files are not allowed here ({filename!r}); "
                        "allowed: " + ", ".join(sorted(kinds))
                    )
                if filename.lower() in seen_names:
                    raise MediaError(f"duplicate media filename {filename!r}")
                seen_names.add(filename.lower())
                size = source.stat().st_size
                if size == 0:
                    raise MediaError(f"{filename!r} is empty")
                if size > MAX_MEDIA_FILE_BYTES:
                    raise MediaError(
                        f"{filename!r} is {size // 1024} KB; the cap is "
                        f"{MAX_MEDIA_FILE_BYTES // 1024} KB per file"
                    )
                target.mkdir(parents=True, exist_ok=True)
                dest = target / filename
                shutil.copyfile(source, dest)
                staged.append(
                    StagedItem(
                        id=f"m-{uuid.uuid4().hex[:8]}",
                        filename=filename,
                        mime=mime,
                        size=size,
                        path=dest,
                        kind=kind,
                    )
                )
        except Exception:
            # All-or-nothing: never leave a half-staged directory behind.
            shutil.rmtree(target, ignore_errors=True)
            raise
        return staged

    def staged_path(self, proposal_id: str, filename: str) -> Path:
        return self._dir(proposal_id) / filename

    def discard(self, proposal_id: str) -> None:
        shutil.rmtree(self._dir(proposal_id), ignore_errors=True)

    def sweep(self, max_age_days: int = SWEEP_MAX_AGE_DAYS) -> int:
        """Remove staging dirs older than `max_age_days` (crashed sessions,
        proposals that never resolved). Returns how many were removed."""
        if not self._root.is_dir():
            return 0
        cutoff = time.time() - max_age_days * 86400
        removed = 0
        for entry in self._root.iterdir():
            try:
                if entry.is_dir() and entry.stat().st_mtime < cutoff:
                    shutil.rmtree(entry, ignore_errors=True)
                    removed += 1
            except OSError:
                continue
        return removed


def sound_markers(fields: dict[str, str]) -> set[str]:
    """Every filename referenced by a [sound:...] marker across fields."""
    found: set[str] = set()
    for value in fields.values():
        for match in SOUND_RE.finditer(value):
            found.add(match.group(1).strip())
    return found


def media_references(fields: dict[str, str]) -> set[str]:
    """Every media filename a field references: [sound:...] (audio AND
    video, Anki's own convention) plus <img src=...>."""
    found = sound_markers(fields)
    for value in fields.values():
        for match in IMG_SRC_RE.finditer(value):
            found.add(match.group(1).strip())
    return found


def rewrite_sound_markers(fields: dict[str, str], renames: dict[str, str]) -> dict[str, str]:
    """Rewrite [sound:old] -> [sound:new] per `renames`, other text untouched."""
    if not renames:
        return fields

    def sub(match: re.Match[str]) -> str:
        name = match.group(1).strip()
        return f"[sound:{renames.get(name, name)}]"

    return {name: SOUND_RE.sub(sub, value) for name, value in fields.items()}


def rewrite_media_markers(fields: dict[str, str], renames: dict[str, str]) -> dict[str, str]:
    """Rewrite [sound:old]->[sound:new] AND <img src="old">-><img src="new">
    per `renames`; everything else untouched."""
    if not renames:
        return fields
    fields = rewrite_sound_markers(fields, renames)

    def sub(match: re.Match[str]) -> str:
        name = match.group(1).strip()
        return match.group(0).replace(match.group(1), renames.get(name, name))

    return {name: IMG_SRC_RE.sub(sub, value) for name, value in fields.items()}
