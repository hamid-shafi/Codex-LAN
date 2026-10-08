"""CODEX: Browser regressions for abandoning, racing, and committing image uploads."""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path

from playwright.async_api import expect

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from project_layout import configure_imports, source_file
configure_imports(__file__)

import test_lan_browser as browser_fixture
from test_lan_server import PNG


class LanAttachmentLifecycleBrowserTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = browser_fixture.LanBrowserTests.asyncSetUp
    asyncTearDown = browser_fixture.LanBrowserTests.asyncTearDown
    start_worker = browser_fixture.LanBrowserTests.start_worker
    login = browser_fixture.LanBrowserTests.login

    def attachments(self):
        return list(self.service.store.db.execute(
            "SELECT id,path FROM attachments WHERE owner_id=? ORDER BY created_at,id",
            (self.admin_id,),
        ))

    async def wait_for_attachment_count(self, expected):
        for _ in range(300):
            if len(self.attachments()) == expected:
                return
            await asyncio.sleep(.01)
        self.fail(f"Expected {expected} attachments, found {len(self.attachments())}")

    async def upload(self):
        await self.page.locator("#attachment-input").set_input_files({
            "name": "image.png", "mimeType": "image/png", "buffer": PNG,
        })
        await expect(self.page.locator(".attachment-card small")).to_have_text("Ready")
        rows = self.attachments()
        self.assertTrue(rows)
        return rows[-1]

    async def test_abandoned_uploads_are_discarded_but_sent_images_survive_navigation(self):
        page = self.page
        await self.login(self.admin)
        await expect(page.locator("#workspace")).to_be_visible()

        removed = await self.upload()
        removed_path = Path(removed["path"])
        await page.locator(".attachment-card button").click()
        await self.wait_for_attachment_count(0)
        self.assertFalse(removed_path.exists())

        started, release, continued = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def hold_upload(route, request):
            started.set()
            await release.wait()
            try:
                await route.continue_()
            finally:
                continued.set()

        await page.route("**/api/attachments", hold_upload)
        await page.locator("#attachment-input").set_input_files({
            "name": "racing.png", "mimeType": "image/png", "buffer": PNG,
        })
        await asyncio.wait_for(started.wait(), timeout=3)
        await expect(page.locator(".attachment-card small")).to_have_text("Uploading…")
        await page.locator(".attachment-card button").click()
        release.set()
        await asyncio.wait_for(continued.wait(), timeout=3)
        await page.unroute("**/api/attachments", hold_upload)
        await self.wait_for_attachment_count(0)
        await expect(page.locator(".attachment-card")).to_have_count(0)

        abandoned = await self.upload()
        abandoned_path = Path(abandoned["path"])
        await page.evaluate(
            "dispatchEvent(new PageTransitionEvent('pagehide', { persisted: true }))"
        )
        await asyncio.sleep(.05)
        self.assertEqual(len(self.attachments()), 1)
        self.assertTrue(abandoned_path.is_file())
        await page.goto("about:blank")
        await self.wait_for_attachment_count(0)
        self.assertFalse(abandoned_path.exists())

        await page.goto(self.base)
        await expect(page.locator("#workspace")).to_be_visible()
        committed = await self.upload()
        committed_path = Path(committed["path"])
        before = await page.locator(".message.user").count()
        await page.locator("#message-input").fill("Keep the attached image")
        await page.locator("#send-button").click()
        await expect(page.locator(".message.user")).to_have_count(before + 1)
        await expect(page.locator(".attachment-card")).to_have_count(0)
        await page.goto("about:blank")
        await asyncio.sleep(.1)

        self.assertTrue(committed_path.is_file())
        self.assertEqual(len(self.attachments()), 1)
        self.assertEqual(self.attachments()[0]["id"], committed["id"])
        self.assertFalse(self.errors, self.errors)

    async def test_releasing_room_during_batch_upload_discards_inflight_and_stops_batch(self):
        page = self.page
        await self.login(self.admin)
        await expect(page.locator("#workspace")).to_be_visible()
        started, release, continued = asyncio.Event(), asyncio.Event(), asyncio.Event()
        requests = []

        async def hold_upload(route, request):
            requests.append(request.url)
            started.set()
            await release.wait()
            try:
                await route.continue_()
            finally:
                continued.set()

        await page.route("**/api/attachments", hold_upload)
        await page.locator("#attachment-input").set_input_files([
            {"name": "first.png", "mimeType": "image/png", "buffer": PNG},
            {"name": "second.png", "mimeType": "image/png", "buffer": PNG},
        ])
        await asyncio.wait_for(started.wait(), timeout=3)
        released = await page.evaluate("""() => {
            const cache = roomState();
            const file = cache.files[0];
            releaseRoomState(cache);
            return { released: cache.released, files: cache.files.length, discardRequested: file.discardRequested };
        }""")
        self.assertEqual(released, {"released": True, "files": 0, "discardRequested": True})
        release.set()
        await asyncio.wait_for(continued.wait(), timeout=3)
        await self.wait_for_attachment_count(0)
        await asyncio.sleep(.1)
        self.assertEqual(len(requests), 1)
        self.assertFalse(self.errors, self.errors)

    async def test_session_logout_waits_for_inflight_upload_cleanup(self):
        page = self.page
        await self.login(self.admin)
        await expect(page.locator("#workspace")).to_be_visible()
        upload_started, release_upload = asyncio.Event(), asyncio.Event()
        logout_started, release_logout = asyncio.Event(), asyncio.Event()

        async def hold_upload(route, request):
            upload_started.set()
            await release_upload.wait()
            await route.continue_()

        async def hold_logout(route, request):
            logout_started.set()
            await release_logout.wait()
            await route.continue_()

        await page.route("**/api/attachments", hold_upload)
        await page.route("**/api/logout", hold_logout)
        await page.locator("#attachment-input").set_input_files({
            "name": "before-logout.png", "mimeType": "image/png", "buffer": PNG,
        })
        await asyncio.wait_for(upload_started.wait(), timeout=3)
        logout = asyncio.create_task(page.evaluate("logoutBrowser()"))
        await asyncio.sleep(.1)
        self.assertFalse(logout_started.is_set())
        release_upload.set()
        await asyncio.wait_for(logout_started.wait(), timeout=3)
        self.assertEqual(self.attachments(), [])
        release_logout.set()
        await asyncio.wait_for(logout, timeout=3)
        await expect(page.locator("#gate")).to_be_visible()
        self.assertFalse(self.errors, self.errors)


if __name__ == "__main__":
    unittest.main()
