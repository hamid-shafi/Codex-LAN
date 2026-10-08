"""CODEX: Verify selectable Enter behavior with real Chromium and fake workers."""

from __future__ import annotations

import unittest
from pathlib import Path

from playwright.async_api import async_playwright, expect

import test_lan_browser as browser_fixture


class LanEnterModeTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = browser_fixture.LanBrowserTests.asyncSetUp
    asyncTearDown = browser_fixture.LanBrowserTests.asyncTearDown
    start_worker = browser_fixture.LanBrowserTests.start_worker
    login = browser_fixture.LanBrowserTests.login
    wait_worker_turn = browser_fixture.LanBrowserTests.wait_worker_turn

    async def start_chat(self):
        await self.login(self.admin)
        await expect(self.page.locator("#room-title")).to_have_text("Working together")
        return self.page.locator("#message-input")

    def submitted_text(self):
        calls = [call for call in self.worker.calls if call["method"] == "turn/start"]
        self.assertEqual(len(calls), 1)
        return "\n".join(item["text"] for item in calls[0]["params"]["input"] if item.get("type") == "text")

    async def test_double_enter_is_default_and_does_not_send_first_press(self):
        input = await self.start_chat()
        await expect(self.page.get_by_role("checkbox", name="Double Enter to send")).to_be_checked()
        await input.fill("Keep accidental Enter safe")
        await input.press("Enter")
        await expect(input).to_have_value("Keep accidental Enter safe\n")
        self.assertIsNone(self.worker.turn)
        await input.press("Enter")
        await self.wait_worker_turn()
        self.assertEqual(self.submitted_text(), "Keep accidental Enter safe")
        self.assertFalse(self.errors, self.errors)

    async def test_single_enter_mode_still_allows_shift_enter_newline(self):
        input = await self.start_chat()
        await self.page.get_by_role("checkbox", name="Double Enter to send").uncheck()
        await expect(self.page.locator("#composer-hint")).to_have_text("Enter to send · Shift+Enter for a new line")
        await input.fill("First line")
        await input.press("Shift+Enter")
        await expect(input).to_have_value("First line\n")
        self.assertIsNone(self.worker.turn)
        await input.press_sequentially("Second line")
        await input.press("Enter")
        await self.wait_worker_turn()
        self.assertEqual(self.submitted_text(), "First line\nSecond line")
        self.assertFalse(self.errors, self.errors)

    async def test_preference_survives_reload_and_is_per_device(self):
        await self.start_chat()
        checkbox = self.page.get_by_role("checkbox", name="Double Enter to send")
        await checkbox.uncheck()
        await self.page.reload()
        await expect(self.page.locator("#room-title")).to_have_text("Working together")
        await expect(checkbox).not_to_be_checked()
        await self.login(self.client)
        await expect(self.page.locator("#room-title")).to_have_text("Client-owned room")
        await expect(checkbox).to_be_checked()
        await self.login(self.admin)
        await expect(self.page.locator("#room-title")).to_have_text("Working together")
        await expect(checkbox).not_to_be_checked()
        await checkbox.check()
        await self.page.reload()
        await expect(self.page.locator("#room-title")).to_have_text("Working together")
        await expect(checkbox).to_be_checked()
        self.assertFalse(self.errors, self.errors)

    async def test_ime_and_repeat_do_not_send_ctrl_enter_still_does(self):
        input = await self.start_chat()
        await self.page.get_by_role("checkbox", name="Double Enter to send").uncheck()
        await input.fill("Composing \u0641\u0627\u0631\u0633\u06cc")
        await self.page.evaluate("""() => {
          const input=document.getElementById('message-input');
          for (const properties of [{isComposing:true}, {keyCode:229}, {repeat:true}]) {
            input.dispatchEvent(new KeyboardEvent('keydown', {key:'Enter', bubbles:true, cancelable:true, ...properties}));
          }
        }""")
        self.assertIsNone(self.worker.turn)
        await expect(input).to_have_value("Composing \u0641\u0627\u0631\u0633\u06cc")
        await input.press("Control+Enter")
        await self.wait_worker_turn()
        self.assertEqual(self.submitted_text(), "Composing \u0641\u0627\u0631\u0633\u06cc")
        self.assertFalse(self.errors, self.errors)

    async def test_mode_room_and_edit_changes_clear_pending_enter(self):
        input = await self.start_chat()
        await input.fill("Draft")
        await input.press("Enter")
        self.assertTrue(await self.page.evaluate("state.lastEnter !== null"))
        await self.page.get_by_role("checkbox", name="Double Enter to send").uncheck()
        self.assertTrue(await self.page.evaluate("state.lastEnter === null"))
        await self.page.get_by_role("checkbox", name="Double Enter to send").check()
        await input.focus()
        await input.press("Enter")
        self.assertIsNone(self.worker.turn)
        await expect(input).to_have_value("Draft\n\n")
        await input.press("ArrowLeft")
        self.assertTrue(await self.page.evaluate("state.lastEnter === null"))
        await input.press("Enter")
        self.assertTrue(await self.page.evaluate("state.lastEnter !== null"))
        await self.page.evaluate("id => selectRoom(id)", self.foreign["id"])
        self.assertTrue(await self.page.evaluate("state.lastEnter === null"))
        self.assertIsNone(self.worker.turn)
        self.assertFalse(self.errors, self.errors)

    async def test_command_enter_bypasses_double_mode(self):
        input = await self.start_chat()
        await input.fill("Command key submit")
        await input.press("Meta+Enter")
        await self.wait_worker_turn()
        self.assertEqual(self.submitted_text(), "Command key submit")
        self.assertFalse(self.errors, self.errors)


class LanComposerPerformanceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(
            executable_path="/usr/bin/google-chrome",
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        self.page = await self.browser.new_page()
        self.errors = []
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        root = Path(__file__).resolve().parents[2] / "apps/workspace"
        html = (root / "codex_lan" / "web" / "index.html").read_text(encoding="utf-8")
        html = html.replace('<script src="/app.js" defer></script>', "")
        await self.page.set_content(html)
        await self.page.add_script_tag(path=str(root / "codex_lan" / "web" / "app.js"))
        await self.page.wait_for_timeout(20)
        await self.page.evaluate("""() => {
          cancelTimers();
          state.me = {id: 'perf-device', role: 'admin', status: 'approved', name: 'Performance'};
          state.connected = true;
          state.workerOnline = true;
          state.view = 'chat';
          state.models = [{
            id: 'astra', model: 'astra', displayName: 'Astra', isDefault: true,
            defaultReasoningEffort: 'medium',
            supportedReasoningEfforts: [{reasoningEffort: 'medium'}],
          }];
          state.rooms = [{
            id: 'perf-room', owner_id: 'perf-device', title: 'Performance',
            model: 'astra', effort: 'medium', speed: 'standard',
          }];
          state.selected = 'perf-room';
          document.getElementById('workspace').hidden = false;
          renderModelControls();
          updateComposer();
        }""")

    async def asyncTearDown(self):
        await self.browser.close()
        await self.playwright.stop()

    async def test_large_draft_input_skips_serialization_and_coalesces_resize(self):
        result = await self.page.evaluate("""async () => {
          await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
          const input = document.getElementById('message-input');
          const originalStringify = JSON.stringify;
          const originalResize = window.resizeComposer;
          let draftSerializations = 0;
          let resizeCalls = 0;
          JSON.stringify = function(value, ...args) {
            if (value && typeof value === 'object' && typeof value.text === 'string' && value.text.length >= 100000) draftSerializations += 1;
            return originalStringify.call(this, value, ...args);
          };
          window.resizeComposer = function() { resizeCalls += 1; return originalResize(); };
          try {
            const base = 'A'.repeat(180000);
            const started = performance.now();
            for (let index = 0; index < 40; index += 1) {
              input.value = base + 'x'.repeat(index);
              input.dispatchEvent(new InputEvent('input', {bubbles: true, inputType: 'insertText', data: 'x'}));
            }
            const dispatchMs = performance.now() - started;
            await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
            return {
              dispatchMs,
              draftSerializations,
              resizeCalls,
              draftLength: roomState().draft.length,
              inputLength: input.value.length,
              sendDisabled: document.getElementById('send-button').disabled,
            };
          } finally {
            JSON.stringify = originalStringify;
            window.resizeComposer = originalResize;
          }
        }""")
        self.assertEqual(result["draftSerializations"], 0, result)
        self.assertEqual(result["resizeCalls"], 1, result)
        self.assertEqual(result["draftLength"], result["inputLength"], result)
        self.assertFalse(result["sendDisabled"], result)
        self.assertLess(result["dispatchMs"], 1000, result)
        self.assertFalse(self.errors, self.errors)


if __name__ == "__main__":
    unittest.main()
