# -*- coding: utf-8 -*-
"""Focused interaction tests for message-level selection context actions."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GLib, Gtk, Pango  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from project_layout import configure_imports, source_file
configure_imports(__file__)

from markdown_view import MarkdownView  # noqa: E402


def drain_events() -> None:
    context = GLib.MainContext.default()
    while context.pending():
        context.iteration(False)


class SelectionContextTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        ready, _args = Gtk.init_check([])
        if not ready:
            raise unittest.SkipTest("GTK display is unavailable")

    def setUp(self) -> None:
        self.window = Gtk.Window()
        self.window.set_default_size(560, 220)

    def tearDown(self) -> None:
        self.window.destroy()
        drain_events()

    @staticmethod
    def _label_point(label: Gtk.Label, offset: int) -> tuple[float, float]:
        rendered = label.get_text()
        byte_offset = len(rendered[:offset].encode("utf-8"))
        position = label.get_layout().index_to_pos(byte_offset)
        layout_x, layout_y = MarkdownView._label_layout_origin(label)
        return (
            layout_x + position.x / Pango.SCALE,
            layout_y + (position.y + position.height / 2) / Pango.SCALE,
        )

    def _button_event(
        self,
        label: Gtk.Label,
        button: int,
        offset: int,
        event_type: Gdk.EventType = Gdk.EventType.BUTTON_PRESS,
    ) -> Gdk.Event:
        event = Gdk.Event.new(event_type)
        event.button.button = button
        event.button.state = Gdk.ModifierType(0)
        event.button.window = label.get_window()
        event.button.x, event.button.y = self._label_point(label, offset)
        rect = label.get_toplevel().get_window().get_origin()
        if len(rect) == 3:
            origin_x, origin_y = rect[1], rect[2]
        else:
            origin_x, origin_y = rect[0], rect[1]
        translated = label.translate_coordinates(label.get_toplevel(), 0, 0)
        assert translated is not None
        event.button.x_root = origin_x + translated[0] + event.button.x
        event.button.y_root = origin_y + translated[1] + event.button.y
        return event

    def test_secondary_click_emits_context_and_preserves_selection(self) -> None:
        view = MarkdownView("Select these words")
        self.window.add(view)
        self.window.show_all()
        drain_events()
        label = view.get_children()[0]
        label.select_region(0, 6)
        drain_events()
        requests = []
        view.connect_selection_context_requested(
            lambda *args: requests.append(args[1:])
        )

        event = self._button_event(label, Gdk.BUTTON_SECONDARY, 3)
        handled = label.emit("button-press-event", event)
        drain_events()

        self.assertTrue(handled)
        self.assertEqual(len(requests), 1)
        text, emitted_event, source, root_x, root_y = requests[0]
        self.assertEqual(text, "Select")
        self.assertEqual(emitted_event.button, Gdk.BUTTON_SECONDARY)
        self.assertEqual(emitted_event.time, event.button.time)
        self.assertIs(source, label)
        self.assertAlmostEqual(root_x, event.button.x_root)
        self.assertAlmostEqual(root_y, event.button.y_root)
        self.assertEqual(view.get_selected_text(), "Select")

    def test_plain_click_defers_to_native_selection_without_fallback_highlight(self) -> None:
        view = MarkdownView("Old selection then caret")
        self.window.add(view)
        self.window.show_all()
        drain_events()
        label = view.get_children()[0]
        label.select_region(0, 3)
        drain_events()
        self.assertEqual(view.get_selected_text(), "Old")

        press = self._button_event(label, Gdk.BUTTON_PRIMARY, 15)
        release = self._button_event(
            label,
            Gdk.BUTTON_PRIMARY,
            15,
            Gdk.EventType.BUTTON_RELEASE,
        )
        self.assertFalse(label.emit("button-press-event", press))
        self.assertFalse(label.emit("button-release-event", release))
        drain_events()

        self.assertEqual(view.get_selected_text(), "Old")
        self.assertFalse(view._native_selection_fallbacks)
        self.assertIsNone(label.get_attributes())

    def test_keyboard_context_api_uses_selected_block_as_anchor(self) -> None:
        view = MarkdownView("Keyboard selection")
        self.window.add(view)
        self.window.show_all()
        drain_events()
        label = view.get_children()[0]
        label.select_region(0, 8)
        drain_events()
        requests = []
        view.connect_selection_context_requested(
            lambda *args: requests.append(args[1:])
        )

        self.assertTrue(view.request_selection_context(source_widget=label))
        text, event, source, root_x, root_y = requests[0]
        self.assertEqual(text, "Keyboard")
        self.assertIsNone(event)
        self.assertIs(source, label)
        self.assertGreater(root_x, 0)
        self.assertGreater(root_y, 0)

    def test_context_is_not_requested_without_selection(self) -> None:
        view = MarkdownView("No selection")
        requests = []
        view.connect_selection_context_requested(
            lambda *args: requests.append(args[1:])
        )
        self.assertFalse(view.request_selection_context())
        self.assertEqual(requests, [])

    def test_escape_clears_selection_and_emits_empty_state(self) -> None:
        view = MarkdownView("Dismiss me")
        self.window.add(view)
        self.window.show_all()
        drain_events()
        label = view.get_children()[0]
        label.select_region(0, -1)
        drain_events()
        changes = []
        view.connect_selection_changed(lambda _view, text: changes.append(text))

        event = Gdk.Event.new(Gdk.EventType.KEY_PRESS)
        event.key.keyval = Gdk.KEY_Escape
        event.key.state = Gdk.ModifierType(0)
        event.key.window = label.get_window()
        self.assertTrue(label.emit("key-press-event", event))
        drain_events()

        self.assertEqual(view.get_selected_text(), "")
        self.assertEqual(changes[-1], "")


if __name__ == "__main__":
    unittest.main()
