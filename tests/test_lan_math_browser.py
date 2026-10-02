"""CODEX: Local MathML rendering remains safe, readable in Persian, and separate from code/source."""

import unittest

from playwright.async_api import expect

import test_lan_browser as browser_fixtures


class MathBrowserTests(unittest.IsolatedAsyncioTestCase):
    asyncTearDown = browser_fixtures.LanBrowserTests.asyncTearDown
    start_worker = browser_fixtures.LanBrowserTests.start_worker

    async def asyncSetUp(self):
        await browser_fixtures.LanBrowserTests.asyncSetUp(self)
        await self.page.goto(self.base)
        await self.page.wait_for_function("window.CodexMath && window.katex && typeof markdown === 'function'")
        await self.page.evaluate("""() => {
            const node = document.createElement('div'); node.id = 'math-fixture';
            node.className = 'message-content'; node.style.width = '340px';
            document.body.prepend(node);
        }""")

    async def render(self, source):
        await self.page.evaluate("source => document.querySelector('#math-fixture').replaceChildren(markdown(source))", source)
        return self.page.locator("#math-fixture")

    async def test_persian_inline_and_display_math_are_native_and_not_duplicated(self):
        source = r"ترتیب را بررسی کنیم: \(a_1\le a_2\) و سپس:" + "\n\n" + r"\[ a_1\le a_2\le\dots a_n \]"
        root = await self.render(source)
        await expect(root.locator("math")).to_have_count(2)
        await expect(root.locator("p").first).to_have_attribute("dir", "rtl")
        await expect(root.locator(".math-inline")).to_have_attribute("dir", "ltr")
        await expect(root.locator(".math-display")).to_have_attribute("dir", "ltr")
        self.assertEqual(await root.locator(".math-display").evaluate("node => getComputedStyle(node).direction"), "ltr")
        visible = await root.inner_text()
        self.assertNotIn(r"\le", visible)
        self.assertNotIn(r"\[", visible)
        self.assertIn("≤", visible)
        self.assertFalse(self.errors)
        await self.page.screenshot(path="/tmp/codex-math-persian.png", full_page=True)

    async def test_multiline_display_blank_lines_and_dollar_math(self):
        source = "پیش از فرمول\n" + r"\[" + "\n" + r"\frac{a_1}{a_2}" + "\n\n" + r"+ \sqrt{x^2}" + "\n" + r"\]" + "\nبعد از فرمول\n\n$$x^2+y^2=z^2$$\n\nInline $x_1+2$ works."
        root = await self.render(source)
        await expect(root.locator(".math-display")).to_have_count(2)
        await expect(root.locator(".math-inline")).to_have_count(1)
        await expect(root.locator("mfrac")).to_have_count(1)
        await expect(root.locator("msqrt")).to_have_count(1)
        self.assertIn("بعد از فرمول", await root.inner_text())

    async def test_fenced_inline_code_currency_and_escaped_delimiters_stay_literal(self):
        source = "```latex\n\\[x_1\\]\n$$y^2$$\n```\n\n`\\(z_1\\)` and `$x_1$`\n\nPrices are $5 and $10. Pay $20 tomorrow.\n\nEscaped \\$x$ stays text."
        root = await self.render(source)
        await expect(root.locator("math")).to_have_count(0)
        self.assertEqual(await root.locator("pre code").inner_text(), "\\[x_1\\]\n$$y^2$$")
        self.assertIn("Prices are $5 and $10.", await root.inner_text())
        self.assertIn(r"\(z_1\)", await root.inner_text())

    async def test_invalid_incomplete_and_oversized_formulas_fall_back_safely(self):
        for source in (r"\[a_1\le", r"\(\notARealCommand{x}\)", r"\(\def\a{\a}\a\)", "\\(" + "x" * 12001 + "\\)"):
            with self.subTest(source_start=source[:30]):
                root = await self.render(source)
                await expect(root.locator("math")).to_have_count(0)
                self.assertEqual(await root.inner_text(), source)
        root = await self.render(r"\[a_1\le a_2\]")
        await expect(root.locator("math")).to_have_count(1)
        self.assertFalse(self.errors)

    async def test_untrusted_tex_cannot_add_links_images_html_or_script(self):
        attempts = [r"\(\href{javascript:alert(1)}{Click}\)", r"\(\includegraphics{https://example.com/tracker}\)",
                    r"\(\htmlStyle{background:url(https://example.com/tracker)}{x}\)",
                    r"\(\text{<img src=x onerror=alert(1)>}\)"]
        requested = []
        self.page.on("request", lambda request: requested.append(request.url))
        for source in attempts:
            root = await self.render(source)
            await expect(root.locator("a, img, script, iframe, [onerror], [onclick]")).to_have_count(0)
            await expect(root.locator("math [style]")).to_have_count(0)
        self.assertFalse(any("example.com" in url for url in requested))
        self.assertFalse(self.errors)

    async def test_rendering_does_not_modify_message_source_or_copy_payload(self):
        source = r"فرمول: \(a_1\le a_2\)"
        result = await self.page.evaluate("""source => {
            const message = {content: source}; const original = message.content;
            document.querySelector('#math-fixture').replaceChildren(markdown(message.content));
            return {source: message.content, unchanged: message.content === original};
        }""", source)
        self.assertEqual(result, {"source": source, "unchanged": True})
        self.assertEqual(await self.page.evaluate("window.katex.version"), "0.18.7")
        for path in ("math.js", "math.css", "katex-0.18.7.min.js", "KaTeX-LICENSE.txt"):
            response = await self.page.request.get(self.base + path)
            self.assertEqual(response.status, 200)

    async def test_cosmic_math_stays_inside_card_and_selection_has_no_raw_duplicate(self):
        await self.page.evaluate("""() => {
            setTheme('cosmic');
            const root = document.querySelector('#math-fixture');
            root.style.padding = '20px'; root.style.margin = '24px';
            root.style.background = 'var(--panel)'; root.style.borderRadius = '16px';
        }""")
        source = r"رابطهٔ مرتب بودن: \(a_1\le a_2\le\dots a_n\)" + "\n\n" + r"\[\frac{a_1+a_2}{2}\le a_n\]"
        root = await self.render(source)
        selection = await root.evaluate("""node => {
            const range = document.createRange(); range.selectNodeContents(node);
            getSelection().removeAllRanges(); getSelection().addRange(range);
            return getSelection().toString();
        }""")
        self.assertNotIn(r"\le", selection)
        self.assertNotIn(r"\frac", selection)
        self.assertEqual(selection.count("≤"), 3)
        await self.page.evaluate("getSelection().removeAllRanges()")
        await root.screenshot(path="/tmp/codex-math-cosmic.png")
        for source in ("\\[" + "+".join(["x_i"] * 100) + "\\]", "\\(" + "+".join(["x_i"] * 100) + "\\)"):
            root = await self.render(source)
            self.assertTrue(await root.evaluate("node => node.scrollWidth <= node.clientWidth + 1"))
            self.assertTrue(await root.locator(".math-render").evaluate("node => node.scrollWidth > node.clientWidth"))


if __name__ == "__main__":
    unittest.main()
