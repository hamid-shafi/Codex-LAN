"""CODEX: Real-browser checks for persistent selection panels and independent side rooms."""

from __future__ import annotations

import asyncio
import json
import unittest

from playwright.async_api import expect

import test_lan_browser as browser_fixture
from test_lan_server import FakeWorker, host_worker_socket


PANEL_MODELS = [*browser_fixture.BROWSER_MODELS, {
    "id": "gpt-5.6-terra", "model": "gpt-5.6-terra", "isDefault": False,
    "defaultReasoningEffort": "low",
    "supportedReasoningEfforts": [{"reasoningEffort": value} for value in ("low", "medium", "high")],
}]


class CompanionBrowserTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = browser_fixture.LanBrowserTests.asyncSetUp
    asyncTearDown = browser_fixture.LanBrowserTests.asyncTearDown
    login = browser_fixture.LanBrowserTests.login
    wait_worker_turn = browser_fixture.LanBrowserTests.wait_worker_turn

    async def start_worker(self, device, name):
        ws, _auth = await host_worker_socket(self.http, self.service, self.admin, device)
        await ws.send_json({"type": "ready", "models": PANEL_MODELS, "cwd": "/home/" + name})
        worker = FakeWorker(ws, name)
        self.workers.append(worker)
        await self.until(lambda: self.service.runtime.connected(device["device"]["id"]))
        return worker

    async def until(self, condition):
        for _ in range(600):
            if condition():
                return
            await asyncio.sleep(.01)
        self.fail("Condition did not become true")

    def companions(self):
        return self.service.store.list_companions(self.admin_id, self.room["id"])

    async def select_source(self):
        paragraph = self.page.locator("#timeline .message.assistant .message-content p").first
        await paragraph.scroll_into_view_if_needed()
        await paragraph.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
        await paragraph.evaluate("""node => {
            const range = document.createRange(); range.selectNodeContents(node);
            const selection = getSelection(); selection.removeAllRanges(); selection.addRange(range);
            document.dispatchEvent(new Event('selectionchange'));
        }""")
        await expect(self.page.locator("#selection-actions")).to_be_visible()
        return await paragraph.inner_text()

    async def create_panel(self, kind):
        await self.select_source()
        await self.page.locator(f'#selection-actions [data-kind="{kind}"]').click()
        await expect(self.page.locator(".companion-dock" if kind == "side_thread" else ".companion-details-window")).to_be_visible()
        await self.until(lambda: bool(self.companions()))
        return self.companions()[-1]

    async def test_selection_waits_for_release_then_delays_and_fades(self):
        await self.login(self.admin)
        paragraph = self.page.locator("#timeline .message.assistant .message-content p").first
        await paragraph.scroll_into_view_if_needed()
        await paragraph.hover()
        await self.page.mouse.down()
        await paragraph.evaluate("""node => {
            const range = document.createRange(); range.selectNodeContents(node);
            getSelection().removeAllRanges(); getSelection().addRange(range);
        }""")
        await self.page.wait_for_timeout(300)
        toolbar = self.page.locator("#selection-actions")
        await expect(toolbar).to_be_hidden()
        await self.page.mouse.up()
        self.assertTrue(await toolbar.evaluate("node => node.hidden"))
        await expect(toolbar).to_be_visible()
        self.assertEqual(await toolbar.evaluate("node => getComputedStyle(node).animationName"), "selection-actions-enter")
        selected = await self.page.evaluate("getSelection().toString()")
        self.assertTrue(selected)
        await self.page.keyboard.press("Escape")
        await self.page.wait_for_timeout(250)
        await expect(toolbar).to_be_hidden()
        self.assertEqual(await self.page.evaluate("getSelection().toString()"), selected)
        self.assertFalse(self.errors, self.errors)

    async def test_pending_selection_popup_cancels_and_respects_reduced_motion(self):
        await self.login(self.admin)
        await self.select_source()
        toolbar = self.page.locator("#selection-actions")
        for cancellation in ("scroll", "resize", "blur", "escape", "deselect"):
            await self.page.evaluate("""kind => {
                document.dispatchEvent(new Event('selectionchange'));
                if (kind === 'scroll') document.getElementById('timeline-scroll').dispatchEvent(new Event('scroll'));
                else if (kind === 'escape') document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape'}));
                else if (kind === 'deselect') getSelection().removeAllRanges();
                else window.dispatchEvent(new Event(kind));
            }""", cancellation)
            await self.page.wait_for_timeout(250)
            await expect(toolbar).to_be_hidden()
        await self.page.emulate_media(reduced_motion="reduce")
        await self.select_source()
        self.assertEqual(await toolbar.evaluate("node => getComputedStyle(node).animationName"), "none")
        self.assertFalse(self.errors, self.errors)

    async def test_background_tab_pauses_companion_polling_and_resumes_when_visible(self):
        await self.login(self.admin)
        await self.page.evaluate("""() => {
            window.__companionFetches = [];
            const nativeFetch = window.fetch.bind(window);
            window.fetch = (...args) => {
                const source = typeof args[0] === 'string' ? args[0] : args[0].url;
                const url = new URL(source, location.href);
                if (/\/api\/rooms\/[^/]+\/companions$/.test(url.pathname)) {
                    window.__companionFetches.push(url.pathname);
                }
                return nativeFetch(...args);
            };
            Object.defineProperty(document, 'hidden', {configurable: true, get: () => true});
            document.dispatchEvent(new Event('visibilitychange'));
        }""")
        await self.page.wait_for_timeout(6200)
        self.assertEqual(await self.page.evaluate("window.__companionFetches"), [])
        await self.page.evaluate("""() => {
            Object.defineProperty(document, 'hidden', {configurable: true, get: () => false});
            document.dispatchEvent(new Event('visibilitychange'));
        }""")
        await self.page.wait_for_function("window.__companionFetches.length > 0")
        self.assertFalse(self.errors, self.errors)

    async def test_selection_actions_preserve_reply_and_native_selection(self):
        await self.login(self.admin)
        await expect(self.page.locator("#room-title")).to_have_text("Working together")
        footer = self.page.locator("#timeline .message.assistant .message-footer")
        await expect(footer.get_by_role("button", name="Reply", exact=True)).to_be_visible()
        await expect(footer.get_by_role("button", name="Side thread", exact=True)).to_have_count(0)
        selected = await self.select_source()
        toolbar = self.page.locator("#selection-actions")
        await expect(toolbar.get_by_role("button", name="Side thread", exact=True)).to_be_visible()
        await expect(toolbar.get_by_role("button", name="More details", exact=True)).to_be_visible()
        self.assertEqual(await self.page.evaluate("getSelection().toString()"), selected)
        await self.page.keyboard.press("Escape")
        await expect(toolbar).to_be_hidden()
        self.assertEqual(await self.page.evaluate("getSelection().toString()"), selected)
        await footer.get_by_role("button", name="Reply", exact=True).click()
        await expect(self.page.locator("#reply-text")).to_have_text(selected)
        self.assertFalse(self.errors, self.errors)

    async def test_details_use_low_read_only_and_survive_reload_and_disconnect(self):
        await self.login(self.admin)
        await expect(self.page.locator("#room-title")).to_have_text("Working together")
        companion = await self.create_panel("more_details")
        await self.wait_worker_turn()
        child = self.service.store.get_room(self.admin_id, companion["room_id"])
        self.assertEqual((child["model"], child["effort"], child["speed"], child["tool_read_only"]),
                         ("gpt-5.6-terra", "low", "standard", True))
        turn = next(call for call in self.worker.calls if call["method"] == "turn/start")
        self.assertEqual((turn["params"]["model"], turn["params"]["effort"]), ("gpt-5.6-terra", "low"))
        self.assertEqual(turn["params"]["sandboxPolicy"]["type"], "readOnly")
        self.assertIn(json.dumps(self.answer["content"], ensure_ascii=False), turn["params"]["input"][0]["text"])
        self.assertIn("Help plan this project", turn["params"]["input"][0]["text"])
        await self.worker.delta("A useful explanation of the selected passage.")
        explanation = self.page.locator(".companion-explanation")
        await expect(explanation).to_contain_text("A useful explanation", timeout=10000)
        await self.context.set_offline(True)
        await self.worker.delta(" Additional durable context.")
        await self.worker.usage(50, 20)
        await self.worker.complete()
        await expect(explanation).to_contain_text("A useful explanation")
        await expect(self.page.locator(".companion-details-window")).to_be_visible()
        await self.context.set_offline(False)
        await expect(explanation).to_contain_text("Additional durable context", timeout=10000)
        await self.page.locator(f'.room-row[data-room-id="{self.foreign["id"]}"] .room-item').click()
        await expect(self.page.locator("#room-title")).to_have_text("Client-owned room")
        await expect(self.page.locator(".companion-details-window")).to_be_hidden()
        await self.page.locator(f'.room-row[data-room-id="{self.room["id"]}"] .room-item').click()
        await expect(explanation).to_contain_text("Additional durable context")
        await expect(self.page.locator(".companion-details-window")).to_be_visible()
        await self.page.reload()
        await expect(self.page.locator(".companion-explanation")).to_contain_text("Additional durable context", timeout=10000)
        self.assertEqual(len([call for call in self.worker.calls if call["method"] == "turn/start"]), 1)
        self.assertEqual(self.companions()[0]["id"], companion["id"])
        dialogs = []

        async def on_dialog(dialog):
            dialogs.append(dialog.message)
            await dialog.dismiss()

        self.page.on("dialog", on_dialog)
        await self.page.get_by_role("button", name="Close More details", exact=True).click()
        await expect(self.page.locator(".companion-details-window")).to_be_hidden()
        self.assertEqual(self.companions(), [])
        self.assertEqual(dialogs, [])
        with self.assertRaises(PermissionError):
            self.service.store.get_room(self.admin_id, companion["room_id"])
        await self.page.reload()
        await expect(self.page.locator("#room-title")).to_have_text("Working together")
        await expect(self.page.locator(".companion-details-window")).to_be_hidden()
        self.assertFalse(self.errors, self.errors)

    async def test_side_room_settings_draft_and_confirmed_delete_are_independent(self):
        await self.login(self.admin)
        await expect(self.page.locator("#room-title")).to_have_text("Working together")
        await self.page.locator("#message-input").fill("Main room draft remains untouched")
        companion = await self.create_panel("side_thread")
        frame = self.page.frame_locator(".companion-frame")
        await expect(frame.locator("#workspace")).to_be_visible(timeout=10000)
        await expect(frame.locator("#message-input")).to_be_visible()
        await expect(frame.locator("html")).to_have_attribute("data-embedded", "true")
        await expect(frame.locator("#sidebar")).to_be_hidden()
        await expect(frame.locator(".topbar")).to_be_hidden()
        await expect(self.page.locator("#room-list")).not_to_contain_text("Side thread")
        await frame.locator("#model-select").select_option("sol")
        await expect(frame.locator("#model-select")).to_have_value("sol")
        await frame.locator("#effort-select").select_option("high")
        await frame.locator("#tool-read-only").check()
        await self.until(lambda: self.service.store.get_room(self.admin_id, companion["room_id"])["effort"] == "high")
        parent = self.service.store.get_room(self.admin_id, self.room["id"])
        child = self.service.store.get_room(self.admin_id, companion["room_id"])
        self.assertEqual((parent["model"], parent["effort"], parent["tool_read_only"]), ("astra", "medium", False))
        self.assertEqual((child["model"], child["effort"], child["tool_read_only"]), ("sol", "high", True))
        await self.page.locator(".companion-menu-trigger").click()
        await self.page.locator(".companion-menu-card").get_by_role("button", name="Room settings", exact=True).click()
        await expect(frame.locator("#room-dialog")).to_be_visible()
        await frame.locator("#room-cwd-input").fill("/home/server/separate-workspace")
        await frame.locator("#room-save").click()
        await expect(frame.locator("#room-dialog")).to_be_hidden()
        self.assertEqual(self.service.store.get_room(self.admin_id, companion["room_id"])["cwd"], "/home/server/separate-workspace")
        self.assertEqual(self.service.store.get_room(self.admin_id, self.room["id"])["cwd"], "/home/server/project")
        await frame.locator("#message-input").fill("Independent side draft فارسی")
        await self.until(lambda: self.companions()[0]["draft"] == "Independent side draft فارسی")
        await expect(self.page.locator("#message-input")).to_have_value("Main room draft remains untouched")
        self.assertEqual(await self.page.evaluate("localStorage.getItem(`codex-lan-room:${state.me.id}`)"), self.room["id"])
        await self.page.reload()
        frame = self.page.frame_locator(".companion-frame")
        await expect(frame.locator("#message-input")).to_have_value("Independent side draft فارسی", timeout=10000)
        await expect(frame.locator("#model-select")).to_have_value("sol")
        await expect(frame.locator("#effort-select")).to_have_value("high")
        await expect(frame.locator("#tool-read-only")).to_be_checked()
        self.page.once("dialog", lambda dialog: dialog.dismiss())
        await self.page.locator(".companion-menu-trigger").click()
        await self.page.get_by_role("button", name="Delete side thread", exact=True).click()
        await expect(self.page.locator(".companion-frame")).to_be_visible()
        self.assertEqual(len(self.companions()), 1)
        self.page.once("dialog", lambda dialog: dialog.accept())
        await self.page.locator(".companion-menu-trigger").click()
        await self.page.get_by_role("button", name="Delete side thread", exact=True).click()
        await expect(self.page.locator(".companion-dock")).to_be_hidden()
        self.assertEqual(self.companions(), [])
        with self.assertRaises(PermissionError):
            self.service.store.get_room(self.admin_id, companion["room_id"])
        self.assertEqual(self.service.store.get_room(self.admin_id, self.room["id"])["title"], "Working together")
        self.assertFalse(self.errors, self.errors)

    async def test_details_selected_during_completion_render_final_text_after_deselect(self):
        await self.login(self.admin)
        await expect(self.page.locator("#room-title")).to_have_text("Working together")
        await self.create_panel("more_details")
        await self.wait_worker_turn()
        await self.worker.delta("Selected explanation stays stable.")
        explanation = self.page.locator(".companion-explanation")
        await expect(explanation).to_have_text("Selected explanation stays stable.", timeout=10000)
        await explanation.locator("p").evaluate("""node => {
            const range = document.createRange(); range.selectNodeContents(node);
            const selection = getSelection(); selection.removeAllRanges(); selection.addRange(range);
        }""")
        await self.worker.delta(" The final sentence must appear after deselecting.")
        await self.worker.usage(50, 25)
        await self.worker.complete()
        await expect(self.page.locator(".companion-status")).to_contain_text("Saved in this room", timeout=10000)
        self.assertEqual(await self.page.evaluate("getSelection().toString()"), "Selected explanation stays stable.")
        await expect(explanation).not_to_contain_text("final sentence")
        await self.page.evaluate("getSelection().removeAllRanges()")
        await expect(explanation).to_contain_text("The final sentence must appear after deselecting.", timeout=10000)
        self.assertFalse(self.errors, self.errors)

    async def test_inactive_side_panels_do_not_load_iframes_until_selected(self):
        panels = [self.service.store.create_companion(
            self.admin_id, self.room["id"], "side_thread", self.answer["id"],
            "The plan starts here.", f"saved-side-{index}",
        )["companion"] for index in range(3)]
        frame_requests = []
        self.page.on("request", lambda request: frame_requests.append(request.url)
                     if request.is_navigation_request() and "embedded=1" in request.url else None)
        await self.login(self.admin)
        await expect(self.page.locator(".companion-tab")).to_have_count(3)
        active = self.page.locator(f'#companion-panel-{panels[-1]["id"]}')
        await expect(active).to_be_visible()
        await expect(active.frame_locator("iframe").locator("#workspace")).to_be_visible(timeout=10000)
        self.assertEqual(len(frame_requests), 1, frame_requests)
        self.assertIn(panels[-1]["room_id"], frame_requests[0])
        await self.page.locator(f'#companion-tab-{panels[0]["id"]}').click()
        first = self.page.locator(f'#companion-panel-{panels[0]["id"]}')
        await expect(first.frame_locator("iframe").locator("#workspace")).to_be_visible(timeout=10000)
        self.assertEqual(len(frame_requests), 2, frame_requests)
        self.assertIn(panels[0]["room_id"], frame_requests[1])
        self.assertFalse(any(panels[1]["room_id"] in url for url in frame_requests), frame_requests)
        await self.page.locator(f'#companion-tab-{panels[-1]["id"]}').click()
        await expect(active).to_be_visible()
        self.assertEqual(len(frame_requests), 2, frame_requests)
        self.assertFalse(self.errors, self.errors)

    async def test_split_layout_and_same_origin_frame_policy(self):
        response = await self.http.get("/")
        self.assertEqual(response.headers["X-Frame-Options"], "SAMEORIGIN")
        self.assertIn("frame-ancestors 'self'", response.headers["Content-Security-Policy"])
        self.assertIn("frame-src 'self'", response.headers["Content-Security-Policy"])
        await self.login(self.admin)
        await expect(self.page.locator("#room-title")).to_have_text("Working together")
        await self.create_panel("side_thread")
        frame = self.page.frame_locator(".companion-frame")
        await expect(frame.locator("#workspace")).to_be_visible(timeout=10000)
        await expect(frame.locator(".topbar")).to_be_hidden()
        for width in (1920, 1440, 768, 400):
            await self.page.set_viewport_size({"width": width, "height": 950})
            if width == 400:
                await expect(self.page.locator("#sidebar")).to_have_css("transform", "matrix(1, 0, 0, 1, -270, 0)")
            await expect(frame.locator("#message-input")).to_be_visible()
            if width in (1920, 1440):
                composer = await self.page.locator("#composer").bounding_box()
                embedded_composer = await frame.locator("#composer").bounding_box()
                self.assertLess(composer["height"], 230, composer)
                self.assertLess(embedded_composer["height"], 230, embedded_composer)
                for root in (self.page.locator("#composer"), frame.locator("#composer")):
                    geometry = await root.evaluate("""composer => {
                        const selectors = ['.attachment-tool', '.model-picker-trigger', '#speed-select', '.smart-effort', '.composer-read-only'];
                        const bounds = selectors.map(selector => {
                            const box = composer.querySelector(selector).getBoundingClientRect();
                            return {selector, left: box.left, right: box.right, top: box.top, bottom: box.bottom};
                        });
                        const outer = composer.getBoundingClientRect();
                        const inside = bounds.every(box => box.left >= outer.left - 1 && box.right <= outer.right + 1 && box.top >= outer.top - 1 && box.bottom <= outer.bottom + 1);
                        const overlaps = [];
                        for (let left = 0; left < bounds.length; left++) for (let right = left + 1; right < bounds.length; right++) {
                            const a = bounds[left], b = bounds[right];
                            if (Math.min(a.right, b.right) - Math.max(a.left, b.left) > 1 && Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top) > 1) overlaps.push([a.selector, b.selector]);
                        }
                        const attachment = composer.querySelector('.attachment-tool');
                        return {inside, overlaps, bounds,
                            attachment: {width: attachment.getBoundingClientRect().width, labelDisplay: getComputedStyle(attachment.querySelector('span')).display},
                            outer: {left: outer.left, right: outer.right, top: outer.top, bottom: outer.bottom}};
                    }""")
                    self.assertTrue(geometry["inside"], geometry)
                    self.assertEqual(geometry["overlaps"], [], geometry)
                    self.assertLessEqual(geometry["attachment"]["width"], 40, geometry)
                    self.assertEqual(geometry["attachment"]["labelDisplay"], "none", geometry)
            self.assertTrue(await self.page.evaluate("document.documentElement.scrollWidth <= innerWidth"), width)
            self.assertTrue(await frame.locator("html").evaluate("node => node.scrollWidth <= innerWidth"), width)
            await frame.locator(".model-picker-trigger").click()
            menu = frame.locator(".model-picker-menu")
            await expect(menu).to_be_visible()
            await expect(menu).to_have_css("transform", "none")
            bounds = await menu.evaluate("""node => {
                const box = node.getBoundingClientRect();
                return { left: box.left, top: box.top, right: box.right, bottom: box.bottom,
                         width: innerWidth, height: innerHeight };
            }""")
            self.assertGreaterEqual(bounds["left"], 0, (width, bounds))
            self.assertGreaterEqual(bounds["top"], 0, (width, bounds))
            self.assertLessEqual(bounds["right"], bounds["width"], (width, bounds))
            self.assertLessEqual(bounds["bottom"], bounds["height"], (width, bounds))
            await self.page.screenshot(path=f"/tmp/codex-companion-model-picker-{width}.png", full_page=True)
            await self.page.keyboard.press("Escape")
            await expect(menu).to_have_count(0)
            await self.page.screenshot(path=f"/tmp/codex-companions-{width}.png", full_page=True)
        self.assertFalse(self.errors, self.errors)

    async def test_phone_split_preserves_both_conversation_timelines(self):
        await self.login(self.admin)
        await self.create_panel("side_thread")
        frame = self.page.frame_locator(".companion-frame")
        await expect(frame.locator("#workspace")).to_be_visible(timeout=10000)
        for width, height in ((360, 760), (400, 850)):
            with self.subTest(viewport=(width, height)):
                await self.page.set_viewport_size({"width": width, "height": height})
                await self.page.wait_for_timeout(100)
                main_timeline = await self.page.locator(".chat-workbench > .chat-view #timeline-scroll").bounding_box()
                side_timeline = await frame.locator("#timeline-scroll").bounding_box()
                self.assertGreaterEqual(main_timeline["height"], 120, main_timeline)
                self.assertGreaterEqual(side_timeline["height"], 120, side_timeline)
                self.assertTrue(await self.page.evaluate("document.documentElement.scrollWidth <= innerWidth"), width)
                self.assertTrue(await frame.locator("html").evaluate("node => node.scrollWidth <= innerWidth"), width)
        self.assertFalse(self.errors, self.errors)

    async def test_details_float_without_resizing_chat_and_follow_theme(self):
        await self.login(self.admin)
        before = await self.page.locator("#chat-view").bounding_box()
        await self.create_panel("more_details")
        popup = self.page.get_by_role("dialog", name="More details", exact=True)
        await expect(popup).to_be_visible()
        await expect(self.page.locator(".companion-dock")).to_be_hidden()
        await expect(self.page.locator(".companion-divider")).to_be_hidden()
        after = await self.page.locator("#chat-view").bounding_box()
        self.assertAlmostEqual(before["width"], after["width"], delta=1)
        self.assertAlmostEqual(before["height"], after["height"], delta=1)
        await expect(popup.locator(".companion-explanation")).to_have_css("overflow-y", "auto")
        await self.page.evaluate("setTheme('cosmic')")
        cosmic = await popup.evaluate("node => getComputedStyle(node).backgroundColor")
        await self.page.screenshot(path="/tmp/codex-details-floating-cosmic.png", full_page=True)
        await self.page.evaluate("setTheme('light')")
        self.assertNotEqual(cosmic, await popup.evaluate("node => getComputedStyle(node).backgroundColor"))
        for width in (1440, 768, 390):
            await self.page.set_viewport_size({"width": width, "height": 800})
            bounds = await popup.bounding_box()
            self.assertGreaterEqual(bounds["x"], 0)
            self.assertLessEqual(bounds["x"] + bounds["width"], width)
            self.assertLessEqual(bounds["y"] + bounds["height"], 800)
        self.assertFalse(self.errors, self.errors)

    async def test_side_split_mouse_keyboard_resize_persists_and_details_coexist(self):
        await self.login(self.admin)
        await self.page.set_viewport_size({"width": 1600, "height": 1000})
        await self.create_panel("side_thread")
        divider = self.page.get_by_role("separator", name="Resize main room and side thread")
        await expect(divider).to_be_visible()
        initial = await self.page.locator("#chat-view").bounding_box()
        box = await divider.bounding_box()
        await self.page.mouse.move(box["x"] + box["width"] / 2, box["y"] + 120)
        await self.page.mouse.down()
        await self.page.mouse.move(box["x"] + 110, box["y"] + 120, steps=8)
        await self.page.mouse.up()
        moved = await self.page.locator("#chat-view").bounding_box()
        self.assertGreater(moved["width"], initial["width"] + 70)
        await divider.focus()
        await self.page.keyboard.press("ArrowLeft")
        smaller = await self.page.locator("#chat-view").bounding_box()
        self.assertLess(smaller["width"], moved["width"])
        await self.page.reload()
        await expect(divider).to_be_visible()
        restored = await self.page.locator("#chat-view").bounding_box()
        self.assertAlmostEqual(restored["width"], smaller["width"], delta=2)
        await self.page.screenshot(path="/tmp/codex-side-resized.png", full_page=True)
        await self.create_panel("more_details")
        await expect(self.page.locator(".companion-dock")).to_be_visible()
        await expect(self.page.locator(".companion-details-window")).to_be_visible()
        await self.wait_worker_turn()
        await self.page.get_by_role("button", name="Close More details", exact=True).click()
        await self.until(lambda: any(call["method"] == "turn/interrupt" for call in self.worker.calls))
        await self.worker.complete(status="interrupted")
        await expect(self.page.locator(".companion-details-window")).to_be_hidden()
        await expect(self.page.locator(".companion-dock")).to_be_visible()
        await self.page.set_viewport_size({"width": 390, "height": 900})
        await expect(divider).to_be_hidden()
        self.assertTrue(await self.page.evaluate("document.documentElement.scrollWidth <= innerWidth"))
        self.assertFalse(self.errors, self.errors)

    async def test_selection_across_messages_copies_literal_text_without_clipboard(self):
        await self.login(self.admin)
        await expect(self.page.locator("#timeline .message.assistant .message-content")).to_be_visible()
        await self.page.evaluate("""() => {
            window.clipboardWrites = 0;
            Object.defineProperty(navigator.clipboard, 'writeText', {value: () => { window.clipboardWrites++; throw Error('clipboard forbidden'); }});
            const contents = [...document.querySelectorAll('#timeline .message-content')];
            const range = document.createRange(); range.setStart(contents[0], 0);
            range.setEnd(contents[contents.length - 1], contents[contents.length - 1].childNodes.length);
            getSelection().removeAllRanges(); getSelection().addRange(range);
        }""")
        await expect(self.page.locator("#selection-actions")).to_be_visible()
        selected = await self.page.evaluate("getSelection().toString().trim()")
        await self.page.locator('#selection-actions [data-kind="side_thread"]').click()
        await expect(self.page.locator(".companion-dock")).to_be_visible()
        self.assertEqual(self.companions()[0]["selected_text"], selected)
        self.assertEqual(await self.page.evaluate("window.clipboardWrites"), 0)
        self.assertFalse(self.errors, self.errors)


if __name__ == "__main__":
    unittest.main()
