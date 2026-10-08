"""CODEX: Central authorization, reconnect persistence, and accounting regressions."""

from __future__ import annotations

import concurrent.futures
import json
import os
import stat
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from project_layout import configure_imports, source_file
configure_imports(__file__)

from codex_lan.store import DeviceBusyError, RoomBusyError, Store, title_from_first_prompt


class PrivateStoreStateTests(unittest.TestCase):
    def test_existing_public_directory_and_database_are_tightened_before_open(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "server"
            previous_umask = os.umask(0o022)
            try:
                path.mkdir(mode=0o755)
                store = Store(path)
                try:
                    self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700)
                    store.bootstrap_admin("Test host")
                    for name in ("server.sqlite3", "server.sqlite3-wal", "server.sqlite3-shm"):
                        self.assertEqual(stat.S_IMODE((path / name).stat().st_mode), 0o600)
                finally:
                    store.close()
            finally:
                os.umask(previous_umask)

            (path / "server.sqlite3").chmod(0o644)
            path.chmod(0o755)
            store = Store(path)
            try:
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700)
                self.assertEqual(stat.S_IMODE(store.db_path.stat().st_mode), 0o600)
                self.assertEqual(len(store.list_devices(store.db.execute(
                    "SELECT id FROM devices WHERE is_admin=1"
                ).fetchone()[0])), 1)
            finally:
                store.close()

    def test_existing_wal_and_shm_modes_are_tightened_without_losing_data(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            first = Store(path)
            admin = first.bootstrap_admin("Test host")
            try:
                for name in ("server.sqlite3", "server.sqlite3-wal", "server.sqlite3-shm"):
                    (path / name).chmod(0o644)
                second = Store(path)
                try:
                    self.assertEqual(second.authenticate(admin["token"])["id"], admin["device"]["id"])
                    for name in ("server.sqlite3", "server.sqlite3-wal", "server.sqlite3-shm"):
                        self.assertEqual(stat.S_IMODE((path / name).stat().st_mode), 0o600)
                finally:
                    second.close()
            finally:
                first.close()

    def test_state_directory_and_ancestor_symlinks_are_rejected_without_chmod(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target"
            target.mkdir()
            target.chmod(0o755)
            linked = root / "linked"
            linked.symlink_to(target, target_is_directory=True)
            for path in (linked, linked / "child"):
                with self.subTest(path=path), self.assertRaises(ValueError):
                    Store(path)
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o755)
            self.assertFalse((target / "child").exists())

    def test_database_sidecar_symlinks_and_hardlinks_are_rejected(self):
        for name in ("server.sqlite3", "server.sqlite3-wal", "server.sqlite3-shm", "server.sqlite3-journal"):
            for kind in ("symlink", "hardlink"):
                with self.subTest(name=name, kind=kind), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    target = root / "other-private-file"
                    target.write_bytes(b"must not be opened as database state")
                    target.chmod(0o644)
                    state = root / "server"
                    state.mkdir()
                    if kind == "symlink":
                        (state / name).symlink_to(target)
                    else:
                        os.link(target, state / name)
                    with self.assertRaises(ValueError):
                        Store(state)
                    self.assertEqual(target.read_bytes(), b"must not be opened as database state")
                    self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o644)

    def test_database_fifo_is_rejected_without_waiting_for_a_writer(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            os.mkfifo(path / "server.sqlite3", mode=0o600)
            with self.assertRaises(ValueError):
                Store(path)


class LanStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)
        self.store = Store(self.path)
        self.admin = self.store.bootstrap_admin("Server .56")
        self.a = self.store.enroll("Client .57")
        self.b = self.store.enroll("Client .58")
        self.admin_id = self.admin["device"]["id"]
        self.a_id, self.b_id = self.a["device"]["id"], self.b["device"]["id"]

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def approve(self):
        for device_id in (self.a_id, self.b_id):
            self.store.set_device(self.admin_id, device_id, status="approved")

    def room(self, device_id=None, title="Private room"):
        return self.store.create_room(device_id or self.a_id, title, "/home/client/project")

    def make_run(self, room, request_id="request-1", text="hello", attachments=None):
        return self.store.create_run(room["owner_id"], room["id"], text, attachments or [], request_id)

    def test_failed_message_carries_status_beyond_recent_run_window(self):
        self.approve()
        room = self.room()
        first = self.make_run(room)
        run_id = first["run"]["id"]
        error = "Codex app-server disconnected."
        self.store.finish_run(run_id, "failed", "", error)
        for index in range(101):
            created = self.make_run(room, request_id=f"later-{index}")
            self.store.finish_run(created["run"]["id"], "completed", "done")
        self.assertNotIn(run_id, {run["id"] for run in self.store.list_runs(self.a_id, room["id"])})
        messages = self.store.list_messages(self.a_id, room["id"], limit=500)
        user, assistant = messages[:2]
        self.assertNotIn("run_error", user)
        self.assertEqual(assistant["run_status"], "failed")
        self.assertEqual(assistant["run_error"], error)
        self.assertEqual(assistant["content"], "")

    def test_enrollment_requires_explicit_admin_approval(self):
        self.assertEqual(self.a["device"]["status"], "pending")
        self.assertEqual(self.admin["device"]["role"], "admin")
        self.assertEqual(self.a["device"]["role"], "client")
        self.assertEqual(self.store.authenticate(self.a["token"], allow_pending=True)["id"], self.a_id)
        with self.assertRaises(PermissionError):
            self.store.authenticate(self.a["token"])
        for action in (
            lambda: self.store.list_rooms(self.a_id),
            lambda: self.room(),
            lambda: self.store.list_devices(self.a_id),
            lambda: self.store.set_device(self.a_id, self.a_id, status="approved"),
        ):
            with self.assertRaises(PermissionError):
                action()
        self.store.set_device(self.admin_id, self.a_id, status="approved")
        self.assertEqual(self.store.authenticate(self.a["token"])["status"], "approved")
        self.assertTrue(self.room())

    def test_single_admin_and_credentials_are_not_stored_or_returned(self):
        with self.assertRaises(ValueError):
            self.store.bootstrap_admin("Second admin")
        devices = self.store.list_devices(self.admin_id)
        self.assertEqual(len(devices), 3)
        self.assertNotIn("token_hash", json.dumps(devices))
        code = self.a["device"]["verification_code"]
        self.assertRegex(code, r"^[A-F0-9]{4}-[A-F0-9]{4}$")
        self.assertEqual(self.store.get_device(self.a_id)["verification_code"], code)
        self.assertNotIn(self.a["token"], self.store.db.execute("SELECT token_hash FROM devices WHERE id=?", (self.a_id,)).fetchone()[0])
        for token in ("", "unknown", None, self.a["token"][:-1]):
            with self.assertRaises(PermissionError):
                self.store.authenticate(token)
        with self.assertRaises(ValueError):
            self.store.set_device(self.admin_id, self.admin_id, status="revoked")

    def test_device_deletion_is_admin_only_atomic_and_removes_dependent_data(self):
        self.approve()
        room = self.room()
        attachment = self.store.add_attachment(
            self.a_id, "private.png", "image/png", 8, str(self.path / "uploads" / "private")
        )
        created = self.make_run(room, attachments=[attachment["id"]])
        run_id = created["run"]["id"]
        receipt, _created = self.store.prepare_steering(
            self.a_id, room["id"], run_id, "follow up", [], "follow-up-1", False
        )
        self.store.finish_steering(receipt["id"], "accepted")
        self.store.append_event(run_id, "delta", {"text": "answer"})
        self.store.record_usage(run_id, 100, 20, 40)
        connector_token = self.store.issue_device_credential(self.a_id)
        self.store.set_browser_login(self.a_id, "client-a", "correct horse battery")
        browser_token, _lifetime = self.store.create_browser_session(self.a_id, remember=True)
        with self.store.db:
            self.store.db.execute(
                "INSERT INTO client_maintenance(device_id,updated_at) VALUES(?,?)", (self.a_id, 1.0)
            )
            self.store.db.execute(
                "INSERT INTO quota_unknown_acknowledgements(run_id,owner_id,acknowledged_by,acknowledged_at) "
                "VALUES(?,?,?,?)", (run_id, self.a_id, self.admin_id, 1.0)
            )
            observed_at = time.time() + 1
            self.store.quota.save({
                "limit_id": "codex", "starts_at": observed_at - 100,
                "ends_at": observed_at + 604800, "used_percent": 7.5,
                "observed_at": observed_at, "baseline_percent": 1.0,
                "baseline_event_id": 0, "capacity_credits_micros": 12345,
            })

        with self.assertRaises(PermissionError):
            self.store.delete_device(self.b_id, self.a_id)
        with self.assertRaises(ValueError):
            self.store.delete_device(self.admin_id, self.admin_id)
        with self.assertRaises(DeviceBusyError):
            self.store.delete_device(self.admin_id, self.a_id)
        self.assertEqual(self.store.get_device(self.a_id)["id"], self.a_id)

        self.store.finish_run(run_id, "completed", "done")
        result = self.store.delete_device(self.admin_id, self.a_id)
        self.assertEqual(result["device_id"], self.a_id)
        self.assertEqual(result["attachment_paths"], [attachment["path"]])
        for table in (
            "devices", "browser_logins", "browser_sessions", "device_credentials",
            "client_maintenance", "rooms", "attachments", "runs", "messages", "events",
            "artifact_deliveries",
            "usage_ledger", "usage_daily", "steering_receipts", "quota_usage", "quota_deltas",
            "quota_unknown_acknowledgements",
        ):
            columns = {row["name"] for row in self.store.db.execute(f"PRAGMA table_info({table})")}
            clauses = []
            parameters = []
            for column in ("id", "device_id", "owner_id", "run_id", "acknowledged_by"):
                if column in columns:
                    clauses.append(f"{column} IN (?,?,?)")
                    parameters.extend((self.a_id, room["id"], run_id))
            count = self.store.db.execute(
                f"SELECT COUNT(*) FROM {table} WHERE " + " OR ".join(clauses), parameters
            ).fetchone()[0]
            self.assertEqual(count, 0, table)
        self.assertEqual(self.store.get_device(self.b_id)["id"], self.b_id)
        quota_state = self.store.quota.state()
        self.assertEqual(quota_state["capacity_credits_micros"], 12345)
        self.assertEqual(quota_state["baseline_percent"], 7.5)
        self.assertEqual(quota_state["baseline_event_id"], 0)
        for token, authenticate in (
            (self.a["token"], self.store.authenticate),
            (connector_token, self.store.authenticate),
            (browser_token, self.store.authenticate_browser_session),
        ):
            with self.assertRaises(PermissionError, msg=token[:4]):
                authenticate(token)
        self.assertEqual(list(self.store.db.execute("PRAGMA foreign_key_check")), [])

    def test_rejected_or_revoked_tokens_cannot_use_pending_exception(self):
        self.approve()
        room = self.room()
        created = self.make_run(room)
        self.store.set_device(self.admin_id, self.a_id, status="revoked")
        for action in (
            lambda: self.store.authenticate(self.a["token"]),
            lambda: self.store.authenticate(self.a["token"], allow_pending=True),
            lambda: self.store.get_room(self.a_id, room["id"]),
            lambda: self.store.get_run(self.a_id, created["run"]["id"]),
            lambda: self.store.events(self.a_id, room["id"]),
            lambda: self.store.create_run(self.a_id, room["id"], "hello", [], "request-1"),
        ):
            with self.assertRaises(PermissionError):
                action()
        self.assertEqual(self.store.get_run_internal(created["run"]["id"])["status"], "queued")
        self.store.finish_run(created["run"]["id"], "cancelled", "Stopped by administrator")
        self.store.set_device(self.admin_id, self.a_id, status="approved")
        self.assertEqual(self.store.authenticate(self.a["token"])["id"], self.a_id)
        self.store.set_device(self.admin_id, self.b_id, status="rejected")
        with self.assertRaises(PermissionError):
            self.store.authenticate(self.b["token"], allow_pending=True)

    def test_private_room_ids_messages_runs_and_events_fail_closed(self):
        self.approve()
        room_a, room_b = self.room(), self.room(self.b_id)
        run_a = self.make_run(room_a)
        self.store.append_event(run_a["run"]["id"], "answer/delta", {"text": "secret"})
        own_rooms = self.store.list_rooms(self.a_id)
        admin_rooms = self.store.list_rooms(self.admin_id)
        self.assertEqual([row["id"] for row in own_rooms], [room_a["id"]])
        self.assertEqual(own_rooms[0]["owner_name"], "Client .57")
        self.assertEqual({row["id"] for row in admin_rooms}, {room_a["id"], room_b["id"]})
        self.assertEqual({row["owner_name"] for row in admin_rooms}, {"Client .57", "Client .58"})
        for target in (room_a["id"], "unknown-room"):
            for action in (
                lambda target=target: self.store.get_room(self.b_id, target),
                lambda target=target: self.store.list_messages(self.b_id, target),
                lambda target=target: self.store.list_runs(self.b_id, target),
                lambda target=target: self.store.events(self.b_id, target),
                lambda target=target: self.store.update_room(self.b_id, target, title="stolen"),
                lambda target=target: self.store.delete_room(self.b_id, target),
                lambda target=target: self.store.create_run(self.b_id, target, "inject", [], "evil"),
            ):
                with self.assertRaises(PermissionError):
                    action()
        with self.assertRaises(PermissionError):
            self.store.get_run(self.b_id, run_a["run"]["id"])
        self.assertEqual(self.store.list_messages(self.admin_id, room_a["id"])[0]["content"], "hello")
        with self.assertRaises(PermissionError):
            self.store.create_run(self.admin_id, room_a["id"], "admin remote execution", [], "admin-run")
        with self.assertRaises(PermissionError):
            self.store.usage(self.a_id)

    def test_side_room_parent_and_attachment_ownership(self):
        self.approve()
        room_a, room_b = self.room(), self.room(self.b_id)
        image_a = self.store.add_attachment(self.a_id, "photo.png", "image/png", 10, "/server/a.png")
        image_b = self.store.add_attachment(self.b_id, "photo.png", "image/png", 10, "/server/b.png")
        for device_id in (self.b_id, self.admin_id):
            with self.assertRaises(PermissionError):
                self.store.create_room(device_id, "branch", "/tmp/work", parent_room_id=room_a["id"], reply_quote="secret")
        with self.assertRaises(ValueError):
            self.store.create_room(self.a_id, "branch", "/tmp/work", parent_room_id=room_a["id"])
        branch = self.store.create_room(self.a_id, "branch", "/tmp/work", parent_room_id=room_a["id"], reply_quote="selected text")
        self.assertEqual(branch["parent_room_id"], room_a["id"])
        for attachment_id in (image_b["id"], "unknown"):
            with self.assertRaises(PermissionError):
                self.store.get_attachment(self.a_id, attachment_id)
            with self.assertRaises(PermissionError):
                self.make_run(room_a, attachments=[attachment_id])
        self.assertEqual(self.store.get_attachment(self.admin_id, image_a["id"])["owner_id"], self.a_id)
        created = self.make_run(room_a, attachments=[image_a["id"]])
        self.assertEqual(created["user_message"]["attachment_ids"], [image_a["id"]])
        self.assertEqual(self.store.list_messages(self.b_id, room_b["id"]), [])

    def test_attachment_quota_includes_incoming_bytes_and_serializes_writers(self):
        self.approve()
        with mock.patch("codex_lan.store.MAX_ATTACHMENT_STORAGE_BYTES", 10):
            self.store.add_attachment(self.a_id, "first.png", "image/png", 4, "/server/first.png")
            second_store = Store(self.path)

            def add_competing(index):
                try:
                    (self.store if index % 2 else second_store).add_attachment(
                        self.a_id, f"competing-{index}.png", "image/png", 6,
                        f"/server/competing-{index}.png",
                    )
                except ValueError:
                    return False
                return True

            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                accepted = list(pool.map(add_competing, range(8)))

            self.assertEqual(sum(accepted), 1)
            self.assertEqual(
                self.store.db.execute(
                    "SELECT SUM(size) FROM attachments WHERE owner_id=?", (self.a_id,)
                ).fetchone()[0],
                10,
            )
            with self.assertRaisesRegex(ValueError, "storage is full"):
                self.store.add_attachment(
                    self.a_id, "over.png", "image/png", 1, "/server/over.png"
                )
            second_store.close()

    def test_attachment_count_quota_bounds_empty_and_tiny_files_per_device(self):
        self.approve()
        with mock.patch("codex_lan.store.MAX_ATTACHMENT_COUNT", 2):
            empty = self.store.add_attachment(
                self.a_id, "empty.txt", "application/octet-stream", 0, "/server/empty.txt"
            )
            self.store.add_attachment(
                self.a_id, "tiny.txt", "application/octet-stream", 1, "/server/tiny.txt"
            )
            with self.assertRaisesRegex(ValueError, "file-count limit"):
                self.store.add_attachment(
                    self.a_id, "extra.txt", "application/octet-stream", 0, "/server/extra.txt"
                )
            self.store.add_attachment(
                self.b_id, "other.txt", "application/octet-stream", 0, "/server/other.txt"
            )
            self.assertTrue(self.store.discard_attachment(self.a_id, empty["id"])["deleted"])
            self.store.add_attachment(
                self.a_id, "replacement.txt", "application/octet-stream", 0, "/server/replacement.txt"
            )
            self.assertEqual(self.store.db.execute(
                "SELECT COUNT(*) FROM attachments WHERE owner_id=?", (self.a_id,)
            ).fetchone()[0], 2)

    def test_attachment_count_quota_serializes_competing_store_writers(self):
        self.approve()
        second_store = Store(self.path)
        try:
            with mock.patch("codex_lan.store.MAX_ATTACHMENT_COUNT", 2):
                self.store.add_attachment(
                    self.a_id, "first.txt", "application/octet-stream", 0, "/server/first.txt"
                )

                def add_competing(index):
                    try:
                        (self.store if index % 2 else second_store).add_attachment(
                            self.a_id, f"empty-{index}.txt", "application/octet-stream", 0,
                            f"/server/empty-{index}.txt",
                        )
                    except ValueError:
                        return False
                    return True

                with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                    accepted = list(pool.map(add_competing, range(8)))
                self.assertEqual(sum(accepted), 1)
                self.assertEqual(self.store.db.execute(
                    "SELECT COUNT(*) FROM attachments WHERE owner_id=?", (self.a_id,)
                ).fetchone()[0], 2)
        finally:
            second_store.close()

    def test_generated_artifact_delivery_intent_is_committed_persistent_and_idempotent(self):
        self.approve()
        room = self.room()
        attachment = self.store.add_attachment(
            self.a_id, "generated.png", "image/png", 8, "/server/generated.png"
        )
        created = self.make_run(room)
        thread_id = "a6ea9433-896d-4025-9d4f-f4969a4c7689"
        self.store.update_run(created["run"]["id"], thread_id=thread_id)

        self.assertEqual(self.store.pending_artifact_deliveries(self.a_id), [])
        self.store.finish_run(
            created["run"]["id"], "completed", "done", attachment_ids=[attachment["id"]]
        )
        pending = self.store.pending_artifact_deliveries(self.a_id)
        self.assertEqual([row["attachment_id"] for row in pending], [attachment["id"]])
        self.assertEqual(pending[0]["status"], "pending")
        self.assertTrue(self.store.defer_artifact_delivery(
            self.a_id, thread_id, attachment["id"], 0
        ))
        self.assertFalse(self.store.defer_artifact_delivery(
            self.a_id, thread_id, attachment["id"], 0
        ))
        self.assertEqual(
            self.store.pending_artifact_deliveries(self.a_id)[0]["attempts"], 1
        )

        self.store.close()
        self.store = Store(self.path)
        self.assertEqual(
            [row["attachment_id"] for row in self.store.pending_artifact_deliveries(self.a_id)],
            [attachment["id"]],
        )
        digest = "a" * 64
        prepared = self.store.prepare_artifact_delivery(
            self.a_id, thread_id, attachment["id"], digest
        )
        self.assertEqual(prepared["attempts"], 2)
        with self.assertRaisesRegex(ValueError, "digest does not match"):
            self.store.complete_artifact_delivery(
                self.a_id, thread_id, attachment["id"], "b" * 64
            )
        delivered = self.store.complete_artifact_delivery(
            self.a_id, thread_id, attachment["id"], digest
        )
        self.assertEqual(delivered["status"], "delivered")
        self.assertEqual(self.store.pending_artifact_deliveries(self.a_id), [])
        repeated = self.store.prepare_artifact_delivery(
            self.a_id, thread_id, attachment["id"], digest
        )
        self.assertEqual(repeated["attempts"], 2)
        self.assertEqual(
            self.store.complete_artifact_delivery(
                self.a_id, thread_id, attachment["id"], digest
            )["status"],
            "delivered",
        )
        with self.assertRaisesRegex(ValueError, "content changed"):
            self.store.prepare_artifact_delivery(
                self.a_id, thread_id, attachment["id"], "b" * 64
            )

    def test_room_deletion_collects_exclusive_attachments_but_keeps_shared_ones(self):
        self.approve()
        first_room, second_room = self.room(), self.room()
        shared = self.store.add_attachment(
            self.a_id, "shared.png", "image/png", 1, "/server/shared.png"
        )
        exclusive = self.store.add_attachment(
            self.a_id, "exclusive.png", "image/png", 1, "/server/exclusive.png"
        )
        first = self.make_run(first_room, attachments=[shared["id"], exclusive["id"]])
        self.store.finish_run(first["run"]["id"], "completed", "done")
        second = self.make_run(second_room, request_id="second-room", attachments=[shared["id"]])
        self.store.finish_run(second["run"]["id"], "completed", "done")

        result = self.store.delete_room_with_cleanup(self.a_id, first_room["id"])

        self.assertEqual(result["attachment_paths"], [exclusive["path"]])
        self.assertEqual(result["run_ids"], [first["run"]["id"]])
        self.assertEqual(self.store.get_attachment(self.a_id, shared["id"])["id"], shared["id"])
        with self.assertRaises(PermissionError):
            self.store.get_attachment(self.a_id, exclusive["id"])

    def test_attachment_pruning_preserves_pending_steering_then_releases_rejected(self):
        self.approve()
        room = self.room()
        attachment = self.store.add_attachment(
            self.a_id, "follow-up.png", "image/png", 1, "/server/follow-up.png"
        )
        created = self.make_run(room)
        receipt, _created = self.store.prepare_steering(
            self.a_id, room["id"], created["run"]["id"], "use this",
            [attachment["id"]], "attachment-follow-up", False,
        )
        with self.store.db:
            self.store.db.execute(
                "UPDATE attachments SET created_at=? WHERE id=?", (1.0, attachment["id"])
            )

        self.assertEqual(self.store.prune_unreferenced_attachments(2.0), [])
        self.assertEqual(
            self.store.get_attachment(self.a_id, attachment["id"])["id"], attachment["id"]
        )

        self.store.finish_steering(receipt["id"], "rejected", "not delivered")
        self.assertEqual(
            self.store.prune_unreferenced_attachments(2.0), [attachment["path"]]
        )
        with self.assertRaises(PermissionError):
            self.store.get_attachment(self.a_id, attachment["id"])

    def test_attachment_discard_and_send_are_serialized_without_dangling_references(self):
        self.approve()
        room = self.room()
        for index in range(12):
            attachment = self.store.add_attachment(
                self.a_id, f"race-{index}.png", "image/png", 1,
                f"/server/race-{index}.png",
            )
            barrier = threading.Barrier(2)

            def send():
                barrier.wait()
                try:
                    return self.store.create_run(
                        self.a_id, room["id"], "race", [attachment["id"]], f"race-{index}"
                    )
                except PermissionError:
                    return None

            def discard():
                barrier.wait()
                return self.store.discard_attachment(self.a_id, attachment["id"])

            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                sent_future = pool.submit(send)
                discarded_future = pool.submit(discard)
                sent = sent_future.result()
                discarded = discarded_future.result()

            self.assertNotEqual(sent is not None, discarded["deleted"])
            if sent is not None:
                self.assertEqual(discarded, {"deleted": False, "path": None})
                self.assertEqual(
                    sent["user_message"]["attachment_ids"], [attachment["id"]]
                )
                self.store.finish_run(sent["run"]["id"], "completed", "done")
            else:
                with self.assertRaises(PermissionError):
                    self.store.get_attachment(self.a_id, attachment["id"])

    def test_run_submission_and_completion_are_idempotent(self):
        self.approve()
        room = self.room()
        first = self.make_run(room)
        second = self.make_run(room)
        self.assertFalse(first["deduplicated"])
        self.assertTrue(second["deduplicated"])
        self.assertEqual(first["run"]["id"], second["run"]["id"])
        self.assertEqual(len(self.store.list_messages(self.a_id, room["id"])), 1)
        with self.assertRaises(ValueError):
            self.make_run(room, text="different content")
        answer = self.store.finish_run(first["run"]["id"], "completed", "answer")
        replay = self.store.finish_run(first["run"]["id"], "failed", "duplicate error")
        self.assertEqual(answer["id"], replay["id"])
        self.assertEqual(replay["content"], "answer")
        self.assertIsNotNone(answer["duration_seconds"])
        self.assertEqual(len(self.store.list_messages(self.a_id, room["id"])), 2)
        self.assertEqual(self.store.get_run(self.a_id, first["run"]["id"])["status"], "completed")
        with self.assertRaises(ValueError):
            self.store.update_run(first["run"]["id"], status="running")

    def test_first_prompt_names_only_an_untouched_new_chat(self):
        self.approve()
        room = self.room(title="New chat")
        first = self.make_run(
            room,
            text='In reply to this message:\n> old quoted title\n\n# \u0628\u0631\u0631\u0633\u06cc \u0645\u0634\u06a9\u0644 \u0627\u062a\u0635\u0627\u0644 \u0645\u06a9\u0631\u0631 \u0633\u0631\u0648\u06cc\u0633',
        )
        self.assertEqual(first["room"]["title"], '\u0628\u0631\u0631\u0633\u06cc \u0645\u0634\u06a9\u0644 \u0627\u062a\u0635\u0627\u0644 \u0645\u06a9\u0631\u0631 \u0633\u0631\u0648\u06cc\u0633')
        self.assertEqual(self.store.get_room(self.a_id, room["id"])["title"], '\u0628\u0631\u0631\u0633\u06cc \u0645\u0634\u06a9\u0644 \u0627\u062a\u0635\u0627\u0644 \u0645\u06a9\u0631\u0631 \u0633\u0631\u0648\u06cc\u0633')
        replay = self.make_run(
            room,
            text='In reply to this message:\n> old quoted title\n\n# \u0628\u0631\u0631\u0633\u06cc \u0645\u0634\u06a9\u0644 \u0627\u062a\u0635\u0627\u0644 \u0645\u06a9\u0631\u0631 \u0633\u0631\u0648\u06cc\u0633',
        )
        self.assertTrue(replay["deduplicated"])
        self.assertEqual(replay["room"]["title"], '\u0628\u0631\u0631\u0633\u06cc \u0645\u0634\u06a9\u0644 \u0627\u062a\u0635\u0627\u0644 \u0645\u06a9\u0631\u0631 \u0633\u0631\u0648\u06cc\u0633')
        self.store.finish_run(first["run"]["id"], "completed", "done")
        second = self.make_run(room, request_id="request-2", text="A later prompt must not rename it")
        self.assertEqual(second["room"]["title"], '\u0628\u0631\u0631\u0633\u06cc \u0645\u0634\u06a9\u0644 \u0627\u062a\u0635\u0627\u0644 \u0645\u06a9\u0631\u0631 \u0633\u0631\u0648\u06cc\u0633')
        self.store.finish_run(second["run"]["id"], "completed", "done")

        named = self.room(title="Chosen manually")
        named_result = self.make_run(named, request_id="named", text="Replace this title")
        self.assertEqual(named_result["room"]["title"], "Chosen manually")

    def test_prompt_title_is_bounded_and_has_a_safe_fallback(self):
        self.assertEqual(title_from_first_prompt("\n\t"), "New chat")
        title = title_from_first_prompt("word " * 40)
        self.assertEqual(len(title.split()), 9)
        self.assertLessEqual(len(title), 72)

    def test_simultaneous_submissions_schedule_one_run(self):
        self.approve()
        room = self.room()
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: self.make_run(room), range(20)))
        self.assertEqual(sum(not result["deduplicated"] for result in results), 1)
        self.assertEqual(len({result["run"]["id"] for result in results}), 1)
        self.assertEqual(len(self.store.list_messages(self.a_id, room["id"])), 1)

    def test_quotas_and_concurrency_are_central_and_cannot_be_bypassed(self):
        self.approve()
        room_a, room_a2, room_b = self.room(), self.room(), self.room(self.b_id)
        self.store.set_device(self.admin_id, self.a_id, token_budget=100)
        first = self.make_run(room_a)
        with self.assertRaises(ValueError):
            self.make_run(room_a2, request_id="second")
        self.assertTrue(self.make_run(room_b))
        self.store.set_device(self.admin_id, self.a_id, max_concurrent=2)
        second = self.make_run(room_a2, request_id="second")
        with self.assertRaises(ValueError):
            self.make_run(room_a, request_id="third")
        self.store.record_usage(first["run"]["id"], 60, 40)
        self.store.finish_run(first["run"]["id"], "completed", "done")
        self.store.finish_run(second["run"]["id"], "completed", "done")
        with self.assertRaises(ValueError):
            self.make_run(room_a, request_id="budget-over")
        self.assertTrue(self.make_run(room_a)["deduplicated"])
        self.store.delete_room(self.a_id, room_a["id"])
        with self.assertRaises(ValueError):
            self.make_run(room_a2, request_id="deletion-bypass")

    def test_usage_is_monotonic_deduplicated_and_survives_deletion_and_restart(self):
        self.approve()
        room = self.room()
        run = self.make_run(room)["run"]
        self.store.record_usage(run["id"], 100, 25, 80)
        self.store.record_usage(run["id"], 100, 25, 80)
        self.store.record_usage(run["id"], 20, 5, 10)
        self.store.record_usage(run["id"], 110, 40, 85)
        answer = self.store.finish_run(run["id"], "completed", "done")
        self.assertEqual(answer["token_usage"], 150)
        self.assertEqual(answer["usage"]["cached_tokens"], 85)
        self.store.append_event(run["id"], "done", {})
        self.store.delete_room(self.a_id, room["id"])
        self.assertIsNone(self.store.get_run_internal(run["id"])["room_id"])
        self.store.record_usage(run["id"], 115, 40, 85)
        self.store.close()
        self.store = Store(self.path)
        usage = {row["device_id"]: row for row in self.store.usage(self.admin_id)}
        self.assertEqual(usage[self.a_id]["total_tokens"], 155)
        self.assertEqual(usage[self.a_id]["today_tokens"], 155)
        self.assertEqual(usage[self.a_id]["month_tokens"], 155)
        self.assertEqual(usage[self.a_id]["unknown_usage_runs"], 0)
        self.assertEqual(usage[self.a_id]["runs"], 1)
        self.assertEqual(usage[self.a_id]["active_runs"], 0)
        self.assertEqual(usage[self.b_id]["total_tokens"], 0)
        self.assertEqual(self.store.list_rooms(self.a_id), [])

    def test_events_are_durable_ordered_deduplicated_and_room_scoped(self):
        self.approve()
        room_a, room_b = self.room(), self.room(self.b_id)
        run_a, run_b = self.make_run(room_a)["run"], self.make_run(room_b)["run"]
        first = self.store.append_event(run_a["id"], "text", {"delta": '\u0627\u0644\u0641'}, event_key="one")
        self.store.append_event(run_b["id"], "text", {"delta": "private"}, event_key="one")
        replay = self.store.append_event(run_a["id"], "text", {"delta": '\u0627\u0644\u0641'}, event_key="one")
        last = self.store.append_event(run_a["id"], "text", {"delta": '\u0628'})
        self.assertEqual(first["id"], replay["id"])
        self.assertGreater(last["id"], first["id"])
        self.store.close()
        self.store = Store(self.path)
        self.assertEqual([row["id"] for row in self.store.events(self.a_id, room_a["id"])], [first["id"], last["id"]])
        self.assertEqual(self.store.events(self.a_id, room_a["id"], after=first["id"])[0]["payload"], {"delta": '\u0628'})
        self.assertEqual(len(self.store.events(self.a_id, room_a["id"], limit=1)), 1)

    def test_event_burst_uses_one_transaction_and_preserves_order(self):
        self.approve()
        room = self.room()
        run = self.make_run(room)["run"]
        statements = []
        self.store.db.set_trace_callback(statements.append)
        events = self.store.append_events([
            (run["id"], "delta", {"text": str(index)}, None)
            for index in range(64)
        ])
        self.store.db.set_trace_callback(None)
        self.assertEqual(len(events), 64)
        self.assertEqual([event["payload"]["text"] for event in events], [str(index) for index in range(64)])
        self.assertEqual(sum(statement == "BEGIN " for statement in statements), 1)
        self.assertEqual(sum(statement == "COMMIT" for statement in statements), 1)

    def test_runtime_batch_commits_events_and_latest_usage_together(self):
        self.approve()
        room = self.room()
        run = self.make_run(room)["run"]
        statements = []
        self.store.runtime_db.set_trace_callback(statements.append)
        events = self.store.persist_runtime_batch(
            [
                (run["id"], "delta", {"text": "one"}, None),
                (run["id"], "delta", {"text": "two"}, None),
            ],
            [(run["id"], 100, 20, 40, 120)],
        )
        self.store.runtime_db.set_trace_callback(None)
        self.assertEqual([event["payload"]["text"] for event in events], ["one", "two"])
        usage = self.store.db.execute(
            "SELECT input_tokens,output_tokens,cached_tokens,total_tokens "
            "FROM usage_ledger WHERE run_id=?", (run["id"],),
        ).fetchone()
        self.assertEqual(tuple(usage), (100, 20, 40, 120))
        self.assertEqual(sum(statement == "BEGIN IMMEDIATE" for statement in statements), 1)
        self.assertEqual(sum(statement == "COMMIT" for statement in statements), 1)

    def test_sqlite_wal_policy_avoids_full_sync_checkpoint_stalls(self):
        settings = {
            "synchronous": self.store.db.execute("PRAGMA synchronous").fetchone()[0],
            "wal_autocheckpoint": self.store.db.execute("PRAGMA wal_autocheckpoint").fetchone()[0],
            "journal_size_limit": self.store.db.execute("PRAGMA journal_size_limit").fetchone()[0],
            "runtime_wal_autocheckpoint": self.store.runtime_db.execute(
                "PRAGMA wal_autocheckpoint"
            ).fetchone()[0],
        }
        self.assertEqual(settings, {
            "synchronous": 1,
            "wal_autocheckpoint": 0,
            "journal_size_limit": 64 * 1024 * 1024,
            "runtime_wal_autocheckpoint": 4000,
        })

    def test_recovery_finishes_live_runs_once_including_revoked_devices(self):
        self.approve()
        room_a, room_b = self.room(), self.room(self.b_id)
        run_a, run_b = self.make_run(room_a)["run"], self.make_run(room_b)["run"]
        self.store.update_run(run_a["id"], status="running", thread_id="thread-a", turn_id="turn-a")
        self.store.record_usage(run_a["id"], 50, 10)
        self.store.append_event(run_a["id"], "delta", {"text": "First part "})
        self.store.append_event(run_a["id"], "activity", {"text": "Must not enter answer"})
        self.store.append_event(run_a["id"], "delta", {"text": "second part"})
        self.store.finish_run(run_b["id"], "completed", "done")
        self.store.set_device(self.admin_id, self.a_id, status="revoked")
        self.store.close()
        self.store = Store(self.path)
        recovered = self.store.recover_runs()
        self.assertEqual([row["id"] for row in recovered], [run_a["id"]])
        self.assertEqual(recovered[0]["status"], "interrupted")
        self.assertEqual(self.store.recover_runs(), [])
        self.assertEqual(self.store.list_active_runs(), [])
        self.assertEqual(self.store.get_run_internal(run_b["id"])["status"], "completed")
        messages = self.store.list_messages(self.admin_id, room_a["id"])
        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[-1]["token_usage"], 60)
        self.assertEqual(messages[-1]["content"], "First part second part")
        events = self.store.events(self.admin_id, room_a["id"])
        self.assertEqual(len(events), 4)
        self.assertEqual(events[-1]["kind"], "completed")
        self.assertEqual(events[-1]["payload"]["message"]["content"], "First part second part")
        self.assertEqual(events[-1]["payload"]["status"], "interrupted")
        totals = {row["device_id"]: row for row in self.store.usage(self.admin_id)}
        self.assertEqual(totals[self.b_id]["unknown_usage_runs"], 1)

    def test_recovery_bounds_persisted_deltas_and_skips_malformed_payloads(self):
        self.approve()
        room = self.room()
        run = self.make_run(room)["run"]
        self.store.update_run(run["id"], status="running")
        malformed = self.store.append_event(run["id"], "delta", {"text": "invalid"})
        with self.store._lock, self.store.db:
            self.store.db.execute("UPDATE events SET payload='{' WHERE id=?", (malformed["id"],))
        self.store.append_event(run["id"], "delta", {"text": "a" * 60})
        self.store.append_event(run["id"], "delta", {"text": "b" * 60})
        with mock.patch("codex_lan.store.MAX_RECOVERED_RESPONSE_CHARACTERS", 96):
            self.store.recover_runs()
        content = self.store.list_messages(self.a_id, room["id"])[-1]["content"]
        self.assertLessEqual(len(content), 96)
        self.assertTrue(content.startswith("a"))
        self.assertIn("additional recovered response omitted", content)

    def test_recovery_bounds_number_of_persisted_delta_events(self):
        self.approve()
        room = self.room()
        run = self.make_run(room)["run"]
        self.store.update_run(run["id"], status="running")
        self.store.append_event(run["id"], "delta", {"text": "first"})
        self.store.append_event(run["id"], "delta", {"text": "second"})
        with mock.patch("codex_lan.store.MAX_RECOVERED_DELTA_EVENTS", 1):
            self.store.recover_runs()
        content = self.store.list_messages(self.a_id, room["id"])[-1]["content"]
        self.assertTrue(content.startswith("first"))
        self.assertNotIn("second", content)
        self.assertIn("additional recovered response omitted", content)

    def test_daily_usage_counts_only_new_tokens_across_midnight(self):
        self.approve()
        room = self.room()
        run_id = self.make_run(room)["run"]["id"]
        with mock.patch("codex_lan.store.datetime") as clock:
            clock.now.return_value = datetime(2026, 8, 31, 23, 59, tzinfo=timezone.utc)
            self.store.record_usage(run_id, 80, 20)
            clock.now.return_value = datetime(2026, 9, 1, 0, 1, tzinfo=timezone.utc)
            self.store.record_usage(run_id, 110, 40)
            self.store.record_usage(run_id, 110, 40)
            self.store.record_usage(run_id, 10, 5)
            report = next(row for row in self.store.usage(self.admin_id) if row["device_id"] == self.a_id)
        self.assertEqual(report["total_tokens"], 150)
        self.assertEqual(report["today_tokens"], 50)
        self.assertEqual(report["month_tokens"], 50)

    def test_clearing_budget_is_distinct_from_not_changing_budget(self):
        self.approve()
        self.store.set_device(self.admin_id, self.a_id, token_budget=100)
        self.store.set_device(self.admin_id, self.a_id, max_concurrent=2)
        self.assertEqual(self.store.get_device(self.a_id)["token_budget"], 100)
        self.store.set_device(self.admin_id, self.a_id, token_budget=None)
        self.assertIsNone(self.store.get_device(self.a_id)["token_budget"])

    def test_message_pagination_and_foreign_cursor_rejection(self):
        self.approve()
        room_a, room_b = self.room(), self.room(self.b_id)
        for index in range(4):
            created = self.make_run(room_a, request_id=str(index), text=f"question-{index}")
            self.store.finish_run(created["run"]["id"], "completed", f"answer-{index}")
        latest = self.store.list_messages(self.a_id, room_a["id"], limit=3)
        older = self.store.list_messages(self.a_id, room_a["id"], before=latest[0]["id"], limit=3)
        self.assertEqual([row["content"] for row in latest], ["answer-2", "question-3", "answer-3"])
        self.assertEqual([row["content"] for row in older], ["question-1", "answer-1", "question-2"])
        foreign = self.make_run(room_b)["user_message"]
        with self.assertRaises(PermissionError):
            self.store.list_messages(self.a_id, room_a["id"], before=foreign["id"])

    def test_message_pages_keep_metadata_and_attachment_order_with_bounded_reads(self):
        self.approve()
        room = self.room()
        attachments = [self.store.add_attachment(
            self.a_id, name, "application/octet-stream", 1, "/server/" + name,
        )["id"] for name in ("second.txt", "first.txt")]
        expected = []
        for index in range(25):
            created = self.make_run(
                room, request_id=f"page-{index}", text=f"question-{index}",
                attachments=attachments if index % 5 == 0 else [],
            )
            run_id = created["run"]["id"]
            if index % 2 == 0:
                self.store.configure_run_usage(run_id, "gpt-5.6-sol", "high", "standard")
                self.store.record_usage(run_id, 100 + index, 20, 50)
            status = "failed" if index % 3 == 0 else "completed"
            error = "Fixture interruption" if status == "failed" else None
            answer = self.store.finish_run(run_id, status, f"answer-{index}", error)
            expected.extend([created["user_message"], answer])
        with self.store.db:
            self.store.db.execute("UPDATE messages SET created_at=100 WHERE room_id=?", (room["id"],))
        for message in expected:
            message["created_at"] = 100.0

        for limit, before, wanted in (
            (1, None, expected[-1:]),
            (50, None, expected),
            (20, expected[30]["id"], expected[10:30]),
        ):
            with self.subTest(limit=limit, before=before):
                statements = []
                self.store.db.set_trace_callback(statements.append)
                try:
                    messages = self.store.list_messages(self.a_id, room["id"], before=before, limit=limit)
                finally:
                    self.store.db.set_trace_callback(None)
                self.assertEqual(messages, wanted)
                reads = [sql for sql in statements if sql.lstrip().upper().startswith("SELECT")]
                self.assertLessEqual(len(reads), 7)
        self.assertEqual(expected[0]["attachment_ids"], attachments)
        self.assertEqual(expected[1]["run_error"], "Fixture interruption")
        self.assertEqual(expected[1]["token_usage"], 120)
        self.assertEqual(expected[1]["usage"]["cached_tokens"], 50)
        self.assertIsNotNone(expected[1]["estimated_usage_credits_micros"])
        self.assertIsNone(expected[3]["token_usage"])
        self.assertEqual(self.store.list_messages(self.admin_id, room["id"]), expected)
        with self.assertRaises(PermissionError):
            self.store.list_messages(self.b_id, room["id"])

    def test_message_pagination_uses_index_order_without_sorting_room_history(self):
        for suffix, values in (("", ("fixture-room", 50)), (" AND rowid < ?", ("fixture-room", 500, 50))):
            with self.subTest(suffix=suffix):
                plan = self.store.db.execute(
                    "EXPLAIN QUERY PLAN SELECT * FROM messages WHERE room_id=?" + suffix
                    + " ORDER BY rowid DESC LIMIT ?", values,
                ).fetchall()
                details = " ".join(row["detail"] for row in plan).upper()
                self.assertIn("SEARCH MESSAGES USING INDEX", details)
                self.assertNotIn("TEMP B-TREE", details)

    def test_validation_and_immutable_ownership(self):
        self.approve()
        room = self.room()
        for fields in ({"owner_id": self.b_id}, {"id": "new"}, {}):
            with self.assertRaises(ValueError):
                self.store.update_room(self.a_id, room["id"], **fields)
        for kwargs in ({"max_concurrent": 0}, {"max_concurrent": 11}, {"token_budget": -1}, {"status": "admin"}):
            with self.assertRaises(ValueError):
                self.store.set_device(self.admin_id, self.a_id, **kwargs)
        with self.assertRaises(ValueError):
            self.store.create_room(self.a_id, "bad", "relative/path")
        created = self.make_run(room)
        with self.assertRaises(ValueError):
            self.store.delete_room(self.admin_id, room["id"])
        for values in ((-1, 0, 0), (0, 1, 1), (True, 1, 0)):
            with self.assertRaises(ValueError):
                self.store.record_usage(created["run"]["id"], *values)
        with self.assertRaises(ValueError):
            self.store.record_usage(created["run"]["id"], 10, 10, total_tokens=5)
        with self.assertRaises(ValueError):
            self.store.append_event(created["run"]["id"], "bad", {"nan": float("nan")})
        with self.assertRaises(ValueError):
            self.store.list_messages(self.a_id, room["id"], limit=-1)
        json.dumps(created)
        json.dumps(self.store.usage(self.admin_id))

    def test_workspace_change_is_owner_only_absolute_and_blocked_during_active_work(self):
        self.approve()
        room = self.room()
        with self.assertRaises(ValueError):
            self.store.update_room(self.a_id, room["id"], cwd="relative/project")
        with self.assertRaises(PermissionError):
            self.store.update_room(self.admin_id, room["id"], cwd="/home/admin/foreign")

        created = self.make_run(room)
        with self.assertRaises(RoomBusyError):
            self.store.update_room(self.a_id, room["id"], cwd="/home/client/next")
        self.assertEqual(self.store.get_room(self.a_id, room["id"])["cwd"], "/home/client/project")
        self.store.update_room(self.a_id, room["id"], cwd="/home/client/project")

        self.store.finish_run(created["run"]["id"], "completed", "done")
        changed = self.store.update_room(self.a_id, room["id"], cwd="/home/client/next")
        self.assertEqual(changed["cwd"], "/home/client/next")


if __name__ == "__main__":
    unittest.main()
