"""CODEX: Connection phases remain visible through retries, room changes, and reloads."""

from __future__ import annotations

import time
import unittest

from playwright.async_api import expect

import test_lan_browser as browser_fixture


class LanReconnectBrowserTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = browser_fixture.LanBrowserTests.asyncSetUp
    asyncTearDown = browser_fixture.LanBrowserTests.asyncTearDown
    start_worker = browser_fixture.LanBrowserTests.start_worker
    login = browser_fixture.LanBrowserTests.login

    async def install_connection(self):
        self.connection = {"state": "reconnecting", "mode": "direct", "preferred_mode": "direct", "attempt": 5,
                           "switches": 0, "retry_in_seconds": 4, "updated_at": time.time(),
                           "message": "The model service did not respond. Retrying automatically."}
        self.run = {"id": "reconnect-browser", "status": "running", "retrying": True,
                    "connection": self.connection, "created_at": time.time() - 80,
                    "last_event_id": 0, "output_text": "Saved progress before the connection dropped.",
                    "activity": "Editing the app", "activities": [
                        {"id": "last-command", "kind": "command", "status": "running", "title": "Run Python tests"}]}

        async def snapshot(route):
            response = await route.fetch()
            data = await response.json()
            data["runs"] = [self.run]
            await route.fulfill(response=response, json=data)

        async def rooms(route):
            response = await route.fetch()
            data = await response.json()
            for room in data["rooms"]:
                if room["id"] == self.room["id"]:
                    room.update(status="running", connection=self.connection)
            await route.fulfill(response=response, json=data)

        await self.page.route("**/api/rooms", rooms)
        await self.page.route(f"**/api/rooms/{self.room['id']}/messages*", snapshot)
        await self.page.route(f"**/api/rooms/{self.room['id']}/events*", snapshot)
        await self.login(self.admin)
        await expect(self.page.locator("#room-title")).to_have_text("Working together")
        await expect(self.page.locator('.message[data-key="run:reconnect-browser"] .run-connection')).to_be_visible()

    async def test_provider_retry_and_mode_switch_survive_room_change_and_reload(self):
        await self.install_connection()
        page = self.page
        article = page.locator('.message[data-key="run:reconnect-browser"]')
        card = article.locator(".run-connection")
        await expect(card).to_be_visible()
        await expect(card).to_contain_text("Retrying saved connection · Direct")
        await expect(article.locator(".activity-current")).to_be_hidden()
        await expect(article.locator(".activity-details")).not_to_have_attribute("open", "")
        await expect(article.locator(".activity-counts")).to_have_text("1 last reported active")
        await expect(article.locator(".message-metrics")).to_contain_text("Elapsed 1m")
        await expect(page.locator("#connection-text")).to_have_text("Retrying saved connection · Direct")
        room = page.locator(f'.room-row[data-room-id="{self.room["id"]}"]')
        await expect(room.locator(".room-item-meta")).to_have_text("Retrying saved connection · Direct")
        await expect(article.locator(".run-failure")).to_be_hidden()
        self.connection.update(mode="proxy", attempt=1, switches=1, updated_at=time.time())
        await page.evaluate("() => scheduleEvents(0)")
        await expect(card).to_contain_text("Trying another network route · SOCKS5 proxy")
        await expect(card).to_contain_text("Network route changed 1 time")
        await page.evaluate("() => refreshRooms()")
        await page.evaluate("id => selectRoom(id)", self.foreign["id"])
        await expect(page.locator("#room-title")).to_have_text("Client-owned room")
        await expect(room.locator(".room-item-meta")).to_have_text("Trying another network route · Proxy")
        await page.evaluate("id => selectRoom(id)", self.room["id"])
        await expect(card).to_contain_text("SOCKS5 proxy")
        await page.reload()
        await expect(card).to_contain_text("SOCKS5 proxy")
        await expect(article.locator(".message-content")).to_contain_text("Saved progress")
        for width in (400, 1365):
            await page.set_viewport_size({"width": width, "height": 900})
            self.assertTrue(await page.evaluate("document.documentElement.scrollWidth <= innerWidth"))
        await page.screenshot(path="/tmp/codex-lan-reconnecting.png", full_page=True)
        self.connection.update(state="switching")
        await page.evaluate("() => scheduleEvents(0)")
        await expect(card).to_contain_text("Trying another network route · SOCKS5 proxy")
        self.connection.update(state="resuming")
        await page.evaluate("() => scheduleEvents(0)")
        await expect(card).to_contain_text("Restoring response · SOCKS5 proxy")
        await expect(page.locator("#connection-text")).to_have_text("Restoring response · Proxy")
        await expect(card).not_to_contain_text("Connection restored")
        self.connection.update(state="connected", retry_in_seconds=0)
        self.run["retrying"] = False
        await page.evaluate("() => { state.workerConnection={state:'resuming',mode:'direct',attempt:2}; scheduleEvents(0); }")
        await expect(card).to_be_hidden()
        await expect(article.locator(".activity-current-label")).to_have_text("Run Python tests")
        await expect(page.locator("#connection-text")).to_have_text("Worker ready")
        self.assertFalse(self.errors, self.errors)

    async def test_browser_offline_and_legacy_retry_do_not_claim_current_work(self):
        await self.install_connection()
        page = self.page
        article = page.locator('.message[data-key="run:reconnect-browser"]')
        self.run.pop("connection")
        await page.evaluate("() => { roomState().runs.get('reconnect-browser').connection=null; scheduleEvents(0); }")
        await expect(article.locator(".run-connection-title")).to_contain_text("Retrying saved connection")
        await page.locator("#message-input").fill("Keep my unsent update فارسی")
        await self.context.set_offline(True)
        await expect(page.locator("#connection-text")).to_have_text("Reconnecting to server")
        await expect(article.locator(".run-connection-title")).to_have_text("Reconnecting to server — live status unavailable")
        await expect(article.locator(".run-connection-detail")).to_contain_text("server may still be processing")
        await expect(article.locator(".run-failure")).to_be_hidden()
        await expect(page.locator("#message-input")).to_have_value("Keep my unsent update فارسی")
        await self.context.set_offline(False)
        await page.evaluate("() => checkSession()")
        await expect(article.locator(".run-connection-title")).to_have_text("Retrying saved connection")
        await expect(page.locator("#message-input")).to_have_value("Keep my unsent update فارسی")
        self.run.update(status="failed", error="Authentication expired. Sign in again.", retrying=False)
        await page.evaluate("""() => {
            cancelTimers();
            const cache = roomState();
            const run = cache.runs.get('reconnect-browser');
            run.status = 'failed'; run.error = 'Authentication expired. Sign in again.';
            mergeMessage(cache, {id:'reconnect-auth-error',role:'assistant',run_id:run.id,run_status:'failed',run_error:run.error,content:'',created_at:Date.now()/1000});
            renderCurrent();
        }""")
        failure = page.locator('.message[data-key="message:reconnect-auth-error"]')
        await expect(failure.locator(".run-failure")).to_be_visible()
        await expect(failure.locator(".run-error")).to_have_text("Authentication expired. Sign in again.")
        await expect(failure.locator(".run-connection")).to_be_hidden()
        self.assertFalse(self.errors, self.errors)

    async def test_stale_failed_request_cannot_overwrite_newer_server_success(self):
        await self.login(self.admin)
        result = await self.page.evaluate("""async () => {
            cancelTimers();
            const realFetch = window.fetch;
            window.fetch = (input, options) => {
              const path = String(input);
              if (path === '/test/old-timeout') return new Promise((resolve, reject) => setTimeout(() => reject(new TypeError('stale request')), 120));
              if (path === '/test/new-success') return new Promise((resolve) => setTimeout(() => resolve(new Response('{}', {status: 200, headers: {'Content-Type': 'application/json'}})), 20));
              return realFetch(input, options);
            };
            try {
              const oldRequest = api('/test/old-timeout', {timeout: 1000}).catch(() => null);
              await new Promise((resolve) => setTimeout(resolve, 10));
              await api('/test/new-success', {timeout: 1000});
              await oldRequest;
              await new Promise((resolve) => setTimeout(resolve, 650));
              return {
                connected: state.connected,
                status: document.getElementById('connection-text').textContent,
                bannerHidden: document.getElementById('connection-banner').hidden,
              };
            } finally {
              window.fetch = realFetch;
            }
        }""")
        self.assertTrue(result["connected"])
        self.assertEqual(result["status"], "Worker ready")
        self.assertTrue(result["bannerHidden"])
        self.assertFalse(self.errors, self.errors)

    async def test_unlock_events_debounce_a_transient_browser_offline_signal(self):
        await self.login(self.admin)
        immediate = await self.page.evaluate("""() => {
            window.__connectionOriginalFetch = window.fetch;
            window.__connectionSessionFetches = 0;
            window.fetch = (input, options) => {
              if (String(input).split('?')[0] === '/api/me') window.__connectionSessionFetches += 1;
              return window.__connectionOriginalFetch(input, options);
            };
            window.dispatchEvent(new Event('offline'));
            window.dispatchEvent(new PageTransitionEvent('pageshow'));
            window.dispatchEvent(new Event('focus'));
            return {
              connected: state.connected,
              status: document.getElementById('connection-text').textContent,
            };
        }""")
        self.assertTrue(immediate["connected"])
        self.assertEqual(immediate["status"], "Worker ready")
        await self.page.wait_for_timeout(250)
        self.assertEqual(await self.page.evaluate("window.__connectionSessionFetches"), 1)
        await self.page.wait_for_timeout(2150)
        await expect(self.page.locator("#connection-status")).to_have_attribute("data-server-connected", "true")
        await expect(self.page.locator("#connection-text")).to_have_text("Worker ready")
        await expect(self.page.locator("#connection-banner")).to_be_hidden()
        await self.page.evaluate("() => { window.fetch = window.__connectionOriginalFetch; }")
        self.assertFalse(self.errors, self.errors)


if __name__ == "__main__":
    unittest.main()
