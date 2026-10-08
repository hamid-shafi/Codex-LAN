"""CODEX: Public progress archives survive completion and browser recovery without exposing markup."""

from __future__ import annotations

import unittest

from playwright.async_api import expect

import test_lan_browser as browser_fixture


class LanProgressBrowserTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = browser_fixture.LanBrowserTests.asyncSetUp
    asyncTearDown = browser_fixture.LanBrowserTests.asyncTearDown
    start_worker = browser_fixture.LanBrowserTests.start_worker
    login = browser_fixture.LanBrowserTests.login
    wait_worker_turn = browser_fixture.LanBrowserTests.wait_worker_turn

    async def install_run(self, entries, *, status="running", cursor=101, truncated=False):
        await self.page.evaluate(
            """({entries, status, cursor, truncated}) => {
              applySnapshots(roomState(), [{
                id: 'progress-browser-run', status, activities: [],
                progress_log: entries, progress_log_count: entries.length,
                progress_log_truncated: truncated, output_text: '',
                last_event_id: cursor, created_at: Date.now() / 1000
              }]);
              renderCurrent();
            }""",
            {"entries": entries, "status": status, "cursor": cursor, "truncated": truncated},
        )

    def entry(self, text="\u062f\u0631 \u062d\u0627\u0644 \u0628\u0631\u0631\u0633\u06cc \u0641\u0627\u06cc\u0644\u200c\u0647\u0627 \u0648 \u0627\u062c\u0631\u0627\u06cc \u062a\u0633\u062a\u200c\u0647\u0627", **changes):
        return {"id": "public-update", "text": text, "started_at": 1_700_000_000,
                "updated_at": 1_700_000_001, **changes}

    async def test_public_log_is_safe_accessible_and_copyable(self):
        page = self.page
        await self.login(self.admin)
        text = "\u062f\u0631 \u062d\u0627\u0644 \u0628\u0631\u0631\u0633\u06cc \u0641\u0627\u06cc\u0644\u200c\u0647\u0627\n<img src=x onerror=alert(1)>\nSecond line."
        await self.install_run([self.entry(text)], status="completed", truncated=True)
        await page.evaluate("""() => {
          mergeMessage(roomState(), {id: 'saved-progress-message', role: 'assistant',
            run_id: 'progress-browser-run', run_status: 'completed', content: 'Done.'});
          renderCurrent();
        }""")
        panel = page.locator('.message[data-key="message:saved-progress-message"] .run-activity')
        await expect(panel.locator(".activity-details")).to_be_visible()
        await expect(panel.locator(".activity-disclosure-label")).to_have_text("Activity · 1 update")
        await panel.locator(".activity-disclosure").click()
        activity = panel.get_by_role("tab", name="Activity", exact=True)
        progress = panel.get_by_role("tab", name="Progress log", exact=True)
        context = panel.get_by_role("tab", name="Prepared context", exact=True)
        await activity.focus()
        await activity.press("ArrowRight")
        await expect(progress).to_be_focused()
        await expect(progress).to_have_attribute("aria-selected", "true")
        await expect(panel.locator(".activity-progress-text")).to_have_text(text)
        await expect(panel.locator(".activity-progress-text")).to_have_attribute("dir", "rtl")
        await expect(panel.locator(".activity-progress img")).to_have_count(0)
        await expect(panel.locator(".activity-progress-truncation")).to_be_visible()
        await progress.press("ArrowRight")
        await expect(context).to_be_focused()
        await context.press("Home")
        await expect(activity).to_be_focused()
        await activity.press("ArrowLeft")
        await expect(context).to_be_focused()
        await context.press("ArrowLeft")
        await expect(progress).to_be_focused()
        await page.evaluate("""() => {
          Object.defineProperty(navigator, 'clipboard', {configurable: true,
            value: {writeText: async text => { window.progressCopied = text; }}});
        }""")
        await panel.get_by_role("button", name="Copy progress log", exact=True).click()
        copied = await page.evaluate("window.progressCopied")
        self.assertIn(text, copied)
        self.assertIn("Earlier updates or text omitted", copied)
        key = f"codex-lan-activity:{self.admin_id}:progress-browser-run"
        saved = await page.evaluate("key => sessionStorage.getItem(key)", key)
        self.assertIn('"tab":"progress"', saved)
        self.assertNotIn(text, saved)
        self.assertNotIn("progress_log", saved)
        for width in (400, 1365):
            await page.set_viewport_size({"width": width, "height": 900})
            self.assertTrue(await page.evaluate("document.documentElement.scrollWidth <= innerWidth"))
        await page.evaluate("""() => {
          const run = roomState().runs.get('progress-browser-run');
          run.progress_log = []; run.progress_log_count = 0; run.progress_log_truncated = false;
          run.activities = [{id: 'legacy-action', kind: 'command', title: 'Existing tool action', status: 'completed'}];
          renderCurrent();
        }""")
        await expect(panel.locator(".activity-progress-empty")).to_be_visible()
        await expect(panel.get_by_role("button", name="Copy progress log", exact=True)).to_be_disabled()
        self.assertFalse(self.errors, self.errors)

    async def test_log_selection_scroll_and_lightweight_snapshots_are_stable(self):
        page = self.page
        await self.login(self.admin)
        entries = [self.entry("\n".join(f"line {n}: \u0645\u062a\u0646 \u0641\u0627\u0631\u0633\u06cc English" for n in range(20)),
                              id=f"update-{number}") for number in range(12)]
        await self.install_run(entries)
        panel = page.locator('.message[data-key="run:progress-browser-run"] .run-activity')
        await panel.locator(".activity-disclosure").click()
        await panel.get_by_role("tab", name="Progress log", exact=True).click()
        stable = await page.evaluate("""() => {
          const list = document.querySelector('.activity-progress-list');
          const text = list.querySelector('.activity-progress-text');
          list.scrollTop = 120;
          const range = document.createRange(); range.setStart(text.firstChild, 0); range.setEnd(text.firstChild, 20);
          getSelection().removeAllRanges(); getSelection().addRange(range);
          const selected = getSelection().toString(), scroll = list.scrollTop;
          const run = roomState().runs.get('progress-browser-run');
          applySnapshots(roomState(), [{id: run.id, status: 'running', last_event_id: 102}]);
          run.progress_log[0] = {...run.progress_log[0], text: run.progress_log[0].text + '\\nNew update'};
          renderCurrent();
          return {selection: getSelection().toString() === selected,
            sameNode: list.querySelector('.activity-progress-text') === text,
            sameScroll: list.scrollTop === scroll, count: run.progress_log.length,
            bounded: list.clientHeight <= 480 && list.scrollHeight > list.clientHeight};
        }""")
        self.assertEqual(stable, {"selection": True, "sameNode": True, "sameScroll": True,
                                  "count": 12, "bounded": True})
        await page.evaluate("getSelection().removeAllRanges(); renderCurrent()")
        await expect(panel.locator(".activity-progress-text").first).to_contain_text("New update")
        result = await page.evaluate("""() => messageRun({
          id: 'final-progress-message', role: 'assistant', run_id: 'progress-browser-run',
          run_status: 'completed', content: 'Done', progress_log: [{id: 'final-log', text: 'Final retained log'}],
          progress_log_count: 1, progress_log_truncated: false
        }, roomState()).progress_log""")
        self.assertEqual(result, [{"id": "final-log", "text": "Final retained log"}])
        cleared = await page.evaluate("""() => messageRun({
          id: 'final-without-progress', role: 'assistant', run_id: 'progress-browser-run',
          run_status: 'completed', content: 'Final answer only', progress_log: [],
          progress_log_count: 0, progress_log_truncated: false
        }, roomState()).progress_log""")
        self.assertEqual(cleared, [])
        self.assertFalse(self.errors, self.errors)

    async def test_worker_progress_survives_final_answer_room_switch_and_reload(self):
        page = self.page
        other = self.service.store.create_room(self.admin_id, "Other room", "/home/server/other", "astra", "medium")
        await self.login(self.admin)
        await page.evaluate("id => selectRoom(id)", self.room["id"])
        await expect(page.locator("#room-title")).to_have_text("Working together")
        await page.locator("#message-input").fill("Check the existing files")
        await page.locator("#send-button").click()
        await self.wait_worker_turn()
        commentary = {"id": "comment-one", "type": "agentMessage", "phase": "commentary"}
        await self.worker.event("item/started", item=commentary)
        await self.worker.event("item/agentMessage/delta", itemId="comment-one", delta="\u062f\u0631 \u062d\u0627\u0644 \u0628\u0631\u0631\u0633\u06cc \u0641\u0627\u06cc\u0644\u200c\u0647\u0627\u06cc \u067e\u0631\u0648\u0698\u0647")
        await self.worker.event("item/completed", item={**commentary, "text": "\u062f\u0631 \u062d\u0627\u0644 \u0628\u0631\u0631\u0633\u06cc \u0641\u0627\u06cc\u0644\u200c\u0647\u0627\u06cc \u067e\u0631\u0648\u0698\u0647"})
        second = {"id": "comment-two", "type": "agentMessage", "phase": "commentary"}
        await self.worker.event("item/completed", item={**second, "text": "The checks finished successfully."})
        final_item = {"id": "answer", "type": "agentMessage", "phase": "final_answer"}
        await self.worker.event("item/started", item=final_item)
        await self.worker.delta("Final answer only.")
        await self.worker.event("item/completed", item={**final_item, "text": "Final answer only."})
        await self.worker.complete()
        await expect(page.locator("#stop-button")).to_be_hidden(timeout=10000)
        article = page.locator(".message.assistant").last
        await expect(article.locator(".message-content")).to_have_text("Final answer only.")
        panel = article.locator(".run-activity")
        await panel.locator(".activity-disclosure").click()
        await panel.get_by_role("tab", name="Progress log", exact=True).click()
        await expect(panel.locator(".activity-progress-entry")).to_have_count(2)
        await expect(panel.locator(".activity-progress-text").first).to_have_text("\u062f\u0631 \u062d\u0627\u0644 \u0628\u0631\u0631\u0633\u06cc \u0641\u0627\u06cc\u0644\u200c\u0647\u0627\u06cc \u067e\u0631\u0648\u0698\u0647")
        await expect(panel.locator(".activity-progress-text").last).to_have_text("The checks finished successfully.")
        await expect(panel.locator(".activity-progress")).not_to_contain_text("Final answer only.")
        await page.evaluate("id => selectRoom(id)", other["id"])
        await expect(page.locator("#room-title")).to_have_text("Other room")
        await page.evaluate("id => selectRoom(id)", self.room["id"])
        await expect(panel.locator(".activity-progress-entry")).to_have_count(2)
        await page.reload()
        await expect(page.locator("#room-title")).to_have_text("Working together")
        await expect(panel.get_by_role("tab", name="Progress log", exact=True)).to_have_attribute("aria-selected", "true")
        await expect(panel.locator(".activity-progress-entry")).to_have_count(2)
        await expect(panel.locator(".activity-progress-text").first).to_have_attribute("dir", "rtl")
        self.assertFalse(self.errors, self.errors)

    async def test_incremental_updates_and_same_poll_terminal_summary_are_retained(self):
        page = self.page
        await self.login(self.admin)
        result = await page.evaluate("""async () => {
          clearTimeout(state.eventTimer);
          const cache = roomState(), originalApi = api;
          cache.runs.set('progress-browser-run', {id: 'progress-browser-run', status: 'running', last_event_id: 0});
          let response = {runs: [], events: [{id: 1, run_id: 'progress-browser-run', kind: 'progress', payload: {
            progress_entry: {id: 'comment', text: 'First public update'},
            progress_removed_ids: [], progress_log_count: 1, progress_log_truncated: false
          }}]};
          api = async path => path.includes('/events?') ? response : path.endsWith('/messages') ? {messages: []} : originalApi(path);
          try {
            await fetchEvents(state.selected);
            const first = cache.runs.get('progress-browser-run').progress_log.map(entry => entry.text);
            response = {runs: [], events: [{id: 2, run_id: 'progress-browser-run', kind: 'progress', payload: {
              progress_entry: {id: 'second', text: 'Second public update'},
              progress_removed_ids: ['comment'], progress_log_count: 2, progress_log_truncated: true
            }}]};
            await fetchEvents(state.selected);
            const incremental = cache.runs.get('progress-browser-run').progress_log.map(entry => entry.text);
            response = {runs: [{id: 'progress-browser-run', status: 'completed', last_event_id: 4}], events: [
              {id: 3, run_id: 'progress-browser-run', kind: 'activity_summary', payload: {
                progress_log: [{id: 'kept', text: 'Authoritative final update'}],
                progress_log_count: 1, progress_log_truncated: false
              }},
              {id: 4, run_id: 'progress-browser-run', kind: 'completed', payload: {status: 'completed', message: {
                id: 'race-final-message', role: 'assistant', run_id: 'progress-browser-run', content: 'Done'
              }}}
            ]};
            await fetchEvents(state.selected);
            return {first, incremental, final: cache.runs.get('progress-browser-run').progress_log.map(entry => entry.text),
              truncated: cache.runs.get('progress-browser-run').progress_log_truncated};
          } finally { api = originalApi; }
        }""")
        self.assertEqual(result, {"first": ["First public update"], "incremental": ["Second public update"],
                                  "final": ["Authoritative final update"], "truncated": False})
        self.assertFalse(self.errors, self.errors)


if __name__ == "__main__":
    unittest.main()
