"""CODEX: Admin response safety controls persist without losing in-progress edits."""

from __future__ import annotations

import asyncio
import unittest

from playwright.async_api import expect

import test_lan_browser as browser_fixture


class LanResponseLimitsBrowserTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = browser_fixture.LanBrowserTests.asyncSetUp
    asyncTearDown = browser_fixture.LanBrowserTests.asyncTearDown
    start_worker = browser_fixture.LanBrowserTests.start_worker
    login = browser_fixture.LanBrowserTests.login
    open_admin_device = browser_fixture.LanBrowserTests.open_admin_device
    open_account_menu = browser_fixture.LanBrowserTests.open_account_menu

    async def open_limits(self, device_id=None):
        await self.page.route("**/api/admin/usage", lambda route: route.fulfill(status=200, json={
            "usage": self.service.store.usage(self.admin_id),
            "provider_usage": {"available": False}, "quota": {},
        }))
        await self.login(self.admin)
        await self.open_account_menu()
        await self.page.locator("#admin-open").click()
        return await self.open_admin_device(device_id=device_id or self.client_id)

    async def test_checkbox_saves_both_values_and_persists_after_reload(self):
        card = await self.open_limits()
        checkbox = card.get_by_role("checkbox", name="Limit each response", exact=True)
        await expect(checkbox).to_be_checked()
        await expect(checkbox).to_be_enabled()
        await expect(card.locator(".response-limit-field")).to_contain_text("45 minutes")
        await expect(card.locator(".response-limit-field")).to_contain_text("running and future responses")
        await expect(card.locator(".response-limit-field")).to_contain_text("weekly shares are separate")
        await expect(checkbox).to_have_attribute("aria-describedby", f"response-limits-help-{self.client_id}")
        for enabled in (False, True):
            await checkbox.set_checked(enabled)
            async with self.page.expect_request(lambda request: request.method == "PATCH"
                                                and request.url.endswith(f"/api/admin/devices/{self.client_id}")) as pending:
                await card.get_by_role("button", name="Save limits").click()
            request = await pending.value
            self.assertIs(request.post_data_json["response_limits_enabled"], enabled)
            await expect(card.get_by_role("button", name="Save limits")).to_be_enabled()
            self.assertIs(self.service.store.get_device(self.client_id)["response_limits_enabled"], enabled)
            await self.page.reload()
            await expect(self.page.locator("#admin-device-dialog")).to_be_visible()
            await expect(checkbox).to_be_checked(checked=enabled)
        for width in (400, 1365):
            await self.page.set_viewport_size({"width": width, "height": 900})
            await expect(checkbox).to_be_visible()
            self.assertTrue(await self.page.locator("#admin-device-dialog").evaluate(
                "dialog => dialog.scrollWidth <= dialog.clientWidth + 1"), f"Device detail overflows at {width}px")
        self.assertFalse(self.errors, self.errors)

    async def test_concurrent_response_setting_has_no_ten_response_cap(self):
        card = await self.open_limits()
        concurrency = card.get_by_label("Concurrent responses")
        self.assertIsNone(await concurrency.get_attribute("max"))
        await concurrency.fill("25")
        async with self.page.expect_request(lambda request: request.method == "PATCH"
                                            and request.url.endswith(f"/api/admin/devices/{self.client_id}")) as pending:
            await card.get_by_role("button", name="Save limits").click()
        request = await pending.value
        self.assertEqual(request.post_data_json["max_concurrent"], 25)
        await expect(card.get_by_role("button", name="Save limits")).to_be_enabled()
        self.assertEqual(self.service.store.get_device(self.client_id)["max_concurrent"], 25)
        self.assertFalse(self.errors, self.errors)

    async def test_refresh_preserves_checkbox_draft_and_updates_clean_value(self):
        card = await self.open_limits()
        checkbox = card.get_by_role("checkbox", name="Limit each response", exact=True)
        await checkbox.uncheck()
        await card.get_by_label("Concurrent responses").focus()
        await self.page.evaluate("refreshAdmin()")
        await expect(checkbox).not_to_be_checked()
        self.assertIs(self.service.store.get_device(self.client_id)["response_limits_enabled"], True)
        await card.get_by_role("button", name="Save limits").click()
        await expect(card.get_by_role("button", name="Save limits")).to_be_enabled()
        self.service.store.set_device(self.admin_id, self.client_id, response_limits_enabled=True)
        await self.page.evaluate("refreshAdmin()")
        await expect(checkbox).to_be_checked()
        self.assertFalse(self.errors, self.errors)

    async def test_new_checkbox_edit_survives_an_in_flight_save(self):
        card = await self.open_limits()
        checkbox = card.get_by_role("checkbox", name="Limit each response", exact=True)
        requested = asyncio.Event()
        release = asyncio.Event()

        async def delay_save(route):
            if route.request.method == "PATCH":
                requested.set()
                await release.wait()
            await route.continue_()

        await self.page.route(f"**/api/admin/devices/{self.client_id}", delay_save)
        await checkbox.uncheck()
        await card.get_by_role("button", name="Save limits").click()
        await asyncio.wait_for(requested.wait(), timeout=5)
        try:
            await expect(card.get_by_role("button", name="Save limits")).to_be_disabled()
            await checkbox.check()
            await card.get_by_label("Concurrent responses").focus()
        finally:
            release.set()
        await expect(card.get_by_role("button", name="Save limits")).to_be_enabled()
        self.assertIs(self.service.store.get_device(self.client_id)["response_limits_enabled"], False)
        await expect(checkbox).to_be_checked()
        await self.page.evaluate("refreshAdmin()")
        await expect(checkbox).to_be_checked()
        await card.get_by_role("button", name="Save limits").click()
        await expect(card.get_by_role("button", name="Save limits")).to_be_enabled()
        self.assertIs(self.service.store.get_device(self.client_id)["response_limits_enabled"], True)
        self.assertFalse(self.errors, self.errors)

    async def test_admin_checkbox_stays_disabled_and_unchecked(self):
        card = await self.open_limits(self.admin_id)
        checkbox = card.get_by_role("checkbox", name="Limit each response", exact=True)
        await expect(checkbox).not_to_be_checked()
        await expect(checkbox).to_be_disabled()
        await expect(card.locator(".response-limit-field")).to_contain_text("Admin responses have no per-response")
        await self.page.evaluate("refreshAdmin()")
        await expect(checkbox).not_to_be_checked()
        await expect(checkbox).to_be_disabled()
        async with self.page.expect_request(lambda request: request.method == "PATCH"
                                            and request.url.endswith(f"/api/admin/devices/{self.admin_id}")) as pending:
            await card.get_by_role("button", name="Save limits").click()
        request = await pending.value
        self.assertNotIn("response_limits_enabled", request.post_data_json)
        await expect(card.get_by_role("button", name="Save limits")).to_be_enabled()
        await self.page.reload()
        await expect(self.page.locator("#admin-device-dialog")).to_be_visible()
        await expect(checkbox).not_to_be_checked()
        await expect(checkbox).to_be_disabled()
        self.assertIs(self.service.store.get_device(self.admin_id)["response_limits_enabled"], False)
        self.assertFalse(self.errors, self.errors)

    async def test_ordinary_user_cannot_see_response_limit_control(self):
        await self.login(self.client)
        await self.open_account_menu()
        await expect(self.page.locator("#admin-open")).to_be_hidden()
        await self.page.evaluate("openAdminDevice(state.me.id)")
        await expect(self.page.locator("#admin-device-dialog")).to_be_hidden()
        await expect(self.page.get_by_role("checkbox", name="Limit each response", exact=True)).to_have_count(0)
        self.assertFalse(self.errors, self.errors)


if __name__ == "__main__":
    unittest.main()
