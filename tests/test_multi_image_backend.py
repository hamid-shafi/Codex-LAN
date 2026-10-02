from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from codex_bridge import CodexBridge
from storage import Store


class MultiImageStorageTests(unittest.TestCase):
    def test_message_round_trip_preserves_all_images_and_legacy_primary(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            store = Store(Path(tempdir))
            room = store.create_room("images")
            parent_id = store.add_message(
                room["id"],
                "user",
                "two screenshots",
                image_paths=["/tmp/first.png", "/tmp/second.png"],
            )
            store.add_message(
                room["id"], "assistant", "reply", reply_to_id=parent_id
            )

            parent = store.get_message(parent_id)
            self.assertEqual(parent["image_path"], "/tmp/first.png")
            self.assertEqual(
                parent["image_paths"], ["/tmp/first.png", "/tmp/second.png"]
            )
            reply = store.list_messages_with_replies(room["id"])[1]
            self.assertEqual(
                reply["reply_image_paths"],
                ["/tmp/first.png", "/tmp/second.png"],
            )
            store.close()

    def test_plural_draft_round_trip_synchronizes_legacy_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            store = Store(root)
            room = store.create_room("draft")
            store.update_room(
                room["id"],
                draft_image_paths=["/tmp/one.png", "/tmp/two.png"],
                draft_image_labels=["one", "two"],
            )
            store.close()

            reopened = Store(root)
            draft = reopened.get_room(room["id"])
            self.assertEqual(draft["draft_image_path"], "/tmp/one.png")
            self.assertEqual(draft["draft_image_label"], "one")
            self.assertEqual(
                draft["draft_image_paths"], ["/tmp/one.png", "/tmp/two.png"]
            )
            self.assertEqual(draft["draft_image_labels"], ["one", "two"])

            reopened.update_room(room["id"], draft_image_path=None)
            cleared = reopened.get_room(room["id"])
            self.assertEqual(cleared["draft_image_paths"], [])
            self.assertEqual(cleared["draft_image_labels"], [])
            reopened.close()

    def test_legacy_rows_are_exposed_as_single_image_lists(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            db = sqlite3.connect(root / "hamsohbat.sqlite3")
            db.executescript(
                """
                CREATE TABLE rooms (
                    id INTEGER PRIMARY KEY, title TEXT NOT NULL, thread_id TEXT,
                    model TEXT, effort TEXT, created_at REAL NOT NULL,
                    updated_at REAL NOT NULL, draft_image_path TEXT,
                    draft_image_label TEXT
                );
                CREATE TABLE messages (
                    id INTEGER PRIMARY KEY, room_id INTEGER NOT NULL,
                    role TEXT NOT NULL, content TEXT NOT NULL DEFAULT '',
                    image_path TEXT, created_at REAL NOT NULL
                );
                INSERT INTO rooms VALUES(
                    1, 'legacy', NULL, NULL, NULL, 1, 1,
                    '/tmp/draft.png', 'draft.png'
                );
                INSERT INTO messages VALUES(
                    1, 1, 'user', '', '/tmp/legacy.png', 1
                );
                """
            )
            db.commit()
            db.close()

            store = Store(root)
            self.assertEqual(
                store.get_room(1)["draft_image_paths"], ["/tmp/draft.png"]
            )
            self.assertEqual(
                store.get_message(1)["image_paths"], ["/tmp/legacy.png"]
            )
            store.close()

    def test_secondary_image_reference_is_protected_and_deleted_with_room(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            first_source = root / "first.png"
            second_source = root / "second.png"
            first_source.write_bytes(b"first")
            second_source.write_bytes(b"second")
            store = Store(root / "data")
            room = store.create_room("cleanup")
            first = store.import_image(str(first_source))
            second = store.import_image(str(second_source))
            store.add_message(
                room["id"], "user", "", image_paths=[first, second]
            )

            self.assertFalse(store.remove_image_if_unused(second))
            self.assertTrue(store.delete_room(room["id"]))
            self.assertFalse(Path(first).exists())
            self.assertFalse(Path(second).exists())
            store.close()


class MultiImageBridgeTests(unittest.TestCase):
    @staticmethod
    def _bridge_and_capture():
        bridge = CodexBridge(dispatch=lambda fn, *args: fn(*args), on_event=lambda *_: None)
        captured = []

        def request(method, params=None, *_args, **kwargs):
            captured.append((method, params, kwargs))
            return 42

        bridge.request = request
        return bridge, captured

    def test_start_turn_emits_one_local_image_input_per_path(self) -> None:
        bridge, captured = self._bridge_and_capture()
        request_id = bridge.start_turn(
            "thread",
            "look",
            "model",
            "high",
            "/tmp",
            None,
            lambda _result: None,
            lambda _error: None,
            room_id=7,
            image_paths=["/tmp/a.png", "/tmp/b.png", "/tmp/a.png"],
        )
        self.assertEqual(request_id, 42)
        self.assertEqual(captured[0][0], "turn/start")
        self.assertEqual(
            captured[0][1]["input"],
            [
                {"type": "text", "text": "look"},
                {"type": "localImage", "path": "/tmp/a.png"},
                {"type": "localImage", "path": "/tmp/b.png"},
            ],
        )

    def test_existing_image_argument_also_accepts_a_sequence(self) -> None:
        bridge, captured = self._bridge_and_capture()
        bridge.steer_turn(
            "thread",
            "turn",
            "",
            [Path("/tmp/one.png"), Path("/tmp/two.png")],
            lambda _result: None,
            lambda _error: None,
            room_id=3,
        )
        self.assertEqual(
            captured[0][1]["input"],
            [
                {"type": "localImage", "path": "/tmp/one.png"},
                {"type": "localImage", "path": "/tmp/two.png"},
            ],
        )


if __name__ == "__main__":
    unittest.main()
