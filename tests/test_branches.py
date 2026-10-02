#!/usr/bin/env python3
"""Storage tests for isolated message branches."""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR))

from storage import Store  # noqa: E402


class BranchStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tempdir.name))
        self.room = self.store.create_room("main")
        self.parent_id = self.store.add_message(
            self.room["id"], "assistant", "A long answer with a selected section."
        )

    def tearDown(self) -> None:
        self.store.close()
        self.tempdir.cleanup()

    def test_branch_messages_are_isolated_from_main_history(self) -> None:
        room_updated_at = self.store.get_room(self.room["id"])["updated_at"]
        branch = self.store.create_branch(
            self.room["id"],
            self.parent_id,
            "  selected section  ",
        )
        question_id = self.store.add_branch_message(
            branch["id"], "user", "Explain only this section."
        )
        answer_id = self.store.add_branch_message(
            branch["id"],
            "assistant",
            "This explanation is private to the branch.",
            duration_seconds=1.25,
            token_usage=42,
        )

        self.assertEqual(branch["selected_quote"], "selected section")
        self.assertEqual(
            [message["id"] for message in self.store.list_messages(self.room["id"])],
            [self.parent_id],
        )
        self.assertEqual(
            self.store.get_room(self.room["id"])["updated_at"], room_updated_at
        )
        branch_messages = self.store.list_branch_messages(branch["id"])
        self.assertEqual(
            [(message["id"], message["role"]) for message in branch_messages],
            [(question_id, "user"), (answer_id, "assistant")],
        )
        self.assertEqual(branch_messages[-1]["duration_seconds"], 1.25)
        self.assertEqual(branch_messages[-1]["token_usage"], 42)

    def test_branch_lookup_filtering_and_thread_assignment(self) -> None:
        second_parent = self.store.add_message(
            self.room["id"], "assistant", "A second answer."
        )
        first = self.store.create_branch(
            self.room["id"], self.parent_id, "first quote"
        )
        second = self.store.create_branch(
            self.room["id"], second_parent, "second quote", "thread-branch-2"
        )

        self.assertEqual(len(self.store.list_branches(self.room["id"])), 2)
        self.assertEqual(
            [item["id"] for item in self.store.list_branches(
                self.room["id"], self.parent_id
            )],
            [first["id"]],
        )
        assigned = self.store.set_branch_thread_id(first["id"], " thread-branch-1 ")
        self.assertEqual(assigned["thread_id"], "thread-branch-1")
        self.assertEqual(
            self.store.get_branch_by_thread_id("thread-branch-1")["id"],
            first["id"],
        )
        self.assertEqual(
            self.store.get_branch_by_thread_id("thread-branch-2")["id"],
            second["id"],
        )
        cleared = self.store.set_branch_thread_id(first["id"], "   ")
        self.assertIsNone(cleared["thread_id"])
        self.assertIsNone(self.store.set_branch_thread_id(999999, "missing"))

    def test_branch_anchor_and_role_validation(self) -> None:
        other_room = self.store.create_room("other")
        user_message = self.store.add_message(
            self.room["id"], "user", "Not an assistant answer."
        )

        with self.assertRaisesRegex(ValueError, "selected_quote"):
            self.store.create_branch(self.room["id"], self.parent_id, "   ")
        with self.assertRaisesRegex(ValueError, "does not belong"):
            self.store.create_branch(other_room["id"], self.parent_id, "quote")
        with self.assertRaisesRegex(ValueError, "assistant"):
            self.store.create_branch(self.room["id"], user_message, "quote")
        with self.assertRaisesRegex(ValueError, "role"):
            branch = self.store.create_branch(
                self.room["id"], self.parent_id, "valid"
            )
            self.store.add_branch_message(branch["id"], "invalid", "text")
        with self.assertRaisesRegex(ValueError, "does not exist"):
            self.store.add_branch_message(999999, "user", "text")

    def test_branch_thread_ids_are_unique(self) -> None:
        first = self.store.create_branch(
            self.room["id"], self.parent_id, "first", "shared-thread"
        )
        second = self.store.create_branch(
            self.room["id"], self.parent_id, "second"
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.set_branch_thread_id(second["id"], "shared-thread")
        self.assertEqual(
            self.store.get_branch_by_thread_id("shared-thread")["id"],
            first["id"],
        )

    def test_branch_parent_and_room_deletions_cascade(self) -> None:
        first = self.store.create_branch(
            self.room["id"], self.parent_id, "first"
        )
        second = self.store.create_branch(
            self.room["id"], self.parent_id, "second"
        )
        self.store.add_branch_message(first["id"], "user", "first question")
        self.store.add_branch_message(second["id"], "user", "second question")

        self.assertTrue(self.store.delete_branch(first["id"]))
        self.assertFalse(self.store.delete_branch(first["id"]))
        self.assertEqual(self.store.list_branch_messages(first["id"]), [])
        self.assertIsNotNone(self.store.get_branch(second["id"]))

        with self.store._lock, self.store.db:
            self.store.db.execute(
                "DELETE FROM messages WHERE id = ?", (self.parent_id,)
            )
        self.assertIsNone(self.store.get_branch(second["id"]))
        self.assertEqual(self.store.list_branch_messages(second["id"]), [])

        parent = self.store.add_message(
            self.room["id"], "assistant", "Another answer."
        )
        branch = self.store.create_branch(self.room["id"], parent, "quote")
        self.store.add_branch_message(branch["id"], "assistant", "private")
        self.assertTrue(self.store.delete_room(self.room["id"]))
        self.assertIsNone(self.store.get_branch(branch["id"]))
        self.assertEqual(self.store.list_branch_messages(branch["id"]), [])

    def test_concurrent_branch_writes_remain_isolated(self) -> None:
        branches = [
            self.store.create_branch(
                self.room["id"], self.parent_id, f"quote-{index}"
            )
            for index in range(4)
        ]
        barrier = threading.Barrier(len(branches))
        failures: list[BaseException] = []

        def write(branch: dict, prefix: str) -> None:
            try:
                barrier.wait(timeout=2)
                for index in range(50):
                    self.store.add_branch_message(
                        branch["id"], "user", f"{prefix}:{index}"
                    )
            except BaseException as exc:  # pragma: no cover - assertion reports it
                failures.append(exc)

        workers = [
            threading.Thread(target=write, args=(branch, f"b{index}"))
            for index, branch in enumerate(branches)
        ]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=5)

        self.assertEqual(failures, [])
        self.assertTrue(all(not worker.is_alive() for worker in workers))
        for index, branch in enumerate(branches):
            messages = self.store.list_branch_messages(branch["id"])
            self.assertEqual(len(messages), 50)
            self.assertTrue(
                all(message["content"].startswith(f"b{index}:") for message in messages)
            )
        self.assertEqual(len(self.store.list_messages(self.room["id"])), 1)


