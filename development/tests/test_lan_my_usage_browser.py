"""CODEX: Approved clients can understand only their own estimated allowance."""

from __future__ import annotations

import time
import unittest

from playwright.async_api import expect

import test_lan_browser as browser_fixture


class LanMyUsageBrowserTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = browser_fixture.LanBrowserTests.asyncSetUp
    asyncTearDown = browser_fixture.LanBrowserTests.asyncTearDown
    start_worker = browser_fixture.LanBrowserTests.start_worker
    open_account_menu = browser_fixture.LanBrowserTests.open_account_menu

    async def open_client(self, quota):
        self.quota = quota
        self.admin_requests = []
        self.page.on("request", lambda request: self.admin_requests.append(request.url) if "/api/admin/" in request.url else None)

        async def own_report(route):
            response = await route.fetch()
            data = await response.json()
            data["quota"] = self.quota
            await route.fulfill(response=response, json=data)

        async def refresh_report(route):
            response = await route.fetch()
            if response.ok:
                await route.fulfill(response=response, json=self.quota)
            else:
                await route.fulfill(response=response)

        await self.page.route("**/api/me", own_report)
        await self.page.route("**/api/usage/me", refresh_report)
        await self.page.goto(self.base + "#login=" + self.client["token"])
        await expect(self.page.locator("#workspace")).to_be_visible()
        await self.open_account_menu()
        await expect(self.page.locator("#admin-open")).to_be_hidden()
        await self.page.get_by_role("button", name="My usage").click()
        await expect(self.page.get_by_role("dialog", name="My weekly usage")).to_be_visible()
        await expect(self.page.locator("#my-usage-refresh")).to_be_enabled()

    def report(self):
        return {"estimated": True, "window": {"starts_at": time.time() - 100, "ends_at": time.time() + 86400}, "calibration": {"status": "estimated", "carried_forward": False}, "device": {"device_id": self.client_id, "allocated_percent": 3, "spent_credits_micros": 1500000, "allocated_credits_micros": 3000000, "used_percent": 50, "remaining_credits_micros": 1500000, "unknown_runs": 0, "limit_active": True, "strict_blocked": False, "block_reason": "available"}}

    async def test_regular_client_sees_own_fraction_explanation_reset_and_tips(self):
        await self.open_client(self.report())
        dialog = self.page.locator("#my-usage-dialog")
        await expect(self.page.locator("#my-usage-brief")).to_have_text("50% of share")
        await expect(dialog).to_contain_text("1.5% of account allowance used out of 3% · 50% of assigned share")
        await expect(dialog).to_contain_text("Estimated attribution to this device: 1.5 account-percentage points · 3% allocated")
        await expect(dialog).to_contain_text("Estimated units remaining: 1.5")
        await expect(self.page.locator("#my-usage-reset")).to_contain_text("UTC")
        for text in ("reasoning tokens", "Cached input", "not an exact OpenAI debit", "does not erase", "provider’s reset", "Choose Standard", "smaller model", "Stop responses"):
            await expect(dialog).to_contain_text(text)
        await expect(dialog.locator("input:visible")).to_have_count(0)
        await expect(self.page.locator("#my-usage-admin-controls")).to_be_hidden()
        await expect(dialog).not_to_contain_text("Server .56")
        await expect(dialog).not_to_contain_text("Official shared-account quota")
        self.assertEqual(self.admin_requests, [])
        await self.page.screenshot(path="/tmp/codex-lan-my-usage.png", full_page=True)
        await self.page.keyboard.press("Escape")
        await expect(dialog).to_be_hidden()
        await expect(self.page.locator("#account-menu-open")).to_be_focused()
        await self.open_account_menu()
        await self.page.locator("#my-usage-open").click()
        await self.page.get_by_role("button", name="Close my usage").click()
        await expect(dialog).to_be_hidden()
        self.assertFalse(self.errors, self.errors)

    async def test_learning_stale_unknown_and_revocation_do_not_leave_live_fraction(self):
        quota = self.report()
        quota["calibration"]["status"] = "learning"
        quota["device"].update(used_percent=0, limit_active=True, strict_blocked=False, block_reason="available")
        await self.open_client(quota)
        meter = self.page.locator("#my-usage-meter")
        await expect(meter).to_contain_text("Usage fraction unavailable")
        await expect(meter).not_to_contain_text("About")
        await expect(self.page.locator("#my-usage-status")).to_contain_text("Learning weekly capacity")
        await expect(self.page.locator("#my-usage-status")).to_contain_text("does not block new responses by itself")
        await expect(meter.get_by_role("progressbar")).not_to_have_attribute("aria-valuenow", "0")
        quota["calibration"]["status"] = "stale"
        quota["device"].update(used_percent=50, limit_active=True, strict_blocked=False, block_reason="available")
        await self.page.locator("#my-usage-refresh").click()
        await expect(self.page.locator("#my-usage-status")).to_contain_text("Stale report")
        await expect(self.page.locator("#my-usage-status")).to_contain_text("staleness alone does not block")
        await expect(meter).not_to_contain_text("About")
        await expect(meter).not_to_contain_text("Blocked · Usage check unavailable")
        quota["calibration"]["status"] = "estimated"
        quota["device"].update(unknown_runs=1, spent_credits_micros=0, strict_blocked=False, block_reason="available")
        await self.page.locator("#my-usage-refresh").click()
        await expect(meter).not_to_contain_text("Blocked · Usage incomplete")
        await expect(meter).to_contain_text("does not block new responses by itself")
        await expect(meter).to_contain_text("Estimated usage units: Unreported")
        await expect(meter).not_to_contain_text("Estimated usage units: 0")
        await self.page.evaluate("() => { roomState().draft = 'This draft must not be sent'; updateComposer(); }")
        await expect(self.page.locator("#send-button")).to_be_enabled()
        await expect(self.page.locator("#composer-hint")).not_to_contain_text("unknown usage")
        for width in (400, 768):
            await self.page.set_viewport_size({"width": width, "height": 850})
            self.assertTrue(await self.page.locator("#my-usage-dialog").evaluate("el => el.scrollWidth <= el.clientWidth + 1"))
        self.service.store.set_device(self.admin_id, self.client_id, status="revoked")
        await self.page.locator("#my-usage-refresh").click()
        await expect(self.page.locator("#my-usage-dialog")).to_be_hidden()
        await expect(self.page.locator("#workspace")).to_be_hidden()
        self.assertEqual(self.admin_requests, [])
        self.assertFalse(self.errors, self.errors)

    async def test_known_partial_usage_distinguishes_legacy_and_missing_reports(self):
        quota = self.report()
        quota["calibration"].update(status="learning", learning_reason="awaiting_weekly_change")
        quota["device"].update(spent_credits_micros=287446700, unknown_runs=117,
                               legacy_unknown_runs=115, unreported_runs=2, other_unknown_runs=0,
                               used_percent=None, limit_active=True, strict_blocked=False,
                               block_reason="available", allocated_credits_micros=None)
        await self.open_client(quota)
        dialog = self.page.locator("#my-usage-dialog")
        await expect(dialog).to_contain_text("Known usage recorded so far: 287.4467 estimated units")
        await expect(dialog).to_contain_text("115 older responses predate model and speed tracking")
        await expect(dialog).to_contain_text("2 responses have not reported usage")
        await expect(dialog).to_contain_text("waiting for enough change in the account’s weekly meter")
        await expect(dialog).to_contain_text("Recorded usage is kept")
        await expect(dialog).not_to_contain_text("Blocked · Usage check unavailable")
        await expect(dialog).to_contain_text("does not block new responses by itself")
        await expect(dialog).not_to_contain_text("0% of share used")
        await self.page.screenshot(path="/tmp/codex-lan-myusage-partial.png")
        await self.page.set_viewport_size({"width": 400, "height": 850})
        self.assertTrue(await dialog.evaluate("el => el.scrollWidth <= el.clientWidth + 1"))
        self.assertEqual(self.admin_requests, [])
        self.assertFalse(self.errors, self.errors)

    async def test_admin_sees_uncapped_partial_account_usage_and_can_save_a_personal_target(self):
        async def own_report(route):
            response = await route.fetch()
            data = await response.json()
            share = self.service.store.get_device(self.admin_id)["weekly_share_percent"]
            device = {
                "device_id": self.admin_id, "allocated_percent": share,
                "spent_credits_micros": 1_933_000_000,
                "allocated_credits_micros": None if share is None else int(22_557_463_000 * share / 100),
                "account_used_percent": 8.57,
                "snapshot_account_used_percent": 3,
                "pending_account_used_percent": 5.57,
                "used_percent": None if share is None or share == 0 else 857 / share,
                "remaining_credits_micros": None, "unknown_runs": 117,
                "legacy_unknown_runs": 115, "unreported_runs": 2, "other_unknown_runs": 0,
                "limit_active": False, "target_active": share is not None,
                "analysis_only": share is not None, "enforcement_enabled": False,
                "strict_blocked": False, "block_reason": "analysis_only" if share is not None else "not_configured",
            }
            data["quota"] = {"estimated": True,
                "window": {"starts_at": time.time() - 100, "ends_at": time.time() + 86400},
                "calibration": {"status": "estimated", "carried_forward": False,
                                "provider_used_percent": 3, "provisional_higher_capacity": True}, "device": device}
            await route.fulfill(response=response, json=data)

        await self.page.route("**/api/me", own_report)
        await self.page.goto(self.base + "#login=" + self.admin["token"])
        await expect(self.page.locator("#workspace")).to_be_visible()
        await expect(self.page.locator("#my-usage-brief")).to_have_text("3% official")
        await self.open_account_menu()
        await self.page.get_by_role("button", name="My usage").click()
        dialog = self.page.locator("#my-usage-dialog")
        await expect(dialog).to_contain_text("At least 8.57 account-percentage points are attributable to this device from recorded work · No personal weekly cap")
        await expect(dialog).to_contain_text("Official account meter: 3% used")
        await expect(dialog).to_contain_text("Estimated for this device through the last official snapshot: 3% account-percentage points · +5.57% recorded after that snapshot")
        meter = dialog.get_by_role("progressbar", name="Official shared-account usage")
        await expect(meter).to_have_attribute("aria-valuenow", "3")
        await expect(dialog).not_to_contain_text("whole account used")
        controls = self.page.locator("#my-usage-admin-controls")
        await expect(controls).to_be_visible()
        target = self.page.get_by_label("My weekly analysis target (% of account)")
        await expect(target).to_have_value("")
        await target.fill("12")
        async with self.page.expect_response(lambda response: response.request.method == "PATCH" and "/api/admin/devices/" in response.url and response.status == 200):
            await self.page.get_by_role("button", name="Save my target").click()
        self.assertEqual(self.service.store.get_device(self.admin_id)["weekly_share_percent"], 12)
        await expect(self.page.locator("#my-usage-brief")).to_have_text("3% official")
        await expect(dialog).to_contain_text("Personal analysis target only. It never blocks host administrator responses.")
        await self.page.evaluate("""() => {
          state.myQuota = {calibration: {status: 'unknown'}, device: null};
          roomState().draft = 'Admin must remain able to send';
          updateComposer();
        }""")
        await expect(self.page.locator("#send-button")).to_be_enabled()
        await self.page.screenshot(path="/tmp/codex-lan-admin-my-usage.png", full_page=True)
        self.assertFalse(self.errors, self.errors)


if __name__ == "__main__":
    unittest.main()
