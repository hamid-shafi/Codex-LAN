"""Headless regression tests for room isolation and concurrent turn state.

These tests deliberately avoid constructing a Gtk window.  They exercise the
window controller methods with small fakes, so they run on CI without an X/Wayland
display while still checking the state-routing code used by the real UI.
"""

from __future__ import annotations

import sys
import tempfile
import threading
import unittest
import socket
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR))

from app import (  # noqa: E402
    MainWindow,
    clean_activity_text,
    composer_is_rtl,
    conversation_markdown,
    inflight_turn_display_text,
    model_display_label,
    model_tooltip_text,
    ordered_effort_options,
    preferred_model_id,
    room_is_busy,
)
from codex_bridge import CodexBridge  # noqa: E402
from backend import Backend  # noqa: E402
from storage import Store  # noqa: E402
from markdown_view import MarkdownView, inline_markup, is_rtl, split_blocks  # noqa: E402


class FakeBubble:
    def __init__(self) -> None:
        self.text = ""
        self.elapsed = None
        self.timer_hidden = False
        self.activity_presentations = []

    def set_text(self, text: str) -> None:
        self.text = text

    def set_elapsed(self, seconds: int, stopping: bool = False) -> None:
        self.elapsed = (seconds, stopping)

    def hide_timer(self, _duration_seconds=None) -> None:
        self.timer_hidden = True

    def set_activity(self, text: str) -> None:
        self.activity = text

    def set_activity_presentation(self, value: str) -> None:
        self.activity_presentations.append(value)

    def set_token_usage(self, total: int) -> None:
        self.token_usage = total


class FakeBridge:
    def __init__(self) -> None:
        self.interrupts = []
        self.names = []

    def interrupt(self, thread_id: str, turn_id: str) -> None:
        self.interrupts.append((thread_id, turn_id))

    def set_thread_name(self, thread_id: str, name: str, **_kwargs) -> None:
        self.names.append((thread_id, name))

    def list_models(self, success, error) -> None:
        self.model_callbacks = (success, error)


class FakeControl:
    def __init__(self) -> None:
        self.sensitive = True
        self.tooltip = ""
        self.visible = True
        self.no_show_all = False

    def set_sensitive(self, value: bool) -> None:
        self.sensitive = bool(value)

    def set_tooltip_text(self, value: str) -> None:
        self.tooltip = value

    def set_no_show_all(self, value: bool) -> None:
        self.no_show_all = bool(value)

    def show(self) -> None:
        self.visible = True

    def hide(self) -> None:
        self.visible = False


class FakeCombo(FakeControl):
    def __init__(self) -> None:
        super().__init__()
        self.items = []
        self.active_id = None
        self.changed_callback = None

    def _emit_changed(self) -> None:
        if self.changed_callback:
            self.changed_callback(self)

    def remove_all(self) -> None:
        self.items.clear()
        self.active_id = None
        self._emit_changed()

    def append(self, item_id: str, label: str) -> None:
        self.items.append((str(item_id), str(label)))

    def set_active_id(self, item_id: str) -> None:
        value = str(item_id)
        self.active_id = value if any(key == value for key, _label in self.items) else None
        self._emit_changed()

    def set_active(self, index: int) -> None:
        self.active_id = self.items[index][0] if 0 <= index < len(self.items) else None
        self._emit_changed()

    def get_active_id(self):
        return self.active_id


class FakeInputView:
    def __init__(self) -> None:
        self.editable = True

    def set_editable(self, value: bool) -> None:
        self.editable = bool(value)


class ControlsHarness:
    """Headless adapter around the real model/effort controller methods."""

    update_effort_options = MainWindow.update_effort_options
    update_model_tooltip = MainWindow.update_model_tooltip
    update_effort_tooltip = MainWindow.update_effort_tooltip
    on_model_changed = MainWindow.on_model_changed
    on_effort_changed = MainWindow.on_effort_changed
    sync_active_room_controls = MainWindow.sync_active_room_controls

    def __init__(self, store: Store, active_room: dict, models: dict) -> None:
        self.store = store
        self.active_room = active_room
        self.models = models
        self.room_turns = {}
        self.model_combo = FakeCombo()
        self.effort_combo = FakeCombo()
        self._effort_options_by_id = {}
        self._loading_room = False
        self._syncing_controls = False
        self.connected = True
        self.pending_image = None
        self.send_button = FakeControl()
        self.stop_button = FakeControl()
        self.image_button = FakeControl()
        self.input_view = FakeInputView()
        self.statuses = []
        for model_id, model in models.items():
            self.model_combo.append(model_id, model_display_label(model_id, model))
        selected = preferred_model_id(models, active_room.get("model"))
        if selected:
            self.model_combo.set_active_id(selected)
        self.update_effort_options()

    def _input_text(self) -> str:
        return ""

    def set_status(self, text: str, style: str) -> None:
        self.statuses.append((text, style))


class ControllerHarness:
    """Just enough of MainWindow for controller-method unit tests."""

    stop_generation = MainWindow.stop_generation
    _state_for_event = MainWindow._state_for_event
    on_codex_event = MainWindow.on_codex_event
    finish_turn = MainWindow.finish_turn
    _update_turn_timers = MainWindow._update_turn_timers
    is_near_bottom = MainWindow.is_near_bottom
    _restore_orphaned_turn = MainWindow._restore_orphaned_turn
    _handle_orphan_response = MainWindow._handle_orphan_response
    _thread_started = MainWindow._thread_started
    _thread_resumed = MainWindow._thread_resumed
    _turn_accepted = MainWindow._turn_accepted
    _room_turn_error = MainWindow._room_turn_error
    _mark_turn_reconnecting = MainWindow._mark_turn_reconnecting
    _expire_disconnected_turns = MainWindow._expire_disconnected_turns
    _cancel_final_answer_fallback = MainWindow._cancel_final_answer_fallback
    _schedule_final_answer_fallback = MainWindow._schedule_final_answer_fallback
    _active_turn_states = MainWindow._active_turn_states
    on_bridge_ready = MainWindow.on_bridge_ready
    on_bridge_error = MainWindow.on_bridge_error

    def __init__(self, store: Store, rooms: list[dict]) -> None:
        self.store = store
        self.active_room = rooms[0]
        self.room_turns = {}
        self.room_buttons = {}
        self.room_titles = {}
        self.unread_rooms = set()
        self.bridge = FakeBridge()
        self.loaded_threads = set()
        self.started_turns = []
        self.statuses = []
        self.sync_count = 0
        self.refresh_count = 0
        self.reconnect_count = 0
        self._closing = False
        self._reconnect_attempts = 0
        self._reconnect_source_id = None
        self.connected = True
        adjustment = SimpleNamespace(
            get_upper=lambda: 1000.0,
            get_value=lambda: 900.0,
            get_page_size=lambda: 100.0,
        )
        self.message_scroll = SimpleNamespace(
            get_vadjustment=lambda: adjustment
        )

    def sync_active_room_controls(self) -> None:
        self.sync_count += 1

    def refresh_rooms(self, *_args, **_kwargs) -> None:
        self.refresh_count += 1

    def scroll_to_bottom(self, *_args, **_kwargs) -> None:
        pass

    def _queue_stream_render(self, room_id: int) -> None:
        state = self.room_turns[room_id]
        state["bubble"].set_text(state["streaming_text"])

    def set_status(self, text: str, style: str) -> None:
        self.statuses.append((text, style))

    def schedule_reconnect(self) -> None:
        self.reconnect_count += 1

    def _start_turn(self, room_id: int, thread_id: str) -> None:
        self.started_turns.append((room_id, thread_id))

    def on_models_loaded(self, _result) -> bool:
        return False

    def on_models_error(self, _error) -> bool:
        return False


