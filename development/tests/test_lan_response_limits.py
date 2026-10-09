"""Per-owner response policies preserve usage and apply to live worker sessions."""

from __future__ import annotations

import time
import unittest
from unittest import mock

import test_lan_server as fixture
from codex_lan.runtime import (
    MAX_RUN_SECONDS, MAX_RUN_TOTAL_TOKENS, MAX_RUN_CREDITS_MICROS,
    RUN_TIME_LIMIT_MESSAGE, RUN_USAGE_LIMIT_MESSAGE,
)


class ResponseLimitTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.LanServerTests.asyncSetUp
    asyncTearDown = fixture.LanServerTests.asyncTearDown
    request = fixture.LanServerTests.request
    until = fixture.LanServerTests.until
    worker = fixture.LanServerTests.worker
    room = fixture.LanServerTests.room
    submit_run = fixture.LanServerTests.submit_run
    ready_run = fixture.LanServerTests.ready_run

    async def configure(self, device, enabled, actor=None, expected=200):
        return await self.request(
            "PATCH", f"/api/admin/devices/{device['device']['id']}", actor or self.admin,
            expected=expected, json={"response_limits_enabled": enabled},
        )

    async def usage_observed(self, worker, live):
        with mock.patch("codex_lan.runtime.estimate_usage",
                        return_value={"credits_micros": MAX_RUN_CREDITS_MICROS}):
            await worker.usage(MAX_RUN_TOTAL_TOKENS, 0)
            await self.until(lambda: live.response_usage_exceeded)

    def interrupts(self, worker):
        return [call for call in worker.calls if call["method"] == "turn/interrupt"]

    async def test_admin_has_no_timer_and_crosses_usage_limit_without_interruption(self):
        worker = await self.worker(self.admin, "admin")
        room = await self.room(self.admin)
        with mock.patch("codex_lan.runtime.MAX_RUN_SECONDS", 0):
            created = await self.ready_run(self.admin, worker, room)
        live = self.service.runtime.runs[created["run"]["id"]]
        self.assertFalse(live.response_limits_enabled)
        self.assertIsNone(live.safety_handle)
        self.assertFalse(self.service.runtime._request_safety_stop(live, RUN_TIME_LIMIT_MESSAGE))
        await self.usage_observed(worker, live)
        await worker.complete()
        await self.until(lambda: created["run"]["id"] not in self.service.runtime.runs)
        run = self.service.store.get_run_internal(created["run"]["id"])
        self.assertEqual(run["status"], "completed")
        self.assertFalse(self.interrupts(worker))
        usage = self.service.store.db.execute(
            "SELECT total_tokens FROM usage_ledger WHERE run_id=?", (run["id"],)
        ).fetchone()
        self.assertEqual(usage[0], MAX_RUN_TOTAL_TOKENS)

    async def test_disable_during_response_cancels_timer_and_stale_callback(self):
        worker = await self.worker(self.a, "client")
        room = await self.room(self.a)
        created = await self.ready_run(self.a, worker, room)
        live = self.service.runtime.runs[created["run"]["id"]]
        old_timer = live.safety_handle
        self.assertTrue(live.response_limits_enabled)
        await self.configure(self.a, False)
        self.assertTrue(old_timer.cancelled())
        self.assertIsNone(live.safety_handle)
        self.assertFalse(self.service.runtime._request_safety_stop(live, RUN_TIME_LIMIT_MESSAGE))
        await self.usage_observed(worker, live)
        await worker.complete()
        await self.until(lambda: created["run"]["id"] not in self.service.runtime.runs)
        self.assertFalse(self.interrupts(worker))
        self.assertEqual(self.service.store.get_run_internal(created["run"]["id"])["status"], "completed")

    async def test_reenable_stops_already_over_budget_response_once(self):
        await self.configure(self.a, False)
        worker = await self.worker(self.a, "client")
        room = await self.room(self.a)
        created = await self.ready_run(self.a, worker, room)
        live = self.service.runtime.runs[created["run"]["id"]]
        await self.usage_observed(worker, live)
        self.assertFalse(self.interrupts(worker))
        await self.configure(self.a, True)
        await self.configure(self.a, True)
        await self.until(lambda: self.interrupts(worker))
        await worker.complete("interrupted")
        await self.until(lambda: created["run"]["id"] not in self.service.runtime.runs)
        run = self.service.store.get_run_internal(created["run"]["id"])
        self.assertEqual((run["status"], run["error"]), ("interrupted", RUN_USAGE_LIMIT_MESSAGE))
        self.assertEqual(len(self.interrupts(worker)), 1)

    async def test_reenable_uses_original_response_age(self):
        await self.configure(self.a, False)
        worker = await self.worker(self.a, "client")
        room = await self.room(self.a)
        created = await self.ready_run(self.a, worker, room)
        live = self.service.runtime.runs[created["run"]["id"]]
        live.run["created_at"] = time.time() - MAX_RUN_SECONDS - 1
        await self.configure(self.a, True)
        await self.until(lambda: self.interrupts(worker))
        self.assertEqual(live.safety_reason, RUN_TIME_LIMIT_MESSAGE)

    async def test_policy_requires_admin_and_cannot_reenable_admin_limit(self):
        await self.configure(self.a, False, actor=self.a, expected=403)
        await self.configure(self.admin, True, expected=400)
        for invalid in (0, 1, None, "false", [], {}):
            await self.configure(self.a, invalid, expected=400)
        admin = await self.configure(self.admin, False)
        self.assertIs(admin["response_limits_enabled"], False)
        me = await self.request("GET", "/api/me", self.a)
        self.assertIs(me["device"]["response_limits_enabled"], True)

    async def test_disabled_response_policy_does_not_disable_account_token_budget(self):
        await self.configure(self.a, False)
        self.service.store.set_device(self.admin["device"]["id"], self.a["device"]["id"], token_budget=0)
        await self.worker(self.a, "client")
        room = await self.room(self.a)
        result = await self.request("POST", f"/api/rooms/{room['id']}/runs", self.a, expected=400,
                                    json={"text": "hello", "request_id": "budget-still-enforced"})
        self.assertEqual(result["error"], "Device token budget exhausted")


if __name__ == "__main__":
    unittest.main()
