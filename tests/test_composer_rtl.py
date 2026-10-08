"""GTK-level regression tests for the main composer direction lifecycle."""

from __future__ import annotations

import time
import unittest

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GLib, Gtk  # noqa: E402

from app import (  # noqa: E402
    COMPOSER_RTL_MARK,
    MainWindow,
    composer_is_rtl,
    composer_paragraph_is_rtl,
)


RTL_SAMPLES = {
    "Persian": "\u0633\u0644\u0627\u0645 \u062f\u0646\u06cc\u0627",
    "Arabic": "\u0645\u0631\u062d\u0628\u0627 \u0628\u0627\u0644\u0639\u0627\u0644\u0645",
    "Hebrew": "שלום עולם",
    "Urdu": "\u0627\u0631\u062f\u0648 \u0632\u0628\u0627\u0646",
    "Syriac": "\u0710\u0712\u0713 \u0715\u0717\u0718",
    "Thaana": "\u0780\u07a6\u0781 \u0782\u07a7\u0783",
    "NKo": "\u07ca\u07cb\u07cc \u07cd\u07ce\u07cf",
    "Adlam": "\U0001e900\U0001e922 \U0001e923\U0001e924",
}


class ComposerDirectionHelperTests(unittest.TestCase):
    def test_rtl_scripts_work_after_a_technical_prefix(self) -> None:
        for language, text in RTL_SAMPLES.items():
            with self.subTest(language=language):
                self.assertTrue(composer_is_rtl(f"Spark {text}"))
                self.assertTrue(composer_paragraph_is_rtl(f"Spark {text}"))

    def test_non_ascii_ltr_text_and_neutral_symbols_do_not_flip_direction(self) -> None:
        for text in ["Café déjà vu", "Ελληνικά", "中文", "日本語", "हिन्दी", "👋 🌍", "\u0661\u0662\u0663"]:
            with self.subTest(text=text):
                self.assertFalse(composer_is_rtl(text))
                self.assertFalse(composer_paragraph_is_rtl(text))

    def test_empty_paragraph_keeps_its_deliberate_rtl_default(self) -> None:
        for text in ["", " ", "\n\t"]:
            with self.subTest(text=text):
                self.assertTrue(composer_is_rtl(text))
                self.assertTrue(composer_paragraph_is_rtl(text))


def drain_events() -> None:
    context = GLib.MainContext.default()
    while context.pending():
        context.iteration(False)


