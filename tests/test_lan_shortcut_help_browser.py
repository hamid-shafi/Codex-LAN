"""CODEX: Real Chromium checks for the keyboard guide and its actual bindings."""

from __future__ import annotations

import unittest

from playwright.async_api import expect

import test_lan_browser as browser_fixture


class LanShortcutHelpBrowserTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = browser_fixture.LanBrowserTests.asyncSetUp
    asyncTearDown = browser_fixture.LanBrowserTests.asyncTearDown
    start_worker = browser_fixture.LanBrowserTests.start_worker
    login = browser_fixture.LanBrowserTests.login

    async def dispatch_key(self, options):
        return await self.page.evaluate(
            """options => {
                const event = new KeyboardEvent('keydown', {
                    ...options, bubbles: true, cancelable: true,
                });
                if (options.alreadyPrevented) event.preventDefault();
                document.activeElement.dispatchEvent(event);
                return event.defaultPrevented;
            }""",
            options,
        )

    async def test_visible_help_trigger_and_keyboard_preserve_draft_and_focus(self):
        await self.login(self.admin)
        trigger = self.page.get_by_role("button", name="Keyboard shortcuts", exact=True)
        dialog = self.page.get_by_role("dialog", name="Keyboard shortcuts", exact=True)
        close = self.page.get_by_role("button", name="Close keyboard shortcuts", exact=True)
        message = self.page.locator("#message-input")
        await expect(trigger).to_have_attribute("aria-keyshortcuts", "Control+/ Meta+/")
        await expect(self.page.locator("#sidebar")).not_to_contain_text("Ctrl K")
        await trigger.click()
        await expect(dialog).to_be_visible()
        await expect(close).to_be_focused()
        await message.evaluate("node => node.focus()")
        await expect(close).to_be_focused()
        await close.click()
        await expect(dialog).to_be_hidden()
        await expect(trigger).to_be_focused()

        await message.fill("A draft stays intact while learning shortcuts")
        await message.press("Control+/")
        await expect(dialog).to_be_visible()
        await expect(close).to_be_focused()
        await expect(message).to_have_value("A draft stays intact while learning shortcuts")
        self.assertIsNone(self.worker.turn)
        await self.page.keyboard.press("Escape")
        await expect(dialog).to_be_hidden()
        await expect(message).to_be_focused()
        self.assertFalse(self.errors, self.errors)

    async def test_help_uses_physical_slash_for_non_english_layouts_and_cmd(self):
        await self.login(self.admin)
        dialog = self.page.locator("#shortcut-help")
        for selector, key, modifier in (
            ("#message-input", "\u061f", "ctrlKey"),
            ("#room-search", "\u061f", "metaKey"),
            ("#message-input", "\u05ea", "ctrlKey"),
        ):
            with self.subTest(selector=selector, key=key, modifier=modifier):
                target = self.page.locator(selector)
                await target.focus()
                self.assertTrue(await self.dispatch_key({
                    "key": key, "code": "Slash", modifier: True,
                }))
                await expect(dialog).to_be_visible()
                await self.page.keyboard.press("Escape")
                await expect(dialog).to_be_hidden()
                await expect(target).to_be_focused()

        self.assertTrue(await self.dispatch_key({"key": "/", "ctrlKey": True}))
        await expect(dialog).to_be_visible()
        self.assertTrue(await self.dispatch_key({"key": "/", "ctrlKey": True}))
        await expect(dialog).to_be_hidden()
        self.assertFalse(self.errors, self.errors)

    async def test_help_keeps_composition_modifier_and_other_modal_guards(self):
        await self.login(self.admin)
        dialog = self.page.locator("#shortcut-help")
        for options in (
            {"key": "/", "code": "KeyX", "ctrlKey": True},
            {"key": "/", "code": "Slash"},
            {"key": "/", "code": "Slash", "ctrlKey": True, "altKey": True},
            {"key": "/", "code": "Slash", "ctrlKey": True, "shiftKey": True},
            {"key": "/", "code": "Slash", "ctrlKey": True, "repeat": True},
            {"key": "/", "code": "Slash", "ctrlKey": True, "isComposing": True},
            {"key": "/", "code": "Slash", "ctrlKey": True, "keyCode": 229},
            {"key": "/", "code": "Slash", "ctrlKey": True, "alreadyPrevented": True},
        ):
            with self.subTest(options=options):
                await self.dispatch_key(options)
                await expect(dialog).to_be_hidden()

        await self.page.locator("#room-menu").click()
        room_dialog = self.page.locator("#room-dialog")
        await expect(room_dialog).to_be_visible()
        name = self.page.locator("#room-name-input")
        await name.fill("Keep room settings focused")
        self.assertFalse(await self.dispatch_key({
            "key": "\u061f", "code": "Slash", "ctrlKey": True,
        }))
        await expect(dialog).to_be_hidden()
        await expect(room_dialog).to_be_visible()
        await expect(name).to_be_focused()
        await expect(name).to_have_value("Keep room settings focused")
        await name.press("Escape")

        await self.page.keyboard.press("Control+K")
        await expect(self.page.locator("#quick-switcher")).to_be_visible()
        self.assertFalse(await self.dispatch_key({"key": "/", "code": "Slash", "ctrlKey": True}))
        await expect(dialog).to_be_hidden()
        await self.page.keyboard.press("Escape")

        await self.page.locator("#message-input").press("Control+/")
        initial = await self.page.evaluate("[document.getElementById('model-select').value, document.getElementById('effort-select').value]")
        for options in (
            {"key": "k", "code": "KeyK", "ctrlKey": True},
            {"key": "ArrowDown", "altKey": True, "ctrlKey": True},
            {"key": "ArrowUp", "altKey": True},
        ):
            self.assertFalse(await self.dispatch_key(options))
        await expect(dialog).to_be_visible()
        await expect(self.page.locator("#quick-switcher")).to_be_hidden()
        self.assertEqual(
            await self.page.evaluate("[document.getElementById('model-select').value, document.getElementById('effort-select').value]"),
            initial,
        )
        self.assertFalse(self.errors, self.errors)

    async def test_guide_tracks_enter_setting_and_describes_actual_model_bindings(self):
        await self.login(self.admin)
        message = self.page.locator("#message-input")
        model = self.page.locator("#model-select")
        effort = self.page.locator("#effort-select")
        await message.press("Control+Alt+ArrowDown")
        await expect(model).to_have_value("sol")
        await message.press("Control+Alt+ArrowUp")
        await expect(model).to_have_value("astra")
        await message.press("Alt+ArrowUp")
        await expect(effort).to_have_value("ultra")
        await message.press("Alt+ArrowDown")
        await expect(effort).to_have_value("medium")

        await message.press("Control+/")
        dialog = self.page.locator("#shortcut-help")
        await expect(self.page.locator("#shortcut-send-key")).to_have_text("Enter twice")
        await expect(self.page.locator("#shortcut-send-description")).to_have_text("Send with Double Enter enabled")
        for shortcut in ("Ctrl K", "Cmd K", "Ctrl Enter", "Cmd Enter", "Shift Enter", "Ctrl Alt ↑", "Ctrl Alt ↓", "Alt ↑", "Alt ↓"):
            await expect(dialog.get_by_text(shortcut, exact=True)).to_have_count(1)
        await expect(dialog).to_contain_text("Increase thinking effort")
        await expect(dialog).to_contain_text("Decrease thinking effort")
        await self.page.keyboard.press("Escape")
        await self.page.get_by_role("checkbox", name="Double Enter to send").uncheck()
        await message.press("Control+/")
        await expect(self.page.locator("#shortcut-send-key")).to_have_text("Enter")
        await expect(self.page.locator("#shortcut-send-description")).to_have_text("Send with Double Enter disabled")
        self.assertFalse(self.errors, self.errors)

    async def test_help_closes_model_picker_and_restores_its_durable_trigger(self):
        await self.login(self.admin)
        trigger = self.page.locator(".model-picker-trigger").first
        await trigger.click()
        picker = self.page.locator(".model-picker-menu")
        await expect(picker).to_be_visible()
        await self.page.keyboard.press("Control+/")
        await expect(self.page.locator("#shortcut-help")).to_be_visible()
        await expect(picker).to_have_count(0)
        await self.page.keyboard.press("Escape")
        await expect(self.page.locator("#shortcut-help")).to_be_hidden()
        await expect(trigger).to_be_focused()
        self.assertFalse(self.errors, self.errors)

    async def test_queued_close_does_not_steal_focus_from_reopened_help(self):
        await self.login(self.admin)
        await self.page.locator("#message-input").focus()
        result = await self.page.evaluate(
            """async () => {
                openShortcutHelp();
                const dialog = document.getElementById('shortcut-help');
                const closed = new Promise(resolve => dialog.addEventListener('close', resolve, {once: true}));
                dialog.close();
                openShortcutHelp();
                await closed;
                return {open: dialog.open, focus: document.activeElement.id};
            }"""
        )
        self.assertEqual(result, {"open": True, "focus": "shortcut-help-close"})
        await self.page.keyboard.press("Escape")
        await expect(self.page.locator("#message-input")).to_be_focused()
        self.assertFalse(self.errors, self.errors)

    async def test_queued_close_preserves_a_new_focus_target(self):
        await self.login(self.admin)
        for dialog_id in ("shortcut-help", "quick-switcher"):
            with self.subTest(dialog=dialog_id):
                await self.page.locator("#message-input").focus()
                result = await self.page.evaluate(
                    """async dialogId => {
                        if (dialogId === 'shortcut-help') openShortcutHelp();
                        else openQuickSwitcher();
                        const dialog = document.getElementById(dialogId);
                        const closed = new Promise(resolve => dialog.addEventListener('close', resolve, {once: true}));
                        dialog.close();
                        document.getElementById('room-search').focus();
                        const beforeCloseEvent = document.activeElement.id;
                        await closed;
                        return {beforeCloseEvent, afterCloseEvent: document.activeElement.id};
                    }""",
                    dialog_id,
                )
                self.assertEqual(result, {
                    "beforeCloseEvent": "room-search",
                    "afterCloseEvent": "room-search",
                })
        self.assertFalse(self.errors, self.errors)

    async def test_queued_close_restores_focus_when_it_falls_to_the_body(self):
        await self.login(self.admin)
        for dialog_id in ("shortcut-help", "quick-switcher"):
            with self.subTest(dialog=dialog_id):
                await self.page.locator("#message-input").focus()
                result = await self.page.evaluate(
                    """async dialogId => {
                        if (dialogId === 'shortcut-help') openShortcutHelp();
                        else openQuickSwitcher();
                        const dialog = document.getElementById(dialogId);
                        const closed = new Promise(resolve => dialog.addEventListener('close', resolve, {once: true}));
                        dialog.close();
                        document.activeElement.blur();
                        const bodyBeforeCloseEvent = document.activeElement === document.body;
                        await closed;
                        return {bodyBeforeCloseEvent, focus: document.activeElement.id};
                    }""",
                    dialog_id,
                )
                self.assertEqual(result, {
                    "bodyBeforeCloseEvent": True,
                    "focus": "message-input",
                })
        self.assertFalse(self.errors, self.errors)

    async def test_guide_fits_narrow_viewports_and_all_themes_with_reduced_motion(self):
        await self.login(self.admin)
        await self.page.emulate_media(reduced_motion="reduce")
        await self.page.locator("#message-input").press("Control+/")
        for width in (360, 640):
            await self.page.set_viewport_size({"width": width, "height": 600})
            for theme in await self.page.evaluate("themes"):
                with self.subTest(width=width, theme=theme):
                    await self.page.evaluate("theme => setTheme(theme)", theme)
                    geometry = await self.page.locator("#shortcut-help").evaluate(
                        """dialog => {
                            const rect = dialog.getBoundingClientRect();
                            const style = getComputedStyle(dialog);
                            return {
                                fits: rect.left >= 0 && rect.right <= innerWidth
                                    && rect.top >= 0 && rect.bottom <= innerHeight,
                                noHorizontalOverflow: dialog.scrollWidth <= dialog.clientWidth + 1,
                                scrollable: style.overflowY === 'auto',
                                noAnimation: style.animationName === 'none',
                                themed: style.color === getComputedStyle(document.getElementById('shortcut-help-title')).color,
                            };
                        }"""
                    )
                    self.assertTrue(all(geometry.values()), geometry)
                    await expect(self.page.locator("#shortcut-help-close")).to_be_visible()
        self.assertFalse(self.errors, self.errors)


if __name__ == "__main__":
    unittest.main()
