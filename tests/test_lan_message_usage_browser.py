"""CODEX: Response-local usage disclosure and unobtrusive room action regressions."""

from __future__ import annotations

import unittest

from playwright.async_api import expect

import test_lan_browser as browser_fixture


class LanMessageUsageBrowserTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = browser_fixture.LanBrowserTests.asyncSetUp
    asyncTearDown = browser_fixture.LanBrowserTests.asyncTearDown
    start_worker = browser_fixture.LanBrowserTests.start_worker
    login = browser_fixture.LanBrowserTests.login

    async def test_each_message_shows_only_its_own_usage_and_keeps_disclosure_open(self):
        created = self.service.store.create_run(self.admin_id, self.room["id"], "Another prompt", [], "second")
        run_id = created["run"]["id"]
        self.service.store.finish_run(run_id, "completed", "A separate response.")
        self.service.store.record_usage(run_id, 700, 60, 500, 760)
        await self.login(self.admin)
        usage = self.page.locator(".message.assistant .message-usage")
        await expect(usage).to_have_count(2)
        await usage.nth(0).locator("summary").click()
        await expect(usage.nth(0).locator("dd")).to_have_text(["200", "50", "100", "300"])
        await usage.nth(1).locator("summary").click()
        await expect(usage.nth(1).locator("dd")).to_have_text(["700", "500", "60", "760"])
        await self.page.evaluate("renderCurrent()")
        self.assertTrue(await usage.nth(0).evaluate("node => node.open"))
        self.assertTrue(await usage.nth(1).evaluate("node => node.open"))
        await self.page.set_viewport_size({"width": 390, "height": 844})
        self.assertTrue(await self.page.evaluate("document.documentElement.scrollWidth <= innerWidth"))
        self.assertFalse(self.errors, self.errors)

    async def test_unknown_usage_is_not_zero_and_estimates_do_not_cross_week_boundaries(self):
        await self.login(self.admin)
        values = await self.page.evaluate("""() => {
            const now = Date.now() / 1000;
            const before = state.myQuota;
            state.myQuota = {window: {starts_at: now - 1000, ends_at: now + 1000},
                calibration: {status: 'estimated', refresh_status: 'fresh', effective_capacity_credits_micros: 1000000}};
            const message = {role:'assistant', usage:{input_tokens:100, cached_tokens:70, output_tokens:20,total_tokens:120},
                estimated_usage_credits_micros: 1000};
            const run = {status:'completed',started_at:now-100,finished_at:now-50,model:'gpt-6-astra'};
            const current = responseUsage(message,run);
            const old = responseUsage(message,{...run,started_at:now-2000});
            const spark = responseUsage(message,{...run,model:'gpt-5.3-codex-spark'});
            state.myQuota.calibration.refresh_status='stale';
            const stale = responseUsage(message,run);
            const unknown = responseUsage({role:'assistant'}, {status:'completed'});
            const live = responseUsage({...message,usage:{total_tokens:1}},
                {status:'running',usage:{inputTokens:900,cachedInputTokens:300,outputTokens:80,totalTokens:980}});
            const article = document.querySelector('.message.assistant');
            updateResponseUsage(article,{role:'assistant'}, {status:'completed'});
            state.myQuota=before;
            return {current,old,stale,spark,unknown,live};
        }""")
        self.assertAlmostEqual(values["current"]["percent"], .1)
        for name in ("old", "stale", "spark"):
            self.assertIsNone(values[name]["percent"])
        self.assertIsNone(values["unknown"]["total"])
        self.assertEqual(values["live"]["total"], 980)
        label = self.page.locator(".message.assistant .message-usage summary").first
        await expect(label).to_have_text("Usage · awaiting report")
        await label.click()
        await expect(self.page.locator(".message-usage-body").first).to_contain_text("Unknown usage is not zero")
        self.assertFalse(self.errors, self.errors)

    async def test_room_actions_are_quiet_pin_is_rightmost_and_keyboard_reveals_them(self):
        await self.login(self.admin)
        row = self.page.locator(".room-row.active")
        actions = row.locator(".room-actions")
        await self.page.locator("#theme-select").select_option("cosmic")
        await self.page.locator("#message-input").focus()
        await self.page.mouse.move(650, 180)
        await expect(actions).to_have_css("opacity", "0.16")
        await row.hover()
        await expect(actions).to_have_css("opacity", "1")
        positions = await actions.locator("button").evaluate_all("nodes => nodes.map(n => ({name:n.className,x:n.getBoundingClientRect().x}))")
        self.assertIn("room-pin", positions[-1]["name"])
        self.assertEqual(positions[-1]["x"], max(value["x"] for value in positions))
        await self.page.mouse.move(650, 180)
        await row.locator(".room-pin").focus()
        await expect(actions).to_have_css("opacity", "1")
        await self.page.emulate_media(reduced_motion="reduce")
        await expect(actions).to_have_css("opacity", "1")
        await self.page.screenshot(path="/tmp/codex-lan-message-usage-actions.png")
        self.assertFalse(self.errors, self.errors)


if __name__ == "__main__":
    unittest.main()