def turn_state(room: dict, thread_id: str, turn_id: str, started_at=10.0):
    return {
        "room_id": int(room["id"]),
        "thread_id": thread_id,
        "turn_id": turn_id,
        "model": room.get("model"),
        "effort": room.get("effort"),
        "text": "",
        "image_path": None,
        "streaming_text": "",
        "final_text": "",
        "completed_agent_text": "",
        "started_at": started_at,
        "stop_requested": False,
        "bubble": FakeBubble(),
    }


class MultiRoomControllerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tempdir.name))
        self.room_a = self.store.create_room("A")
        self.room_b = self.store.create_room("B")
        self.store.update_room(self.room_a["id"], thread_id="thread-a")
        self.store.update_room(self.room_b["id"], thread_id="thread-b")
        self.room_a = self.store.get_room(self.room_a["id"])
        self.room_b = self.store.get_room(self.room_b["id"])
        self.controller = ControllerHarness(self.store, [self.room_a, self.room_b])
        self.controller.room_turns = {
            self.room_a["id"]: turn_state(self.room_a, "thread-a", "turn-a"),
            self.room_b["id"]: turn_state(self.room_b, "thread-b", "turn-b"),
        }

    def tearDown(self) -> None:
        self.store.close()
        self.tempdir.cleanup()

    def test_stream_delta_is_routed_only_to_matching_room(self) -> None:
        self.controller.on_codex_event(
            "item/agentMessage/delta", {"threadId": "thread-b", "delta": "B only"}
        )
        state_a = self.controller.room_turns[self.room_a["id"]]
        state_b = self.controller.room_turns[self.room_b["id"]]
        self.assertEqual(state_a["streaming_text"], "")
        self.assertEqual(state_a["bubble"].text, "")
        self.assertEqual(state_b["streaming_text"], "B only")
        self.assertEqual(state_b["bubble"].text, "B only")

    def test_distinct_streamed_commentary_messages_start_on_a_new_line(self) -> None:
        room_id = self.room_b["id"]
        for item_id, delta in (
            ("message-1", "دارم بررسی می‌کنم"),
            ("message-1", "..."),
            ("message-2", "مشکل پیدا شد"),
        ):
            self.controller.on_codex_event(
                "item/agentMessage/delta",
                {
                    "threadId": "thread-b",
                    "turnId": "turn-b",
                    "itemId": item_id,
                    "delta": delta,
                },
            )
        state = self.controller.room_turns[room_id]
        self.assertEqual(
            state["streaming_text"],
            "دارم بررسی می‌کنم...\n\nمشکل پیدا شد",
        )

    def test_room_restore_keeps_all_live_commentary_after_item_completion(self) -> None:
        room_id = self.room_b["id"]
        self.controller.on_codex_event(
            "item/agentMessage/delta",
            {
                "threadId": "thread-b",
                "turnId": "turn-b",
                "itemId": "commentary-1",
                "delta": "Inspecting the room switch.",
            },
        )
        self.controller.on_codex_event(
            "item/completed",
            {
                "threadId": "thread-b",
                "turnId": "turn-b",
                "item": {
                    "type": "agentMessage",
                    "id": "commentary-1",
                    "phase": "commentary",
                    "text": "Inspecting the room switch.",
                },
            },
        )
        self.controller.on_codex_event(
            "item/agentMessage/delta",
            {
                "threadId": "thread-b",
                "turnId": "turn-b",
                "itemId": "commentary-2",
                "delta": "The live body must survive switching rooms.",
            },
        )

        state = self.controller.room_turns[room_id]
        self.assertEqual(state["final_text"], "")
        self.assertEqual(
            inflight_turn_display_text(state),
            "Inspecting the room switch.\n\n"
            "The live body must survive switching rooms.",
        )

    def test_final_answer_remains_the_displayed_body_after_final_phase(self) -> None:
        room_id = self.room_b["id"]
        state = self.controller.room_turns[room_id]
        state["streaming_text"] = "Commentary that is still in the live buffer."

        with patch("app.GLib.timeout_add", return_value=73):
            self.controller.on_codex_event(
                "item/completed",
                {
                    "threadId": "thread-b",
                    "turnId": "turn-b",
                    "item": {
                        "type": "agentMessage",
                        "phase": "final_answer",
                        "text": "The final answer.",
                    },
                },
            )

        self.assertEqual(inflight_turn_display_text(state), "The final answer.")

    def test_unphased_completed_message_remains_a_legacy_display_fallback(self) -> None:
        room_id = self.room_b["id"]
        self.controller.on_codex_event(
            "item/completed",
            {
                "threadId": "thread-b",
                "turnId": "turn-b",
                "item": {
                    "type": "agentMessage",
                    "text": "A response delivered without delta events.",
                },
            },
        )

        state = self.controller.room_turns[room_id]
        self.assertEqual(state["final_text"], "")
        self.assertEqual(
            inflight_turn_display_text(state),
            "A response delivered without delta events.",
        )

    def test_completed_commentary_reconciles_a_partial_delta_by_item_id(self) -> None:
        room_id = self.room_b["id"]
        self.controller.on_codex_event(
            "item/agentMessage/delta",
            {
                "threadId": "thread-b",
                "turnId": "turn-b",
                "itemId": "commentary-1",
                "delta": "Inspecting the ",
            },
        )
        self.controller.on_codex_event(
            "item/completed",
            {
                "threadId": "thread-b",
                "turnId": "turn-b",
                "item": {
                    "id": "commentary-1",
                    "type": "agentMessage",
                    "phase": "commentary",
                    "text": "Inspecting the room switch.",
                },
            },
        )
        self.controller.on_codex_event(
            "item/completed",
            {
                "threadId": "thread-b",
                "turnId": "turn-b",
                "item": {
                    "id": "commentary-2",
                    "type": "agentMessage",
                    "phase": "commentary",
                    "text": "Keeping every completed paragraph.",
                },
            },
        )

        self.assertEqual(
            self.controller.room_turns[room_id]["streaming_text"],
            "Inspecting the room switch.\n\n"
            "Keeping every completed paragraph.",
        )

    def test_completed_commentary_adopts_anonymous_live_delta(self) -> None:
        room_id = self.room_b["id"]
        self.controller.on_codex_event(
            "item/agentMessage/delta",
            {
                "threadId": "thread-b",
                "turnId": "turn-b",
                "delta": "Inspecting the ",
            },
        )
        self.controller.on_codex_event(
            "item/completed",
            {
                "threadId": "thread-b",
                "turnId": "turn-b",
                "item": {
                    "id": "commentary-1",
                    "type": "agentMessage",
                    "phase": "commentary",
                    "text": "Inspecting the room switch.",
                },
            },
        )

        state = self.controller.room_turns[room_id]
        self.assertEqual(state["streaming_text"], "Inspecting the room switch.")
        self.assertEqual(len(state["streaming_items"]), 1)
        self.assertEqual(state["streaming_items"][0]["id"], "commentary-1")

    def test_turn_completion_persists_only_matching_room(self) -> None:
        self.controller.room_turns[self.room_b["id"]]["final_text"] = "answer B"
        self.controller.on_codex_event(
            "turn/completed", {"threadId": "thread-b", "turn": {"id": "turn-b"}}
        )
        self.assertIn(self.room_a["id"], self.controller.room_turns)
        self.assertNotIn(self.room_b["id"], self.controller.room_turns)
        self.assertEqual(self.store.list_messages(self.room_a["id"]), [])
        messages = self.store.list_messages(self.room_b["id"])
        self.assertEqual([(m["role"], m["content"]) for m in messages], [("assistant", "answer B")])

    def test_final_answer_fallback_finishes_when_terminal_event_is_missing(self) -> None:
        room_id = self.room_b["id"]
        callbacks = []

        def fake_timeout(_milliseconds, callback):
            callbacks.append(callback)
            return 71

        with patch("app.GLib.timeout_add", side_effect=fake_timeout), patch(
            "app.GLib.source_remove"
        ):
            self.controller.on_codex_event(
                "item/completed",
                {
                    "threadId": "thread-b",
                    "turnId": "turn-b",
                    "item": {
                        "type": "agentMessage",
                        "phase": "final_answer",
                        "text": "The completed answer",
                    },
                },
            )
            self.assertEqual(len(callbacks), 1)
            self.assertIn(room_id, self.controller.room_turns)
            self.assertFalse(callbacks[0]())

        self.assertNotIn(room_id, self.controller.room_turns)
        self.assertEqual(
            [(message["role"], message["content"])
             for message in self.store.list_messages(room_id)],
            [("assistant", "The completed answer")],
        )

    def test_duplicate_terminal_and_usage_events_cannot_resurrect_finished_turn(self) -> None:
        room_id = self.room_b["id"]
        self.store.add_message(room_id, "user", "pending")
        self.controller.room_turns[room_id]["final_text"] = "done"
        terminal = {"threadId": "thread-b", "turn": {"id": "turn-b"}}
        self.controller.on_codex_event("turn/completed", terminal)
        expected_messages = self.store.list_messages(room_id)

        self.controller.on_codex_event("turn/completed", terminal)
        self.controller.on_codex_event(
            "thread/tokenUsage/updated",
            {
                "threadId": "thread-b",
                "turnId": "turn-b",
                "tokenUsage": {"last": {"totalTokens": 99}},
            },
        )

        self.assertNotIn(room_id, self.controller.room_turns)
        self.assertEqual(self.store.list_messages(room_id), expected_messages)

    def test_stop_interrupts_active_room_only(self) -> None:
        self.controller.active_room = self.room_b
        self.controller.stop_generation(SimpleNamespace())
        self.assertFalse(self.controller.room_turns[self.room_a["id"]]["stop_requested"])
        self.assertTrue(self.controller.room_turns[self.room_b["id"]]["stop_requested"])
        self.assertEqual(self.controller.bridge.interrupts, [("thread-b", "turn-b")])

    def test_missing_turn_id_finishes_the_pending_turn(self) -> None:
        room_id = self.room_b["id"]
        self.controller._turn_accepted(room_id, {"turn": {}})

        self.assertNotIn(room_id, self.controller.room_turns)
        messages = self.store.list_messages(room_id)
        self.assertEqual(messages[-1]["role"], "assistant")
        self.assertIn("did not return a turn ID", messages[-1]["content"])

    def test_turn_acceptance_refreshes_active_room_controls(self) -> None:
        room_id = self.room_b["id"]
        before = self.controller.sync_count
        self.controller._turn_accepted(room_id, {"turn": {"id": "accepted"}})

        self.assertEqual(self.controller.room_turns[room_id]["turn_id"], "accepted")
        self.assertEqual(self.controller.sync_count, before + 1)

    def test_preaccept_turn_is_considered_active_on_close(self) -> None:
        state = self.controller.room_turns[self.room_b["id"]]
        state["turn_id"] = None
        self.assertIn(state, self.controller._active_turn_states())

    def test_each_room_timer_uses_its_own_start_time(self) -> None:
        self.controller.room_turns[self.room_a["id"]]["started_at"] = 100.0
        self.controller.room_turns[self.room_b["id"]]["started_at"] = 125.0
        self.controller.room_turns[self.room_b["id"]]["stop_requested"] = True
        with patch("app.time.monotonic", return_value=160.0):
            self.assertTrue(self.controller._update_turn_timers())
        self.assertEqual(self.controller.room_turns[self.room_a["id"]]["bubble"].elapsed, (60, False))
        self.assertEqual(self.controller.room_turns[self.room_b["id"]]["bubble"].elapsed, (35, True))

    def test_late_event_recovers_room_from_persisted_thread_mapping(self) -> None:
        self.controller.room_turns.clear()
        self.store.add_message(self.room_b["id"], "user", "still pending")
        recovered = self.controller._state_for_event(
            {"threadId": "thread-b", "turnId": "late-turn"}
        )
        self.assertIsNotNone(recovered)
        self.assertEqual(recovered["room_id"], self.room_b["id"])
        self.assertEqual(recovered["turn_id"], "late-turn")
        self.assertNotIn(self.room_a["id"], self.controller.room_turns)

    def test_stale_turn_event_is_not_routed_by_matching_thread(self) -> None:
        state = self.controller._state_for_event(
            {"threadId": "thread-b", "turnId": "an-older-turn"}
        )
        self.assertIsNone(state)
        self.assertEqual(
            self.controller.room_turns[self.room_b["id"]]["turn_id"], "turn-b"
        )

    def test_reasoning_summary_is_shown_as_activity(self) -> None:
        self.controller.on_codex_event(
            "item/reasoning/summaryTextDelta",
            {"threadId": "thread-b", "turnId": "turn-b", "delta": "بررسی فایل‌ها"},
        )
        state = self.controller.room_turns[self.room_b["id"]]
        self.assertEqual(state["activity_text"], "بررسی فایل‌ها")
        self.assertEqual(state["bubble"].activity, "بررسی فایل‌ها")

    def test_token_usage_event_updates_only_matching_turn(self) -> None:
        self.controller.on_codex_event(
            "thread/tokenUsage/updated",
            {
                "threadId": "thread-b",
                "turnId": "turn-b",
                "tokenUsage": {"last": {"totalTokens": 4321}},
            },
        )
        self.assertEqual(self.controller.room_turns[self.room_b["id"]]["token_usage"], 4321)
        self.assertEqual(self.controller.room_turns[self.room_b["id"]]["bubble"].token_usage, 4321)
        self.assertNotIn("token_usage", self.controller.room_turns[self.room_a["id"]])

    def test_scroll_follow_detection_respects_user_position(self) -> None:
        adjustment = SimpleNamespace(
            get_upper=lambda: 1000.0,
            get_value=lambda: 300.0,
            get_page_size=lambda: 400.0,
        )
        self.controller.message_scroll = SimpleNamespace(
            get_vadjustment=lambda: adjustment
        )
        self.assertFalse(self.controller.is_near_bottom())
        adjustment.get_value = lambda: 590.0
        self.assertTrue(self.controller.is_near_bottom())

    def test_stream_render_updates_are_coalesced(self) -> None:
        room_id = self.room_b["id"]
        state = self.controller.room_turns[room_id]
        state["streaming_text"] = "latest"
        state["render_source_id"] = None
        callbacks = []

        def fake_timeout(_milliseconds, callback):
            callbacks.append(callback)
            return 77

        with patch("app.GLib.timeout_add", side_effect=fake_timeout):
            MainWindow._queue_stream_render(self.controller, room_id)
            MainWindow._queue_stream_render(self.controller, room_id)
        self.assertEqual(len(callbacks), 1)
        self.assertEqual(state["render_source_id"], 77)
        self.assertFalse(callbacks[0]())
        self.assertIsNone(state["render_source_id"])
        self.assertEqual(state["bubble"].text, "latest")

    def test_old_stream_callback_does_not_mutate_a_replacement_turn(self) -> None:
        room_id = self.room_b["id"]
        old_state = self.controller.room_turns[room_id]
        old_state["streaming_text"] = "stale"
        old_state["render_source_id"] = None
        callbacks = []

        def fake_timeout(_milliseconds, callback):
            callbacks.append(callback)
            return 77

        with patch("app.GLib.timeout_add", side_effect=fake_timeout):
            MainWindow._queue_stream_render(self.controller, room_id)
        replacement = turn_state(self.room_b, "thread-b", "next-turn")
        replacement["render_source_id"] = 88
        self.controller.room_turns[room_id] = replacement

        self.assertFalse(callbacks[0]())
        self.assertEqual(replacement["render_source_id"], 88)
        self.assertEqual(replacement["bubble"].text, "")

    def test_bridge_close_preserves_active_turns_and_starts_reconnect(self) -> None:
        self.controller.room_turns[self.room_a["id"]]["streaming_text"] = "partial A"
        self.controller.room_turns[self.room_b["id"]]["streaming_text"] = "partial B"
        self.controller.on_codex_event("bridge/closed", {})
        self.assertEqual(len(self.controller.room_turns), 2)
        self.assertEqual(self.store.list_messages(self.room_a["id"]), [])
        self.assertEqual(self.store.list_messages(self.room_b["id"]), [])
        self.assertEqual(self.controller.reconnect_count, 1)
        self.assertEqual(
            self.controller.statuses[-1], ("Reconnecting…", "connecting")
        )
        for state in self.controller.room_turns.values():
            self.assertEqual(state["activity_presentation"], "reconnecting")
            self.assertEqual(
                state["bubble"].activity_presentations[-1], "reconnecting"
            )

    def test_reconnect_marks_turns_as_restoring_until_new_activity_arrives(self) -> None:
        self.controller.on_codex_event("bridge/closed", {})

        self.controller.on_bridge_ready({})

        for state in self.controller.room_turns.values():
            self.assertEqual(state["activity_presentation"], "restoring")
            self.assertEqual(
                state["bubble"].activity_presentations[-1], "restoring"
            )
        state = self.controller.room_turns[self.room_b["id"]]
        self.controller.on_codex_event(
            "item/reasoning/summaryTextDelta",
            {"threadId": "thread-b", "turnId": "turn-b", "delta": "Checking files"},
        )
        self.assertEqual(state["activity_presentation"], "working")
        self.assertEqual(state["bubble"].activity_presentations[-1], "working")

    def test_disconnect_keeps_a_requested_stop_in_stopping_state(self) -> None:
        state = self.controller.room_turns[self.room_b["id"]]
        state["stop_requested"] = True

        self.controller.on_codex_event("bridge/closed", {})

        self.assertEqual(state["activity_presentation"], "stopping")
        self.assertIsNotNone(state["connection_lost_at"])
        self.assertEqual(state["bubble"].activity_presentations[-1], "stopping")

        self.controller.on_bridge_ready({})

        self.assertEqual(state["activity_presentation"], "stopping")
        self.assertIn(("thread-b", "turn-b"), self.controller.bridge.interrupts)

    def test_transient_transport_error_never_finishes_or_persists_turn(self) -> None:
        room_id = self.room_a["id"]
        state = self.controller.room_turns[room_id]
        self.controller._room_turn_error(
            room_id,
            {
                "code": "transport_disconnected",
                "message": "The connection to Codex was lost.",
                "transient": True,
            },
        )
        self.assertIs(self.controller.room_turns[room_id], state)
        self.assertIn("reconnecting", state["bubble"].activity.lower())
        self.assertEqual(state["activity_presentation"], "reconnecting")
        self.assertEqual(state["bubble"].activity_presentations[-1], "reconnecting")
        self.assertEqual(self.store.list_messages(room_id), [])

    def test_reconnect_error_marks_a_deadline_and_watchdog_releases_room(self) -> None:
        room_id = self.room_b["id"]
        self.controller.on_bridge_error(
            {"message": "socket closed", "transient": True}
        )
        state = self.controller.room_turns[room_id]
        self.assertIsNotNone(state.get("connection_lost_at"))
        self.assertEqual(state["activity_presentation"], "reconnecting")

        state["connection_lost_at"] = -1000.0
        with patch("app.time.monotonic", return_value=100.0):
            self.controller._update_turn_timers()

        self.assertNotIn(room_id, self.controller.room_turns)
        message = self.store.list_messages(room_id)[-1]
        self.assertIn("Connection timed out", message["content"])
        self.assertEqual(self.controller.statuses[-1], ("Connection unavailable", "error"))

    def test_sidebar_timer_reports_reconnecting_instead_of_responding(self) -> None:
        room_id = self.room_a["id"]
        state = self.controller.room_turns[room_id]
        state["activity_presentation"] = "reconnecting"

        class Button:
            def set_status(self, text):
                self.text = text

        button = Button()
        self.controller.room_buttons[room_id] = button
        with patch("app.time.monotonic", return_value=81.0):
            self.controller._update_turn_timers()

        self.assertEqual(button.text, "● Reconnecting • 1m 11s")

    def test_orphaned_thread_start_reconnects_to_original_room(self) -> None:
        self.controller.room_turns.clear()
        self.store.update_room(self.room_b["id"], thread_id=None, model="model-x")
        original = self.store.add_message(self.room_b["id"], "assistant", "original context")
        self.store.add_message(
            self.room_b["id"],
            "user",
            "continue me",
            reply_to_id=original,
            reply_quote="selected context",
        )
        self.controller._handle_orphan_response(
            {
                "requestMethod": "thread/start",
                "context": {"roomId": self.room_b["id"]},
                "result": {"thread": {"id": "recovered-thread"}},
            }
        )
        recovered = self.controller.room_turns[self.room_b["id"]]
        self.assertEqual(recovered["thread_id"], "recovered-thread")
        self.assertEqual(recovered["text"], "continue me")
        self.assertIn("selected context", recovered["prompt_text"])
        self.assertNotIn("original context", recovered["prompt_text"])
        self.assertEqual(
            self.controller.started_turns,
            [(self.room_b["id"], "recovered-thread")],
        )
        self.assertEqual(
            self.store.get_room(self.room_b["id"])["thread_id"],
            "recovered-thread",
        )

    def test_orphan_restore_does_not_resurrect_a_completed_history(self) -> None:
        self.controller.room_turns.clear()
        self.store.add_message(self.room_b["id"], "user", "already answered")
        self.store.add_message(self.room_b["id"], "assistant", "done")
        self.assertIsNone(
            self.controller._restore_orphaned_turn(self.room_b["id"])
        )


