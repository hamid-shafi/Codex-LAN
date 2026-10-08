"""GTK regressions for bottom-first room history loading."""

from __future__ import annotations

import os
import tempfile
import time
import unittest
from unittest.mock import patch

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import GdkPixbuf, GLib, Gtk  # noqa: E402

import app  # noqa: E402


def drain_events() -> None:
    context = GLib.MainContext.default()
    while context.pending():
        context.iteration(False)


class LazyRoomLoadingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        ready, _args = Gtk.init_check([])
        if not ready:
            raise unittest.SkipTest("GTK display is unavailable")

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="codex-lazy-test-")
        self.environment = patch.dict(
            os.environ, {"HAMSOHBAT_DATA_DIR": self.temporary.name}
        )
        self.environment.start()
        self.bridge_start = patch.object(app.CodexBridge, "start", lambda *_a, **_k: None)
        self.bridge_start.start()
        self.window = app.MainWindow(None)

    def tearDown(self) -> None:
        if not self.window._closing:
            self.window._finish_close()
        self.window.destroy()
        drain_events()
        self.bridge_start.stop()
        self.environment.stop()
        self.temporary.cleanup()

    def bubbles(self):
        return [
            child
            for child in self.window.message_box.get_children()
            if isinstance(child, app.MessageBubble)
        ]

    def add_messages(self, room_id: int, count: int) -> None:
        for index in range(count):
            self.window.store.add_message(room_id, "user", f"message-{index:02d}")

    def begin_streaming_turn(self, room: dict, thread_id: str, turn_id: str) -> dict:
        room_id = int(room["id"])
        self.window.store.update_room(room_id, thread_id=thread_id)
        self.window.store.add_message(room_id, "user", "Keep this live response")
        state = {
            "room_id": room_id,
            "thread_id": thread_id,
            "turn_id": turn_id,
            "model": room.get("model"),
            "effort": room.get("effort"),
            "text": "Keep this live response",
            "prompt_text": "Keep this live response",
            "image_path": None,
            "image_paths": [],
            "streaming_text": "",
            "streaming_item_id": "",
            "final_text": "",
            "started_at": time.monotonic(),
            "stop_requested": False,
            "activity_text": "",
            "reasoning_summary": "",
            "activity_feed": app.ActivityFeed(),
            "activity_snapshot": None,
            "activity_presentation": "working",
            "render_source_id": None,
            "final_answer_source_id": None,
            "final_answer_generation": 0,
            "connection_lost_at": None,
            "bubble": app.MessageBubble(
                "assistant",
                "",
                streaming=True,
                on_reply=self.window.start_reply,
                on_branch=self.window.open_side_thread,
            ),
        }
        self.window.room_turns[room_id] = state
        self.window.message_box.pack_start(state["bubble"], False, False, 0)
        state["bubble"].show_all()
        return state

    def test_room_opens_at_the_end_without_background_history_insertion(self) -> None:
        room_id = int(self.window.active_room["id"])
        self.add_messages(room_id, 60)
        room = self.window.store.get_room(room_id)
        self.assertIsNotNone(room)

        self.window.load_room(room)
        self.assertEqual(len(self.bubbles()), app.ROOM_INITIAL_RENDER_COUNT)
        self.assertEqual(
            [bubble.content for bubble in self.bubbles()],
            [f"message-{index:02d}" for index in range(50, 60)],
        )
        drain_events()
        self.assertIsNotNone(self.window._load_older_button)
        self.assertEqual(len(self.bubbles()), app.ROOM_INITIAL_RENDER_COUNT)

    def test_older_history_loads_only_after_requesting_it(self) -> None:
        room_id = int(self.window.active_room["id"])
        self.add_messages(room_id, 60)
        room = self.window.store.get_room(room_id)
        self.assertIsNotNone(room)
        self.window.load_room(room)

        self.window.load_older_messages(None)
        drain_events()
        self.assertEqual(
            [bubble.content for bubble in self.bubbles()],
            [f"message-{index:02d}" for index in range(26, 60)],
        )

    def test_switching_room_keeps_old_history_out_of_the_new_room(self) -> None:
        first_id = int(self.window.active_room["id"])
        self.add_messages(first_id, 60)
        first = self.window.store.get_room(first_id)
        second = self.window.store.create_room("Second")
        self.window.store.add_message(int(second["id"]), "user", "second-only")

        self.window.load_room(first)
        self.window.load_older_messages(None)
        self.window.load_room(self.window.store.get_room(int(second["id"])))
        drain_events()

        self.assertEqual([bubble.content for bubble in self.bubbles()], ["second-only"])

    def test_streaming_response_survives_switching_away_and_back(self) -> None:
        first_id = int(self.window.active_room["id"])
        first = self.window.store.get_room(first_id)
        self.assertIsNotNone(first)
        second = self.window.store.create_room("Second")
        state = self.begin_streaming_turn(first, "thread-live", "turn-live")

        with patch.object(self.window, "_queue_stream_render"):
            self.window.on_codex_event(
                "item/agentMessage/delta",
                {
                    "threadId": "thread-live",
                    "turnId": "turn-live",
                    "itemId": "message-one",
                    "delta": "First regular response paragraph.",
                },
            )
        state["bubble"].set_text(state["streaming_text"])
        expected = "First regular response paragraph."
        self.assertEqual(state["bubble"].content, expected)

        self.window.load_room(self.window.store.get_room(int(second["id"])))
        self.assertIsNone(state["bubble"])
        self.window.load_room(self.window.store.get_room(first_id))

        restored = self.window.room_turns[first_id]["bubble"]
        self.assertIsNotNone(restored)
        self.assertTrue(restored.streaming)
        self.assertEqual(restored.content, expected)

    def test_background_stream_deltas_are_shown_when_returning_to_the_room(self) -> None:
        first_id = int(self.window.active_room["id"])
        first = self.window.store.get_room(first_id)
        self.assertIsNotNone(first)
        second = self.window.store.create_room("Second")
        state = self.begin_streaming_turn(first, "thread-live", "turn-live")

        with patch.object(self.window, "_queue_stream_render"):
            self.window.on_codex_event(
                "item/agentMessage/delta",
                {
                    "threadId": "thread-live",
                    "turnId": "turn-live",
                    "itemId": "message-one",
                    "delta": "First regular response paragraph.",
                },
            )
        self.window.load_room(self.window.store.get_room(int(second["id"])))
        self.assertIsNone(state["bubble"])

        with patch.object(self.window, "_queue_stream_render") as queue_render:
            self.window.on_codex_event(
                "item/agentMessage/delta",
                {
                    "threadId": "thread-live",
                    "turnId": "turn-live",
                    "itemId": "message-two",
                    "delta": "Second paragraph arrived in the background.",
                },
            )
        expected = (
            "First regular response paragraph.\n\n"
            "Second paragraph arrived in the background."
        )
        self.assertEqual(state["streaming_text"], expected)
        queue_render.assert_called_once_with(first_id)

        self.window.load_room(self.window.store.get_room(first_id))

        restored = self.window.room_turns[first_id]["bubble"]
        self.assertIsNotNone(restored)
        self.assertEqual(restored.content, expected)

    def test_queued_stream_render_targets_the_rebuilt_bubble_after_a_room_round_trip(self) -> None:
        first_id = int(self.window.active_room["id"])
        first = self.window.store.get_room(first_id)
        self.assertIsNotNone(first)
        second = self.window.store.create_room("Second")
        state = self.begin_streaming_turn(first, "thread-live", "turn-live")
        callbacks = []

        def queue_callback(_milliseconds, callback):
            callbacks.append(callback)
            return 71

        with patch("app.GLib.timeout_add", side_effect=queue_callback):
            self.window.on_codex_event(
                "item/agentMessage/delta",
                {
                    "threadId": "thread-live",
                    "turnId": "turn-live",
                    "itemId": "message-one",
                    "delta": "Queued response paragraph.",
                },
            )
        old_bubble = state["bubble"]
        self.assertEqual(len(callbacks), 1)
        self.assertEqual(state["render_source_id"], 71)

        self.window.load_room(self.window.store.get_room(int(second["id"])))
        self.window.load_room(self.window.store.get_room(first_id))

        restored = state["bubble"]
        self.assertIsNot(restored, old_bubble)
        self.assertFalse(callbacks[0]())
        self.assertIsNone(state["render_source_id"])
        self.assertEqual(restored.content, "Queued response paragraph.")

    def test_nonfinal_completed_message_does_not_hide_later_live_stream_after_room_round_trip(self) -> None:
        first_id = int(self.window.active_room["id"])
        first = self.window.store.get_room(first_id)
        self.assertIsNotNone(first)
        second = self.window.store.create_room("Second")
        state = self.begin_streaming_turn(first, "thread-live", "turn-live")
        first_paragraph = "First completed commentary paragraph."
        second_paragraph = "Second paragraph is still streaming."

        with patch.object(self.window, "_queue_stream_render"):
            self.window.on_codex_event(
                "item/agentMessage/delta",
                {
                    "threadId": "thread-live",
                    "turnId": "turn-live",
                    "itemId": "commentary-one",
                    "delta": first_paragraph,
                },
            )
            self.window.on_codex_event(
                "item/completed",
                {
                    "threadId": "thread-live",
                    "turnId": "turn-live",
                    "item": {
                        "type": "agentMessage",
                        "phase": "commentary",
                        "text": first_paragraph,
                    },
                },
            )
            self.window.on_codex_event(
                "item/agentMessage/delta",
                {
                    "threadId": "thread-live",
                    "turnId": "turn-live",
                    "itemId": "commentary-two",
                    "delta": second_paragraph,
                },
            )
        expected = f"{first_paragraph}\n\n{second_paragraph}"
        self.assertEqual(state["final_text"], "")
        self.assertEqual(state["streaming_text"], expected)

        self.window.load_room(self.window.store.get_room(int(second["id"])))
        self.window.load_room(self.window.store.get_room(first_id))

        restored = self.window.room_turns[first_id]["bubble"]
        self.assertIsNotNone(restored)
        self.assertEqual(restored.content, expected)

    def test_completed_commentary_items_without_deltas_are_all_restored_after_room_round_trip(self) -> None:
        first_id = int(self.window.active_room["id"])
        first = self.window.store.get_room(first_id)
        self.assertIsNotNone(first)
        second = self.window.store.create_room("Second")
        self.begin_streaming_turn(first, "thread-live", "turn-live")
        first_paragraph = "First completed commentary paragraph."
        second_paragraph = "Second completed commentary paragraph."

        self.window.on_codex_event(
            "item/completed",
            {
                "threadId": "thread-live",
                "turnId": "turn-live",
                "item": {
                    "id": "commentary-one",
                    "type": "agentMessage",
                    "phase": "commentary",
                    "text": first_paragraph,
                },
            },
        )
        self.window.on_codex_event(
            "item/completed",
            {
                "threadId": "thread-live",
                "turnId": "turn-live",
                "item": {
                    "id": "commentary-two",
                    "type": "agentMessage",
                    "phase": "commentary",
                    "text": second_paragraph,
                },
            },
        )

        self.window.load_room(self.window.store.get_room(int(second["id"])))
        self.window.load_room(self.window.store.get_room(first_id))

        restored = self.window.room_turns[first_id]["bubble"]
        self.assertIsNotNone(restored)
        self.assertEqual(
            restored.content,
            f"{first_paragraph}\n\n{second_paragraph}",
        )

    def test_attachment_strip_contains_a_clickable_image_preview(self) -> None:
        source = os.path.join(self.temporary.name, "screenshot.png")
        pixbuf = GdkPixbuf.Pixbuf.new(
            GdkPixbuf.Colorspace.RGB, True, 8, 320, 180
        )
        pixbuf.fill(0x3979BDFF)
        pixbuf.savev(source, "png", [], [])
        imported = self.window.store.import_image(source)

        self.window.set_attachment(imported, "screenshot.png")
        drain_events()

        self.assertTrue(self.window.attachment_bar.get_visible())
        children = self.window.attachment_flow.get_children()
        self.assertEqual(len(children), 1)
        chip = children[0].get_child()
        thumbnails = [
            child
            for child in chip.get_children()
            if isinstance(child, Gtk.Button)
            and child.get_style_context().has_class("attachment-thumbnail")
        ]
        self.assertEqual(len(thumbnails), 1)
        self.assertEqual(thumbnails[0].get_tooltip_text(), "Preview screenshot.png")

    def test_missing_pending_image_keeps_a_visible_non_clickable_fallback(self) -> None:
        source = os.path.join(self.temporary.name, "vanishing.png")
        pixbuf = GdkPixbuf.Pixbuf.new(
            GdkPixbuf.Colorspace.RGB, True, 8, 320, 180
        )
        pixbuf.fill(0x3979BDFF)
        pixbuf.savev(source, "png", [], [])
        imported = self.window.store.import_image(source)
        self.window.set_attachment(imported, "vanishing.png")
        os.unlink(imported)
        self.window._render_attachment_strip()

        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            drain_events()
            chip = self.window.attachment_flow.get_children()[0].get_child()
            thumbnail = next(
                child
                for child in chip.get_children()
                if isinstance(child, Gtk.Button)
                and child.get_style_context().has_class("attachment-thumbnail")
            )
            if thumbnail.get_tooltip_text().startswith("Could not preview"):
                break
            time.sleep(0.01)

        self.assertFalse(thumbnail.get_sensitive())
        self.assertTrue(thumbnail.get_tooltip_text().startswith("Could not preview"))

    def test_attachment_preview_dialog_opens_without_blocking_the_window(self) -> None:
        source = os.path.join(self.temporary.name, "preview.png")
        pixbuf = GdkPixbuf.Pixbuf.new(
            GdkPixbuf.Colorspace.RGB, True, 8, 1600, 800
        )
        pixbuf.fill(0x3979BDFF)
        pixbuf.savev(source, "png", [], [])
        imported = self.window.store.import_image(source)

        dialog = self.window.show_attachment_preview(imported, "preview.png")

        self.assertIsInstance(dialog, Gtk.Dialog)
        self.assertTrue(dialog.get_visible())
        dialog.response(Gtk.ResponseType.CLOSE)
        drain_events()

    def test_unavailable_attachment_is_not_sent_to_the_backend(self) -> None:
        source = os.path.join(self.temporary.name, "removed-before-send.png")
        pixbuf = GdkPixbuf.Pixbuf.new(
            GdkPixbuf.Colorspace.RGB, True, 8, 320, 180
        )
        pixbuf.fill(0x3979BDFF)
        pixbuf.savev(source, "png", [], [])
        imported = self.window.store.import_image(source)
        self.window.set_attachment(imported, "removed-before-send.png")
        os.unlink(imported)

        usable = self.window._usable_pending_paths()

        self.assertEqual(usable, [])
        self.assertEqual(self.window._pending_paths(), [])
        self.assertFalse(self.window.attachment_bar.get_visible())


if __name__ == "__main__":
    unittest.main()
