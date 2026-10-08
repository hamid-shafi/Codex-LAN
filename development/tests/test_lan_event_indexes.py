"""CODEX: Schema regressions for hot event lookups."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from project_layout import configure_imports, source_file
configure_imports(__file__)

from codex_lan.store import Store


class EventIndexTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)
        self.store = Store(self.path)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_existing_database_adds_event_run_kind_index_idempotently(self):
        with self.store.db:
            self.store.db.execute("DROP INDEX event_run_kind_id")
        self.store.close()

        self.store = Store(self.path)
        indexes = {
            row["name"]: row["sql"]
            for row in self.store.db.execute(
                "SELECT name,sql FROM sqlite_master WHERE type='index' AND tbl_name='events'"
            )
        }
        self.assertIn("event_run_kind_id", indexes)
        self.assertIn("events(run_id, kind, id)", indexes["event_run_kind_id"])

        self.store.close()
        self.store = Store(self.path)
        count = self.store.db.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='index' AND name='event_run_kind_id'"
        ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_latest_activity_lookup_uses_index_without_temporary_sort(self):
        plan = [
            row["detail"]
            for row in self.store.db.execute(
                "EXPLAIN QUERY PLAN SELECT payload FROM events "
                "WHERE run_id=? AND kind='activity' ORDER BY id DESC LIMIT 1",
                ("run-1",),
            )
        ]
        details = "\n".join(plan)
        self.assertIn("event_run_kind_id", details)
        self.assertNotIn("USE TEMP B-TREE", details.upper())


if __name__ == "__main__":
    unittest.main()