class ModelEffortControlTests(unittest.TestCase):
    """Regression coverage for the shared header controls in multiple rooms."""

    @staticmethod
    def astra_protocol_model() -> dict:
        """Current GPT-6 Astra shape returned by app-server ``model/list``.

        The server remains the authority for the default effort. This fixture
        uses ``high`` only to verify preservation of a declared default while
        rejecting a room's stale, unsupported saved effort.
        """
        return {
            "id": "gpt-6-astra",
            "displayName": "GPT-6 Astra",
            "description": "Flagship model for the hardest end-to-end work.",
            "isDefault": True,
            "defaultReasoningEffort": "high",
            # CODEX: Deliberately shuffled so the selector uses stable
            # CODEX: user-facing order without inventing absent wire values.
            "supportedReasoningEfforts": [
                {"reasoningEffort": "max"},
                {"reasoningEffort": "low"},
                {"reasoningEffort": "ultra"},
                {"reasoningEffort": "xhigh"},
                {"reasoningEffort": "high"},
                {"reasoningEffort": "medium"},
            ],
        }

    @staticmethod
    def protocol_models() -> dict:
        """A compact fixture matching the local app-server ``model/list`` shape."""
        return {
            "gpt-5.6-sol": {
                "id": "gpt-5.6-sol",
                "displayName": "GPT-5.6-Sol",
                "description": "Reliable agentic workhorse for everyday tasks.",
                "isDefault": True,
                "defaultReasoningEffort": "low",
                # Deliberately shuffled: the UI must apply protocol order itself.
                "supportedReasoningEfforts": [
                    {
                        "reasoningEffort": "ultra",
                        "description": "Maximum reasoning with automatic task delegation",
                    },
                    {
                        "reasoningEffort": "max",
                        "description": "Maximum reasoning depth for the hardest problems",
                    },
                    {
                        "reasoningEffort": "xhigh",
                        "description": "Extra high reasoning depth for complex problems",
                    },
                    {
                        "reasoningEffort": "medium",
                        "description": (
                            "Balances speed and reasoning depth for everyday tasks"
                        ),
                    },
                    {
                        "reasoningEffort": "high",
                        "description": "Greater reasoning depth for complex problems",
                    },
                    {
                        "reasoningEffort": "low",
                        "description": "Fast responses with lighter reasoning",
                    },
                ],
            },
            "gpt-5.6-terra": {
                "id": "gpt-5.6-terra",
                "displayName": "GPT-5.6-Terra",
                "description": "Balanced agentic coding model for everyday work.",
                "isDefault": False,
                "defaultReasoningEffort": "medium",
                "supportedReasoningEfforts": [
                    {
                        "reasoningEffort": effort,
                        "description": f"{effort} reasoning description",
                    }
                    for effort in ("low", "medium", "high", "xhigh", "max", "ultra")
                ],
            },
        }

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tempdir.name))
        self.room_a = self.store.create_room("A")
        self.room_b = self.store.create_room("B")
        self.models = self.protocol_models()

    def tearDown(self) -> None:
        self.store.close()
        self.tempdir.cleanup()

    def test_efforts_are_sorted_and_have_clear_english_labels(self) -> None:
        options = ordered_effort_options(self.models["gpt-5.6-sol"])
        self.assertEqual(
            [option["id"] for option in options],
            ["low", "medium", "high", "xhigh", "max", "ultra"],
        )
        self.assertEqual(
            [option["label"] for option in options],
            [
                "Low ★",
                "Medium",
                "High",
                "Extra",
                "Max",
                "Ultra",
            ],
        )
        self.assertEqual(
            options[-1]["description"],
            "Maximum reasoning with automatic task delegation",
        )
        self.assertFalse(
            any(
                "\u0600" <= char <= "\u06ff"
                for option in options
                for char in option["label"]
            )
        )

    def test_model_label_and_tooltip_are_fully_english(self) -> None:
        model = self.models["gpt-5.6-sol"]
        self.assertEqual(
            model_display_label("gpt-5.6-sol", model),
            "GPT-5.6-Sol ★",
        )
        tooltip = model_tooltip_text("gpt-5.6-sol", model)
        self.assertIn("Reliable agentic workhorse", tooltip)
        self.assertIn("Model ID: gpt-5.6-sol", tooltip)
        self.assertFalse(any("\u0600" <= char <= "\u06ff" for char in tooltip))

    def test_gpt6_astra_uses_backend_efforts_and_visible_identity(
        self,
    ) -> None:
        model = self.astra_protocol_model()
        options = ordered_effort_options(model, model_id="gpt-6-astra")

        self.assertEqual(
            [option["id"] for option in options],
            ["low", "medium", "high", "xhigh", "max", "ultra"],
        )
        self.assertEqual(
            [option["label"] for option in options],
            ["Low", "Medium", "High ★", "Extra", "Max", "Ultra"],
        )
        self.assertEqual(
            model_display_label("gpt-6-astra", model), "GPT-6 Astra ★"
        )
        tooltip = model_tooltip_text("gpt-6-astra", model)
        self.assertIn("Flagship model", tooltip)
        self.assertIn("Model ID: gpt-6-astra", tooltip)
        self.assertIn(
            "Reasoning: Low, Medium, High, Extra, Max, Ultra", tooltip
        )
        self.assertNotIn("none", [option["id"] for option in options])
        self.assertNotIn("minimal", [option["id"] for option in options])
        self.assertIn("ultra", [option["id"] for option in options])

    def test_switching_to_gpt6_astra_preserves_backend_declared_ultra(
        self,
    ) -> None:
        models = {
            "gpt-5.6-sol": self.models["gpt-5.6-sol"],
            "gpt-6-astra": self.astra_protocol_model(),
        }
        self.store.update_room(
            self.room_b["id"], model="gpt-5.6-sol", effort="ultra"
        )
        room = self.store.get_room(self.room_b["id"])
        harness = ControlsHarness(self.store, room, models)

        harness.model_combo.set_active_id("gpt-6-astra")
        harness.on_model_changed(harness.model_combo)

        persisted = self.store.get_room(self.room_b["id"])
        self.assertEqual(
            (persisted["model"], persisted["effort"]),
            ("gpt-6-astra", "ultra"),
        )
        self.assertEqual(harness.effort_combo.get_active_id(), "ultra")

    def test_switching_to_gpt6_astra_clamps_stale_minimal_to_declared_default(
        self,
    ) -> None:
        models = {
            "gpt-5.6-sol": self.models["gpt-5.6-sol"],
            "gpt-6-astra": self.astra_protocol_model(),
        }
        self.store.update_room(
            self.room_b["id"], model="gpt-5.6-sol", effort="minimal"
        )
        room = self.store.get_room(self.room_b["id"])
        harness = ControlsHarness(self.store, room, models)

        harness.model_combo.set_active_id("gpt-6-astra")
        harness.on_model_changed(harness.model_combo)

        persisted = self.store.get_room(self.room_b["id"])
        self.assertEqual(
            (persisted["model"], persisted["effort"]),
            ("gpt-6-astra", "high"),
        )
        self.assertEqual(harness.effort_combo.get_active_id(), "high")

    def test_gpt6_astra_invalid_default_falls_back_to_first_supported(
        self,
    ) -> None:
        model = self.astra_protocol_model()
        model["defaultReasoningEffort"] = "minimal"
        self.store.update_room(
            self.room_b["id"], model="gpt-6-astra", effort="minimal"
        )
        room = self.store.get_room(self.room_b["id"])
        harness = ControlsHarness(
            self.store, room, {"gpt-6-astra": model}
        )

        self.assertEqual(harness.effort_combo.get_active_id(), "low")
        harness.on_model_changed(harness.model_combo)
        self.assertEqual(
            self.store.get_room(self.room_b["id"])["effort"], "low"
        )

    def test_selected_effort_tooltip_uses_protocol_description(self) -> None:
        harness = ControlsHarness(self.store, self.room_b, self.models)
        self.assertIn("Low reasoning effort.", harness.effort_combo.tooltip)
        self.assertIn("Default for this model.", harness.effort_combo.tooltip)
        self.assertIn(
            "Fast responses with lighter reasoning",
            harness.effort_combo.tooltip,
        )
        harness.effort_combo.set_active_id("ultra")
        harness.update_effort_tooltip()
        self.assertIn("Ultra", harness.effort_combo.tooltip)
        self.assertIn("automatic task delegation", harness.effort_combo.tooltip)

    def test_unsaved_room_uses_the_protocol_default_model(self) -> None:
        self.assertEqual(
            preferred_model_id(self.models, None),
            "gpt-5.6-sol",
        )
        self.assertEqual(
            preferred_model_id(self.models, "missing-model"),
            "gpt-5.6-sol",
        )
        self.assertEqual(
            preferred_model_id(self.models, "gpt-5.6-terra"),
            "gpt-5.6-terra",
        )

    def test_background_busy_room_does_not_lock_active_idle_room(self) -> None:
        harness = ControlsHarness(self.store, self.room_b, self.models)
        harness.room_turns = {
            self.room_a["id"]: {"turn_id": "turn-a", "stop_requested": False}
        }

        harness.sync_active_room_controls()
        self.assertTrue(room_is_busy(self.room_a, harness.room_turns))
        self.assertFalse(room_is_busy(self.room_b, harness.room_turns))
        self.assertTrue(harness.model_combo.sensitive)
        self.assertTrue(harness.effort_combo.sensitive)

        harness.active_room = self.room_a
        harness.sync_active_room_controls()
        self.assertFalse(harness.model_combo.sensitive)
        self.assertFalse(harness.effort_combo.sensitive)

        harness.active_room = self.room_b
        harness.sync_active_room_controls()
        self.assertTrue(harness.model_combo.sensitive)
        self.assertTrue(harness.effort_combo.sensitive)

    def test_changes_update_only_the_active_idle_room(self) -> None:
        self.store.update_room(
            self.room_a["id"], model="gpt-5.6-sol", effort="low"
        )
        harness = ControlsHarness(self.store, self.room_b, self.models)
        harness.room_turns = {
            self.room_a["id"]: {"turn_id": "turn-a", "stop_requested": False}
        }

        harness.model_combo.set_active_id("gpt-5.6-terra")
        harness.on_model_changed(harness.model_combo)
        harness.effort_combo.set_active_id("max")
        harness.on_effort_changed(harness.effort_combo)

        room_a = self.store.get_room(self.room_a["id"])
        room_b = self.store.get_room(self.room_b["id"])
        self.assertEqual(
            (room_a["model"], room_a["effort"]),
            ("gpt-5.6-sol", "low"),
        )
        self.assertEqual(
            (room_b["model"], room_b["effort"]),
            ("gpt-5.6-terra", "max"),
        )
        self.assertEqual(harness.room_turns[self.room_a["id"]]["turn_id"], "turn-a")

    def test_busy_active_room_ignores_selector_change_callbacks(self) -> None:
        self.store.update_room(
            self.room_a["id"], model="gpt-5.6-sol", effort="low"
        )
        room_a = self.store.get_room(self.room_a["id"])
        harness = ControlsHarness(self.store, room_a, self.models)
        harness.room_turns = {
            self.room_a["id"]: {"turn_id": "turn-a", "stop_requested": False}
        }

        harness.model_combo.set_active_id("gpt-5.6-terra")
        harness.on_model_changed(harness.model_combo)
        harness.effort_combo.set_active_id("max")
        harness.on_effort_changed(harness.effort_combo)

        persisted = self.store.get_room(self.room_a["id"])
        self.assertEqual(
            (persisted["model"], persisted["effort"]),
            ("gpt-5.6-sol", "low"),
        )

    def test_programmatic_effort_rebuild_is_not_saved_as_a_user_edit(self) -> None:
        harness = ControlsHarness(self.store, self.room_b, self.models)
        harness.effort_combo.changed_callback = harness.on_effort_changed

        harness.update_effort_options()

        self.assertEqual(harness.effort_combo.get_active_id(), "low")
        self.assertIsNone(self.store.get_room(self.room_b["id"])["effort"])