class ComposerHarness:
    _input_text = MainWindow._input_text
    _set_composer_rtl = MainWindow._set_composer_rtl
    _is_generated_composer_marker = MainWindow._is_generated_composer_marker
    _composer_line_bounds = MainWindow._composer_line_bounds
    _composer_line_is_rtl = MainWindow._composer_line_is_rtl
    _composer_line_has_user_content = MainWindow._composer_line_has_user_content
    _composer_range_counts = MainWindow._composer_range_counts
    _on_composer_insert_text = MainWindow._on_composer_insert_text
    _on_composer_delete_range = MainWindow._on_composer_delete_range
    _composer_line_scan = MainWindow._composer_line_scan
    _delete_composer_markers = MainWindow._delete_composer_markers
    _set_composer_line_tag = MainWindow._set_composer_line_tag
    _normalise_composer_line = MainWindow._normalise_composer_line
    _discard_composer_direction_job = MainWindow._discard_composer_direction_job
    _run_composer_direction_jobs = MainWindow._run_composer_direction_jobs
    _drain_composer_direction_jobs = MainWindow._drain_composer_direction_jobs
    _schedule_composer_direction_jobs = MainWindow._schedule_composer_direction_jobs
    _clear_composer_direction_jobs = MainWindow._clear_composer_direction_jobs
    _sync_composer_direction_markers = MainWindow._sync_composer_direction_markers
    _composer_user_offset = MainWindow._composer_user_offset
    _composer_snapshot = MainWindow._composer_snapshot
    _composer_iter_at_user_offset = MainWindow._composer_iter_at_user_offset
    _apply_composer_snapshot = MainWindow._apply_composer_snapshot
    _reset_composer_history = MainWindow._reset_composer_history
    _on_composer_user_action_begin = MainWindow._on_composer_user_action_begin
    _on_composer_user_action_end = MainWindow._on_composer_user_action_end
    _undo_composer_edit = MainWindow._undo_composer_edit
    _redo_composer_edit = MainWindow._redo_composer_edit
    _reset_enter_send_confirmation = MainWindow._reset_enter_send_confirmation
    on_input_key = MainWindow.on_input_key
    on_draft_changed = MainWindow.on_draft_changed

    def __init__(self) -> None:
        self.input_view = Gtk.TextView()
        self.input_view.set_direction(Gtk.TextDirection.RTL)
        self.input_view.set_justification(Gtk.Justification.LEFT)
        self.input_placeholder = Gtk.Label(label="Message Codex…")
        self.input_placeholder.set_halign(Gtk.Align.END)
        self.input_placeholder.set_direction(Gtk.TextDirection.LTR)
        text_buffer = self.input_view.get_buffer()
        self._composer_rtl_tag = text_buffer.create_tag(
            None,
            direction=Gtk.TextDirection.RTL,
            justification=Gtk.Justification.LEFT,
        )
        self._composer_ltr_tag = text_buffer.create_tag(
            None,
            direction=Gtk.TextDirection.LTR,
            justification=Gtk.Justification.LEFT,
        )
        self._composer_direction_marker_tag = text_buffer.create_tag(
            None,
        )
        self._loading_room = True
        self._syncing_composer_direction = False
        self._closing = False
        self._composer_direction_jobs = []
        self._composer_direction_idle_source_id = None
        self._composer_user_char_count = 0
        self._composer_non_whitespace_count = 0
        self._composer_generated_marker_count = 0
        self._composer_active_line_rtl = True
        self._composer_undo_stack = []
        self._composer_redo_stack = []
        self._composer_history_before = None
        self._composer_history_depth = 0
        self._composer_history_limit = 100
        self._restoring_composer_history = False
        self._draft_save_source_id = None
        self._enter_send_armed_until = 0.0
        self._enter_send_hint_source_id = None
        self._composer_notice_source_id = None
        text_buffer.connect("insert-text", self._on_composer_insert_text)
        text_buffer.connect("delete-range", self._on_composer_delete_range)
        text_buffer.connect("changed", self.on_draft_changed)
        text_buffer.connect(
            "begin-user-action", self._on_composer_user_action_begin
        )
        text_buffer.connect(
            "end-user-action", self._on_composer_user_action_end
        )

    def update_composer_state(self) -> None:
        pass


class ComposerDirectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        ready, _args = Gtk.init_check([])
        if not ready:
            raise unittest.SkipTest("GTK display is unavailable")

    def assert_direction(
        self,
        composer: ComposerHarness,
        direction: Gtk.TextDirection,
        justification: Gtk.Justification,
        alignment: Gtk.Align,
    ) -> None:
        self.assertEqual(composer.input_view.get_direction(), direction)
        self.assertEqual(composer.input_view.get_justification(), justification)
        self.assertEqual(composer.input_placeholder.get_halign(), alignment)
        self.assertEqual(
            composer.input_placeholder.get_direction(), Gtk.TextDirection.LTR
        )

    def test_typing_mixed_text_and_clearing_restores_rtl_default(self) -> None:
        composer = ComposerHarness()
        text_buffer = composer.input_view.get_buffer()

        text_buffer.set_text("Spark")
        drain_events()
        self.assert_direction(
            composer,
            Gtk.TextDirection.LTR,
            Gtk.Justification.LEFT,
            Gtk.Align.START,
        )

        for character in " \u0627\u06cc\u0646 \u0627\u062f\u0627\u0645\u0647 \u0641\u0627\u0631\u0633\u06cc \u0627\u0633\u062a":
            text_buffer.insert(text_buffer.get_end_iter(), character)
        drain_events()
        self.assert_direction(
            composer,
            Gtk.TextDirection.RTL,
            Gtk.Justification.LEFT,
            Gtk.Align.END,
        )

        text_buffer.set_text("")
        drain_events()
        self.assert_direction(
            composer,
            Gtk.TextDirection.RTL,
            Gtk.Justification.LEFT,
            Gtk.Align.END,
        )

    def test_english_prefix_with_persian_stays_on_the_physical_right(self) -> None:
        composer = ComposerHarness()
        window = Gtk.Window()
        window.set_default_size(720, 80)
        window.add(composer.input_view)
        window.show_all()
        text_buffer = composer.input_view.get_buffer()
        draft = "Spark \u0627\u062f\u0627\u0645\u0647 \u0641\u0627\u0631\u0633\u06cc\nEnglish continuation"
        text_buffer.set_text(draft)
        drain_events()

        raw_text = text_buffer.get_text(
            text_buffer.get_start_iter(), text_buffer.get_end_iter(), True
        )
        self.assertEqual(composer._input_text(), draft)
        self.assertEqual(raw_text.count(COMPOSER_RTL_MARK), 1)
        first_line = text_buffer.get_iter_at_line(0)
        rect = composer.input_view.get_iter_location(first_line)
        self.assertGreater(
            rect.x,
            composer.input_view.get_allocation().width * 0.8,
        )
        english_line = text_buffer.get_iter_at_line(1)
        self.assertLess(
            composer.input_view.get_iter_location(english_line).x,
            composer.input_view.get_allocation().width * 0.2,
        )
        window.destroy()

    def test_all_rtl_scripts_use_markers_without_changing_sent_text(self) -> None:
        for language, text in RTL_SAMPLES.items():
            with self.subTest(language=language):
                composer = ComposerHarness()
                draft = f"Spark {text}"
                text_buffer = composer.input_view.get_buffer()
                text_buffer.set_text(draft)
                drain_events()
                self.assertEqual(composer._input_text(), draft)
                self.assertEqual(composer.input_view.get_direction(), Gtk.TextDirection.RTL)
                self.assertEqual(composer._composer_generated_marker_count, 1)

    def test_non_ascii_ltr_paragraphs_do_not_get_generated_markers(self) -> None:
        composer = ComposerHarness()
        for draft in ["Café déjà vu", "Ελληνικά", "中文", "日本語", "👋 🌍"]:
            with self.subTest(draft=draft):
                composer.input_view.get_buffer().set_text(draft)
                drain_events()
                self.assertEqual(composer._input_text(), draft)
                self.assertEqual(composer.input_view.get_direction(), Gtk.TextDirection.LTR)
                self.assertEqual(composer._composer_generated_marker_count, 0)

    def test_inserting_ltr_unicode_and_spaces_into_long_line_keeps_ltr(self) -> None:
        composer = ComposerHarness()
        text_buffer = composer.input_view.get_buffer()
        text_buffer.set_text("x" * 1000)
        drain_events()
        for inserted in [" ", "é", "中", "👋"]:
            with self.subTest(inserted=inserted):
                text_buffer.insert(text_buffer.get_end_iter(), inserted)
                drain_events()
                self.assertEqual(composer.input_view.get_direction(), Gtk.TextDirection.LTR)
                self.assertEqual(composer._composer_generated_marker_count, 0)

    def test_deleting_last_rtl_character_from_long_line_restores_ltr(self) -> None:
        composer = ComposerHarness()
        text_buffer = composer.input_view.get_buffer()
        draft = "x" * 1000
        text_buffer.set_text(draft + "\U0001e900")
        drain_events()
        self.assertEqual(composer.input_view.get_direction(), Gtk.TextDirection.RTL)
        end = text_buffer.get_end_iter()
        previous = end.copy()
        previous.backward_char()
        text_buffer.delete(previous, end)
        drain_events()
        self.assertEqual(composer._input_text(), draft)
        self.assertEqual(composer.input_view.get_direction(), Gtk.TextDirection.LTR)
        self.assertEqual(composer._composer_generated_marker_count, 0)

    def test_large_multiline_draft_is_bounded_and_english_lines_stay_left(self) -> None:
        composer = ComposerHarness()
        window = Gtk.Window()
        window.set_default_size(720, 240)
        window.add(composer.input_view)
        window.show_all()
        draft = "\n".join(
            f"English line {index:03d} " + ("x" * 68)
            for index in range(200)
        )
        started = time.perf_counter()
        composer.input_view.get_buffer().set_text(draft)
        drain_events()
        elapsed = time.perf_counter() - started
        self.assertLess(elapsed, 0.75)
        self.assertEqual(composer._input_text(), draft)
        middle = composer.input_view.get_buffer().get_iter_at_line(100)
        self.assertLess(
            composer.input_view.get_iter_location(middle).x,
            composer.input_view.get_allocation().width * 0.2,
        )
        window.destroy()

    def test_large_ascii_line_backspace_does_not_rescan_the_draft(self) -> None:
        composer = ComposerHarness()
        text_buffer = composer.input_view.get_buffer()
        draft = "x" * 100_000
        text_buffer.set_text(draft)
        drain_events()

        end = text_buffer.get_end_iter()
        previous = end.copy()
        previous.backward_char()
        started = time.perf_counter()
        text_buffer.delete(previous, end)
        drain_events()

        self.assertLess(time.perf_counter() - started, 0.5)
        self.assertEqual(composer._input_text(), draft[:-1])

    def test_literal_direction_mark_is_preserved_in_the_sent_text(self) -> None:
        composer = ComposerHarness()
        draft = "A\u200fB"
        composer.input_view.get_buffer().set_text(draft)
        drain_events()

        self.assertEqual(composer._input_text(), draft)

    def test_backspace_before_a_generated_line_marker_merges_the_lines(self) -> None:
        composer = ComposerHarness()
        text_buffer = composer.input_view.get_buffer()
        text_buffer.set_text("first\n\u0633\u0644\u0627\u0645")
        drain_events()
        line_start = text_buffer.get_iter_at_line(1)
        cursor = line_start.copy()
        cursor.forward_char()
        text_buffer.place_cursor(cursor)
        event = type(
            "BackspaceEvent",
            (),
            {
                "keyval": Gdk.KEY_BackSpace,
                "state": Gdk.ModifierType(0),
                "hardware_keycode": 0,
            },
        )()
        self.assertTrue(composer.on_input_key(composer.input_view, event))
        drain_events()
        self.assertEqual(composer._input_text(), "first\u0633\u0644\u0627\u0645")

    @staticmethod
    def _edit(composer: ComposerHarness, text: str) -> None:
        text_buffer = composer.input_view.get_buffer()
        text_buffer.begin_user_action()
        try:
            text_buffer.insert_at_cursor(text)
        finally:
            text_buffer.end_user_action()
        drain_events()

    @staticmethod
    def _shortcut(
        composer: ComposerHarness,
        keyval: int,
        state: Gdk.ModifierType,
    ) -> bool:
        event = type(
            "ShortcutEvent",
            (),
            {"keyval": keyval, "state": state, "hardware_keycode": 0},
        )()
        return composer.on_input_key(composer.input_view, event)

    def test_ctrl_z_and_both_redo_shortcuts_restore_composer_edits(self) -> None:
        composer = ComposerHarness()
        control = Gdk.ModifierType.CONTROL_MASK
        shift_control = control | Gdk.ModifierType.SHIFT_MASK

        self._edit(composer, "hello")
        self.assertTrue(self._shortcut(composer, Gdk.KEY_z, control))
        self.assertEqual(composer._input_text(), "")
        self.assertTrue(self._shortcut(composer, Gdk.KEY_z, shift_control))
        self.assertEqual(composer._input_text(), "hello")
        self.assertTrue(self._shortcut(composer, Gdk.KEY_z, control))
        self.assertTrue(self._shortcut(composer, Gdk.KEY_y, control))
        self.assertEqual(composer._input_text(), "hello")

    def test_new_edit_after_undo_discards_redo_branch(self) -> None:
        composer = ComposerHarness()
        control = Gdk.ModifierType.CONTROL_MASK

        self._edit(composer, "old")
        self._shortcut(composer, Gdk.KEY_z, control)
        self._edit(composer, "new")
        self._shortcut(composer, Gdk.KEY_y, control)

        self.assertEqual(composer._input_text(), "new")
        self.assertEqual(composer._composer_redo_stack, [])

    def test_persian_undo_history_ignores_generated_direction_markers(self) -> None:
        composer = ComposerHarness()
        control = Gdk.ModifierType.CONTROL_MASK
        text_buffer = composer.input_view.get_buffer()

        self._edit(composer, "\u0633\u0644\u0627\u0645")
        raw = text_buffer.get_text(
            text_buffer.get_start_iter(), text_buffer.get_end_iter(), True
        )
        self.assertEqual(raw.count(COMPOSER_RTL_MARK), 1)
        self._shortcut(composer, Gdk.KEY_z, control)
        self.assertEqual(composer._input_text(), "")
        self._shortcut(composer, Gdk.KEY_y, control)
        self.assertEqual(composer._input_text(), "\u0633\u0644\u0627\u0645")
        raw = text_buffer.get_text(
            text_buffer.get_start_iter(), text_buffer.get_end_iter(), True
        )
        self.assertEqual(raw.count(COMPOSER_RTL_MARK), 1)


if __name__ == "__main__":
    unittest.main()
