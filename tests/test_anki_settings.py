"""Profile configuration must be reviewable, scoped, private and reversible."""

from __future__ import annotations

import copy
import json
import unittest
from typing import Any

from chat_with_your_cards.anki_settings import read, write
from chat_with_your_cards.proposals import ProposalError, ProposalManager
from chat_with_your_cards.tools import build_registry


class Profile:
    name = "Synthetic"
    base = "/synthetic"

    def __init__(self) -> None:
        self.profile: dict[str, Any] = {
            "autoSync": True,
            "syncMedia": True,
            "autoSyncMediaMinutes": 15,
            "networkTimeout": 60,
            "customSyncUrl": "",
            "currentSyncUrl": "",
            "syncKey": "",
            "unrelated": "preserved",
        }
        self.persisted = copy.deepcopy(self.profile)
        self.fail_save = False

    def custom_sync_url(self):
        return self.profile["customSyncUrl"]

    def auto_syncing_enabled(self):
        return self.profile["autoSync"]

    def media_syncing_enabled(self):
        return self.profile["syncMedia"]

    def periodic_sync_media_minutes(self):
        return self.profile["autoSyncMediaMinutes"]

    def network_timeout(self):
        return self.profile["networkTimeout"]

    def set_custom_sync_url(self, value):
        if value != self.custom_sync_url():
            self.profile["currentSyncUrl"] = None
            self.profile["customSyncUrl"] = value

    def set_periodic_sync_media_minutes(self, value):
        self.profile["autoSyncMediaMinutes"] = value

    def set_network_timeout(self, value):
        self.profile["networkTimeout"] = value

    def save(self):
        if self.fail_save:
            raise ValueError("synthetic-secret-write-error")
        self.persisted = copy.deepcopy(self.profile)


class AnkiSettingsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.pm = Profile()
        self.config = {"permission_mode": "read-only"}
        self.pushed: list[dict[str, Any]] = []
        self.busy = False
        self.manager = ProposalManager(
            get_col=lambda: self.fail("profile settings must not access the collection"),
            push=self.pushed.append,
            config=self.config,
            after_write=lambda _ids: self.fail("must not invoke collection refresh"),
            read_anki_settings=lambda: read(self.pm),
            write_anki_settings=lambda changes, expected, identity: write(
                self.pm, changes, expected, identity, busy=self.busy
            ),
        )

    def submit(self, **settings):
        return self.manager.submit_set_anki_settings({"settings": settings})

    def test_read_allowlist_and_sensitive_url_redaction(self):
        self.pm.profile.update(
            syncKey="synthetic-account-key",
            customSyncUrl="https://user:synthetic-pass@example.com/?token=synthetic-query",
        )
        snapshot = self.manager.get_anki_settings()
        self.assertTrue(snapshot["sync_signed_in"])
        self.assertNotIn("custom_sync_url", snapshot["settings"])
        for text in ("synthetic-account-key", "synthetic-pass", "synthetic-query", "/synthetic"):
            self.assertNotIn(text, json.dumps(snapshot))
        with self.assertRaises(ProposalError):
            self.submit(custom_sync_url="https://example.org/")
        self.submit(network_timeout_seconds=120)  # unrelated changes remain possible

    def test_tools_available_in_collection_read_only(self):
        names = {spec.name for spec in build_registry().specs(include_writes=False)}
        self.assertIn("get_anki_settings", names)
        self.assertIn("set_anki_settings", names)
        self.assertNotIn("propose_note", names)

    def test_every_mode_reviews_before_applying_and_can_undo(self):
        for mode in ("read-only", "auto-accept", "trusted-writes", "full-collection"):
            self.config["permission_mode"] = mode
            response = self.submit(auto_sync=False, network_timeout_seconds=120)
            self.assertEqual("pending_user_review", response["status"])
            self.assertEqual(60, self.pm.profile["networkTimeout"])
            pid = response["proposal_id"]
            self.assertTrue(self.manager._proposals[pid].requires_confirmation)
            self.manager.accept({"id": pid})
            self.assertEqual(120, self.pm.persisted["networkTimeout"])
            self.assertFalse(self.pm.persisted["autoSync"])
            self.manager.revert({"id": pid})
            self.assertEqual(60, self.pm.persisted["networkTimeout"])
            self.assertTrue(self.pm.persisted["autoSync"])
            self.assertEqual("preserved", self.pm.persisted["unrelated"])

    def test_url_normalizes_proxy_slash_and_resets_cached_endpoint(self):
        self.pm.profile["currentSyncUrl"] = "https://example.org/redirect/"
        result = self.submit(custom_sync_url="https://example.com/anki")
        pid = result["proposal_id"]
        self.manager.accept({"id": pid})
        self.assertEqual("https://example.com/anki/", self.pm.custom_sync_url())
        self.assertIsNone(self.pm.profile["currentSyncUrl"])
        self.manager.revert({"id": pid})
        self.assertEqual("", self.pm.custom_sync_url())

    def test_invalid_values_do_not_change_anything(self):
        before = copy.deepcopy(self.pm.profile)
        for settings in (
            {"sync_key": "x"},
            {"backups.daily": 0},
            {"auto_sync": "false"},
            {"network_timeout_seconds": True},
            {"network_timeout_seconds": 0},
            {"periodic_media_sync_minutes": -1},
            {"custom_sync_url": "file:///tmp/example"},
            {"custom_sync_url": "https://example.com/?token=secret"},
            {"custom_sync_url": "https://user:pass@example.com/"},
            {"custom_sync_url": "http://example.com:bad/"},
            {"custom_sync_url": "https://example.com/path\x00"},
        ):
            with self.subTest(settings=settings), self.assertRaises(ProposalError):
                self.manager.submit_set_anki_settings({"settings": settings})
        self.assertEqual(before, self.pm.profile)

    def test_changed_settings_refuse_stale_accept(self):
        pid = self.submit(network_timeout_seconds=120)["proposal_id"]
        self.pm.profile["networkTimeout"] = 90
        self.assertIsNone(self.manager.accept({"id": pid}))
        self.assertEqual(90, self.pm.profile["networkTimeout"])
        self.assertEqual([], self.manager._ledger)

    def test_profile_switch_blocks_accept_and_forced_undo(self):
        pid = self.submit(auto_sync=False)["proposal_id"]
        self.pm.name = "Other synthetic profile"
        self.assertIsNone(self.manager.accept({"id": pid}))
        self.assertTrue(self.pm.auto_syncing_enabled())
        self.pm.name = "Synthetic"
        self.manager.accept({"id": pid})
        self.pm.name = "Other synthetic profile"
        self.manager.revert({"id": pid, "force": True})
        self.assertFalse(self.pm.auto_syncing_enabled())
        self.assertFalse(self.manager._ledger[-1].undone)

    def test_signed_in_destination_changes_block_both_submit_and_late_accept(self):
        pid = self.submit(custom_sync_url="https://example.com/")["proposal_id"]
        self.pm.profile["syncKey"] = "synthetic-account-key"
        self.assertIsNone(self.manager.accept({"id": pid}))
        self.assertEqual("", self.pm.custom_sync_url())
        with self.assertRaisesRegex(ProposalError, "Sign out"):
            self.submit(custom_sync_url="https://example.com/")
        self.assertNotIn("synthetic-account-key", json.dumps(self.pushed))

    def test_undo_staleness_requires_explicit_force(self):
        pid = self.submit(network_timeout_seconds=120)["proposal_id"]
        self.manager.accept({"id": pid})
        self.pm.profile["networkTimeout"] = 240
        self.manager.revert({"id": pid})
        self.assertEqual(240, self.pm.network_timeout())
        self.assertTrue(self.pushed[-1].get("conflict"))
        self.manager.revert({"id": pid, "force": True})
        self.assertEqual(60, self.pm.network_timeout())

    def test_forced_destination_undo_cannot_reuse_new_account_credentials(self):
        self.pm.profile["customSyncUrl"] = "https://example.com/original"
        pid = self.submit(custom_sync_url="https://example.org/new/")["proposal_id"]
        self.manager.accept({"id": pid})
        self.pm.profile["syncKey"] = "synthetic-new-account-key"
        self.manager.revert({"id": pid, "force": True})
        self.assertEqual("https://example.org/new/", self.pm.custom_sync_url())
        self.assertFalse(self.manager._ledger[-1].undone)
        self.pm.profile["syncKey"] = ""
        self.manager.revert({"id": pid})
        self.assertEqual("https://example.com/original", self.pm.custom_sync_url())

    def test_background_activity_blocks_apply_and_forced_undo(self):
        pid = self.submit(sync_media=False)["proposal_id"]
        self.busy = True
        self.assertIsNone(self.manager.accept({"id": pid}))
        self.assertTrue(self.pm.media_syncing_enabled())
        self.busy = False
        self.manager.accept({"id": pid})
        self.busy = True
        self.manager.revert({"id": pid, "force": True})
        self.assertFalse(self.pm.media_syncing_enabled())

    def test_save_failure_restores_in_memory_values_and_never_echoes_error(self):
        pid = self.submit(auto_sync=False, network_timeout_seconds=120)["proposal_id"]
        before = copy.deepcopy(self.pm.profile)
        self.pm.fail_save = True
        self.manager.accept({"id": pid})
        self.assertEqual(before, self.pm.profile)
        self.assertEqual([], self.manager._ledger)
        self.assertNotIn("synthetic-secret-write-error", json.dumps(self.pushed))

    def test_read_only_manager_without_app_is_honest(self):
        manager = ProposalManager(get_col=lambda: None, push=lambda _: None, config={})
        with self.assertRaises(ProposalError):
            manager.get_anki_settings()


if __name__ == "__main__":
    unittest.main()
