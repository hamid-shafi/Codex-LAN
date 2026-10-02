"""CODEX: Browser checks for room indicators, motion preferences, and compact controls."""

from __future__ import annotations

import re
import unittest

from playwright.async_api import expect

import test_lan_browser as browser_fixture


class LanRoomIndicatorTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = browser_fixture.LanBrowserTests.asyncSetUp
    asyncTearDown = browser_fixture.LanBrowserTests.asyncTearDown
    start_worker = browser_fixture.LanBrowserTests.start_worker
    login = browser_fixture.LanBrowserTests.login

    async def test_active_rings_respect_reduced_motion_and_stop_when_complete(self):
        run = self.service.store.create_run(
            self.admin_id, self.room["id"], "A temporary visual fixture", [], "indicator-fixture"
        )["run"]
        await self.login(self.admin)
        await expect(self.page.locator("#room-title")).to_have_text(self.room["title"])
        activity = self.page.locator('.run-activity:not([hidden]) .activity-dot:not([hidden])')
        sidebar = self.page.locator("#room-list .running-dot:not([hidden])")
        await expect(activity).to_have_count(1)
        await expect(sidebar).to_have_count(1)
        for indicator in (activity, sidebar):
            await expect(indicator).to_have_css("animation-name", "activity-spin")
            await expect(indicator).to_have_css("animation-iteration-count", "infinite")
        self.assertTrue(await self.page.evaluate("""async () => {
            const ring = document.querySelector('#room-list .running-dot:not([hidden])');
            const animation = ring.getAnimations()[0];
            await refreshRooms();
            return ring === document.querySelector('#room-list .running-dot:not([hidden])')
                && animation === ring.getAnimations()[0];
        }"""))
        await self.page.emulate_media(reduced_motion="reduce")
        for indicator in (activity, sidebar):
            await expect(indicator).to_have_css("animation-name", "none")
        await self.page.emulate_media(reduced_motion="no-preference")
        self.service.store.finish_run(run["id"], "completed", "Fixture finished")
        await self.page.evaluate("async () => { await fetchEvents(state.selected); await refreshRooms(); renderCurrent(); }")
        await expect(self.page.locator("#room-list .running-dot:not([hidden])")).to_have_count(0)
        self.assertEqual(await self.page.locator(".activity-dot").evaluate_all(
            "nodes => nodes.filter(node => getComputedStyle(node).animationName !== 'none').length"
        ), 0)
        self.assertFalse(self.errors, self.errors)

    async def test_room_controls_and_enter_setting_fit_themes_and_mobile(self):
        background = self.service.store.create_room(
            self.admin_id, "Background tasks · اتاق دوم", "/home/server/background", "astra", "medium"
        )
        self.service.store.update_room(self.admin_id, self.room["id"], title=self.room["title"])
        await self.login(self.admin)
        await expect(self.page.locator("#room-title")).to_have_text(self.room["title"])
        await self.page.wait_for_function("() => roomState().loaded && !roomState().loading")
        completed = self.service.store.create_run(
            self.admin_id, background["id"], "A fixture for unread styling", [], "unread-style"
        )["run"]
        self.service.store.finish_run(completed["id"], "completed", "An unread fixture reply")
        active = self.service.store.create_run(
            self.admin_id, background["id"], "A fixture for concurrent indicators", [], "busy-style"
        )["run"]
        self.service.runtime.start(active, background, "A fixture for concurrent indicators", [])
        await self.page.evaluate("async () => { await refreshRooms(); }")
        background_row = self.page.locator(f'.room-row[data-room-id="{background["id"]}"]')
        await expect(background_row.locator('.running-dot:not([hidden])')).to_have_count(1)
        await expect(self.page.locator('.unread-dot')).to_have_count(0)
        await expect(background_row).to_have_class(re.compile(r"\bunread\b"))
        row = self.page.locator(f'.room-row[data-room-id="{self.room["id"]}"]')
        pin = row.locator(".room-pin")
        await pin.click()
        await expect(pin).to_have_attribute("aria-pressed", "true")
        await expect(row.locator(".room-actions .icon-button")).to_have_count(3)
        await expect(self.page.locator("#double-enter-send")).to_be_visible()
        await expect(self.page.locator("#toast")).to_be_hidden(timeout=10000)
        for theme in ("light", "dark", "midnight", "chatgpt", "torte", "cosmic", "aurora", "ember", "velvet"):
            await self.page.locator("#theme-select").select_option(theme)
            await self.page.screenshot(path=f"/tmp/codex-lan-room-indicators-{theme}.png", full_page=True)
            await expect(pin).not_to_have_css("background-color", "rgba(0, 0, 0, 0)")
        for width in (400, 768, 1365):
            await self.page.set_viewport_size({"width": width, "height": 900})
            if width == 400:
                await self.page.locator("#sidebar-toggle").click()
                await expect(self.page.locator("#sidebar")).to_have_css("transform", "matrix(1, 0, 0, 1, 0, 0)")
            dimensions = await row.evaluate("""row => {
                const title = row.querySelector('.room-item-title').getBoundingClientRect();
                const actions = row.querySelector('.room-actions').getBoundingClientRect();
                const bounds = row.getBoundingClientRect();
                return {titleWidth: title.width, titleBottom: title.bottom, actionsTop: actions.top,
                    actionsRight: actions.right, rowRight: bounds.right};
            }""")
            self.assertGreaterEqual(dimensions["titleWidth"], 160, dimensions)
            self.assertLessEqual(dimensions["titleBottom"], dimensions["actionsTop"], dimensions)
            self.assertLessEqual(dimensions["actionsRight"], dimensions["rowRight"], dimensions)
            self.assertTrue(await self.page.evaluate(
                "document.documentElement.scrollWidth <= innerWidth"
            ))
            await self.page.screenshot(path=f"/tmp/codex-lan-room-indicators-{width}.png", full_page=True)
            if width == 400:
                await self.page.locator("#sidebar-scrim").click(position={"x": 350, "y": 450})
                await expect(self.page.locator("#sidebar")).to_have_css("transform", "matrix(1, 0, 0, 1, -270, 0)")
                await expect(self.page.locator("#double-enter-send")).to_be_visible()
                await self.page.screenshot(path="/tmp/codex-lan-room-indicators-mobile-composer.png", full_page=True)
        self.assertFalse(self.errors, self.errors)


if __name__ == "__main__":
    unittest.main()
