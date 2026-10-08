"""CODEX: File cards and the compact composer remain usable at narrow widths."""

import json
import unittest

from playwright.async_api import expect
import test_lan_browser as fixture


class LanFileAttachmentsBrowserTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.LanBrowserTests.asyncSetUp
    asyncTearDown = fixture.LanBrowserTests.asyncTearDown
    start_worker = fixture.LanBrowserTests.start_worker
    login = fixture.LanBrowserTests.login
    wait_worker_turn = fixture.LanBrowserTests.wait_worker_turn

    async def open_room(self):
        await self.login(self.admin)
        await self.page.evaluate("id => selectRoom(id)", self.room["id"])

    async def test_file_picker_sends_download_card_and_a_path_without_copying_file_content(self):
        await self.open_room()
        await expect(self.page.locator("#attach-button")).to_have_attribute("aria-label", "Attach files")
        self.assertIsNone(await self.page.locator("#attachment-input").get_attribute("accept"))
        await self.page.locator("#attachment-input").set_input_files({"name": "report.txt", "mimeType": "text/plain", "buffer": b"CONTENTS-STAY-IN-THE-FILE"})
        await expect(self.page.locator(".attachment-card small")).to_contain_text("Ready")
        await expect(self.page.locator(".attachment-card img")).to_have_count(0)
        await expect(self.page.locator(".attachment-file-icon")).to_have_text("TXT")
        await self.page.locator("#message-input").fill("Read the attached report")
        await self.page.locator("#send-button").click()
        await self.wait_worker_turn()
        await expect(self.page.locator(".message-file")).to_have_count(1)
        await expect(self.page.locator(".message-file")).to_have_attribute("download", "report.txt")
        await expect(self.page.locator(".message-file img")).to_have_count(0)
        turn = next(call for call in self.worker.calls if call["method"] == "turn/start")
        self.assertIn("report.txt", json.dumps(turn["params"]["input"]))
        self.assertNotIn("CONTENTS-STAY-IN-THE-FILE", json.dumps(turn["params"]["input"]))
        await self.page.screenshot(path="/tmp/codex-file-composer.png")
        self.assertFalse(self.errors, self.errors)

    async def test_speed_text_fits_and_send_uses_plane_in_main_and_embedded_views(self):
        await self.open_room()
        for embedded in (False, True):
            await self.page.evaluate("value => document.documentElement.dataset.embedded = String(value)", embedded)
            for width in (390, 480, 760, 1280):
                await self.page.set_viewport_size({"width": width, "height": 900})
                result = await self.page.locator("#speed-select").evaluate("""select => {
                    const css = getComputedStyle(select), canvas = document.createElement('canvas');
                    const ctx = canvas.getContext('2d'); ctx.font = `${css.fontWeight} ${css.fontSize} ${css.fontFamily}`;
                    const available = select.clientWidth - parseFloat(css.paddingLeft) - parseFloat(css.paddingRight);
                    return { available, needed: Math.max(ctx.measureText('Standard').width, ctx.measureText('Fast ⚡').width), width: select.clientWidth };
                }""")
                self.assertGreaterEqual(result["available"], result["needed"], (embedded, width, result))
                self.assertFalse(await self.page.evaluate("document.documentElement.scrollWidth > innerWidth"), (embedded, width))
                await expect(self.page.locator("#send-button .send-plane")).to_have_count(1)
        self.assertFalse(self.errors, self.errors)


if __name__ == "__main__":
    unittest.main()
