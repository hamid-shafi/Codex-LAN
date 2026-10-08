"""CODEX: Event-driven motion stays local, respects accessibility and never replays settled history."""

import asyncio
import unittest

from playwright.async_api import expect

import test_lan_browser as browser_fixture
from test_lan_server import PNG


class LanInteractionEffectsBrowserTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = browser_fixture.LanBrowserTests.asyncSetUp
    asyncTearDown = browser_fixture.LanBrowserTests.asyncTearDown
    start_worker = browser_fixture.LanBrowserTests.start_worker
    login = browser_fixture.LanBrowserTests.login
    wait_worker_turn = browser_fixture.LanBrowserTests.wait_worker_turn

    async def test_control_effects_stop_for_reduced_motion_and_preserve_small_layouts(self):
        await self.login(self.admin)
        await self.page.locator("#theme-select").select_option("cosmic")
        await self.page.get_by_role("button", name="More thinking", exact=True).click()
        await self.page.wait_for_function("() => document.getAnimations().some(a => a.id === 'codex-thinking-wave')")
        await self.page.wait_for_timeout(150)
        await self.page.screenshot(path="/tmp/codex-interaction-thinking.png")
        await self.page.emulate_media(reduced_motion="reduce")
        await self.page.wait_for_function("() => !document.getAnimations().some(a => a.id.startsWith('codex-'))")
        await self.page.get_by_role("button", name="Less thinking", exact=True).click()
        self.assertFalse(await self.page.evaluate("document.getAnimations().some(a => a.id.startsWith('codex-'))"))
        await self.page.emulate_media(reduced_motion="no-preference")
        for theme in ("light", "dark", "midnight", "chatgpt", "torte", "cosmic", "aurora", "ember", "velvet"):
            await self.page.locator("#theme-select").select_option(theme)
            await self.page.get_by_role("button", name="More thinking", exact=True).click()
            await self.page.get_by_role("button", name="Less thinking", exact=True).click()
        for width in (360, 720, 1440):
            await self.page.set_viewport_size({"width": width, "height": 900})
            await self.page.get_by_role("button", name="More thinking", exact=True).click()
            self.assertFalse(await self.page.evaluate("document.documentElement.scrollWidth > innerWidth + 1"))
            await self.page.get_by_role("button", name="Less thinking", exact=True).click()
        await self.page.emulate_media(forced_colors="active")
        await self.page.wait_for_function("() => !document.getAnimations().some(a => a.id.startsWith('codex-'))")
        self.assertFalse(self.errors, self.errors)

    async def test_live_reply_reveals_once_and_only_success_gets_a_completion_spark(self):
        await self.login(self.admin)
        await self.page.locator("#theme-select").select_option("cosmic")
        self.assertFalse(await self.page.evaluate("document.getAnimations().some(a => a.id.startsWith('codex-reply'))"))
        await self.page.locator("#message-input").fill("Preview a live answer")
        await self.page.locator("#send-button").click()
        await self.wait_worker_turn()
        live = self.page.locator('.message.assistant[data-key^="run:"]')
        await expect(live).to_have_count(1)
        await self.worker.delta("A small idea, beautifully delivered.")
        await expect(live.locator(".message-content")).to_have_text("A small idea, beautifully delivered.")
        await self.page.wait_for_function("() => document.getAnimations().some(a => a.id === 'codex-reply-enter')")
        await self.page.wait_for_function("() => !document.getAnimations().some(a => a.id === 'codex-reply-enter')")
        await self.worker.delta(" A second sentence.")
        await expect(live.locator(".message-content")).to_have_text("A small idea, beautifully delivered. A second sentence.")
        self.assertFalse(await self.page.evaluate("document.getAnimations().some(a => a.id === 'codex-reply-enter')"))
        await self.worker.complete()
        spark = self.page.locator(".reply-ready-effect")
        await expect(spark).to_have_count(1)
        await expect(spark).to_have_attribute("aria-hidden", "true")
        await self.page.wait_for_timeout(250)
        await self.page.screenshot(path="/tmp/codex-interaction-reply.png")
        await expect(spark).to_have_count(0)
        await self.page.wait_for_function("() => !document.getAnimations().some(a => a.id.startsWith('codex-reply'))", timeout=3000)
        await self.page.evaluate("renderCurrent(); renderCurrent();")
        self.assertFalse(await self.page.evaluate("document.getAnimations().some(a => a.id.startsWith('codex-reply'))"))
        self.worker.turn = None
        await self.page.locator("#message-input").fill("Preview an interrupted answer")
        await self.page.locator("#send-button").click()
        await self.wait_worker_turn()
        await expect(live).to_have_count(1)
        await self.worker.delta("This answer will be interrupted.")
        await expect(live.locator(".message-content")).to_have_text("This answer will be interrupted.")
        await self.worker.complete("failed")
        await expect(self.page.locator(".run-failure:visible")).to_have_count(1)
        self.assertFalse(await self.page.evaluate("document.getAnimations().some(a => a.id === 'codex-reply-ready')"))
        await expect(spark).to_have_count(0)
        self.assertFalse(self.errors, self.errors)

    async def test_room_entry_does_not_replay_on_refresh_or_change_selection_and_scroll(self):
        await self.login(self.admin)
        await self.page.wait_for_timeout(800)
        await self.page.evaluate("window.CodexControls.enterRoom()")
        await self.page.wait_for_function("() => document.getAnimations().some(a => a.id === 'codex-room-enter')")
        await self.page.wait_for_timeout(800)
        await self.page.evaluate("renderCurrent(); renderCurrent();")
        self.assertFalse(await self.page.evaluate("document.getAnimations().some(a => a.id === 'codex-room-enter')"))
        before = await self.page.locator("#timeline-scroll").evaluate("node => node.scrollTop")
        await self.page.evaluate("""() => {
            const p = document.querySelector('.message.assistant .message-content p');
            const range = document.createRange(); range.selectNodeContents(p);
            const selection = getSelection(); selection.removeAllRanges(); selection.addRange(range);
            window.selectedBeforeEffect = selection.toString(); window.CodexControls.enterRoom();
        }""")
        self.assertTrue(await self.page.evaluate("getSelection().toString() === window.selectedBeforeEffect"))
        self.assertFalse(await self.page.evaluate("document.getAnimations().some(a => a.id === 'codex-room-enter')"))
        self.assertEqual(before, await self.page.locator("#timeline-scroll").evaluate("node => node.scrollTop"))
        await self.page.evaluate("getSelection().removeAllRanges()")
        room = self.service.store.create_room(self.admin_id, "A quiet new room", "/home/server/project", "astra", "medium")
        await self.page.evaluate("async room => { mergeRoom(room); await selectRoom(room.id); }", room)
        await self.page.wait_for_function("() => document.querySelector('#empty-state .empty-symbol').getAnimations().some(a => a.id === 'codex-room-enter')")
        await self.page.wait_for_timeout(300)
        await self.page.screenshot(path="/tmp/codex-interaction-empty.png")
        await asyncio.sleep(.7)
        self.assertFalse(self.errors, self.errors)

    async def test_small_confirmations_are_finite_accessible_and_do_not_replay(self):
        await self.context.grant_permissions(["clipboard-read", "clipboard-write"])
        await self.login(self.admin)
        await self.page.locator("#theme-select").select_option("cosmic")

        copy = self.page.locator(".message.assistant .message-footer .message-action").first
        await copy.click()
        await expect(copy).to_have_text("Copied")
        await self.page.wait_for_function("() => document.getAnimations().some(a => a.id === 'codex-copy-confirm')")
        await expect(copy.locator(".copy-success-effect")).to_have_count(1)
        await copy.click()
        await expect(copy.locator(".copy-success-effect")).to_have_count(1)
        await expect(self.page.locator("#interaction-feedback-status")).to_have_text("Copied to clipboard")
        self.assertIn("The plan starts here", await self.page.evaluate("navigator.clipboard.readText()"))
        await expect(copy).to_have_text("Copy", timeout=2500)
        await expect(copy.locator(".copy-success-effect")).to_have_count(0)

        usage = self.page.locator(".message.assistant .message-usage").first
        if await usage.count():
            await usage.locator("summary").click()
            await self.page.wait_for_function("() => document.getAnimations().some(a => a.animationName === 'codex-detail-reveal')")
            await usage.locator("summary").click()

        await self.page.locator("#attachment-input").set_input_files({
            "name": "tiny-detail.png", "mimeType": "image/png", "buffer": PNG,
        })
        card = self.page.locator(".attachment-card")
        await expect(card.locator("small")).to_have_text("Ready")
        await self.page.wait_for_function("() => document.getAnimations().some(a => a.id === 'codex-attachment-ready')")
        await expect(card.locator(".attachment-ready-effect")).to_have_count(1)
        await expect(self.page.locator("#interaction-feedback-status")).to_have_text("tiny-detail.png is ready")
        await self.page.screenshot(path="/tmp/codex-interaction-small-details.png")
        await self.page.wait_for_function("() => !document.getAnimations().some(a => a.id.startsWith('codex-attachment'))", timeout=2500)
        await self.page.evaluate("renderAttachments()")
        self.assertFalse(await self.page.evaluate("document.getAnimations().some(a => a.id.startsWith('codex-attachment'))"))

        own_dot = self.page.locator(".room-row.own .unread-dot").first
        await own_dot.evaluate("node => { node.hidden = false; window.CodexControls.unreadFeedback(node); }")
        await self.page.wait_for_function("() => document.getAnimations().some(a => a.id === 'codex-unread-arrive')")
        await self.page.wait_for_function("() => !document.getAnimations().some(a => a.id === 'codex-unread-arrive')", timeout=2500)
        other_dot = self.page.locator(".room-row.other .unread-dot").first
        await other_dot.evaluate("node => { node.hidden = false; window.CodexControls.unreadFeedback(node); }")
        self.assertFalse(await self.page.evaluate("document.getAnimations().some(a => a.id === 'codex-unread-arrive')"))

        await self.page.emulate_media(reduced_motion="reduce")
        await copy.click()
        await expect(copy).to_have_text("Copied")
        await expect(self.page.locator("#interaction-feedback-status")).to_have_text("Copied to clipboard")
        self.assertFalse(await self.page.evaluate("document.getAnimations().some(a => a.id.startsWith('codex-copy'))"))
        await expect(copy.locator(".copy-success-effect")).to_have_count(0)
        self.assertFalse(self.errors, self.errors)


if __name__ == "__main__":
    unittest.main()