class BranchMigrationTests(unittest.TestCase):
    def test_legacy_database_adds_branch_schema_without_touching_history(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            db = sqlite3.connect(root / "hamsohbat.sqlite3")
            db.executescript(
                """
                CREATE TABLE rooms (
                    id INTEGER PRIMARY KEY, title TEXT NOT NULL, thread_id TEXT,
                    model TEXT, effort TEXT, created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE messages (
                    id INTEGER PRIMARY KEY, room_id INTEGER NOT NULL,
                    role TEXT NOT NULL, content TEXT NOT NULL DEFAULT '',
                    image_path TEXT, created_at REAL NOT NULL
                );
                CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                INSERT INTO rooms VALUES(7, 'legacy room', NULL, NULL, NULL, 10, 20);
                INSERT INTO messages VALUES(
                    11, 7, 'assistant', 'legacy answer', NULL, 15
                );
                """
            )
            db.commit()
            db.close()

            store = Store(root)
            self.assertEqual(
                [(message["id"], message["content"])
                 for message in store.list_messages(7)],
                [(11, "legacy answer")],
            )
            table_names = {
                row[0]
                for row in store.db.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
            self.assertTrue({"branches", "branch_messages"}.issubset(table_names))
            branch_columns = {
                row[1] for row in store.db.execute("PRAGMA table_info(branches)")
            }
            self.assertTrue(
                {"room_id", "parent_message_id", "selected_quote", "thread_id"}
                .issubset(branch_columns)
            )
            branch = store.create_branch(7, 11, "legacy quote")
            store.add_branch_message(branch["id"], "user", "legacy follow-up")
            self.assertEqual(store.db.execute("PRAGMA quick_check").fetchone()[0], "ok")
            self.assertEqual(store.db.execute("PRAGMA foreign_key_check").fetchall(), [])
            store.close()

            reopened = Store(root)
            self.assertEqual(
                reopened.list_branch_messages(branch["id"])[0]["content"],
                "legacy follow-up",
            )
            self.assertEqual(reopened.list_messages(7)[0]["content"], "legacy answer")
            reopened.close()


if __name__ == "__main__":
    unittest.main()
