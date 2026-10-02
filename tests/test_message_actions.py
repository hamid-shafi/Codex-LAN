"""Regression tests for message selection context actions."""

from __future__ import annotations

import unittest

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk  # noqa: E402

from app import MessageBubble  # noqa: E402
from branch_view import BranchPanel  # noqa: E402


def drain_events() -> None:
    context = GLib.MainContext.default()
    while context.pending():
        context.iteration(False)


class MessageSelectionActionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        ready, _args = Gtk.init_check([])
        if not ready:
            raise unittest.SkipTest("GTK display is unavailable")

    def test_selected_text_can_reply_or_open_private_side_thread(self) -> None:
        replies = []
        branches = []
        bubble = MessageBubble(
            "assistant",
            "First paragraph.\n\nSecond paragraph.",
            message_id=42,
            on_reply=lambda *args: replies.append(args),
            on_branch=lambda *args: branches.append(args),
        )
        window = Gtk.Window()
        window.add(bubble)
        window.show_all()
        try:
            bubble.content_view.select_all_prose()
            drain_events()
            self.assertFalse(hasattr(bubble, "selection_revealer"))
            self.assertEqual(
                bubble.content_view.get_selected_text(),
                "First paragraph.",
            )
            bubble.selection_context_menu = bubble._build_selection_context_menu()
            self.assertEqual(
                bubble.selection_reply_item.get_label(), "Reply to selection"
            )
            self.assertEqual(
                bubble.selection_branch_item.get_label(), "Ask in side thread"
            )
            bubble.selection_reply_item.activate()
            drain_events()
            self.assertEqual(replies[0][:2], (42, "assistant"))
            self.assertEqual(
                replies[0][2], "First paragraph."
            )

            bubble.content_view.select_all_prose()
            drain_events()
            bubble.selection_context_menu = bubble._build_selection_context_menu()
            bubble.selection_branch_item.activate()
            self.assertEqual(
                branches[0][:2],
                (42, "First paragraph."),
            )
            self.assertIs(branches[0][2], bubble)
        finally:
            window.destroy()
            drain_events()

    def test_context_menu_copies_and_explicitly_clears_selection(self) -> None:
        bubble = MessageBubble(
            "user",
            "Selected words",
            message_id=9,
            on_reply=lambda *_args: None,
        )
        bubble.content_view.select_all_prose()
        drain_events()
        menu = bubble._build_selection_context_menu()

        self.assertIsNone(bubble.selection_branch_item)
        self.assertEqual(bubble.selection_copy_item.get_label(), "Copy selection")
        self.assertEqual(bubble.selection_clear_item.get_label(), "Clear selection")
        bubble.selection_clear_item.activate()
        drain_events()
        self.assertEqual(bubble.content_view.get_selected_text(), "")
        menu.destroy()

    def test_side_thread_panels_collapse_and_reopen_under_parent(self) -> None:
        bubble = MessageBubble(
            "assistant",
            "Answer",
            message_id=7,
            on_reply=lambda *_args: None,
            on_branch=lambda *_args: None,
        )
        panel = BranchPanel(9, "selected words", [], lambda *_args: None)
        bubble.add_branch_panel(panel)
        self.assertIs(bubble.branch_panels[9], panel)
        self.assertTrue(bubble.branch_box.get_visible())
        self.assertEqual(bubble.branch_toggle_button.get_label(), "Side threads · 1")

        bubble.collapse_branch(9, panel)
        self.assertFalse(bubble.branch_box.get_visible())
        bubble._toggle_branches(bubble.branch_toggle_button)
        self.assertTrue(bubble.branch_box.get_visible())
        self.assertTrue(panel.get_visible())


if __name__ == "__main__":
    unittest.main()
