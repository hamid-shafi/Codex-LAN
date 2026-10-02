"""CODEX: Browser regressions for loading, keyboard scope, and compact layout polish."""

from __future__ import annotations

import asyncio
import json
import unittest

from playwright.async_api import expect

import test_lan_browser as browser_fixture


class LanFrontendPolishBrowserTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = browser_fixture.LanBrowserTests.asyncSetUp
    asyncTearDown = browser_fixture.LanBrowserTests.asyncTearDown
    start_worker = browser_fixture.LanBrowserTests.start_worker
    login = browser_fixture.LanBrowserTests.login

    async def test_concurrent_session_checks_share_one_poll_timer(self):
        await self.login(self.admin)
        await expect(self.page.locator("#workspace")).to_be_visible()
        result = await self.page.evaluate(
            """async () => {
                clearTimeout(state.sessionTimer);
                state.sessionTimer = null;
                const original = window.setTimeout;
                const timers = [];
                window.setTimeout = function(callback, delay, ...args) {
                    const timer = original.call(window, callback, delay, ...args);
                    if (delay === 10000) timers.push(timer);
                    return timer;
                };
                try {
                    await Promise.all([checkSession(), checkSession()]);
                    return {
                        count: timers.length,
                        currentIsOnlyTimer: timers.length === 1
                            && state.sessionTimer === timers[0],
                    };
                } finally {
                    for (const timer of timers) clearTimeout(timer);
                    state.sessionTimer = null;
                    window.setTimeout = original;
                }
            }"""
        )
        self.assertEqual(result, {"count": 1, "currentIsOnlyTimer": True})
        self.assertFalse(self.errors, self.errors)

    async def test_login_waits_for_inflight_poll_then_refreshes_new_identity(self):
        await self.login(self.admin)
        await expect(self.page.locator("#room-title")).to_have_text("Working together")
        stale_admin = await self.page.evaluate(
            "() => fetch('/api/me').then(response => response.json())"
        )
        await self.page.evaluate(
            "() => { clearTimeout(state.sessionTimer); state.sessionTimer = null; }"
        )
        held = asyncio.Event()
        release = asyncio.Event()
        calls = 0
        poll_task = None
        login_task = None

        async def hold_first_me(route):
            nonlocal calls
            calls += 1
            if calls == 1:
                held.set()
                await release.wait()
                await route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps(stale_admin),
                )
                return
            await route.continue_()

        pattern = "**/api/me"
        await self.page.route(pattern, hold_first_me)
        try:
            poll_task = asyncio.create_task(self.page.evaluate("() => checkSession()"))
            await asyncio.wait_for(held.wait(), timeout=5)
            async with self.page.expect_response(
                lambda response: response.url.endswith("/api/session")
                and response.request.method == "POST"
            ) as response_info:
                login_task = asyncio.create_task(
                    self.page.evaluate("token => login(token)", self.client["token"])
                )
            await response_info.value
            await self.page.wait_for_function("() => state.me === null")
            release.set()
            await asyncio.gather(poll_task, login_task)

            self.assertEqual(calls, 2)
            self.assertEqual(
                await self.page.evaluate("() => String(state.me && state.me.id)"),
                self.client_id,
            )
            await expect(self.page.locator("#room-title")).to_have_text(
                "Client-owned room"
            )
            self.assertFalse(self.errors, self.errors)
        finally:
            release.set()
            pending = [
                task
                for task in (poll_task, login_task)
                if task is not None and not task.done()
            ]
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            await self.page.unroute(pattern, hold_first_me)

    async def test_uncached_room_load_is_busy_non_submittable_and_preserves_focus(self):
        await self.login(self.admin)
        await expect(self.page.locator("#room-title")).to_have_text("Working together")
        room = self.service.store.create_room(
            self.admin_id,
            "Uncached loading room",
            "/home/server/loading",
            "astra",
            "medium",
        )
        await self.page.evaluate("() => checkSession()")
        await self.page.wait_for_function(
            "id => state.rooms.some(room => String(room.id) === String(id))",
            arg=room["id"],
        )

        release = asyncio.Event()
        request_started = asyncio.Event()
        route_pattern = f"**/api/rooms/{room['id']}/messages*"

        async def hold_messages(route):
            request_started.set()
            await release.wait()
            await route.continue_()

        await self.page.route(route_pattern, hold_messages)
        try:
            await self.page.evaluate("id => { void selectRoom(id); }", room["id"])
            await asyncio.wait_for(request_started.wait(), timeout=1)

            timeline = self.page.locator("#timeline-scroll")
            loading = self.page.locator("#room-loading")
            spinner = loading.locator(".room-loading-spinner")
            await expect(timeline).to_have_attribute("aria-busy", "true")
            await expect(loading).to_be_visible()
            await expect(loading).to_contain_text("Loading recent messages")
            await expect(spinner).to_be_visible()
            await expect(spinner).to_have_attribute("aria-hidden", "true")
            self.assertNotEqual(
                await spinner.evaluate("node => getComputedStyle(node).animationName"),
                "none",
            )

            await self.page.locator("#message-input").fill("Keep this draft while history loads")
            await expect(self.page.locator("#send-button")).to_be_disabled()
            search = self.page.locator("#room-search")
            await search.focus()
            await expect(search).to_be_focused()

            release.set()
            await self.page.wait_for_function(
                "() => roomState().loaded && !roomState().loading"
            )
            await expect(loading).to_be_hidden()
            await expect(timeline).to_have_attribute("aria-busy", "false")
            await expect(search).to_be_focused()
            await expect(self.page.locator("#message-input")).to_have_value(
                "Keep this draft while history loads"
            )
            await expect(self.page.locator("#send-button")).to_be_enabled()
            self.assertFalse(self.errors, self.errors)
        finally:
            release.set()
            await self.page.unroute(route_pattern, hold_messages)

    async def test_model_shortcuts_ignore_search_and_open_room_dialog(self):
        await self.login(self.admin)
        model = self.page.locator("#model-select")
        effort = self.page.locator("#effort-select")
        await expect(model).to_have_value("astra")
        await expect(effort).to_have_value("medium")
        initial = [await model.input_value(), await effort.input_value()]

        search = self.page.locator("#room-search")
        await search.fill("Working")
        await search.press("Alt+ArrowDown")
        await search.press("Control+Alt+ArrowDown")
        await self.page.wait_for_timeout(100)
        await expect(search).to_be_focused()
        self.assertEqual(
            [await model.input_value(), await effort.input_value()],
            initial,
        )

        await self.page.locator("#new-room").click()
        await expect(self.page.locator("#room-title")).to_have_text("New chat")
        await self.page.locator("#room-menu").click()
        await expect(self.page.locator("#room-dialog")).to_be_visible()
        name = self.page.locator("#room-name-input")
        await name.fill("Shortcut-safe room name")
        await name.press("Alt+ArrowUp")
        await name.press("Control+Alt+ArrowUp")
        await self.page.wait_for_timeout(100)
        await expect(name).to_be_focused()
        await expect(name).to_have_value("Shortcut-safe room name")
        self.assertEqual(
            [await model.input_value(), await effort.input_value()],
            initial,
        )
        persisted = self.service.store.get_room(self.admin_id, self.room["id"])
        self.assertEqual([persisted["model"], persisted["effort"]], initial)
        self.assertFalse(self.errors, self.errors)

    async def test_header_and_composer_do_not_overflow_compact_breakpoints(self):
        await self.login(self.admin)
        await self.page.locator("#message-input").fill(
            "Responsive composer draft فارسی with English details"
        )

        for width in (360, 400, 768):
            await self.page.set_viewport_size({"width": width, "height": 850})
            await self.page.evaluate(
                "() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))"
            )
            for selector in (
                "#sidebar-toggle",
                "#speed-select",
                "#theme-select",
                "#connector-open",
                "#room-menu",
                "#message-input",
                "#model-select",
                "#effort-select",
                "#send-button",
            ):
                await expect(self.page.locator(selector)).to_be_visible()

            geometry = await self.page.evaluate(
                """() => {
                    const visible = node => {
                        const style = getComputedStyle(node);
                        const rect = node.getBoundingClientRect();
                        return style.display !== 'none' && style.visibility !== 'hidden'
                            && rect.width > 0 && rect.height > 0;
                    };
                    const contains = (outer, inner) => {
                        const parent = outer.getBoundingClientRect();
                        const child = inner.getBoundingClientRect();
                        return child.left >= parent.left - 1
                            && child.right <= parent.right + 1
                            && child.top >= parent.top - 1
                            && child.bottom <= parent.bottom + 1;
                    };
                    const topbar = document.querySelector('.topbar');
                    const composer = document.getElementById('composer-area');
                    const composerCard = document.getElementById('composer');
                    const toolbar = document.querySelector('.composer-toolbar');
                    const modelSelect = document.getElementById('model-select');
                    const speedSelect = document.getElementById('speed-select');
                    const modelRect = modelSelect.getBoundingClientRect();
                    const speedRect = speedSelect.getBoundingClientRect();
                    const speedStyle = getComputedStyle(speedSelect);
                    const measure = document.createElement('canvas').getContext('2d');
                    measure.font = `${speedStyle.fontStyle} ${speedStyle.fontWeight} ${speedStyle.fontSize} ${speedStyle.fontFamily}`;
                    const speedTextWidth = measure.measureText(speedSelect.selectedOptions[0].textContent.trim()).width;
                    const speedRequiredWidth = speedTextWidth
                        + parseFloat(speedStyle.paddingLeft)
                        + parseFloat(speedStyle.paddingRight)
                        + parseFloat(speedStyle.borderLeftWidth)
                        + parseFloat(speedStyle.borderRightWidth);
                    const headerChildren = [...topbar.children].filter(visible);
                    const composerChildren = [
                        document.getElementById('message-input'),
                        toolbar,
                        document.querySelector('.composer-footer'),
                        document.getElementById('attach-button'),
                        document.querySelector('.model-field'),
                        document.querySelector('.speed-field'),
                        document.querySelector('.effort-field'),
                        document.querySelector('.send-tools'),
                    ].filter(visible);
                    return {
                        documentFits: document.documentElement.scrollWidth <= innerWidth + 1,
                        topbarFits: topbar.scrollWidth <= topbar.clientWidth + 1,
                        composerFits: composer.scrollWidth <= composer.clientWidth + 1,
                        toolbarFits: toolbar.scrollWidth <= toolbar.clientWidth + 1,
                        headerChildrenFit: headerChildren.every(node => contains(topbar, node)),
                        composerChildrenFit: composerChildren.every(node => contains(composer, node)),
                        composerCompact: composerCard.getBoundingClientRect().height <= 175,
                        speedShowsStandard: speedSelect.selectedOptions[0].textContent.trim() === 'Standard',
                        speedValueFits: speedRect.width + 1 >= speedRequiredWidth,
                        speedBesideModel: Math.abs(speedRect.top - modelRect.top) <= 1
                            && modelRect.right <= speedRect.left + 1,
                    };
                }"""
            )
            self.assertTrue(all(geometry.values()), f"{width}px: {geometry}")

        self.assertFalse(self.errors, self.errors)

    async def test_quick_switcher_finds_rooms_and_actions_from_the_keyboard(self):
        await self.login(self.admin)
        await self.page.keyboard.press("Control+K")
        dialog = self.page.locator("#quick-switcher")
        search = self.page.locator("#quick-switcher-input")
        await expect(dialog).to_be_visible()
        await expect(search).to_be_focused()
        await expect(dialog.get_by_text("Rooms", exact=True)).to_be_visible()
        await expect(dialog.get_by_text("Actions", exact=True)).to_be_visible()

        await search.fill("Client-owned")
        await expect(dialog.locator(".quick-switcher-item")).to_have_count(1)
        await self.page.keyboard.press("Enter")
        await expect(dialog).to_be_hidden()
        await expect(self.page.locator("#room-title")).to_have_text("Client-owned room")

        await self.page.keyboard.press("Control+K")
        await search.fill("Administration")
        await expect(dialog.locator(".quick-switcher-item")).to_have_count(1)
        await self.page.keyboard.press("ArrowDown")
        await self.page.keyboard.press("Enter")
        await expect(self.page.locator("#admin-view")).to_be_visible()
        self.assertEqual(await self.page.evaluate("new URL(location.href).searchParams.get('view')"), "admin")
        self.assertFalse(self.errors, self.errors)

    async def test_draft_meter_is_quiet_until_a_meaningful_prompt_is_typed(self):
        await self.login(self.admin)
        meter = self.page.locator("#draft-meter")
        await self.page.locator("#message-input").fill("short")
        await expect(meter).to_be_hidden()
        await self.page.locator("#message-input").fill("Explain this deployment carefully and include the important failure cases.")
        await expect(meter).to_be_visible()
        await expect(meter).to_contain_text("chars · ≈")
        self.assertIn("attachments and conversation context are not included", await meter.get_attribute("title"))
        await self.page.locator("#message-input").fill("")
        await expect(meter).to_be_hidden()
        self.assertFalse(self.errors, self.errors)

    async def test_selection_refreshes_once_only_when_streamed_content_was_deferred(self):
        await self.login(self.admin)
        await expect(self.page.locator(".message.assistant .message-content")).to_be_visible()
        deferred = await self.page.evaluate(
            r"""() => {
                const article = document.querySelector('.message.assistant');
                const paragraph = article.querySelector('.message-content p');
                const range = document.createRange();
                range.selectNodeContents(paragraph);
                const selection = getSelection();
                selection.removeAllRanges();
                selection.addRange(range);
                const id = article.dataset.key.slice('message:'.length);
                const message = roomState().messages.find(item => String(item.id) === id);
                message.content += '\n\nDeferred selection refresh marker';
                renderTimeline();
                return {
                    pending: state.selectionRefreshPending,
                    visible: article.querySelector('.message-content').textContent.includes('Deferred selection refresh marker'),
                };
            }"""
        )
        self.assertEqual(deferred, {"pending": True, "visible": False})

        await self.page.evaluate(
            """() => {
                getSelection().removeAllRanges();
                document.dispatchEvent(new Event('selectionchange'));
            }"""
        )
        await expect(self.page.locator(".message.assistant .message-content")).to_contain_text(
            "Deferred selection refresh marker"
        )
        self.assertFalse(await self.page.evaluate("state.selectionRefreshPending"))

        calls = await self.page.evaluate(
            """() => {
                const original = window.renderTimeline;
                window.__selectionRenderCalls = 0;
                window.renderTimeline = (...args) => {
                    window.__selectionRenderCalls += 1;
                    return original(...args);
                };
                document.getElementById('message-input').focus();
                document.dispatchEvent(new Event('selectionchange'));
                return window.__selectionRenderCalls;
            }"""
        )
        self.assertEqual(calls, 0)
        self.assertFalse(self.errors, self.errors)

    async def test_many_attachments_and_long_landscape_draft_remain_reachable(self):
        await self.login(self.admin)
        preview = "data:image/gif;base64,R0lGODlhAQABAAD/ACwAAAAAAQABAAACADs="
        await self.page.set_viewport_size({"width": 360, "height": 640})
        await self.page.evaluate(
            """preview => {
                roomState().files = Array.from({length: 10}, (_, index) => ({
                    localId: `image-${index}`,
                    name: `Screenshot ${index + 1}.png`,
                    preview,
                    status: 'ready',
                }));
                renderAttachments();
                updateComposer();
            }""",
            preview,
        )
        await self.page.wait_for_timeout(50)
        attachments = await self.page.evaluate(
            """() => {
                const list = document.getElementById('attachment-list');
                const composer = document.getElementById('composer-area').getBoundingClientRect();
                const send = document.getElementById('send-button').getBoundingClientRect();
                const timeline = document.querySelector('.timeline-stage').getBoundingClientRect();
                return {
                    scrollable: list.scrollHeight > list.clientHeight,
                    composerBottom: composer.bottom,
                    sendBottom: send.bottom,
                    timelineHeight: timeline.height,
                    viewport: innerHeight,
                };
            }"""
        )
        self.assertTrue(attachments["scrollable"], attachments)
        self.assertLessEqual(attachments["composerBottom"], attachments["viewport"] + 1, attachments)
        self.assertLessEqual(attachments["sendBottom"], attachments["viewport"] + 1, attachments)
        self.assertGreaterEqual(attachments["timelineHeight"], 72, attachments)

        await self.page.evaluate("() => { roomState().files = []; renderAttachments(); updateComposer(); }")
        await self.page.set_viewport_size({"width": 667, "height": 375})
        await self.page.locator("#message-input").fill("\n".join(f"Line {index}" for index in range(20)))
        await self.page.wait_for_timeout(50)
        await self.page.evaluate("() => { document.getElementById('jump-bottom').hidden = false; }")
        landscape = await self.page.evaluate(
            """() => {
                const rect = selector => document.querySelector(selector).getBoundingClientRect();
                const timeline = rect('.timeline-stage');
                const composer = rect('#composer-area');
                const toolbar = rect('.composer-toolbar');
                const jump = rect('#jump-bottom');
                const overlaps = !(jump.right <= composer.left || jump.left >= composer.right || jump.bottom <= composer.top || jump.top >= composer.bottom);
                return {timelineHeight: timeline.height, composerBottom: composer.bottom, toolbarBottom: toolbar.bottom, jumpBottom: jump.bottom, timelineBottom: timeline.bottom, overlaps, viewport: innerHeight};
            }"""
        )
        self.assertGreaterEqual(landscape["timelineHeight"], 72, landscape)
        self.assertLessEqual(landscape["composerBottom"], landscape["viewport"] + 1, landscape)
        self.assertLessEqual(landscape["toolbarBottom"], landscape["viewport"] + 1, landscape)
        self.assertLessEqual(landscape["jumpBottom"], landscape["timelineBottom"] + 1, landscape)
        self.assertFalse(landscape["overlaps"], landscape)
        self.assertFalse(self.errors, self.errors)

    async def test_dialog_sidebar_reply_and_attachment_focus_are_accessible(self):
        await self.login(self.admin)
        await self.page.locator("#new-room").click()
        await expect(self.page.locator("#room-dialog")).to_be_hidden()
        await expect(self.page.locator("#room-title")).to_have_text("New chat")
        await self.page.locator("#room-menu").click()
        await expect(self.page.get_by_role("dialog", name="Room settings")).to_be_visible()
        await self.page.locator("#dialog-cancel").click()
        await self.page.locator("#room-list .room-item").filter(has_text="Working together").click()

        reply = self.page.locator(".message.assistant").get_by_role("button", name="Reply", exact=True)
        await reply.click()
        await self.page.locator("#reply-clear").focus()
        await self.page.locator("#reply-clear").press("Enter")
        await expect(self.page.locator("#reply-preview")).to_be_hidden()
        await expect(self.page.locator("#message-input")).to_be_focused()

        preview = "data:image/gif;base64,R0lGODlhAQABAAD/ACwAAAAAAQABAAACADs="
        await self.page.evaluate(
            """preview => {
                roomState().files = [
                    {localId: 'first', name: 'same.png', preview, status: 'ready'},
                    {localId: 'second', name: 'same.png', preview, status: 'ready'},
                ];
                renderAttachments();
            }""",
            preview,
        )
        remove_buttons = self.page.locator("#attachment-list button")
        await remove_buttons.first.click()
        await expect(self.page.locator("#attachment-list button")).to_be_focused()
        await self.page.locator("#attachment-list button").click()
        await expect(self.page.locator("#attach-button")).to_be_focused()

        await self.page.set_viewport_size({"width": 400, "height": 700})
        await self.page.locator("#sidebar-toggle").click()
        await expect(self.page.locator("#new-room")).to_be_focused()
        self.assertTrue(await self.page.locator(".main").evaluate("node => node.inert"))
        await self.page.keyboard.press("Tab")
        await expect(self.page.locator("#room-search")).to_be_focused()
        await self.page.keyboard.press("Escape")
        await expect(self.page.locator("#sidebar-toggle")).to_be_focused()
        self.assertFalse(await self.page.locator(".main").evaluate("node => node.inert"))
        self.assertFalse(self.errors, self.errors)

    async def test_toast_dismissal_is_keyboard_safe_and_stale_timers_are_ignored(self):
        await self.login(self.admin)
        message = self.page.locator("#message-input")
        toast_node = self.page.locator("#toast")
        close = self.page.get_by_role(
            "button", name="Dismiss notification", exact=True
        )

        await message.focus()
        await self.page.evaluate("() => toast('Click notification')")
        await expect(toast_node).to_be_visible()
        await expect(toast_node).to_contain_text("Click notification")
        await close.click()
        await expect(toast_node).to_be_hidden()
        await expect(message).to_be_focused()

        timers = await self.page.evaluate(
            """() => {
                toast('Superseded notification');
                const stale = state.toastTimer;
                toast('Keyboard notification');
                return {stale, current: state.toastTimer};
            }"""
        )
        self.assertNotEqual(timers["stale"], timers["current"])
        self.assertFalse(
            await self.page.evaluate(
                "timer => dismissToast(timer)", timers["stale"]
            )
        )
        await expect(toast_node).to_be_visible()
        await expect(toast_node).to_contain_text("Keyboard notification")

        await close.focus()
        await close.press("Enter")
        await expect(toast_node).to_be_hidden()
        await expect(message).to_be_focused()
        self.assertFalse(self.errors, self.errors)


if __name__ == "__main__":
    unittest.main()
