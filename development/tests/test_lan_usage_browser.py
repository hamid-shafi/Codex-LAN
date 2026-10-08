"""CODEX: Keep official quota separate from explicitly estimated per-device shares."""

from __future__ import annotations

import time
import unittest

from playwright.async_api import expect

import test_lan_browser as browser_fixture


class LanProviderUsageBrowserTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = browser_fixture.LanBrowserTests.asyncSetUp
    asyncTearDown = browser_fixture.LanBrowserTests.asyncTearDown
    start_worker = browser_fixture.LanBrowserTests.start_worker
    open_admin_device = browser_fixture.LanBrowserTests.open_admin_device
    open_account_menu = browser_fixture.LanBrowserTests.open_account_menu

    async def open_usage(self, provider):
        self.provider = provider
        self.quota = getattr(self, "quota", {})
        await self.page.route("**/api/admin/usage", lambda route: route.fulfill(status=200, json={
            "usage": self.service.store.usage(self.admin_id), "provider_usage": self.provider, "quota": self.quota,
        }))
        await self.page.goto(self.base + "#login=" + self.admin["token"])
        await expect(self.page.locator("#workspace")).to_be_visible()
        await self.open_account_menu()
        await self.page.locator("#admin-open").click()
        await expect(self.page.locator("#provider-usage")).to_be_visible()

    async def test_official_windows_remaining_reset_and_existing_device_controls(self):
        reset = time.time() + 86400 * 3
        await self.open_usage({"available": True, "stale": False, "fetched_at": time.time(), "windows": [
            {"limit_id": "codex", "limit_name": "Codex", "kind": "primary", "used_percent": 25, "window_minutes": 300, "resets_at": reset},
            {"limit_id": "codex", "limit_name": "Codex", "kind": "primary", "used_percent": 82.5, "window_minutes": 10080, "resets_at": reset},
        ]})
        await expect(self.page.locator(".provider-window")).to_have_count(2)
        await expect(self.page.locator(".provider-window").first).to_contain_text("75% remaining · 25% used")
        await expect(self.page.locator(".provider-window").nth(1)).to_contain_text("17.5% remaining · 82.5% used")
        await expect(self.page.locator(".provider-window").nth(1)).to_contain_text("7-day window")
        await expect(self.page.locator(".quota-reset").first).to_contain_text("UTC")
        await expect(self.page.locator("#provider-usage")).to_contain_text("do not measure each person’s percentage")
        await expect(self.page.locator("#usage-summary")).to_contain_text("Estimated usage units")
        await expect(self.page.locator("#estimated-quota")).to_contain_text("not OpenAI credits")
        await expect(self.page.locator("#estimated-quota")).to_contain_text("Codex-Spark has a separate provider allowance")
        await expect(self.page.get_by_label("Weekly allowance (usage units)")).to_have_count(0)
        await expect(self.page.locator(".admin-device-summary")).to_have_count(3)
        await expect(self.page.get_by_label("Token budget")).to_have_count(0)
        await expect(self.page.get_by_role("button", name="Approve", exact=True)).to_have_count(0)
        pending = await self.open_admin_device(name="Pending laptop")
        await expect(pending.get_by_label("Token budget")).to_have_count(1)
        await expect(pending.get_by_role("button", name="Approve", exact=True)).to_have_count(1)
        await self.page.screenshot(path="/tmp/codex-lan-provider-usage.png")
        self.assertFalse(self.errors, self.errors)

    async def test_spark_bucket_is_labeled_separate_and_carried_calibration_is_explicit(self):
        reset = time.time() + 86400 * 3
        self.quota = {"calibration": {"status": "estimated", "capacity_credits_micros": 100000066,
                                      "carried_forward": True},
                      "window": {"limit_id": "codex", "ends_at": reset}, "devices": []}
        await self.open_usage({"available": True, "stale": False, "fetched_at": time.time(), "windows": [
            {"limit_id": "codex_bengalfox", "limit_name": "GPT-5.3-Codex-Spark", "kind": "secondary",
             "used_percent": 2, "window_minutes": 10080, "resets_at": reset},
        ]})
        await expect(self.page.locator(".provider-window")).to_contain_text("Separate Codex-Spark allowance")
        await expect(self.page.locator("#quota-calibration")).to_contain_text("previously measured capacity is being enforced")
        self.assertFalse(self.errors, self.errors)

    async def test_share_recalculation_keeps_units_and_advanced_budget_and_drafts(self):
        self.service.store.set_device(self.admin_id, self.client_id, token_budget=10000, weekly_share_percent=3)
        row = {"device_id": self.client_id, "allocated_percent": 3, "spent_credits_micros": 1500001, "allocated_credits_micros": 3000002, "used_percent": 50, "remaining_credits_micros": 1500001, "unknown_runs": 0, "limit_active": True, "strict_blocked": False, "block_reason": "available"}
        self.quota = {"calibration": {"status": "estimated", "capacity_credits_micros": 100000066}, "window": {"limit_id": "codex", "ends_at": time.time() + 86400}, "devices": [row]}
        await self.open_usage({"available": False})
        card = await self.open_admin_device(device_id=self.client_id)
        await expect(card).to_contain_text("1.5% of account allowance used out of 3% · 50% of assigned share")
        await expect(card).to_contain_text("Estimated attribution to this device: 1.5 account-percentage points · 3% allocated")
        await expect(card.locator(".quota-meter-scale")).to_contain_text("1.5% used · 50% of this allocation")
        await expect(card.locator(".quota-meter-scale")).to_contain_text("3% cap")
        await expect(card.get_by_role("progressbar", name="Estimated weekly share consumed")).to_have_attribute("aria-valuenow", "1.5")
        await expect(card.get_by_role("progressbar", name="Estimated weekly share consumed")).to_have_attribute("aria-valuemax", "3")
        self.assertEqual(await card.get_by_role("progressbar", name="Estimated weekly share consumed").locator("span").evaluate("el => el.style.width"), "50%")
        await expect(card).to_contain_text("Estimated usage units: 1.500001 / 3.000002 allocated")
        await expect(card.locator("details")).not_to_have_attribute("open", "")
        await card.locator("summary").click()
        await expect(card.get_by_label("Token budget", exact=True)).to_have_value("10000")
        share = card.get_by_label("Weekly share of account (%)")
        await share.fill("5")
        await self.page.evaluate("() => { window.savedShareInput = document.activeElement; }")
        await self.page.evaluate("() => refreshAdmin()")
        self.assertTrue(await self.page.evaluate("() => document.activeElement === window.savedShareInput"))
        await expect(share).to_have_value("5")
        await expect(card.locator("details")).to_have_attribute("open", "")
        await card.get_by_role("button", name="Save limits").click()
        await expect(card.get_by_role("button", name="Save limits")).to_be_enabled()
        self.assertEqual(self.service.store.get_device(self.client_id)["weekly_share_percent"], 5)
        self.assertEqual(self.service.store.get_device(self.client_id)["token_budget"], 10000)
        row.update(allocated_percent=5, allocated_credits_micros=5000003, used_percent=30, remaining_credits_micros=3500002)
        await self.page.evaluate("refreshAdmin()")
        await expect(card).to_contain_text("1.5% of account allowance used out of 5% · 30% of assigned share")
        await expect(card).to_contain_text("Estimated attribution to this device: 1.5 account-percentage points · 5% allocated")
        await expect(card.locator(".quota-meter-scale")).to_contain_text("1.5% used · 30% of this allocation")
        await expect(card.locator(".quota-meter-scale")).to_contain_text("5% cap")
        await expect(card).to_contain_text("Estimated usage units: 1.500001 / 5.000003 allocated")
        await expect(card).to_contain_text("Estimated units remaining: 3.500002")
        await card.screenshot(path="/tmp/codex-lan-estimated-quota.png")
        await share.fill("101")
        await card.get_by_role("button", name="Save limits").click()
        self.assertEqual(self.service.store.get_device(self.client_id)["weekly_share_percent"], 5)
        self.assertFalse(self.errors, self.errors)

    async def test_allocated_pool_meter_is_relative_to_all_limited_shares(self):
        self.quota = {
            "calibration": {"status": "estimated", "capacity_credits_micros": 100000000},
            "allocated_pool": {"status": "available", "limited_devices": 2, "zero_share_devices": 0,
                               "allocated_percent": 40, "account_used_percent": 10,
                               "used_percent": 25, "unknown_runs": 0, "lower_bound": False},
            "devices": [],
        }
        await self.open_usage({"available": False})
        pool = self.page.locator("#allocated-quota")
        await expect(pool).to_contain_text("25% of allocated pool used")
        await expect(pool).to_contain_text("10% of the account used · 25% filled")
        await expect(pool).to_contain_text("40% of the account is allocated across 2 approved limited users")
        meter = pool.get_by_role("progressbar", name="Combined allocated weekly pool")
        await expect(meter).to_have_attribute("aria-valuenow", "10")
        await expect(meter).to_have_attribute("aria-valuemax", "40")
        self.assertEqual(await meter.locator("span").evaluate("el => el.style.width"), "25%")
        self.assertFalse(self.errors, self.errors)

    async def test_acknowledged_partial_usage_meter_is_relative_to_its_eight_percent_share(self):
        self.service.store.set_device(self.admin_id, self.client_id, weekly_share_percent=8)
        row = {"device_id": self.client_id, "allocated_percent": 8, "account_used_percent": 0.0828,
               "spent_credits_micros": 2770598, "allocated_credits_micros": 267690000,
               "used_percent": 1.035, "remaining_credits_micros": 264919402,
               "unknown_runs": 1, "blocking_unknown_runs": 0, "acknowledged_unreported_runs": 1,
               "acknowledgeable_unreported_runs": 0, "limit_active": True,
               "strict_blocked": False, "block_reason": "available"}
        self.quota = {"calibration": {"status": "estimated", "capacity_credits_micros": 3346125000},
                      "window": {"limit_id": "codex", "ends_at": time.time() + 86400}, "devices": [row]}
        await self.open_usage({"available": False})
        card = await self.open_admin_device(device_id=self.client_id)
        meter = card.get_by_role("progressbar", name="Estimated weekly share consumed")
        await expect(card).to_contain_text("At least 0.0828% of account allowance used out of 8% · 1.03% of assigned share")
        await expect(card.locator(".quota-meter-scale")).to_contain_text("At least 0.0828% used · 1.03% of this allocation")
        await expect(card.locator(".quota-meter-scale")).to_contain_text("8% cap")
        await expect(meter).to_have_attribute("aria-valuenow", "0.0828")
        await expect(meter).to_have_attribute("aria-valuemax", "8")
        self.assertEqual(await meter.locator("span").evaluate("el => el.style.width"), "1.035%")
        self.assertFalse(self.errors, self.errors)

    async def test_learning_unknown_and_zero_limit_are_distinct(self):
        row = {"device_id": self.client_id, "allocated_percent": 3, "spent_credits_micros": 2000001, "used_percent": 0, "unknown_runs": 1, "limit_active": True, "strict_blocked": False, "block_reason": "available"}
        self.quota = {"calibration": {"status": "learning", "capacity_credits_micros": None}, "devices": [row]}
        await self.open_usage({"available": False})
        card = await self.open_admin_device(device_id=self.client_id)
        await expect(card).to_contain_text("Usage fraction unavailable")
        await expect(card).not_to_contain_text("About")
        await expect(card).not_to_contain_text("Blocked ·")
        await expect(card).to_contain_text("does not block new responses by itself")
        await expect(card.get_by_role("progressbar")).not_to_have_attribute("aria-valuenow", "0")
        self.assertEqual(await card.get_by_role("progressbar").locator("span").evaluate("el => el.style.width"), "0%")
        await expect(card).to_contain_text("unknown usage")
        self.quota["calibration"]["status"] = "estimated"
        row.update(strict_blocked=False, block_reason="available")
        await self.page.evaluate("refreshAdmin()")
        await expect(card).not_to_contain_text("Blocked · Usage incomplete")
        await expect(card).to_contain_text("does not block new responses by itself")
        await expect(card).not_to_contain_text("About")
        dialogs = []
        async def dismiss(dialog):
            dialogs.append(dialog.message)
            await dialog.dismiss()
        self.page.on("dialog", dismiss)
        await card.get_by_label("Weekly share of account (%)").fill("4")
        await card.get_by_role("button", name="Save limits").click()
        self.assertEqual(len(dialogs), 1)
        self.assertIn("blocked only at 0% or when a fresh calibrated measurement reaches the share", dialogs[0])
        self.assertIsNone(self.service.store.get_device(self.client_id)["weekly_share_percent"])
        await card.get_by_label("Weekly share of account (%)").fill("0")
        await card.get_by_role("button", name="Save limits").click()
        await expect(card.get_by_role("button", name="Save limits")).to_be_enabled()
        self.assertEqual(self.service.store.get_device(self.client_id)["weekly_share_percent"], 0)
        row.update(allocated_percent=0, strict_blocked=True, block_reason="disabled")
        await self.page.evaluate("refreshAdmin()")
        await expect(card).to_contain_text("Blocked · 0% weekly share")
        self.assertEqual(len(dialogs), 1)
        self.assertFalse(self.errors, self.errors)

    async def test_stale_unknown_and_over_limit_are_not_displayed_as_zero_usage(self):
        await self.open_usage({"available": True, "stale": True, "windows": [
            {"limit_name": "Weekly", "used_percent": None, "window_minutes": 10080, "resets_at": None},
            {"limit_name": "Limited", "used_percent": 125, "window_minutes": 300, "resets_at": time.time() - 20},
        ]})
        await expect(self.page.locator("#provider-usage-status")).to_contain_text("Stale report")
        await expect(self.page.locator(".provider-window").first).to_contain_text("Usage unavailable")
        await expect(self.page.locator(".provider-window").first.get_by_role("progressbar")).not_to_have_attribute("aria-valuenow", "0")
        await expect(self.page.locator(".provider-window").nth(1)).to_contain_text("0% remaining · 125% used")
        await expect(self.page.locator(".provider-window").nth(1).get_by_role("progressbar")).to_have_attribute("aria-valuenow", "100")
        await expect(self.page.locator(".provider-window").nth(1)).to_contain_text("awaiting a new report")
        self.provider = {"available": False, "windows": [], "stale": False}
        await self.page.locator("#admin-refresh").click()
        await expect(self.page.locator("#provider-usage-status")).to_contain_text("Official quota unavailable")
        await expect(self.page.locator(".provider-window")).to_have_count(0)
        await expect(self.page.locator("#provider-usage")).not_to_contain_text("0%")
        self.assertFalse(self.errors, self.errors)


if __name__ == "__main__":
    unittest.main()
