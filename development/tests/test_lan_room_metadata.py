"""CODEX: Stable sidebar order and owner-scoped, content-free unread metadata."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from project_layout import configure_imports, source_file
configure_imports(__file__)

from codex_lan.store import Store


class LanRoomMetadataTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name))
        self.admin_id = self.store.bootstrap_admin("Server")["device"]["id"]
        self.a_id = self.store.enroll("Client A")["device"]["id"]
        self.b_id = self.store.enroll("Client B")["device"]["id"]
        for identifier in (self.a_id, self.b_id):
            self.store.set_device(self.admin_id, identifier, status="approved")

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def room(self, title="Room", owner=None, timestamp=100):
        with mock.patch("codex_lan.store.time.time", return_value=timestamp):
            return self.store.create_room(owner or self.a_id, title, "/work/project")

    def make_run(self, room, request="first", timestamp=200):
        with mock.patch("codex_lan.store.time.time", return_value=timestamp):
            return self.store.create_run(room["owner_id"], room["id"], "PRIVATE USER CONTENT", [], room["id"] + request)["run"]

    def metadata(self, room):
        return next(item for item in self.store.list_rooms(room["owner_id"]) if item["id"] == room["id"])

    def test_new_room_and_user_message_have_no_assistant_notification(self):
        room = self.room()
        before = self.metadata(room)
        self.assertIsNone(before["last_assistant_message_id"])
        self.assertIsNone(before["last_assistant_message_at"])
        self.assertIsNone(before["last_message_id"])
        self.assertIsNone(before["last_message_at"])
        run = self.make_run(room)
        after = self.metadata(room)
        self.assertIsNone(after["last_assistant_message_id"])
        self.assertIsNone(after["last_assistant_message_at"])
        self.assertEqual(after["last_message_id"], run["user_message_id"])
        self.assertEqual(after["last_message_at"], 200)
        self.assertIsInstance(after["last_message_sequence"], int)
        self.assertNotIn("PRIVATE USER CONTENT", json.dumps(after))

    def test_only_terminal_assistant_messages_advance_signal(self):
        room = self.room()
        run = self.make_run(room)
        self.store.update_run(run["id"], status="running")
        self.store.append_event(run["id"], "delta", {"text": "LIVE CONTENT"})
        self.assertIsNone(self.metadata(room)["last_assistant_message_id"])
        with mock.patch("codex_lan.store.time.time", return_value=301.25):
            message = self.store.finish_run(run["id"], "completed", "PRIVATE ASSISTANT CONTENT")
        metadata = self.metadata(room)
        self.assertEqual(metadata["last_assistant_message_id"], message["id"])
        self.assertEqual(metadata["last_assistant_message_at"], 301.25)
        self.assertEqual(metadata["last_message_id"], message["id"])
        self.assertEqual(metadata["last_message_at"], 301.25)
        self.assertNotIn("PRIVATE", json.dumps(metadata))
        self.assertNotIn("LIVE CONTENT", json.dumps(metadata))
        self.store.update_room(self.a_id, room["id"], title="Renamed", model="astra", effort="ultra")
        pending = self.make_run(room, "second", timestamp=400)
        self.assertEqual(self.metadata(room)["last_assistant_message_id"], message["id"])
        self.assertEqual(self.metadata(room)["last_assistant_message_at"], 301.25)
        with mock.patch("codex_lan.store.time.time", return_value=500):
            failed = self.store.finish_run(pending["id"], "failed", "", "Run failed")
        self.assertEqual(self.metadata(room)["last_assistant_message_id"], failed["id"])
        self.assertEqual(self.metadata(room)["last_assistant_message_at"], 500)

    def test_oldest_first_order_stays_fixed_through_messages_and_edits(self):
        first = self.room("First", timestamp=100)
        second = self.room("Second", timestamp=101)
        third = self.room("Third", timestamp=102)
        expected = [first["id"], second["id"], third["id"]]
        self.assertEqual([item["id"] for item in self.store.list_rooms(self.a_id)], expected)
        run = self.make_run(first)
        self.store.finish_run(run["id"], "completed", "reply")
        self.store.update_room(self.a_id, second["id"], title="New title", model="terra", effort="high")
        self.make_run(third)
        self.assertEqual([item["id"] for item in self.store.list_rooms(self.a_id)], expected)
        fourth = self.room("Fourth", timestamp=103)
        self.assertEqual([item["id"] for item in self.store.list_rooms(self.a_id)], expected + [fourth["id"]])

    def test_equal_creation_times_have_stable_id_tiebreak(self):
        rooms = [self.room(str(index), timestamp=100) for index in range(4)]
        self.assertEqual([item["id"] for item in self.store.list_rooms(self.a_id)], sorted(item["id"] for item in rooms))

    def test_metadata_is_owner_scoped_and_admin_sees_all_rooms(self):
        a = self.room("A", self.a_id)
        b = self.room("B", self.b_id, timestamp=101)
        host = self.room("Host", self.admin_id, timestamp=102)
        replies = {}
        for room in (a, b, host):
            run = self.make_run(room)
            replies[room["id"]] = self.store.finish_run(run["id"], "completed", "secret-" + room["id"])["id"]
        for owner, room in ((self.a_id, a), (self.b_id, b)):
            visible = self.store.list_rooms(owner)
            self.assertEqual(len(visible), 1)
            self.assertEqual(visible[0]["id"], room["id"])
            self.assertEqual(visible[0]["last_assistant_message_id"], replies[room["id"]])
        all_rooms = self.store.list_rooms(self.admin_id)
        self.assertEqual([room["id"] for room in all_rooms], [a["id"], b["id"], host["id"]])
        self.assertNotIn("secret-", json.dumps(all_rooms))
        pending = self.store.enroll("Pending")["device"]["id"]
        with self.assertRaises(PermissionError):
            self.store.list_rooms(pending)
        self.store.set_device(self.admin_id, self.a_id, status="revoked")
        with self.assertRaises(PermissionError):
            self.store.list_rooms(self.a_id)

    def test_latest_assistant_follows_transcript_even_if_clock_moves_backwards(self):
        room = self.room()
        first = self.make_run(room)
        with mock.patch("codex_lan.store.time.time", return_value=500):
            old = self.store.finish_run(first["id"], "completed", "old")
        second = self.make_run(room, "second", timestamp=300)
        with mock.patch("codex_lan.store.time.time", return_value=400):
            newest = self.store.finish_run(second["id"], "cancelled", "new")
        self.assertNotEqual(old["id"], newest["id"])
        self.assertEqual(self.metadata(room)["last_assistant_message_id"], newest["id"])
        self.assertEqual(self.metadata(room)["last_assistant_message_at"], 400)


if __name__ == "__main__":
    unittest.main()
