"""Browser coverage for safe backdrop dismissal and modal cleanup."""

import unittest

from playwright.async_api import expect

import test_lan_browser as browser_fixture


class LanDialogDismissalBrowserTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = browser_fixture.LanBrowserTests.asyncSetUp
    asyncTearDown = browser_fixture.LanBrowserTests.asyncTearDown
    start_worker = browser_fixture.LanBrowserTests.start_worker
    login = browser_fixture.LanBrowserTests.login
    open_account_menu = browser_fixture.LanBrowserTests.open_account_menu

    async def open_usage(self):
        await self.open_account_menu()
        await self.page.locator("#my-usage-open").click()
        dialog = self.page.locator("#my-usage-dialog")
        await expect(dialog).to_be_visible()
        return dialog

    async def test_my_usage_backdrop_closes_restores_focus_and_clears_route(self):
        await self.login(self.admin)
        dialog = await self.open_usage()
        self.assertEqual(await self.page.evaluate("new URL(location.href).searchParams.get('panel')"), "usage")
        await self.page.mouse.click(2, 2)
        await expect(dialog).to_be_hidden()
        await expect(self.page.locator("#account-menu-open")).to_be_focused()
        await self.page.wait_for_function("() => !new URL(location.href).searchParams.has('panel')")
        self.assertFalse(self.errors, self.errors)

    async def test_saved_room_settings_clear_route_before_queued_close_event(self):
        await self.login(self.admin)
        await self.page.locator("#room-menu").click()
        dialog = self.page.locator("#room-dialog")
        await expect(dialog).to_be_visible()
        self.assertEqual(await self.page.evaluate("new URL(location.href).searchParams.get('panel')"), "room-settings")
        await self.page.locator("#room-name-input").fill("Saved settings stay closed")
        await dialog.evaluate("""node => {
          node.addEventListener('close', event => event.stopImmediatePropagation(), {capture: true, once: true});
        }""")
        await self.page.locator("#room-save").click()
        await expect(dialog).to_be_hidden()
        await expect(self.page.locator("#room-title")).to_have_text("Saved settings stay closed")
        self.assertIsNone(await self.page.evaluate("new URL(location.href).searchParams.get('panel')"))
        self.assertEqual(self.service.store.get_room(self.admin_id, self.room["id"])["title"], "Saved settings stay closed")

        await self.page.reload()
        await self.page.wait_for_function("() => state.routeReady")
        await expect(dialog).to_be_hidden()
        await expect(self.page.locator("#room-title")).to_have_text("Saved settings stay closed")
        await self.page.locator("#room-search").fill("Saved settings stay closed")
        await expect(self.page.locator("#room-search")).to_have_value("Saved settings stay closed")
        await expect(self.page.locator("#room-list .room-row")).to_have_count(1)
        self.assertFalse(self.errors, self.errors)

    async def test_padding_drag_and_cancel_are_not_backdrop_clicks(self):
        await self.login(self.admin)
        dialog = await self.open_usage()
        bounds = await dialog.bounding_box()
        x, y = bounds["x"] + 5, bounds["y"] + 5
        await self.page.mouse.click(x, y)
        await expect(dialog).to_be_visible()

        await self.page.mouse.move(x, y)
        await self.page.mouse.down()
        await self.page.mouse.move(2, 2)
        await self.page.mouse.up()
        await expect(dialog).to_be_visible()

        await self.page.mouse.down()
        await self.page.mouse.move(x, y)
        await self.page.mouse.up()
        await expect(dialog).to_be_visible()

        await self.page.mouse.move(2, 2)
        await self.page.mouse.down()
        await dialog.evaluate("node => node.dispatchEvent(new PointerEvent('pointercancel', {bubbles: true, pointerId: 1}))")
        await self.page.mouse.up()
        await expect(dialog).to_be_visible()

        await self.page.mouse.click(2, 2)
        await expect(dialog).to_be_hidden()
        self.assertFalse(self.errors, self.errors)

    async def test_all_static_modals_dismiss_only_the_clicked_backdrop(self):
        await self.login(self.admin)
        ids = await self.page.locator("dialog").evaluate_all("nodes => nodes.map(node => node.id)")
        self.assertIn("my-usage-dialog", ids)
        self.assertIn("shortcut-help", ids)
        for dialog_id in ids:
            with self.subTest(dialog=dialog_id):
                dialog = self.page.locator("#" + dialog_id)
                await dialog.evaluate("node => node.showModal()")
                await expect(dialog).to_be_visible()
                await self.page.mouse.click(2, 2)
                await expect(dialog).to_be_hidden()

        await self.page.locator("#my-usage-dialog").evaluate("node => node.showModal()")
        await self.page.locator("#shortcut-help").evaluate("node => node.showModal()")
        await self.page.mouse.click(2, 2)
        await expect(self.page.locator("#shortcut-help")).to_be_hidden()
        await expect(self.page.locator("#my-usage-dialog")).to_be_visible()
        await self.page.mouse.click(2, 2)
        await expect(self.page.locator("#my-usage-dialog")).to_be_hidden()
        self.assertFalse(self.errors, self.errors)

    async def test_signin_backdrop_resets_passwords_without_saving(self):
        await self.login(self.admin)
        await self.open_account_menu()
        await self.page.locator("#browser-login-open").click()
        dialog = self.page.locator("#browser-login-dialog")
        await expect(dialog).to_be_visible()
        await self.page.locator("#browser-login-password").fill("temporary unsaved value")
        await self.page.locator("#browser-login-confirm").fill("temporary unsaved value")
        await self.page.mouse.click(2, 2)
        await expect(dialog).to_be_hidden()
        await expect(self.page.locator("#browser-login-password")).to_have_value("")
        await expect(self.page.locator("#browser-login-confirm")).to_have_value("")
        self.assertFalse(self.service.store.browser_login_profile(self.admin_id)["password_login_enabled"])
        await self.page.wait_for_function("() => !new URL(location.href).searchParams.has('panel')")
        self.assertFalse(self.errors, self.errors)

    async def test_touch_backdrop_tap_closes_usage_without_closing_from_padding(self):
        context = await self.browser.new_context(viewport={"width": 390, "height": 844}, has_touch=True)
        page = await context.new_page()
        page.on("pageerror", lambda error: self.errors.append(str(error)))
        try:
            await self.login(self.admin, page=page)
            await page.locator("#sidebar-toggle").tap()
            await page.locator("#account-menu-open").tap()
            await page.locator("#my-usage-open").tap()
            dialog = page.locator("#my-usage-dialog")
            await expect(dialog).to_be_visible()
            bounds = await dialog.bounding_box()
            await page.touchscreen.tap(bounds["x"] + 5, bounds["y"] + 5)
            await expect(dialog).to_be_visible()
            await page.touchscreen.tap(2, 2)
            await expect(dialog).to_be_hidden()
            self.assertFalse(self.errors, self.errors)
        finally:
            await context.close()

    async def test_escape_closes_mobile_guide_before_the_sidebar(self):
        await self.login(self.admin)
        await self.page.set_viewport_size({"width": 390, "height": 844})
        await self.page.locator("#sidebar-toggle").click()
        await self.page.locator("#account-menu-open").click()
        await self.page.locator("#shortcut-help-open").click()
        await expect(self.page.locator("#shortcut-help")).to_be_visible()
        await self.page.keyboard.press("Escape")
        await expect(self.page.locator("#shortcut-help")).to_be_hidden()
        await expect(self.page.locator("#workspace")).to_have_class("workspace sidebar-open")
        await expect(self.page.locator("#account-menu-open")).to_be_focused()
        await self.page.keyboard.press("Escape")
        await expect(self.page.locator("#workspace")).to_have_class("workspace")
        await expect(self.page.locator("#sidebar-toggle")).to_be_focused()
        self.assertFalse(self.errors, self.errors)


if __name__ == "__main__":
    unittest.main()
