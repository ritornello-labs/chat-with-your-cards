"""Verify reviewed profile settings through real Anki APIs and the chat UI."""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any, Callable


def run(
    mw: Any, dock: Any, addon: Any, evaluate: Callable[..., Any], wait: Callable[..., Any]
) -> dict[str, Any]:
    from chat_with_your_cards.tools import build_registry

    pm, manager = mw.pm, addon.state.proposals
    original = copy.deepcopy(pm.profile)
    before = (
        mw.col.note_count(),
        mw.col.card_count(),
        mw.col.db.scalar("select count() from revlog"),
    )
    mode = addon.state.config["permission_mode"]
    sync_calls: list[bool] = []
    previous_sync = mw.onSync
    mw.onSync = lambda: sync_calls.append(True)

    def js(code: str) -> Any:
        return evaluate(dock.web, code, 5000, "Anki profile settings proposal")

    try:
        pm.profile.update(
            {
                "autoSync": False,
                "syncMedia": False,
                "autoSyncMediaMinutes": 0,
                "networkTimeout": 60,
                "customSyncUrl": "",
                "currentSyncUrl": None,
                "syncKey": None,
            }
        )
        pm.save()
        addon.state.config["permission_mode"] = "full-collection"
        registry = build_registry()
        ctx = addon._ToolCtx()
        settings = registry.call(ctx, "get_anki_settings", {})
        if settings["scope"] != "current Anki profile" or settings["sync_signed_in"]:
            raise AssertionError("Incorrect settings scope or auth state")
        response = registry.call(
            ctx,
            "set_anki_settings",
            {
                "settings": {
                    "custom_sync_url": "https://example.com/anki",
                    "network_timeout_seconds": 120,
                    "periodic_media_sync_minutes": 30,
                },
                "rationale": "Use the chosen sync destination for this profile.",
            },
        )
        pid = response["proposal_id"]
        if response["status"] != "pending_user_review" or pm.custom_sync_url():
            raise AssertionError("Full collection bypassed settings review")
        selector = f'[data-proposal-card-id="{pid}"]'
        wait(
            lambda: js(f"!!document.querySelector({json.dumps(selector)})"),
            5000,
            "settings proposal",
        )
        text = js(f"document.querySelector({json.dumps(selector)}).textContent")
        if not all(
            part in text
            for part in (
                "Anki profile settings",
                "AnkiWeb (default)",
                "https://example.com/anki/",
                "120",
            )
        ):
            raise AssertionError("Profile settings diff was not rendered")
        screenshot_dir = os.environ.get("CWYC_MCP_SCREENSHOT_DIR")
        if screenshot_dir:
            js(
                f"document.querySelector({json.dumps(selector)}).scrollIntoView({{block:'center'}}); true;"
            )
            mw.grab().save(str(Path(screenshot_dir) / "anki-settings-proposal.png"), "PNG")
        js(
            f"document.querySelector({json.dumps(selector + ' [data-testid=proposal-approve]')}).click(); true;"
        )
        wait(lambda: manager._proposals[pid].status == "accepted", 5000, "settings approval")
        if pm.custom_sync_url() != "https://example.com/anki/" or pm.network_timeout() != 120:
            raise AssertionError("Approved profile settings did not apply")
        if pm.periodic_sync_media_minutes() != 30:
            raise AssertionError("Periodic media setting did not apply")
        # Read back the persisted synthetic profile using Anki's decoder.
        saved = pm._unpickle(pm.db.scalar("select data from profiles where name = ?", pm.name))
        if (
            saved.get("networkTimeout") != 120
            or saved.get("customSyncUrl") != "https://example.com/anki/"
        ):
            raise AssertionError("Approved settings were not persisted")
        manager.revert({"id": pid})
        if (
            pm.custom_sync_url()
            or pm.network_timeout() != 60
            or pm.periodic_sync_media_minutes() != 0
        ):
            raise AssertionError("Undo did not restore profile settings")

        stale = registry.call(
            ctx, "set_anki_settings", {"settings": {"network_timeout_seconds": 90}}
        )["proposal_id"]
        pm.set_network_timeout(75)
        manager.accept({"id": stale})
        if pm.network_timeout() != 75 or manager._proposals[stale].status != "pending":
            raise AssertionError("Stale proposal overwrote a manual settings change")
        manager.reject({"id": stale})

        late = registry.call(
            ctx, "set_anki_settings", {"settings": {"custom_sync_url": "https://example.org/"}}
        )["proposal_id"]
        pm.profile["syncKey"] = "synthetic-native-account-key"
        manager.accept({"id": late})
        if pm.custom_sync_url() or manager._proposals[late].status != "pending":
            raise AssertionError("Destination changed after the user signed in")
        if "synthetic-native-account-key" in json.dumps(
            registry.call(ctx, "get_anki_settings", {})
        ):
            raise AssertionError("Account credential was exposed")
        manager.reject({"id": late})
        if before != (
            mw.col.note_count(),
            mw.col.card_count(),
            mw.col.db.scalar("select count() from revlog"),
        ):
            raise AssertionError("Profile settings changed notes/cards/reviews")
        if sync_calls:
            raise AssertionError("Profile settings started sync")
        return {
            "review_required": True,
            "ui_diff": True,
            "native_apply_and_persist": True,
            "undo": True,
            "stale_refused": True,
            "signed_in_refused": True,
            "secrets_hidden": True,
            "collection_unchanged": True,
            "no_sync": True,
        }
    finally:
        mw.onSync = previous_sync
        pm.profile.clear()
        pm.profile.update(original)
        pm.save()
        addon.state.config["permission_mode"] = mode