class StorePersistenceTests(unittest.TestCase):
    def test_markdown_export_preserves_persian_and_image_only_messages(self) -> None:
        room = {"title": "گفتگوی تست"}
        rendered = conversation_markdown(
            room,
            [
                {
                    "role": "user",
                    "content": "",
                    "image_path": "/tmp/my image.png",
                    "created_at": 0,
                },
                {
                    "role": "assistant",
                    "content": "پاسخ **Markdown**",
                    "image_path": None,
                    "created_at": 1,
                },
            ],
        )
        self.assertIn("# گفتگوی تست", rendered)
        self.assertIn("![Attached image](</tmp/my image.png>)", rendered)
        self.assertIn("_Image message_", rendered)
        self.assertIn("پاسخ **Markdown**", rendered)

    def test_paged_messages_include_reply_preview_and_token_usage(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            store = Store(Path(tempdir))
            room = store.create_room("paged")
            first = store.add_message(room["id"], "assistant", "parent")
            for index in range(5):
                store.add_message(room["id"], "user", f"message {index}")
            last = store.add_message(
                room["id"], "assistant", "child", reply_to_id=first, token_usage=987
            )
            page = store.list_messages_with_replies(room["id"], limit=2)
            self.assertEqual([message["id"] for message in page], [last - 1, last])
            self.assertEqual(page[-1]["reply_content"], "parent")
            self.assertEqual(page[-1]["token_usage"], 987)
            self.assertTrue(store.has_messages_before(room["id"], page[0]["id"]))
            store.close()

    def test_paged_reply_includes_parent_image_for_image_only_preview(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            store = Store(Path(tempdir))
            room = store.create_room("image reply")
            parent = store.add_message(
                room["id"], "user", "", image_path="/tmp/screenshot.png"
            )
            child = store.add_message(
                room["id"], "assistant", "دیدم", reply_to_id=parent
            )
            page = store.list_messages_with_replies(room["id"])
            self.assertEqual(page[-1]["id"], child)
            self.assertEqual(page[-1]["reply_content"], "")
            self.assertEqual(page[-1]["reply_image_path"], "/tmp/screenshot.png")
            store.close()

    def test_thread_mapping_is_unique(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            store = Store(Path(tempdir))
            first = store.create_room("one")
            second = store.create_room("two")
            store.update_room(first["id"], thread_id="same-thread")
            with self.assertRaises(sqlite3.IntegrityError):
                store.update_room(second["id"], thread_id="same-thread")
            store.close()

    def test_room_summaries_search_rename_and_delete(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            store = Store(Path(tempdir))
            alpha = store.create_room("Alpha")
            beta = store.create_room("Beta")
            store.add_message(alpha["id"], "user", "searchable deployment note")
            store.add_message(beta["id"], "assistant", "unrelated")
            summaries = store.list_room_summaries("deployment")
            self.assertEqual([room["id"] for room in summaries], [alpha["id"]])
            self.assertEqual(summaries[0]["last_message"], "searchable deployment note")
            renamed = store.rename_room(alpha["id"], "  New   title  ")
            self.assertEqual(renamed["title"], "New title")
            self.assertTrue(store.delete_room(beta["id"]))
            self.assertIsNone(store.get_room(beta["id"]))
            self.assertFalse(store.delete_room(beta["id"]))
            store.close()

    def test_room_search_treats_sql_wildcards_as_literal_text(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            store = Store(Path(tempdir))
            percent = store.create_room("Progress 100%")
            underscore = store.create_room("release_candidate")
            ordinary = store.create_room("ordinary room")
            store.add_message(ordinary["id"], "user", "value is 50_percent")

            self.assertEqual(
                [room["id"] for room in store.list_room_summaries("100%")],
                [percent["id"]],
            )
            self.assertEqual(
                [room["id"] for room in store.list_room_summaries("_")],
                [ordinary["id"], underscore["id"]],
            )
            self.assertEqual(store.list_room_summaries("%missing"), [])
            store.close()

    def test_reply_duration_and_theme_persist(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            store = Store(Path(tempdir))
            room = store.create_room("reply")
            first = store.add_message(room["id"], "assistant", "original")
            second = store.add_message(
                room["id"], "user", "answer", reply_to_id=first,
                reply_quote="selected excerpt",
                duration_seconds=2.75,
            )
            store.update_room(
                room["id"],
                draft_reply_to_id=first,
                draft_reply_quote="draft excerpt",
            )
            store.set_setting("theme", "graphite")
            message = store.get_message(second)
            self.assertEqual(message["reply_to_id"], first)
            self.assertEqual(message["reply_quote"], "selected excerpt")
            self.assertEqual(message["duration_seconds"], 2.75)
            self.assertEqual(store.get_room(room["id"])["draft_reply_quote"], "draft excerpt")
            self.assertEqual(store.get_setting("theme"), "graphite")
            store.close()

    def test_old_database_schema_migrates_without_marking_history_unread(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            root.mkdir(exist_ok=True)
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
                INSERT INTO rooms VALUES(1, 'legacy', NULL, NULL, NULL, 10, 20);
                INSERT INTO messages VALUES(1, 1, 'assistant', 'old', NULL, 15);
                """
            )
            db.commit()
            db.close()
            store = Store(root)
            room = store.get_room(1)
            self.assertEqual(room["draft_text"], "")
            self.assertIsNone(room["draft_reply_quote"])
            self.assertIsNone(store.get_message(1)["reply_quote"])
            self.assertEqual(room["last_read_at"], 20)
            self.assertEqual(store.list_unread_room_ids(), set())
            store.close()

    def test_draft_updates_do_not_reorder_rooms(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            store = Store(Path(tempdir))
            room = store.create_room("stable order")
            before = store.get_room(room["id"])["updated_at"]
            store.update_room(room["id"], draft_text="typing")
            self.assertEqual(store.get_room(room["id"])["updated_at"], before)
            store.close()

    def test_unreferenced_imported_images_are_removed_safely(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            source = root / "source.png"
            source.write_bytes(b"fake image")
            store = Store(root / "data")
            room = store.create_room("image")
            imported = store.import_image(str(source))
            store.update_room(room["id"], draft_image_path=imported)
            self.assertFalse(store.remove_image_if_unused(imported))
            store.update_room(room["id"], draft_image_path=None)
            self.assertTrue(store.remove_image_if_unused(imported))
            self.assertFalse(Path(imported).exists())
            self.assertFalse(store.remove_image_if_unused(str(source)))
            store.close()

    def test_unread_assistant_response_survives_reopen(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir)
            store = Store(path)
            room = store.create_room("unread")
            store.add_message(room["id"], "assistant", "new answer")
            self.assertIn(room["id"], store.list_unread_room_ids())
            store.close()
            reopened = Store(path)
            self.assertIn(room["id"], reopened.list_unread_room_ids())
            reopened.mark_room_read(room["id"])
            self.assertNotIn(room["id"], reopened.list_unread_room_ids())
            reopened.close()
    def test_each_room_keeps_an_independent_text_and_image_draft(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir)
            store = Store(path)
            room_a = store.create_room("draft A")
            room_b = store.create_room("draft B")
            store.update_room(
                room_a["id"],
                draft_text="متن الف",
                draft_image_path="/tmp/a.png",
                draft_image_label="a.png",
            )
            store.update_room(
                room_b["id"],
                draft_text="متن ب",
                draft_image_path="/tmp/b.png",
                draft_image_label="b.png",
            )
            store.close()

            reopened = Store(path)
            persisted_a = reopened.get_room(room_a["id"])
            persisted_b = reopened.get_room(room_b["id"])
            self.assertEqual(
                (persisted_a["draft_text"], persisted_a["draft_image_path"], persisted_a["draft_image_label"]),
                ("متن الف", "/tmp/a.png", "a.png"),
            )
            self.assertEqual(
                (persisted_b["draft_text"], persisted_b["draft_image_path"], persisted_b["draft_image_label"]),
                ("متن ب", "/tmp/b.png", "b.png"),
            )
            reopened.close()

    def test_rooms_threads_models_and_messages_survive_reopen(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir)
            first = Store(path)
            room = first.create_room("اتاق پایدار")
            first.update_room(room["id"], thread_id="thread-1", model="model-1", effort="high")
            first.add_message(room["id"], "user", "سلام")
            first.close()

            reopened = Store(path)
            persisted = reopened.get_room(room["id"])
            self.assertEqual(persisted["thread_id"], "thread-1")
            self.assertEqual(persisted["model"], "model-1")
            self.assertEqual(persisted["effort"], "high")
            self.assertEqual(reopened.list_messages(room["id"])[0]["content"], "سلام")
            reopened.close()

    def test_concurrent_room_writes_remain_isolated(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            store = Store(Path(tempdir))
            rooms = [store.create_room(str(index)) for index in range(4)]

            def write(room: dict) -> None:
                for index in range(20):
                    store.add_message(room["id"], "user", f"{room['title']}:{index}")

            workers = [threading.Thread(target=write, args=(room,)) for room in rooms]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join()

            for room in rooms:
                messages = store.list_messages(room["id"])
                self.assertEqual(len(messages), 20)
                self.assertTrue(all(m["content"].startswith(f"{room['title']}:") for m in messages))
            store.close()


class BridgeFailureTests(unittest.TestCase):
    @staticmethod
    def _bare_backend() -> Backend:
        backend = Backend.__new__(Backend)
        backend.client = None
        backend.client_generation = 0
        backend.client_ready_generation = 0
        backend.client_lock = threading.RLock()
        backend.state_lock = threading.RLock()
        backend.buffered_events = []
        backend.initialize_result = {}
        backend.server_initialized = False
        backend.pending = {}
        backend.next_request_id = 0
        backend.initializing_id = None
        return backend

    def test_broken_socket_drains_pending_errback_without_raising(self) -> None:
        dispatched = []
        bridge = CodexBridge(
            dispatch=lambda callback, *args: callback(*args),
            on_event=lambda method, params: dispatched.append((method, params)),
        )
        local, peer = socket.socketpair()
        bridge.socket = local
        peer.close()
        errors = []
        bridge.request("test/method", callback=lambda _result: None, errback=errors.append)
        self.assertEqual(bridge._pending, {})
        self.assertEqual(len(errors), 1)
        self.assertTrue(errors[0]["transient"])
        self.assertEqual(errors[0]["code"], "transport_disconnected")
        self.assertEqual(dispatched[0][0], "bridge/closed")
        bridge.close()

    def test_backend_replays_buffer_before_live_delivery(self) -> None:
        backend = self._bare_backend()
        backend.client, peer = socket.socketpair()
        backend.client_generation = 1
        backend.send_event(
            {"id": 91, "method": "approval/request", "params": {"n": 1}}
        )
        self.assertEqual(len(backend.buffered_events), 1)
        self.assertTrue(
            backend.finish_client_initialization({"id": 7, "result": {}}, 1)
        )
        backend.send_event({"method": "event/two", "params": {"n": 2}})
        reader = peer.makefile("r", encoding="utf-8")
        received = [json.loads(reader.readline()) for _ in range(3)]
        self.assertEqual(received[0]["id"], 7)
        self.assertEqual(received[1]["method"], "approval/request")
        self.assertEqual(received[1]["id"], 91)
        self.assertEqual(received[2]["method"], "event/two")
        reader.close()
        peer.close()
        backend.client.close()

    def test_backend_buffer_never_drops_approval_or_orphan_result(self) -> None:
        backend = self._bare_backend()
        backend.buffer_event({"id": 8, "method": "approval/request"})
        backend.buffer_event({"method": "backend/orphanResponse", "params": {}})
        for index in range(1005):
            backend.buffer_event({"method": "delta", "params": {"n": index}})
        self.assertLessEqual(len(backend.buffered_events), 1000)
        self.assertTrue(any(event.get("id") == 8 for event in backend.buffered_events))
        self.assertTrue(
            any(
                event.get("method") == "backend/orphanResponse"
                for event in backend.buffered_events
            )
        )

    def test_backend_buffer_preserves_terminal_events_under_pressure(self) -> None:
        backend = self._bare_backend()
        terminal = {"method": "turn/completed", "params": {"turnId": "done"}}
        backend.buffer_event(terminal)
        for index in range(1100):
            backend.buffer_event({"method": "noise", "params": {"n": index}})
        self.assertIn(terminal, backend.buffered_events)
        self.assertLessEqual(len(backend.buffered_events), 1000)

    def test_backend_requeues_entire_unsent_replay_suffix_in_order(self) -> None:
        backend = self._bare_backend()
        backend.client = object()
        backend.client_generation = 3
        backend.buffered_events = [
            {"method": "event/one"},
            {"method": "event/two"},
            {"method": "turn/completed"},
        ]
        attempts = []

        def flaky_send(message, _generation=None):
            attempts.append(message)
            return len(attempts) < 3

        backend.send_client = flaky_send
        self.assertFalse(
            backend.finish_client_initialization({"id": 1, "result": {}}, 3)
        )
        self.assertEqual(
            [event["method"] for event in backend.buffered_events],
            ["event/two", "turn/completed"],
        )

    def test_bridge_request_timeout_clears_pending_and_reports_method(self) -> None:
        dispatched = []
        bridge = CodexBridge(
            dispatch=lambda callback, *args: callback(*args),
            on_event=lambda *_args: None,
        )
        bridge._pending[5] = (None, dispatched.append)
        bridge._expire_request(5, "thread/start")
        self.assertEqual(bridge._pending, {})
        self.assertIn("thread/start", dispatched[0]["message"])

    def test_initialized_notification_reaches_server_only_once(self) -> None:
        backend = self._bare_backend()
        forwarded = []
        backend.send_process = forwarded.append
        for _attempt in range(2):
            server_side, client_side = socket.socketpair()
            worker = threading.Thread(
                target=backend.handle_client, args=(server_side,), daemon=True
            )
            worker.start()
            client_side.sendall(b'{"method":"initialized","params":{}}\n')
            client_side.shutdown(socket.SHUT_WR)
            worker.join(timeout=1)
            client_side.close()
        self.assertEqual(
            [message["method"] for message in forwarded], ["initialized"]
        )


class MarkdownTests(unittest.TestCase):
    def test_activity_output_removes_terminal_escape_sequences(self) -> None:
        self.assertEqual(clean_activity_text("\x1b[31mERROR\x1b[0m\nnext"), "ERROR next")

    def test_fenced_cpp_block_is_separated_from_prose(self) -> None:
        blocks = split_blocks("متن\n```cpp\nint main() {}\n```\nپایان")
        self.assertEqual(blocks[0], ("prose", "", "متن"))
        self.assertEqual(blocks[1], ("code", "cpp", "int main() {}"))
        self.assertEqual(blocks[2], ("prose", "", "پایان"))

    def test_inline_markdown_is_escaped_and_formatted(self) -> None:
        markup = inline_markup("**bold** <script> `x < y`")
        self.assertIn("<b>bold</b>", markup)
        self.assertIn("&lt;script&gt;", markup)
        self.assertNotIn("<script>", markup)
        self.assertIn("monospace", markup)
        self.assertIn("<s>old</s>", inline_markup("~~old~~"))
        persian_link = inline_markup(
            "[مطالعه بیشتر](https://fa.wikipedia.org/wiki/یادگیری)"
        )
        self.assertIn('<a href="https://fa.wikipedia.org/wiki/یادگیری">', persian_link)

    def test_direction_detection_handles_persian_and_code(self) -> None:
        self.assertTrue(is_rtl("این یک متن فارسی است"))
        self.assertFalse(is_rtl("const value = 42;"))

    def test_composer_switches_back_to_rtl_after_english_prefix(self) -> None:
        self.assertTrue(composer_is_rtl(""))
        self.assertFalse(composer_is_rtl("Spark"))
        self.assertTrue(composer_is_rtl("Spark این ادامهٔ فارسی است"))
        self.assertTrue(composer_is_rtl("English heading\nادامه فارسی"))

    def test_ltr_identifiers_are_isolated_inside_persian_prose(self) -> None:
        markup = inline_markup("برنامه Spark با exit code 0 تمام شد")
        self.assertIn("\u2066Spark\u2069", markup)
        self.assertIn("\u2066exit code 0\u2069", markup)
        self.assertIn("\u2066value_name\u2069", inline_markup("مقدار value_name است"))

    def test_prose_blocks_allow_latin_prefix_before_persian_continuation(self) -> None:
        view = MarkdownView("توضیح فارسی\n\nSpark یک پردازش جداست")
        prose_labels = view.get_children()
        self.assertEqual(len(prose_labels), 2)
        self.assertEqual(
            [label.get_direction().value_nick for label in prose_labels],
            ["rtl", "rtl"],
        )
        self.assertTrue(all(label.get_hexpand() for label in prose_labels))
        self.assertTrue(all(label.get_xalign() == 0.0 for label in prose_labels))

    def test_markdown_table_is_detected(self) -> None:
        blocks = split_blocks("| نام | مقدار |\n| --- | ---: |\n| تست | 42 |")
        self.assertEqual(blocks, [("table", "", "| نام | مقدار |\n| تست | 42 |")])


if __name__ == "__main__":
    unittest.main()
