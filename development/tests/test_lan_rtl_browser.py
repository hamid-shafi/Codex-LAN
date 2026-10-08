"""Real-browser coverage for language-neutral RTL direction and LTR isolation."""

import unittest

from playwright.async_api import expect

import test_lan_browser as browser_fixtures


class InternationalDirectionBrowserTests(unittest.IsolatedAsyncioTestCase):
    asyncTearDown = browser_fixtures.LanBrowserTests.asyncTearDown
    start_worker = browser_fixtures.LanBrowserTests.start_worker
    login = browser_fixtures.LanBrowserTests.login

    async def asyncSetUp(self):
        await browser_fixtures.LanBrowserTests.asyncSetUp(self)
        await self.login(self.admin)
        await self.page.wait_for_function("typeof markdown === 'function'")

    async def test_all_rtl_script_families_and_mixed_prefixes_use_strong_bidi_classes(self):
        rtl = [
            "\u0633\u0644\u0627\u0645", "\u0645\u0631\u062d\u0628\u0627", "שלום", "\u0627\u0631\u062f\u0648", "\u0710\u0712", "\u0780\u0781", "\u07ca\u07cb",
            "\u0800\u0801", "\u0840\u0841", "\U0001e900\U0001e901", "\U00010d00", "\U00010d50",
            "\U00010940", "\U00010840", "\U00010f70", "\U0001ee00", "\ufb1d", "\ufb50",
        ]
        ltr = ["", "English", "Ελληνικά", "中文", "हिन्दी", "日本語", "🙂", "123", "\u0661\u0662\u0663", "\u06f1\u06f2\u06f3", "\u060c", "\u064e\u064f", "\u05b0", "\u200c\u200d", "\U0001e944"]
        actual = await self.page.evaluate("values => values.map(direction)", rtl + [f"identifier {value}" for value in rtl] + ltr)
        self.assertEqual(actual, ["rtl"] * (2 * len(rtl)) + ["ltr"] * len(ltr))
        for value in ("שלום", "\U0001e900\U0001e901", "identifier \u0627\u0631\u062f\u0648", "\u0661\u0662\u0663", "Ελληνικά"):
            await self.page.locator("#message-input").fill(value)
            await expect(self.page.locator("#message-input")).to_have_attribute("dir", "rtl" if value in rtl or value.startswith("identifier") else "ltr")
        self.assertFalse(self.errors)

    async def test_markdown_prose_titles_and_quote_keep_direction_but_code_math_passwords_are_ltr(self):
        rendered = await self.page.evaluate("""() => {
          const fixture = document.createElement('section');
          fixture.id = 'international-direction-fixture';
          fixture.className = 'message-content';
          fixture.append(markdown('## identifier שלום\\n\\n\u0645\u0631\u062d\u0628\u0627 `variable = 42`\\n\\n- \u0627\u0631\u062f\u0648\\n\\n> \\u0710\\u0712\\n\\n| שלום | English |\\n| --- | --- |\\n| \\u0780\\u0781 | \u0661\u0662\u0663 |\\n\\n```python\\nprint("שלום")\\n```\\n\\n\\\\[x^2 + 1\\\\]'));
          document.body.append(fixture);
          mergeRoom({id:state.selected, title:'identifier שלום'});
          renderHeader();
          return [...fixture.querySelectorAll('h2, p, li, blockquote, th, td')].map(node => [node.tagName, node.dir]);
        }""")
        self.assertIn(["H2", "rtl"], rendered)
        self.assertIn(["LI", "rtl"], rendered)
        self.assertIn(["BLOCKQUOTE", "rtl"], rendered)
        self.assertIn(["TH", "ltr"], rendered)
        self.assertIn(["TD", "ltr"], rendered)
        await expect(self.page.locator("#room-title")).to_have_attribute("dir", "rtl")
        fixture = self.page.locator("#international-direction-fixture")
        for selector in ("p code", ".code-block pre", ".math-render"):
            await expect(fixture.locator(selector).first).to_have_css("direction", "ltr")
        for selector in ("#password-login-password", "#login-token", "#browser-login-password", "#browser-login-confirm"):
            await expect(self.page.locator(selector)).to_have_attribute("dir", "ltr")
        notice = await self.http.get("/Unicode-LICENSE.txt")
        self.assertEqual(notice.status, 200)
        self.assertIn("UNICODE LICENSE V3", await notice.text())
        self.assertFalse(self.errors)
