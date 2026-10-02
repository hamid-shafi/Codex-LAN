"""Pure-data regression coverage for the live thinking/activity pipeline."""

from __future__ import annotations

import sys
import threading
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from activity_model import (
    ActivityFeed,
    activity_fragments,
    merge_activity_delta,
    normalize_activity_text,
)
from backend import Backend


class ActivityTextTests(unittest.TestCase):
    def test_markdown_summary_becomes_individual_readable_checkpoints(self) -> None:
        raw = "**Analyzing architecture****Checking the cache**"
        self.assertEqual(
            activity_fragments(raw),
            ["Analyzing architecture", "Checking the cache"],
        )
        self.assertEqual(
            normalize_activity_text(raw),
            "Analyzing architecture Checking the cache",
        )

    def test_partial_bold_checkpoint_waits_for_its_closing_marker(self) -> None:
        self.assertEqual(activity_fragments("**Inspecting the current"), [])
        self.assertEqual(
            activity_fragments("**Inspecting the current code**"),
            ["Inspecting the current code"],
        )

    def test_snapshot_and_delta_forms_merge_without_repeating_text(self) -> None:
        first = "**Read the project**"
        full = "**Read the project****Inspect tests**"
        self.assertEqual(merge_activity_delta(first, full), full)
        self.assertEqual(merge_activity_delta(full, full), full)
        self.assertEqual(
            merge_activity_delta("inspect source", "source and tests"),
            "inspect source and tests",
        )


class ActivityFeedTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = 0.0
        self.feed = ActivityFeed(now=self._now, max_details=4, max_trace=5)

    def _now(self) -> float:
        self.clock += 1.0
        return self.clock

    def test_reasoning_replay_never_becomes_one_giant_markdown_entry(self) -> None:
        delta = "**Analyzing architecture****Checking storage****Planning fix**"
        first = self.feed.ingest("item/reasoning/summaryTextDelta", {"delta": delta})
        second = self.feed.ingest("item/reasoning/summaryTextDelta", {"delta": delta})

        self.assertEqual(first.overview, "Planning fix: 3 checkpoints")
        self.assertEqual(second.overview, "Planning fix: 3 checkpoints")
        self.assertEqual(
            [entry.title for entry in second.trace],
            ["Analyzing architecture", "Checking storage", "Planning fix"],
        )
        self.assertTrue(all("**" not in entry.text for entry in second.entries))
        self.assertTrue(all(len(entry.text) < 300 for entry in second.entries))

    def test_snapshot_update_adds_only_the_new_reasoning_checkpoint(self) -> None:
        self.feed.ingest(
            "item/reasoning/summaryTextDelta", {"delta": "**Inspecting files**"}
        )
        snapshot = self.feed.ingest(
            "item/reasoning/summaryTextDelta",
            {"delta": "**Inspecting files****Reading tests**"},
        )
        self.assertEqual(
            [entry.title for entry in snapshot.trace],
            ["Inspecting files", "Reading tests"],
        )
        self.assertEqual(snapshot.overview, "Reading tests: 2 checkpoints")

    def test_item_lifecycle_groups_details_and_preserves_command_detail(self) -> None:
        started = self.feed.ingest(
            "item/started",
            {
                "item": {
                    "id": "cmd-1",
                    "type": "commandExecution",
                    "command": ["rg", "TODO", "src"],
                }
            },
        )
        self.assertEqual(started.overview, "Running a command: rg TODO src")
        output = self.feed.ingest(
            "item/commandExecution/outputDelta",
            {"itemId": "cmd-1", "delta": "src/a.py: TODO\nsrc/b.py: TODO"},
        )
        completed = self.feed.ingest(
            "item/completed",
            {"item": {"id": "cmd-1", "type": "commandExecution"}},
        )

        self.assertEqual(output.overview, "Running a command: rg TODO src")
        command = next(entry for entry in completed.details if entry.key == "item:cmd-1")
        self.assertEqual(command.status, "complete")
        self.assertEqual(command.detail, "rg TODO src")
        stage = next(stage for stage in completed.stages if stage.name == "Command")
        self.assertEqual([entry.level for entry in stage.entries], ["detail", "trace"])

    def test_plan_has_overview_detail_and_bounded_step_trace(self) -> None:
        snapshot = self.feed.ingest(
            "turn/plan/updated",
            {
                "plan": [
                    {"description": "Inspect current UI", "status": "completed"},
                    {"description": "Build activity model", "status": "in_progress"},
                    {"description": "Verify behavior", "status": "pending"},
                ],
                "explanation": "Prepare a safe incremental change.",
            },
        )
        self.assertEqual(snapshot.overview, "Planning 1/3: Build activity model")
        self.assertEqual(snapshot.details[0].level, "detail")
        self.assertEqual(snapshot.details[0].detail, "Build activity model")
        self.assertEqual(snapshot.trace[0].level, "trace")
        self.assertIn("Inspect current UI (completed)", snapshot.trace[0].detail)

    def test_trace_is_bounded_and_deduplicates_identical_output(self) -> None:
        for index in range(10):
            self.feed.ingest(
                "item/commandExecution/outputDelta",
                {"itemId": "cmd", "delta": f"line {index}"},
            )
        self.feed.ingest(
            "item/commandExecution/outputDelta",
            {"itemId": "cmd", "delta": "line 9"},
        )
        snapshot = self.feed.snapshot()
        self.assertEqual(len(snapshot.trace), 5)
        self.assertEqual([entry.detail for entry in snapshot.trace], [
            "line 5", "line 6", "line 7", "line 8", "line 9",
        ])
        self.assertEqual(snapshot.trace[-1].count, 2)

    def test_unknown_events_do_not_pollute_activity(self) -> None:
        self.assertIsNone(self.feed.ingest("thread/tokenUsage/updated", {}))
        self.assertEqual(self.feed.snapshot().overview, "Getting ready…")

    def test_connection_lifecycle_is_visible_without_erasing_prior_trace(self) -> None:
        self.feed.ingest(
            "item/reasoning/summaryTextDelta", {"delta": "**Inspecting state**"}
        )
        disconnected = self.feed.ingest("bridge/closed", {})
        restored = self.feed.ingest("bridge/reconnected", {})
        self.assertEqual(disconnected.overview, "Connection lost; reconnecting…")
        self.assertEqual(disconnected.overview_entry.status, "warning")
        self.assertEqual(restored.overview, "Reconnected; restoring the response…")
        self.assertEqual(
            [entry.title for entry in restored.trace], ["Inspecting state"]
        )


