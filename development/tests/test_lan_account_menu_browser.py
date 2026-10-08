"""Real browser coverage for the compact, credential-free account popover."""

import unittest

from playwright.async_api import expect

import test_lan_browser as browser_fixture


class LanAccountMenuBrowserTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = browser_fixture.LanBrowserTests.asyncSetUp
    asyncTearDown = browser_fixture.LanBrowserTests.asyncTearDown
    start_worker = browser_fixture.LanBrowserTests.start_worker
    login = browser_fixture.LanBrowserTests.login
    open_account_menu = browser_fixture.LanBrowserTests.open_account_menu

    async def test_sidebar_keeps_account_actions_and_profile_details_hidden(self):
        await self.login(self.admin)
        await expect(self.page.locator("#account-menu-open")).to_be_visible()
        await expect(self.page.locator("#account-menu-open")).to_have_attribute("aria-expanded", "false")
        await expect(self.page.locator(".search-field button, .search-field kbd")).to_have_count(0)
        for action in ("my-usage-open", "browser-login-open", "admin-open", "shortcut-help-open", "theme-toggle"):
            await expect(self.page.locator("#" + action)).to_be_hidden()
        await expect(self.page.locator("#account-profile-name")).to_be_hidden()
        await expect(self.page.locator("#theme-select")).to_be_visible()

        await self.open_account_menu()
        await expect(self.page.locator("#account-profile-name")).to_have_text("Server .56")
        await expect(self.page.locator("#account-profile-role")).to_have_text("Administrator")
        await expect(self.page.locator("#account-profile-status")).to_have_text("Approved")
        await expect(self.page.locator("#my-usage-open")).to_be_focused()
        html = await self.page.locator("#account-menu").inner_html()
        self.assertNotIn(self.admin["token"], html)
        self.assertNotIn("type=\"password\"", html)
        self.assertFalse(self.errors, self.errors)

    async def test_outside_and_escape_dismiss_without_stealing_a_new_focus(self):
        await self.login(self.admin)
        await self.open_account_menu()
        await self.page.locator("#message-input").click()
        await expect(self.page.locator("#account-menu")).to_be_hidden()
        await expect(self.page.locator("#message-input")).to_be_focused()
        await self.open_account_menu()
        await self.page.keyboard.press("Escape")
        await expect(self.page.locator("#account-menu")).to_be_hidden()
        await expect(self.page.locator("#account-menu-open")).to_be_focused()
        await self.page.evaluate("() => { openAccountMenu(); closeAccountMenu(); openAccountMenu({last: true}); }")
        await expect(self.page.locator("#account-menu")).to_be_visible()
        await expect(self.page.locator("#shortcut-help-open")).to_be_focused()
        await self.page.locator("#room-search").focus()
        await expect(self.page.locator("#account-menu")).to_be_hidden()
        await expect(self.page.locator("#room-search")).to_be_focused()
        self.assertFalse(self.errors, self.errors)

    async def test_keyboard_navigation_wraps_and_skips_disabled_actions(self):
        await self.login(self.admin)
        await self.page.locator("#account-menu-open").focus()
        await self.page.keyboard.press("ArrowUp")
        await expect(self.page.locator("#shortcut-help-open")).to_be_focused()
        await self.page.keyboard.press("ArrowDown")
        await expect(self.page.locator("#my-usage-open")).to_be_focused()
        await self.page.locator("#browser-login-open").evaluate("node => {node.disabled = true;}")
        await self.page.keyboard.press("ArrowDown")
        await expect(self.page.locator("#admin-open")).to_be_focused()
        await self.page.keyboard.press("End")
        await expect(self.page.locator("#shortcut-help-open")).to_be_focused()
        await self.page.keyboard.press("Home")
        await expect(self.page.locator("#my-usage-open")).to_be_focused()
        await self.page.keyboard.press("Escape")
        await expect(self.page.locator("#account-menu-open")).to_be_focused()
        await self.page.keyboard.press("ArrowDown")
        await expect(self.page.locator("#my-usage-open")).to_be_focused()
        self.assertFalse(self.errors, self.errors)

    async def test_modal_actions_return_to_visible_profile_trigger(self):
        await self.login(self.admin)
        for action, dialog_id, close_id in (
            ("my-usage-open", "my-usage-dialog", "my-usage-close"),
            ("browser-login-open", "browser-login-dialog", "browser-login-close"),
            ("shortcut-help-open", "shortcut-help", "shortcut-help-close"),
        ):
            with self.subTest(action=action):
                await self.open_account_menu()
                await self.page.locator("#" + action).click()
                await expect(self.page.locator("#account-menu")).to_be_hidden()
                await expect(self.page.locator("#" + dialog_id)).to_be_visible()
                await self.page.locator("#" + close_id).click()
                await expect(self.page.locator("#" + dialog_id)).to_be_hidden()
                await expect(self.page.locator("#account-menu-open")).to_be_focused()
        await self.open_account_menu()
        await self.page.locator("#admin-open").click()
        await expect(self.page.locator("#account-menu")).to_be_hidden()
        await expect(self.page.locator("#admin-view")).to_be_visible()
        self.assertEqual(await self.page.evaluate("new URL(location.href).searchParams.get('view')"), "admin")
        self.assertFalse(self.errors, self.errors)

    async def test_client_profile_never_offers_administration_or_keeps_stale_menu(self):
        await self.login(self.client)
        await self.open_account_menu()
        await expect(self.page.locator("#admin-open")).to_be_hidden()
        await expect(self.page.locator("#account-profile-role")).to_have_text("Connected client")
        await self.page.keyboard.press("Home")
        await self.page.keyboard.press("ArrowDown")
        await self.page.keyboard.press("ArrowDown")
        await expect(self.page.locator("#theme-toggle")).to_be_focused()
        await self.page.evaluate("showLogin()")
        await expect(self.page.locator("#account-menu")).to_be_hidden()
        await expect(self.page.locator("#account-menu-open")).to_have_attribute("aria-expanded", "false")
        self.assertFalse(self.errors, self.errors)

    async def test_menu_fits_every_theme_and_mobile_returns_to_visible_sidebar_button(self):
        await self.login(self.admin)
        for theme in ("light", "dark", "midnight", "chatgpt", "torte", "cosmic", "aurora", "ember", "velvet"):
            with self.subTest(theme=theme):
                await self.page.locator("#theme-select").select_option(theme)
                await self.open_account_menu()
                bounds = await self.page.locator("#account-menu").bounding_box()
                self.assertGreaterEqual(bounds["x"], 0)
                self.assertGreaterEqual(bounds["y"], 0)
                self.assertLessEqual(bounds["x"] + bounds["width"], 1365)
                self.assertLessEqual(bounds["y"] + bounds["height"], 900)
                await self.page.keyboard.press("Escape")
        await self.page.set_viewport_size({"width": 390, "height": 844})
        await self.open_account_menu()
        bounds = await self.page.locator("#account-menu").bounding_box()
        self.assertGreaterEqual(bounds["x"], 0)
        self.assertGreaterEqual(bounds["y"], 0)
        self.assertLessEqual(bounds["x"] + bounds["width"], 390)
        await self.page.locator("#my-usage-open").click()
        await expect(self.page.locator("#my-usage-dialog")).to_be_visible()
        await self.page.locator("#my-usage-close").click()
        await expect(self.page.locator("#sidebar-toggle")).to_be_focused()
        await expect(self.page.locator("#account-menu-open")).not_to_be_in_viewport()
        self.assertFalse(self.errors, self.errors)


if __name__ == "__main__":
    unittest.main()
