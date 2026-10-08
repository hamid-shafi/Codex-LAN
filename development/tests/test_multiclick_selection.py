"""Regression coverage for word/line selection in rendered messages.

Gtk.Label normally makes a text caret visible when it is made selectable.  A
message is read-only though: pointer gestures should select useful text, not
turn a transcript line into a text editor.
"""

from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path
from typing import Callable

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GLib, Gtk, Pango

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from project_layout import configure_imports, source_file
configure_imports(__file__)

from markdown_view import MarkdownView


def drain_events() -> None:
    context = GLib.MainContext.default()
    while context.pending():
        context.iteration(False)


def drain_until(predicate: Callable[[], bool], timeout: float = 0.1) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        drain_events()
        if predicate():
            return True
        time.sleep(0.001)
    drain_events()
    return predicate()


class MultiClickSelectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        ready, _args = Gtk.init_check([])
        if not ready:
            raise unittest.SkipTest("GTK display is unavailable")

    def setUp(self) -> None:
        self.window = Gtk.Window()
        self.window.set_default_size(560, 240)
        self.view = MarkdownView("alpha beta gamma\nfirst line\nsecond line")
        self.window.add(self.view)
        self.window.show_all()
        drain_events()
        self.label = self.view.get_children()[0]
        self.assertIsInstance(self.label, Gtk.Label)

    def tearDown(self) -> None:
        self.view.clear_selection()
        drain_events()
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

    def _click_event(
        self,
        offset: int,
        event_type: Gdk.EventType,
        label: Gtk.Label | None = None,
    ) -> Gdk.Event:
        label = label or self.label
        event = Gdk.Event.new(event_type)
        event.button.button = Gdk.BUTTON_PRIMARY
        event.button.state = Gdk.ModifierType(0)
        event.button.window = label.get_window()
        event.button.x, event.button.y = self._label_point(label, offset)
        origin = label.get_toplevel().get_window().get_origin()
        origin_x, origin_y = (
            (origin[1], origin[2]) if len(origin) == 3 else (origin[0], origin[1])
        )
        translated = label.translate_coordinates(label.get_toplevel(), 0, 0)
        self.assertIsNotNone(translated)
        event.button.x_root = origin_x + translated[0] + event.button.x
        event.button.y_root = origin_y + translated[1] + event.button.y
        return event

    def test_double_click_selects_one_word_with_focusable_native_prose(self) -> None:
        event = self._click_event(7, Gdk.EventType._2BUTTON_PRESS)

        self.assertTrue(self.label.emit("button-press-event", event))
        drain_events()

        self.assertEqual(self.view.get_selected_text(), "beta")
        self.assertTrue(self.label.get_can_focus())

    def test_double_click_uses_layout_coordinates_for_mixed_rtl_and_ltr_words(self) -> None:
        self.window.remove(self.view)
        self.view = MarkdownView("Deploy \u0627\u06cc\u0646 \u0627\u062f\u0627\u0645\u0647 \u0641\u0627\u0631\u0633\u06cc \u0627\u0633\u062a \u0648 Spark \u0628\u0627\u06cc\u062f \u062e\u0648\u0627\u0646\u0627 \u0628\u0645\u0627\u0646\u062f")
        self.window.add(self.view)
        self.window.show_all()
        drain_events()
        self.label = self.view.get_children()[0]
        self.assertIsInstance(self.label, Gtk.Label)

        for word in ("Deploy", "Spark", "\u0628\u0627\u06cc\u062f"):
            with self.subTest(word=word):
                rendered_offset = self.label.get_text().index(word) + len(word) // 2
                event = self._click_event(
                    rendered_offset, Gdk.EventType._2BUTTON_PRESS
                )

                self.assertTrue(self.label.emit("button-press-event", event))
                drain_events()
                self.assertEqual(self.view.get_selected_text(), word)

    def test_mapped_prose_starts_unfocused_before_idle_enables_native_selection(self) -> None:
        window = Gtk.Window()
        view = MarkdownView("A mapped transcript line")
        window.add(view)
        label = view.get_children()[0]
        self.assertIsInstance(label, Gtk.Label)
        self.assertFalse(label.get_can_focus())
        window.show_all()
        try:
            self.assertTrue(label.get_mapped())
            self.assertFalse(label.get_can_focus())
            self.assertFalse(label.get_selection_bounds()[0])

            drain_events()

            self.assertTrue(label.get_can_focus())
            self.assertFalse(label.get_selection_bounds()[0])
        finally:
            window.destroy()
            drain_events()

    def test_stale_empty_native_notification_restores_cached_primary_selection(self) -> None:
        event = self._click_event(7, Gdk.EventType._2BUTTON_PRESS)
        self.assertTrue(self.label.emit("button-press-event", event))
        drain_events()
        self.assertEqual(self.view.get_selected_text(), "beta")

        self.label.select_region(0, 0)
        drain_events()

        self.assertEqual(self.view.get_selected_text(), "beta")

        next_press = self._click_event(1, Gdk.EventType.BUTTON_PRESS)
        stop_default = self.label.connect(
            "button-press-event", lambda *_arguments: True
        )
        try:
            self.assertTrue(self.label.emit("button-press-event", next_press))
        finally:
            self.label.disconnect(stop_default)
        self.label.select_region(0, 0)
        drain_events()

        self.assertEqual(self.view.get_selected_text(), "")

    def test_silent_empty_native_range_returns_cached_primary_text_and_recovers(self) -> None:
        event = self._click_event(7, Gdk.EventType._2BUTTON_PRESS)
        self.assertTrue(self.label.emit("button-press-event", event))
        drain_events()
        self.assertEqual(self.view.get_selected_text(), "beta")

        self.label.select_region(0, 0)
        self.view._cancel_native_selection_recovery()

        self.assertEqual(self.view.get_selected_text(), "beta")
        self.assertTrue(
            drain_until(lambda: self.label.get_selection_bounds() == (True, 6, 10))
        )

    def test_new_bubble_primary_selection_relinquishes_old_cached_owner(self) -> None:
        other_window = Gtk.Window()
        other_view = MarkdownView("delta epsilon zeta")
        other_window.add(other_view)
        other_window.show_all()
        drain_events()
        other_label = other_view.get_children()[0]
        self.assertIsInstance(other_label, Gtk.Label)

        try:
            first_event = self._click_event(7, Gdk.EventType._2BUTTON_PRESS)
            self.assertTrue(self.label.emit("button-press-event", first_event))
            drain_events()
            self.assertEqual(self.view.get_selected_text(), "beta")
            self.assertIsNotNone(self.view._native_selection_cache)

            other_offset = other_label.get_text().index("epsilon") + 2
            other_event = self._click_event(
                other_offset, Gdk.EventType._2BUTTON_PRESS, other_label
            )
            self.assertTrue(other_label.emit("button-press-event", other_event))
            drain_events()

            self.assertEqual(self.view.get_selected_text(), "")
            self.assertIsNone(self.view._native_selection_cache)
            self.assertIsNone(self.view._active_selection_widget)
            self.assertEqual(other_view.get_selected_text(), "epsilon")
        finally:
            other_view.clear_selection()
            drain_events()
            other_window.destroy()
            drain_events()

    def test_triple_click_selects_the_clicked_source_line(self) -> None:
        event = self._click_event(18, Gdk.EventType._3BUTTON_PRESS)

        self.assertTrue(self.label.emit("button-press-event", event))
        drain_events()

        self.assertEqual(self.view.get_selected_text(), "first line")

    def test_selectable_prose_uses_native_focus_with_a_hidden_caret_style(self) -> None:
        self.assertTrue(self.label.get_selectable())
        self.assertTrue(self.label.get_can_focus())
        self.assertTrue(self.label.get_focus_on_click())
        self.assertTrue(
            self.label.get_style_context().has_class("markdown-prose")
        )


if __name__ == "__main__":
    unittest.main()