class BufferedActivityEventTests(unittest.TestCase):
    @staticmethod
    def _backend() -> Backend:
        backend = Backend.__new__(Backend)
        backend.state_lock = threading.RLock()
        backend.buffered_events = []
        return backend

    def test_detached_reasoning_snapshots_coalesce_without_repeating_markdown(self) -> None:
        backend = self._backend()
        first = "**Inspect project**"
        full = "**Inspect project****Verify tests**"
        backend.buffer_event(
            {
                "method": "item/reasoning/summaryTextDelta",
                "params": {"threadId": "t", "turnId": "u", "itemId": "r", "delta": first},
            }
        )
        backend.buffer_event(
            {
                "method": "item/reasoning/summaryTextDelta",
                "params": {"threadId": "t", "turnId": "u", "itemId": "r", "delta": full},
            }
        )
        self.assertEqual(len(backend.buffered_events), 1)
        self.assertEqual(backend.buffered_events[0]["params"]["delta"], full)

    def test_detached_response_deltas_remain_lossless_when_text_repeats(self) -> None:
        backend = self._backend()
        for delta in ("ha ", "ha"):
            backend.buffer_event(
                {
                    "method": "item/agentMessage/delta",
                    "params": {"threadId": "t", "turnId": "u", "itemId": "m", "delta": delta},
                }
            )
        self.assertEqual(len(backend.buffered_events), 1)
        self.assertEqual(backend.buffered_events[0]["params"]["delta"], "ha ha")

    def test_detached_plan_snapshots_keep_only_the_latest_matching_turn(self) -> None:
        backend = self._backend()
        for completed in (0, 1):
            backend.buffer_event(
                {
                    "method": "turn/plan/updated",
                    "params": {"threadId": "t", "turnId": "u", "plan": [{"status": "completed"}] * completed},
                }
            )
        self.assertEqual(len(backend.buffered_events), 1)
        self.assertEqual(len(backend.buffered_events[0]["params"]["plan"]), 1)


if __name__ == "__main__":
    unittest.main()
