"""CODEX: Legacy import fidelity, read-only source, and attachment boundaries."""

from __future__ import annotations

import base64
import hashlib
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from codex_lan.migrate import migrate
from codex_lan.store import Store
from storage import Store as DesktopStore


PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9ZlS0AAAAASUVORK5CYII=")


class LanMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)
        self.source_dir, self.destination = self.path / "desktop", self.path / "server"
        self.old = DesktopStore(self.source_dir)
        self.source = self.source_dir / "hamsohbat.sqlite3"
        store = Store(self.destination)
        self.admin = store.bootstrap_admin("Host")
        self.admin_id = self.admin["device"]["id"]
        store.close()
        self.credential = self.path / "admin.token"
        self.credential.write_text(self.admin["token"])
        self.image = self.old.upload_dir / "pixel.png"
        self.image.write_bytes(PNG)

    def tearDown(self):
        if self.old:
            self.old.close()
        self.temp.cleanup()

    def fixture(self):
        room = self.old.create_room("گفت‌وگوی اصلی")
        self.old.update_room(room["id"], model="astra", effort="ultra", thread_id="thread-main")
        self.old.add_message(room["id"], "user", "سلام", image_paths=[str(self.image)])
        answer = self.old.add_message(room["id"], "assistant", "answer with selected quote", duration_seconds=3.5, token_usage=250)
        self.old.add_message(room["id"], "user", "a reply", reply_to_id=answer, reply_quote="selected quote", image_paths=[str(self.image)])
        branch = self.old.create_branch(room["id"], answer, "selected quote", "thread-branch")
        self.old.add_branch_message(branch["id"], "user", "explain")
        self.old.add_branch_message(branch["id"], "assistant", "branch answer", duration_seconds=2, token_usage=50)
        return room

    def close_source(self):
        self.old.close()
        self.old = None

    def test_history_images_quotes_branches_models_and_timestamps_are_preserved(self):
        room = self.fixture()
        original = self.old.get_room(room["id"])
        self.close_source()
        before = hashlib.sha256(self.source.read_bytes()).hexdigest()
        modified = self.source.stat().st_mtime_ns
        self.source.chmod(0o400)
        report = migrate(self.source, self.destination, self.credential)
        self.assertEqual(report["rooms"], 2)
        self.assertEqual(report["messages"], 5)
        self.assertEqual(report["attachments"], 1)
        self.assertEqual(report["missing_images"], 0)
        self.assertTrue(Path(report["backup"]).is_file())
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), before)
        self.assertEqual(self.source.stat().st_mtime_ns, modified)
        store = Store(self.destination)
        try:
            rooms = store.list_rooms(self.admin_id)
            main = next(row for row in rooms if row["parent_room_id"] is None)
            branch = next(row for row in rooms if row["parent_room_id"] is not None)
            self.assertEqual(main["title"], "گفت‌وگوی اصلی")
            self.assertEqual((main["model"], main["effort"], main["thread_id"]), ("astra", "ultra", "thread-main"))
            self.assertEqual(main["created_at"], original["created_at"])
            self.assertEqual(main["updated_at"], original["updated_at"])
            self.assertEqual(branch["parent_room_id"], main["id"])
            self.assertEqual(branch["reply_quote"], "selected quote")
            self.assertEqual(branch["thread_id"], "thread-branch")
            messages = store.list_messages(self.admin_id, main["id"])
            self.assertEqual(messages[0]["content"], "سلام")
            self.assertEqual(messages[1]["content"], "answer with selected quote")
            self.assertEqual(messages[1]["duration_seconds"], 3.5)
            self.assertEqual(messages[2]["content"], "In reply to:\n> selected quote\n\na reply")
            self.assertEqual(messages[0]["attachment_ids"], messages[2]["attachment_ids"])
            attachment = store.get_attachment(self.admin_id, messages[0]["attachment_ids"][0])
            self.assertEqual(Path(attachment["path"]).read_bytes(), PNG)
            self.assertEqual(Path(attachment["path"]).stat().st_mode & 0o777, 0o600)
            self.assertEqual([row["content"] for row in store.list_messages(self.admin_id, branch["id"])], ["explain", "branch answer"])
            other = store.enroll("Other client")
            store.set_device(self.admin_id, other["device"]["id"], status="approved")
            self.assertEqual(store.list_rooms(other["device"]["id"]), [])
        finally:
            store.close()

    def test_reimport_is_idempotent_and_does_not_replace_new_conversation_edits(self):
        self.fixture()
        self.close_source()
        migrate(self.source, self.destination, self.credential)
        store = Store(self.destination)
        main = next(row for row in store.list_rooms(self.admin_id) if row["parent_room_id"] is None)
        store.update_room(self.admin_id, main["id"], title="renamed after import")
        counts = tuple(store.db.execute("SELECT COUNT(*) FROM " + table).fetchone()[0] for table in ("rooms", "messages", "attachments", "runs"))
        store.close()
        report = migrate(self.source, self.destination, self.credential)
        self.assertEqual((report["rooms"], report["messages"], report["attachments"]), (0, 0, 0))
        store = Store(self.destination)
        try:
            self.assertEqual(store.get_room(self.admin_id, main["id"])["title"], "renamed after import")
            self.assertEqual(tuple(store.db.execute("SELECT COUNT(*) FROM " + table).fetchone()[0] for table in ("rooms", "messages", "attachments", "runs")), counts)
        finally:
            store.close()

    def test_import_never_reads_attachment_paths_outside_desktop_uploads(self):
        room = self.old.create_room("Boundaries")
        outside = self.path / "private-server-image.png"
        outside.write_bytes(PNG + b"PRIVATE")
        link = self.old.upload_dir / "escape.png"
        link.symlink_to(outside)
        missing = self.old.upload_dir / "missing.png"
        invalid = self.old.upload_dir / "invalid.png"
        invalid.write_bytes(b"not an image")
        self.old.add_message(room["id"], "user", "image boundary", image_paths=[str(outside), str(link), str(missing), str(invalid), str(self.image)])
        self.close_source()
        report = migrate(self.source, self.destination, self.credential)
        self.assertEqual(report["attachments"], 1)
        self.assertEqual(report["missing_images"], 4)
        store = Store(self.destination)
        try:
            room = store.list_rooms(self.admin_id)[0]
            messages = store.list_messages(self.admin_id, room["id"])
            self.assertEqual(len(messages[0]["attachment_ids"]), 1)
            uploaded = list((self.destination / "uploads").iterdir())
            self.assertEqual(len(uploaded), 1)
            self.assertEqual(uploaded[0].read_bytes(), PNG)
        finally:
            store.close()
        self.assertEqual(outside.read_bytes(), PNG + b"PRIVATE")

    def test_only_referenced_sessions_are_copied_without_overwriting(self):
        self.fixture()
        self.close_source()
        sessions = self.path / "sessions"
        sessions.mkdir()
        (sessions / "rollout-thread-main.jsonl").write_text("main history\n")
        (sessions / "rollout-thread-branch.jsonl").write_text("branch history\n")
        (sessions / "rollout-unrelated.jsonl").write_text("unrelated private history\n")
        (sessions / "symlink-thread-main.jsonl").symlink_to(sessions / "rollout-unrelated.jsonl")
        worker_home = self.path / "worker"
        report = migrate(self.source, self.destination, self.credential, worker_home, sessions)
        self.assertEqual(report["threads"], 2)
        copied = worker_home / "sessions"
        self.assertEqual({path.name for path in copied.iterdir()}, {"rollout-thread-main.jsonl", "rollout-thread-branch.jsonl"})
        target = copied / "rollout-thread-main.jsonl"
        target.write_text("new worker history\n")
        report = migrate(self.source, self.destination, self.credential, worker_home, sessions)
        self.assertEqual(report["threads"], 0)
        self.assertEqual(target.read_text(), "new worker history\n")
        self.assertEqual((sessions / "rollout-thread-main.jsonl").read_text(), "main history\n")

    def test_older_database_without_branches_imports_and_snapshot_is_queryable(self):
        room = self.old.create_room("Older history")
        self.old.add_message(room["id"], "user", "question")
        self.old.add_message(room["id"], "assistant", "answer")
        with self.old.db:
            self.old.db.execute("DROP TABLE branch_messages")
            self.old.db.execute("DROP TABLE branches")
        self.close_source()
        report = migrate(self.source, self.destination, self.credential)
        self.assertEqual((report["rooms"], report["messages"]), (1, 2))
        backup = sqlite3.connect(Path(report["backup"]).as_uri() + "?mode=ro", uri=True)
        try:
            self.assertEqual(backup.execute("SELECT content FROM messages ORDER BY id").fetchall(), [("question",), ("answer",)])
        finally:
            backup.close()


if __name__ == "__main__":
    unittest.main()
