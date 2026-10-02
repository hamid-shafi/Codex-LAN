"""Headless branch protocol routing tests."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from branch_controller import BranchController
from storage import Store


class FakePanel:
    def __init__(self) -> None:
        self.messages = []
        self.answer = ""
        self.busy = False

    def add_message(self, role, content):
        self.messages.append((role, content))

    def start_answer(self):
        self.busy = True

    def set_answer(self, text):
        self.answer = text

    def finish_answer(self, text):
        self.answer = text
        self.busy = False

    def set_busy(self, busy):
        self.busy = busy


class FakeBridge:
    def __init__(self) -> None:
        self.started_threads = []
        self.started_turns = []

    def start_thread(self, model, cwd, callback, _errback, **kwargs):
        self.started_threads.append((model, cwd, kwargs))
        callback({"thread": {"id": "branch-thread"}})

    def resume_thread(self, *args, **kwargs):
        raise AssertionError("unexpected resume")

    def start_turn(self, thread_id, prompt, model, effort, cwd, image, callback, _errback, **kwargs):
        self.started_turns.append((thread_id, prompt, model, effort, cwd, image, kwargs))
        callback({"turn": {"id": "branch-turn"}})


class BranchControllerTests(unittest.TestCase):
    def test_branch_events_never_touch_parent_timeline(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            store = Store(Path(tempdir))
            room = store.create_room("Room")
            parent = store.add_message(room["id"], "assistant", "Parent answer")
            branch = store.create_branch(room["id"], parent, "selected passage")
            bridge = FakeBridge()
            errors = []
            controller = BranchController(store, lambda: bridge, set(), lambda *error: errors.append(error))
            panel = FakePanel()
            self.assertTrue(controller.ask(branch["id"], "Why?", panel, "model", "high", "/tmp"))
            self.assertIn("selected passage", bridge.started_turns[0][1])
            controller.handle_event(
                "item/agentMessage/delta",
                {"threadId": "branch-thread", "turnId": "branch-turn", "delta": "Because"},
            )
            controller.handle_event(
                "turn/completed",
                {"threadId": "branch-thread", "turn": {"id": "branch-turn"}},
            )
            self.assertEqual(panel.answer, "Because")
            self.assertEqual(len(store.list_messages(room["id"])), 1)
            self.assertEqual(
                [(m["role"], m["content"]) for m in store.list_branch_messages(branch["id"])],
                [("user", "Why?"), ("assistant", "Because")],
            )
            self.assertEqual(errors, [])
            store.close()

    def test_buffered_branch_events_recover_after_gui_reopen(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            store = Store(Path(tempdir))
            room = store.create_room("Room")
            store.update_room(room["id"], model="model", effort="high")
            parent = store.add_message(room["id"], "assistant", "Parent answer")
            branch = store.create_branch(room["id"], parent, "selected passage")
            store.set_branch_thread_id(branch["id"], "persisted-branch-thread")
            store.add_branch_message(branch["id"], "user", "Pending question")
            panel = FakePanel()
            controller = BranchController(
                store, lambda: FakeBridge(), set(), lambda *_error: None
            )
            controller.register_panel(branch["id"], panel)

            self.assertTrue(
                controller.handle_event(
                    "item/agentMessage/delta",
                    {
                        "threadId": "persisted-branch-thread",
                        "turnId": "replayed-turn",
                        "delta": "Recovered answer",
                    },
                )
            )
            self.assertTrue(panel.busy)
            self.assertEqual(panel.answer, "Recovered answer")
            self.assertTrue(
                controller.handle_event(
                    "turn/completed",
                    {
                        "threadId": "persisted-branch-thread",
                        "turn": {"id": "replayed-turn"},
                    },
                )
            )
            self.assertFalse(panel.busy)
            self.assertEqual(
                [(m["role"], m["content"]) for m in store.list_branch_messages(branch["id"])],
                [("user", "Pending question"), ("assistant", "Recovered answer")],
            )
            self.assertEqual(len(store.list_messages(room["id"])), 1)
            store.close()

    def test_orphaned_branch_thread_start_recovers_and_starts_turn(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            store = Store(Path(tempdir))
            room = store.create_room("Room")
            store.update_room(room["id"], model="model", effort="high")
            parent = store.add_message(room["id"], "assistant", "Parent answer")
            branch = store.create_branch(room["id"], parent, "selected passage")
            store.add_branch_message(branch["id"], "user", "Pending question")
            bridge = FakeBridge()
            controller = BranchController(
                store, lambda: bridge, set(), lambda *_error: None
            )

            self.assertTrue(
                controller.handle_event(
                    "backend/orphanResponse",
                    {
                        "requestMethod": "thread/start",
                        "context": {"branchId": branch["id"]},
                        "result": {"thread": {"id": "branch-thread"}},
                    },
                )
            )
            self.assertEqual(
                store.get_branch(branch["id"])["thread_id"], "branch-thread"
            )
            self.assertEqual(len(bridge.started_turns), 1)
            self.assertIn("selected passage", bridge.started_turns[0][1])
            self.assertIn("Pending question", bridge.started_turns[0][1])
            store.close()

    def test_transient_disconnect_keeps_branch_turn_pending(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            store = Store(Path(tempdir))
            room = store.create_room("Room")
            parent = store.add_message(room["id"], "assistant", "Parent answer")
            branch = store.create_branch(room["id"], parent, "selected passage")
            controller = BranchController(
                store, lambda: FakeBridge(), set(), lambda *_error: None
            )
            panel = FakePanel()
            self.assertTrue(
                controller.ask(branch["id"], "Why?", panel, "model", "high", "/tmp")
            )
            controller._failed(
                branch["id"],
                {"message": "connection lost", "transient": True},
            )
            self.assertIn(branch["id"], controller.turns)
            self.assertTrue(panel.busy)
            self.assertEqual(
                [m["role"] for m in store.list_branch_messages(branch["id"])],
                ["user"],
            )
            store.close()


if __name__ == "__main__":
    unittest.main()
