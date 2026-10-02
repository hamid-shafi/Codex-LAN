"""Headless presentation-adapter checks for the structured activity panel."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from activity_model import ActivityFeed  # noqa: E402
from app import MessageBubble  # noqa: E402


class ActivityPanelAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.panel = MessageBubble.__new__(MessageBubble)
        self.feed = ActivityFeed()

    def _snapshot(self):
        self.feed.ingest(
            "item/started",
            {
                "item": {
                    "id": "command-1",
                    "type": "commandExecution",
                    "command": ["rg", "TODO", "src"],
                }
            },
        )
        self.feed.ingest(
            "item/commandExecution/outputDelta",
            {"itemId": "command-1", "delta": "src/app.py: TODO"},
        )
        return self.feed.ingest(
            "item/reasoning/summaryTextDelta",
            {"delta": "**Inspecting UI****Planning verification**"},
        )

    def test_stage_chips_use_activity_stage_name_and_status(self) -> None:
        snapshot = self._snapshot()
        stages = self.panel._activity_stages(snapshot)

        self.assertEqual(
            [(stage["stage"], stage["status"]) for stage in stages],
            [("Command", "active"), ("Reasoning", "active")],
        )

    def test_object_and_serialized_snapshot_keep_detail_levels_separate(self) -> None:
        snapshot = self._snapshot()
        for source in (snapshot, snapshot.as_dict()):
            details = self.panel._activity_entries(source, "details")
            trace = self.panel._activity_entries(source, "trace")

            self.assertEqual([entry["level"] for entry in details], ["detail"])
            self.assertEqual(
                [entry["title"] for entry in trace],
                ["Output", "Inspecting UI", "Planning verification"],
            )
            self.assertTrue(all("**" not in entry["title"] for entry in trace))


if __name__ == "__main__":
    unittest.main()
