"""CODEX: Real Chromium checks against a temporary server and fake local workers."""

from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer
from playwright.async_api import async_playwright, expect

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from codex_lan.server import SERVICE, create_app
from test_lan_server import FakeAuth, FakeWorker, MODELS, PNG, host_worker_socket


BROWSER_MODELS = [*MODELS, {
    "id": "sol", "model": "sol", "isDefault": False,
    "defaultReasoningEffort": "medium",
    "supportedReasoningEfforts": [{"reasoningEffort": value} for value in ("low", "medium", "high")],
}]

WEB = Path(__file__).resolve().parents[1] / "codex_lan" / "web"


class LanBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = create_app(Path(self.temp.name), auth=FakeAuth())
        self.service = self.app[SERVICE]
        self.http = TestClient(TestServer(self.app))
        await self.http.start_server()
        self.base = str(self.http.make_url("/"))
        self.admin = self.service.store.bootstrap_admin("Server .56")
        self.client = self.service.store.enroll("Client .57")
        self.pending = self.service.store.enroll("Pending laptop")
        self.admin_id = self.admin["device"]["id"]
        self.client_id = self.client["device"]["id"]
        self.service.store.set_device(self.admin_id, self.client_id, status="approved")
        self.room = self.service.store.create_room(self.admin_id, "Working together", "/home/server/project", "astra", "medium")
        self.foreign = self.service.store.create_room(self.client_id, "Client-owned room", "/home/client/project", "astra", "medium")
        run = self.service.store.create_run(self.admin_id, self.room["id"], "Help plan this project", [], "seed")
        self.answer = self.service.store.finish_run(run["run"]["id"], "completed", "The plan starts here. \u0627\u06cc\u0646 \u06cc\u06a9 \u067e\u0627\u0631\u0627\u06af\u0631\u0627\u0641 \u0641\u0627\u0631\u0633\u06cc \u0627\u0633\u062a.\n\nA purely English paragraph stays left aligned.\n\n## Practical steps\n\n- Keep the files together\n- Verify the result\n\n| Part | Notes |\n| --- | --- |\n| Browser | Persian \u0641\u0627\u0631\u0633\u06cc text |\n\n```python\nprint('Native selection works')\n" + "x" * 160 + "\n```\n\n<img src=x onerror=alert(1)> is displayed as text.")
        self.service.store.record_usage(run["run"]["id"], 200, 100, 50, 300)
        self.workers = []
        client_worker = await self.start_worker(self.client, "client")
        self.worker = await self.start_worker(self.admin, "server")
        self.workers[:] = [self.worker, client_worker]
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(executable_path="/usr/bin/google-chrome", headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        self.context = await self.browser.new_context(viewport={"width": 1365, "height": 900})
        self.page = await self.context.new_page()
        self.errors = []
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))

    async def asyncTearDown(self):
        await self.browser.close()
        await self.playwright.stop()
        for worker in self.workers:
            await worker.close()
        await self.http.close()
        self.temp.cleanup()

    async def start_worker(self, device, name):
        ws, _auth = await host_worker_socket(self.http, self.service, self.admin, device)
        await ws.send_json({"type": "ready", "models": BROWSER_MODELS, "cwd": "/home/" + name})
        worker = FakeWorker(ws, name)
        self.workers.append(worker)
        for _ in range(100):
            if self.service.runtime.connected(device["device"]["id"]):
                break
            await asyncio.sleep(.01)
        return worker

    async def login(self, device, page=None):
        page = page or self.page
        await page.goto(self.base + "#login=" + device["token"])
        await page.wait_for_function("() => location.hash === ''")
        await page.wait_for_function("""() => state.me && (
          ['approved', 'active'].includes(state.me.status)
            ? state.routeReady && !document.getElementById('workspace').hidden
            : !document.getElementById('gate').hidden
        )""")

    async def wait_worker_turn(self):
        for _ in range(300):
            if self.worker.turn:
                return
            await asyncio.sleep(.01)
        self.fail("Fake worker did not receive a turn")

    async def open_account_menu(self, page=None):
        page = page or self.page
        await page.wait_for_function("""() => {
          const expanded = mobileSidebar()
            ? document.getElementById('workspace').classList.contains('sidebar-open')
            : !state.sidebarCollapsed;
          const sidebar = document.getElementById('sidebar');
          return document.getElementById('sidebar-toggle').getAttribute('aria-expanded') === String(expanded)
            && sidebar.inert === !expanded
            && sidebar.getAttribute('aria-hidden') === String(!expanded);
        }""")
        if await page.locator("#sidebar-toggle").get_attribute("aria-expanded") == "false":
            await page.locator("#sidebar-toggle").click()
        menu = page.locator("#account-menu")
        if not await menu.is_visible():
            await page.locator("#account-menu-open").click()
        await expect(menu).to_be_visible()

    async def open_admin_device(self, *, name=None, device_id=None):
        selector = f'.admin-device-summary[data-device-id="{device_id}"]' if device_id else ".admin-device-summary"
        summary = self.page.locator(selector)
        if name:
            summary = summary.filter(has_text=name)
        await expect(summary).to_have_count(1)
        await summary.locator(".admin-device-menu").click()
        dialog = self.page.locator("#admin-device-dialog")
        await expect(dialog).to_be_visible()
        card = dialog.locator(f'.admin-device[data-device-id="{device_id}"]') if device_id else dialog.locator(".admin-device")
        await expect(card).to_have_count(1)
        return card

    async def test_primary_ui_streams_selection_images_and_layout(self):
        page = self.page
        await self.login(self.admin)
        await expect(page.locator("#workspace")).to_be_visible()
        await expect(page.locator("#room-title")).to_have_text("Working together")
        await page.locator("#theme-select").select_option("light")
        await expect(page.locator("#effort-select option[value='ultra']")).to_have_count(1)
        mixed = page.locator(".message.assistant .message-content p").first
        await expect(mixed).to_have_attribute("dir", "rtl")
        await expect(page.locator(".message.assistant .message-content p").nth(1)).to_have_attribute("dir", "ltr")
        await expect(page.locator(".message-content img")).to_have_count(0)
        self.assertEqual(await page.locator(".markdown-table").count(), 1)
        await page.evaluate("""() => {
          const p = document.querySelector('.message.assistant .message-content p');
          const range = document.createRange(); range.selectNodeContents(p);
          const selection = window.getSelection(); selection.removeAllRanges(); selection.addRange(range);
          window.beforeSelection = selection.toString(); renderCurrent();
        }""")
        self.assertTrue(await page.evaluate("window.getSelection().toString() === window.beforeSelection"))
        await page.locator(".message.assistant").get_by_role("button", name="Reply", exact=True).click()
        await expect(page.locator("#reply-preview")).to_be_visible()
        await expect(page.locator("#reply-text")).to_have_attribute("dir", "rtl")
        await page.locator("#reply-clear").click()
        await page.locator("#effort-select").select_option("ultra")
        await page.wait_for_function("() => selectedRoom().effort === 'ultra'")
        for theme in ("light", "dark", "midnight"):
            await page.locator("#theme-select").select_option(theme)
            await expect(page.locator("html")).to_have_attribute("data-theme", theme)
            await page.locator("#timeline-scroll").evaluate("element => {element.scrollTop=0;}")
            await page.screenshot(path=f"/tmp/codex-lan-v2-{theme}.png", full_page=True)
        await page.reload()
        await expect(page.locator("#workspace")).to_be_visible()
        await expect(page.locator("#theme-select")).to_have_value("midnight")
        await expect(page.locator("html")).to_have_attribute("data-theme", "midnight")
        await page.locator("#theme-select").select_option("light")
        for width in (400, 768, 1440):
            await page.set_viewport_size({"width": width, "height": 900})
            if width == 400:
                await expect(page.locator("#sidebar")).to_have_css("transform", "matrix(1, 0, 0, 1, -270, 0)")
            self.assertTrue(await page.evaluate("document.documentElement.scrollWidth <= innerWidth"), f"Document overflows at {width}px")
            self.assertTrue(await page.evaluate("[...document.querySelectorAll('pre, .message-content')].every(e => e.scrollWidth <= e.clientWidth + 1)"), f"Message overflows at {width}px")
            await expect(page.locator("#composer #model-select")).to_be_visible()
            await expect(page.locator("#composer #effort-select")).to_be_visible()
            metrics = await page.evaluate("""() => {
              const read = selector => { const e=document.querySelector(selector), r=e.getBoundingClientRect(), s=getComputedStyle(e); return {height:r.height,font:parseFloat(s.fontSize),left:r.left,right:r.right}; };
              return {message:read('.message-content'),input:read('#message-input'),composer:read('#composer'),model:read('#model-select'),effort:read('#effort-select'),topbar:read('.topbar'),send:read('#send-button')};
            }""")
            self.assertGreaterEqual(metrics["message"]["font"], 15, metrics)
            self.assertGreaterEqual(metrics["input"]["font"], 15, metrics)
            self.assertGreaterEqual(metrics["input"]["height"], 68, metrics)
            self.assertLessEqual(metrics["composer"]["height"], 175, metrics)
            self.assertGreaterEqual(metrics["send"]["height"], 36, metrics)
            for key in ("model", "effort"):
                self.assertGreaterEqual(metrics[key]["font"], 13, metrics)
                self.assertGreaterEqual(metrics[key]["height"], 34, metrics)
                self.assertGreaterEqual(metrics[key]["left"], 0, metrics)
                self.assertLessEqual(metrics[key]["right"], width, metrics)
            self.assertLessEqual(metrics["topbar"]["height"], 110, metrics)
            await page.screenshot(path=f"/tmp/codex-lan-v2-{width}.png", full_page=True)
        await page.set_viewport_size({"width": 1365, "height": 900})
        await page.locator("#new-room").click()
        await expect(page.locator("#room-dialog")).to_be_hidden()
        await expect(page.locator("#room-title")).to_have_text("New chat")
        await page.locator("#room-menu").click()
        await page.locator("#room-name-input").fill("A fresh room")
        await expect(page.locator("#room-cwd-input")).to_have_value("/home/server")
        await page.locator("#room-cwd-input").fill("/home/server/fresh-project")
        await page.locator("#room-save").click()
        await expect(page.locator("#room-title")).to_have_text("A fresh room")
        self.assertEqual(self.service.store.get_room(self.admin_id, await page.evaluate("state.selected"))["cwd"],
                         "/home/server/fresh-project")
        await page.locator("#attachment-input").set_input_files({"name": "image.png", "mimeType": "image/png", "buffer": PNG})
        await expect(page.locator(".attachment-card small")).to_have_text("Ready")
        await page.evaluate("""bytes => {
          const data = new DataTransfer();
          data.items.add(new File([new Uint8Array(bytes)], 'pasted.png', {type:'image/png'}));
          document.getElementById('message-input').dispatchEvent(new ClipboardEvent('paste', {clipboardData:data, bubbles:true}));
        }""", list(PNG))
        await expect(page.locator(".attachment-card")).to_have_count(2)
        await expect(page.locator(".attachment-card small").nth(1)).to_have_text("Ready")
        await page.locator("#message-input").fill("A real fake-worker request")
        await page.locator("#message-input").press("Enter")
        await page.locator("#message-input").press("Enter")
        await self.wait_worker_turn()
        await expect(page.locator(".message.user")).to_have_count(1)
        await expect(page.locator(".message.user img")).to_have_count(2)
        await self.worker.delta("Hello ")
        await expect(page.locator(".message.assistant .message-content")).to_have_text("Hello ")
        await self.context.set_offline(True)
        await self.worker.delta("from the durable stream.")
        await asyncio.sleep(.15)
        await self.context.set_offline(False)
        await expect(page.locator(".message.assistant .message-content")).to_have_text("Hello from the durable stream.", timeout=10000)
        await self.worker.usage(10, 5)
        await self.worker.complete()
        await expect(page.locator("#stop-button")).to_be_hidden(timeout=10000)
        await expect(page.locator(".message.assistant")).to_have_count(1)
        await expect(page.locator(".message.assistant .message-content")).to_have_text("Hello from the durable stream.")
        await page.reload()
        await expect(page.locator("#workspace")).to_be_visible()
        await expect(page.locator(".message.assistant")).to_have_count(1)
        self.assertFalse(self.errors, self.errors)

    async def test_stream_preserves_reading_position_and_other_room_controls(self):
        page = self.page
        idle_room = self.service.store.create_room(self.admin_id, "Independent controls", "/home/server/second", "astra", "medium")
        history = self.service.store.create_run(self.admin_id, self.room["id"], "A long history", [], "long-history")
        self.service.store.finish_run(history["run"]["id"], "completed", "\n\n".join(
            f"Paragraph {number}: \u0645\u062a\u0646 \u0641\u0627\u0631\u0633\u06cc \u0628\u0631\u0627\u06cc \u0628\u0631\u0631\u0633\u06cc \u0627\u0646\u062a\u062e\u0627\u0628 \u0637\u0628\u06cc\u0639\u06cc\u060c \u0647\u0645\u0631\u0627\u0647 English technical text."
            for number in range(32)
        ))
        await self.login(self.admin)
        await expect(page.locator("#room-title")).to_have_text("Working together")
        await page.locator("#message-input").fill("Continue this room")
        await page.locator("#message-input").press("Enter")
        await page.locator("#message-input").press("Enter")
        await self.wait_worker_turn()
        await self.worker.delta("A streaming answer")
        await expect(page.locator(".message.assistant .message-content").last).to_have_text("A streaming answer")
        await page.locator("#timeline-scroll").evaluate("element => {element.scrollTop = 30; element.dispatchEvent(new Event('scroll'));}")
        await page.wait_for_function("() => state.follow === false")
        reading_top = await page.locator("#timeline-scroll").evaluate("element => element.scrollTop")
        await page.evaluate("""() => {
          const p = document.querySelector('.message.assistant .message-content p');
          const range = document.createRange(); range.selectNodeContents(p);
          const selection = window.getSelection(); selection.removeAllRanges(); selection.addRange(range);
          window.readingSelection = selection.toString();
        }""")
        await self.worker.delta(" arrives without moving the reader.")
        await expect(page.locator(".message.assistant .message-content").last).to_have_text("A streaming answer arrives without moving the reader.")
        self.assertLessEqual(abs(await page.locator("#timeline-scroll").evaluate("element => element.scrollTop") - reading_top), 2)
        self.assertTrue(await page.evaluate("window.getSelection().toString() === window.readingSelection"))
        await page.evaluate("window.getSelection().removeAllRanges()")
        await expect(page.locator("#model-select")).to_be_disabled()
        await expect(page.locator("#effort-select")).to_be_disabled()
        await page.keyboard.press("Alt+ArrowDown")
        await page.keyboard.press("Control+Alt+ArrowDown")
        self.assertEqual(self.service.store.get_room(self.admin_id, self.room["id"])["model"], "astra")
        self.assertEqual(self.service.store.get_room(self.admin_id, self.room["id"])["effort"], "medium")
        await page.locator("#room-list .room-item").filter(has_text="Independent controls").click()
        await expect(page.locator("#room-title")).to_have_text("Independent controls")
        await expect(page.locator("#model-select")).to_be_enabled()
        await expect(page.locator("#effort-select")).to_be_enabled()
        await page.keyboard.press("Alt+ArrowUp")
        await page.wait_for_function("() => selectedRoom().effort === 'ultra'")
        self.assertEqual(self.service.store.get_room(self.admin_id, idle_room["id"])["effort"], "ultra")
        await page.keyboard.press("Alt+ArrowUp")
        await expect(page.locator("#effort-select")).to_have_value("ultra")
        await page.keyboard.press("Alt+ArrowDown")
        await page.wait_for_function("() => selectedRoom().effort === 'medium'")
        await page.keyboard.press("Control+Alt+ArrowDown")
        await page.wait_for_function("() => selectedRoom().model === 'sol'")
        await expect(page.locator("#model-select")).to_have_value("sol")
        remaining_models = await page.locator("#model-select").evaluate("""select => {
            const values = [...select.options].filter(option => !option.disabled && option.value).map(option => option.value);
            return values.slice(values.indexOf('sol') + 1).concat(values.slice(0, values.indexOf('sol')));
        }""")
        for model in remaining_models:
            await page.keyboard.press("Control+Alt+ArrowDown")
            await page.wait_for_function("model => selectedRoom().model === model", arg=model)
            if model == "astra":
                break
        await expect(page.locator("#model-select")).to_have_value("astra")
        await self.worker.delta(" Background output stays in its own room.")
        await expect(page.locator(".message.assistant")).to_have_count(0)
        await page.locator("#room-list .room-item").filter(has_text="Working together").click()
        await expect(page.locator(".message.assistant .message-content").last).to_contain_text("Background output stays in its own room.")
        await expect(page.locator("#effort-select")).to_be_disabled()
        await self.worker.complete()
        await expect(page.locator("#effort-select")).to_be_enabled(timeout=10000)
        self.assertFalse(self.errors, self.errors)

    async def test_admin_approval_pending_gate_and_read_only_rooms(self):
        page = self.page
        foreign_run = self.service.store.create_run(self.client_id, self.foreign["id"], "Private client request", [], "foreign-failure")
        self.service.store.finish_run(foreign_run["run"]["id"], "failed", "", "Client worker stopped.")
        pending_context = await self.browser.new_context()
        pending_page = await pending_context.new_page()
        await self.login(self.pending, pending_page)
        await expect(pending_page.locator("#gate-title")).to_have_text("Waiting for approval")
        await expect(pending_page.locator("#gate-status")).to_contain_text(self.pending["device"]["verification_code"])
        await expect(pending_page.locator("#workspace")).to_be_hidden()
        await self.login(self.admin)
        await expect(page.locator("#workspace")).to_be_visible()
        await page.locator("#room-list .room-item").filter(has_text="Client-owned room").click()
        await expect(page.locator("#inspector-notice")).to_be_visible()
        await expect(page.locator("#inspector-notice")).to_contain_text("astra")
        await expect(page.locator("#inspector-notice")).to_contain_text("Medium")
        await expect(page.locator("#composer-area")).to_be_hidden()
        await expect(page.locator(".message.assistant .failure-title")).to_have_text("Response failed")
        await expect(page.locator(".message.assistant .retry-button")).to_be_hidden()
        await self.open_account_menu(page)
        await page.locator("#admin-open").click()
        await expect(page.locator("#admin-view")).to_be_visible()
        await expect(page.locator(".admin-device-summary")).to_have_count(3)
        await expect(page.locator("#admin-devices .admin-device")).to_have_count(0)
        pending_card = await self.open_admin_device(name="Pending laptop")
        await pending_card.get_by_role("button", name="Approve", exact=True).click()
        await expect(pending_page.locator("#workspace")).to_be_visible(timeout=10000)
        await expect(pending_page.locator("#room-list .room-item")).to_have_count(0)
        await expect(page.locator("#usage-summary")).to_contain_text("Estimated usage units")
        await page.locator("#admin-device-close").click()
        client_card = await self.open_admin_device(name="Client .57")
        await client_card.locator("input[type=number]").first.fill("2")
        await client_card.locator("summary").click()
        await client_card.get_by_label("Token budget", exact=True).fill("10000")
        await client_card.get_by_role("button", name="Save limits").click()
        for _ in range(100):
            if self.service.store.get_device(self.client_id)["max_concurrent"] == 2:
                break
            await asyncio.sleep(.02)
        self.assertEqual(self.service.store.get_device(self.client_id)["token_budget"], 10000)
        await page.screenshot(path="/tmp/codex-lan-admin.png", full_page=True)
        await pending_context.close()
        self.assertFalse(self.errors, self.errors)

    async def test_admin_device_deletion_confirms_impact_handles_conflict_and_refreshes_lists(self):
        page = self.page
        delete_attempts = 0
        deleted = False

        async def delete_device(route):
            nonlocal delete_attempts, deleted
            delete_attempts += 1
            if delete_attempts == 1:
                await route.fulfill(status=409, content_type="application/json", body='{"error":"Device has 2 active responses."}')
                return
            deleted = True
            await route.fulfill(status=200, content_type="application/json", body='{"deleted":true}')

        async def device_list(route):
            response = await route.fetch()
            payload = await response.json()
            if deleted:
                payload["devices"] = [device for device in payload.get("devices", []) if device["id"] != self.client_id]
            await route.fulfill(response=response, json=payload)

        async def room_list(route):
            response = await route.fetch()
            payload = await response.json()
            if deleted:
                payload["rooms"] = [room for room in payload.get("rooms", []) if room["owner_id"] != self.client_id]
            await route.fulfill(response=response, json=payload)

        await page.route(f"**/api/admin/devices/{self.client_id}", delete_device)
        await page.route("**/api/admin/devices", device_list)
        await page.route("**/api/rooms", room_list)
        confirmations = []

        async def accept_delete(dialog):
            confirmations.append(dialog.message)
            await dialog.accept()

        page.on("dialog", accept_delete)
        await self.login(self.admin)
        await self.open_account_menu(page)
        await page.locator("#admin-open").click()
        admin_card = await self.open_admin_device(name="Server .56")
        await expect(admin_card.get_by_role("button", name="Delete Server .56 and its data permanently")).to_be_hidden()
        await page.locator("#admin-device-close").click()
        client_card = await self.open_admin_device(name="Client .57")
        delete_button = client_card.get_by_role("button", name="Delete Client .57 and its data permanently")
        await expect(delete_button).to_be_visible()

        await delete_button.click()
        await expect(page.locator("#toast-message")).to_contain_text("Device has 2 active responses")
        await expect(page.locator("#toast-message")).to_contain_text("Stop this device’s active responses")
        await expect(delete_button).to_be_enabled()
        self.assertIn("Permanently delete “Client .57”?", confirmations[0])
        self.assertIn("rooms, messages, uploaded files, usage history, and settings", confirmations[0])
        self.assertIn("cannot be undone", confirmations[0])

        await delete_button.click()
        await expect(page.locator("#admin-device-dialog")).to_be_hidden()
        await expect(page.locator(f'.admin-device-summary[data-device-id="{self.client_id}"]')).to_have_count(0)
        await expect(page.locator("#room-list .room-item").filter(has_text="Client-owned room")).to_have_count(0)
        await expect(page.locator("#toast-message")).to_contain_text("Client .57 and its owned data were permanently deleted")
        self.assertEqual(delete_attempts, 2)
        self.assertEqual(len(confirmations), 2)
        self.assertFalse(self.errors, self.errors)

    async def test_url_restores_admin_usage_connector_room_settings_and_device_profile(self):
        page = self.page
        await self.login(self.admin)

        await self.open_account_menu(page)
        await page.locator("#admin-open").click()
        await page.wait_for_function("() => new URL(location.href).searchParams.get('view') === 'admin'")
        await page.reload()
        await expect(page.locator("#admin-view")).to_be_visible()

        await self.open_admin_device(device_id=self.client_id)
        await page.wait_for_function("id => { const u = new URL(location.href); return u.searchParams.get('panel') === 'device' && u.searchParams.get('device') === id; }", arg=self.client_id)
        await page.reload()
        await expect(page.locator("#admin-device-dialog")).to_be_visible()
        await expect(page.locator("#admin-device-title")).to_have_text("Client .57")
        await page.locator("#admin-device-close").click()

        await self.open_account_menu(page)
        await page.locator("#my-usage-open").click()
        await page.wait_for_function("() => new URL(location.href).searchParams.get('panel') === 'usage'")
        await page.reload()
        await expect(page.locator("#my-usage-dialog")).to_be_visible()
        await page.locator("#my-usage-close").click()

        await page.get_by_role("button", name="Local connector", exact=True).click()
        await page.wait_for_function("() => new URL(location.href).searchParams.get('panel') === 'connector'")
        await page.reload()
        await expect(page.locator("#connector-dialog")).to_be_visible()
        await page.locator("#connector-close").click()

        await page.locator("#room-list .room-item").filter(has_text="Working together").click()
        await page.locator("#room-menu").click()
        await page.wait_for_function("id => { const u = new URL(location.href); return u.searchParams.get('room') === id && u.searchParams.get('panel') === 'room-settings'; }", arg=self.room["id"])
        await page.reload()
        await expect(page.locator("#room-dialog")).to_be_visible()
        await expect(page.locator("#room-name-input")).to_have_value("Working together")
        self.assertFalse(self.errors, self.errors)

    async def test_failed_answer_retries_only_into_reviewable_draft(self):
        page = self.page
        store = self.service.store
        failed_room = store.create_room(self.admin_id, "Failed attempt", "/home/server/project", "astra", "ultra")
        image_path = self.service.upload_dir / "failure-fixture.png"
        image_path.write_bytes(PNG)
        attachment = store.add_attachment(self.admin_id, "Original screenshot.png", "image/png", len(PNG), str(image_path))
        prompt = "\u0644\u0637\u0641\u0627\u064b \u0627\u06cc\u0646 \u062a\u0635\u0648\u06cc\u0631 \u0631\u0627 \u0628\u0631\u0631\u0633\u06cc \u06a9\u0646.\nKeep the existing project files intact."
        failed = store.create_run(self.admin_id, failed_room["id"], prompt, [attachment["id"]], "failed-before-turn")
        store.update_run(failed["run"]["id"], thread_id="old-thread-resume")
        answer = store.finish_run(failed["run"]["id"], "failed", "", "Codex app-server disconnected.")
        with store.db:
            store.db.execute("UPDATE messages SET duration_seconds=1 WHERE id=?", (answer["id"],))
        await self.login(self.admin)
        await expect(page.locator("#room-title")).to_have_text("Failed attempt")
        await page.locator("#theme-select").select_option("dark")
        user = page.locator(".message.user")
        assistant = page.locator(".message.assistant")
        await expect(user).to_have_count(1)
        await expect(assistant).to_have_count(1)
        await expect(user.locator(".run-error")).to_have_count(0)
        await expect(user.locator(".message-metrics")).to_be_empty()
        await expect(assistant).to_have_class("message assistant failed empty-answer")
        await expect(assistant.locator(".failure-title")).to_have_text("Response failed")
        await expect(page.locator(".run-error:visible")).to_have_count(1)
        await expect(assistant.locator(".run-error")).to_have_text("Codex app-server disconnected.")
        await expect(assistant.locator(".message-footer")).to_be_visible()
        await expect(assistant.locator(".message-usage-label")).to_have_text("Usage · awaiting report")
        await expect(assistant.locator(".message-metrics")).to_be_empty()
        await expect(assistant.locator(".message-content")).to_be_hidden()
        await expect(assistant.get_by_role("button", name="Edit & retry")).to_be_visible()
        for action in ("Copy", "Reply", "Side thread"):
            await expect(assistant.get_by_role("button", name=action, exact=True)).to_have_count(0)
        await expect(assistant.locator(".retry-note")).to_contain_text("may have changed files or run commands")
        await page.evaluate("roomState().runs.clear(); renderCurrent()")
        await expect(assistant.locator(".run-error")).to_have_text("Codex app-server disconnected.")
        await expect(assistant.locator(".failure-title")).to_have_text("Response failed")
        await page.evaluate("roomState().messages.find(message => message.role === 'assistant').content = 'Error: Codex app-server disconnected.'; renderCurrent()")
        await expect(assistant.locator(".message-content")).to_be_hidden()
        await expect(page.locator(".run-error:visible")).to_have_count(1)
        await page.evaluate("roomState().messages.find(message => message.role === 'assistant').content = ''; renderCurrent()")
        for width, name in ((1440, "dark"), (400, "mobile")):
            await page.set_viewport_size({"width": width, "height": 900})
            if width == 400:
                await expect(page.locator("#sidebar")).to_have_css("transform", "matrix(1, 0, 0, 1, -270, 0)")
            await page.locator("#timeline-scroll").evaluate("element => {element.scrollTop=0;}")
            self.assertTrue(await page.evaluate("document.documentElement.scrollWidth <= innerWidth"))
            await expect(assistant.get_by_role("button", name="Edit & retry")).to_be_visible()
            await page.screenshot(path=f"/tmp/codex-lan-failure-{name}.png", full_page=True)
        await page.set_viewport_size({"width": 1365, "height": 900})
        run_count = len(store.list_runs(self.admin_id, failed_room["id"]))
        calls = len(self.worker.calls)
        await assistant.get_by_role("button", name="Edit & retry").click()
        await expect(page.locator("#message-input")).to_have_value(prompt)
        await expect(page.locator(".attachment-card")).to_have_count(1)
        await expect(page.locator(".attachment-card strong")).to_have_text("Original screenshot.png")
        await expect(page.locator(".attachment-card small")).to_have_text("Ready")
        await expect(page.locator(".attachment-card img")).to_have_attribute("src", self.base + "api/attachments/" + attachment["id"])
        self.assertEqual(await page.evaluate("roomState().files.map(file => file.id)"), [attachment["id"]])
        self.assertEqual(len(store.list_runs(self.admin_id, failed_room["id"])), run_count)
        self.assertEqual(len(self.worker.calls), calls)
        await page.locator("#message-input").fill("An unsent draft that must not disappear")
        confirmations = []

        async def dismiss(dialog):
            confirmations.append(dialog.message)
            await dialog.dismiss()

        page.once("dialog", dismiss)
        await assistant.get_by_role("button", name="Edit & retry").click()
        await expect(page.locator("#message-input")).to_have_value("An unsent draft that must not disappear")
        self.assertEqual(len(confirmations), 1)
        page.once("dialog", lambda dialog: dialog.accept())
        await assistant.get_by_role("button", name="Edit & retry").click()
        await expect(page.locator("#message-input")).to_have_value(prompt)
        await expect(page.locator(".attachment-card")).to_have_count(1)
        self.assertEqual(len(store.list_runs(self.admin_id, failed_room["id"])), run_count)
        self.assertEqual(len(self.worker.calls), calls)
        await page.evaluate("roomState().draft = ''; roomState().files = []; roomState().messages = roomState().messages.filter(message => message.role !== 'user'); document.getElementById('message-input').value = ''; renderCurrent()")
        requested = asyncio.Event()
        release = asyncio.Event()
        retry_url = f"**/api/rooms/{failed_room['id']}/messages?before=*"

        async def delayed_history(route):
            requested.set()
            await release.wait()
            await route.continue_()

        await page.route(retry_url, delayed_history)
        await assistant.get_by_role("button", name="Edit & retry").click()
        await asyncio.wait_for(requested.wait(), 3)
        await page.locator("#room-list .room-item").filter(has_text="Working together").click()
        await page.locator("#message-input").fill("Leave this other room’s draft untouched")
        release.set()
        await page.wait_for_function("roomId => roomState(roomId).draft.length > 0", arg=failed_room["id"])
        await page.unroute(retry_url, delayed_history)
        await expect(page.locator("#room-title")).to_have_text("Working together")
        await expect(page.locator("#message-input")).to_have_value("Leave this other room’s draft untouched")
        await page.locator("#room-list .room-item").filter(has_text="Failed attempt").click()
        await expect(page.locator("#message-input")).to_have_value(prompt)
        await expect(page.locator(".attachment-card")).to_have_count(1)
        self.assertEqual(len(self.worker.calls), calls)
        await self.worker.close()
        for _ in range(100):
            if not self.service.runtime.connected(self.admin_id):
                break
            await asyncio.sleep(.01)
        await page.evaluate("checkSession()")
        await expect(page.locator("#connection-status")).to_have_attribute("data-server-connected", "true")
        await expect(page.locator("#connection-status")).to_have_attribute("data-worker-ready", "false")
        await expect(page.locator("#connection-text")).to_contain_text("Worker offline")
        await expect(page.locator("#connection-banner")).to_be_visible()
        await expect(page.locator("#connection-banner")).to_contain_text("worker")
        await expect(page.locator("#send-button")).to_be_disabled()
        self.assertTrue(self.service.runtime.connected(self.client_id))
        self.assertFalse(self.errors, self.errors)

    async def test_room_row_settings_and_delete_require_owned_room_and_confirmation(self):
        page = self.page
        store = self.service.store
        disposable = store.create_room(self.admin_id, "Disposable test room", "/home/server/temporary", "astra", "medium")
        await page.route("**/style.css", lambda route: route.fulfill(path=WEB / "style.css", content_type="text/css"))
        await self.login(self.admin)
        await page.locator("#theme-select").select_option("cosmic")
        await expect(page.locator("html")).to_have_attribute("data-theme", "cosmic")
        await page.locator("#room-list .room-item").filter(has_text="Working together").click()
        await expect(page.locator("#room-title")).to_have_text("Working together")
        own_row = page.locator(f".room-row[data-room-id='{disposable['id']}']")
        foreign_row = page.locator(f".room-row[data-room-id='{self.foreign['id']}']")
        await expect(own_row.get_by_role("button", name="Settings for Disposable test room", exact=True)).to_be_visible()
        delete_button = own_row.get_by_role("button", name="Delete Disposable test room", exact=True)
        await expect(delete_button).to_be_visible()
        default_delete_background = await delete_button.evaluate("node => getComputedStyle(node).backgroundColor")
        await delete_button.hover()
        danger_color = await delete_button.evaluate("""node => {
            const probe = document.createElement('span'); probe.style.color = 'var(--danger)';
            node.append(probe); const color = getComputedStyle(probe).color; probe.remove(); return color;
        }""")
        await expect(delete_button).to_have_css("color", danger_color)
        hovered_delete_appearance = await delete_button.evaluate("""node => ({
            color: getComputedStyle(node).color,
            background: getComputedStyle(node).backgroundColor,
        })""")
        self.assertNotEqual(hovered_delete_appearance["background"], default_delete_background, hovered_delete_appearance)
        self.assertEqual(hovered_delete_appearance["color"], danger_color, hovered_delete_appearance)
        await expect(foreign_row.locator(".room-settings, .room-delete")).to_have_count(0)
        await expect(foreign_row.locator(".room-pin")).to_have_count(1)
        await own_row.get_by_role("button", name="Settings for Disposable test room", exact=True).click()
        await expect(page.locator("#room-dialog")).to_be_visible()
        await expect(page.locator("#room-name-input")).to_have_value("Disposable test room")
        await expect(page.locator("#room-cwd-input")).to_have_value("/home/server/temporary")
        await expect(page.locator("#room-cwd-input")).to_be_enabled()
        await page.locator("#room-name-input").fill("Renamed test room")
        await page.locator("#room-cwd-input").fill("/home/server/renamed-workspace")
        await page.locator("#room-save").click()
        await expect(page.locator("#room-dialog")).to_be_hidden()
        await expect(page.locator("#room-title")).to_have_text("Working together")
        self.assertEqual(store.get_room(self.admin_id, disposable["id"])["title"], "Renamed test room")
        self.assertEqual(store.get_room(self.admin_id, disposable["id"])["cwd"], "/home/server/renamed-workspace")
        await expect(own_row.get_by_role("button", name="Delete Renamed test room", exact=True)).to_be_visible()
        confirmations = []

        async def dismiss(dialog):
            confirmations.append(dialog.message)
            await dialog.dismiss()

        page.once("dialog", dismiss)
        await own_row.get_by_role("button", name="Delete Renamed test room", exact=True).click()
        await expect(own_row).to_be_visible()
        self.assertEqual(len(confirmations), 1)
        self.assertIn("cannot be undone", confirmations[0])
        self.assertEqual(store.get_room(self.admin_id, disposable["id"])["title"], "Renamed test room")
        page.once("dialog", lambda dialog: dialog.accept())
        await own_row.get_by_role("button", name="Delete Renamed test room", exact=True).click()
        await expect(own_row).to_have_count(0)
        await expect(page.locator("#room-title")).to_have_text("Working together")
        with self.assertRaises(PermissionError):
            store.get_room(self.admin_id, disposable["id"])
        self.assertEqual(store.get_room(self.client_id, self.foreign["id"])["title"], "Client-owned room")
        await page.locator("#message-input").fill("Keep running room protected")
        await page.locator("#message-input").press("Enter")
        await page.locator("#message-input").press("Enter")
        await self.wait_worker_turn()
        await expect(page.get_by_role("button", name="Delete Working together", exact=True)).to_be_disabled()
        await self.worker.complete()
        await expect(page.get_by_role("button", name="Delete Working together", exact=True)).to_be_enabled()
        self.assertFalse(self.errors, self.errors)


if __name__ == "__main__":
    unittest.main()
