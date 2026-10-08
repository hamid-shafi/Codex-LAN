"""CODEX: Room reload checks use temporary state, fake workers, and real Chromium."""

from __future__ import annotations

import unittest
from urllib.parse import parse_qs, urlsplit

from playwright.async_api import expect

import test_lan_browser as browser_fixture


class LanRoomReloadTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = browser_fixture.LanBrowserTests.asyncSetUp
    asyncTearDown = browser_fixture.LanBrowserTests.asyncTearDown
    start_worker = browser_fixture.LanBrowserTests.start_worker
    login = browser_fixture.LanBrowserTests.login
    wait_worker_turn = browser_fixture.LanBrowserTests.wait_worker_turn

    def extra_room(self, owner=None, title="A second room"):
        return self.service.store.create_room(
            owner or self.admin_id, title, "/home/server/second", "astra", "medium"
        )

    async def choose(self, room, page=None):
        page = page or self.page
        await page.locator(f'#room-list .room-row[data-room-id="{room["id"]}"] .room-item').click()
        await expect(page.locator("#room-title")).to_have_text(room["title"])
        await page.wait_for_function("() => roomState().loaded && !roomState().loading")

    async def assert_selected(self, room, page=None):
        page = page or self.page
        await expect(page.locator("#room-title")).to_have_text(room["title"])
        await page.wait_for_function("() => roomState().loaded && !roomState().loading")
        self.assertEqual(parse_qs(urlsplit(page.url).query).get("room"), [room["id"]])
        self.assertEqual(await page.evaluate("state.selected"), room["id"])

    async def saved_room(self, device_id):
        return await self.page.evaluate(
            "id => localStorage.getItem(`codex-lan-room:${id}`)", device_id
        )

    async def test_nonfirst_room_survives_reload_and_room_reordering(self):
        second = self.extra_room()
        await self.login(self.admin)
        await self.choose(second)
        self.assertEqual(await self.saved_room(self.admin_id), second["id"])
        self.service.store.update_room(self.admin_id, self.room["id"], title=self.room["title"])
        self.assertEqual(self.service.store.list_rooms(self.admin_id)[0]["id"], self.room["id"])
        await self.page.reload()
        await self.assert_selected(second)
        self.assertFalse(self.errors, self.errors)

    async def test_two_tabs_reload_their_own_room(self):
        second = self.extra_room()
        await self.login(self.admin)
        await self.choose(self.room)
        other_page = await self.context.new_page()
        await other_page.goto(self.base)
        await self.assert_selected(self.room, other_page)
        await self.choose(second, other_page)
        self.assertEqual(await self.saved_room(self.admin_id), second["id"])
        await self.page.reload()
        await self.assert_selected(self.room)
        await other_page.reload()
        await self.assert_selected(second, other_page)
        await self.assert_selected(self.room)

    async def test_device_preferences_are_isolated_and_url_access_is_checked(self):
        second = self.extra_room()
        await self.login(self.admin)
        await self.choose(second)
        await self.login(self.client)
        await self.assert_selected(self.foreign)
        self.assertEqual(await self.saved_room(self.admin_id), second["id"])
        self.assertEqual(await self.saved_room(self.client_id), self.foreign["id"])
        await self.page.goto(self.base + "?room=" + second["id"])
        await self.assert_selected(self.foreign)
        await self.login(self.admin)
        await self.assert_selected(second)

    async def test_login_fragment_is_removed_and_authorized_query_wins(self):
        second = self.extra_room()
        await self.login(self.admin)
        await self.choose(self.room)
        await self.page.goto(
            self.base + "?view=chat&room=" + second["id"] + "#login=" + self.admin["token"]
        )
        await self.assert_selected(second)
        self.assertEqual(urlsplit(self.page.url).fragment, "")
        self.assertEqual(parse_qs(urlsplit(self.page.url).query)["view"], ["chat"])

    async def test_deleted_saved_room_falls_back_and_updates_preference(self):
        second = self.extra_room()
        await self.login(self.admin)
        await self.choose(second)
        self.service.store.delete_room(self.admin_id, second["id"])
        await self.page.reload()
        await self.assert_selected(self.room)
        self.assertEqual(await self.saved_room(self.admin_id), self.room["id"])
        self.assertFalse(self.errors, self.errors)

    async def test_unauthorized_saved_room_falls_back_to_owned_room(self):
        await self.login(self.client)
        await self.assert_selected(self.foreign)
        await self.page.evaluate(
            "([deviceId, roomId]) => localStorage.setItem(`codex-lan-room:${deviceId}`, roomId)",
            [self.client_id, self.room["id"]],
        )
        await self.page.goto(self.base + "?room=" + self.room["id"])
        await self.assert_selected(self.foreign)
        self.assertEqual(await self.saved_room(self.client_id), self.foreign["id"])
        self.assertFalse(self.errors, self.errors)

    async def test_storage_errors_do_not_prevent_url_reload(self):
        second = self.extra_room()
        await self.context.add_init_script("""
            for (const method of ['getItem', 'setItem', 'removeItem']) {
                Storage.prototype[method] = () => { throw new DOMException('Storage disabled', 'SecurityError'); };
            }
        """)
        await self.login(self.admin)
        await self.choose(second)
        self.service.store.update_room(self.admin_id, self.room["id"], title=self.room["title"])
        await self.page.reload()
        await self.assert_selected(second)
        self.assertFalse(self.errors, self.errors)

    async def test_no_rooms_clears_url_and_device_preference(self):
        await self.login(self.client)
        await self.assert_selected(self.foreign)
        self.service.store.delete_room(self.client_id, self.foreign["id"])
        await self.page.reload()
        await expect(self.page.locator("#workspace")).to_be_visible()
        await expect(self.page.locator("#room-title")).to_have_text("Your workspace")
        self.assertNotIn("room", parse_qs(urlsplit(self.page.url).query))
        self.assertIsNone(await self.saved_room(self.client_id))
        self.assertIsNone(await self.page.evaluate("state.selected"))
        self.assertFalse(self.errors, self.errors)

    async def test_reload_reattaches_to_running_room_without_resubmitting(self):
        second = self.extra_room()
        await self.login(self.admin)
        await self.choose(second)
        await self.page.locator("#message-input").fill("Continue across reload")
        await self.page.locator("#message-input").press("Enter")
        await self.page.locator("#message-input").press("Enter")
        await self.wait_worker_turn()
        await self.worker.delta("Before reload")
        await expect(self.page.locator(".message.assistant .message-content")).to_have_text("Before reload")
        await self.page.reload()
        await self.assert_selected(second)
        await expect(self.page.locator(".message.user")).to_have_count(1)
        await self.worker.delta(" and after reload")
        await expect(self.page.locator(".message.assistant .message-content")).to_have_text(
            "Before reload and after reload", timeout=10000
        )
        self.assertEqual(sum(call["method"] == "turn/start" for call in self.worker.calls), 1)
        await self.worker.complete()
        await expect(self.page.locator("#stop-button")).to_be_hidden(timeout=10000)
        self.assertFalse(self.errors, self.errors)


if __name__ == "__main__":
    unittest.main()
