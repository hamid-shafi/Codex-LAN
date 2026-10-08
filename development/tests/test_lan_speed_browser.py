"""CODEX: Per-room Fast controls use fake workers, never a live provider request."""

from __future__ import annotations

import asyncio
import unittest

from playwright.async_api import expect

import test_lan_browser as browser_fixture
import test_lan_steering_browser as steering_fixture
from test_lan_server import host_worker_socket


class LanSpeedBrowserTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = browser_fixture.LanBrowserTests.asyncSetUp
    asyncTearDown = browser_fixture.LanBrowserTests.asyncTearDown
    wait_worker_turn = browser_fixture.LanBrowserTests.wait_worker_turn

    async def start_worker(self, device, name):
        ws, _auth = await host_worker_socket(self.http, self.service, self.admin, device)
        models = [*browser_fixture.BROWSER_MODELS, {
            "id": "gpt-6-astra", "model": "gpt-6-astra", "displayName": "GPT-6 Astra",
            "defaultReasoningEffort": "medium", "supportedReasoningEfforts": [{"reasoningEffort": "medium"}, {"reasoningEffort": "ultra"}],
        }]
        await ws.send_json({"type": "ready", "models": models, "cwd": "/home/" + name})
        worker = steering_fixture.SteeringWorker(ws, name)
        self.workers.append(worker)
        for _ in range(100):
            if self.service.runtime.connected(device["device"]["id"]):
                break
            await asyncio.sleep(.01)
        return worker

    async def open_chat(self):
        self.service.store.update_room(self.admin_id, self.room["id"], model="gpt-6-astra", speed="standard")
        await self.page.goto(self.base + "#login=" + self.admin["token"])
        await expect(self.page.locator("#room-title")).to_have_text("Working together")
        await expect(self.page.locator("#speed-select")).to_be_enabled()

    async def test_fast_persists_inherits_resets_for_unsupported_models_and_fits_mobile(self):
        await self.open_chat()
        placement = await self.page.evaluate(
            """() => {
                const model = document.getElementById('model-select').closest('label');
                const speed = document.getElementById('speed-select').closest('label');
                return {
                    inComposer: speed.closest('.composer-tools') !== null,
                    inHeader: speed.closest('.topbar') !== null,
                    followsModel: model.nextElementSibling === speed,
                };
            }"""
        )
        self.assertEqual(
            placement,
            {"inComposer": True, "inHeader": False, "followsModel": True},
        )
        await expect(self.page.locator("#speed-select")).to_have_value("standard")
        await expect(self.page.locator("#speed-notice")).to_be_hidden()
        async with self.page.expect_response(lambda response: response.request.method == "PATCH" and "/api/rooms/" in response.url):
            await self.page.locator("#speed-select").select_option("fast")
        await expect(self.page.locator("#speed-notice")).to_contain_text("2.5× estimated usage weight")
        await self.page.reload()
        await expect(self.page.locator("#speed-select")).to_have_value("fast")
        for width in (400, 768, 1365):
            await self.page.set_viewport_size({"width": width, "height": 900})
            await expect(self.page.locator("#speed-select")).to_be_visible()
            self.assertTrue(await self.page.evaluate("() => document.documentElement.scrollWidth <= innerWidth"))
            self.assertTrue(await self.page.locator("#speed-select").evaluate("el => el.getBoundingClientRect().right <= innerWidth"))
        await self.page.set_viewport_size({"width": 1365, "height": 900})
        await self.page.locator("#new-room").click()
        await expect(self.page.locator("#room-title")).to_have_text("New chat")
        await self.page.locator("#room-menu").click()
        await self.page.locator("#room-name-input").fill("Fast inherited")
        await self.page.locator("#room-save").click()
        await expect(self.page.locator("#room-title")).to_have_text("Fast inherited")
        await expect(self.page.locator("#speed-select")).to_have_value("fast")
        room = next(room for room in self.service.store.list_rooms(self.admin_id) if room["title"] == "Fast inherited")
        self.assertEqual(room["speed"], "fast")
        async with self.page.expect_response(lambda response: response.request.method == "PATCH" and "/api/rooms/" in response.url):
            await self.page.locator("#model-select").select_option("sol")
        await expect(self.page.locator("#speed-select")).to_have_value("standard")
        await expect(self.page.locator("#speed-select")).to_be_disabled()
        self.assertEqual(self.service.store.get_room(self.admin_id, room["id"])["speed"], "standard")
        async with self.page.expect_response(lambda response: response.request.method == "PATCH" and "/api/rooms/" in response.url):
            await self.page.locator("#model-select").select_option("gpt-6-astra")
        await expect(self.page.locator("#speed-select")).to_have_value("standard")
        await expect(self.page.locator("#speed-select")).to_be_enabled()
        async with self.page.expect_response(lambda response: response.request.method == "PATCH" and "/api/rooms/" in response.url):
            await self.page.locator("#speed-select").select_option("fast")
        await expect(self.page.locator("#speed-notice")).to_contain_text("2.5× estimated usage weight")
        await self.page.screenshot(path="/tmp/codex-lan-fast-selector.png", full_page=True)
        self.assertFalse(self.errors, self.errors)

    async def test_active_room_locks_speed_other_rooms_editable_and_steering_never_overrides(self):
        await self.open_chat()
        idle = self.service.store.create_room(self.admin_id, "Independent speed", "/home/server/project", "gpt-6-astra", "medium")
        async with self.page.expect_response(lambda response: response.request.method == "PATCH" and "/api/rooms/" in response.url):
            await self.page.locator("#speed-select").select_option("fast")
        await self.page.locator("#message-input").fill("Start a fake Fast response")
        await self.page.get_by_role("button", name="Send message", exact=True).click()
        await self.wait_worker_turn()
        await self.page.locator("#room-menu").click()
        await expect(self.page.locator("#room-cwd-input")).to_be_disabled()
        await expect(self.page.locator("#room-cwd-hint")).to_contain_text("Stop the active response")
        await self.page.locator("#dialog-cancel").click()
        start = next(call for call in self.worker.calls if call["method"] == "turn/start")
        self.assertEqual(start["params"]["serviceTierForTurn"], "fast")
        await expect(self.page.locator("#speed-select")).to_be_disabled()
        await self.page.locator("#message-input").fill("Keep the current speed unchanged")
        async with self.page.expect_request(lambda request: request.method == "POST" and request.url.endswith("/steer")) as request:
            await self.page.get_by_role("button", name="Send update", exact=True).click()
        steering = (await request.value).post_data_json
        self.assertNotIn("speed", steering)
        self.assertNotIn("model", steering)
        await expect(self.page.locator("#message-input")).to_have_value("")
        await self.page.evaluate("() => refreshRooms()")
        await self.page.locator("#room-list .room-item").filter(has_text="Independent speed").click()
        await expect(self.page.locator("#speed-select")).to_be_enabled()
        await expect(self.page.locator("#speed-select")).to_have_value("standard")
        async with self.page.expect_response(lambda response: response.request.method == "PATCH" and "/api/rooms/" in response.url):
            await self.page.locator("#speed-select").select_option("fast")
        self.assertEqual(self.service.store.get_room(self.admin_id, idle["id"])["speed"], "fast")
        await self.page.locator("#room-list .room-item").filter(has_text="Working together").click()
        await expect(self.page.locator("#speed-select")).to_have_value("fast")
        await expect(self.page.locator("#speed-select")).to_be_disabled()
        await self.worker.complete()
        await expect(self.page.locator("#speed-select")).to_be_enabled(timeout=10000)
        async with self.page.expect_response(lambda response: response.request.method == "PATCH" and "/api/rooms/" in response.url):
            await self.page.locator("#speed-select").select_option("standard")
        await self.page.locator("#message-input").fill("Standard next response")
        await self.page.get_by_role("button", name="Send message", exact=True).click()
        for _ in range(100):
            starts = [call for call in self.worker.calls if call["method"] == "turn/start"]
            if len(starts) == 2:
                break
            await asyncio.sleep(.02)
        self.assertEqual(starts[-1]["params"]["serviceTierForTurn"], "default")
        self.assertFalse(self.errors, self.errors)


if __name__ == "__main__":
    unittest.main()
