"""CODEX: Sidebar animation keeps the active conversation, draft, and reading position."""

from __future__ import annotations

import re
import unittest

from playwright.async_api import expect

import test_lan_browser as browser_fixture


class LanSidebarBrowserTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = browser_fixture.LanBrowserTests.asyncSetUp
    asyncTearDown = browser_fixture.LanBrowserTests.asyncTearDown
    start_worker = browser_fixture.LanBrowserTests.start_worker
    login = browser_fixture.LanBrowserTests.login
    wait_worker_turn = browser_fixture.LanBrowserTests.wait_worker_turn

    async def test_collapse_preserves_draft_focus_scroll_and_reload_preference(self):
        page = self.page
        await self.login(self.admin)
        await expect(page.locator("#room-title")).to_have_text("Working together")
        toggle = page.locator("#sidebar-toggle")
        await expect(toggle).to_be_visible()
        await expect(toggle).to_have_attribute("aria-expanded", "true")
        await page.locator("#message-input").fill("Keep this draft فارسی")
        await page.evaluate("""() => {
            const input = document.getElementById('message-input'); input.setSelectionRange(4, 9);
            const scroll = document.getElementById('timeline-scroll'); scroll.scrollTop = 100;
            state.follow = false;
            const top = scroll.getBoundingClientRect().top;
            window.sidebarAnchor = [...document.querySelectorAll('.message-content > *, .run-activity, .message-header')].find(node => node.getBoundingClientRect().bottom > top + 8);
            window.sidebarAnchorOffset = window.sidebarAnchor.getBoundingClientRect().top - top;
            window.sidebarTimeline = document.getElementById('timeline');
            window.sidebarFirstMessage = window.sidebarTimeline.firstChild;
            window.sidebarActiveRoom = state.selected;
        }""")
        await toggle.click()
        await expect(toggle).to_have_attribute("aria-expanded", "false")
        await expect(toggle).to_have_attribute("aria-label", "Show rooms")
        await page.wait_for_function("() => !state.sidebarTransition")
        await expect(page.locator("#sidebar")).to_have_attribute("inert", "")
        await expect(page.locator("#message-input")).to_be_focused()
        await expect(page.locator("#message-input")).to_have_value("Keep this draft فارسی")
        self.assertEqual(await page.locator("#message-input").evaluate("el => [el.selectionStart, el.selectionEnd]"), [4, 9])
        self.assertTrue(await page.evaluate("document.getElementById('timeline')===window.sidebarTimeline && document.getElementById('timeline').firstChild===window.sidebarFirstMessage && state.selected===window.sidebarActiveRoom"))
        self.assertLess(abs(await page.evaluate("window.sidebarAnchor.getBoundingClientRect().top-document.getElementById('timeline-scroll').getBoundingClientRect().top-window.sidebarAnchorOffset")), 4)
        self.assertFalse(await page.evaluate("state.follow"))
        await page.reload()
        await expect(page.locator("#room-title")).to_have_text("Working together")
        await expect(page.locator("#message-input")).to_have_value("Keep this draft فارسی")
        await expect(toggle).to_have_attribute("aria-expanded", "false")
        await expect(page.locator("#sidebar")).to_have_attribute("aria-hidden", "true")
        await toggle.focus()
        await page.keyboard.press("Enter")
        await expect(toggle).to_have_attribute("aria-expanded", "true")
        await page.wait_for_function("() => !state.sidebarTransition")
        await expect(toggle).to_be_focused()
        await expect(page.locator("#sidebar")).not_to_have_attribute("inert", "")
        self.assertTrue(await page.evaluate("document.documentElement.scrollWidth<=innerWidth"))
        await page.screenshot(path="/tmp/codex-lan-sidebar-toggle.png")
        self.assertFalse(self.errors, self.errors)

    async def test_room_drafts_are_isolated_and_removed_after_send_or_delete(self):
        page = self.page
        disposable = self.service.store.create_room(
            self.admin_id,
            "Disposable drafted room",
            "/home/server/disposable-draft",
            "astra",
            "medium",
        )
        await self.login(self.admin)
        await page.locator("#room-list .room-item").filter(
            has_text="Working together"
        ).click()
        await expect(page.locator("#room-title")).to_have_text("Working together")
        device_id = self.admin["device"]["id"]
        primary_key = f"codex-lan-draft:{device_id}:{self.room['id']}"
        disposable_key = f"codex-lan-draft:{device_id}:{disposable['id']}"
        other_device_key = f"codex-lan-draft:not-{device_id}:{self.room['id']}"
        await page.evaluate(
            "([key, value]) => localStorage.setItem(key, value)",
            [other_device_key, "Draft from another device must not leak"],
        )
        await page.reload()
        await expect(page.locator("#message-input")).to_have_value("")

        await page.locator("#message-input").fill("Primary room survives reload فارسی")
        self.assertEqual(
            await page.evaluate("key => localStorage.getItem(key)", primary_key),
            "Primary room survives reload فارسی",
        )
        await page.locator("#room-list .room-item").filter(
            has_text="Disposable drafted room"
        ).click()
        await expect(page.locator("#message-input")).to_have_value("")
        await page.locator("#message-input").fill("Delete this room draft")
        self.assertEqual(
            await page.evaluate("key => localStorage.getItem(key)", disposable_key),
            "Delete this room draft",
        )

        await page.reload()
        await expect(page.locator("#room-title")).to_have_text("Disposable drafted room")
        await expect(page.locator("#message-input")).to_have_value(
            "Delete this room draft"
        )
        await page.locator("#room-list .room-item").filter(
            has_text="Working together"
        ).click()
        await expect(page.locator("#message-input")).to_have_value(
            "Primary room survives reload فارسی"
        )

        disposable_row = page.locator(
            f".room-row[data-room-id='{disposable['id']}']"
        )
        page.once("dialog", lambda dialog: dialog.accept())
        await disposable_row.get_by_role(
            "button", name="Delete Disposable drafted room", exact=True
        ).click()
        await expect(disposable_row).to_have_count(0)
        self.assertIsNone(
            await page.evaluate("key => localStorage.getItem(key)", disposable_key)
        )
        await expect(page.locator("#message-input")).to_have_value(
            "Primary room survives reload فارسی"
        )

        await page.get_by_role("button", name="Send message", exact=True).click()
        await self.wait_worker_turn()
        await expect(page.locator("#message-input")).to_have_value("")
        self.assertIsNone(
            await page.evaluate("key => localStorage.getItem(key)", primary_key)
        )
        self.assertEqual(
            await page.evaluate("key => localStorage.getItem(key)", other_device_key),
            "Draft from another device must not leak",
        )
        await self.worker.complete()
        self.assertFalse(self.errors, self.errors)

    async def test_mobile_drawer_and_reduced_motion_do_not_overwrite_desktop_preference(self):
        page = self.page
        await self.login(self.admin)
        await expect(page.locator("#room-title")).to_have_text("Working together")
        await page.locator("#sidebar-toggle").click()
        await page.wait_for_function("() => !state.sidebarTransition")
        await page.set_viewport_size({"width": 400, "height": 850})
        toggle = page.locator("#sidebar-toggle")
        await expect(toggle).to_have_attribute("aria-expanded", "false")
        await toggle.focus()
        await page.keyboard.press("Space")
        await expect(toggle).to_have_attribute("aria-expanded", "true")
        await expect(page.locator("#sidebar-scrim")).to_be_visible()
        await expect(page.locator("#sidebar")).not_to_have_attribute("inert", "")
        await page.keyboard.press("Escape")
        await expect(toggle).to_have_attribute("aria-expanded", "false")
        await expect(toggle).to_be_focused()
        await expect(page.locator("#sidebar-scrim")).to_be_hidden()
        self.assertTrue(await page.evaluate("document.documentElement.scrollWidth <= innerWidth"))
        await page.set_viewport_size({"width": 1365, "height": 900})
        await expect(toggle).to_have_attribute("aria-expanded", "false")
        await page.emulate_media(reduced_motion="reduce")
        await toggle.click()
        await expect(toggle).to_have_attribute("aria-expanded", "true")
        self.assertEqual(await page.locator("#sidebar").evaluate("el => getComputedStyle(el).transitionDuration"), "0s")
        self.assertFalse(self.errors, self.errors)

    async def test_open_room_stays_selected_but_unread_styles_only_emphasize_owned_rooms(self):
        await self.login(self.admin)
        await self.page.emulate_media(reduced_motion="reduce")
        await self.page.mouse.move(0, 0)
        for theme in ("light", "dark", "midnight", "chatgpt", "torte", "cosmic", "aurora", "ember", "velvet"):
            await self.page.locator("#theme-select").select_option(theme)
            for scope in ("own", "other"):
                styles = await self.page.locator(f".room-row.{scope}").first.evaluate("""row => {
                    const button = row.querySelector('.room-item');
                    const classes = [row.className, button.className];
                    const read = () => { const s=getComputedStyle(button), marker=getComputedStyle(button, '::before'); return [s.backgroundColor,s.backgroundImage,s.borderTopColor,s.boxShadow,marker.backgroundColor,marker.width]; };
                    button.classList.add('active');
                    const selected = read();
                    button.classList.remove('active'); row.classList.add('unread');
                    const unread = read(); row.classList.remove('unread');
                    const normal = read();
                    [row.className, button.className] = classes;
                    return {selected,unread,normal};
                }""")
                compare = self.assertNotEqual if scope == "own" else self.assertEqual
                self.assertNotEqual(styles["selected"], styles["normal"], (theme, scope, styles))
                compare(styles["unread"], styles["normal"], (theme, scope, styles))
                self.assertEqual(styles["normal"][3], "none", (theme, scope, styles))
        await expect(self.page.locator('.room-item[aria-current="page"]')).to_have_count(1)
        active = self.page.locator('.room-item[aria-current="page"]')
        for theme in ("light", "dark", "midnight", "chatgpt", "torte", "cosmic", "aurora", "ember", "velvet"):
            await self.page.locator("#theme-select").select_option(theme)
            before = await active.evaluate("node => {const s=getComputedStyle(node, '::before'); return [s.content,s.backgroundColor,s.width,s.height];}")
            await active.hover()
            hovered = await active.evaluate("node => {const s=getComputedStyle(node, '::before'); return [s.content,s.backgroundColor,s.width,s.height];}")
            self.assertEqual(hovered, before, (theme, before, hovered))
            self.assertNotEqual(hovered[1], "rgba(0, 0, 0, 0)", (theme, hovered))
            self.assertEqual(hovered[2], "3px", (theme, hovered))
        await active.focus()
        self.assertEqual(await active.evaluate("node => getComputedStyle(node).outlineStyle"), "solid")
        await self.page.locator("#theme-select").select_option("cosmic")
        await self.page.locator("#message-input").focus()
        await self.page.screenshot(path="/tmp/codex-room-owner-highlights.png")
        self.assertFalse(self.errors, self.errors)

    async def test_unread_is_personal_persists_and_admin_reading_does_not_mark_owner_read(self):
        own_background = self.service.store.create_room(self.admin_id, "My background room", "/work/admin")
        client_idle = self.service.store.create_room(self.client_id, "Client idle room", "/work/client")
        await self.login(self.admin)
        await self.page.evaluate("id => selectRoom(id)", self.room["id"])
        owner_context = await self.browser.new_context(viewport={"width": 1365, "height": 900})
        owner_page = await owner_context.new_page()
        owner_page.on("pageerror", lambda error: self.errors.append(str(error)))
        try:
            await self.login(self.client, page=owner_page)
            await owner_page.evaluate("id => selectRoom(id)", client_idle["id"])
            own_row = self.page.locator(f'.room-row[data-room-id="{own_background["id"]}"]')
            other_row = self.page.locator(f'.room-row[data-room-id="{self.foreign["id"]}"]')
            owner_row = owner_page.locator(f'.room-row[data-room-id="{self.foreign["id"]}"]')
            runs = []
            for room in (own_background, self.foreign):
                runs.append(self.service.store.create_run(
                    room["owner_id"], room["id"], "A sent prompt is not an unread reply", [], "personal-" + room["id"]
                )["run"])
            for page in (self.page, owner_page):
                await page.evaluate("refreshRooms()")
            for row in (own_row, other_row, owner_row):
                await expect(row).not_to_have_class(re.compile(r"\bunread\b"))
                await expect(row.locator(".unread-dot")).to_be_hidden()
            for run in runs:
                self.service.store.finish_run(run["id"], "completed", "A new reply for this room's owner")
            for page in (self.page, owner_page):
                await page.evaluate("refreshRooms()")
            await expect(own_row).to_have_class(re.compile(r"\bunread\b"))
            await expect(owner_row).to_have_class(re.compile(r"\bunread\b"))
            await expect(other_row).not_to_have_class(re.compile(r"\bunread\b"))
            await expect(own_row.locator(".unread-dot")).to_be_visible()
            await expect(owner_row.locator(".unread-dot")).to_be_visible()
            await expect(other_row.locator(".unread-dot")).to_be_hidden()
            await self.page.locator("#theme-select").select_option("cosmic")
            unread_style = await own_row.locator(".unread-dot").evaluate(
                "node => { const style = getComputedStyle(node); const ripple = getComputedStyle(node, '::after'); return { width: style.width, height: style.height, radius: style.borderRadius, background: style.backgroundColor, shadow: style.boxShadow, ripple: ripple.animationName }; }"
            )
            self.assertEqual(unread_style["width"], "7px")
            self.assertEqual(unread_style["height"], "7px")
            self.assertEqual(unread_style["radius"], "50%")
            self.assertNotEqual(unread_style["background"], "rgba(0, 0, 0, 0)")
            self.assertNotEqual(unread_style["shadow"], "none")
            self.assertEqual(unread_style["ripple"], "cosmic-unread-ripple")
            await expect(other_row.locator(".room-item")).not_to_have_attribute("aria-label", re.compile("New response"))
            seen_before = await owner_page.evaluate("id => sidebarPreferences().seen[id]", self.foreign["id"])
            await other_row.locator(".room-item").click()
            await self.page.wait_for_function("() => roomState().loaded && !roomState().loading")
            await self.page.evaluate("renderRooms()")
            await expect(other_row.locator(".room-item")).to_have_attribute("aria-current", "page")
            self.assertFalse(await self.page.evaluate(
                "id => Object.hasOwn(sidebarPreferences().seen, id)", self.foreign["id"]
            ))
            self.assertEqual(await owner_page.evaluate("id => sidebarPreferences().seen[id]", self.foreign["id"]), seen_before)
            for page in (self.page, owner_page):
                await page.reload()
                await page.wait_for_function("() => state.routeReady && roomState().loaded && !roomState().loading")
            await expect(own_row).to_have_class(re.compile(r"\bunread\b"))
            await expect(owner_row).to_have_class(re.compile(r"\bunread\b"))
            await expect(other_row).not_to_have_class(re.compile(r"\bunread\b"))
            await owner_row.locator(".room-item").click()
            await expect(owner_row).not_to_have_class(re.compile(r"\bunread\b"))
            await expect(owner_row.locator(".unread-dot")).to_be_hidden()
            await owner_page.reload()
            await owner_page.wait_for_function("() => state.routeReady && roomState().loaded && !roomState().loading")
            await expect(owner_row).not_to_have_class(re.compile(r"\bunread\b"))
            await own_row.locator(".room-item").click()
            await expect(own_row).not_to_have_class(re.compile(r"\bunread\b"))
            await expect(own_row.locator(".unread-dot")).to_be_hidden()
            self.assertFalse(self.errors, self.errors)
        finally:
            await owner_context.close()


if __name__ == "__main__":
    unittest.main()
