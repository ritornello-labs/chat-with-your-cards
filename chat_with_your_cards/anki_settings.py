"""Allowlisted Anki profile settings, through the same API as Preferences.

No account credentials, collection access, sync requests or arbitrary config keys.
The host calls these functions on Anki's main thread.
"""

from __future__ import annotations

import hashlib
from typing import Any
from urllib.parse import urlsplit, urlunsplit


class SettingsError(ValueError):
    """A fixed, user-readable error that contains no private config values."""


class SettingsConflict(SettingsError):
    """Only a changed value can be overridden by the ledger's Undo anyway."""


SPECS: dict[str, dict[str, Any]] = {
    "custom_sync_url": {
        "title": "Sync server",
        "type": "string",
        "description": "Custom sync server URL; empty means AnkiWeb",
    },
    "auto_sync": {
        "title": "Automatic sync",
        "type": "boolean",
        "description": "Sync automatically when opening/closing this profile",
    },
    "sync_media": {"title": "Sync media", "type": "boolean", "description": "Include media when syncing"},
    "periodic_media_sync_minutes": {
        "title": "Background media sync",
        "type": "integer",
        "minimum": 0,
        "maximum": 1440,
        "description": "Background media sync interval; 0 disables it",
    },
    "network_timeout_seconds": {
        "title": "Network timeout",
        "type": "integer",
        "minimum": 10,
        "maximum": 3600,
        "description": "Sync network timeout in seconds",
    },
}


def _url(value: Any) -> str:
    if not isinstance(value, str) or len(value) > 2048 or any(c.isspace() for c in value):
        raise SettingsError("Use an HTTP(S) sync server URL, or an empty string for AnkiWeb.")
    if not value:
        return ""
    try:
        parts = urlsplit(value)
        valid = parts.scheme in ("http", "https") and bool(parts.hostname)
        _ = parts.port  # validate the port without connecting
        if not valid or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError
        if (
            parts.username is not None
            or parts.password is not None
            or parts.query
            or parts.fragment
        ):
            raise SettingsError(
                "Keep account credentials, query parameters and fragments out of the sync URL. Use Anki Preferences for authentication."
            )
    except SettingsError:
        raise
    except ValueError:
        raise SettingsError(
            "Use a complete HTTP(S) sync server URL with a valid host and port."
        ) from None
    # Anki requires the trailing slash for reverse-proxy subpaths.
    path = parts.path if parts.path.endswith("/") else parts.path + "/"
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def read(pm: Any) -> dict[str, Any]:
    if pm is None or not pm.name or pm.profile is None:
        raise SettingsError("Open an Anki profile before reading its settings.")
    identity = f"{pm.base}\0{pm.name}".encode()
    settings: dict[str, Any] = {
        "auto_sync": bool(pm.auto_syncing_enabled()),
        "sync_media": bool(pm.media_syncing_enabled()),
        "periodic_media_sync_minutes": pm.periodic_sync_media_minutes(),
        "network_timeout_seconds": pm.network_timeout(),
    }
    unavailable: dict[str, str] = {}
    raw_url = pm.custom_sync_url() or ""
    try:
        _url(raw_url)
        # Preserve the exact stored baseline so accepting/reverting can detect
        # edits in Preferences. Only NEW URLs are normalized.
        settings["custom_sync_url"] = raw_url
    except SettingsError:
        unavailable["custom_sync_url"] = (
            "The stored URL contains sensitive or unsupported parts; manage it in Anki Preferences."
        )
    return {
        "scope": "current Anki profile",
        "profile_id": hashlib.sha256(identity).hexdigest(),
        "settings": settings,
        "editable": {key: spec for key, spec in SPECS.items() if key in settings},
        "unavailable": unavailable,
        "sync_signed_in": bool(pm.profile.get("syncKey")),
        "note": "Use set_anki_settings to propose changes. Every change needs review. Account secrets, sync direction and backup retention are excluded; applying settings does not start a sync.",
    }


def normalize(raw: Any, current: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, dict) or not raw:
        raise SettingsError(
            "settings must be a non-empty object; read get_anki_settings for editable names."
        )
    if any(key not in SPECS for key in raw):
        raise SettingsError("Unsupported setting; read get_anki_settings for editable names.")
    changes: dict[str, Any] = {}
    for key, value in raw.items():
        if key not in current["settings"]:
            raise SettingsError(
                "That setting cannot be safely read; manage it in Anki Preferences."
            )
        spec = SPECS[key]
        if key == "custom_sync_url":
            value = _url(value)
        elif spec["type"] == "boolean":
            if not isinstance(value, bool):
                raise SettingsError(f"{key} must be true or false.")
        elif (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not spec["minimum"] <= value <= spec["maximum"]
        ):
            raise SettingsError(
                f"{key} must be a whole number from {spec['minimum']} to {spec['maximum']}."
            )
        if value != current["settings"][key]:
            changes[key] = value
    if not changes:
        raise SettingsError("No effective change; those settings already have these values.")
    check_destination(changes, current)
    return changes


def check_destination(changes: dict[str, Any], current: dict[str, Any]) -> None:
    if (
        "custom_sync_url" in changes
        and changes["custom_sync_url"] != current["settings"].get("custom_sync_url")
        and current["sync_signed_in"]
    ):
        raise SettingsError(
            "Sign out of sync in Anki Preferences before changing sync servers, so the old server's credentials are not sent to the new one. Sign in to the new server in Preferences afterwards."
        )


def write(
    pm: Any,
    changes: dict[str, Any],
    expected: dict[str, Any],
    profile_id: str,
    *,
    busy: bool = False,
) -> None:
    current = read(pm)
    if current["profile_id"] != profile_id:
        raise SettingsError(
            "The active Anki profile changed. Make a new settings proposal for this profile."
        )
    if busy:
        raise SettingsError(
            "Wait for Anki's current sync or background operation to finish before changing profile settings."
        )
    if any(
        key not in current["settings"] or current["settings"][key] != value
        for key, value in expected.items()
    ):
        raise SettingsConflict(
            "Anki settings changed since this proposal was made. Review the current values before overwriting them."
        )
    # Validate again at acceptance. Preserve a valid historical URL verbatim on
    # undo rather than silently changing its spelling/trailing slash.
    normalize(changes, current)
    before = pm.profile.copy()
    try:
        for key, value in changes.items():
            if key == "custom_sync_url":
                pm.set_custom_sync_url(value)
            elif key == "auto_sync":
                pm.profile["autoSync"] = value
            elif key == "sync_media":
                pm.profile["syncMedia"] = value
            elif key == "periodic_media_sync_minutes":
                pm.set_periodic_sync_media_minutes(value)
            elif key == "network_timeout_seconds":
                pm.set_network_timeout(value)
        pm.save()
    except Exception:
        # Keep raw config only in this stack frame, never in proposals/logs.
        pm.profile.clear()
        pm.profile.update(before)
        try:
            pm.save()
        except Exception:
            pass
        raise SettingsError(
            "Could not persist Anki settings; the previous in-memory settings were restored. Reopen Preferences to verify them."
        ) from None
