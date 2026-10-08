"""CODEX: Untrusted Markdown stays inert and cannot exhaust the browser renderer."""

import unittest

from playwright.async_api import expect

import test_lan_browser as browser_fixtures


class MarkdownSecurityBrowserTests(unittest.IsolatedAsyncioTestCase):
    asyncTearDown = browser_fixtures.LanBrowserTests.asyncTearDown
    start_worker = browser_fixtures.LanBrowserTests.start_worker

    async def asyncSetUp(self):
        await browser_fixtures.LanBrowserTests.asyncSetUp(self)
        await self.page.goto(self.base)
        await self.page.wait_for_function("typeof markdown === 'function'")
        await self.page.evaluate("""() => {
            const root = document.createElement('div');
            root.id = 'markdown-security-fixture';
            document.body.prepend(root);
        }""")

    async def render(self, source):
        await self.page.evaluate("""source => {
            document.querySelector('#markdown-security-fixture').replaceChildren(markdown(source));
        }""", source)
        return self.page.locator("#markdown-security-fixture")

    async def test_deep_quotes_keep_the_remaining_source_without_stack_overflow(self):
        tail = " Persian \u0641\u0627\u0631\u0633\u06cc <img src=x onerror=alert(1)> [unsafe](javascript:alert)"
        source = ">" * 8192 + tail
        root = await self.render(source)
        await expect(root.locator("blockquote")).to_have_count(16)
        await expect(root.locator("img, script, iframe, a, [onerror]")).to_have_count(0)
        visible = await root.text_content()
        self.assertEqual(visible, ">" * (8192 - 16) + tail)
        self.assertFalse(self.errors)

    async def test_normal_nested_quotes_and_links_remain_formatted(self):
        root = await self.render("> Outer\n> > **Inner** [docs](https://example.com/docs)\n\nAfter")
        await expect(root.locator("blockquote")).to_have_count(2)
        await expect(root.locator("strong")).to_have_text("Inner")
        link = root.locator("a")
        await expect(link).to_have_attribute("href", "https://example.com/docs")
        await expect(link).to_have_attribute("rel", "noopener noreferrer")
        self.assertIn("After", await root.text_content())
        self.assertFalse(self.errors)

    async def test_html_and_unsafe_url_schemes_are_text_not_active_content(self):
        source = (
            "<img src=x onerror=alert(1)> <script>alert(1)</script>\n\n"
            "[js](javascript:alert) [data](data:text/html,evil) "
            "[file](file:///tmp/secret) [vb](vbscript:msgbox) "
            "[safe](https://example.com/safe)"
        )
        root = await self.render(source)
        await expect(root.locator("img, script, iframe, [onerror], [onclick]")).to_have_count(0)
        await expect(root.locator("a")).to_have_count(1)
        await expect(root.locator("a")).to_have_attribute("href", "https://example.com/safe")
        self.assertIn("<img src=x onerror=alert(1)>", await root.text_content())
        self.assertFalse(self.errors)

    async def test_unmatched_brackets_do_not_rescan_the_remaining_message(self):
        result = await self.page.evaluate("""() => {
            const source = '['.repeat(65536) + ' unmatched';
            const started = performance.now();
            const root = markdown(source);
            return {text: root.textContent, elapsed: performance.now() - started};
        }""")
        self.assertEqual(result["text"], "[" * 65536 + " unmatched")
        # Before the fix this fixture took ~1.7s and grew quadratically with input.
        self.assertLess(result["elapsed"], 1000)
        self.assertFalse(self.errors)


if __name__ == "__main__":
    unittest.main()
