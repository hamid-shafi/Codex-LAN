"""CODEX: Browser steering uses real server routes and deterministic local workers."""

from __future__ import annotations

import asyncio
import json
import unittest

from aiohttp import WSMsgType
from playwright.async_api import expect

import test_lan_browser as browser_fixture
from test_lan_server import FakeWorker, PNG, host_worker_socket


class SteeringWorker(FakeWorker):
    def __init__(self, ws, name):
        self.uncertain = False
        super().__init__(ws, name)

    async def serve(self):
        async for message in self.ws:
            if message.type != WSMsgType.TEXT:
                continue
            packet = json.loads(message.data)
            if packet.get("type") != "rpc":
                continue
            self.calls.append(packet)
            method = packet["method"]
            if method in {"thread/start", "thread/resume"}:
                result = {"thread": {"id": self.thread}}
            elif method == "turn/start":
                self.next_turn += 1
                self.turn = f"turn-{self.name}-{self.next_turn}"
                result = {"turn": {"id": self.turn}}
            elif method == "turn/steer":
                result = {"turnId": "unconfirmed-turn" if self.uncertain else self.turn}
            elif method == "lan/upload":
                result = {"path": "/fake/client/update.png"}
            else:
                result = {}
            await self.ws.send_json({"type": "result", "id": packet["id"], "result": result})


class LanSteeringBrowserTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = browser_fixture.LanBrowserTests.asyncSetUp
    asyncTearDown = browser_fixture.LanBrowserTests.asyncTearDown
    login = browser_fixture.LanBrowserTests.login
    wait_worker_turn = browser_fixture.LanBrowserTests.wait_worker_turn

    async def start_worker(self, device, name):
        ws, _auth = await host_worker_socket(self.http, self.service, self.admin, device)
        await ws.send_json({"type": "ready", "models": browser_fixture.BROWSER_MODELS, "cwd": "/home/" + name})
        worker = SteeringWorker(ws, name)
        self.workers.append(worker)
        for _ in range(100):
            if self.service.runtime.connected(device["device"]["id"]):
                break
            await asyncio.sleep(.01)
        return worker

    async def active_chat(self):
        await self.login(self.admin)
        await expect(self.page.locator("#room-title")).to_have_text("Working together")
        await self.page.locator("#message-input").fill("Original request")
        await self.page.get_by_role("button", name="Send message", exact=True).click()
        await self.wait_worker_turn()
        await self.worker.delta("Original streaming response")
        await expect(self.page.locator('.message[data-key^="run:"] .message-content')).to_have_text("Original streaming response")
        return self.page.locator("#message-input")

    def calls(self, method):
        return [call for call in self.worker.calls if call["method"] == method]

    async def test_multiple_updates_images_polling_reload_and_stop_remain_independent(self):
        input = await self.active_chat()
        await expect(self.page.locator("#model-select")).to_be_disabled()
        await expect(self.page.locator("#effort-select")).to_be_disabled()
        await input.fill("First update فارسی")
        await expect(self.page.get_by_role("button", name="Send update", exact=True)).to_be_enabled()
        await input.press("Enter")
        await input.press("Enter")
        await expect(input).to_have_value("")
        await expect(self.page.locator(".message.user .message-content").filter(has_text="First update فارسی")).to_have_count(1)
        await expect(self.page.locator('.message[data-key^="run:"] .message-content')).to_have_text("Original streaming response")
        await self.page.locator("#attachment-input").set_input_files({"name": "update.png", "mimeType": "image/png", "buffer": PNG})
        await expect(self.page.locator(".attachment-card small")).to_have_text("Ready")
        await self.page.get_by_role("checkbox", name="Double Enter to send").uncheck()
        await input.fill("Second update with image")
        await input.press("Enter")
        await expect(input).to_have_value("")
        await expect(self.page.locator(".message.user").filter(has_text="Second update with image").locator("img")).to_have_count(1)
        await self.page.evaluate("fetchEvents(state.selected).then(() => renderCurrent())")
        self.assertEqual(len(self.calls("turn/start")), 1)
        self.assertEqual(len(self.calls("turn/steer")), 2)
        for call in self.calls("turn/steer"):
            self.assertEqual(call["params"]["expectedTurnId"], self.worker.turn)
        self.assertTrue(any(item["type"] == "localImage" for item in self.calls("turn/steer")[1]["params"]["input"]))
        await self.page.reload()
        await expect(self.page.locator("#room-title")).to_have_text("Working together")
        for text in ("Original request", "First update فارسی", "Second update with image"):
            await expect(self.page.locator(".message.user .message-content").filter(has_text=text)).to_have_count(1)
        await expect(self.page.locator('.message[data-key^="run:"] .message-content')).to_have_text("Original streaming response")
        await expect(self.page.locator("#stop-button")).to_be_enabled()
        await self.page.locator("#stop-button").click()
        await self.worker.complete("interrupted")
        await expect(self.page.locator("#stop-button")).to_be_hidden()
        self.assertEqual(len(self.calls("turn/interrupt")), 1)
        self.assertEqual(len(self.calls("turn/start")), 1)
        self.assertFalse(self.errors, self.errors)

    async def test_conflict_keeps_draft_and_retry_targets_original_run_after_completion(self):
        input = await self.active_chat()
        requests = []

        async def conflict(route):
            requests.append(route.request.post_data_json)
            await route.fulfill(status=409, content_type="application/json", body=json.dumps({"error": "The original turn ended.", "retryable": False}))

        await self.page.route("**/api/rooms/*/steer", conflict)
        await input.fill("Keep this update")
        await self.page.get_by_role("button", name="Send update", exact=True).click()
        await expect(input).to_be_enabled()
        await expect(input).to_have_value("Keep this update")
        self.assertEqual(len(requests), 1)
        await self.worker.complete()
        await expect(self.page.locator("#stop-button")).to_be_hidden()
        await expect(self.page.get_by_role("button", name="Send update", exact=True)).to_be_enabled()
        self.assertEqual(len(requests), 1)
        await self.page.get_by_role("button", name="Send update", exact=True).click()
        await expect(input).to_be_enabled()
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[0]["run_id"], requests[1]["run_id"])
        self.assertEqual(requests[0]["request_id"], requests[1]["request_id"])
        self.assertEqual(len(self.calls("turn/start")), 1)
        await expect(input).to_have_value("Keep this update")
        self.assertFalse(self.errors, self.errors)

    async def test_uncertain_delivery_keeps_draft_and_id_without_resending_provider(self):
        input = await self.active_chat()
        self.worker.uncertain = True
        await input.fill("Possibly delivered update")
        await self.page.get_by_role("button", name="Send update", exact=True).click()
        await expect(self.page.get_by_role("button", name="Check update", exact=True)).to_be_enabled()
        await expect(input).to_have_value("Possibly delivered update")
        await expect(self.page.locator("#composer-hint")).to_contain_text("delivery is unconfirmed")
        first_id = await self.page.evaluate("roomState().pendingSend.id")
        await self.page.get_by_role("button", name="Check update", exact=True).click()
        await expect(input).to_be_enabled()
        self.assertEqual(first_id, await self.page.evaluate("roomState().pendingSend.id"))
        self.assertEqual(len(self.calls("turn/steer")), 1)
        await expect(input).to_have_value("Possibly delivered update")
        await self.page.reload()
        await expect(self.page.locator("#room-title")).to_have_text("Working together")
        await expect(self.page.locator(".steering-notice")).to_contain_text("Delivery of 1 update is unconfirmed")
        self.assertEqual(len(self.calls("turn/steer")), 1)
        self.assertFalse(self.errors, self.errors)

    async def test_another_browser_receives_followup_event_once(self):
        input = await self.active_chat()
        second = await self.context.new_page()
        await second.goto(self.base + "?room=" + self.room["id"])
        await expect(second.locator("#room-title")).to_have_text("Working together")
        await input.fill("Update visible in both tabs")
        await self.page.get_by_role("button", name="Send update", exact=True).click()
        await expect(input).to_have_value("")
        await expect(second.locator(".message.user .message-content").filter(has_text="Update visible in both tabs")).to_have_count(1, timeout=10000)
        await second.evaluate("fetchEvents(state.selected).then(() => renderCurrent())")
        await expect(second.locator(".message.user .message-content").filter(has_text="Update visible in both tabs")).to_have_count(1)
        await expect(second.locator('.message[data-key^="run:"] .message-content')).to_have_text("Original streaming response")
        self.assertFalse(self.errors, self.errors)

    async def test_lost_initial_response_retry_does_not_turn_into_followup(self):
        page = self.page
        await self.login(self.admin)
        await expect(page.locator("#room-title")).to_have_text("Working together")
        requests = []

        async def submit(route):
            requests.append(route.request.post_data_json)
            response = await route.fetch()
            if len(requests) == 1:
                await route.abort("failed")
            else:
                await route.fulfill(response=response)

        await page.route("**/api/rooms/*/runs", submit)
        await page.locator("#message-input").fill("Initial message with lost receipt")
        await page.get_by_role("button", name="Send message", exact=True).click()
        await self.wait_worker_turn()
        await self.worker.delta("Still processing the original")
        await expect(page.locator('.message[data-key^="run:"] .message-content')).to_have_text("Still processing the original", timeout=10000)
        await expect(page.get_by_role("button", name="Send message", exact=True)).to_be_enabled()
        await page.get_by_role("button", name="Send message", exact=True).click()
        await expect(page.locator("#message-input")).to_have_value("")
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[0]["request_id"], requests[1]["request_id"])
        self.assertEqual(len(self.calls("turn/start")), 1)
        self.assertEqual(len(self.calls("turn/steer")), 0)
        await expect(page.locator('.message[data-key^="run:"] .message-content')).to_have_text("Still processing the original")
        await expect(page.locator(".message.user .message-content").filter(has_text="Initial message with lost receipt")).to_have_count(1)
        self.assertFalse(self.errors, self.errors)


if __name__ == "__main__":
    unittest.main()
