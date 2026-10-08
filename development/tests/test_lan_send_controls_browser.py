"""CODEX: Regressions for removing Live, stable model ordering, and local Send feedback."""

import asyncio
import re
import unittest

from playwright.async_api import expect

import test_lan_browser as browser_fixture


class LanSendControlsBrowserTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = browser_fixture.LanBrowserTests.asyncSetUp
    asyncTearDown = browser_fixture.LanBrowserTests.asyncTearDown
    start_worker = browser_fixture.LanBrowserTests.start_worker
    login = browser_fixture.LanBrowserTests.login

    async def test_retired_live_preference_migrates_before_app_boot_and_stays_cosmic(self):
        await self.context.add_init_script("""if (location.protocol === 'http:' && !localStorage.getItem('codex-lan-theme')) {
            localStorage.setItem('codex-lan-theme', 'cosmic-live');
        }""")
        for asset in ("app.js", "controls.js", "companions.js"):
            await self.page.route(f"**/{asset}", lambda route: route.fulfill(body="", content_type="text/javascript"))
        await self.page.goto(self.base)
        await expect(self.page.locator("html")).to_have_attribute("data-theme", "cosmic")
        self.assertEqual(await self.page.evaluate("localStorage.getItem('codex-lan-theme')"), "cosmic")
        for asset in ("app.js", "controls.js", "companions.js"):
            await self.page.unroute(f"**/{asset}")
        await self.page.goto("about:blank")
        await self.login(self.admin)
        await expect(self.page.locator("#theme-select")).to_have_value("cosmic")
        await expect(self.page.locator('#theme-select option[value="cosmic-live"]')).to_have_count(0)
        await expect(self.page.locator('.cosmic-live-sky, #cosmic-live-signal')).to_have_count(0)
        await self.page.reload()
        await expect(self.page.locator("#theme-select")).to_have_value("cosmic")
        self.assertFalse(self.errors, self.errors)

    async def test_models_are_grouped_and_sorted_without_mutating_provider_default(self):
        await self.login(self.admin)
        models = ["gpt-5.6-luna", "gpt-6-luna", "gpt-5.5", "gpt-6-sol", "gpt-5.6-sol", "gpt-6.1-sol", "gpt-6-astra", "gpt-5.6-terra", "gpt-5.10", "custom-model"]
        expected = ["gpt-6-astra", "gpt-6.1-sol", "gpt-6-sol", "gpt-6-luna", "gpt-5.10", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna", "gpt-5.5", "custom-model"]
        await self.page.evaluate("""models => {
            state.models = models.map((model, index) => ({id:model, model, displayName:model,
                isDefault:index === 0, availabilityVerified:true, supportedReasoningEfforts:[{reasoningEffort:'medium'}]}));
            selectedRoom().model = ''; renderCurrent();
        }""", models)
        await expect(self.page.locator("#model-select")).to_have_value(models[0])
        self.assertEqual(await self.page.evaluate("state.models.map(model => model.id)"), models)
        self.assertEqual(await self.page.locator("#model-select option").evaluate_all("nodes => nodes.map(node => node.value)"), expected)
        await self.page.locator(".model-picker-trigger").click()
        self.assertEqual(await self.page.locator(".model-picker-group").all_text_contents(), ["GPT-6", "GPT-5", "Other models"])
        self.assertEqual(await self.page.locator(".model-picker-option").evaluate_all("nodes => nodes.map(node => node.dataset.modelValue)"), expected)
        await self.page.get_by_role("searchbox", name="Find a model").fill("6.1")
        await expect(self.page.locator(".model-picker-option")).to_have_count(1)
        await expect(self.page.locator(".model-picker-group")).to_have_text("GPT-6")
        await self.page.screenshot(path="/tmp/codex-send-model-search.png")
        await self.page.keyboard.press("Escape")
        await self.page.evaluate("""() => { selectedRoom().model = 'gpt-6-sol'; renderCurrent(); }""")
        await expect(self.page.locator("#model-select")).to_have_value("gpt-6-sol")
        await self.page.locator(".model-picker-trigger").hover()
        await self.page.mouse.wheel(0, 120)
        await expect(self.page.locator("#model-select")).to_have_value("gpt-6-luna")
        self.assertFalse(self.errors, self.errors)

    async def test_send_spark_is_local_accessible_and_respects_motion_and_small_screens(self):
        await self.login(self.admin)
        await self.page.locator("#message-input").fill("A local button preview")
        button = self.page.locator("#send-button")
        for theme in ("cosmic", "light", "dark", "midnight", "chatgpt", "torte", "aurora", "ember", "velvet"):
            await self.page.locator("#theme-select").select_option(theme)
            await self.page.mouse.move(0, 0)
            await self.page.locator("#message-input").focus()
            self.assertEqual(await button.evaluate("node => getComputedStyle(node, '::after').animationName"), "none")
            await button.hover()
            await expect(button).to_have_attribute("aria-label", "Send message")
            self.assertEqual(await button.evaluate("node => getComputedStyle(node, '::after').animationName"), "send-ai-scan")
            self.assertEqual(await button.locator(".send-symbol").get_attribute("aria-hidden"), "true")
        await self.page.locator("#theme-select").select_option("cosmic")
        await button.hover()
        await self.page.wait_for_timeout(300)
        await self.page.screenshot(path="/tmp/codex-send-spark-desktop.png")
        await self.page.emulate_media(reduced_motion="reduce")
        self.assertEqual(await button.evaluate("node => getComputedStyle(node, '::after').animationName"), "none")
        await self.page.mouse.move(0, 0)
        await button.focus()
        self.assertEqual(await button.locator(".send-spark").evaluate("node => getComputedStyle(node).opacity"), "1")
        for width in (360, 720, 1024):
            await self.page.set_viewport_size({"width": width, "height": 900})
            layout = await button.evaluate("""node => {const r=node.getBoundingClientRect();return {left:r.left,right:r.right,width:r.width,height:r.height,overflow:document.documentElement.scrollWidth>innerWidth+1}}""")
            self.assertFalse(layout["overflow"], layout)
            self.assertGreaterEqual(layout["left"], 0, layout)
            self.assertLessEqual(layout["right"], width, layout)
            self.assertGreaterEqual(layout["height"], 36, layout)
        self.assertFalse(self.errors, self.errors)

    async def test_sending_feedback_tracks_request_and_failure_keeps_draft(self):
        await self.login(self.admin)
        began, release = asyncio.Event(), asyncio.Event()
        requests = []

        async def fail_after_wait(route, request):
            requests.append(request.post_data_json)
            began.set()
            await release.wait()
            await route.fulfill(status=503, content_type="application/json", body='{"error":"Temporarily unavailable"}')

        await self.page.route("**/api/rooms/*/runs", fail_after_wait)
        draft = "Keep this if delivery fails"
        await self.page.locator("#message-input").fill(draft)
        button = self.page.locator("#send-button")
        try:
            await button.click()
            await asyncio.wait_for(began.wait(), timeout=5)
            await expect(button).to_have_attribute("data-sending", "true")
            await expect(button).to_have_attribute("aria-busy", "true")
            await expect(button).to_be_disabled()
            await expect(button.locator(".send-spark")).to_have_count(1)
            await self.page.evaluate("renderCurrent(); renderCurrent();")
            await expect(button.locator(".send-spark")).to_have_count(1)
            release.set()
            await expect(button).to_have_attribute("data-sending", "false")
            await expect(button).to_have_attribute("aria-busy", "false")
            await expect(self.page.locator("#message-input")).to_have_value(draft)
            self.assertEqual(len(requests), 1)
        finally:
            release.set()
        self.assertFalse(self.errors, self.errors)

    async def test_fast_success_keeps_confirmation_and_message_arrival_visible(self):
        await self.login(self.admin)
        await self.page.locator("#theme-select").select_option("cosmic")
        button = self.page.locator("#send-button")
        await self.page.locator("#message-input").fill("Show a visible send confirmation")
        await self.page.locator("#message-input").press("Enter")
        await self.page.locator("#message-input").press("Enter")
        await expect(button).to_have_class(re.compile(r"\bsend-confirmed\b"))
        await expect(self.page.locator("#composer")).to_have_class(re.compile(r"\bsend-confirmed\b"))
        self.assertEqual(await button.evaluate("node => getComputedStyle(node).animationName"), "send-ai-confirm")
        self.assertEqual(await self.page.locator(".send-neural-scan").evaluate("node => getComputedStyle(node).animationName"), "send-neural-scan")
        self.assertEqual(await self.page.locator(".composer-send-feedback").evaluate("node => getComputedStyle(node).animationDuration"), "3.05s")
        self.assertEqual(await self.page.locator(".composer-send-feedback > i").first.evaluate("node => getComputedStyle(node).animationName"), "send-neural-particle")
        message = self.page.locator(".message.user").filter(has_text="Show a visible send confirmation")
        await expect(message).to_have_count(1)
        await expect(message).to_have_class(re.compile(r"\bmessage-send-arrive\b"))
        self.assertEqual(await message.evaluate("node => getComputedStyle(node).animationName"), "message-send-arrive")
        self.assertEqual(await message.evaluate("node => getComputedStyle(node, '::before').animationName"), "message-ai-aura")
        await self.page.wait_for_timeout(180)
        await self.page.screenshot(path="/tmp/codex-send-neural-feedback.png")
        await self.page.wait_for_timeout(1500)
        await expect(self.page.locator("#composer")).to_have_class(re.compile(r"\bsend-confirmed\b"))
        self.assertGreater(float(await self.page.locator(".composer-send-feedback").evaluate("node => getComputedStyle(node).opacity")), 0.1)
        await expect(button).not_to_have_class(re.compile(r"\bsend-confirmed\b"), timeout=4500)
        await expect(self.page.locator("#composer")).not_to_have_class(re.compile(r"\bsend-confirmed\b"), timeout=4500)
        self.assertFalse(self.errors, self.errors)


if __name__ == "__main__":
    unittest.main()
