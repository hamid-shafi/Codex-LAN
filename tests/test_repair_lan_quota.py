"""CODEX: Quota recovery copies vetted calibration metadata without rolling chat data backward."""

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from repair_lan_quota import restore


class RepairLanQuotaTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.current, self.saved = root / "current.sqlite3", root / "saved.sqlite3"

    @staticmethod
    def write(path, state, marker):
        with sqlite3.connect(path) as db:
            db.execute("CREATE TABLE quota_state(id INTEGER PRIMARY KEY, payload TEXT NOT NULL)")
            db.execute("CREATE TABLE quota_deltas(id INTEGER PRIMARY KEY, created_at REAL NOT NULL)")
            db.execute("CREATE TABLE messages(id INTEGER PRIMARY KEY, content TEXT NOT NULL)")
            db.execute("INSERT INTO quota_state VALUES(1,?)", (json.dumps(state),))
            db.execute("INSERT INTO quota_deltas VALUES(7,?)", (state.get("observed_at", 0),))
            db.execute("INSERT INTO messages VALUES(1,?)", (marker,))

    @staticmethod
    def read(path):
        with sqlite3.connect(path) as db:
            return json.loads(db.execute("SELECT payload FROM quota_state WHERE id=1").fetchone()[0]), db.execute(
                "SELECT content FROM messages WHERE id=1"
            ).fetchone()[0]

    def states(self):
        saved = {"limit_id": "codex", "starts_at": 100, "ends_at": 800,
                 "observed_at": 350, "used_percent": 28, "capacity_credits_micros": 11316346500,
                 "capacity_observed_at": 350, "calibration_delta_percent": 8,
                 "calibration_delta_credits_micros": 905307720}
        current = {"limit_id": "codex", "starts_at": 50, "ends_at": 750,
                   "observed_at": 500, "baseline_percent": 1, "used_percent": 2,
                   "capacity_credits_micros": None}
        return current, saved

    def test_restores_only_capacity_metadata_and_preserves_current_data(self):
        current, saved = self.states()
        self.write(self.current, current, "new chat")
        self.write(self.saved, saved, "old chat")
        result = restore(self.current, self.saved, now=600)
        actual, marker = self.read(self.current)
        self.assertTrue(result["changed"])
        self.assertEqual(marker, "new chat")
        self.assertEqual(actual["baseline_percent"], 1)
        self.assertEqual(actual["used_percent"], 2)
        self.assertEqual(actual["capacity_credits_micros"], saved["capacity_credits_micros"])
        self.assertEqual(actual["capacity_observed_at"], saved["capacity_observed_at"])
        self.assertEqual(actual["calibration_delta_credits_micros"], 905307720)
        self.assertTrue(actual["carried_forward"])

    def test_existing_valid_capacity_is_never_overwritten(self):
        current, saved = self.states()
        current["capacity_credits_micros"] = 99
        self.write(self.current, current, "new chat")
        self.write(self.saved, saved, "old chat")
        result = restore(self.current, self.saved, now=600)
        actual, _ = self.read(self.current)
        self.assertFalse(result["changed"])
        self.assertEqual(actual["capacity_credits_micros"], 99)

    def test_explicit_replacement_uses_trusted_capacity_and_new_checkpoint(self):
        current, saved = self.states()
        current.update(capacity_credits_micros=30845242500, used_percent=31,
                       baseline_percent=29, baseline_event_id=1)
        self.write(self.current, current, "new chat")
        self.write(self.saved, saved, "old chat")
        result = restore(self.current, self.saved, now=600, replace_existing=True,
                         repair_id="usage-analysis-v1")
        actual, marker = self.read(self.current)
        self.assertTrue(result["changed"])
        self.assertEqual(marker, "new chat")
        self.assertEqual(actual["capacity_credits_micros"], 11316346500)
        self.assertEqual(actual["baseline_percent"], 31)
        self.assertEqual(actual["baseline_at"], 500)
        self.assertEqual(actual["baseline_event_id"], 7)
        self.assertEqual(actual["last_calibration_result"], "restored_trusted_checkpoint")
        self.assertEqual(actual["applied_repairs"], ["usage-analysis-v1"])
        self.assertTrue(actual["carried_forward"])
        actual["capacity_credits_micros"] = 12000000000
        with sqlite3.connect(self.current) as db:
            db.execute("UPDATE quota_state SET payload=? WHERE id=1", (json.dumps(actual),))
        repeated = restore(self.current, self.saved, now=610, replace_existing=True,
                           repair_id="usage-analysis-v1")
        after_repeat, _ = self.read(self.current)
        self.assertFalse(repeated["changed"])
        self.assertEqual(after_repeat["capacity_credits_micros"], 12000000000)

    def test_rejects_nonoverlapping_or_expired_windows(self):
        current, saved = self.states()
        current.update(starts_at=900, ends_at=1600, observed_at=1000)
        self.write(self.current, current, "new chat")
        self.write(self.saved, saved, "old chat")
        with self.assertRaisesRegex(RuntimeError, "do not overlap"):
            restore(self.current, self.saved, now=1000)
        actual, marker = self.read(self.current)
        self.assertIsNone(actual["capacity_credits_micros"])
        self.assertEqual(marker, "new chat")

    def test_explicit_adjacent_reset_carries_capacity_and_checkpoints_current_window(self):
        week = 7 * 24 * 60 * 60
        gap = 31 * 60
        saved = {"limit_id": "codex", "starts_at": 100, "ends_at": 100 + week,
                 "observed_at": 350, "used_percent": 28, "capacity_credits_micros": 11316346500,
                 "capacity_observed_at": 350, "calibration_delta_percent": 8,
                 "calibration_delta_credits_micros": 905307720}
        current_start = saved["ends_at"] + gap
        current = {"limit_id": "codex", "starts_at": current_start,
                   "ends_at": current_start + week, "observed_at": current_start + 500,
                   "baseline_percent": 0, "used_percent": 1,
                   "capacity_credits_micros": None,
                   "pending_capacity_credits_micros": 99,
                   "pending_capacity_observed_at": current_start + 100}
        self.write(self.current, current, "new chat")
        self.write(self.saved, saved, "old chat")
        result = restore(self.current, self.saved, now=current_start + 600,
                         allow_adjacent_reset=True, repair_id="shifted-reset-v1")
        actual, marker = self.read(self.current)
        self.assertTrue(result["changed"])
        self.assertEqual(marker, "new chat")
        self.assertEqual((actual["starts_at"], actual["ends_at"]),
                         (current_start, current_start + week))
        self.assertEqual(actual["capacity_credits_micros"], saved["capacity_credits_micros"])
        self.assertEqual((actual["baseline_percent"], actual["baseline_at"], actual["baseline_event_id"]),
                         (1, current_start + 500, 7))
        self.assertEqual(actual["last_calibration_result"], "restored_adjacent_window")
        self.assertEqual(actual["applied_repairs"], ["shifted-reset-v1"])
        self.assertNotIn("pending_capacity_credits_micros", actual)
        self.assertTrue(actual["carried_forward"])
        repeated = restore(self.current, self.saved, now=current_start + 700,
                           allow_adjacent_reset=True, repair_id="shifted-reset-v1")
        self.assertFalse(repeated["changed"])

    def test_adjacent_reset_requires_opt_in_and_rejects_large_gap(self):
        week = 7 * 24 * 60 * 60
        saved = {"limit_id": "codex", "starts_at": 100, "ends_at": 100 + week,
                 "observed_at": 350, "used_percent": 28, "capacity_credits_micros": 11316346500,
                 "capacity_observed_at": 350}
        current_start = saved["ends_at"] + 31 * 60
        current = {"limit_id": "codex", "starts_at": current_start,
                   "ends_at": current_start + week, "observed_at": current_start + 500,
                   "baseline_percent": 0, "used_percent": 1, "capacity_credits_micros": None}
        self.write(self.current, current, "new chat")
        self.write(self.saved, saved, "old chat")
        with self.assertRaisesRegex(RuntimeError, "do not overlap"):
            restore(self.current, self.saved, now=current_start + 600)
        current.update(starts_at=saved["ends_at"] + 7 * 60 * 60,
                       ends_at=saved["ends_at"] + 7 * 60 * 60 + week,
                       observed_at=saved["ends_at"] + 7 * 60 * 60 + 500)
        with sqlite3.connect(self.current) as db:
            db.execute("UPDATE quota_state SET payload=? WHERE id=1", (json.dumps(current),))
            db.execute("UPDATE quota_deltas SET created_at=? WHERE id=7", (current["observed_at"],))
        with self.assertRaisesRegex(RuntimeError, "do not overlap"):
            restore(self.current, self.saved, now=current["observed_at"] + 100,
                    allow_adjacent_reset=True)


if __name__ == "__main__":
    unittest.main()
