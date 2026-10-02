"""CODEX: Verify bundled Vazir actually renders Persian without replacing Latin or code fonts."""

from __future__ import annotations

import hashlib
import unittest
from pathlib import Path

from playwright.async_api import expect

import test_lan_browser as browser_fixture


class VazirFontBrowserTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = browser_fixture.LanBrowserTests.asyncSetUp
    asyncTearDown = browser_fixture.LanBrowserTests.asyncTearDown
    login = browser_fixture.LanBrowserTests.login
    start_worker = browser_fixture.LanBrowserTests.start_worker

    async def test_original_font_is_served_locally_with_license(self):
        response = await self.http.get("/Vazir-Variable-v30.1.0.woff2")
        self.assertEqual(response.status, 200)
        data = await response.read()
        self.assertEqual(len(data), 41696)
        self.assertEqual(data[:4], b"wOF2")
        self.assertEqual(hashlib.sha256(data).hexdigest(),
                         "3d7c6820aa45e4f1c569561a7dabc6472153e14ea883b5b6ef41aa764c66def0")
        self.assertEqual(response.content_type, "font/woff2")
        license_path = Path(__file__).resolve().parents[1] / "codex_lan/web/Vazir-OFL.txt"
        self.assertIn("SIL OPEN FONT LICENSE Version 1.1", license_path.read_text())

    async def test_persian_uses_real_vazir_and_latin_code_stay_unchanged(self):
        await self.login(self.admin)
        await expect(self.page.locator("#room-title")).to_have_text("Working together")
        await self.page.evaluate("document.fonts.ready")
        self.assertTrue(await self.page.evaluate("document.fonts.check('16px Vazir', 'فارسی')"))
        face = await self.page.evaluate("""() => [...document.fonts].filter(font => font.family === 'Vazir').map(font => ({
            status: font.status, display: font.display, weight: font.weight, range: font.unicodeRange
        }))""")
        self.assertEqual(len(face), 1)
        self.assertEqual((face[0]["status"], face[0]["display"], face[0]["weight"]),
                         ("loaded", "swap", "100 900"))
        session = await self.context.new_cdp_session(self.page)
        await session.send("DOM.enable")
        await session.send("CSS.enable")
        root = await session.send("DOM.getDocument")

        async def fonts(selector):
            selected = await session.send("DOM.querySelector", {"nodeId": root["root"]["nodeId"], "selector": selector})
            result = await session.send("CSS.getPlatformFontsForNode", {"nodeId": selected["nodeId"]})
            return result["fonts"]

        persian = await fonts(".message.assistant .message-content p[dir='rtl']")
        self.assertTrue(any(item["familyName"].startswith("Vazir") and item["isCustomFont"] and item["glyphCount"] > 0 for item in persian), persian)
        latin = await fonts(".message.assistant .message-content p[dir='ltr']")
        self.assertFalse(any(item["familyName"].startswith("Vazir") for item in latin), latin)
        code = await fonts(".message.assistant .message-content pre code")
        self.assertFalse(any(item["familyName"].startswith("Vazir") for item in code), code)
        await self.page.locator("#message-input").fill("متن فارسی با فونت وزیر؛ Latin text stays unchanged.")
        for theme in ("light", "chatgpt", "cosmic"):
            await self.page.locator("#theme-select").select_option(theme)
            await self.page.locator("#timeline-scroll").evaluate("node => {node.scrollTop = 0;}")
            await self.page.screenshot(path=f"/tmp/codex-vazir-{theme}.png", full_page=True)
        self.assertFalse(self.errors, self.errors)


if __name__ == "__main__":
    unittest.main()
