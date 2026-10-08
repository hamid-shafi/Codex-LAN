"""CODEX: Cached room switches stay instant and preserve each room's reading position."""

from __future__ import annotations

import asyncio
import unittest
from pathlib import Path

from playwright.async_api import expect

import test_lan_browser as browser_fixture
from test_lan_server import PNG


class LanRoomCacheBrowserTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = browser_fixture.LanBrowserTests.asyncSetUp
    asyncTearDown = browser_fixture.LanBrowserTests.asyncTearDown
    start_worker = browser_fixture.LanBrowserTests.start_worker
    login = browser_fixture.LanBrowserTests.login

    async def test_returning_to_cached_room_is_immediate_and_restores_scroll(self):
        store = self.service.store
        for index in range(18):
            run = store.create_run(
                self.admin_id,
                self.room["id"],
                f"Cached prompt {index}",
                [],
                f"cached-room-{index}",
            )
            store.finish_run(
                run["run"]["id"],
                "completed",
                "\n\n".join(
                    f"Cached answer {index}, paragraph {paragraph}: "
                    "\u0645\u062a\u0646 \u0641\u0627\u0631\u0633\u06cc \u0647\u0645\u0631\u0627\u0647 English details \u0628\u0631\u0627\u06cc \u062d\u0641\u0638 \u0645\u0648\u0642\u0639\u06cc\u062a \u0645\u0637\u0627\u0644\u0639\u0647."
                    for paragraph in range(4)
                ),
            )
        other = store.create_room(
            self.admin_id,
            "Fast room switch",
            "/home/server/other",
            "astra",
            "medium",
        )

        await self.login(self.admin)
        await self.page.evaluate("id => selectRoom(id)", self.room["id"])
        await expect(self.page.locator("#room-title")).to_have_text("Working together")
        await self.page.wait_for_function("() => roomState().loaded && !roomState().loading")
        saved_top = await self.page.locator("#timeline-scroll").evaluate(
            """element => {
                element.scrollTop = Math.floor((element.scrollHeight - element.clientHeight) * 0.38);
                element.dispatchEvent(new Event('scroll'));
                return element.scrollTop;
            }"""
        )
        self.assertGreater(saved_top, 100)

        await self.page.evaluate("id => selectRoom(id)", other["id"])
        await expect(self.page.locator("#room-title")).to_have_text("Fast room switch")
        await self.page.wait_for_function("() => roomState().loaded && !roomState().loading")

        release = asyncio.Event()
        refresh_started = asyncio.Event()

        async def delay_refresh(route):
            refresh_started.set()
            await release.wait()
            await route.continue_()

        await self.page.route(
            f"**/api/rooms/{self.room['id']}/messages*",
            delay_refresh,
        )
        try:
            await self.page.evaluate("id => { selectRoom(id); }", self.room["id"])
            await expect(self.page.locator("#room-title")).to_have_text(
                "Working together", timeout=300
            )
            await expect(self.page.locator("#timeline")).to_contain_text(
                "Cached answer 17", timeout=300
            )
            restored_top = await self.page.locator("#timeline-scroll").evaluate(
                "element => element.scrollTop"
            )
            self.assertLess(abs(restored_top - saved_top), 8)

            try:
                await asyncio.wait_for(refresh_started.wait(), timeout=0.5)
            except asyncio.TimeoutError:
                pass
            release.set()
            await asyncio.sleep(0.25)
            final_top = await self.page.locator("#timeline-scroll").evaluate(
                "element => element.scrollTop"
            )
            self.assertLess(abs(final_top - saved_top), 8)
            self.assertFalse(await self.page.evaluate("state.follow"))
            self.assertFalse(self.errors, self.errors)
        finally:
            release.set()

    async def test_delayed_older_page_cannot_move_a_new_visit(self):
        store = self.service.store
        for index in range(18):
            run = store.create_run(
                self.admin_id,
                self.room["id"],
                f"Older-page prompt {index}",
                [],
                f"older-page-{index}",
            )
            store.finish_run(
                run["run"]["id"],
                "completed",
                (f"Older-page answer {index}: \u0645\u062a\u0646 \u0641\u0627\u0631\u0633\u06cc and English detail.\n\n" * 5),
            )
        other = store.create_room(
            self.admin_id,
            "Room between visits",
            "/home/server/between",
            "astra",
            "medium",
        )

        await self.login(self.admin)
        await self.page.evaluate("id => selectRoom(id)", self.room["id"])
        await self.page.wait_for_function("() => roomState().loaded && !roomState().loading")
        old_top = await self.page.locator("#timeline-scroll").evaluate(
            """element => {
                element.scrollTop = Math.min(120, element.scrollHeight - element.clientHeight);
                element.dispatchEvent(new Event('scroll'));
                return element.scrollTop;
            }"""
        )
        self.assertGreater(old_top, 20)

        release = asyncio.Event()
        request_started = asyncio.Event()

        async def delay_older_page(route):
            if "before=" in route.request.url:
                request_started.set()
                await release.wait()
            await route.continue_()

        await self.page.route(
            f"**/api/rooms/{self.room['id']}/messages*",
            delay_older_page,
        )
        try:
            await self.page.evaluate("() => { void loadOlder(); }")
            await asyncio.wait_for(request_started.wait(), timeout=1)
            await self.page.evaluate("id => selectRoom(id)", other["id"])
            await self.page.evaluate("id => selectRoom(id)", self.room["id"])
            new_top = await self.page.locator("#timeline-scroll").evaluate(
                """element => {
                    const maximum = element.scrollHeight - element.clientHeight;
                    element.scrollTop = Math.floor(maximum * 0.72);
                    element.dispatchEvent(new Event('scroll'));
                    return element.scrollTop;
                }"""
            )
            self.assertGreater(abs(new_top - old_top), 100)
            release.set()
            await self.page.wait_for_function(
                "() => !document.getElementById('load-older').disabled"
            )
            await asyncio.sleep(0.1)
            final_top = await self.page.locator("#timeline-scroll").evaluate(
                "element => element.scrollTop"
            )
            self.assertLess(abs(final_top - new_top), 8)
            self.assertFalse(self.errors, self.errors)
        finally:
            release.set()

    async def test_image_load_does_not_interrupt_selection_or_admin(self):
        image_path = Path(self.service.upload_dir) / "cache-selection.png"
        image_path.write_bytes(PNG)
        attachment = self.service.store.add_attachment(
            self.admin_id,
            "Selection image.png",
            "image/png",
            len(PNG),
            str(image_path),
        )
        run = self.service.store.create_run(
            self.admin_id,
            self.room["id"],
            "Keep this selection stable",
            [attachment["id"]],
            "image-selection",
        )
        self.service.store.finish_run(
            run["run"]["id"],
            "completed",
            "Select this answer while the image finishes loading.",
        )

        await self.login(self.admin)
        image = self.page.locator(".message-attachment img").last
        await expect(image).to_be_visible()
        selected = await self.page.evaluate(
            """() => {
                window.imageScrollBottomCalls = 0;
                const original = scrollBottom;
                scrollBottom = () => { window.imageScrollBottomCalls += 1; return original(); };
                const content = [...document.querySelectorAll('.message.assistant .message-content')].at(-1);
                const range = document.createRange();
                range.selectNodeContents(content);
                const selection = window.getSelection();
                selection.removeAllRanges(); selection.addRange(range);
                state.follow = true; roomState().follow = true;
                document.querySelectorAll('.message-attachment img')[document.querySelectorAll('.message-attachment img').length - 1].dispatchEvent(new Event('load'));
                return selection.toString();
            }"""
        )
        self.assertIn("Select this answer", selected)
        self.assertEqual(await self.page.evaluate("window.imageScrollBottomCalls"), 0)

        await self.page.evaluate("window.getSelection().removeAllRanges(); openAdmin()")
        await expect(self.page.locator("#admin-view")).to_be_visible()
        await self.page.evaluate(
            """() => {
                state.follow = true;
                document.querySelectorAll('.message-attachment img')[document.querySelectorAll('.message-attachment img').length - 1].dispatchEvent(new Event('load'));
            }"""
        )
        self.assertEqual(await self.page.evaluate("window.imageScrollBottomCalls"), 0)
        self.assertFalse(self.errors, self.errors)

    async def test_stale_overlapping_room_refresh_cannot_replace_newer_state(self):
        other = self.service.store.create_room(
            self.admin_id,
            "Overlap staging room",
            "/home/server/overlap",
            "astra",
            "medium",
        )
        await self.login(self.admin)
        await self.page.evaluate("id => selectRoom(id)", other["id"])
        await self.page.wait_for_function("() => roomState().loaded && !roomState().loading")

        stale_captured = asyncio.Event()
        release_stale = asyncio.Event()
        request_count = 0

        async def overlap_refresh(route):
            nonlocal request_count
            request_count += 1
            if request_count != 1:
                await route.continue_()
                return
            response = await route.fetch()
            body = await response.body()
            stale_captured.set()
            await release_stale.wait()
            await route.fulfill(
                status=response.status,
                headers=response.headers,
                body=body,
            )

        await self.page.route(
            f"**/api/rooms/{self.room['id']}/messages*",
            overlap_refresh,
        )
        try:
            await self.page.evaluate("id => { void selectRoom(id); }", self.room["id"])
            await asyncio.wait_for(stale_captured.wait(), timeout=2)
            self.service.store.update_room(
                self.admin_id,
                self.room["id"],
                title="Fresh room title",
            )
            await self.page.evaluate("id => selectRoom(id)", other["id"])
            await self.page.evaluate("id => selectRoom(id)", self.room["id"])
            await expect(self.page.locator("#room-title")).to_have_text("Fresh room title")
            release_stale.set()
            await asyncio.sleep(0.2)
            self.assertEqual(
                await self.page.evaluate("selectedRoom().title"),
                "Fresh room title",
            )
            self.assertFalse(self.errors, self.errors)
        finally:
            release_stale.set()

    async def test_deleted_selected_room_does_not_close_administration(self):
        await self.login(self.admin)
        await self.page.locator("#admin-open").click()
        await expect(self.page.locator("#admin-view")).to_be_visible()

        self.service.store.delete_room(self.admin_id, self.room["id"])
        await self.page.evaluate("() => refreshRooms()")

        await expect(self.page.locator("#admin-view")).to_be_visible()
        await expect(self.page.locator("#chat-view")).to_be_hidden()
        await expect(self.page.locator("#room-title")).to_have_text("Administration")
        self.assertEqual(await self.page.evaluate("state.view"), "admin")
        self.assertIsNone(await self.page.evaluate("state.selected"))
        self.assertFalse(self.errors, self.errors)


if __name__ == "__main__":
    unittest.main()
