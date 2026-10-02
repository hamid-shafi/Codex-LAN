"""CODEX: Keep provider quota and local token accounting distinct and administrator-only."""

import unittest
import time
import asyncio
from unittest import mock

import test_lan_server as fixture


class UsageApiTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.LanServerTests.asyncSetUp
    asyncTearDown = fixture.LanServerTests.asyncTearDown
    request = fixture.LanServerTests.request

    async def test_clients_cannot_fetch_shared_account_usage(self):
        calls = []

        async def limits():
            calls.append(True)
            return {}

        self.service.runtime.auth.rate_limits = limits
        await self.request("GET", "/api/admin/usage", self.a, expected=403)
        self.assertEqual(calls, [])

    async def test_provider_percent_is_not_derived_from_local_tokens(self):
        expected = {"available": True, "stale": False, "fetched_at": 1788900000,
                    "windows": [{"limit_id": "codex", "limit_name": None, "kind": "secondary",
                                 "used_percent": 63, "window_minutes": 10080, "resets_at": 1789200000}]}

        async def limits():
            return expected

        self.service.runtime.auth.rate_limits = limits
        report = await self.request("GET", "/api/admin/usage", self.admin)
        self.assertEqual(report["provider_usage"], expected)
        self.assertTrue(all(row["total_tokens"] == 0 for row in report["usage"]))

    async def test_provider_failure_keeps_local_usage_available(self):
        async def limits():
            raise RuntimeError("FAKE-PRIVATE-PROVIDER-ERROR")

        self.service.runtime.auth.rate_limits = limits
        report = await self.request("GET", "/api/admin/usage", self.admin)
        self.assertFalse(report["provider_usage"]["available"])
        self.assertEqual(report["provider_usage"]["windows"], [])
        self.assertIsNone(report["provider_usage"]["fetched_at"])
        self.assertNotIn("FAKE-PRIVATE", str(report))
        self.assertGreater(len(report["usage"]), 0)

    async def test_share_api_preserves_lifetime_limit_and_learning_does_not_block(self):
        now = time.time()

        async def limits():
            return {"available": True, "stale": False, "fetched_at": now,
                    "windows": [{"limit_id": "codex", "kind": "primary", "window_minutes": 10080,
                                 "used_percent": 14, "resets_at": now + 86400}]}

        self.service.runtime.auth.rate_limits = limits
        path = f"/api/admin/devices/{self.a['device']['id']}"
        await self.request("PATCH", path, self.admin, json={"token_budget": 90000})
        device = await self.request("PATCH", path, self.admin, json={"weekly_share_percent": 3})
        self.assertEqual(device["token_budget"], 90000)

        self.assertEqual(device["weekly_share_percent"], 3)
        await self.request("PATCH", path, self.a, expected=403, json={"weekly_share_percent": 100})
        for value in (-1, 101, True, "3"):
            await self.request("PATCH", path, self.admin, expected=400, json={"weekly_share_percent": value})
        report = await self.request("GET", "/api/admin/usage", self.admin)
        self.assertEqual(report["quota"]["calibration"]["status"], "learning")
        client = next(row for row in report["quota"]["devices"] if row["device_id"] == self.a["device"]["id"])
        self.assertEqual(client["allocated_percent"], 3)
        self.assertIsNone(client["used_percent"])
        self.assertTrue(client["limit_active"])
        self.assertFalse(client["strict_blocked"])
        self.assertEqual(client["block_reason"], "available")
        device = await self.request("PATCH", path, self.admin, json={"weekly_share_percent": None})
        self.assertIsNone(device["weekly_share_percent"])
        self.assertEqual(device["token_budget"], 90000)

    async def test_admin_can_acknowledge_terminal_incomplete_usage_without_removing_share(self):
        now = time.time()
        first_end = now + 86400
        self.service.store.observe_provider_usage({
            "available": True, "stale": False, "fetched_at": now,
            "windows": [{"limit_id": "codex", "kind": "primary", "window_minutes": 10080,
                         "used_percent": 10, "resets_at": first_end}],
        })
        room_a = self.service.store.create_room(
            self.a["device"]["id"], "A", "/tmp/a", model="gpt-5.6-sol"
        )
        room_b = self.service.store.create_room(
            self.b["device"]["id"], "B", "/tmp/b", model="gpt-5.6-sol"
        )
        run_a = self.service.store.create_run(self.a["device"]["id"], room_a["id"], "a", [], "calibrate-a")["run"]
        run_b = self.service.store.create_run(self.b["device"]["id"], room_b["id"], "b", [], "calibrate-b")["run"]
        self.service.store.record_usage(run_a["id"], 1_000_000, 0)
        self.service.store.record_usage(run_b["id"], 1_000_000, 0)
        self.service.store.observe_provider_usage({
            "available": True, "stale": False, "fetched_at": now + 1,
            "windows": [{"limit_id": "codex", "kind": "primary", "window_minutes": 10080,
                         "used_percent": 13, "resets_at": first_end}],
        })
        device_id = self.a["device"]["id"]
        device_path = f"/api/admin/devices/{device_id}"
        await self.request("PATCH", device_path, self.admin,
                           json={"max_concurrent": 2, "weekly_share_percent": 3})
        incomplete_room = self.service.store.create_room(
            device_id, "Incomplete", "/tmp/a", model="gpt-5.6-sol"
        )
        missing = self.service.store.create_run(device_id, incomplete_room["id"], "missing", [], "missing-usage")["run"]
        self.service.store.finish_run(missing["id"], "interrupted", "No final usage snapshot")
        acknowledgement_path = device_path + "/quota/acknowledge-unknown"

        await self.request("POST", acknowledgement_path, self.a, expected=403)
        response = await self.request("POST", acknowledgement_path, self.admin)
        self.assertEqual(response["acknowledged_runs"], 1)
        self.assertEqual(response["quota"]["device_id"], device_id)
        self.assertEqual(response["quota"]["allocated_percent"], 3)
        self.assertEqual(response["quota"]["unknown_runs"], 1)
        self.assertEqual(response["quota"]["acknowledged_unreported_runs"], 1)
        self.assertEqual(response["quota"]["blocking_unknown_runs"], 0)
        self.assertFalse(response["quota"]["strict_blocked"])
        replay = await self.request("POST", acknowledgement_path, self.admin)
        self.assertEqual(replay["acknowledged_runs"], 0)
        self.assertEqual(self.service.store.get_device(device_id)["weekly_share_percent"], 3)

    async def test_background_sampling_does_not_require_open_dashboard(self):
        reader = mock.AsyncMock(return_value={})
        for limited, expected in ((False, 1), (True, 2)):
            self.service.store.set_device(self.admin_id, self.a["device"]["id"],
                                          weekly_share_percent=3 if limited else None)
            reader.reset_mock()
            sleep = mock.AsyncMock(side_effect=[None, asyncio.CancelledError()])
            with mock.patch.object(self.service, "read_provider_usage", reader), mock.patch(
                    "codex_lan.server.asyncio.sleep", new=sleep):
                with self.assertRaises(asyncio.CancelledError):
                    await self.service.sample_provider_usage()
            self.assertEqual(reader.await_count, expected)
            self.assertEqual([call.args[0] for call in sleep.await_args_list], [10, 60 if not limited else 10])

    async def test_background_sampling_uses_normal_interval_after_fresh_reading(self):
        reader = mock.AsyncMock(return_value={"available": True, "stale": False, "windows": []})
        sleep = mock.AsyncMock(side_effect=asyncio.CancelledError())
        with mock.patch.object(self.service, "read_provider_usage", reader), mock.patch(
                "codex_lan.server.asyncio.sleep", new=sleep):
            with self.assertRaises(asyncio.CancelledError):
                await self.service.sample_provider_usage()
        reader.assert_awaited_once_with()
        sleep.assert_awaited_once_with(60)

    async def test_background_sampling_retries_partially_stale_reading_and_propagates_cancellation(self):
        reader = mock.AsyncMock(return_value={"available": True, "stale": False,
                                               "windows": [{"stale": True}, {}]})
        sleep = mock.AsyncMock(side_effect=asyncio.CancelledError())
        with mock.patch.object(self.service, "read_provider_usage", reader), mock.patch(
                "codex_lan.server.asyncio.sleep", new=sleep):
            with self.assertRaises(asyncio.CancelledError):
                await self.service.sample_provider_usage()
        reader.assert_awaited_once_with()
        sleep.assert_awaited_once_with(10)

    async def test_background_sampling_survives_failure_and_health_reports_recovery(self):
        self.service.runtime.auth.rate_limits = mock.AsyncMock()
        reader = mock.AsyncMock(side_effect=[RuntimeError("PRIVATE-PROVIDER-DETAIL"),
                                             {"available": True, "stale": False, "windows": []}])
        failed_sleep, resume, recovered_sleep = asyncio.Event(), asyncio.Event(), asyncio.Event()
        delays = []

        async def controlled_sleep(delay):
            delays.append(delay)
            if len(delays) == 1:
                failed_sleep.set()
                await resume.wait()
            else:
                recovered_sleep.set()
                await asyncio.Event().wait()

        with mock.patch.object(self.service, "read_provider_usage", reader), mock.patch(
                "codex_lan.server.asyncio.sleep", new=controlled_sleep):
            task = asyncio.create_task(self.service.sample_provider_usage())
            await asyncio.wait_for(failed_sleep.wait(), 1)
            failed = (await self.request("GET", "/api/health"))["usage_sampler"]
            self.assertEqual(failed["status"], "degraded")
            self.assertTrue(failed["running"])
            self.assertEqual(failed["consecutive_failures"], 1)
            self.assertEqual(failed["last_error"], "RuntimeError")
            self.assertIsNone(failed["last_success"])
            self.assertNotIn("PRIVATE-PROVIDER-DETAIL", str(failed))
            self.assertFalse(task.done())

            resume.set()
            await asyncio.wait_for(recovered_sleep.wait(), 1)
            recovered = (await self.request("GET", "/api/health"))["usage_sampler"]
            self.assertEqual(recovered["status"], "healthy")
            self.assertTrue(recovered["running"])
            self.assertEqual(recovered["consecutive_failures"], 0)
            self.assertIsInstance(recovered["last_success"], float)
            self.assertEqual(recovered["last_error"], "RuntimeError")
            self.assertFalse(task.done())

            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(delays, [10, 60])
        self.assertEqual(reader.await_count, 2)
        self.assertFalse(self.service.usage_sampler["running"])

    async def test_clients_see_only_their_own_share_without_account_details(self):
        self.service.store.set_device(self.admin_id, self.a["device"]["id"], weekly_share_percent=3)
        self.service.store.set_device(self.admin_id, self.b["device"]["id"], weekly_share_percent=8)
        report = await self.request("GET", "/api/me", self.a)
        quota = report["quota"]
        self.assertEqual(quota["device"]["device_id"], self.a["device"]["id"])
        self.assertEqual(quota["device"]["allocated_percent"], 3)
        self.assertNotIn("devices", quota)
        self.assertNotIn("capacity_credits_micros", quota["calibration"])
        self.assertNotIn("provider_used_percent", quota["calibration"])
        self.assertNotIn(self.b["device"]["id"], str(quota))
        pending = await self.request("GET", "/api/me", self.pending)
        self.assertIsNone(pending["quota"])

    async def test_personal_refresh_forces_provider_read_and_returns_only_own_quota(self):
        now = time.time()
        limits = mock.AsyncMock(return_value={"available": True, "stale": False, "fetched_at": now,
            "windows": [{"limit_id": "codex", "kind": "primary", "window_minutes": 10080,
                         "used_percent": 17, "resets_at": now + 86400}]})
        self.service.runtime.auth.rate_limits = limits
        self.service.store.set_device(self.admin_id, self.a["device"]["id"], weekly_share_percent=3)
        report = await self.request("POST", "/api/usage/me", self.a, json={"device_id": self.b["device"]["id"]})
        limits.assert_awaited_once_with(force=True)
        self.assertEqual(report["device"]["device_id"], self.a["device"]["id"])
        self.assertEqual(report["device"]["allocated_percent"], 3)
        self.assertEqual(report["calibration"]["status"], "learning")
        for private in ("provider_used_percent", "capacity_credits_micros", self.b["device"]["id"]):
            self.assertNotIn(private, str(report))

    async def test_admin_personal_share_is_reported_as_analysis_only(self):
        await self.request("PATCH", f"/api/admin/devices/{self.admin_id}", self.admin,
                           json={"weekly_share_percent": 12.5})
        report = await self.request("GET", "/api/me", self.admin)
        own = report["quota"]["device"]
        self.assertEqual(own["allocated_percent"], 12.5)
        self.assertTrue(own["analysis_only"])
        self.assertFalse(own["enforcement_enabled"])
        self.assertFalse(own["limit_active"])
        self.assertFalse(own["strict_blocked"])
        self.assertEqual(own["block_reason"], "analysis_only")

    async def test_personal_refresh_rejects_pending_and_unknown_before_provider_work(self):
        limits = mock.AsyncMock(return_value={})
        self.service.runtime.auth.rate_limits = limits
        for device in (self.pending, None):
            await self.request("POST", "/api/usage/me", device, expected=403)
        limits.assert_not_awaited()

if __name__ == "__main__":
    unittest.main()
