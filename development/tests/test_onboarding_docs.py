"""Keep the source ZIP's offline start page and entry-point links usable."""

from html.parser import HTMLParser
from pathlib import Path
import re
import unittest
from urllib.parse import unquote, urlsplit


ROOT = Path(__file__).resolve().parents[2]
GUIDE = ROOT / "docs/getting-started/index.html"
PAGES = (
    "README.md", ".github/CONTRIBUTING.md", ".github/SECURITY.md",
    "setup/README.md", "docs/README.md", "development/README.md",
    "docs/architecture/REPOSITORY.md", "docs/legal/THIRD_PARTY_NOTICES.md",
    "docs/operations/SETUP.md", "docs/operations/SSH-RELAY.md",
    "docs/operations/WINDOWS-WSL.md", "docs/operations/PUBLISHING.md",
    "docs/getting-started/README.md", "docs/getting-started/DEPENDENCIES.md",
    "docs/product/OVERVIEW.md",
)


class StartPage(HTMLParser):
    def __init__(self, source):
        super().__init__()
        self.links = []
        self.ids = []
        self.tags = []
        self.attributes = []
        self.feed(source)

    def handle_starttag(self, tag, attributes):
        attrs = dict(attributes)
        self.tags.append(tag)
        self.attributes.append(attrs)
        if "href" in attrs:
            self.links.append(attrs["href"])
        if "id" in attrs:
            self.ids.append(attrs["id"])


class OnboardingDocsTests(unittest.TestCase):
    def assert_local_link(self, source, link):
        url = urlsplit(link)
        if url.scheme or url.netloc:
            return
        target = (source.parent / unquote(url.path)).resolve() if url.path else source
        self.assertTrue(target.is_relative_to(ROOT), f"Link escapes checkout: {source.name}: {link}")
        self.assertTrue(target.exists(), f"Broken link: {source.relative_to(ROOT)}: {link}")
        if target.suffix == ".html" and url.fragment:
            page = StartPage(target.read_text(encoding="utf-8"))
            self.assertIn(url.fragment, page.ids)
        if target.suffix == ".md" and url.fragment:
            headings = re.findall(r"^#{1,6}\s+(.+)$", target.read_text(encoding="utf-8"), re.M)
            anchors = {re.sub(r"[^\w\- ]", "", heading.lower()).replace(" ", "-") for heading in headings}
            self.assertIn(url.fragment, anchors, f"Missing heading: {link}")

    def test_product_and_setup_markdown_links_resolve_after_policy_moves(self):
        for name in PAGES:
            source = ROOT / name
            for link in re.findall(r"\]\(([^)\s]+)\)", source.read_text(encoding="utf-8")):
                with self.subTest(page=name, link=link):
                    self.assert_local_link(source, link)

    def test_offline_page_links_and_all_role_anchors_exist(self):
        page = StartPage(GUIDE.read_text(encoding="utf-8"))
        self.assertEqual(len(page.ids), len(set(page.ids)))
        for role in ("host", "join", "windows", "relay"):
            self.assertIn(role, page.ids)
        for link in page.links:
            self.assert_local_link(GUIDE, link)

    def test_start_page_has_no_active_content_or_remote_assets(self):
        source = GUIDE.read_text(encoding="utf-8")
        page = StartPage(source)
        self.assertFalse(set(page.tags) & {"script", "iframe", "object", "embed", "form"})
        self.assertIn("default-src 'none'", source)
        for attrs in page.attributes:
            self.assertFalse(any(key.startswith("on") for key in attrs))
            self.assertFalse(set(attrs) & {"src", "srcset"})
        self.assertNotRegex(source, r"(?i)@import|url\s*\(")
        self.assertIn('lang="en"', source)
        self.assertIn('name="viewport"', source)
        self.assertIn(":focus-visible", source)
        self.assertIn("@media (max-width:", source)

    def test_newcomers_are_not_promised_a_bundled_os_or_native_windows_connector(self):
        source = (ROOT / "docs/getting-started/DEPENDENCIES.md").read_text(encoding="utf-8")
        self.assertIn("Python itself is not bundled", source)
        self.assertIn("model inference is not offline", source)
        self.assertIn("not a native Windows Connector", source)
        guide = (ROOT / "docs/getting-started/README.md").read_text(encoding="utf-8")
        self.assertIn("SSH authorization and app approval are separate", guide)
        self.assertIn("before WSL or a Connector is installed", guide)

    def test_root_stays_small_and_distribution_notices_remain_discoverable(self):
        for name in ("CONTRIBUTING.md", "SECURITY.md", "THIRD_PARTY_NOTICES.md"):
            self.assertFalse((ROOT / name).exists())
        self.assertTrue((ROOT / ".github/SECURITY.md").is_file())
        self.assertTrue((ROOT / "docs/legal/THIRD_PARTY_NOTICES.md").is_file())
        self.assertLess(len((ROOT / "README.md").read_text(encoding="utf-8").split()), 700)


if __name__ == "__main__":
    unittest.main()
