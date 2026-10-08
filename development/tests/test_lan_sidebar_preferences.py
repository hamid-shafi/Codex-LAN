"""CODEX: Fixed pins, activity-ordered rooms, and restrained reply state in Chromium."""

import re
import unittest

from playwright.async_api import expect
import test_lan_browser as fixture


class SidebarPreferenceTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.LanBrowserTests.asyncSetUp
    asyncTearDown = fixture.LanBrowserTests.asyncTearDown
    start_worker = fixture.LanBrowserTests.start_worker
    login = fixture.LanBrowserTests.login
    wait_worker_turn = fixture.LanBrowserTests.wait_worker_turn

    def extra_room(self):
        return self.service.store.create_room(self.admin_id, "Other work", "/home/project", "astra", "medium")

    def complete(self, room, text="New answer"):
        created = self.service.store.create_run(room["owner_id"], room["id"], "Question", [], "sidebar-" + text)
        return self.service.store.finish_run(created["run"]["id"], "completed", text)

    async def ready(self):
        await self.page.goto(self.base + "?room=" + self.room["id"] + "#login=" + self.admin["token"])
        await expect(self.page.locator("#room-title")).to_have_text("Working together")
        await self.page.wait_for_function("() => roomState().loaded && !roomState().loading")

    def row(self, room):
        return self.page.locator(f".room-row[data-room-id='{room['id']}']")

    async def order(self):
        return await self.page.locator(".room-row").evaluate_all("rows => rows.map(row => row.dataset.roomId)")

    async def sidebar_structure(self):
        return await self.page.locator("#room-list").evaluate("""list => [...list.children].map(node =>
            node.classList.contains('room-group-label')
              ? ['group', node.dataset.roomGroup, node.textContent.trim()]
              : ['room', node.dataset.roomId]
        )""")

    async def test_own_rooms_are_grouped_before_other_devices_with_local_pin_and_recency_order(self):
        own_older = self.service.store.create_room(
            self.admin_id, "Own older recent", "/home/own/older", "astra", "medium"
        )
        foreign_older = self.service.store.create_room(
            self.client_id, "Foreign older recent", "/home/foreign/older", "astra", "medium"
        )
        own_pinned_later = self.service.store.create_room(
            self.admin_id, "Own later pinned", "/home/own/pinned", "astra", "medium"
        )
        foreign_pinned_later = self.service.store.create_room(
            self.client_id, "Foreign later pinned", "/home/foreign/pinned", "astra", "medium"
        )
        own_newer = self.service.store.create_room(
            self.admin_id, "Own newest recent", "/home/own/newest", "astra", "medium"
        )
        foreign_newer = self.service.store.create_room(
            self.client_id, "Foreign newest recent", "/home/foreign/newest", "astra", "medium"
        )
        await self.ready()
        for room in (self.room, own_pinned_later, self.foreign, foreign_pinned_later):
            await self.row(room).locator(".room-pin").click()

        self.assertEqual(await self.sidebar_structure(), [
            ["group", "own-pinned", "Your pinned"],
            ["room", self.room["id"]],
            ["room", own_pinned_later["id"]],
            ["group", "own-recent", "Your rooms"],
            ["room", own_newer["id"]],
            ["room", own_older["id"]],
            ["group", "other-recent", "Other rooms"],
            ["room", self.foreign["id"]],
            ["room", foreign_pinned_later["id"]],
            ["room", foreign_newer["id"]],
            ["room", foreign_older["id"]],
        ])

        await self.row(foreign_newer).locator(".room-pin").click()
        self.assertEqual((await self.order())[:4], [
            self.room["id"], own_pinned_later["id"], own_newer["id"], own_older["id"]
        ])
        self.assertEqual(await self.page.locator(".room-group-label").evaluate_all(
            "labels => labels.map(label => label.dataset.roomGroup)"
        ), ["own-pinned", "own-recent", "other-recent"])

        await self.page.locator("#new-room").click()
        await expect(self.page.locator("#room-title")).to_have_text("New chat")
        await self.page.locator("#room-menu").click()
        await self.page.locator("#room-name-input").fill("Brand new own room")
        await self.page.locator("#room-save").click()
        await expect(self.page.locator("#room-title")).to_have_text("Brand new own room")
        new_id = await self.page.evaluate("state.selected")
        expected_after_creation = [
            self.room["id"], own_pinned_later["id"], new_id, own_newer["id"], own_older["id"]
        ]
        self.assertEqual((await self.order())[:5], expected_after_creation)
        other_start = await self.page.locator(".room-group-label[data-room-group='other-recent']").evaluate(
            "label => [...label.parentElement.children].indexOf(label)"
        )
        new_position = await self.row({"id": new_id}).evaluate(
            "row => [...row.parentElement.children].indexOf(row)"
        )
        self.assertLess(new_position, other_start)

        stable_order = await self.order()
        self.complete(foreign_older, "Foreign activity must not mix ownership groups")
        await self.page.evaluate("async () => await refreshRooms()")
        self.assertEqual(await self.order(), stable_order)
        await self.page.reload()
        await expect(self.page.locator("#room-title")).to_have_text("Brand new own room")
        self.assertEqual(await self.order(), stable_order)

        await self.page.locator("#room-search").fill("Foreign older recent")
        await expect(self.page.locator("#room-list .room-row")).to_have_count(1)
        await expect(self.row(foreign_older)).to_be_visible()
        self.assertEqual(await self.sidebar_structure(), [
            ["group", "other-recent", "Other rooms"],
            ["room", foreign_older["id"]],
        ])
        self.assertFalse(self.errors, self.errors)

    async def test_message_activity_lifts_unpinned_rooms_while_pins_survive_reload(self):
        other = self.extra_room()
        await self.ready()
        order = await self.order()
        self.assertEqual(order, [other["id"], self.room["id"], self.foreign["id"]])
        await self.page.evaluate("roomId => { window.beforeRow = document.querySelector(`.room-row[data-room-id='${roomId}']`); window.beforeSpinner = window.beforeRow.querySelector('.running-dot'); }", self.room["id"])
        self.service.store.update_room(self.admin_id, self.room["id"], title="Renamed older room")
        self.complete(self.room)
        await self.page.evaluate("async () => { await refreshRooms(); renderRooms(); }")
        active_order = [self.room["id"], other["id"], self.foreign["id"]]
        self.assertEqual(await self.order(), active_order)
        self.assertTrue(await self.page.evaluate("roomId => { const row = document.querySelector(`.room-row[data-room-id='${roomId}']`); return window.beforeRow === row && window.beforeSpinner === row.querySelector('.running-dot'); }", self.room["id"]))
        await self.row(other).locator(".room-pin").click()
        self.assertEqual((await self.order())[0], other["id"])
        await expect(self.row(other).locator(".room-pin")).to_have_attribute("aria-pressed", "true")
        self.assertEqual(await self.page.locator("#room-title").inner_text(), "Renamed older room")
        await self.page.reload()
        await expect(self.row(other).locator(".room-pin")).to_have_attribute("aria-pressed", "true")
        self.assertEqual((await self.order())[0], other["id"])
        await self.row(other).locator(".room-pin").click()
        self.assertEqual(await self.order(), active_order)
        self.assertFalse(self.errors, self.errors)

    async def test_background_reply_marker_and_read_state_survive_reload(self):
        other = self.extra_room()
        await self.ready()
        await expect(self.page.locator(".unread-dot:visible")).to_have_count(0)
        self.complete(other)
        await self.page.evaluate("async () => await refreshRooms()")
        await expect(self.row(other)).to_have_class(re.compile(r"\bunread\b"))
        await expect(self.row(other).locator(".running-dot")).to_be_hidden()
        await expect(self.row(other).locator(".unread-dot")).to_be_visible()
        await self.page.reload()
        await expect(self.row(other)).to_have_class(re.compile(r"\bunread\b"))
        await expect(self.row(other).locator(".unread-dot")).to_be_visible()
        await self.row(other).locator(".room-item").click()
        await expect(self.page.locator("#timeline")).to_contain_text("New answer")
        await expect(self.row(other)).not_to_have_class(re.compile(r"\bunread\b"))
        await expect(self.row(other).locator(".unread-dot")).to_be_hidden()
        await self.page.reload()
        await expect(self.row(other)).not_to_have_class(re.compile(r"\bunread\b"))
        await expect(self.row(other).locator(".unread-dot")).to_be_hidden()
        self.assertFalse(self.errors, self.errors)

    async def test_new_room_appears_below_fixed_pins_then_yields_to_newer_message_activity(self):
        older_unpinned = self.extra_room()
        later_pin = self.service.store.create_room(
            self.admin_id, "Another pinned room", "/home/pinned", "astra", "medium"
        )
        await self.ready()
        await self.row(later_pin).locator(".room-pin").click()
        await self.row(self.room).locator(".room-pin").click()
        pinned_order = await self.page.locator(".room-row.pinned").evaluate_all(
            "rows => rows.map(row => row.dataset.roomId)"
        )
        self.assertEqual(pinned_order, [self.room["id"], later_pin["id"]])
        await self.page.locator("#new-room").click()
        await expect(self.page.locator("#room-title")).to_have_text("New chat")
        await self.page.locator("#room-menu").click()
        await self.page.locator("#room-name-input").fill("Newest room")
        await self.page.locator("#room-save").click()
        await expect(self.page.locator("#room-title")).to_have_text("Newest room")
        new_id = await self.page.evaluate("state.selected")
        expected = [*pinned_order, new_id, older_unpinned["id"], self.foreign["id"]]
        self.assertEqual(await self.order(), expected)
        self.service.store.update_room(self.admin_id, older_unpinned["id"], title="Older room renamed")
        self.service.store.update_room(self.admin_id, self.room["id"], effort="ultra")
        self.complete(older_unpinned, "Recent reply in the oldest unpinned room")
        await self.page.evaluate("async () => await refreshRooms()")
        moved = [*pinned_order, older_unpinned["id"], new_id, self.foreign["id"]]
        self.assertEqual(await self.order(), moved)
        await expect(self.row(older_unpinned)).to_have_class(re.compile(r"\bunread\b"))
        await expect(self.row(older_unpinned).locator(".unread-dot")).to_be_visible()
        await expect(self.row(self.foreign).locator(".unread-dot")).to_be_hidden()
        await self.page.reload()
        await expect(self.page.locator("#room-title")).to_have_text("Newest room")
        self.assertEqual(await self.order(), moved)
        for pinned in (self.room, later_pin):
            await expect(self.row(pinned).locator(".room-pin")).to_have_attribute("aria-pressed", "true")
        self.assertFalse(self.errors, self.errors)

    async def test_settings_and_user_messages_do_not_trigger_unread(self):
        other = self.extra_room()
        newest = self.service.store.create_room(
            self.admin_id, "Newest untouched", "/home/newest", "astra", "medium"
        )
        await self.ready()
        original = await self.order()
        self.assertLess(original.index(newest["id"]), original.index(other["id"]))
        self.service.store.update_room(self.admin_id, other["id"], title="Updated title", effort="ultra")
        await self.page.evaluate("async () => await refreshRooms()")
        self.assertEqual(await self.order(), original)
        self.service.store.create_run(self.admin_id, other["id"], "Only a user message", [], "pending")
        await self.page.evaluate("async () => await refreshRooms()")
        self.assertLess((await self.order()).index(other["id"]), (await self.order()).index(newest["id"]))
        await expect(self.row(other)).not_to_have_class(re.compile(r"\bunread\b"))
        await expect(self.page.locator(".unread-dot:visible")).to_have_count(0)
        self.assertFalse(self.errors, self.errors)

    async def test_reading_older_text_or_hidden_tab_does_not_clear_new_reply(self):
        await self.ready()
        await self.page.evaluate("() => { state.follow = false; }")
        answer = self.complete(self.room, "While reading older text")
        await self.page.evaluate("message => { mergeMessage(roomState(), message); renderRooms(); }", answer)
        await expect(self.row(self.room)).to_have_class(re.compile(r"\bunread\b"))
        await self.page.evaluate("() => { Object.defineProperty(document, 'hidden', {value: true, configurable: true}); state.follow = true; renderRooms(); }")
        await expect(self.row(self.room)).to_have_class(re.compile(r"\bunread\b"))
        await self.page.evaluate("() => { Object.defineProperty(document, 'hidden', {value: false, configurable: true}); renderRooms(); }")
        await expect(self.row(self.room)).not_to_have_class(re.compile(r"\bunread\b"))
        self.assertFalse(self.errors, self.errors)

    async def test_first_prompt_replaces_new_chat_title_immediately(self):
        await self.ready()
        await self.page.locator("#new-room").click()
        await expect(self.page.locator("#room-title")).to_have_text("New chat")
        room_id = await self.page.evaluate("state.selected")
        title = '\u0628\u0631\u0631\u0633\u06cc \u0645\u0634\u06a9\u0644 \u0627\u062a\u0635\u0627\u0644 \u0645\u06a9\u0631\u0631 \u0633\u0631\u0648\u06cc\u0633'
        await self.page.locator("#message-input").fill(title)
        await self.page.locator("#message-input").press("Enter")
        await self.page.locator("#message-input").press("Enter")
        await expect(self.page.locator("#room-title")).to_have_text(title)
        await expect(self.row({"id": room_id}).locator(".room-item-title")).to_have_text(title)
        await self.wait_worker_turn()
        await self.worker.complete()
        self.assertFalse(self.errors, self.errors)

    async def test_pins_are_personal_to_the_device_and_work_without_storage(self):
        await self.ready()
        await self.row(self.foreign).locator(".room-pin").click()
        await expect(self.row(self.foreign).locator(".room-pin")).to_have_attribute("aria-pressed", "true")
        await self.login(self.client)
        await expect(self.row(self.foreign).locator(".room-pin")).to_have_attribute("aria-pressed", "false")
        await self.page.evaluate("() => { Storage.prototype.setItem = () => { throw new Error('Storage denied'); }; }")
        await self.row(self.foreign).locator(".room-pin").click()
        await expect(self.row(self.foreign).locator(".room-pin")).to_have_attribute("aria-pressed", "true")
        self.assertFalse(self.errors, self.errors)


if __name__ == "__main__":
    unittest.main()
