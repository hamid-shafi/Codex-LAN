"""CODEX: Browser checks for independently scoped, keyboard-accessible composer controls."""

import asyncio
import json
from pathlib import Path
import unittest

from playwright.async_api import expect

import test_lan_browser as browser_fixture


WEB = Path(__file__).resolve().parents[2] / "apps/workspace/codex_lan" / "web"


class LanControlsBrowserTests(unittest.IsolatedAsyncioTestCase):
    asyncTearDown = browser_fixture.LanBrowserTests.asyncTearDown
    start_worker = browser_fixture.LanBrowserTests.start_worker

    async def asyncSetUp(self):
        await browser_fixture.LanBrowserTests.asyncSetUp(self)
        await self.page.route("**/controls.js", lambda route: route.fulfill(path=WEB / "controls.js", content_type="text/javascript"))
        await self.page.route("**/controls.css", lambda route: route.fulfill(path=WEB / "controls.css", content_type="text/css"))
        await browser_fixture.LanBrowserTests.login(self, self.admin)
        await expect(self.page.locator("#model-select")).to_have_value("astra")
        if not await self.page.evaluate("Boolean(window.CodexControls)"):
            await self.page.add_style_tag(url=self.base + "controls.css")
            await self.page.add_script_tag(url=self.base + "controls.js")

    async def test_model_changes_clamp_effort_and_controls_preserve_shortcuts(self):
        await self.page.locator("#effort-select").select_option("ultra")
        await expect(self.page.locator(".thinking-value")).to_have_text("Ultra")
        self.assertEqual(
            await self.page.locator(".thinking-mark.is-filled").count(),
            await self.page.locator(".thinking-mark").count(),
        )
        await self.page.locator(".model-picker-trigger").click()
        await self.page.get_by_role("option", name="sol", exact=True).click()
        await expect(self.page.locator("#model-select")).to_have_value("sol")
        await expect(self.page.locator("#effort-select")).to_have_value("medium")
        slider = self.page.get_by_role("slider", name="Reasoning effort", exact=True)
        await expect(slider).to_have_attribute("aria-valuetext", "Medium")
        await self.page.get_by_role("button", name="More thinking", exact=True).click()
        await expect(self.page.locator("#effort-select")).to_have_value("high")
        await expect(self.page.get_by_role("button", name="More thinking", exact=True)).to_be_disabled()
        await slider.focus()
        await slider.press("Home")
        await expect(self.page.locator("#effort-select")).to_have_value("low")
        await expect(self.page.get_by_role("button", name="Less thinking", exact=True)).to_be_disabled()
        await self.page.locator("#message-input").press("Alt+ArrowUp")
        await expect(slider).to_have_attribute("aria-valuetext", "Medium")
        await self.page.locator("#message-input").press("Control+Alt+ArrowUp")
        await expect(self.page.locator(".model-picker-value")).to_have_text("astra")
        self.assertFalse(self.errors, self.errors)

    async def test_rapid_model_changes_are_saved_in_order_without_stale_rollback(self):
        requests = []
        active = 0
        max_active = 0
        first_started = asyncio.Event()
        release_first = asyncio.Event()
        second_finished = asyncio.Event()

        async def settings(route, request):
            nonlocal active, max_active
            payload = request.post_data_json
            requests.append(payload)
            active += 1
            max_active = max(max_active, active)
            try:
                if len(requests) == 1:
                    first_started.set()
                    await release_first.wait()
                room = {**self.room, **payload}
                await route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps({"room": room}),
                )
            finally:
                active -= 1
                if len(requests) == 2:
                    second_finished.set()

        pattern = f"**/api/rooms/{self.room['id']}"
        await self.page.route(pattern, settings)
        try:
            await self.page.evaluate(
                """() => {
                    const select = document.getElementById('model-select');
                    select.value = 'sol';
                    select.dispatchEvent(new Event('change', {bubbles: true}));
                    select.value = 'astra';
                    select.dispatchEvent(new Event('change', {bubbles: true}));
                }"""
            )
            await asyncio.wait_for(first_started.wait(), timeout=5)
            await self.page.wait_for_timeout(100)
            self.assertEqual([request["model"] for request in requests], ["sol"])
            self.assertEqual(max_active, 1)
            release_first.set()
            await asyncio.wait_for(second_finished.wait(), timeout=5)
            await self.page.wait_for_function(
                "() => selectedRoom().model === 'astra' && !roomState().modelSettingsSave"
            )
            self.assertEqual(
                [request["model"] for request in requests], ["sol", "astra"]
            )
            await expect(self.page.locator("#model-select")).to_have_value("astra")
            self.assertEqual(max_active, 1)
            self.assertFalse(self.errors, self.errors)
        finally:
            release_first.set()
            await self.page.unroute(pattern, settings)

    async def test_wheel_changes_model_and_thinking_without_scrolling_the_chat(self):
        timeline = self.page.locator("#timeline-scroll")
        await timeline.evaluate("node => node.scrollTop = Math.max(0, node.scrollHeight - node.clientHeight - 80)")
        before = await timeline.evaluate("node => node.scrollTop")
        await self.page.locator(".model-picker-trigger").hover()
        await self.page.mouse.wheel(0, 120)
        await expect(self.page.locator("#model-select")).to_have_value("sol")
        await expect(self.page.locator("#effort-select")).to_have_value("medium")
        await self.page.wait_for_timeout(110)
        await self.page.locator(".smart-effort").hover()
        await self.page.mouse.wheel(0, 120)
        await expect(self.page.locator("#effort-select")).to_have_value("low")
        await self.page.wait_for_timeout(110)
        await self.page.mouse.wheel(0, -120)
        await expect(self.page.locator("#effort-select")).to_have_value("medium")
        self.assertEqual(await timeline.evaluate("node => node.scrollTop"), before)
        self.assertFalse(self.errors, self.errors)

    async def test_running_response_keeps_its_exact_thinking_snapshot(self):
        await self.page.evaluate("""() => {
            const cache = roomState();
            cache.runs.set('settings-snapshot', {
                id: 'settings-snapshot', status: 'running', created_at: Date.now() / 1000,
                model: 'astra', effort: 'max', speed: 'standard'
            });
            renderCurrent();
        }""")
        self.assertEqual(await self.page.evaluate("selectedRoom().effort"), "medium")
        await expect(self.page.locator("#effort-select")).to_have_value("max")
        await expect(self.page.locator('#effort-select option[value="max"]')).to_have_text("Max")
        await expect(self.page.locator(".thinking-value")).to_have_text("Max")
        await expect(self.page.locator("#effort-select")).to_be_disabled()
        await self.page.evaluate("""() => {
            roomState().runs.delete('settings-snapshot');
            renderCurrent();
        }""")
        await expect(self.page.locator("#effort-select")).to_have_value("medium")
        self.assertFalse(self.errors, self.errors)

    async def test_max_and_ultra_keep_distinct_labels_and_protocol_values(self):
        await self.page.evaluate("""() => {
            const model = matchingModel('sol');
            model.supportedReasoningEfforts = [
                {reasoningEffort: 'low'},
                {reasoningEffort: 'medium'},
                {reasoningEffort: 'high'},
                {reasoningEffort: 'xhigh'},
                {reasoningEffort: 'max', description: 'Maximum reasoning depth'},
                {reasoningEffort: 'ultra', description: 'Automatic task delegation'},
            ];
            selectedRoom().model = 'sol';
            selectedRoom().effort = 'max';
            renderCurrent();
        }""")
        await expect(self.page.locator('#effort-select option[value="max"]')).to_have_text("Max")
        await expect(self.page.locator('#effort-select option[value="ultra"]')).to_have_text("Ultra")
        await expect(self.page.locator('#effort-select option')).to_have_count(6)
        await expect(self.page.locator("#effort-select")).to_have_value("max")
        await expect(self.page.locator(".thinking-value")).to_have_text("Max")
        self.assertEqual(await self.page.locator(".thinking-mark").count(), 6)
        self.assertEqual(await self.page.locator(".thinking-mark.is-filled").count(), 5)
        self.assertEqual(
            await self.page.locator("#effort-select").evaluate(
                "select => [...select.options].map(option => [option.value, option.textContent])"
            ),
            [["low", "Low"], ["medium", "Medium"], ["high", "High"],
             ["xhigh", "Extra high"], ["max", "Max"], ["ultra", "Ultra"]],
        )
        await self.page.evaluate("""() => {
            selectedRoom().effort = 'ultra';
            renderCurrent();
        }""")
        await expect(self.page.locator("#effort-select")).to_have_value("ultra")
        await expect(self.page.locator(".thinking-value")).to_have_text("Ultra")
        self.assertEqual(await self.page.locator(".thinking-mark.is-filled").count(), 6)
        self.assertFalse(self.errors, self.errors)

    async def test_keyboard_picker_and_disabled_state(self):
        trigger = self.page.locator(".model-picker-trigger")
        last_model = await self.page.locator("#model-select option").last.get_attribute("value")
        await trigger.focus()
        await trigger.press("ArrowDown")
        await expect(self.page.locator(".model-picker-menu")).to_be_visible()
        await self.page.keyboard.press("End")
        await self.page.keyboard.press("Enter")
        await expect(self.page.locator("#model-select")).to_have_value(last_model)
        await expect(trigger).to_be_focused()
        await trigger.click()
        await self.page.keyboard.press("Escape")
        await expect(trigger).to_be_focused()
        await expect(self.page.locator(".model-picker-menu")).to_have_count(0)
        await self.page.evaluate("document.getElementById('model-select').disabled = true")
        await expect(trigger).to_be_disabled()
        await self.page.evaluate("document.getElementById('model-select').disabled = false")
        await expect(trigger).to_be_enabled()
        self.assertFalse(self.errors, self.errors)

    async def test_gpt6_manual_choices_explain_unconfirmed_access_without_changing_default(self):
        await expect(self.page.locator("#model-select")).to_have_value("astra")
        await expect(self.page.locator("#model-availability-notice")).to_be_hidden()
        for model in ("gpt-6-astra", "gpt-6.1-sol", "gpt-6-sol", "gpt-6-luna"):
            await self.page.locator(".model-picker-trigger").click()
            option = self.page.locator(f'.model-picker-option[data-model-value="{model}"]')
            await expect(option).to_contain_text("Access not confirmed")
            await option.click()
            await expect(self.page.locator("#model-select")).to_have_value(model)
            await expect(self.page.locator("#model-availability-notice")).to_be_visible()
            await self.page.wait_for_function("model => selectedRoom().model === model", arg=model)
            self.assertEqual(self.service.store.get_room(self.admin_id, self.room["id"])["model"], model)
            await expect(self.page.locator("#effort-select option[value='max']")).to_have_count(1)
            await expect(self.page.locator("#effort-select option[value='ultra']")).to_have_count(0 if model == "gpt-6-luna" else 1)
        await self.page.locator("#model-select").select_option("astra")
        await expect(self.page.locator("#model-availability-notice")).to_be_hidden()
        self.assertIsNone(self.worker.turn)
        self.assertFalse(self.errors, self.errors)

    async def test_model_picker_closes_with_the_non_popover_browser_fallback(self):
        await self.page.evaluate("""() => {
            const nativeMatches = Element.prototype.matches;
            Element.prototype.matches = function(selector) {
                if (selector === ':popover-open') throw new DOMException('Unsupported selector');
                return nativeMatches.call(this, selector);
            };
            Object.defineProperty(HTMLElement.prototype, 'showPopover', {configurable: true, value: undefined});
            Object.defineProperty(HTMLElement.prototype, 'hidePopover', {configurable: true, value: undefined});
        }""")
        trigger = self.page.locator(".model-picker-trigger")
        await trigger.click()
        await expect(self.page.locator(".model-picker-menu.model-picker-fallback")).to_be_visible()
        await trigger.evaluate("node => node.click()")
        await expect(self.page.locator(".model-picker-menu")).to_have_count(0)
        await expect(trigger).to_have_attribute("aria-expanded", "false")
        self.assertFalse(self.errors, self.errors)

    async def test_dynamic_side_controls_are_independent_and_programmatic_values_sync(self):
        await self.page.evaluate("""() => {
            const host = document.createElement('section');
            host.id = 'test-side-controls';
            host.style.cssText = 'position:fixed;top:150px;left:340px;z-index:1000;background:var(--surface);padding:20px';
            for (const [kind, values] of [['model', ['Terra', 'Sol']], ['effort', ['Low', 'Medium', 'High']]]) {
                const select = document.createElement('select');
                select.id = 'test-side-' + kind;
                select.dataset.control = kind;
                for (const value of values) select.append(new Option(value, value.toLowerCase()));
                host.append(select);
            }
            document.body.append(host);
        }""")
        side = self.page.locator("#test-side-controls")
        await expect(side.locator(".model-picker-value")).to_have_text("Terra")
        await side.get_by_role("button", name="More thinking", exact=True).click()
        await expect(self.page.locator("#test-side-effort")).to_have_value("medium")
        await expect(self.page.locator("#effort-select")).to_have_value("medium")
        await side.get_by_role("button", name="More thinking", exact=True).click()
        await expect(self.page.locator("#test-side-effort")).to_have_value("high")
        await expect(self.page.locator("#effort-select")).to_have_value("medium")
        await self.page.evaluate("document.getElementById('test-side-model').value = 'sol'")
        await expect(side.locator(".model-picker-value")).to_have_text("Sol")
        await expect(self.page.locator("#model-select")).to_have_value("astra")
        await self.page.evaluate("""() => {
            const effort = document.getElementById('test-side-effort');
            effort.replaceChildren(new Option('Default', ''));
            effort.value = '';
        }""")
        await expect(side.get_by_role("slider")).to_be_disabled()
        await expect(side.locator(".thinking-value")).to_have_text("Default")
        self.assertFalse(self.errors, self.errors)

    async def test_small_viewport_menu_stays_in_view_and_native_geometry_is_preserved(self):
        for width in (360, 400, 768, 1440):
            await self.page.set_viewport_size({"width": width, "height": 850})
            await self.page.locator(".model-picker-trigger").click()
            await expect(self.page.locator(".model-picker-menu")).to_be_visible()
            metrics = await self.page.evaluate("""() => {
                const bounds = selector => document.querySelector(selector).getBoundingClientRect();
                const menu = bounds('.model-picker-menu'), model = bounds('#model-select');
                const effort = bounds('#effort-select'), trigger = bounds('.model-picker-trigger');
                return {page: document.documentElement.scrollWidth <= innerWidth,
                    menu: menu.left >= 0 && menu.right <= innerWidth && menu.top >= 0 && menu.bottom <= innerHeight,
                    model: model.width === trigger.width && model.height >= 34,
                    effort: effort.width > 0 && effort.height >= 34};
            }""")
            self.assertTrue(all(metrics.values()), f"{width}: {metrics}")
            await self.page.keyboard.press("Escape")
        self.assertFalse(self.errors, self.errors)

    async def test_mist_is_scoped_and_respects_reduced_motion(self):
        await expect(self.page.locator(".pointer-mist")).to_have_count(2)
        brand = self.page.locator(".sidebar-brand")
        await brand.hover(position={"x": 40, "y": 20})
        await expect(brand).to_have_class("sidebar-brand pointer-mist-surface pointer-mist-active")
        first = await brand.locator(".pointer-mist").get_attribute("style")
        await brand.hover(position={"x": 90, "y": 30})
        await self.page.wait_for_function("previous => document.querySelector('.sidebar-brand .pointer-mist').getAttribute('style') !== previous", arg=first)
        await self.page.emulate_media(reduced_motion="reduce")
        await expect(brand.locator(".pointer-mist")).to_be_hidden()
        await brand.hover(position={"x": 100, "y": 25})
        await expect(brand).not_to_have_class("sidebar-brand pointer-mist-surface pointer-mist-active")
        self.assertFalse(self.errors, self.errors)

    async def test_pointer_bursts_measure_once_per_frame_and_keep_latest_position(self):
        result = await self.page.locator("#composer").evaluate("""async surface => {
            surface.dispatchEvent(new PointerEvent('pointerleave'));
            const original = surface.getBoundingClientRect;
            const rect = original.call(surface);
            let reads = 0;
            surface.getBoundingClientRect = function() {
                reads++;
                return original.call(this);
            };
            try {
                for (let index = 0; index < 50; index++) {
                    surface.dispatchEvent(new PointerEvent('pointermove', {
                        pointerType: 'mouse', clientX: rect.left + 50 + index,
                        clientY: rect.top + 30,
                    }));
                }
                const beforeFrame = reads;
                await new Promise(requestAnimationFrame);
                const mist = surface.querySelector('.pointer-mist');
                return {
                    beforeFrame, afterFrame: reads,
                    x: mist.style.getPropertyValue('--pointer-x'),
                    y: mist.style.getPropertyValue('--pointer-y'),
                    active: surface.classList.contains('pointer-mist-active'),
                };
            } finally {
                delete surface.getBoundingClientRect;
                surface.dispatchEvent(new PointerEvent('pointerleave'));
            }
        }""")
        self.assertEqual(result, {
            "beforeFrame": 0, "afterFrame": 1, "x": "99px", "y": "30px", "active": True,
        })
        self.assertFalse(self.errors, self.errors)

    async def test_space_composer_highlight_does_not_tile_and_mist_stops_during_drag(self):
        await self.page.locator("#theme-select").select_option("cosmic")
        composer = self.page.locator("#composer")
        repeat = await composer.evaluate("node => getComputedStyle(node).backgroundRepeat")
        self.assertTrue(all(value.strip() == "no-repeat" for value in repeat.split(',')), repeat)
        await composer.hover(position={"x": 100, "y": 25})
        await self.page.wait_for_function("document.getElementById('composer').classList.contains('pointer-mist-active')")
        mist = composer.locator(".pointer-mist")
        self.assertEqual(await mist.evaluate("node => getComputedStyle(node).backgroundImage"), "none")
        self.assertIn("56px 42px", await mist.evaluate("node => getComputedStyle(node, '::before').backgroundImage"))
        self.assertEqual(await mist.evaluate("node => getComputedStyle(node, '::before').width"), "112px")
        self.assertEqual(await mist.evaluate("node => getComputedStyle(node, '::after').width"), "84px")
        await self.page.mouse.down()
        await composer.hover(position={"x": 150, "y": 30})
        self.assertFalse(await composer.evaluate("node => node.classList.contains('pointer-mist-active')"))
        await self.page.mouse.up()
        await composer.hover(position={"x": 200, "y": 25})
        await self.page.wait_for_function("document.getElementById('composer').classList.contains('pointer-mist-active')")
        await self.page.wait_for_timeout(700)
        self.assertFalse(await composer.evaluate("node => node.classList.contains('pointer-mist-active')"))
        await expect(mist).to_have_css("opacity", "0")
        previous = await mist.get_attribute("style")
        await self.page.wait_for_timeout(200)
        self.assertEqual(await mist.get_attribute("style"), previous)
        self.assertFalse(self.errors, self.errors)
