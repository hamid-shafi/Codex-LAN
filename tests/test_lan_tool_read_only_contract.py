"""CODEX: Guard the lightweight web contract for per-room tool read-only mode."""

from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "codex_lan" / "web"


class _MarkupProbe(HTMLParser):
    void_tags = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[dict[str, object]] = []
        self.nodes: list[dict[str, object]] = []
        self.by_id: dict[str, dict[str, object]] = {}

    def _start(self, tag: str, attributes, push: bool) -> None:
        attrs = dict(attributes)
        node: dict[str, object] = {
            "tag": tag,
            "attrs": attrs,
            "classes": set(attrs.get("class", "").split()),
            "ancestors": tuple(self.stack),
            "text": [],
        }
        self.nodes.append(node)
        if attrs.get("id"):
            self.by_id[attrs["id"]] = node
        if push and tag not in self.void_tags:
            self.stack.append(node)

    def handle_starttag(self, tag: str, attrs) -> None:
        self._start(tag, attrs, True)

    def handle_startendtag(self, tag: str, attrs) -> None:
        self._start(tag, attrs, False)

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index]["tag"] == tag:
                del self.stack[index:]
                return

    def handle_data(self, data: str) -> None:
        for node in self.stack:
            node["text"].append(data)


def _function(source: str, name: str) -> str:
    match = re.search(rf"\b(?:async\s+)?function\s+{re.escape(name)}\s*\([^)]*\)\s*\{{", source)
    if match is None:
        raise AssertionError(f"Missing JavaScript function {name}()")
    start = match.start()
    opening = match.end() - 1
    depth = 0
    quote = ""
    escaped = False
    line_comment = False
    block_comment = False
    index = opening
    while index < len(source):
        char = source[index]
        following = source[index + 1] if index + 1 < len(source) else ""
        if line_comment:
            if char == "\n":
                line_comment = False
        elif block_comment:
            if char == "*" and following == "/":
                block_comment = False
                index += 1
        elif quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
        elif char in {"'", '"', "`"}:
            quote = char
        elif char == "/" and following == "/":
            line_comment = True
            index += 1
        elif char == "/" and following == "*":
            block_comment = True
            index += 1
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[start:index + 1]
        index += 1
    raise AssertionError(f"Unterminated JavaScript function {name}()")


def _braced_blocks(source: str, header: re.Pattern[str]) -> list[str]:
    blocks = []
    for match in header.finditer(source):
        opening = source.find("{", match.end())
        if opening < 0:
            continue
        depth = 0
        for index in range(opening, len(source)):
            if source[index] == "{":
                depth += 1
            elif source[index] == "}":
                depth -= 1
                if depth == 0:
                    blocks.append(source[opening + 1:index])
                    break
    return blocks


class LanToolReadOnlyWebContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.html = (WEB / "index.html").read_text(encoding="utf-8")
        cls.javascript = (WEB / "app.js").read_text(encoding="utf-8")
        cls.css = (WEB / "style.css").read_text(encoding="utf-8")
        cls.markup = _MarkupProbe()
        cls.markup.feed(cls.html)

    def test_labeled_checkbox_lives_with_model_and_effort_controls(self):
        checkbox = self.markup.by_id.get("tool-read-only")
        self.assertIsNotNone(checkbox, "Composer must expose #tool-read-only")
        self.assertEqual(checkbox["tag"], "input")
        self.assertEqual(checkbox["attrs"].get("type"), "checkbox")
        self.assertTrue(
            any("composer-tools" in ancestor["classes"] for ancestor in checkbox["ancestors"]),
            "#tool-read-only must sit beside the model/thinking controls",
        )
        labels = [
            node for node in self.markup.nodes
            if node["tag"] == "label"
            and (node["attrs"].get("for") == "tool-read-only" or node in checkbox["ancestors"])
        ]
        self.assertEqual(len(labels), 1, "The checkbox needs one unambiguous label")
        self.assertIn("Read only", " ".join(labels[0]["text"]))
        positions = [self.html.index(f'id="{identifier}"') for identifier in ("model-select", "effort-select", "tool-read-only")]
        self.assertEqual(positions, sorted(positions), "Read only should follow Model and Thinking in the toolbar")

    def test_room_value_renders_and_has_an_independent_patch(self):
        self.assertIsNotNone(
            re.search(
                r'\$\("tool-read-only"\)\.checked\s*=\s*[^;]*\broom\??\.tool_read_only\b',
                self.javascript,
            ),
            "Room rendering must sync #tool-read-only from room.tool_read_only",
        )
        save = _function(self.javascript, "saveToolReadOnly")
        self.assertIn('method: "PATCH"', save)
        self.assertIn("/api/rooms/", save)
        payload = re.search(r"JSON\.stringify\s*\(\s*(\{[^{}]*\})\s*\)", save, re.DOTALL)
        self.assertIsNotNone(payload, "Read only must use its own explicit PATCH payload")
        self.assertRegex(payload.group(1), r"\btool_read_only\b")
        self.assertNotRegex(payload.group(1), r"\b(?:model|effort|speed)\b")
        self.assertNotIn("saveModelSettings(", save)
        self.assertIsNotNone(
            re.search(
                r'\$\("tool-read-only"\)\.addEventListener\s*\(\s*"change"[^;]*\bsaveToolReadOnly\b',
                self.javascript,
            ),
            "Changing the checkbox must invoke saveToolReadOnly()",
        )

    def test_new_run_payload_and_retry_fingerprint_include_the_mode(self):
        intent = _function(self.javascript, "composerSendIntent")
        self.assertIsNotNone(
            re.search(r"originalFingerprint\s*=\s*JSON\.stringify\s*\(\s*\{[^;]*\btool_read_only\b", intent),
            "The retry fingerprint must include tool_read_only",
        )
        send = _function(self.javascript, "sendMessage")
        self.assertIsNotNone(
            re.search(r"const\s+payload\s*=\s*intent\.steering\s*\?[^;]*:\s*\{[^;]*\btool_read_only\b", send),
            "New-run payloads must include tool_read_only",
        )
        self.assertIsNotNone(
            re.search(
                r"const\s+fingerprint\s*=\s*intent\.steering\s*\?[^;]*:\s*JSON\.stringify\s*\(\s*payload\s*\)",
                send,
            ),
            "The request fingerprint must derive from the complete new-run payload",
        )

    def test_saving_locks_the_control_but_an_active_run_does_not(self):
        self.assertIsNotNone(
            re.search(r"\btoolReadOnlySaving\s*:\s*false\b", self.javascript),
            "Each room cache needs an initial toolReadOnlySaving flag",
        )
        save = _function(self.javascript, "saveToolReadOnly")
        self.assertRegex(save, r"\bcache\.toolReadOnlySaving\s*=\s*true\b")
        self.assertRegex(save, r"\bcache\.toolReadOnlySaving\s*=\s*false\b")

        submission = _function(self.javascript, "composerSubmissionState")
        rendering = _function(self.javascript, "renderComposerSubmission")
        update = _function(self.javascript, "updateComposer")
        checkbox_lock = re.search(r'\$\("tool-read-only"\)\.disabled\s*=\s*([^;]+)', update)
        self.assertIsNotNone(checkbox_lock, "Composer updates must lock the read-only control")
        checkbox_expression = checkbox_lock.group(1)
        self.assertNotRegex(
            checkbox_expression,
            r"\brunning\b|activeRuns|submission\.running",
            "An active response must leave Read only available for Send update",
        )
        checkbox_saving_lock = "toolReadOnlySaving" in checkbox_expression or (
            "unavailable" in checkbox_expression and "toolReadOnlySaving" in submission
        )
        self.assertTrue(checkbox_saving_lock, "Saving must lock the read-only control")

        send_lock = re.search(r'\$\("send-button"\)\.disabled\s*=\s*([^;]+)', rendering)
        self.assertIsNotNone(send_lock)
        send_saving_lock = "toolReadOnlySaving" in send_lock.group(1) or (
            "unavailable" in send_lock.group(1) and "toolReadOnlySaving" in submission
        )
        self.assertTrue(send_saving_lock, "A prompt must not race the room-setting PATCH")

    def test_focus_and_mobile_layout_are_explicit(self):
        self.assertTrue("tool-read-only" in self.css, "Read only needs dedicated composer styling")
        selectors = [selector.strip() for selector in re.findall(r"([^{}]+)\{", self.css)]
        self.assertTrue(
            any("tool-read-only" in selector and ":focus-visible" in selector for selector in selectors),
            "Keyboard focus for the read-only control must be visible",
        )
        mobile_blocks = _braced_blocks(
            self.css,
            re.compile(r"@media\s*\(\s*max-width\s*:\s*720px\s*\)"),
        )
        self.assertTrue(
            any(
                re.search(r"(?:#|\.)tool-read-only[-\w]*[^{}]*\{[^{}]*\bgrid-(?:column|area)\b", block)
                for block in mobile_blocks
            ),
            "The compact composer must place Read only on an explicit mobile grid row",
        )


if __name__ == "__main__":
    unittest.main()
