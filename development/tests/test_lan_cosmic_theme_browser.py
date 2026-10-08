"""CODEX: Browser regressions for the distinctive, persistent, responsive cosmic theme."""

from __future__ import annotations

import unittest

from playwright.async_api import expect

import test_lan_browser as browser_fixture


class LanCosmicThemeBrowserTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = browser_fixture.LanBrowserTests.asyncSetUp
    asyncTearDown = browser_fixture.LanBrowserTests.asyncTearDown
    start_worker = browser_fixture.LanBrowserTests.start_worker
    login = browser_fixture.LanBrowserTests.login

    async def test_saved_theme_is_applied_without_waiting_for_the_application(self):
        response = await self.http.get("/theme-init.js")
        self.assertEqual(response.status, 200)
        self.assertIn("script-src 'self'", response.headers["Content-Security-Policy"])
        await response.read()

        await self.context.add_init_script("localStorage.setItem('codex-lan-theme', 'cosmic')")
        for asset in ("app.js", "controls.js", "companions.js"):
            await self.page.route(f"**/{asset}", lambda route: route.fulfill(body="", content_type="text/javascript"))
        await self.page.goto(self.base)

        root = self.page.locator("html")
        await expect(root).to_have_attribute("data-theme", "cosmic")
        await expect(root).to_have_attribute("data-theme-family", "space")
        order = await self.page.evaluate(
            """() => [...document.head.children].map(node => node.getAttribute('src') || node.getAttribute('href') || node.tagName)"""
        )
        self.assertLess(order.index("/theme-init.js"), order.index("/style.css"), order)
        self.assertFalse(self.errors, self.errors)

    async def test_room_delete_is_neutral_until_hover_then_uses_danger_in_every_theme(self):
        await self.login(self.admin)
        selector = self.page.locator("#theme-select")
        remove = self.page.locator(".room-row.active .room-delete")
        await expect(remove).to_be_visible()

        for theme in ("light", "dark", "midnight", "chatgpt", "torte", "cosmic", "aurora", "ember", "velvet"):
            await selector.select_option(theme)
            await self.page.mouse.move(0, 0)
            before = await remove.evaluate("node => getComputedStyle(node).backgroundColor")
            await remove.hover()
            await self.page.wait_for_timeout(250)
            self.assertTrue(await remove.evaluate("node => node.matches(':hover')"), theme)
            colors = await remove.evaluate(
                """node => {
                    const style = getComputedStyle(node);
                    const probe = document.createElement('span');
                    probe.style.color = getComputedStyle(document.documentElement).getPropertyValue('--danger');
                    document.body.append(probe);
                    const danger = getComputedStyle(probe).color;
                    probe.remove();
                    return {
                        background: style.backgroundColor,
                        border: style.borderTopColor,
                        foreground: style.color,
                        danger,
                    };
                }"""
            )
            self.assertNotEqual(before, colors["danger"], (theme, before, colors))
            self.assertNotEqual(colors["background"], colors["danger"], (theme, colors))
            self.assertNotEqual(colors["background"], before, (theme, colors))
            self.assertEqual(colors["border"], colors["danger"], (theme, colors))
            self.assertEqual(colors["foreground"], colors["danger"], (theme, colors))

        self.assertFalse(self.errors, self.errors)

    async def test_space_palette_variants_persist_and_keep_native_options_readable(self):
        await self.login(self.admin)
        selector = self.page.locator("#theme-select")
        expected = ["light", "dark", "midnight", "chatgpt", "torte", "cosmic", "aurora", "ember", "velvet"]
        self.assertEqual(await selector.locator("option").evaluate_all("nodes => nodes.map(node => node.value)"), expected)

        palettes = {}
        for theme in ("cosmic", "aurora", "ember", "velvet"):
            await selector.select_option(theme)
            await expect(self.page.locator("html")).to_have_attribute("data-theme", theme)
            await expect(self.page.locator("html")).to_have_attribute("data-theme-family", "space")
            palette = await self.page.evaluate(
                """() => {
                    const rootStyle = getComputedStyle(document.documentElement);
                    const optionStyle = getComputedStyle(document.querySelector('#theme-select option'));
                    const probe = document.createElement('span');
                    probe.style.cssText = 'position:fixed;left:-10000px;top:0';
                    document.body.append(probe);
                    const rgb = value => {
                        probe.style.color = value;
                        const match = getComputedStyle(probe).color.match(/[\\d.]+/g);
                        return match ? match.slice(0, 3).map(Number) : [];
                    };
                    const luminance = color => {
                        const channels = rgb(color).map(value => {
                            value /= 255;
                            return value <= .04045 ? value / 12.92 : ((value + .055) / 1.055) ** 2.4;
                        });
                        return .2126 * channels[0] + .7152 * channels[1] + .0722 * channels[2];
                    };
                    const contrast = (foreground, background) => {
                        const a = luminance(foreground), b = luminance(background);
                        return (Math.max(a, b) + .05) / (Math.min(a, b) + .05);
                    };
                    const text = rootStyle.getPropertyValue('--text').trim();
                    const page = rootStyle.getPropertyValue('--page').trim();
                    const surface = rootStyle.getPropertyValue('--surface').trim();
                    const result = {
                        signature: ['--page', '--sidebar', '--surface', '--accent', '--primary-start', '--primary-end']
                            .map(name => rootStyle.getPropertyValue(name).trim()),
                        pageContrast: contrast(text, page),
                        surfaceContrast: contrast(text, surface),
                        optionContrast: contrast(optionStyle.color, optionStyle.backgroundColor),
                        optionBackground: optionStyle.backgroundColor,
                        storedTheme: localStorage.getItem('codex-lan-theme'),
                        mainBackground: getComputedStyle(document.querySelector('.main')).backgroundImage,
                    };
                    probe.remove();
                    return result;
                }"""
            )
            self.assertGreaterEqual(palette["pageContrast"], 4.5, (theme, palette))
            self.assertGreaterEqual(palette["surfaceContrast"], 4.5, (theme, palette))
            self.assertGreaterEqual(palette["optionContrast"], 4.5, (theme, palette))
            self.assertNotEqual(palette["optionBackground"], "rgba(0, 0, 0, 0)", (theme, palette))
            self.assertNotEqual(palette["mainBackground"], "none", (theme, palette))
            self.assertEqual(palette["storedTheme"], theme)
            palettes[theme] = palette["signature"]

        self.assertEqual(len({tuple(value) for value in palettes.values()}), len(palettes), palettes)
        await self.page.reload()
        await expect(self.page.locator("#workspace")).to_be_visible()
        await expect(self.page.locator("html")).to_have_attribute("data-theme", "velvet")
        await expect(self.page.locator("html")).to_have_attribute("data-theme-family", "space")
        await expect(selector).to_have_value("velvet")

        await selector.select_option("light")
        await expect(self.page.locator("html")).not_to_have_attribute("data-theme-family", "space")
        self.assertFalse(self.errors, self.errors)

    async def test_chatgpt_black_theme_is_polished_dark_and_persistent(self):
        await self.login(self.admin)
        selector = self.page.locator("#theme-select")
        await expect(selector.locator("option[value='chatgpt']")).to_have_text("ChatGPT Black")

        await selector.select_option("chatgpt")
        root = self.page.locator("html")
        await expect(root).to_have_attribute("data-theme", "chatgpt")
        await expect(root).not_to_have_attribute("data-theme-family", "space")

        visuals = await self.page.evaluate(
            """() => {
                const style = getComputedStyle(document.documentElement);
                const probe = document.createElement('span');
                probe.style.cssText = 'position:fixed;left:-10000px;top:0';
                document.body.append(probe);
                const rgb = value => {
                    probe.style.color = value;
                    return getComputedStyle(probe).color.match(/[\\d.]+/g).slice(0, 3).map(Number);
                };
                const luminance = value => {
                    const channels = rgb(value).map(channel => {
                        channel /= 255;
                        return channel <= .04045 ? channel / 12.92 : ((channel + .055) / 1.055) ** 2.4;
                    });
                    return .2126 * channels[0] + .7152 * channels[1] + .0722 * channels[2];
                };
                const contrast = (foreground, background) => {
                    const a = luminance(foreground), b = luminance(background);
                    return (Math.max(a, b) + .05) / (Math.min(a, b) + .05);
                };
                const text = style.getPropertyValue('--text').trim();
                const page = style.getPropertyValue('--page').trim();
                const result = {
                    page,
                    sidebar: style.getPropertyValue('--sidebar').trim(),
                    surface: style.getPropertyValue('--surface').trim(),
                    code: style.getPropertyValue('--code').trim(),
                    colorScheme: style.colorScheme,
                    contrast: contrast(text, page),
                    mainBackground: getComputedStyle(document.querySelector('.main')).backgroundImage,
                    composerBackground: getComputedStyle(document.querySelector('.composer')).backgroundColor,
                    storedTheme: localStorage.getItem('codex-lan-theme'),
                };
                probe.remove();
                return result;
            }"""
        )
        self.assertEqual(visuals["page"], "#0d0d0d")
        self.assertEqual(visuals["sidebar"], "#171717")
        self.assertEqual(visuals["surface"], "#212121")
        self.assertEqual(visuals["code"], "#1e1f22")
        self.assertEqual(visuals["colorScheme"], "dark")
        self.assertGreaterEqual(visuals["contrast"], 7)
        self.assertNotEqual(visuals["mainBackground"], "none")
        self.assertEqual(visuals["composerBackground"], "rgb(47, 47, 47)")
        self.assertEqual(visuals["storedTheme"], "chatgpt")

        await self.page.reload()
        await expect(self.page.locator("#workspace")).to_be_visible()
        await expect(root).to_have_attribute("data-theme", "chatgpt")
        await expect(selector).to_have_value("chatgpt")

        for width in (360, 720, 1024):
            await self.page.set_viewport_size({"width": width, "height": 844})
            await self.page.evaluate(
                "() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))"
            )
            await expect(selector).to_be_visible()
            layout = await self.page.evaluate(
                """() => {
                    const select = document.querySelector('#theme-select').getBoundingClientRect();
                    const topbar = document.querySelector('.topbar').getBoundingClientRect();
                    return {
                        noHorizontalOverflow: document.documentElement.scrollWidth <= window.innerWidth + 1,
                        selectInsideViewport: select.left >= 0 && select.right <= window.innerWidth,
                        topbarInsideViewport: topbar.left >= 0 && topbar.right <= window.innerWidth,
                    };
                }"""
            )
            self.assertTrue(layout["noHorizontalOverflow"], (width, layout))
            self.assertTrue(layout["selectInsideViewport"], (width, layout))
            self.assertTrue(layout["topbarInsideViewport"], (width, layout))
        self.assertFalse(self.errors, self.errors)

    async def test_vim_torte_theme_is_accessible_responsive_and_persistent(self):
        await self.login(self.admin)
        selector = self.page.locator("#theme-select")
        await expect(selector.locator("option[value='torte']")).to_have_text("Vim Torte")

        await selector.select_option("torte")
        root = self.page.locator("html")
        await expect(root).to_have_attribute("data-theme", "torte")
        await expect(root).not_to_have_attribute("data-theme-family", "space")
        await self.page.wait_for_timeout(200)

        visuals = await self.page.evaluate(
            """() => {
                const style = getComputedStyle(document.documentElement);
                const probe = document.createElement('span');
                probe.style.cssText = 'position:fixed;left:-10000px;top:0';
                document.body.append(probe);
                const rgb = value => {
                    probe.style.color = value;
                    return getComputedStyle(probe).color.match(/[\\d.]+/g).slice(0, 3).map(Number);
                };
                const luminance = value => {
                    const channels = rgb(value).map(channel => {
                        channel /= 255;
                        return channel <= .04045 ? channel / 12.92 : ((channel + .055) / 1.055) ** 2.4;
                    });
                    return .2126 * channels[0] + .7152 * channels[1] + .0722 * channels[2];
                };
                const contrast = (foreground, background) => {
                    const a = luminance(foreground), b = luminance(background);
                    return (Math.max(a, b) + .05) / (Math.min(a, b) + .05);
                };
                const text = style.getPropertyValue('--text').trim();
                const page = style.getPropertyValue('--page').trim();
                const result = {
                    page,
                    sidebar: style.getPropertyValue('--sidebar').trim(),
                    accent: style.getPropertyValue('--accent').trim(),
                    warning: style.getPropertyValue('--warning').trim(),
                    code: style.getPropertyValue('--code').trim(),
                    colorScheme: style.colorScheme,
                    contrast: contrast(text, page),
                    mainBackground: getComputedStyle(document.querySelector('.main')).backgroundImage,
                    composerBackground: getComputedStyle(document.querySelector('.composer')).backgroundColor,
                    primaryBackground: getComputedStyle(document.querySelector('.new-room')).backgroundColor,
                    storedTheme: localStorage.getItem('codex-lan-theme'),
                };
                probe.remove();
                return result;
            }"""
        )
        self.assertEqual(visuals["page"], "#000")
        self.assertEqual(visuals["sidebar"], "#080808")
        self.assertEqual(visuals["accent"], "#80a0ff")
        self.assertEqual(visuals["warning"], "#ffff60")
        self.assertEqual(visuals["code"], "#050505")
        self.assertEqual(visuals["colorScheme"], "dark")
        self.assertGreaterEqual(visuals["contrast"], 7)
        self.assertNotEqual(visuals["mainBackground"], "none")
        self.assertEqual(visuals["composerBackground"], "rgb(16, 16, 16)")
        self.assertEqual(visuals["primaryBackground"], "rgb(224, 224, 224)")
        self.assertEqual(visuals["storedTheme"], "torte")

        await self.page.reload()
        await expect(self.page.locator("#workspace")).to_be_visible()
        await expect(root).to_have_attribute("data-theme", "torte")
        await expect(selector).to_have_value("torte")

        for width in (360, 720, 1024):
            await self.page.set_viewport_size({"width": width, "height": 844})
            await self.page.evaluate(
                "() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))"
            )
            await expect(selector).to_be_visible()
            layout = await self.page.evaluate(
                """() => {
                    const select = document.querySelector('#theme-select').getBoundingClientRect();
                    return {
                        noHorizontalOverflow: document.documentElement.scrollWidth <= window.innerWidth + 1,
                        selectInsideViewport: select.left >= 0 && select.right <= window.innerWidth,
                    };
                }"""
            )
            self.assertTrue(all(layout.values()), (width, layout))
        self.assertFalse(self.errors, self.errors)

    async def test_cosmic_theme_is_distinct_persistent_accessible_and_responsive(self):
        await self.login(self.admin)
        selector = self.page.locator("#theme-select")
        await expect(selector.locator("option[value='cosmic']")).to_have_count(1)

        await selector.select_option("midnight")
        midnight_palette = await self.page.evaluate(
            """() => {
                const style = getComputedStyle(document.documentElement);
                return ['--page', '--sidebar', '--surface', '--accent', '--user', '--primary-start']
                    .map(name => style.getPropertyValue(name).trim());
            }"""
        )
        await selector.select_option("cosmic")
        await expect(self.page.locator("html")).to_have_attribute("data-theme", "cosmic")
        await expect(selector).to_have_value("cosmic")

        cosmic_visuals = await self.page.evaluate(
            """() => {
                const root = document.documentElement;
                const style = getComputedStyle(root);
                const variables = ['--page', '--sidebar', '--surface', '--accent', '--user', '--primary-start'];
                const values = variables.map(name => style.getPropertyValue(name).trim());
                const probe = document.createElement('span');
                probe.style.cssText = 'position:fixed;left:-10000px;top:0;opacity:0;pointer-events:none';
                document.body.append(probe);
                const rgb = value => {
                    probe.style.color = '';
                    probe.style.color = value;
                    const match = getComputedStyle(probe).color.match(/[\\d.]+/g);
                    return match ? match.slice(0, 3).map(Number) : [];
                };
                const luminance = color => {
                    const channels = rgb(color).map(value => {
                        value /= 255;
                        return value <= .04045 ? value / 12.92 : ((value + .055) / 1.055) ** 2.4;
                    });
                    return .2126 * channels[0] + .7152 * channels[1] + .0722 * channels[2];
                };
                const contrast = (foreground, background) => {
                    const first = luminance(foreground);
                    const second = luminance(background);
                    return (Math.max(first, second) + .05) / (Math.min(first, second) + .05);
                };
                const text = style.getPropertyValue('--text').trim();
                const page = style.getPropertyValue('--page').trim();
                const surface = style.getPropertyValue('--surface').trim();
                const pageContrast = contrast(text, page);
                const surfaceContrast = contrast(text, surface);
                probe.remove();
                return {
                    values,
                    colorScheme: style.colorScheme,
                    pageContrast,
                    surfaceContrast,
                    storedTheme: localStorage.getItem('codex-lan-theme'),
                };
            }"""
        )
        self.assertGreaterEqual(
            sum(current != previous for current, previous in zip(cosmic_visuals["values"], midnight_palette)),
            4,
            (midnight_palette, cosmic_visuals["values"]),
        )
        self.assertIn("dark", cosmic_visuals["colorScheme"])
        self.assertGreaterEqual(cosmic_visuals["pageContrast"], 4.5, cosmic_visuals)
        self.assertGreaterEqual(cosmic_visuals["surfaceContrast"], 4.5, cosmic_visuals)
        self.assertEqual(cosmic_visuals["storedTheme"], "cosmic")

        await self.page.reload()
        await expect(self.page.locator("#workspace")).to_be_visible()
        await expect(self.page.locator("html")).to_have_attribute("data-theme", "cosmic")
        await expect(selector).to_have_value("cosmic")

        for width in (360, 390, 768, 1365):
            await self.page.set_viewport_size({"width": width, "height": 900})
            await self.page.evaluate(
                "() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))"
            )
            await self.page.wait_for_timeout(300)
            for target in ("#room-title", "#message-input", "#model-select", "#effort-select", "#send-button"):
                await expect(self.page.locator(target)).to_be_visible()
            layout = await self.page.evaluate(
                """() => {
                    const fits = selector => {
                        const node = document.querySelector(selector);
                        return node.scrollWidth <= node.clientWidth + 1;
                    };
                    const insideViewport = selector => {
                        const rect = document.querySelector(selector).getBoundingClientRect();
                        return rect.left >= -1 && rect.right <= innerWidth + 1
                            && rect.top >= -1 && rect.bottom <= innerHeight + 1;
                    };
                    return {
                        documentFits: document.documentElement.scrollWidth <= innerWidth + 1,
                        roomListFits: fits('#room-list'),
                        topbarFits: fits('.topbar'),
                        timelineFits: fits('#timeline-scroll'),
                        composerFits: fits('#composer-area'),
                        inputVisible: insideViewport('#message-input'),
                        sendVisible: insideViewport('#send-button'),
                    };
                }"""
            )
            self.assertTrue(all(layout.values()), f"cosmic at {width}px: {layout}")
            await self.page.screenshot(
                path=f"/tmp/codex-lan-cosmic-{width}.png",
                full_page=True,
            )

        self.assertFalse(self.errors, self.errors)

    async def test_cosmic_ambient_layers_preserve_hit_targets_and_reduced_motion(self):
        await self.login(self.admin)
        await self.page.locator("#theme-select").select_option("cosmic")
        await expect(self.page.locator("html")).to_have_attribute("data-theme", "cosmic")

        ambient = await self.page.evaluate(
            """() => {
                const chat = document.querySelector('.chat-view');
                const read = pseudo => {
                    const style = getComputedStyle(chat, pseudo);
                    return {
                        content: style.content,
                        pointerEvents: style.pointerEvents,
                    };
                };
                return {before: read('::before'), after: read('::after')};
            }"""
        )
        for layer, style in ambient.items():
            self.assertNotIn(style["content"], ("none", "normal"), (layer, style))
            self.assertEqual(style["pointerEvents"], "none", (layer, style))

        async def click_center(selector):
            target = self.page.locator(selector)
            await expect(target).to_be_visible()
            box = await target.bounding_box()
            self.assertIsNotNone(box, selector)
            await self.page.mouse.click(
                box["x"] + box["width"] / 2,
                box["y"] + box["height"] / 2,
            )

        await click_center("#message-input")
        await expect(self.page.locator("#message-input")).to_be_focused()

        await click_center("#sidebar-toggle")
        await expect(self.page.locator("#sidebar-toggle")).to_have_attribute(
            "aria-expanded", "false"
        )
        await click_center("#sidebar-toggle")
        await expect(self.page.locator("#sidebar-toggle")).to_have_attribute(
            "aria-expanded", "true"
        )

        await click_center("#room-menu")
        await expect(self.page.locator("#room-dialog")).to_be_visible()
        await self.page.locator("#dialog-close").click()
        await expect(self.page.locator("#room-dialog")).to_be_hidden()

        await self.page.emulate_media(reduced_motion="reduce")
        await self.page.evaluate(
            "() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))"
        )
        motion = await self.page.evaluate(
            """() => {
                const chat = document.querySelector('.chat-view');
                const sample = (node, pseudo = null) => {
                    const style = getComputedStyle(node, pseudo);
                    return {
                        animationName: style.animationName,
                        transitionDuration: style.transitionDuration,
                    };
                };
                return {
                    before: sample(chat, '::before'),
                    after: sample(chat, '::after'),
                    beacon: sample(document.querySelector('.status-dot')),
                    emptySymbol: sample(document.querySelector('.empty-symbol')),
                    sidebar: sample(document.querySelector('.sidebar')),
                };
            }"""
        )
        for target, style in motion.items():
            self.assertEqual(style["animationName"], "none", (target, style))
            self.assertTrue(
                all(value.strip() == "0s" for value in style["transitionDuration"].split(",")),
                (target, style),
            )

        self.assertFalse(self.errors, self.errors)

    async def test_cosmic_mobile_uses_static_ambience_and_sticky_long_dialog_headers(self):
        await self.login(self.admin)
        await self.page.locator("#theme-select").select_option("cosmic")
        await self.page.set_viewport_size({"width": 390, "height": 844})
        await self.page.evaluate(
            "() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))"
        )

        mobile = await self.page.evaluate(
            """() => {
                const chat = document.querySelector('.chat-view');
                const controls = document.querySelector('.header-controls').getBoundingClientRect();
                const roomList = document.querySelector('#room-list');
                return {
                    beforeAnimation: getComputedStyle(chat, '::before').animationName,
                    afterAnimation: getComputedStyle(chat, '::after').animationName,
                    controlsFit: controls.left >= -1 && controls.right <= innerWidth + 1,
                    controlsWideEnough: controls.width >= 140,
                    roomListOverflowX: getComputedStyle(roomList).overflowX,
                    roomListFits: roomList.scrollWidth <= roomList.clientWidth + 1,
                };
            }"""
        )
        self.assertEqual(mobile["beforeAnimation"], "none", mobile)
        self.assertEqual(mobile["afterAnimation"], "none", mobile)
        self.assertTrue(mobile["controlsFit"], mobile)
        self.assertTrue(mobile["controlsWideEnough"], mobile)
        self.assertEqual(mobile["roomListOverflowX"], "hidden", mobile)
        self.assertTrue(mobile["roomListFits"], mobile)

        await self.page.locator("#connector-open").click()
        dialog = self.page.locator("#connector-dialog")
        await expect(dialog).to_be_visible()
        await dialog.evaluate("node => { node.scrollTop = node.scrollHeight; }")
        await self.page.evaluate(
            "() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))"
        )
        sticky = await self.page.evaluate(
            """() => {
                const dialog = document.querySelector('#connector-dialog').getBoundingClientRect();
                const heading = document.querySelector('#connector-dialog .dialog-heading').getBoundingClientRect();
                return {
                    topVisible: heading.top >= dialog.top - 1,
                    bottomVisible: heading.bottom <= dialog.bottom + 1,
                    position: getComputedStyle(document.querySelector('#connector-dialog .dialog-heading')).position,
                };
            }"""
        )
        self.assertEqual(sticky["position"], "sticky", sticky)
        self.assertTrue(sticky["topVisible"], sticky)
        self.assertTrue(sticky["bottomVisible"], sticky)
        await self.page.locator("#connector-close").click()

        self.assertFalse(self.errors, self.errors)


if __name__ == "__main__":
    unittest.main()
