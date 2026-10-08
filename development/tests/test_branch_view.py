"""Widget-level checks for the nested side-thread panel."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from project_layout import configure_imports, source_file
configure_imports(__file__)

from branch_view import BranchPanel  # noqa: E402


class BranchPanelTests(unittest.TestCase):
    def test_submit_and_streaming_answer_lifecycle(self) -> None:
        sent = []
        panel = BranchPanel(
            7,
            "selected excerpt",
            [{"role": "assistant", "content": "existing answer"}],
            lambda branch_id, text, _panel: sent.append((branch_id, text)),
        )
        panel.entry.set_text(" focused question ")
        panel.submit()
        self.assertEqual(sent, [(7, "focused question")])
        self.assertEqual(panel.entry.get_text(), "")
        panel.start_answer()
        self.assertFalse(panel.entry.get_sensitive())
        panel.set_answer("streamed **Markdown**")
        panel.finish_answer("final **Markdown**")
        self.assertTrue(panel.entry.get_sensitive())
        self.assertIsNone(panel.answer_view)

    def test_quote_user_message_and_entry_follow_content_direction(self) -> None:
        panel = BranchPanel(8, "\u0627\u06cc\u0646 \u0646\u0642\u0644\u200c\u0642\u0648\u0644 \u0641\u0627\u0631\u0633\u06cc \u0627\u0633\u062a", [], lambda *_args: None)
        quote = panel.get_children()[1]
        self.assertEqual(quote.get_direction(), Gtk.TextDirection.RTL)
        user = panel.add_message("user", "\u0627\u06cc\u0646 \u0633\u0624\u0627\u0644 \u0641\u0627\u0631\u0633\u06cc \u0627\u0633\u062a")
        self.assertEqual(user.get_direction(), Gtk.TextDirection.RTL)
        mixed_user = panel.add_message("user", "Spark \u0627\u06cc\u0646 \u0633\u0624\u0627\u0644 \u0641\u0627\u0631\u0633\u06cc \u0627\u0633\u062a")
        self.assertEqual(mixed_user.get_direction(), Gtk.TextDirection.RTL)
        panel.entry.set_text("\u0627\u06cc\u0646 \u0648\u0631\u0648\u062f\u06cc \u0641\u0627\u0631\u0633\u06cc \u0627\u0633\u062a")
        self.assertEqual(panel.entry.get_direction(), Gtk.TextDirection.RTL)
        panel.entry.set_text("Spark \u0627\u06cc\u0646 \u0648\u0631\u0648\u062f\u06cc \u0641\u0627\u0631\u0633\u06cc \u0627\u0633\u062a")
        self.assertEqual(panel.entry.get_direction(), Gtk.TextDirection.RTL)
        panel.entry.set_text("English input")
        self.assertEqual(panel.entry.get_direction(), Gtk.TextDirection.LTR)


if __name__ == "__main__":
    unittest.main()
