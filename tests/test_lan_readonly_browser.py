"""CODEX: Archived client rooms retain readable history while new rooms and drafts stay independent."""

from __future__ import annotations

import unittest

from playwright.async_api import expect

import test_lan_browser as browser_fixture
from test_lan_server import PNG


class LanReadOnlyBrowserTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = browser_fixture.LanBrowserTests.asyncSetUp
    asyncTearDown = browser_fixture.LanBrowserTests.asyncTearDown
    start_worker = browser_fixture.LanBrowserTests.start_worker
    login = browser_fixture.LanBrowserTests.login

    def old_client_history(self):
        image_path = self.service.upload_dir / "archived-image.png"
        image_path.write_bytes(PNG)
        image = self.service.store.add_attachment(self.client_id, "archived-image.png", "image/png", len(PNG), str(image_path))
        receipt = self.service.store.create_run(self.client_id, self.foreign["id"], "Earlier question \u0641\u0627\u0631\u0633\u06cc", [image["id"]], "archived-seed")
        self.service.store.finish_run(receipt["run"]["id"], "completed", "Earlier answer \u0641\u0627\u0631\u0633\u06cc\n\n```python\nprint('history stays readable')\n```")
        return image

    async def test_archived_room_keeps_history_copy_images_search_and_clear_new_room_action(self):
        image = self.old_client_history()
        self.service.store.enable_server_execution(self.client_id)
        await self.context.grant_permissions(["clipboard-read", "clipboard-write"])
        await self.login(self.client)
        await expect(self.page.locator("#room-title")).to_have_text("Client-owned room")
        await expect(self.page.locator("#read-only-notice")).to_be_visible()
        await expect(self.page.locator("#read-only-title")).to_have_text("Earlier room · Read-only")
        await expect(self.page.locator("#read-only-reason")).to_contain_text("new room")
        await expect(self.page.locator("#inspector-notice")).to_be_hidden()
        await expect(self.page.locator("#composer-area")).to_be_hidden()
        for selector in ("#message-input", "#send-button", "#attach-button", "#model-select", "#effort-select", "#speed-select", "#room-menu"):
            await expect(self.page.locator(selector)).to_be_disabled()
        await expect(self.page.get_by_role("button", name="Reply", exact=True)).to_have_count(0)
        await expect(self.page.get_by_role("button", name="Side thread", exact=True)).to_have_count(0)
        await expect(self.page.locator(".message.user img")).to_be_visible()
        response = await self.page.request.get(self.base + "api/attachments/" + image["id"])
        self.assertEqual(response.status, 200)
        copy = self.page.locator(".message.assistant .message-footer .message-action").first
        await copy.click()
        await expect(copy).to_have_text("Copied")
        self.assertIn("Earlier answer", await self.page.evaluate("navigator.clipboard.readText()"))
        await self.page.locator("#room-search").fill("Client-owned")
        await expect(self.page.locator(".room-item-meta")).to_contain_text("Read-only")
        await self.page.evaluate("""() => {
            roomState().draft = 'Do not send this archived draft'; updateComposer();
            document.getElementById('send-button').disabled = false;
            sendMessage(); openRoomDialog('side', 'Earlier answer');
            addFiles([new File(['fixture'], 'blocked.png', {type:'image/png'})]);
            updateComposer();
        }""")
        await expect(self.page.locator("#room-dialog")).not_to_be_visible()
        await expect(self.page.locator("#read-only-copy-draft")).to_be_visible()
        await self.page.locator("#read-only-copy-draft").click()
        self.assertEqual(await self.page.evaluate("navigator.clipboard.readText()"), "Do not send this archived draft")
        self.assertEqual(len(self.workers[1].calls), 0)
        for width, theme in ((400, "light"), (1365, "dark"), (1365, "midnight")):
            await self.page.set_viewport_size({"width": width, "height": 900})
            await self.page.locator("#theme-select").select_option(theme)
            self.assertTrue(await self.page.evaluate("document.documentElement.scrollWidth <= innerWidth"))
            await expect(self.page.locator("#read-only-new-room")).to_be_visible()
        await self.page.screenshot(path="/tmp/codex-lan-read-only-room.png")
        await self.page.locator("#read-only-new-room").click()
        await expect(self.page.locator("#room-title")).to_have_text("New chat")
        await self.page.locator("#room-menu").click()
        await self.page.locator("#room-name-input").fill("New server room")
        await self.page.locator("#room-save").click()
        await expect(self.page.locator("#room-title")).to_have_text("New server room")
        await expect(self.page.locator("#read-only-notice")).to_be_hidden()
        await expect(self.page.locator("#message-input")).to_be_enabled()
        await expect(self.page.locator("#message-input")).to_have_value("")
        self.assertEqual(await self.page.evaluate("selectedRoom().execution_origin"), "server")
        self.assertFalse(self.errors, self.errors)

    async def test_room_switching_and_late_archive_preserve_each_draft(self):
        self.old_client_history()
        await self.login(self.client)
        await expect(self.page.locator("#room-title")).to_have_text("Client-owned room")
        await self.page.locator("#message-input").fill("Keep the earlier draft \u0641\u0627\u0631\u0633\u06cc")
        await self.page.locator(".message.assistant").get_by_role("button", name="Reply", exact=True).click()
        await expect(self.page.locator("#reply-preview")).to_be_visible()
        self.service.store.enable_server_execution(self.client_id)
        new_room = self.service.store.create_room(self.client_id, "Active new room", "/home/client", "astra", "medium")
        await self.page.evaluate("refreshRooms()")
        await expect(self.page.locator("#read-only-notice")).to_be_visible()
        await expect(self.page.get_by_role("button", name="Reply", exact=True)).to_have_count(0)
        await self.page.evaluate("id => selectRoom(id)", new_room["id"])
        await expect(self.page.locator("#message-input")).to_be_enabled()
        await expect(self.page.locator("#message-input")).to_have_value("")
        await self.page.locator("#message-input").fill("Keep the new room draft")
        await self.page.evaluate("id => selectRoom(id)", self.foreign["id"])
        await expect(self.page.locator("#read-only-notice")).to_be_visible()
        self.assertEqual(await self.page.evaluate("roomState().draft"), "Keep the earlier draft \u0641\u0627\u0631\u0633\u06cc")
        self.assertTrue(await self.page.evaluate("!!roomState().reply"))
        await self.page.evaluate("id => selectRoom(id)", new_room["id"])
        await expect(self.page.locator("#message-input")).to_have_value("Keep the new room draft")
        await expect(self.page.locator("#reply-preview")).to_be_hidden()
        await expect(self.page.get_by_role("button", name="Send message", exact=True)).to_be_enabled()
        self.assertEqual(len(self.workers[1].calls), 0)
        self.assertFalse(self.errors, self.errors)

    async def test_host_old_rooms_are_unaffected(self):
        self.service.store.enable_server_execution(self.admin_id)
        await self.login(self.admin)
        await expect(self.page.locator("#room-title")).to_have_text("Working together")
        await expect(self.page.locator("#read-only-notice")).to_be_hidden()
        await expect(self.page.locator("#message-input")).to_be_enabled()
        await expect(self.page.locator(".message.assistant").get_by_role("button", name="Reply", exact=True)).to_be_visible()
        self.assertFalse(self.errors, self.errors)

    async def test_archive_race_during_send_keeps_draft_and_explains_read_only_immediately(self):
        self.old_client_history()
        await self.login(self.client)
        await expect(self.page.locator("#room-title")).to_have_text("Client-owned room")
        await self.page.locator("#message-input").fill("Keep this unsent message")
        self.service.store.enable_server_execution(self.client_id)
        await self.page.get_by_role("button", name="Send message", exact=True).click()
        await expect(self.page.locator("#read-only-notice")).to_be_visible()
        await expect(self.page.locator("#toast")).to_contain_text("Your draft is kept")
        self.assertEqual(await self.page.evaluate("roomState().draft"), "Keep this unsent message")
        self.assertEqual(len(self.workers[1].calls), 0)
        self.assertFalse(self.errors, self.errors)


if __name__ == "__main__":
    unittest.main()
