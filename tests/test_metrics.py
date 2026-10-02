"""Focused token-accounting and elapsed-time formatting tests."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import (  # noqa: E402
    MainWindow,
    MessageBubble,
    format_duration,
    format_token_usage,
    token_usage_snapshot,
    token_usage_tooltip,
    update_turn_token_usage,
)


class TokenUsageTests(unittest.TestCase):
    class EventBubble:
        def __init__(self) -> None:
            self.values = []

        def set_token_usage(self, value: int) -> None:
            self.values.append(value)

    @staticmethod
    def event_controller(state, totals=None):
        controller_type = type(
            "MetricsController",
            (),
            {
                "on_codex_event": MainWindow.on_codex_event,
                "_state_for_event": MainWindow._state_for_event,
                "_state_for_usage_event": MainWindow._state_for_usage_event,
            },
        )
        controller = controller_type()
        controller._closing = False
        controller.branch_controller = None
        controller.room_turns = {1: state}
        controller._thread_token_totals = totals or {}
        return controller

    def test_v2_cumulative_snapshots_produce_turn_local_total(self) -> None:
        state = {"thread_id": "thread-1"}
        thread_totals = {}
        first = {
            "threadId": "thread-1",
            "turnId": "turn-1",
            "tokenUsage": {
                "total": {"totalTokens": 1_000},
                "last": {"totalTokens": 200},
            },
        }
        self.assertEqual(
            update_turn_token_usage(state, first, thread_totals), 200
        )
        # A replay of the same cumulative event is a snapshot, not another 200.
        self.assertEqual(
            update_turn_token_usage(state, first, thread_totals), 200
        )
        second = {
            "threadId": "thread-1",
            "turnId": "turn-1",
            "tokenUsage": {
                "total": {"totalTokens": 1_250},
                "last": {"totalTokens": 250},
            },
        }
        self.assertEqual(
            update_turn_token_usage(state, second, thread_totals), 450
        )
        self.assertEqual(thread_totals, {"thread-1": 1_250})

    def test_known_thread_baseline_excludes_previous_turns(self) -> None:
        state = {"thread_id": "thread-1"}
        thread_totals = {"thread-1": 4_000}
        params = {
            "threadId": "thread-1",
            "tokenUsage": {
                "total": {"totalTokens": 4_321},
                "last": {"totalTokens": 321},
            },
        }
        self.assertEqual(
            update_turn_token_usage(state, params, thread_totals), 321
        )

    def test_bare_lifetime_total_is_not_mislabeled_as_turn_usage(self) -> None:
        state = {"thread_id": "thread-1"}
        totals = {}
        value = update_turn_token_usage(
            state,
            {
                "threadId": "thread-1",
                "tokenUsage": {"total": {"totalTokens": 9_999}},
            },
            totals,
        )
        self.assertIsNone(value)
        self.assertNotIn("token_usage", state)
        self.assertEqual(totals["thread-1"], 9_999)

    def test_last_only_fallback_replaces_instead_of_summing(self) -> None:
        state = {"thread_id": "legacy"}
        totals = {}
        event = {
            "threadId": "legacy",
            "tokenUsage": {"last": {"totalTokens": 120}},
        }
        self.assertEqual(update_turn_token_usage(state, event, totals), 120)
        self.assertEqual(update_turn_token_usage(state, event, totals), 120)

    def test_raw_response_ids_are_deduplicated_until_cumulative_arrives(self) -> None:
        state = {"thread_id": "thread-1"}
        totals = {}
        first = {
            "threadId": "thread-1",
            "responseId": "response-1",
            "usage": {
                "inputTokens": 100,
                "cachedInputTokens": 80,
                "outputTokens": 20,
                "reasoningOutputTokens": 10,
            },
        }
        self.assertEqual(
            update_turn_token_usage(state, first, totals, raw_response=True),
            120,
        )
        self.assertEqual(
            update_turn_token_usage(state, first, totals, raw_response=True),
            120,
        )
        second = {
            "threadId": "thread-1",
            "responseId": "response-2",
            "usage": {"inputTokens": 30, "outputTokens": 5},
        }
        self.assertEqual(
            update_turn_token_usage(state, second, totals, raw_response=True),
            155,
        )
        canonical = {
            "threadId": "thread-1",
            "tokenUsage": {
                "total": {"totalTokens": 1_055},
                "last": {"totalTokens": 35},
            },
        }
        self.assertEqual(
            update_turn_token_usage(state, canonical, totals), 155
        )
        self.assertEqual(
            update_turn_token_usage(state, canonical, totals), 155
        )

    def test_legacy_snake_case_and_direct_shapes(self) -> None:
        snapshot = token_usage_snapshot(
            {
                "info": {
                    "total_token_usage": {"total_tokens": 800},
                    "last_token_usage": {"total_tokens": 75},
                }
            }
        )
        self.assertEqual(snapshot["cumulative"], 800)
        self.assertEqual(snapshot["last"], 75)
        raw = token_usage_snapshot(
            {"usage": {"input_tokens": 20, "output_tokens": 7}}
        )
        self.assertEqual(raw["direct"], 27)

    def test_delayed_older_cumulative_snapshot_cannot_roll_back_total(self) -> None:
        state = {
            "thread_id": "thread-1",
            "_token_usage_baseline": 800,
            "_token_usage_authoritative": True,
            "token_usage": 450,
        }
        totals = {"thread-1": 1_250}
        stale = {
            "threadId": "thread-1",
            "tokenUsage": {
                "total": {"totalTokens": 1_000},
                "last": {"totalTokens": 200},
            },
        }
        self.assertEqual(update_turn_token_usage(state, stale, totals), 450)
        self.assertEqual(totals["thread-1"], 1_250)

    def test_restored_usage_becomes_baseline_without_claiming_pending_turn(self) -> None:
        bubble = self.EventBubble()
        state = {
            "thread_id": "thread-1",
            "turn_id": None,
            "bubble": bubble,
        }
        controller = self.event_controller(state)
        restored = {
            "threadId": "thread-1",
            "turnId": "old-turn",
            "tokenUsage": {
                "total": {"totalTokens": 4_000},
                "last": {"totalTokens": 100},
            },
        }
        self.assertFalse(
            controller.on_codex_event("thread/tokenUsage/updated", restored)
        )
        self.assertIsNone(state["turn_id"])
        self.assertNotIn("token_usage", state)
        self.assertEqual(controller._thread_token_totals["thread-1"], 4_000)

        state["turn_id"] = "new-turn"
        current = {
            "threadId": "thread-1",
            "turnId": "new-turn",
            "tokenUsage": {
                "total": {"totalTokens": 4_321},
                "last": {"totalTokens": 321},
            },
        }
        controller.on_codex_event("thread/tokenUsage/updated", current)
        self.assertEqual(state["token_usage"], 321)
        self.assertEqual(bubble.values, [321])

    def test_event_handler_deduplicates_raw_response_before_canonical_total(self) -> None:
        bubble = self.EventBubble()
        state = {
            "thread_id": "thread-1",
            "turn_id": "turn-1",
            "bubble": bubble,
        }
        controller = self.event_controller(state)
        raw = {
            "threadId": "thread-1",
            "turnId": "turn-1",
            "responseId": "response-1",
            "usage": {"totalTokens": 40},
        }
        controller.on_codex_event("rawResponse/completed", raw)
        controller.on_codex_event("rawResponse/completed", raw)
        canonical = {
            "threadId": "thread-1",
            "turnId": "turn-1",
            "tokenUsage": {
                "total": {"totalTokens": 940},
                "last": {"totalTokens": 40},
            },
        }
        controller.on_codex_event("thread/tokenUsage/updated", canonical)
        self.assertEqual(state["token_usage"], 40)
        self.assertEqual(bubble.values, [40, 40, 40])

    def test_first_replayed_usage_can_restore_an_orphaned_turn(self) -> None:
        bubble = self.EventBubble()
        recovered = {
            "thread_id": "thread-1",
            "turn_id": "turn-1",
            "bubble": bubble,
        }
        controller = self.event_controller(recovered)
        controller.room_turns = {}
        routed = []

        def restore(params):
            routed.append(params["turnId"])
            controller.room_turns[1] = recovered
            return recovered

        controller._state_for_event = restore
        event = {
            "threadId": "thread-1",
            "turnId": "turn-1",
            "tokenUsage": {
                "total": {"totalTokens": 640},
                "last": {"totalTokens": 140},
            },
        }
        controller.on_codex_event("thread/tokenUsage/updated", event)
        self.assertEqual(routed, ["turn-1"])
        self.assertEqual(recovered["token_usage"], 140)
        self.assertEqual(bubble.values, [140])


class DurationTests(unittest.TestCase):
    def test_large_processed_token_totals_are_compact_and_explicit(self) -> None:
        self.assertEqual(format_token_usage(1_842), "1,842 total tokens")
        self.assertEqual(format_token_usage(168_246), "168k total tokens")
        self.assertEqual(format_token_usage(3_837_247), "3.8M total tokens")
        self.assertIn("cached context", token_usage_tooltip(3_837_247))
        self.assertIn("3,837,247", token_usage_tooltip(3_837_247))

    def test_bubble_explains_total_processed_token_scope(self) -> None:
        bubble = MessageBubble("assistant", "Done", token_usage=3_837_247)
        self.assertEqual(bubble.usage_label.get_text(), "3.8M total tokens")
        self.assertIn("every model/tool step", bubble.usage_label.get_tooltip_text())
        bubble.set_token_usage(168_246)
        self.assertEqual(bubble.usage_label.get_text(), "168k total tokens")

    def test_format_duration_uses_minutes_and_seconds_after_one_minute(self) -> None:
        self.assertEqual(format_duration(13.4), "13.4s")
        self.assertEqual(format_duration(59.6), "59.6s")
        self.assertEqual(format_duration(60), "1m 0s")
        self.assertEqual(format_duration(81), "1m 21s")
        self.assertEqual(format_duration(121.4), "2m 1s")

    def test_streaming_and_completed_bubble_use_the_same_clear_format(self) -> None:
        bubble = MessageBubble("assistant", "", streaming=True)
        bubble.set_elapsed(81)
        self.assertEqual(bubble.timer_label.get_text(), "Running • 1m 21s")
        bubble.set_elapsed(81, stopping=True)
        self.assertEqual(bubble.timer_label.get_text(), "Stopping • 1m 21s")
        bubble.hide_timer(81)
        self.assertEqual(bubble.duration_label.get_text(), "Thought for 1m 21s")

        restored = MessageBubble(
            "assistant", "Done", duration_seconds=81.2
        )
        self.assertEqual(
            restored.duration_label.get_text(), "Thought for 1m 21s"
        )

    def test_activity_presentation_pauses_spinner_during_reconnect(self) -> None:
        bubble = MessageBubble("assistant", "", streaming=True)
        bubble.set_elapsed(81)

        bubble.set_activity_presentation("reconnecting")

        self.assertEqual(bubble.activity_state_label.get_text(), "RECONNECTING")
        self.assertFalse(bubble.activity_spinner.get_property("active"))
        self.assertEqual(bubble.timer_label.get_text(), "Reconnecting • 1m 21s")

        bubble.set_activity_presentation("restoring")

        self.assertEqual(bubble.activity_state_label.get_text(), "RESTORING")
        self.assertTrue(bubble.activity_spinner.get_property("active"))
        self.assertEqual(bubble.timer_label.get_text(), "Restoring • 1m 21s")

        bubble.set_activity_presentation("stopping")
        self.assertEqual(bubble.activity_state_label.get_text(), "STOPPING")
        self.assertFalse(bubble.activity_spinner.get_property("active"))
        self.assertEqual(bubble.timer_label.get_text(), "Stopping • 1m 21s")

        bubble.hide_timer(81)
        self.assertEqual(bubble.activity_state_label.get_text(), "DONE")
        self.assertIn("no activity updates", bubble.activity_label.get_text())

    def test_room_timer_uses_clear_minute_format(self) -> None:
        class Bubble:
            def set_elapsed(self, seconds, stopping=False):
                self.value = (seconds, stopping)

        class Button:
            def set_status(self, text):
                self.text = text

        bubble = Bubble()
        button = Button()
        controller = type(
            "Controller",
            (),
            {
                "room_turns": {
                    1: {
                        "started_at": 0.0,
                        "bubble": bubble,
                        "stop_requested": False,
                    }
                },
                "room_buttons": {1: button},
                "room_titles": {1: "Chat"},
            },
        )()
        with mock.patch("app.time.monotonic", return_value=81.0):
            self.assertTrue(MainWindow._update_turn_timers(controller))
        self.assertEqual(bubble.value, (81, False))
        self.assertEqual(button.text, "● Responding • 1m 21s")


if __name__ == "__main__":
    unittest.main()
