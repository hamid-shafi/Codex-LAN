"""CODEX: Prepared-context Activity tab rendering, preferences, copy, and accessibility regressions."""

from __future__ import annotations

import unittest

from playwright.async_api import expect

import test_lan_browser as browser_fixture


class LanPreparedContextBrowserTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = browser_fixture.LanBrowserTests.asyncSetUp
    asyncTearDown = browser_fixture.LanBrowserTests.asyncTearDown
    login = browser_fixture.LanBrowserTests.login
    start_worker = browser_fixture.LanBrowserTests.start_worker

    def overview(self):
        return {
            "version": 1,
            "scope": "locally_observed",
            "captured_at": 1_700_000_000,
            "thread": {
                "mode": "resumed",
                "history_managed_by": "codex",
                "exact_prompt_available": False,
            },
            "input": {
                "source": "selected_parent_context",
                "characters": 417,
                "lines": 9,
                "images": 2,
                "truncated": True,
            },
            "instructions": {
                "count": 2,
                "files": ["/home/server/project/AGENTS.md", r"C:\private\SKILL.md"],
                "truncated": False,
            },
            "turn": {
                "model": "astra",
                "effort": "ultra",
                "speed": "fast",
                "access": "read_only",
                "workspace": "/home/server/project",
                "approval_policy": "never",
                "service_tier": "fast",
            },
            "limits": {
                "new_context_tokens": 64_000,
                "scope": "body_after_prefix",
                "tool_output_tokens": 12_000,
            },
            "compaction": {"events": 3, "contents": "opaque"},
            "usage": {
                "input_tokens": 2_400,
                "cached_input_tokens": 800,
                "output_tokens": 320,
                "total_tokens": 2_720,
                "model_context_window": 128_000,
            },
            "content": [
                {
                    "kind": "turn_input",
                    "label": "Turn input",
                    "source": "selected_parent_context",
                    "text": "Explain this selected passage.\n<script>not markup</script>",
                    "truncated": True,
                },
                {
                    "kind": "visible_room_history",
                    "label": "Visible room history · reference only",
                    "source": "local_message_store",
                    "text": "User: Earlier room question\nAssistant: Earlier visible answer",
                    "truncated": False,
                },
                {
                    "kind": "instruction",
                    "label": "AGENTS.md",
                    "source": "app_server_instruction_source",
                    "text": "Keep changes scoped and run the relevant tests.",
                    "truncated": False,
                },
            ],
            "raw_prompt": "RAW PROMPT CONTENT",
            "reasoning": "PRIVATE REASONING",
        }

    async def install_run(self, *, overview, activities=None, status="running"):
        await self.page.evaluate(
            """({overview, activities, status}) => {
              const cache = roomState();
              applySnapshots(cache, [{
                id: 'prepared-context-run', status,
                context_overview: overview, activities: activities || [],
                activity: 'Working…', output_text: '',
                last_event_id: 101, created_at: Date.now() / 1000
              }]);
              renderCurrent();
            }""",
            {"overview": overview, "activities": activities or [], "status": status},
        )

    async def test_prepared_context_tab_is_accessible_bounded_and_copyable(self):
        page = self.page
        await self.login(self.admin)
        await expect(page.locator("#room-title")).to_have_text("Working together")
        await self.install_run(overview=self.overview())

        panel = page.locator('.message[data-key="run:prepared-context-run"] .run-activity')
        await expect(panel).to_be_visible()
        await panel.locator(".activity-disclosure").click()
        tabs = panel.locator(".activity-tabs")
        await expect(tabs).to_have_attribute("role", "tablist")
        activity_tab = tabs.get_by_role("tab", name="Activity", exact=True)
        context_tab = tabs.get_by_role("tab", name="Prepared context", exact=True)
        await expect(activity_tab).to_have_attribute("aria-selected", "true")
        await expect(context_tab).to_have_attribute("aria-selected", "false")

        await activity_tab.focus()
        await activity_tab.press("ArrowRight")
        progress_tab = tabs.get_by_role("tab", name="Progress log", exact=True)
        await expect(progress_tab).to_have_attribute("aria-selected", "true")
        await expect(progress_tab).to_be_focused()
        await progress_tab.press("ArrowRight")
        await expect(context_tab).to_have_attribute("aria-selected", "true")
        await expect(activity_tab).to_have_attribute("aria-selected", "false")
        await expect(context_tab).to_be_focused()
        context_panel = panel.locator(".activity-context")
        await expect(context_panel).to_have_attribute("role", "tabpanel")
        await expect(context_panel).to_be_visible()
        context_id = await context_tab.get_attribute("aria-controls")
        self.assertTrue(context_id)
        self.assertEqual(await context_panel.get_attribute("id"), context_id)
        await expect(context_panel).to_contain_text(
            "The provider’s exact serialized prompt, hidden prompts, hidden reasoning, and opaque compacted memory are unavailable."
        )
        for visible in (
            "Explain this selected passage.",
            "<script>not markup</script>",
            "Earlier visible answer",
            "Keep changes scoped",
            "Turn input",
            "Room history reference",
            "Instruction",
            "Source: Selected parent context",
            "Source: Local room history",
            "Source: Codex instruction source",
            "Truncated",
        ):
            await expect(context_panel).to_contain_text(visible)
        await expect(context_panel.locator(".activity-context-block")).to_have_count(3)
        await expect(context_panel.locator("script")).to_have_count(0)
        await expect(context_panel.locator(".activity-context-metadata")).not_to_have_attribute("open", "")
        await expect(context_panel.get_by_role("progressbar")).to_have_count(0)
        await expect(context_panel).not_to_contain_text("reported input tokens of")
        for hidden in (
            "/home/server/project/AGENTS.md",
            "RAW PROMPT CONTENT",
            "PRIVATE REASONING",
        ):
            await expect(context_panel).not_to_contain_text(hidden)

        await page.evaluate(
            """() => {
              Object.defineProperty(navigator, 'clipboard', {configurable: true,
                value: {writeText: async text => { window.copiedPreparedContext = text; }}});
            }"""
        )
        await panel.get_by_role("button", name="Copy prepared content", exact=True).click()
        copied = await page.evaluate("window.copiedPreparedContext")
        self.assertIn("Explain this selected passage.", copied)
        self.assertIn("<script>not markup</script>", copied)
        self.assertIn("Earlier visible answer", copied)
        self.assertIn("AGENTS.md", copied)
        self.assertIn("[truncated]", copied)
        self.assertNotIn("astra", copied)
        self.assertNotIn("2,400", copied)
        self.assertNotIn("exact serialized prompt", copied)
        self.assertNotIn("/home/server", copied)
        self.assertNotIn("RAW PROMPT CONTENT", copied)
        self.assertNotIn("PRIVATE REASONING", copied)
        self.assertFalse(self.errors, self.errors)

    async def test_metadata_update_without_content_preserves_visible_blocks(self):
        await self.login(self.admin)
        result = await self.page.evaluate(
            """overview => {
              const cache = roomState();
              applySnapshots(cache, [{
                id: 'content-merge-run', status: 'running', last_event_id: 3,
                context_overview: overview
              }]);
              applySnapshots(cache, [{
                id: 'content-merge-run', status: 'running', last_event_id: 4,
                context_overview: {usage: {total_tokens: 999}}
              }]);
              return cache.runs.get('content-merge-run').context_overview;
            }""",
            self.overview(),
        )
        self.assertEqual(len(result["content"]), 3)
        self.assertEqual(result["usage"]["total_tokens"], 999)
        self.assertEqual(result["usage"]["input_tokens"], 2_400)
        self.assertFalse(self.errors, self.errors)

    async def test_terminal_message_context_wins_over_stale_live_snapshot(self):
        await self.login(self.admin)
        result = await self.page.evaluate(
            """({stale, final}) => {
              const cache = roomState();
              cache.runs.set('final-context-run', {
                id: 'final-context-run', status: 'running', context_overview: stale
              });
              return messageRun({
                id: 'final-context-message', role: 'assistant', run_id: 'final-context-run',
                run_status: 'completed', content: 'Done', context_overview: final
              }, cache).context_overview;
            }""",
            {
                "stale": {**self.overview(), "compaction": {"events": 1, "contents": "opaque"}},
                "final": {**self.overview(), "compaction": {"events": 4, "contents": "opaque"}},
            },
        )
        self.assertEqual(result["compaction"]["events"], 4)
        self.assertFalse(self.errors, self.errors)

    async def test_context_preference_is_independent_and_survives_reload(self):
        page = self.page
        await self.login(self.admin)
        entry = {
            "id": "command-one",
            "kind": "command",
            "title": "Run checks",
            "status": "completed",
            "output": "all checks passed",
        }
        await self.install_run(overview=self.overview(), activities=[entry])
        panel = page.locator('.message[data-key="run:prepared-context-run"] .run-activity')
        await panel.locator(".activity-disclosure").click()
        await panel.get_by_role("button", name="Output", exact=True).click()
        await panel.get_by_role("tab", name="Prepared context", exact=True).click()

        key = f"codex-lan-activity:{self.admin_id}:prepared-context-run"
        saved = await page.evaluate("key => JSON.parse(sessionStorage.getItem(key))", key)
        self.assertEqual(saved["tab"], "context")
        self.assertEqual(saved["level"], "output")

        await page.reload()
        await expect(page.locator("#room-title")).to_have_text("Working together")
        await self.install_run(overview=self.overview(), activities=[entry])
        panel = page.locator('.message[data-key="run:prepared-context-run"] .run-activity')
        await expect(panel.locator(".activity-details")).to_have_attribute("open", "")
        await expect(panel.get_by_role("tab", name="Prepared context", exact=True)).to_have_attribute(
            "aria-selected", "true"
        )
        await expect(panel.locator(".activity-context")).to_be_visible()
        await panel.get_by_role("tab", name="Activity", exact=True).click()
        await expect(panel.get_by_role("button", name="Output", exact=True)).to_have_attribute(
            "aria-pressed", "true"
        )
        await expect(panel.locator(".activity-output")).to_have_text("all checks passed")
        self.assertFalse(self.errors, self.errors)

    async def test_legacy_run_has_clear_unavailable_context_state(self):
        page = self.page
        await self.login(self.admin)
        await self.install_run(
            overview=None,
            activities=[{
                "id": "legacy-command",
                "kind": "command",
                "title": "Legacy action",
                "status": "completed",
            }],
        )
        panel = page.locator('.message[data-key="run:prepared-context-run"] .run-activity')
        await panel.locator(".activity-disclosure").click()
        await panel.get_by_role("tab", name="Prepared context", exact=True).click()
        await expect(panel.locator(".activity-context-empty")).to_have_text(
            "Prepared context details are not available for this response."
        )
        self.assertFalse(self.errors, self.errors)


if __name__ == "__main__":
    unittest.main()
