"""CODEX: Persist per-user response guards without limiting the administrator."""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from project_layout import configure_imports
configure_imports(__file__)

from codex_lan.store import Store


class ResponseLimitsStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name)
        self.store = Store(self.path)
        self.admin = self.store.bootstrap_admin("Host")
        self.client = self.store.enroll("Client")
        self.pending = self.store.enroll("Pending")
        self.admin_id = self.admin["device"]["id"]
        self.client_id = self.client["device"]["id"]
        self.store.set_device(self.admin_id, self.client_id, status="approved")

    def tearDown(self):
        self.store.close()
        self.temporary.cleanup()

    def reopen(self):
        self.store.close()
        self.store = Store(self.path)

    def test_new_devices_expose_boolean_defaults_on_all_public_paths(self):
        for enrollment, expected in ((self.admin, False), (self.client, True), (self.pending, True)):
            with self.subTest(device=enrollment["device"]["name"]):
                self.assertIs(enrollment["device"]["response_limits_enabled"], expected)
                self.assertIs(self.store.get_device(enrollment["device"]["id"])["response_limits_enabled"], expected)
                self.assertIs(self.store.authenticate(enrollment["token"], allow_pending=True)["response_limits_enabled"], expected)
        for device in self.store.list_devices(self.admin_id):
            self.assertIs(device["response_limits_enabled"], not device["is_admin"])
        self.assertEqual(self.store.db.execute(
            "SELECT response_limits_enabled FROM devices WHERE id=?", (self.admin_id,)
        ).fetchone()[0], 0)

    def test_admin_can_toggle_client_setting_without_changing_other_limits(self):
        self.store.set_device(
            self.admin_id, self.client_id, max_concurrent=3,
            token_budget=12345, weekly_share_percent=12.5,
        )
        for enabled in (False, True, False):
            with self.subTest(enabled=enabled):
                changed = self.store.set_device(
                    self.admin_id, self.client_id, response_limits_enabled=enabled
                )
                self.assertIs(changed["response_limits_enabled"], enabled)
                self.assertEqual(changed["max_concurrent"], 3)
                self.assertEqual(changed["token_budget"], 12345)
                self.assertEqual(changed["weekly_share_percent"], 12.5)
                self.reopen()
                self.assertIs(self.store.get_device(self.client_id)["response_limits_enabled"], enabled)
        unchanged = self.store.set_device(self.admin_id, self.client_id, max_concurrent=2)
        self.assertIs(unchanged["response_limits_enabled"], False)

    def test_invalid_booleans_reject_the_entire_update(self):
        before = self.store.get_device(self.client_id)
        for invalid in (None, 0, 1, 0.0, 1.0, "true", "false", "", [], {}):
            with self.subTest(invalid=invalid):
                with self.assertRaisesRegex(ValueError, "response limits"):
                    self.store.set_device(
                        self.admin_id, self.client_id,
                        max_concurrent=2, response_limits_enabled=invalid,
                    )
                self.assertEqual(self.store.get_device(self.client_id), before)

    def test_only_approved_admin_can_change_response_limits(self):
        revoked = self.store.enroll("Revoked")["device"]["id"]
        self.store.set_device(self.admin_id, revoked, status="revoked")
        for actor in (self.client_id, self.pending["device"]["id"], revoked, "missing"):
            for target in (self.client_id, self.admin_id):
                with self.subTest(actor=actor, target=target):
                    before = self.store.get_device(target)
                    with self.assertRaises(PermissionError):
                        self.store.set_device(actor, target, response_limits_enabled=False)
                    self.assertEqual(self.store.get_device(target), before)

    def test_admin_exemption_cannot_be_enabled_and_is_normalized_on_read(self):
        before = self.store.get_device(self.admin_id)
        with self.assertRaisesRegex(ValueError, "administrator"):
            self.store.set_device(
                self.admin_id, self.admin_id,
                max_concurrent=2, response_limits_enabled=True,
            )
        self.assertEqual(self.store.get_device(self.admin_id), before)
        self.assertIs(self.store.set_device(
            self.admin_id, self.admin_id, response_limits_enabled=False,
        )["response_limits_enabled"], False)

        # Defensive normalization also keeps the exemption if old/external data
        # carries a value inconsistent with the administrator's fixed policy.
        with self.store.db:
            self.store.db.execute(
                "UPDATE devices SET response_limits_enabled=1 WHERE id=?", (self.admin_id,)
            )
        self.assertIs(self.store.get_device(self.admin_id)["response_limits_enabled"], False)
        self.assertIs(self.store.authenticate(self.admin["token"])["response_limits_enabled"], False)
        admin = next(device for device in self.store.list_devices(self.admin_id) if device["is_admin"])
        self.assertIs(admin["response_limits_enabled"], False)
        self.reopen()
        self.assertEqual(self.store.db.execute(
            "SELECT response_limits_enabled FROM devices WHERE id=?", (self.admin_id,)
        ).fetchone()[0], 0)

    def test_existing_database_migration_preserves_history_credentials_and_quota(self):
        self.store.set_device(
            self.admin_id, self.client_id, max_concurrent=2,
            token_budget=12345, weekly_share_percent=8,
        )
        self.store.set_browser_login(self.client_id, "client-login", "correct horse battery")
        self.store.issue_device_credential(self.client_id)
        room = self.store.create_room(self.client_id, "Saved conversation", "/work")
        run = self.store.create_run(self.client_id, room["id"], "Question", [], "saved-request")["run"]
        self.store.record_usage(run["id"], 100, 20, 10)
        self.store.finish_run(run["id"], "completed", "Saved answer")
        tables = (
            "rooms", "runs", "messages", "events", "usage_ledger", "usage_daily",
            "quota_usage", "quota_deltas", "quota_state", "browser_logins", "device_credentials",
        )
        saved = {
            table: [dict(row) for row in self.store.db.execute(f"SELECT * FROM {table} ORDER BY rowid")]
            for table in tables
        }
        saved_devices = [dict(row) for row in self.store.db.execute("SELECT * FROM devices ORDER BY id")]
        for device in saved_devices:
            device.pop("response_limits_enabled")
        self.store.close()
        with sqlite3.connect(self.path / "server.sqlite3") as database:
            database.execute("ALTER TABLE devices DROP COLUMN response_limits_enabled")
        self.store = Store(self.path)

        for _ in range(2):
            for table, expected in saved.items():
                with self.subTest(table=table):
                    self.assertEqual(
                        [dict(row) for row in self.store.db.execute(f"SELECT * FROM {table} ORDER BY rowid")],
                        expected,
                    )
            actual_devices = [dict(row) for row in self.store.db.execute("SELECT * FROM devices ORDER BY id")]
            for device in actual_devices:
                self.assertEqual(device.pop("response_limits_enabled"), int(not device["is_admin"]))
            self.assertEqual(actual_devices, saved_devices)
            for device in self.store.list_devices(self.admin_id):
                self.assertIs(device["response_limits_enabled"], not device["is_admin"])
            self.reopen()

        self.store.set_device(self.admin_id, self.client_id, response_limits_enabled=False)
        self.reopen()
        self.assertIs(self.store.get_device(self.client_id)["response_limits_enabled"], False)
        self.assertIs(self.store.get_device(self.pending["device"]["id"])["response_limits_enabled"], True)


if __name__ == "__main__":
    unittest.main()
