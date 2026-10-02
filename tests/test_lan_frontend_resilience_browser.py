"""CODEX: Recoverable network failures and room switches must preserve drafts."""

from __future__ import annotations

import unittest

from playwright.async_api import expect

import test_lan_browser as browser_fixture


class LanFrontendResilienceBrowserTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = browser_fixture.LanBrowserTests.asyncSetUp
    asyncTearDown = browser_fixture.LanBrowserTests.asyncTearDown
    start_worker = browser_fixture.LanBrowserTests.start_worker
    login = browser_fixture.LanBrowserTests.login

    async def test_truncated_submit_reply_keeps_draft_and_retries_same_request(self):
        await self.login(self.admin)
        draft = "Keep this prompt if the accepted reply is truncated."
        await self.page.locator("#message-input").fill(draft)
        await self.page.evaluate(
            """() => {
                window.resilienceFetch = window.fetch;
                window.resilienceSubmissions = [];
                window.fetch = async (path, options) => {
                    const response = await window.resilienceFetch(path, options);
                    if (String(path).endsWith('/runs') && options.method === 'POST') {
                        window.resilienceSubmissions.push(JSON.parse(options.body).request_id);
                        if (window.resilienceSubmissions.length === 1) {
                            const body = await response.text();
                            return new Response(body.slice(0, -1), {
                                status: response.status,
                                headers: {'Content-Type': 'application/json'},
                            });
                        }
                    }
                    return response;
                };
            }"""
        )
        await self.page.evaluate("() => sendMessage()")
        await expect(self.page.locator("#message-input")).to_have_value(draft)
        self.assertTrue(await self.page.evaluate("Boolean(roomState().pendingSend?.id)"))
        await expect(self.page.locator("#toast-message")).to_contain_text("Your draft is kept")
        await self.page.evaluate("() => sendMessage()")
        await expect(self.page.locator("#message-input")).to_have_value("")
        request_ids = await self.page.evaluate("window.resilienceSubmissions")
        self.assertEqual(len(request_ids), 2)
        self.assertEqual(request_ids[0], request_ids[1])
        runs = self.service.store.db.execute(
            "SELECT COUNT(*) FROM runs WHERE room_id = ? AND request_id = ?",
            (self.room["id"], request_ids[0]),
        ).fetchone()[0]
        self.assertEqual(runs, 1)
        self.assertFalse(self.errors, self.errors)

    async def test_api_timeout_covers_response_body_and_http_errors_keep_status(self):
        await self.login(self.admin)
        result = await self.page.evaluate(
            """async () => {
                const original = window.fetch;
                let stopBody;
                let aborted = false;
                window.fetch = async (path, options) => {
                    if (path === '/test/body-stalls') {
                        return new Response(new ReadableStream({start(controller) {
                            controller.enqueue(new TextEncoder().encode('{'));
                            stopBody = () => controller.error(new TypeError('Connection closed'));
                            options.signal.addEventListener('abort', () => {
                                aborted = true;
                                controller.error(new DOMException('Request timed out', 'AbortError'));
                            }, {once: true});
                        }}), {status: 200, headers: {'Content-Type': 'application/json'}});
                    }
                    if (path === '/test/no-content') return new Response(null, {status: 204});
                    if (path === '/test/forbidden') return new Response('Forbidden', {status: 403});
                    return original(path, options);
                };
                try {
                    const body = await Promise.race([
                        api('/test/body-stalls', {timeout: 40}).then(() => 'accepted', error => error.message),
                        new Promise(resolve => setTimeout(() => resolve('still waiting'), 350)),
                    ]);
                    const empty = await api('/test/no-content');
                    const forbidden = await api('/test/forbidden').then(() => null, error => error.status);
                    return {body, aborted, empty, forbidden};
                } finally {
                    if (!aborted) stopBody?.();
                    window.fetch = original;
                }
            }"""
        )
        self.assertTrue(result["aborted"], result)
        self.assertIn("did not respond", result["body"])
        self.assertEqual(result["empty"], {})
        self.assertEqual(result["forbidden"], 403)
        self.assertFalse(self.errors, self.errors)

    async def test_focused_room_switch_shows_and_preserves_the_correct_draft(self):
        other = self.service.store.create_room(
            self.admin_id, "Another draft", "/home/server/other", "astra", "medium"
        )
        await self.login(self.admin)
        await self.page.evaluate("id => selectRoom(id)", self.room["id"])
        draft = self.page.locator("#message-input")
        await draft.fill("First room's private draft")
        await self.page.evaluate("id => selectRoom(id)", other["id"])
        await expect(draft).to_be_focused()
        await expect(draft).to_have_value("")
        await draft.fill("Second room's separate draft")
        await self.page.evaluate("id => selectRoom(id)", self.room["id"])
        await expect(draft).to_have_value("First room's private draft")
        await draft.press("End")
        await draft.press("!")
        await self.page.evaluate("() => renderCurrent()")
        await expect(draft).to_have_value("First room's private draft!")
        await self.page.evaluate("id => selectRoom(id)", other["id"])
        await expect(draft).to_have_value("Second room's separate draft")
        self.assertFalse(self.errors, self.errors)


if __name__ == "__main__":
    unittest.main()
