"""CODEX: Reply alerts respect room ownership, visibility, reconnects, and browser permission."""

from __future__ import annotations

import asyncio
import unittest

from playwright.async_api import expect

import test_lan_browser as browser_fixture


class LanReplyNotificationsBrowserTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = browser_fixture.LanBrowserTests.asyncSetUp
    asyncTearDown = browser_fixture.LanBrowserTests.asyncTearDown
    start_worker = browser_fixture.LanBrowserTests.start_worker
    login = browser_fixture.LanBrowserTests.login
    wait_worker_turn = browser_fixture.LanBrowserTests.wait_worker_turn

    async def prepare_notifications(self, permission="granted"):
        await self.context.add_init_script("""(() => {
            window.replyNotifications = [];
            window.replyPermissionRequests = 0;
            class ReplyNotification {
                static permission = PERMISSION;
                static async requestPermission() { window.replyPermissionRequests++; return this.permission = 'granted'; }
                constructor(title, options) {
                    if (window.failReplyNotification) throw new Error('Desktop notifications unavailable');
                    this.title = title; this.options = options; this.closed = false;
                    window.replyNotifications.push(this);
                }
                close() { this.closed = true; this.onclose?.(); }
            }
            Object.defineProperty(window, 'Notification', { configurable: true, value: ReplyNotification });
        })();""".replace("PERMISSION", repr(permission)))
        self.background = self.service.store.create_room(self.admin_id, "Background work", "/work/admin", "astra", "medium")
        await self.login(self.admin)
        await self.page.evaluate("id => selectRoom(id)", self.room["id"])

    def finish_reply(self, room, request_id):
        run = self.service.store.create_run(room["owner_id"], room["id"], "Do this work", [], request_id)["run"]
        message = self.service.store.finish_run(run["id"], "completed", "The requested work is done.")
        self.service.store.append_event(run["id"], "completed", {"status": "completed", "message": message})
        return message

    async def test_only_own_background_replies_alert_and_click_opens_the_room(self):
        await self.prepare_notifications()
        self.finish_reply(self.room, "visible-reply")
        self.finish_reply(self.foreign, "foreign-reply")
        await self.page.evaluate("refreshRooms()")
        self.assertEqual(await self.page.evaluate("replyNotifications.length"), 0)
        self.finish_reply(self.background, "background-reply")
        await self.page.evaluate("refreshRooms()")
        self.assertEqual(await self.page.evaluate("replyNotifications.map(item => item.title)"), ["Reply ready · Background work"])
        self.assertTrue(await self.page.evaluate("replyNotifications[0].options.requireInteraction"))
        await self.page.evaluate("refreshRooms()")
        self.assertEqual(await self.page.evaluate("replyNotifications.length"), 1)
        await self.page.evaluate("replyNotifications[0].onclick()")
        await expect(self.page.locator("#room-title")).to_have_text("Background work")
        self.assertTrue(await self.page.evaluate("replyNotifications[0].closed"))
        await self.page.reload()
        await expect(self.page.locator("#workspace")).to_be_visible()
        await self.page.wait_for_function("state.routeReady")
        self.assertEqual(await self.page.evaluate("replyNotifications.length"), 0)
        self.assertFalse(self.errors, self.errors)

    async def test_blocked_or_failed_native_notifications_have_an_open_room_action(self):
        await self.prepare_notifications("denied")
        await self.page.locator("#message-input").fill("Keep my current draft")
        self.finish_reply(self.background, "fallback-reply")
        await self.page.evaluate("refreshRooms()")
        await expect(self.page.locator(".reply-notification strong")).to_have_text("Reply ready · Background work")
        await expect(self.page.locator(".reply-notification-open")).to_have_text("Open room")
        await self.page.evaluate("toast('Unrelated message')")
        await expect(self.page.locator(".reply-notification")).to_be_visible()
        await expect(self.page.locator("#message-input")).to_have_value("Keep my current draft")
        await self.page.locator(".reply-notification-open").click()
        await expect(self.page.locator("#room-title")).to_have_text("Background work")
        await expect(self.page.locator(".reply-notification")).to_have_count(0)
        await self.page.evaluate("id => selectRoom(id)", self.room["id"])
        await expect(self.page.locator("#message-input")).to_have_value("Keep my current draft")
        await self.page.evaluate("() => { Notification.permission = 'granted'; window.failReplyNotification = true; }")
        self.finish_reply(self.background, "native-failure-reply")
        await self.page.evaluate("refreshRooms()")
        await expect(self.page.locator(".reply-notification-open")).to_be_visible()
        await self.page.locator(".reply-notification-close").click()
        await expect(self.page.locator(".reply-notification")).to_have_count(0)
        self.assertFalse(self.errors, self.errors)

    async def test_same_room_alerts_while_browser_unfocused_and_closes_on_return(self):
        await self.prepare_notifications()
        await self.page.evaluate("() => { document.hasFocus = () => false; window.dispatchEvent(new Event('blur')); }")
        self.finish_reply(self.room, "unfocused-reply")
        await self.page.evaluate("refreshRooms()")
        self.assertEqual(await self.page.evaluate("replyNotifications.length"), 1)
        self.assertFalse(await self.page.evaluate("replyNotifications[0].closed"))
        await self.page.evaluate("refreshRooms()")
        self.assertFalse(await self.page.evaluate("replyNotifications[0].closed"))
        await self.page.evaluate("() => { document.hasFocus = () => true; window.dispatchEvent(new Event('focus')); }")
        self.assertTrue(await self.page.evaluate("replyNotifications[0].closed"))

    async def check_quick_switcher_room_entry_closes_matching_alert(self, permission):
        other = self.service.store.create_room(self.admin_id, "Another background room", "/work/other", "astra", "medium")
        await self.prepare_notifications(permission)
        self.finish_reply(self.background, "switcher-reply")
        self.finish_reply(other, "other-switcher-reply")
        await self.page.evaluate("refreshRooms()")
        if permission == "granted":
            self.assertEqual(await self.page.evaluate("replyNotifications.length"), 2)
        else:
            await expect(self.page.locator(".reply-notification")).to_have_count(2)
        await self.page.keyboard.press("Control+k")
        await expect(self.page.locator("#quick-switcher")).to_be_visible()
        await self.page.locator("#quick-switcher-input").fill("Background work")
        # Dialog focus restoration can lag behind the room-entry action.
        await self.page.evaluate("() => { document.hasFocus = () => false; window.dispatchEvent(new Event('blur')); }")
        seen_before = await self.page.evaluate("id => sidebarPreferences().seen[id]", self.background["id"])
        await self.page.keyboard.press("Enter")
        await expect(self.page.locator("#quick-switcher")).to_be_hidden()
        await expect(self.page.locator("#room-title")).to_have_text("Background work")
        await expect(self.page.locator("#message-input")).to_be_focused()
        if permission == "granted":
            self.assertTrue(await self.page.evaluate(
                "replyNotifications.find(item => item.title === 'Reply ready · Background work').closed"
            ))
            self.assertFalse(await self.page.evaluate(
                "replyNotifications.find(item => item.title === 'Reply ready · Another background room').closed"
            ))
        else:
            await expect(self.page.locator(".reply-notification")).to_have_count(1)
            await expect(self.page.locator(".reply-notification strong")).to_have_text("Reply ready · Another background room")
        self.assertEqual(await self.page.evaluate("id => sidebarPreferences().seen[id]", self.background["id"]), seen_before)
        self.assertFalse(self.errors, self.errors)

    async def test_quick_switcher_room_entry_closes_matching_desktop_alert(self):
        await self.check_quick_switcher_room_entry_closes_matching_alert("granted")

    async def test_quick_switcher_room_entry_closes_matching_in_app_alert(self):
        await self.check_quick_switcher_room_entry_closes_matching_alert("denied")

    async def test_unfocused_loaded_room_entry_dismisses_alert_but_keeps_read_and_future_alert_gates(self):
        await self.prepare_notifications()
        await self.page.evaluate("() => { document.hasFocus = () => false; window.dispatchEvent(new Event('blur')); }")
        self.finish_reply(self.room, "unfocused-room-entry")
        await self.page.evaluate("refreshRooms()")
        self.assertEqual(await self.page.evaluate("replyNotifications.length"), 1)
        self.assertFalse(await self.page.evaluate("replyNotifications[0].closed"))
        seen_before = await self.page.evaluate("id => sidebarPreferences().seen[id]", self.room["id"])
        await self.page.evaluate("id => selectRoom(id, {route:false})", self.room["id"])
        self.assertTrue(await self.page.evaluate("replyNotifications[0].closed"))
        self.assertEqual(await self.page.evaluate("id => sidebarPreferences().seen[id]", self.room["id"]), seen_before)
        self.finish_reply(self.room, "later-unfocused-reply")
        await self.page.evaluate("refreshRooms()")
        self.assertEqual(await self.page.evaluate("replyNotifications.length"), 2)
        self.assertFalse(await self.page.evaluate("replyNotifications[1].closed"))
        await self.page.evaluate("refreshRooms()")
        self.assertFalse(await self.page.evaluate("replyNotifications[1].closed"))
        self.assertEqual(await self.page.evaluate("id => sidebarPreferences().seen[id]", self.room["id"]), seen_before)
        self.assertFalse(self.errors, self.errors)

    async def test_in_app_reply_alert_survives_timeout_then_hides_on_room_focus(self):
        await self.prepare_notifications("denied")
        self.finish_reply(self.background, "persistent-fallback")
        await self.page.evaluate("refreshRooms()")
        await expect(self.page.locator(".reply-notification")).to_be_visible()
        await self.page.clock.install()
        await self.page.clock.fast_forward(20000)
        await expect(self.page.locator(".reply-notification")).to_be_visible()
        await self.page.evaluate("id => selectRoom(id)", self.background["id"])
        await expect(self.page.locator(".reply-notification")).to_have_count(0)

    async def test_hidden_room_and_reconnection_alert_but_seen_replies_do_not(self):
        await self.prepare_notifications()
        self.finish_reply(self.room, "read-reply")
        await self.page.evaluate("async () => { await fetchEvents(state.selected); renderCurrent(); }")
        await self.page.evaluate("id => selectRoom(id)", self.background["id"])
        await self.page.evaluate("refreshRooms()")
        self.assertEqual(await self.page.evaluate("replyNotifications.length"), 0)
        await self.page.evaluate("Object.defineProperty(document, 'hidden', {configurable:true, value:true})")
        self.finish_reply(self.background, "hidden-reply")
        await self.page.evaluate("refreshRooms()")
        self.assertEqual(await self.page.evaluate("replyNotifications.length"), 1)
        await self.page.route("**/api/rooms", lambda route: route.abort())
        self.finish_reply(self.room, "offline-reply")
        await self.page.evaluate("refreshRooms().catch(() => {})")
        await self.page.unroute("**/api/rooms")
        await self.page.evaluate("refreshRooms()")
        self.assertEqual(await self.page.evaluate("replyNotifications.length"), 2)
        await self.page.evaluate("refreshRooms()")
        self.assertEqual(await self.page.evaluate("replyNotifications.length"), 2)
        self.assertFalse(self.errors, self.errors)

    async def test_permission_is_requested_from_send_gesture_not_page_load(self):
        await self.prepare_notifications("default")
        self.assertEqual(await self.page.evaluate("replyPermissionRequests"), 0)
        await self.page.locator("#message-input").fill("A response with optional notifications")
        await self.page.locator("#send-button").click()
        await self.wait_worker_turn()
        self.assertEqual(await self.page.evaluate("replyPermissionRequests"), 1)
        await self.page.evaluate("enableReplyNotifications({automatic:true})")
        self.assertEqual(await self.page.evaluate("replyPermissionRequests"), 1)
        self.assertFalse(self.errors, self.errors)

    async def test_two_tabs_share_alerts_and_suppress_the_room_visible_in_another_tab(self):
        await self.prepare_notifications()
        second = await self.context.new_page()
        second.on("pageerror", lambda error: self.errors.append(str(error)))
        await self.login(self.admin, page=second)
        await second.evaluate("id => selectRoom(id)", self.background["id"])
        await second.bring_to_front()
        await second.locator("#message-input").focus()
        # CODEX: Headless Chrome keeps both pages visible; model the first tab being in the background.
        await self.page.evaluate("Object.defineProperty(document, 'hidden', {configurable:true, value:true})")
        await second.evaluate("publishReplyView()")
        self.finish_reply(self.background, "read-in-other-tab")
        await self.page.evaluate("refreshRooms()")
        self.assertEqual(await self.page.evaluate("replyNotifications.length"), 0)
        await second.evaluate("refreshRooms()")
        self.assertEqual(await second.evaluate("replyNotifications.length"), 0)
        await second.evaluate("id => selectRoom(id)", self.room["id"])
        self.finish_reply(self.background, "one-shared-alert")
        await asyncio.gather(self.page.evaluate("refreshRooms()"), second.evaluate("refreshRooms()"))
        counts = await asyncio.gather(self.page.evaluate("replyNotifications.length"), second.evaluate("replyNotifications.length"))
        self.assertEqual(sum(counts), 1)
        await second.evaluate("id => selectRoom(id)", self.background["id"])
        await asyncio.gather(self.page.wait_for_function("replyNotifications.every(item => item.closed)"),
                             second.wait_for_function("replyNotifications.every(item => item.closed)"))
        self.assertFalse(self.errors, self.errors)

    async def test_admin_view_is_outside_the_selected_room(self):
        await self.prepare_notifications()
        await self.page.evaluate("openAdmin()")
        self.finish_reply(self.room, "reply-while-admin")
        await self.page.evaluate("refreshRooms()")
        self.assertEqual(await self.page.evaluate("replyNotifications.length"), 1)
        self.assertEqual(await self.page.evaluate("state.view"), "admin")
        self.assertFalse(self.errors, self.errors)


if __name__ == "__main__":
    unittest.main()
