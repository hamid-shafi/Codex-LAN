"""Selective history transfer uses synthetic accounts, events and attachment files."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from project_layout import configure_imports
configure_imports(__file__)

from codex_lan.store import Store
from lan_history_transfer import (
    HISTORY_TABLES, export_history, import_history, preflight_history, validate_history,
)


class HistoryTransferTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source, self.target, self.payload = (self.root / name for name in ("source", "target", "payload"))
        self.new_home = self.root / "new-home"
        self.new_home.mkdir()
        source = Store(self.source)
        self.owner = source.bootstrap_admin("Source administrator")["device"]["id"]
        self.other = source.enroll("Unrelated computer")["device"]["id"]
        source.set_device(self.owner, self.other, status="approved")
        self.attachment = self.source / "uploads" / "sample.png"
        self.attachment.parent.mkdir()
        self.image = b"synthetic attachment bytes\x00\x01"
        self.attachment.write_bytes(self.image)
        with source.db:
            for identifier, owner, title in (("selected", self.owner, "Selected synthetic room"),
                                             ("omitted", self.owner, "Omitted synthetic room"),
                                             ("other-owner", self.other, "OTHER PRIVATE HISTORY")):
                source.db.execute("INSERT INTO rooms(id,owner_id,title,cwd,thread_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                                  (identifier, owner, title, "/synthetic/old-workspace", "thread-" + identifier, 1, 2))
                source.db.execute("INSERT INTO runs(id,owner_id,room_id,request_id,request_fingerprint,thread_id,status,"
                                  "created_at,updated_at,model,effort,speed) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                                  ("run-" + identifier, owner, identifier, "request-" + identifier, "fingerprint", "thread-" + identifier,
                                   "running" if identifier == "selected" else "completed", 1, 2, "synthetic-model", "high", "standard"))
            source.db.execute("INSERT INTO attachments(id,owner_id,name,mime,size,path,created_at) VALUES(?,?,?,?,?,?,?)",
                              ("image", self.owner, "sample.png", "image/png", len(self.image), str(self.attachment), 1))
            source.db.execute("INSERT INTO attachments(id,owner_id,name,mime,size,path,created_at) VALUES(?,?,?,?,?,?,?)",
                              ("unreferenced", self.other, "secret.png", "image/png", 123, "/missing/unrelated-file", 1))
            for rowid, identifier, role, content, attachments, created in (
                (4, "user-z", "user", "First selected prompt", '["image"]', 5),
                (11, "assistant-a", "assistant", "Synthetic assistant response", '["image"]', 3),
                (15, "user-a", "user", "Follow-up retained in insertion order", '[]', 3),
            ):
                source.db.execute("INSERT INTO messages(rowid,id,room_id,run_id,role,content,attachment_ids,created_at) VALUES(?,?,?,?,?,?,?,?)",
                                  (rowid, identifier, "selected", "run-selected", role, content, attachments, created))
            source.db.execute("INSERT INTO messages(id,room_id,run_id,role,content,created_at) VALUES(?,?,?,?,?,?)",
                              ("other-message", "other-owner", "run-other-owner", "user", "OTHER PRIVATE HISTORY", 1))
            source.db.execute("UPDATE runs SET user_message_id='user-z',assistant_message_id='assistant-a' WHERE id='run-selected'")
            for identifier, kind, payload in (
                (8, "started", {"run": {"id": "run-selected", "owner_id": self.owner, "cwd": "/synthetic/old-workspace"}}),
                (13, "progress", {"activity": {"steps": ["one", "two"]}, "owner_copy": [self.owner],
                                  "content": "prefix " + self.owner, "path": str(self.attachment)}),
                (24, "assistant_delta", {"delta": "response chunk"}),
                (28, "artifact", {"attachment_id": "image"}),
            ):
                source.db.execute("INSERT INTO events(id,run_id,room_id,kind,payload,created_at) VALUES(?,?,?,?,?,?)",
                                  (identifier, "run-selected", "selected", kind, json.dumps(payload), 1))
            for status in ("queued", "sending", "accepted"):
                source.db.execute("INSERT INTO steering_receipts(id,owner_id,room_id,run_id,request_id,fingerprint,status,content,"
                                  "attachment_ids,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                                  ("receipt-" + status, self.owner, "selected", "run-selected", "steer-" + status,
                                   "fingerprint", status, "Follow up", '["image"]', 1, 2))
            source.db.execute("INSERT INTO usage_ledger VALUES(?,?,?,?,?,?,?)", ("run-selected", self.owner, 10, 5, 2, 15, 2))
            source.db.execute("INSERT INTO quota_usage VALUES(?,?,?,?,?)", ("run-selected", self.owner, 99, "historical", 2))
            source.db.execute("INSERT INTO usage_daily VALUES(?,?,?,?,?,?)", (self.owner, "2001-01-01", 10, 5, 2, 15))
            source.db.execute("INSERT INTO quota_deltas(run_id,owner_id,credits_micros,created_at) VALUES(?,?,?,?)",
                              ("run-selected", self.owner, 99, 2))
            source.db.execute("INSERT OR REPLACE INTO quota_state VALUES(1,?)", ('{"private_calibration":"source-only"}',))
            source.db.execute("INSERT INTO browser_logins VALUES(?,?,?,?,?)",
                              (self.owner, "source-login", "source-key", "SOURCE PASSWORD VERIFIER", 1))
        source.close()
        target = Store(self.target)
        self.new_admin = target.bootstrap_admin("Destination administrator")["device"]["id"]
        with target.db:
            target.db.execute("INSERT INTO browser_logins VALUES(?,?,?,?,?)",
                              (self.new_admin, "destination-login", "destination-key", "DESTINATION PASSWORD VERIFIER", 1))
            target.db.execute("INSERT OR REPLACE INTO quota_state VALUES(1,?)", ('{"destination_calibration":true}',))
        self.destination_auth = self._auth_snapshot(target.db)
        target.close()

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def _auth_snapshot(db):
        return {table: list(db.execute("SELECT * FROM " + table)) for table in (
            "devices", "browser_logins", "device_credentials", "browser_sessions", "quota_state",
        )}

    def export(self):
        return export_history(self.source, self.payload, ["selected"])

    def test_export_selects_history_without_credentials_and_keeps_source_identical(self):
        path = self.source / "server.sqlite3"
        before = (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns)
        metadata = self.export()
        self.assertEqual(before, (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns))
        self.assertEqual(metadata["room_ids"], ["selected"])
        self.assertEqual(metadata["thread_ids"], ["thread-selected"])
        self.assertEqual(metadata["interrupted_runs"], 1)
        with sqlite3.connect(self.payload / "history.sqlite3") as db:
            self.assertEqual({row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}, set(HISTORY_TABLES))
            self.assertEqual(db.execute("SELECT id FROM rooms").fetchall(), [("selected",)])
            self.assertEqual(db.execute("SELECT status FROM runs").fetchone()[0], "interrupted")
            self.assertEqual(dict(db.execute("SELECT id,status FROM steering_receipts")),
                             {"receipt-queued": "rejected", "receipt-sending": "uncertain", "receipt-accepted": "accepted"})
            self.assertEqual(db.execute("SELECT rowid,id FROM messages ORDER BY rowid").fetchall(),
                             [(4, "user-z"), (11, "assistant-a"), (15, "user-a")])
            self.assertEqual(db.execute("SELECT id FROM events ORDER BY id").fetchall(), [(8,), (13,), (24,), (28,)])
            self.assertEqual(db.execute("SELECT id FROM attachments").fetchall(), [("image",)])
        data = (self.payload / "history.sqlite3").read_bytes()
        for secret in (b"SOURCE PASSWORD VERIFIER", b"OTHER PRIVATE HISTORY", b"private_calibration"):
            self.assertNotIn(secret, data)
        with sqlite3.connect(path) as db:
            self.assertEqual(db.execute("SELECT status FROM runs WHERE id='run-selected'").fetchone()[0], "running")
        attachment = metadata["attachments"][0]
        self.assertEqual((self.payload / attachment["file"]).read_bytes(), self.image)
        self.assertEqual(attachment["sha256"], hashlib.sha256(self.image).hexdigest())
        self.assertEqual((self.payload / attachment["file"]).stat().st_mode & 0o777, 0o600)

    def test_import_remaps_owner_paths_preserves_order_and_leaves_auth_and_calibration(self):
        metadata = self.export()
        self.assertEqual(preflight_history(self.target, metadata), {"admin_id": self.new_admin})
        report = import_history(self.payload, self.target, metadata, self.new_home)
        self.assertEqual(report["counts"], metadata["counts"])
        with sqlite3.connect(self.target / "server.sqlite3") as db:
            db.row_factory = sqlite3.Row
            auth = self._auth_snapshot(db)
            self.assertEqual({key: [tuple(row) for row in value] for key, value in auth.items()},
                             {key: [tuple(row) for row in value] for key, value in self.destination_auth.items()})
            room = db.execute("SELECT * FROM rooms").fetchone()
            self.assertEqual((room["owner_id"], room["cwd"], room["thread_id"]), (self.new_admin, str(self.new_home), "thread-selected"))
            for table in ("rooms", "runs", "attachments", "usage_ledger", "quota_usage", "steering_receipts"):
                self.assertEqual({row[0] for row in db.execute("SELECT owner_id FROM " + table)}, {self.new_admin})
            self.assertEqual([tuple(row) for row in db.execute("SELECT rowid,id FROM messages ORDER BY rowid")],
                             [(4, "user-z"), (11, "assistant-a"), (15, "user-a")])
            self.assertEqual([row[0] for row in db.execute("SELECT id FROM events ORDER BY id")], [8, 13, 24, 28])
            first = json.loads(db.execute("SELECT payload FROM events WHERE id=8").fetchone()[0])
            self.assertEqual(first["run"]["owner_id"], self.new_admin)
            self.assertEqual(first["run"]["cwd"], str(self.new_home))
            progress = json.loads(db.execute("SELECT payload FROM events WHERE id=13").fetchone()[0])
            self.assertEqual(progress["owner_copy"], [self.new_admin])
            self.assertEqual(progress["content"], "prefix " + self.owner)
            attachment_path = Path(db.execute("SELECT path FROM attachments").fetchone()[0])
            self.assertTrue(attachment_path.is_relative_to(self.target / "uploads"))
            self.assertEqual(progress["path"], str(attachment_path))
            self.assertEqual(attachment_path.read_bytes(), self.image)
            self.assertEqual(attachment_path.stat().st_nlink, 1)
            self.assertEqual(db.execute("SELECT total_tokens FROM usage_ledger").fetchone()[0], 15)
            self.assertEqual(db.execute("SELECT credits_micros FROM quota_usage").fetchone()[0], 99)
            for table in ("usage_daily", "quota_deltas"):
                self.assertEqual(db.execute("SELECT COUNT(*) FROM " + table).fetchone()[0], 0)
            delivery = db.execute("SELECT owner_id,thread_id,status FROM artifact_deliveries").fetchone()
            self.assertEqual(tuple(delivery), (self.new_admin, "thread-selected", "pending"))
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])
        with self.assertRaisesRegex(ValueError, "empty"):
            import_history(self.payload, self.target, metadata, self.new_home)

    def test_existing_or_source_destination_is_rejected(self):
        metadata = self.export()
        with self.assertRaisesRegex(ValueError, "separate installation"):
            preflight_history(self.source, metadata)
        with sqlite3.connect(self.target / "server.sqlite3") as db:
            db.execute("INSERT INTO rooms(id,owner_id,title,cwd,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                       ("already-here", self.new_admin, "Existing room", str(self.new_home), 1, 2))
        with self.assertRaisesRegex(ValueError, "empty"):
            preflight_history(self.target, metadata)

    def test_missing_symlink_or_external_source_attachment_aborts_without_payload(self):
        original = self.attachment.with_name("saved.png")
        self.attachment.rename(original)
        for kind in ("missing", "symlink", "outside"):
            with self.subTest(kind=kind):
                if kind == "symlink":
                    self.attachment.symlink_to(original)
                if kind == "outside":
                    with sqlite3.connect(self.source / "server.sqlite3") as db:
                        db.execute("UPDATE attachments SET path=? WHERE id='image'", (str(self.root / "outside.png"),))
                    (self.root / "outside.png").write_bytes(self.image)
                with self.assertRaises(ValueError):
                    self.export()
                self.assertFalse(self.payload.exists())
                if self.attachment.is_symlink():
                    self.attachment.unlink()

    def test_altered_attachment_and_payload_symlink_are_rejected_before_import(self):
        metadata = self.export()
        path = self.payload / metadata["attachments"][0]["file"]
        path.write_bytes(b"x" * len(self.image))
        with self.assertRaisesRegex(ValueError, "hash"):
            import_history(self.payload, self.target, metadata, self.new_home)
        path.unlink()
        path.symlink_to(self.attachment)
        with self.assertRaisesRegex(ValueError, "symbolic links"):
            validate_history(self.payload, metadata)
        with sqlite3.connect(self.target / "server.sqlite3") as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM rooms").fetchone()[0], 0)

    def test_collision_never_overwrites_files_or_commits_rows(self):
        metadata = self.export()
        uploads = self.target / "uploads"
        uploads.mkdir()
        collision = uploads / Path(metadata["attachments"][0]["file"]).name
        collision.write_bytes(b"do not replace")
        with self.assertRaisesRegex(ValueError, "already exists"):
            import_history(self.payload, self.target, metadata, self.new_home)
        self.assertEqual(collision.read_bytes(), b"do not replace")
        with sqlite3.connect(self.target / "server.sqlite3") as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM rooms").fetchone()[0], 0)

    def test_untrusted_schema_and_outside_manifest_paths_are_rejected(self):
        metadata = self.export()
        metadata["attachments"][0]["file"] = "../outside"
        with self.assertRaisesRegex(ValueError, "manifest metadata"):
            validate_history(self.payload, metadata)
        metadata["attachments"][0]["file"] = "uploads/" + hashlib.sha256(b"image").hexdigest()
        with sqlite3.connect(self.payload / "history.sqlite3") as db:
            db.execute("CREATE VIEW malicious AS SELECT * FROM rooms")
        with self.assertRaisesRegex(ValueError, "schema objects"):
            validate_history(self.payload, metadata)

    def test_cross_owner_and_branch_selections_fail(self):
        for ids in (["selected", "other-owner"], ["other-owner"], ["not-found"], []):
            with self.subTest(ids=ids), self.assertRaises(ValueError):
                export_history(self.source, self.payload, ids)
        with sqlite3.connect(self.source / "server.sqlite3") as db:
            db.execute("UPDATE rooms SET parent_room_id='selected' WHERE id='omitted'")
        with self.assertRaisesRegex(ValueError, "branches or companions"):
            self.export()
        with self.assertRaisesRegex(ValueError, "root rooms"):
            export_history(self.source, self.payload, ["omitted"])

    def test_cross_room_run_reference_in_payload_fails(self):
        metadata = self.export()
        with sqlite3.connect(self.payload / "history.sqlite3") as db:
            db.execute("UPDATE events SET run_id='not-selected' WHERE id=13")
        with self.assertRaisesRegex(ValueError, "absent or different-room run"):
            validate_history(self.payload, metadata)

    def test_import_rollback_preserves_database_on_invalid_target_constraint(self):
        metadata = self.export()
        with sqlite3.connect(self.target / "server.sqlite3") as db:
            db.execute("CREATE TRIGGER reject_history BEFORE INSERT ON events BEGIN SELECT RAISE(ABORT,'test transaction failure'); END")
        with self.assertRaisesRegex(sqlite3.IntegrityError, "test transaction failure"):
            import_history(self.payload, self.target, metadata, self.new_home)
        with sqlite3.connect(self.target / "server.sqlite3") as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM rooms").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM runs").fetchone()[0], 0)
        self.assertEqual(list((self.target / "uploads").iterdir()), [])

    def test_active_partial_answer_is_recovered_without_changing_existing_order(self):
        with sqlite3.connect(self.source / "server.sqlite3") as db:
            db.execute("DELETE FROM messages WHERE id='assistant-a'")
            db.execute("UPDATE runs SET assistant_message_id=NULL WHERE id='run-selected'")
            db.execute("UPDATE events SET kind='delta',payload=? WHERE id=24", (json.dumps({"text": "Partial saved answer"}),))
        metadata = self.export()
        self.assertEqual(metadata["recovered_messages"], 1)
        import_history(self.payload, self.target, metadata, self.new_home)
        with sqlite3.connect(self.target / "server.sqlite3") as db:
            messages = db.execute("SELECT rowid,role,content FROM messages ORDER BY rowid").fetchall()
            self.assertEqual([row[0] for row in messages[:2]], [4, 15])
            self.assertEqual(messages[-1][1:], ("assistant", "Partial saved answer"))
            run = db.execute("SELECT status,assistant_message_id FROM runs").fetchone()
            self.assertEqual(run[0], "interrupted")
            self.assertEqual(db.execute("SELECT content FROM messages WHERE id=?", (run[1],)).fetchone()[0], "Partial saved answer")
            self.assertEqual(db.execute("SELECT COUNT(*) FROM events").fetchone()[0], 4)

    def test_live_wal_source_is_read_without_writing_database_or_wal(self):
        db = sqlite3.connect(self.source / "server.sqlite3")
        try:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("INSERT INTO events(id,run_id,room_id,kind,payload,created_at) VALUES(?,?,?,'delta',?,?)",
                       (90, "run-selected", "selected", '{"text":"committed in WAL"}', 2))
            db.commit()
            paths = [self.source / "server.sqlite3", self.source / "server.sqlite3-wal"]
            before = [(hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns) for path in paths]
            metadata = self.export()
            self.assertEqual(metadata["counts"]["events"], 5)
            self.assertEqual(before, [(hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns) for path in paths])
        finally:
            db.close()

    def test_event_only_attachment_is_preserved_and_late_file_failure_rolls_back(self):
        second = self.source / "uploads" / "event-only.png"
        second.write_bytes(b"event only")
        with sqlite3.connect(self.source / "server.sqlite3") as db:
            db.execute("INSERT INTO attachments(id,owner_id,name,mime,size,path,created_at) VALUES(?,?,?,?,?,?,?)",
                       ("event-only", self.owner, second.name, "image/png", 10, str(second), 1))
            db.execute("INSERT INTO events(id,run_id,room_id,kind,payload,created_at) VALUES(?,?,?,'artifact',?,?)",
                       (90, "run-selected", "selected", '{"attachment_id":"event-only"}', 2))
        metadata = self.export()
        self.assertEqual(metadata["counts"]["attachments"], 2)
        import os
        original_link = os.link
        calls = 0

        def fail_second(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("synthetic late link failure")
            return original_link(*args, **kwargs)

        with mock.patch("lan_history_transfer.os.link", side_effect=fail_second), self.assertRaisesRegex(OSError, "late link failure"):
            import_history(self.payload, self.target, metadata, self.new_home)
        self.assertEqual(list((self.target / "uploads").iterdir()), [])
        with sqlite3.connect(self.target / "server.sqlite3") as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM rooms").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
