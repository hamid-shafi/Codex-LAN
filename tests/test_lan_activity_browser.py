"""CODEX: Native browser activity rendering, recovery, and selection regression checks."""

from __future__ import annotations

import unittest

from playwright.async_api import expect

import test_lan_browser as browser_fixture


class LanActivityBrowserTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = browser_fixture.LanBrowserTests.asyncSetUp
    asyncTearDown = browser_fixture.LanBrowserTests.asyncTearDown
    start_worker = browser_fixture.LanBrowserTests.start_worker
    login = browser_fixture.LanBrowserTests.login
    wait_worker_turn = browser_fixture.LanBrowserTests.wait_worker_turn

    async def install_run(self, entries, status="running", cursor=100):
        await self.page.evaluate("""({entries, status, cursor}) => {
          const cache = roomState();
          applySnapshots(cache, [{id:'browser-log-run', status, activities:entries,
            activity:'Working…', output_text:'', last_event_id:cursor, created_at:Date.now()/1000}]);
          renderCurrent();
        }""", {"entries": entries, "status": status, "cursor": cursor})

    def command(self, **changes):
        return {"id": "command-1", "kind": "command", "title": "Run Python tests",
                "status": "running", "detail": "python -m unittest -v",
                "output": "test_layout ... ok\n", "started_at": 1700000000,
                "updated_at": 1700000001, **changes}

    async def test_action_lifecycle_levels_safe_text_copy_and_complete(self):
        page = self.page
        await self.login(self.admin)
        await expect(page.locator("#room-title")).to_have_text("Working together")
        await self.install_run([self.command()])
        panel = page.locator('.message[data-key="run:browser-log-run"] .run-activity')
        await expect(panel.locator(".activity-current-label")).to_have_text("Run Python tests")
        await expect(panel.locator(".activity-details")).not_to_have_attribute("open", "")
        await expect(panel.locator(".activity-counts")).to_have_text("1 running")
        await panel.locator(".activity-disclosure").click()
        await expect(panel.locator(".activity-step")).to_have_count(1)
        await expect(panel.locator(".activity-output")).not_to_be_visible()
        await panel.get_by_role("button", name="Output", exact=True).click()
        await expect(panel.locator(".activity-output")).to_be_visible()
        await expect(panel.locator(".activity-command")).to_have_text("python -m unittest -v")
        await self.install_run([self.command(status="failed", exit_code=1, duration_ms=81000,
                                             output="<img src=x onerror=alert(1)>\nFAILED: layout RTL")], cursor=101)
        await expect(panel.locator(".activity-step")).to_have_count(1)
        await expect(panel.locator(".activity-step-status")).to_have_text("failed · exit 1")
        await expect(panel.locator(".activity-step-duration")).to_have_text("1m21s")
        await expect(panel.locator(".activity-counts")).to_have_text("1 failed")
        await expect(panel.locator("img")).to_have_count(0)
        await page.evaluate("""() => {
          Object.defineProperty(navigator, 'clipboard', {configurable:true,
            value:{writeText:async text => {window.copiedActivity=text;}}});
        }""")
        await panel.get_by_role("button", name="Copy log").click()
        self.assertIn("[failed · exit 1 · 1m21s] Run Python tests", await page.evaluate("window.copiedActivity"))
        self.assertIn("FAILED: layout RTL", await page.evaluate("window.copiedActivity"))
        await panel.get_by_role("button", name="Summary", exact=True).click()
        await expect(panel.locator(".activity-overview")).to_be_visible()
        await expect(panel.locator(".activity-latest-line")).to_have_text("FAILED: layout RTL")
        await expect(panel.locator(".activity-latest-label")).to_have_text("Latest update")
        await expect(panel.locator(".activity-steps")).not_to_be_visible()
        await page.evaluate("""() => {
          const cache=roomState(), run=cache.runs.get('browser-log-run');
          run.status='failed'; run.error='Tests did not pass';
          mergeMessage(cache,{id:'browser-log-message',run_id:run.id,role:'assistant',content:'The failing test needs a fix.',created_at:Date.now()/1000});
          renderCurrent();
        }""")
        final = page.locator('.message[data-key="message:browser-log-message"] .run-activity')
        await expect(final.locator(".activity-details")).to_have_attribute("open", "")
        await expect(final.locator(".activity-overview")).to_be_visible()
        await expect(final.locator(".activity-dot")).not_to_be_visible()
        await expect(final.locator(".activity-counts")).to_have_text("1 failed")
        await expect(final.locator(".activity-latest-label")).to_have_text("Last activity")
        self.assertFalse(self.errors, self.errors)

    async def test_summary_latest_output_updates_without_replacing_selected_text(self):
        page = self.page
        await self.login(self.admin)
        await expect(page.locator("#room-title")).to_have_text("Working together")
        await self.install_run([self.command(output="First progress line\n")])
        panel = page.locator('.message[data-key="run:browser-log-run"] .run-activity')
        await panel.locator(".activity-disclosure").click()
        await panel.get_by_role("button", name="Summary", exact=True).click()
        await expect(panel.locator(".activity-latest-line")).to_have_text("First progress line")
        await self.install_run([self.command(output="First progress line\nLatest public result فارسی\n")], cursor=101)
        await expect(panel.locator(".activity-latest-line")).to_have_text("Latest public result فارسی")
        await expect(panel.locator(".activity-latest-line")).to_have_attribute("dir", "rtl")
        await page.evaluate("""() => {
            const node = document.querySelector('.activity-latest-line');
            const range = document.createRange(); range.selectNodeContents(node);
            getSelection().removeAllRanges(); getSelection().addRange(range);
            window.summarySelectedText = getSelection().toString();
            roomState().runs.get('browser-log-run').activities[0].output += '\\nOne newer result';
            renderCurrent();
        }""")
        self.assertTrue(await page.evaluate("getSelection().toString() === window.summarySelectedText"))
        await expect(panel.locator(".activity-latest-line")).to_have_attribute("dir", "rtl")
        await page.evaluate("() => { getSelection().removeAllRanges(); renderCurrent(); }")
        await expect(panel.locator(".activity-latest-line")).to_have_text("One newer result")
        await page.evaluate("""() => {
            roomState().runs.get('browser-log-run').latest_status_line = 'A newer public commentary update';
            renderCurrent();
        }""")
        await expect(panel.locator(".activity-latest-line")).to_have_text("A newer public commentary update")
        await page.evaluate("""() => {
            roomState().runs.get('browser-log-run').connection = {state:'reconnecting',mode:'proxy',attempt:1};
            renderCurrent();
        }""")
        await expect(panel.locator(".activity-latest-label")).to_have_text("Last activity")
        await expect(panel.locator(".activity-latest-line")).to_have_text("A newer public commentary update")
        self.assertFalse(self.errors, self.errors)

    async def test_public_commentary_without_tool_steps_has_a_stable_summary(self):
        page = self.page
        await self.login(self.admin)
        await expect(page.locator("#room-title")).to_have_text("Working together")
        await self.install_run([{"id": "private-reasoning", "kind": "reasoning", "title": "Private reasoning is not an activity"}])
        await page.evaluate("""() => {
            roomState().runs.get('browser-log-run').latest_status_line = 'I am checking the saved conversation.';
            renderCurrent();
        }""")
        panel = page.locator('.message[data-key="run:browser-log-run"] .run-activity')
        await expect(panel.locator(".activity-details")).to_be_visible()
        await panel.locator(".activity-disclosure").click()
        await panel.get_by_role("button", name="Summary", exact=True).click()
        await expect(panel.locator(".activity-latest-line")).to_have_text("I am checking the saved conversation.")
        await expect(panel).not_to_contain_text("Private reasoning")
        for width in (400, 1365):
            await page.set_viewport_size({"width": width, "height": 900})
            await page.evaluate("""() => {
                roomState().runs.get('browser-log-run').latest_status_line = 'A short update.';
                renderCurrent();
            }""")
            before = (await panel.locator(".activity-latest").bounding_box())["height"]
            await page.evaluate("""() => {
                roomState().runs.get('browser-log-run').latest_status_line = 'A long public update mixed with فارسی '.repeat(20);
                renderCurrent();
            }""")
            after = (await panel.locator(".activity-latest").bounding_box())["height"]
            self.assertLess(abs(after - before), 1)
            await expect(panel.locator(".activity-latest-line")).to_have_attribute("dir", "rtl")
            self.assertTrue(await page.evaluate("document.documentElement.scrollWidth <= innerWidth"))
        await page.evaluate("""() => {
            roomState().runs.get('browser-log-run').latest_status_line = 'Checking the updated layout — در حال بررسی نمایش گفتگو و حفظ متن انتخاب‌شده';
            renderCurrent();
        }""")
        await page.screenshot(path="/tmp/codex-lan-public-summary.png")
        await page.evaluate("""() => {
            mergeMessage(roomState(), {id:'saved-summary',run_id:'saved-public-run',role:'assistant',run_status:'completed',activities:[],content:'Completed response.',latest_status_line:'Saved public status from history'});
            renderCurrent();
        }""")
        history = page.locator('.message[data-key="message:saved-summary"] .run-activity')
        await expect(history.locator(".activity-details")).to_be_visible()
        await history.locator(".activity-disclosure").click()
        await history.get_by_role("button", name="Summary", exact=True).click()
        await expect(history.locator(".activity-latest-line")).to_have_text("Saved public status from history")
        await expect(history.locator(".activity-latest-label")).to_have_text("Last activity")
        self.assertFalse(self.errors, self.errors)

    async def test_selection_output_append_room_switch_and_reload_preferences(self):
        page = self.page
        await self.login(self.admin)
        await expect(page.locator("#room-title")).to_have_text("Working together")
        await self.install_run([self.command(output="\n".join(f"line {i}: فارسی English output" for i in range(100)))])
        panel = page.locator('.message[data-key="run:browser-log-run"] .run-activity')
        await panel.locator(".activity-disclosure").click()
        await panel.get_by_role("button", name="Output", exact=True).click()
        await page.evaluate("""() => {
          const output=document.querySelector('.activity-output');
          output.scrollTop=80; window.activityOutputNode=output;
          const range=document.createRange(); range.setStart(output.firstChild,0); range.setEnd(output.firstChild,20);
          getSelection().removeAllRanges(); getSelection().addRange(range);
          window.activitySelection=getSelection().toString();
          window.outputScroll=output.scrollTop;
          const run=roomState().runs.get('browser-log-run');
          run.activities[0].output+='\\nadditional result'; renderCurrent();
        }""")
        self.assertTrue(await page.evaluate("getSelection().toString()===window.activitySelection"))
        self.assertTrue(await page.evaluate("document.querySelector('.activity-output')===window.activityOutputNode"))
        self.assertEqual(await page.evaluate("document.querySelector('.activity-output').scrollTop"), await page.evaluate("window.outputScroll"))
        await page.evaluate("getSelection().removeAllRanges(); renderCurrent()")
        await expect(panel.locator(".activity-output")).to_contain_text("additional result")
        await page.evaluate("renderTimeline(true)")
        await expect(panel.locator(".activity-details")).to_have_attribute("open", "")
        await expect(panel.locator(".activity-step")).to_have_attribute("open", "")
        await page.reload()
        await expect(page.locator("#room-title")).to_have_text("Working together")
        await self.install_run([self.command(output="Recovered from server snapshot")])
        await expect(panel.locator(".activity-details")).to_have_attribute("open", "")
        await expect(panel.locator(".activity-step")).to_have_attribute("open", "")
        await expect(panel.get_by_role("button", name="Output", exact=True)).to_have_attribute("aria-pressed", "true")
        await expect(panel.locator(".activity-output")).to_have_text("Recovered from server snapshot")
        for width in (400, 1365):
            await page.set_viewport_size({"width": width, "height": 900})
            self.assertTrue(await page.evaluate("document.documentElement.scrollWidth<=innerWidth"))
        self.assertFalse(self.errors, self.errors)

    async def test_worker_events_survive_completion_and_reload(self):
        page = self.page
        other = self.service.store.create_room(self.admin_id, "Other room", "/home/server/other", "astra", "medium")
        await self.login(self.admin)
        await expect(page.locator("#room-title")).to_have_text("Other room")
        await page.evaluate("id => selectRoom(id)", self.room["id"])
        await expect(page.locator("#room-title")).to_have_text("Working together")
        await page.locator("#message-input").fill("Run the checks")
        await page.locator("#send-button").click()
        await self.wait_worker_turn()
        item = {"id": "real-command", "type": "commandExecution", "command": "python -m unittest -v", "status": "inProgress"}
        await self.worker.event("item/started", item=item)
        panel = page.locator('.message[data-key^="run:"] .run-activity')
        await expect(panel.locator(".activity-current-label")).to_have_text("python -m unittest -v", timeout=10000)
        await panel.locator(".activity-disclosure").click()
        await panel.get_by_role("button", name="Output", exact=True).click()
        await self.worker.event("item/commandExecution/outputDelta", itemId="real-command", delta="PASS layout\n")
        await expect(panel.locator(".activity-output")).to_have_text("PASS layout\n", timeout=10000)
        await page.evaluate("""() => {
          const output=document.querySelector('.message[data-key^="run:"] .activity-output');
          window.liveOutput=output;
          const range=document.createRange(); range.selectNodeContents(output);
          getSelection().removeAllRanges(); getSelection().addRange(range);
          window.liveSelection=getSelection().toString();
        }""")
        await self.worker.event("item/completed", item={**item, "status": "completed", "aggregatedOutput": "PASS layout\n", "exitCode": 0, "durationMs": 81000})
        await self.worker.delta("The checks passed.")
        await self.worker.complete()
        await expect(page.locator("#stop-button")).to_be_hidden(timeout=10000)
        final = page.locator('.message.assistant .run-activity').last
        await expect(final.locator(".activity-counts")).to_have_text("1 done")
        await expect(final.locator(".activity-output")).to_have_text("PASS layout\n")
        await expect(final.locator(".activity-dot")).not_to_be_visible()
        self.assertTrue(await page.evaluate("window.liveOutput.isConnected && getSelection().toString()===window.liveSelection"))
        await page.evaluate("getSelection().removeAllRanges(); scrollBottom()")
        await page.screenshot(path="/tmp/codex-lan-activity-log.png")
        await page.evaluate("id => selectRoom(id)", other["id"])
        await expect(page.locator("#room-title")).to_have_text("Other room")
        await page.evaluate("id => selectRoom(id)", self.room["id"])
        await expect(page.locator("#room-title")).to_have_text("Working together")
        await expect(final.locator(".activity-output")).to_have_text("PASS layout\n")
        await page.reload()
        await expect(page.locator("#room-title")).to_have_text("Working together")
        await expect(final.locator(".activity-output")).to_have_text("PASS layout\n")
        await expect(final.locator(".activity-step-status")).to_have_text("completed · exit 0")
        self.assertFalse(self.errors, self.errors)

    async def test_legacy_activity_is_compact_and_reasoning_is_not_a_log_entry(self):
        page = self.page
        await self.login(self.admin)
        await expect(page.locator("#room-title")).to_have_text("Working together")
        await self.install_run([{"id": "private", "kind": "reasoning", "title": "Unrendered reasoning"}])
        panel = page.locator('.message[data-key="run:browser-log-run"] .run-activity')
        await expect(panel.locator(".activity-current-label")).to_have_text("Waiting for the next update")
        await expect(panel.locator(".activity-details")).not_to_be_visible()
        await page.evaluate("""() => {
          roomState().runs.get('browser-log-run').activity='Reading project files'; renderCurrent();
        }""")
        await expect(panel.locator(".activity-current-label")).to_have_text("Reading project files")
        await expect(panel).not_to_contain_text("Unrendered reasoning")
        self.assertFalse(self.errors, self.errors)


if __name__ == "__main__":
    unittest.main()
