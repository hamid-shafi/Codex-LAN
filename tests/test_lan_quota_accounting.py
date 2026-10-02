"""CODEX: High-resolution estimated account shares without rounded per-client quota attribution."""

import sqlite3
import unittest
from unittest import mock

import test_lan_store as fixture
from codex_lan.quota_accounting import (
    FRESH_SECONDS,
    QUOTA_EXHAUSTED_ERROR,
    RESET_ROLLOVER_DRIFT_SECONDS,
    WEEK_SECONDS,
)
from codex_lan.store import Store
from codex_lan.usage_estimator import estimate_usage


class QuotaAccountingTests(unittest.TestCase):
    approve = fixture.LanStoreTests.approve
    make_run = fixture.LanStoreTests.make_run

    def setUp(self):
        fixture.LanStoreTests.setUp(self)
        self.approve()
        self.start = 2_000_000_000
        self.now = self.start + 100
        self.clock = mock.patch("codex_lan.quota_accounting.time.time", side_effect=lambda: self.now)
        self.clock.start()
        self.addCleanup(self.clock.stop)

    def tearDown(self):
        fixture.LanStoreTests.tearDown(self)

    def room(self, owner=None, model="gpt-5.6-sol", speed="standard"):
        return self.store.create_room(owner or self.a_id, "Quota room", "/tmp/work", model=model, effort="high", speed=speed)

    def report(self, owner=None):
        return next(row for row in self.store.quota_report(self.admin_id)["devices"] if row["device_id"] == (owner or self.a_id))

    def observe(self, percent=10, end=None, **changes):
        self.now += 1
        envelope = {"available": True, "stale": False, "fetched_at": self.now,
                    "windows": [{"limit_id": "codex", "kind": "primary", "window_minutes": 10080,
                                 "resets_at": end or self.start + WEEK_SECONDS, "used_percent": percent}]}
        envelope.update(changes)
        self.store.observe_provider_usage(envelope)
        return envelope

    def credit_events(self, run_id):
        return [row[0] for row in self.store.db.execute("SELECT credits_micros FROM quota_deltas WHERE run_id=? ORDER BY id", (run_id,))]

    def test_unknown_usage_does_not_rescan_unrelated_deltas_for_every_run(self):
        self.observe()
        room = self.room()
        for index in range(30):
            run = self.make_run(room, request_id=f"history-{index}")["run"]
            self.store.record_usage(run["id"], 100, 20)
            self.store.finish_run(run["id"], "completed", "Done")
        other = self.make_run(self.room(self.b_id))["run"]
        with self.store.db:
            self.store.db.executemany(
                "INSERT INTO quota_deltas(run_id,owner_id,credits_micros,created_at) VALUES(?,?,?,?)",
                [(other["id"], self.b_id, 1, self.now)] * 4000,
            )
        callbacks = 0

        def instruction_budget():
            nonlocal callbacks
            callbacks += 1
            return callbacks >= 100

        self.store.db.set_progress_handler(instruction_budget, 1000)
        try:
            result = self.store.quota._unknown_usage(self.a_id, self.start, self.start + WEEK_SECONDS)
        finally:
            self.store.db.set_progress_handler(None, 0)
        self.assertEqual(result["unknown"], 0)

    def test_existing_database_adds_unknown_delta_index_on_reopen(self):
        with self.store.db:
            self.store.db.execute("DROP INDEX IF EXISTS quota_unknown_delta_run_window")
        self.store.close()
        self.store = Store(self.path)
        self.store.close()
        self.store = Store(self.path)
        plan = "\n".join(row["detail"] for row in self.store.db.execute(
            "EXPLAIN QUERY PLAN SELECT 1 FROM quota_deltas "
            "WHERE run_id=? AND credits_micros IS NULL AND created_at>=? AND created_at<?",
            ("run-id", self.start, self.start + WEEK_SECONDS),
        ))
        self.assertIn("quota_unknown_delta_run_window", plan)

    def calibrate_two_clients(self):
        self.observe(10)
        a, b = self.room(), self.room(self.b_id)
        run_a, run_b = self.make_run(a)["run"], self.make_run(b)["run"]
        self.now += 1
        for run in (run_a, run_b):
            self.store.record_usage(run["id"], 1_000_000, 0)
        self.observe(13)
        return a, b, run_a, run_b

    def test_new_run_configuration_is_snapshot_not_current_room(self):
        room = self.room(speed="fast")
        run = self.make_run(room)["run"]
        self.assertEqual((run["model"], run["effort"], run["speed"]), ("gpt-5.6-sol", "high", "fast"))
        self.store.update_room(self.a_id, room["id"], model="gpt-5.6-luna", speed="standard")
        self.assertEqual(self.store.get_run_internal(run["id"])["model"], "gpt-5.6-sol")
        actual = self.store.configure_run_usage(run["id"], "gpt-5.6-terra", "medium", "standard")
        self.assertEqual(actual["model"], "gpt-5.6-terra")
        self.store.record_usage(run["id"], 100, 10)
        with self.assertRaises(ValueError):
            self.store.configure_run_usage(run["id"], "gpt-5.6-luna", "low", "standard")
        self.store.update_run(run["id"], status="running")
        self.assertEqual(self.store.configure_run_usage(run["id"], "gpt-5.6-terra", "medium", "standard")["speed"], "standard")
        for speed in (None, True, "priority", "FAST", ""):
            with self.assertRaises(ValueError):
                self.store.update_room(self.a_id, room["id"], speed=speed)

    def test_migration_preserves_legacy_history_and_never_backfills_run_model(self):
        room = self.room()
        run = self.make_run(room)["run"]
        self.store.record_usage(run["id"], 100, 20)
        self.store.finish_run(run["id"], "completed", "History")
        self.store.set_device(self.admin_id, self.a_id, token_budget=12345)
        self.store.close()
        with sqlite3.connect(self.path / "server.sqlite3") as db:
            for table in ("quota_deltas", "quota_usage", "quota_state"):
                db.execute(f"DROP TABLE {table}")
            for table, fields in (("runs", ("model", "effort", "speed")), ("rooms", ("speed",)), ("devices", ("weekly_share_percent",))):
                for field in fields:
                    db.execute(f"ALTER TABLE {table} DROP COLUMN {field}")
        self.store = Store(self.path)
        self.store.close()
        self.store = Store(self.path)
        old = self.store.get_run_internal(run["id"])
        self.assertIsNone(old["model"])
        self.assertIsNone(old["speed"])
        self.assertEqual(self.store.get_room(self.a_id, room["id"])["speed"], "standard")
        self.assertEqual(self.store.get_device(self.a_id)["token_budget"], 12345)
        self.assertIsNone(self.store.get_device(self.a_id)["weekly_share_percent"])
        self.observe()
        self.assertEqual(self.report()["unknown_runs"], 1)
        message = self.store.list_messages(self.a_id, room["id"])[-1]
        self.assertEqual(message["content"], "History")
        self.assertEqual(message["token_usage"], 120)
        self.assertIsNone(message["estimated_usage_credits_micros"])
        with self.assertRaises(ValueError):
            self.store.configure_run_usage(run["id"], "gpt-5.6-sol", "high", "standard")

    def test_cumulative_precision_replay_and_negative_cache_correction(self):
        self.observe()
        room = self.room()
        run = self.make_run(room)["run"]
        for _ in range(3):
            self.store.record_usage(run["id"], 1000, 100, 0)
        self.assertEqual(self.credit_events(run["id"]), [150000])
        self.store.record_usage(run["id"], 1000, 100, 800)
        self.store.record_usage(run["id"], 900, 90, 700)
        self.assertEqual(self.credit_events(run["id"]), [150000, -72000])
        self.assertEqual(self.report()["spent_credits_micros"], 78000)
        ledger = self.store.db.execute("SELECT * FROM usage_ledger WHERE run_id=?", (run["id"],)).fetchone()
        self.assertEqual((ledger["input_tokens"], ledger["output_tokens"], ledger["cached_tokens"], ledger["total_tokens"]), (1000, 100, 800, 1100))
        self.store.finish_run(run["id"], "completed", "Done")
        self.assertEqual(self.store.list_messages(self.a_id, room["id"])[-1]["estimated_usage_credits_micros"], 78000)
        self.store.delete_room(self.a_id, room["id"])
        self.assertEqual(self.report()["spent_credits_micros"], 78000)
        tiny = self.make_run(self.room(model="gpt-5.6-luna"), request_id="tiny")["run"]
        for count in range(1, 11):
            self.store.record_usage(tiny["id"], count, 0, count)
        self.assertEqual(sum(self.credit_events(tiny["id"])), 5)

    def test_concurrent_clients_weighted_once_and_cap_edit_does_not_reset_spend(self):
        _, _, a, b = self.calibrate_two_clients()
        for owner in (self.a_id, self.b_id):
            self.store.set_device(self.admin_id, owner, weekly_share_percent=3)
        self.assertEqual(self.store.quota_report(self.admin_id)["calibration"]["status"], "estimated")
        for owner in (self.a_id, self.b_id):
            report = self.report(owner)
            self.assertEqual(report["spent_credits_micros"], 100_000_000)
            self.assertAlmostEqual(report["used_percent"], 50, places=5)
            self.assertTrue(report["limit_active"])
        self.observe(13)
        self.assertEqual(sum(self.credit_events(a["id"]) + self.credit_events(b["id"])), 200_000_000)
        self.store.set_device(self.admin_id, self.a_id, weekly_share_percent=5)
        self.assertAlmostEqual(self.report()["used_percent"], 30, places=5)
        self.assertEqual(self.report()["spent_credits_micros"], 100_000_000)
        self.store.close()
        self.store = Store(self.path)
        self.assertAlmostEqual(self.report()["used_percent"], 30, places=5)

    def test_weighting_includes_actual_model_cache_speed_not_reasoning_multiplier(self):
        self.observe()
        a, b = self.make_run(self.room())["run"], self.make_run(self.room(self.b_id, model="gpt-5.6-terra", speed="fast"))["run"]
        self.store.record_usage(a["id"], 1000, 100, 800)
        self.store.record_usage(b["id"], 1000, 100, 800)
        expected_a = estimate_usage("gpt-5.6-sol", 1000, 100, 800)["credits_micros"]
        expected_b = estimate_usage("gpt-5.6-terra", 1000, 100, 800, "fast")["credits_micros"]
        self.assertEqual(self.report()["spent_credits_micros"], expected_a)
        self.assertEqual(self.report(self.b_id)["spent_credits_micros"], expected_b)
        self.observe(12)
        capacity = self.store.quota_report(self.admin_id)["calibration"]["capacity_credits_micros"]
        self.assertEqual(capacity, 50 * (expected_a + expected_b))

    def test_calibration_checkpoints_do_not_reuse_local_work_or_shrink_for_external_usage(self):
        _, _, run, _ = self.calibrate_two_clients()
        capacity = self.store.quota.state()["capacity_credits_micros"]
        self.assertEqual(self.store.quota.state()["baseline_percent"], 13)
        self.observe(15)
        state = self.store.quota.state()
        self.assertEqual((state["capacity_credits_micros"], state["baseline_percent"], state["last_calibration_result"]),
                         (capacity, 15, "provider_only"))
        self.store.record_usage(run["id"], 1_100_000, 0)
        self.observe(17)
        state = self.store.quota.state()
        self.assertEqual((state["capacity_credits_micros"], state["baseline_percent"], state["last_calibration_result"]),
                         (capacity, 17, "ignored_lower"))
        self.assertGreater(state["untracked_provider_delta_percent"], 3)

    def test_provider_only_account_activity_never_charges_an_idle_device(self):
        self.observe(10)
        run = self.make_run(self.room())["run"]
        self.store.record_usage(run["id"], 1_000_000, 0)
        self.observe(12)
        active_before = self.report()
        idle_before = self.report(self.b_id)
        self.observe(20)
        active_after = self.report()
        idle_after = self.report(self.b_id)
        self.assertEqual(idle_before["spent_credits_micros"], 0)
        self.assertEqual(idle_after["spent_credits_micros"], 0)
        self.assertEqual(idle_after["account_used_percent"], 0)
        self.assertEqual(
            active_after["account_used_percent"], active_before["account_used_percent"]
        )

    def test_higher_capacity_requires_two_independent_compatible_intervals(self):
        _, _, first, _ = self.calibrate_two_clients()
        original = self.store.quota.state()["capacity_credits_micros"]
        self.store.record_usage(first["id"], 3_000_000, 0)
        self.observe(15)
        state = self.store.quota.state()
        self.assertEqual(state["capacity_credits_micros"], original)
        self.assertEqual(state["pending_capacity_credits_micros"], 10_000_000_000)
        calibration = self.store.quota_report(self.admin_id)["calibration"]
        self.assertEqual(calibration["effective_capacity_credits_micros"], 10_000_000_000)
        self.assertTrue(calibration["provisional_higher_capacity"])
        first_report = self.report()
        self.assertAlmostEqual(first_report["snapshot_account_used_percent"], 3, places=5)
        self.assertAlmostEqual(first_report["pending_account_used_percent"], 0, places=5)
        self.assertAlmostEqual(first_report["account_used_percent"], 3, places=5)
        self.store.record_usage(first["id"], 5_000_000, 0)
        pending_report = self.report()
        self.assertAlmostEqual(pending_report["snapshot_account_used_percent"], 3, places=5)
        self.assertAlmostEqual(pending_report["pending_account_used_percent"], 2, places=5)
        self.assertAlmostEqual(pending_report["account_used_percent"], 5, places=5)
        self.store.set_device(self.admin_id, self.a_id, weekly_share_percent=5)
        reconciled_limit = self.report()
        self.assertAlmostEqual(reconciled_limit["used_percent"], 100, places=5)
        self.assertTrue(reconciled_limit["strict_blocked"])
        self.assertEqual(reconciled_limit["block_reason"], "exhausted")
        self.observe(17)
        state = self.store.quota.state()
        self.assertEqual(state["capacity_credits_micros"], 10_000_000_000)
        self.assertEqual(state["last_calibration_result"], "accepted_confirmed")
        self.assertNotIn("pending_capacity_credits_micros", state)
        self.assertFalse(self.store.quota_report(self.admin_id)["calibration"]["provisional_higher_capacity"])

    def test_higher_capacity_candidate_survives_unrelated_provider_and_lower_intervals(self):
        _, _, first, _ = self.calibrate_two_clients()
        original = self.store.quota.state()["capacity_credits_micros"]
        self.store.record_usage(first["id"], 3_000_000, 0)
        self.observe(15)
        pending = self.store.quota.state()["pending_capacity_credits_micros"]
        before = self.report()["account_used_percent"]

        self.observe(17)
        provider_only = self.store.quota.state()
        self.assertEqual(provider_only["pending_capacity_credits_micros"], pending)
        self.assertAlmostEqual(self.report()["account_used_percent"], before, places=5)

        self.store.record_usage(first["id"], 4_000_000, 0)
        self.observe(19)
        ignored_lower = self.store.quota.state()
        self.assertEqual(ignored_lower["capacity_credits_micros"], original)
        self.assertEqual(ignored_lower["pending_capacity_credits_micros"], pending)
        self.assertEqual(ignored_lower["last_calibration_result"], "ignored_lower")

    def test_confirming_higher_capacity_never_reduces_effective_high_water_mark(self):
        _, _, first, _ = self.calibrate_two_clients()
        state = self.store.quota.state()
        state.update(pending_capacity_credits_micros=12_000_000_000,
                     pending_capacity_observed_at=self.now)
        self.store.quota.save(state)
        before = self.report()["account_used_percent"]

        self.store.record_usage(first["id"], 3_000_000, 0)
        self.observe(15)
        confirmed = self.store.quota.state()
        self.assertEqual(confirmed["capacity_credits_micros"], 12_000_000_000)
        self.assertNotIn("pending_capacity_credits_micros", confirmed)
        self.assertGreaterEqual(self.report()["account_used_percent"], before)

    def test_report_separates_share_fraction_from_whole_account_equivalent(self):
        self.calibrate_two_clients()
        self.store.set_device(self.admin_id, self.a_id, weekly_share_percent=3)
        report = self.report()
        self.assertAlmostEqual(report["used_percent"], 50, places=5)
        self.assertAlmostEqual(report["account_used_percent"], 1.5, places=5)

    def test_reconciled_attribution_prevents_raw_credit_false_block(self):
        self.calibrate_two_clients()
        state = self.store.quota.state()
        state["capacity_credits_micros"] = 1_000_000_000
        self.store.quota.save(state)
        self.store.set_device(self.admin_id, self.a_id, weekly_share_percent=7)
        report = self.report()
        self.assertGreater(report["spent_credits_micros"], report["allocated_credits_micros"])
        self.assertAlmostEqual(report["account_used_percent"], 6.5, places=5)
        self.assertGreater(report["remaining_credits_micros"], 0)
        self.assertEqual((report["strict_blocked"], report["block_reason"]), (False, "available"))

    def test_learning_and_stale_measurements_allow_work_but_zero_limit_blocks(self):
        self.store.set_device(self.admin_id, self.a_id, weekly_share_percent=3)
        self.assertEqual(self.store.quota_report(self.admin_id)["calibration"]["status"], "unknown")
        self.assertTrue(self.report()["limit_active"])
        self.assertEqual((self.report()["strict_blocked"], self.report()["block_reason"]),
                         (False, "available"))
        self.observe(10)
        self.assertEqual((self.report()["strict_blocked"], self.report()["block_reason"]),
                         (False, "available"))
        run = self.make_run(self.room(self.b_id))["run"]
        self.store.record_usage(run["id"], 1000, 100)
        self.observe(11.99)
        calibration = self.store.quota_report(self.admin_id)["calibration"]
        self.assertEqual(calibration["status"], "learning")
        self.assertIsNone(calibration["capacity_credits_micros"])
        self.assertEqual((self.report()["strict_blocked"], self.report()["block_reason"]),
                         (False, "available"))
        self.observe(12)
        self.assertTrue(self.report()["limit_active"])
        self.assertFalse(self.report()["strict_blocked"])
        self.assertEqual(self.report()["block_reason"], "available")
        self.now += FRESH_SECONDS + 1
        calibration = self.store.quota_report(self.admin_id)["calibration"]
        self.assertEqual(calibration["status"], "estimated")
        self.assertEqual(calibration["refresh_status"], "stale")
        self.assertTrue(calibration["using_last_known"])
        self.assertTrue(self.report()["limit_active"])
        self.assertFalse(self.report()["strict_blocked"])
        self.assertEqual(self.report()["block_reason"], "available")
        stale_run = self.make_run(self.room(), request_id="stale-allowed")["run"]
        self.store.record_usage(stale_run["id"], 0, 0)
        self.store.finish_run(run["id"], "completed", "Calibration work completed")
        self.store.set_device(self.admin_id, self.b_id, weekly_share_percent=0)
        self.assertTrue(self.report(self.b_id)["limit_active"])
        self.assertTrue(self.report(self.b_id)["strict_blocked"])
        self.assertEqual(self.report(self.b_id)["block_reason"], "disabled")
        with self.assertRaisesRegex(ValueError, "weekly account share"):
            self.make_run(self.room(self.b_id), request_id="zero-blocked")

    def test_unreported_usage_is_visible_but_allows_run_and_steering(self):
        room, _, original, _ = self.calibrate_two_clients()
        self.store.set_device(self.admin_id, self.a_id, max_concurrent=2, weekly_share_percent=3)
        second_room = self.room()
        unreported = self.make_run(second_room, request_id="unreported-run")["run"]
        report = self.report()
        self.assertEqual((report["unknown_runs"], report["unreported_runs"]), (1, 1))
        self.assertEqual((report["strict_blocked"], report["block_reason"]), (False, "available"))
        self.store.finish_run(original["id"], "completed", "Existing work may finish")
        third_room = self.room()
        concurrent = self.make_run(third_room, request_id="unknown-new-run")["run"]
        receipt, created = self.store.prepare_steering(
            self.a_id, second_room["id"], unreported["id"], "More", [], "unknown-steer",
            tool_read_only=False,
        )
        self.assertTrue(created)
        self.assertEqual(receipt["status"], "queued")
        self.store.record_usage(unreported["id"], 0, 0)
        self.store.record_usage(concurrent["id"], 0, 0)
        self.assertEqual(self.report()["unknown_runs"], 0)
        self.assertFalse(self.report()["strict_blocked"])
        receipt, created = self.store.prepare_steering(
            self.a_id, second_room["id"], unreported["id"], "Now safe", [], "known-steer",
            tool_read_only=False,
        )
        self.assertTrue(created)
        self.assertEqual(receipt["status"], "queued")

    def test_admin_acknowledges_only_existing_terminal_unreported_usage(self):
        self.calibrate_two_clients()
        self.store.set_device(self.admin_id, self.a_id, max_concurrent=2, weekly_share_percent=3)
        incomplete = self.make_run(self.room(), request_id="terminal-incomplete")["run"]
        self.store.finish_run(incomplete["id"], "interrupted", "Connection ended before usage arrived")
        before = self.report()
        self.assertEqual((before["unknown_runs"], before["blocking_unknown_runs"]), (1, 1))
        self.assertEqual((before["strict_blocked"], before["block_reason"]), (False, "available"))
        spent_before = before["spent_credits_micros"]

        result = self.store.acknowledge_unknown_usage(self.admin_id, self.a_id)
        self.assertEqual(result["acknowledged_runs"], 1)
        report = self.report()
        self.assertEqual(report["unknown_runs"], 1)
        self.assertEqual(report["acknowledged_unreported_runs"], 1)
        self.assertEqual(report["blocking_unknown_runs"], 0)
        self.assertEqual(report["spent_credits_micros"], spent_before)
        self.assertEqual(report["allocated_percent"], 3)
        self.assertEqual((report["strict_blocked"], report["block_reason"]), (False, "available"))
        self.assertEqual(self.store.get_device(self.a_id)["weekly_share_percent"], 3)
        self.assertEqual(self.store.acknowledge_unknown_usage(self.admin_id, self.a_id)["acknowledged_runs"], 0)
        self.store.close()
        self.store = Store(self.path)
        persisted = self.report()
        self.assertEqual((persisted["acknowledged_unreported_runs"], persisted["blocking_unknown_runs"]), (1, 0))
        self.assertFalse(persisted["strict_blocked"])

        future = self.make_run(self.room(), request_id="future-incomplete")["run"]
        self.store.finish_run(future["id"], "interrupted", "A later usage report is also missing")
        report = self.report()
        self.assertEqual(report["unknown_runs"], 2)
        self.assertEqual(report["acknowledged_unreported_runs"], 1)
        self.assertEqual(report["blocking_unknown_runs"], 1)
        self.assertEqual((report["strict_blocked"], report["block_reason"]), (False, "available"))
        self.make_run(self.room(), request_id="allowed-after-future-incomplete")

    def test_admin_cannot_acknowledge_live_unreported_usage(self):
        self.calibrate_two_clients()
        self.store.set_device(self.admin_id, self.a_id, max_concurrent=2, weekly_share_percent=3)
        live = self.make_run(self.room(), request_id="live-incomplete")["run"]
        result = self.store.acknowledge_unknown_usage(self.admin_id, self.a_id)
        self.assertEqual(result["acknowledged_runs"], 0)
        report = self.report()
        self.assertEqual((report["unknown_runs"], report["acknowledged_unreported_runs"], report["blocking_unknown_runs"]),
                         (1, 0, 1))
        self.assertEqual((report["strict_blocked"], report["block_reason"]), (False, "available"))
        with self.assertRaises(PermissionError):
            self.store.acknowledge_unknown_usage(self.a_id, self.a_id)
        self.store.record_usage(live["id"], 0, 0)
        self.assertEqual(self.report()["blocking_unknown_runs"], 0)

    def test_exact_boundary_blocks_new_work_allows_completion_and_reset_reopens(self):
        room, _, run, _ = self.calibrate_two_clients()
        self.store.set_device(self.admin_id, self.a_id, max_concurrent=2, weekly_share_percent=3)
        steering_room = self.room()
        live = self.make_run(steering_room, request_id="known-live")["run"]
        self.store.record_usage(live["id"], 0, 0)
        terminal_receipt, _ = self.store.prepare_steering(
            self.a_id, steering_room["id"], live["id"], "Delivered", [], "terminal-receipt",
            tool_read_only=False,
        )
        self.store.finish_steering(terminal_receipt["id"], "accepted")
        self.store.record_usage(run["id"], 2_000_000, 0)
        report = self.report()
        self.assertEqual(report["spent_credits_micros"], report["allocated_credits_micros"])
        self.assertEqual((report["strict_blocked"], report["block_reason"]), (True, "exhausted"))
        completed = self.store.finish_run(run["id"], "completed", "Already-running turn completed")
        self.assertEqual(completed["content"], "Already-running turn completed")
        replay, created = self.store.prepare_steering(
            self.a_id, steering_room["id"], live["id"], "Delivered", [], "terminal-receipt",
            tool_read_only=False,
        )
        self.assertFalse(created)
        self.assertEqual(replay["status"], "accepted")
        blocked_room = self.room()
        with self.assertRaisesRegex(ValueError, "weekly account share exhausted") as run_error:
            self.make_run(blocked_room, request_id="boundary-run")
        self.assertEqual(str(run_error.exception), QUOTA_EXHAUSTED_ERROR)
        with self.assertRaisesRegex(ValueError, "weekly account share exhausted") as steering_error:
            self.store.prepare_steering(
                self.a_id, steering_room["id"], live["id"], "Blocked", [], "boundary-steer",
                tool_read_only=False,
            )
        self.assertEqual(str(steering_error.exception), QUOTA_EXHAUSTED_ERROR)
        self.store.finish_run(live["id"], "completed", "Second active turn completed")
        self.now = self.start + WEEK_SECONDS + 10
        self.observe(0, end=self.start + 2 * WEEK_SECONDS)
        reset = self.report()
        self.assertEqual(reset["spent_credits_micros"], 0)
        self.assertEqual((reset["strict_blocked"], reset["block_reason"]), (False, "available"))
        self.assertFalse(self.make_run(blocked_room, request_id="after-reset")["deduplicated"])

    def test_restart_uses_durable_in_window_calibration_and_cap_edits_apply_immediately(self):
        room, _, run, _ = self.calibrate_two_clients()
        second_room = self.room()
        self.store.set_device(self.admin_id, self.a_id, max_concurrent=2, weekly_share_percent=3)
        self.now += FRESH_SECONDS + 1
        self.store.close()
        self.store = Store(self.path)
        report = self.report()
        self.assertEqual((report["strict_blocked"], report["block_reason"]), (False, "available"))
        calibration = self.store.quota_report(self.admin_id)["calibration"]
        self.assertEqual((calibration["status"], calibration["refresh_status"], calibration["using_last_known"]),
                         ("estimated", "stale", True))
        before = self.store.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        restarted = self.make_run(second_room, request_id="restart-stale")["run"]
        self.store.record_usage(restarted["id"], 0, 0)
        receipt, created = self.store.prepare_steering(
            self.a_id, room["id"], run["id"], "Stale but enforceable", [], "restart-stale-steer",
            tool_read_only=False,
        )
        self.assertTrue(created)
        self.assertEqual(receipt["status"], "queued")
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0], before + 1)
        self.observe(13)
        self.assertFalse(self.report()["strict_blocked"])
        queued, created = self.store.prepare_steering(
            self.a_id, room["id"], run["id"], "Queued before edit", [], "queued-before-edit",
            tool_read_only=False,
        )
        self.assertTrue(created)
        self.store.set_device(self.admin_id, self.a_id, weekly_share_percent=0)
        self.assertEqual((self.report()["strict_blocked"], self.report()["block_reason"]), (True, "disabled"))
        before_receipt = tuple(self.store.db.execute("SELECT * FROM steering_receipts WHERE id=?", (queued["id"],)).fetchone())
        with self.assertRaisesRegex(ValueError, "weekly account share exhausted"):
            self.store.finish_steering(queued["id"], "sending")
        after_receipt = tuple(self.store.db.execute("SELECT * FROM steering_receipts WHERE id=?", (queued["id"],)).fetchone())
        self.assertEqual(after_receipt, before_receipt)
        with self.assertRaisesRegex(ValueError, "weekly account share exhausted"):
            self.store.prepare_steering(
                self.a_id, room["id"], run["id"], "Disabled", [], "disabled-steer",
                tool_read_only=False,
            )
        self.store.set_device(self.admin_id, self.a_id, weekly_share_percent=None)
        uncapped = self.report()
        self.assertEqual((uncapped["limit_active"], uncapped["strict_blocked"], uncapped["block_reason"]),
                         (False, False, "not_configured"))
        receipt, created = self.store.prepare_steering(
            self.a_id, room["id"], run["id"], "Uncapped", [], "uncapped-steer",
            tool_read_only=False,
        )
        self.assertTrue(created)
        self.assertEqual(receipt["status"], "queued")
        self.assertEqual(self.store.finish_steering(queued["id"], "sending")["steering"]["status"], "sending")

    def test_expired_last_known_window_does_not_block_until_reset_is_observed(self):
        self.calibrate_two_clients()
        self.store.set_device(self.admin_id, self.a_id, max_concurrent=2, weekly_share_percent=3)
        self.now = self.start + WEEK_SECONDS + 1
        self.store.close()
        self.store = Store(self.path)
        calibration = self.store.quota_report(self.admin_id)["calibration"]
        report = self.report()
        self.assertEqual((calibration["status"], calibration["refresh_status"], calibration["using_last_known"]),
                         ("stale", "expired", False))
        self.assertEqual((report["strict_blocked"], report["block_reason"]),
                         (False, "available"))
        self.make_run(self.room(), request_id="expired-last-known")

    def test_stale_measurement_does_not_block_even_if_last_known_share_was_exhausted(self):
        _, _, run, _ = self.calibrate_two_clients()
        self.store.set_device(self.admin_id, self.a_id, max_concurrent=2, weekly_share_percent=3)
        self.store.record_usage(run["id"], 2_000_000, 0)
        self.store.observe_provider_usage(None)
        calibration = self.store.quota_report(self.admin_id)["calibration"]
        report = self.report()
        self.assertEqual((calibration["status"], calibration["refresh_status"], calibration["using_last_known"]),
                         ("estimated", "stale", True))
        self.assertEqual(report["spent_credits_micros"], report["allocated_credits_micros"])
        self.assertEqual((report["strict_blocked"], report["block_reason"]), (False, "available"))
        self.make_run(self.room(), request_id="stale-exhausted-allowed")

    def test_cross_window_live_run_is_unreported_until_a_current_snapshot(self):
        room, _, run, _ = self.calibrate_two_clients()
        self.store.set_device(self.admin_id, self.a_id, weekly_share_percent=3)
        self.now = self.start + WEEK_SECONDS + 10
        self.observe(0, end=self.start + 2 * WEEK_SECONDS)
        report = self.report()
        self.assertEqual((report["unknown_runs"], report["unreported_runs"]), (1, 1))
        self.assertEqual((report["strict_blocked"], report["block_reason"]), (False, "available"))
        receipt, created = self.store.prepare_steering(
            self.a_id, room["id"], run["id"], "Before snapshot", [], "cross-window-before",
            tool_read_only=False,
        )
        self.assertTrue(created)
        self.assertEqual(receipt["status"], "queued")
        self.store.record_usage(run["id"], 1_000_000, 0)
        report = self.report()
        self.assertEqual((report["unknown_runs"], report["unreported_runs"]), (0, 0))
        self.assertEqual((report["strict_blocked"], report["block_reason"]), (False, "available"))
        receipt, created = self.store.prepare_steering(
            self.a_id, room["id"], run["id"], "After snapshot", [], "cross-window-after",
            tool_read_only=False,
        )
        self.assertTrue(created)
        self.assertEqual(receipt["status"], "queued")

    def test_new_work_and_queued_steering_recheck_but_terminal_receipts_replay(self):
        room, _, run, _ = self.calibrate_two_clients()
        receipts = {}
        for status in ("queued", "sending", "accepted", "rejected", "uncertain"):
            receipt, _ = self.store.prepare_steering(
                self.a_id, room["id"], run["id"], status, [], status,
                tool_read_only=False,
            )
            receipts[status] = receipt
            if status != "queued":
                self.store.finish_steering(receipt["id"], status)
        self.store.set_device(self.admin_id, self.a_id, weekly_share_percent=1)
        for key in ("queued", "new"):
            with self.assertRaisesRegex(ValueError, "weekly account share"):
                self.store.prepare_steering(
                    self.a_id, room["id"], run["id"], key, [], key,
                    tool_read_only=False,
                )
        for status in ("sending", "accepted", "rejected", "uncertain"):
            receipt, created = self.store.prepare_steering(
                self.a_id, room["id"], run["id"], status, [], status,
                tool_read_only=False,
            )
            self.assertFalse(created)
            self.assertEqual(receipt["status"], status)
        self.assertTrue(self.make_run(room)["deduplicated"])
        self.store.finish_run(run["id"], "completed", "Not interrupted")
        with self.assertRaisesRegex(ValueError, "weekly account share"):
            self.make_run(room, request_id="new-run")
        self.store.set_device(self.admin_id, self.a_id, weekly_share_percent=None, token_budget=1)
        with self.assertRaisesRegex(ValueError, "token budget"):
            self.make_run(room, request_id="lifetime")

    def test_normal_provider_reset_carries_estimated_capacity_not_old_spend(self):
        room, _, run, _ = self.calibrate_two_clients()
        self.store.set_device(self.admin_id, self.a_id, weekly_share_percent=3)
        old = self.store.quota_report(self.admin_id)["calibration"]["capacity_credits_micros"]
        self.now = self.start + WEEK_SECONDS + 10
        self.observe(0, end=self.start + 2 * WEEK_SECONDS)
        report = self.store.quota_report(self.admin_id)
        self.assertEqual(report["window"]["starts_at"], self.start + WEEK_SECONDS)
        self.assertEqual(report["calibration"]["capacity_credits_micros"], old)
        self.assertTrue(report["calibration"]["carried_forward"])
        self.assertEqual(self.report()["spent_credits_micros"], 0)
        self.store.record_usage(run["id"], 1_000_010, 0)
        self.assertEqual(self.report()["spent_credits_micros"], 1000)
        self.assertEqual(self.report()["unknown_runs"], 0)

    def test_reset_timestamp_jitter_does_not_restart_learning_or_erase_capacity(self):
        self.observe(10)
        run = self.make_run(self.room())["run"]
        self.now += 1
        self.store.record_usage(run["id"], 1000, 100)
        self.observe(11, end=self.start + WEEK_SECONDS + 1)
        baseline = self.store.quota.state()
        self.assertEqual(baseline["baseline_percent"], 10)
        self.assertEqual(baseline["ends_at"], self.start + WEEK_SECONDS)
        self.observe(12, end=self.start + WEEK_SECONDS - 1)
        calibration = self.store.quota_report(self.admin_id)["calibration"]
        self.assertEqual(calibration["status"], "estimated")
        capacity = calibration["capacity_credits_micros"]
        self.observe(12, end=self.start + WEEK_SECONDS + 2)
        self.assertEqual(self.store.quota_report(self.admin_id)["calibration"]["capacity_credits_micros"], capacity)
        self.assertEqual(self.report()["spent_credits_micros"], 150000)

    def test_normal_week_reset_with_jitter_keeps_capacity_and_stable_boundaries(self):
        self.calibrate_two_clients()
        old_capacity = self.store.quota_report(self.admin_id)["calibration"]["capacity_credits_micros"]
        self.now = self.start + WEEK_SECONDS + 10
        self.observe(0, end=self.start + 2 * WEEK_SECONDS + 1)
        report = self.store.quota_report(self.admin_id)
        self.assertEqual(report["window"]["starts_at"], self.start + WEEK_SECONDS)
        self.assertEqual(report["window"]["ends_at"], self.start + 2 * WEEK_SECONDS)
        self.assertEqual(report["calibration"]["capacity_credits_micros"], old_capacity)
        self.assertTrue(report["calibration"]["carried_forward"])
        self.assertEqual(self.report()["spent_credits_micros"], 0)

    def test_schedule_corrections_discard_unconfirmed_capacity_candidates(self):
        self.calibrate_two_clients()
        state = self.store.quota.state()
        state.update(pending_capacity_credits_micros=state["capacity_credits_micros"] * 2,
                     pending_capacity_observed_at=self.now)
        self.store.quota.save(state)
        self.observe(13, end=self.start + WEEK_SECONDS + 60)
        corrected = self.store.quota.state()
        self.assertNotIn("pending_capacity_credits_micros", corrected)
        self.assertNotIn("pending_capacity_observed_at", corrected)

        corrected.update(pending_capacity_credits_micros=corrected["capacity_credits_micros"] * 2,
                         pending_capacity_observed_at=self.now)
        self.store.quota.save(corrected)
        self.observe(1, end=self.start + WEEK_SECONDS - 60)
        regressive = self.store.quota.state()
        self.assertNotIn("pending_capacity_credits_micros", regressive)
        self.assertNotIn("pending_capacity_observed_at", regressive)

    def test_regressive_same_limit_snapshot_is_quarantined_without_losing_capacity_or_spend(self):
        shift = 21 * 60 * 60
        old_end = self.start + WEEK_SECONDS + shift
        self.now = self.start + 2 * 24 * 60 * 60
        self.observe(28, end=old_end)
        first = self.make_run(self.room())["run"]
        second = self.make_run(self.room(self.b_id))["run"]
        self.now += 1
        for run in (first, second):
            self.store.record_usage(run["id"], 1_000_000, 0)
        self.observe(30, end=old_end)
        self.store.set_device(self.admin_id, self.a_id, weekly_share_percent=3)
        before = self.store.quota_report(self.admin_id)
        capacity, spent = before["calibration"]["capacity_credits_micros"], self.report()["spent_credits_micros"]
        self.assertEqual(before["window"]["starts_at"], self.start + shift)
        self.assertEqual(before["calibration"]["provider_used_percent"], 30)
        corrected_end = self.start + WEEK_SECONDS
        self.observe(1, end=corrected_end)
        corrected = self.store.quota_report(self.admin_id)
        state = self.store.quota.state()
        self.assertEqual(corrected["window"]["starts_at"], self.start + shift)
        self.assertEqual(corrected["window"]["ends_at"], old_end)
        self.assertEqual(corrected["calibration"]["capacity_credits_micros"], capacity)
        self.assertTrue(corrected["calibration"]["carried_forward"])
        self.assertEqual(corrected["calibration"]["provider_used_percent"], 30)
        self.assertEqual(corrected["calibration"]["refresh_status"], "stale")
        self.assertTrue(corrected["calibration"]["using_last_known"])
        self.assertEqual((state["baseline_percent"], state["baseline_at"]), (30, self.now))
        self.assertEqual(state["rejected_regressive_ends_at"], corrected_end)
        self.assertEqual(self.report()["spent_credits_micros"], spent)
        self.assertFalse(self.report()["strict_blocked"])
        self.now += 1
        self.store.record_usage(first["id"], 1_100_000, 0)
        spent += 10_000_000
        self.observe(31, end=old_end)
        restored = self.store.quota_report(self.admin_id)
        state = self.store.quota.state()
        self.assertEqual((restored["window"]["starts_at"], restored["window"]["ends_at"]),
                         (self.start + shift, old_end))
        self.assertEqual(restored["calibration"]["capacity_credits_micros"], capacity)
        self.assertTrue(restored["calibration"]["carried_forward"])
        self.assertEqual((state["baseline_percent"], state["baseline_at"]), (30, self.now - 2))
        self.assertEqual(self.report()["spent_credits_micros"], spent)
        self.assertFalse(self.report()["strict_blocked"])
        self.now = corrected_end
        self.assertFalse(self.report()["strict_blocked"])
        self.now = old_end
        self.assertEqual((self.report()["strict_blocked"], self.report()["block_reason"]),
                         (False, "available"))

    def test_spark_bucket_neither_calibrates_nor_blocks_regular_codex_share(self):
        self.calibrate_two_clients()
        self.store.set_device(self.admin_id, self.a_id, max_concurrent=2, weekly_share_percent=3)
        before = self.store.quota_report(self.admin_id)["calibration"]["capacity_credits_micros"]
        spark = self.make_run(self.room(model="gpt-5.3-codex-spark"), request_id="spark-separate")["run"]
        self.store.record_usage(spark["id"], 10_000_000, 1_000_000)
        estimate = self.store.db.execute(
            "SELECT credits_micros FROM quota_usage WHERE run_id=?", (spark["id"],)
        ).fetchone()
        self.assertIsNone(estimate[0])
        self.assertEqual(self.credit_events(spark["id"]), [])
        self.store.db.execute(
            "INSERT INTO quota_deltas(run_id,owner_id,credits_micros,created_at) VALUES(?,?,NULL,?)",
            (spark["id"], self.a_id, self.now),
        )
        report = self.report()
        self.assertEqual(report["unknown_runs"], 0)
        self.assertFalse(report["strict_blocked"])
        self.observe(13, windows=[
            {"limit_id": "codex_bengalfox", "kind": "primary", "window_minutes": 10080,
             "resets_at": self.start + WEEK_SECONDS, "used_percent": 99},
            {"limit_id": "codex", "kind": "primary", "window_minutes": 10080,
             "resets_at": self.start + WEEK_SECONDS, "used_percent": 13},
        ])
        calibration = self.store.quota_report(self.admin_id)["calibration"]
        self.assertEqual(calibration["capacity_credits_micros"], before)
        self.assertFalse(calibration["unknown_activity"])
        self.assertFalse(self.report()["strict_blocked"])

    def test_separate_spark_window_is_not_used_as_regular_codex_capacity(self):
        self.observe(80, windows=[
            {"limit_id": "codex_bengalfox", "kind": "primary", "window_minutes": 10080,
             "resets_at": self.start + WEEK_SECONDS, "used_percent": 80},
        ])
        report = self.store.quota_report(self.admin_id)
        self.assertIsNone(report["window"])
        self.assertEqual(report["calibration"]["status"], "unknown")

    def test_cross_week_cache_correction_preserves_new_and_unrelated_run_spending(self):
        _, _, run, _ = self.calibrate_two_clients()
        self.now = self.start + WEEK_SECONDS + 10
        self.observe(0, end=self.start + 2 * WEEK_SECONDS)
        self.store.record_usage(run["id"], 1_000_010, 0)
        self.store.set_device(self.admin_id, self.a_id, max_concurrent=2)
        fresh = self.make_run(self.room(), request_id="fresh-week")["run"]
        self.store.record_usage(fresh["id"], 100, 0)
        self.assertEqual(self.report()["spent_credits_micros"], 11000)
        self.store.record_usage(run["id"], 1_000_010, 0, 100)
        self.assertEqual(self.report()["spent_credits_micros"], 11000)
        self.assertEqual(self.report()["unknown_runs"], 1)
        self.assertEqual(self.report()["status"], "unknown")
        self.assertNotIn(-9000, self.credit_events(run["id"]))
        baseline = self.store.db.execute("SELECT credits_micros FROM quota_usage WHERE run_id=?", (run["id"],)).fetchone()[0]
        self.assertEqual(baseline, estimate_usage("gpt-5.6-sol", 1_000_010, 0, 100)["credits_micros"])
        self.store.record_usage(run["id"], 1_000_020, 0, 100)
        self.store.record_usage(run["id"], 1_000_020, 0, 100)
        self.assertEqual(self.report()["spent_credits_micros"], 12000)
        self.assertEqual(self.report()["unknown_runs"], 1)

    def test_cross_week_mixed_positive_growth_and_cache_correction_is_conservative(self):
        _, _, run, _ = self.calibrate_two_clients()
        self.now = self.start + WEEK_SECONDS + 10
        self.observe(0, end=self.start + 2 * WEEK_SECONDS)
        self.store.record_usage(run["id"], 1_000_100, 0, 50)
        self.assertEqual(self.report()["spent_credits_micros"], 10000)
        self.assertEqual(self.report()["unknown_runs"], 1)
        self.store.record_usage(run["id"], 1_000_200, 0, 100)
        self.assertEqual(self.report()["spent_credits_micros"], 20000)
        self.assertEqual(self.report()["unknown_runs"], 1)
        self.store.record_usage(run["id"], 1_000_210, 0, 100)
        self.assertEqual(self.report()["spent_credits_micros"], 21000)
        self.observe(2, end=self.start + 2 * WEEK_SECONDS)
        self.assertTrue(self.store.quota_report(self.admin_id)["calibration"]["unknown_activity"])

    def test_cross_week_correction_before_provider_refresh_stays_unknown_after_refresh(self):
        _, _, run, _ = self.calibrate_two_clients()
        self.now = self.start + WEEK_SECONDS + 10
        self.assertEqual(self.store.quota_report(self.admin_id)["calibration"]["status"], "stale")
        self.store.record_usage(run["id"], 1_000_100, 0, 50)
        self.observe(0, end=self.start + 2 * WEEK_SECONDS)
        self.assertEqual(self.report()["spent_credits_micros"], 10000)
        self.assertEqual(self.report()["unknown_runs"], 1)
        self.store.record_usage(run["id"], 1_000_110, 0, 50)
        self.assertEqual(self.report()["spent_credits_micros"], 11000)
        self.store.close()
        self.store = Store(self.path)
        self.assertEqual(self.report()["unknown_runs"], 1)
        self.store.finish_run(run["id"], "completed", "Cross-window work completed")
        self.now = self.start + 2 * WEEK_SECONDS + 10
        self.observe(0, end=self.start + 3 * WEEK_SECONDS)
        self.assertEqual(self.report()["spent_credits_micros"], 0)
        self.assertEqual(self.report()["unknown_runs"], 0)

    def test_device_quota_is_approved_self_only_and_redacts_shared_account_calibration(self):
        self.calibrate_two_clients()
        self.store.set_device(self.admin_id, self.a_id, weekly_share_percent=3)
        own = self.store.device_quota(self.a_id)
        self.assertEqual(set(own), {"window", "calibration", "device", "estimated"})
        self.assertEqual(set(own["window"]), {"starts_at", "ends_at"})
        self.assertEqual(set(own["calibration"]), {"status", "warning", "carried_forward", "learning_reason",
                                                   "refresh_status", "using_last_known"})
        self.assertEqual(own["device"]["device_id"], self.a_id)
        self.assertAlmostEqual(own["device"]["used_percent"], 50, places=5)
        self.assertNotIn(self.b_id, str(own))
        self.assertNotIn("capacity_credits_micros", str(own))
        self.assertNotIn("provider_used_percent", str(own))
        host = self.store.device_quota(self.admin_id)
        self.assertEqual(host["calibration"]["provider_used_percent"], 13)
        self.assertIn("provisional_higher_capacity", host["calibration"])
        pending = self.store.enroll("Pending device")["device"]["id"]
        self.store.set_device(self.admin_id, self.b_id, status="revoked")
        for owner in (pending, self.b_id, "unknown-device"):
            with self.assertRaises(PermissionError):
                self.store.device_quota(owner)

    def test_unknown_usage_breakdown_distinguishes_legacy_unreported_and_unpriced_work(self):
        self.observe(10)
        legacy = self.make_run(self.room(), request_id="legacy-breakdown")["run"]
        with self.store.db:
            self.store.db.execute("UPDATE runs SET model=NULL,speed=NULL WHERE id=?", (legacy["id"],))
        self.store.record_usage(legacy["id"], 100, 10)
        self.store.finish_run(legacy["id"], "completed", "Legacy response")
        unreported = self.make_run(self.room(), request_id="unreported-breakdown")["run"]
        self.store.finish_run(unreported["id"], "cancelled", "")
        unpriced = self.make_run(self.room(model="unknown-future-model"), request_id="unpriced-breakdown")["run"]
        self.store.record_usage(unpriced["id"], 100, 10)
        self.store.finish_run(unpriced["id"], "completed", "Unpriced response")
        known = self.make_run(self.room(), request_id="known-breakdown")["run"]
        self.store.record_usage(known["id"], 100, 10)
        own = self.store.device_quota(self.a_id)
        self.assertEqual(own["device"]["spent_credits_micros"], 15000)
        self.assertEqual(own["device"]["unknown_runs"], 3)
        for field in ("legacy_unknown_runs", "unreported_runs", "other_unknown_runs"):
            self.assertEqual(own["device"][field], 1)
        self.assertEqual(own["calibration"]["learning_reason"], "awaiting_weekly_change")
        self.observe(12)
        own = self.store.device_quota(self.a_id)
        self.assertEqual(own["calibration"]["learning_reason"], "unpriced_usage")
        self.assertIsNone(own["device"]["used_percent"])
        self.assertEqual(own["device"]["spent_credits_micros"], 15000)

    def test_learning_reason_does_not_expose_shared_account_percentage(self):
        self.observe(10)
        own = self.store.device_quota(self.a_id)
        self.assertEqual(own["calibration"]["learning_reason"], "awaiting_weekly_change")
        self.observe(12)
        own = self.store.device_quota(self.a_id)
        self.assertEqual(own["calibration"]["learning_reason"], "awaiting_usage_reports")
        self.assertNotIn("provider_used_percent", str(own))
        self.now += FRESH_SECONDS + 1
        self.assertIsNone(self.store.device_quota(self.a_id)["calibration"]["learning_reason"])

    def test_bucket_selection_and_shifted_weekly_reset_carries_allowance(self):
        _, _, first, second = self.calibrate_two_clients()
        self.store.set_device(self.admin_id, self.a_id, weekly_share_percent=3)
        capacity = self.store.quota_report(self.admin_id)["calibration"]["capacity_credits_micros"]
        self.store.finish_run(first["id"], "completed", "First window complete")
        self.store.finish_run(second["id"], "completed", "First window complete")
        self.observe(13, windows=[
            {"limit_id": "other", "kind": "secondary", "window_minutes": 10080, "resets_at": self.start + WEEK_SECONDS, "used_percent": 90},
            {"limit_id": "codex", "kind": "primary", "window_minutes": 10080, "resets_at": self.start + WEEK_SECONDS, "used_percent": 13},
        ])
        self.assertEqual(self.store.quota_report(self.admin_id)["window"]["limit_id"], "codex")
        self.now = self.start + WEEK_SECONDS + 100
        self.observe(0, end=self.start + 2 * WEEK_SECONDS + 100)
        report = self.store.quota_report(self.admin_id)
        self.assertEqual(report["window"]["starts_at"], self.start + WEEK_SECONDS + 100)
        self.assertEqual(report["calibration"]["status"], "estimated")
        self.assertEqual(report["calibration"]["capacity_credits_micros"], capacity)
        self.assertEqual(report["calibration"]["last_result"], "carried_shifted_reset")
        self.assertEqual((self.report()["strict_blocked"], self.report()["block_reason"]), (False, "available"))

    def test_distant_weekly_window_does_not_inherit_allowance(self):
        self.calibrate_two_clients()
        capacity = self.store.quota_report(self.admin_id)["calibration"]["capacity_credits_micros"]
        self.assertIsNotNone(capacity)
        drift = RESET_ROLLOVER_DRIFT_SECONDS + 1
        self.now = self.start + WEEK_SECONDS + drift
        self.observe(0, end=self.start + 2 * WEEK_SECONDS + drift)
        calibration = self.store.quota_report(self.admin_id)["calibration"]
        self.assertEqual(calibration["status"], "learning")
        self.assertIsNone(calibration["capacity_credits_micros"])

    def test_provider_drop_replay_and_malformed_readings_never_charge_clients(self):
        self.calibrate_two_clients()
        spent = self.report()["spent_credits_micros"]
        state = self.store.quota.state()
        state.update(pending_capacity_credits_micros=state["capacity_credits_micros"] * 2,
                     pending_capacity_observed_at=self.now)
        self.store.quota.save(state)
        envelope = self.observe(5)
        corrected = self.store.quota.state()
        self.assertNotIn("pending_capacity_credits_micros", corrected)
        self.assertNotIn("pending_capacity_observed_at", corrected)
        for _ in range(3):
            self.store.observe_provider_usage(envelope)
        self.assertEqual(self.report()["spent_credits_micros"], spent)
        for bad in (None, {}, {"available": True, "windows": [], "fetched_at": self.now},
                    {**envelope, "stale": True}, {**envelope, "fetched_at": float("nan")}):
            self.store.observe_provider_usage(bad)
            self.assertEqual(self.report()["spent_credits_micros"], spent)
            calibration = self.store.quota_report(self.admin_id)["calibration"]
            self.assertEqual(calibration["status"], "estimated")
            self.assertEqual(calibration["refresh_status"], "stale")
            self.assertTrue(calibration["using_last_known"])
            self.assertFalse(self.report()["strict_blocked"])
        self.observe(5)
        calibration = self.store.quota_report(self.admin_id)["calibration"]
        self.assertEqual(calibration["status"], "estimated")
        self.assertEqual(calibration["refresh_status"], "fresh")
        self.assertFalse(calibration["using_last_known"])

    def test_cached_provider_snapshot_does_not_calibrate_later_activity(self):
        _, _, run, _ = self.calibrate_two_clients()
        envelope = self.observe(13)
        old_capacity = self.store.quota_report(self.admin_id)["calibration"]["capacity_credits_micros"]
        self.now += 1
        self.store.record_usage(run["id"], 2_000_000, 0)
        self.store.observe_provider_usage(envelope)
        self.assertEqual(self.store.quota_report(self.admin_id)["calibration"]["capacity_credits_micros"], old_capacity)
        self.assertEqual(self.report()["spent_credits_micros"], 200_000_000)

    def test_unknown_model_is_not_zero_cost_or_used_for_calibration(self):
        self.observe(10)
        unknown = self.make_run(self.room(model="unknown-future-model"))["run"]
        known = self.make_run(self.room(self.b_id))["run"]
        self.store.record_usage(unknown["id"], 1000, 100)
        self.store.record_usage(unknown["id"], 1000, 100)
        self.store.record_usage(known["id"], 1000, 100)
        self.assertEqual(self.credit_events(unknown["id"]), [None])
        self.assertEqual(self.report()["unknown_runs"], 1)
        self.observe(12)
        calibration = self.store.quota_report(self.admin_id)["calibration"]
        self.assertIsNone(calibration["capacity_credits_micros"])
        self.assertTrue(calibration["unknown_activity"])
        self.store.record_usage(known["id"], 2000, 200)
        self.observe(14)
        self.assertEqual(self.store.quota_report(self.admin_id)["calibration"]["status"], "estimated")
        self.assertEqual(self.report()["status"], "unknown")

    def test_share_authorization_validation_and_omitted_versus_null(self):
        self.store.set_device(self.admin_id, self.a_id, token_budget=1000, weekly_share_percent=3.125)
        self.store.set_device(self.admin_id, self.a_id, max_concurrent=2)
        self.assertEqual(self.store.get_device(self.a_id)["weekly_share_percent"], 3.125)
        for invalid in (-1, 101, True, "3", float("nan"), float("inf"), 10 ** 1000):
            with self.assertRaises(ValueError):
                self.store.set_device(self.admin_id, self.a_id, weekly_share_percent=invalid)
        for action in (lambda: self.store.set_device(self.b_id, self.a_id, weekly_share_percent=3),
                       lambda: self.store.quota_report(self.a_id),
                       lambda: self.store.update_room(self.b_id, self.room()["id"], speed="fast")):
            with self.assertRaises(PermissionError):
                action()
        self.store.set_device(self.admin_id, self.a_id, weekly_share_percent=None)
        device = self.store.get_device(self.a_id)
        self.assertIsNone(device["weekly_share_percent"])
        self.assertEqual(device["token_budget"], 1000)

    def test_allocated_pool_excludes_unlimited_admin_and_inactive_devices(self):
        self.calibrate_two_clients()
        self.store.set_device(self.admin_id, self.a_id, weekly_share_percent=15)
        self.store.set_device(self.admin_id, self.b_id, weekly_share_percent=25)
        self.store.set_device(self.admin_id, self.admin_id, weekly_share_percent=40)
        unlimited = self.store.enroll("Unlimited user")["device"]["id"]
        self.store.set_device(self.admin_id, unlimited, status="approved")
        unlimited_run = self.make_run(self.room(unlimited), request_id="unlimited-usage")["run"]
        self.store.record_usage(unlimited_run["id"], 2_000_000, 0)
        pending = self.store.enroll("Pending allocation")["device"]["id"]
        self.store.set_device(self.admin_id, pending, weekly_share_percent=70)

        report = self.store.quota_report(self.admin_id)
        rows = {row["device_id"]: row for row in report["devices"]}
        expected_used = rows[self.a_id]["account_used_percent"] + rows[self.b_id]["account_used_percent"]
        pool = report["allocated_pool"]
        self.assertEqual((pool["status"], pool["limited_devices"], pool["allocated_percent"]),
                         ("available", 2, 40.0))
        self.assertAlmostEqual(pool["account_used_percent"], expected_used)
        self.assertAlmostEqual(pool["used_percent"], expected_used * 100 / 40)

        self.store.set_device(self.admin_id, self.b_id, status="revoked")
        self.assertEqual(self.store.quota_report(self.admin_id)["allocated_pool"]["allocated_percent"], 15.0)
        self.store.set_device(self.admin_id, self.a_id, weekly_share_percent=0)
        empty = self.store.quota_report(self.admin_id)["allocated_pool"]
        self.assertEqual((empty["status"], empty["allocated_percent"], empty["zero_share_devices"]),
                         ("zero_allocation", 0.0, 1))

    def test_administrator_share_is_an_analysis_target_and_never_blocks_host_work(self):
        self.calibrate_two_clients()
        room = self.room(self.admin_id)
        measured = self.make_run(room, request_id="host-measurement")["run"]
        self.store.record_usage(measured["id"], 100_000_000, 0)
        self.store.finish_run(measured["id"], "completed", "Measured")
        self.store.set_device(self.admin_id, self.admin_id, weekly_share_percent=1)
        report = self.report(self.admin_id)
        self.assertTrue(report["analysis_only"])
        self.assertFalse(report["enforcement_enabled"])
        self.assertFalse(report["limit_active"])
        self.assertFalse(report["strict_blocked"])
        self.assertEqual(report["block_reason"], "analysis_only")
        self.assertGreater(report["used_percent"], 100)
        created = self.make_run(room, request_id="host-remains-available")
        self.assertEqual(created["run"]["status"], "queued")


if __name__ == "__main__":
    unittest.main()
