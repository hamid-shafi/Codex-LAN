# -*- coding: utf-8 -*-
"""Focused regression tests for the native Markdown/RTL renderer."""

from __future__ import annotations

import gc
import sys
import unittest
from pathlib import Path
from unittest import mock

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, Gtk, Pango  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import markdown_view  # noqa: E402
from markdown_view import (  # noqa: E402
    MarkdownView,
    content_prefers_rtl,
    inline_markup,
    is_rtl,
    split_blocks,
)


class DirectionTests(unittest.TestCase):
    def test_unicode_strong_direction_covers_all_supported_rtl_scripts(self) -> None:
        samples = {
            "Arabic": "\u0645\u0631\u062d\u0628\u0627 \u0628\u0627\u0644\u0639\u0627\u0644\u0645",
            "Hebrew": "שלום עולם",
            "Urdu": "\u0627\u0631\u062f\u0648 \u0632\u0628\u0627\u0646",
            "Syriac": "\u0710\u0712\u0713 \u0715\u0717\u0718",
            "Thaana": "\u0780\u07a6\u0781 \u0782\u07a7\u0783",
            "NKo": "\u07ca\u07cb\u07cc \u07cd\u07ce\u07cf",
            "Adlam": "\U0001e900\U0001e922 \U0001e923\U0001e924",
        }
        for language, text in samples.items():
            with self.subTest(language=language):
                self.assertTrue(is_rtl(text))
                self.assertFalse(is_rtl(f"Spark {text}"))
                self.assertTrue(content_prefers_rtl(f"Spark {text}"))
                view = MarkdownView(f"Spark {text}")
                self.assertTrue(view.message_rtl)
                self.assertEqual(view.get_children()[0].get_direction(), Gtk.TextDirection.RTL)

    def test_non_ascii_ltr_letters_participate_in_first_strong_direction(self) -> None:
        for text in ["Café", "Ελληνικά", "中文", "日本語", "हिन्दी"]:
            with self.subTest(text=text):
                self.assertFalse(is_rtl(f"{text} שלום"))
                self.assertFalse(content_prefers_rtl(f"{text} שלום"))
                self.assertFalse(MarkdownView(text).message_rtl)
        self.assertFalse(is_rtl("\u0661\u0662\u0663"))

    def test_combining_marks_and_joiners_do_not_add_direction_votes(self) -> None:
        self.assertFalse(content_prefers_rtl("English sentence with שָׁלוֹם as one label"))
        self.assertFalse(content_prefers_rtl("English sentence with \u06af\u0641\u062a\u200c\u0648\u06af\u0648 as one label"))
        self.assertTrue(content_prefers_rtl("Café שָׁלוֹם עוֹלָם"))

    def test_rtl_text_in_code_fences_does_not_change_code_direction(self) -> None:
        view = MarkdownView("```text\nשלום \U0001e900\U0001e922\n```\n\nEnglish explanation")
        code_view = next(
            widget for widget in view._selection_widgets
            if isinstance(widget, Gtk.TextView)
        )
        self.assertEqual(code_view.get_direction(), Gtk.TextDirection.LTR)
        self.assertFalse(view.message_rtl)

    def test_first_strong_helper_remains_predictable(self) -> None:
        self.assertTrue(is_rtl("\u0627\u06cc\u0646 Spark \u0627\u0633\u062a"))
        self.assertFalse(is_rtl("Spark \u06cc\u06a9 \u0627\u0628\u0632\u0627\u0631 \u0627\u0633\u062a"))
        self.assertTrue(content_prefers_rtl("Spark \u0627\u06cc\u0646 \u0627\u062f\u0627\u0645\u0647\u0654 \u0641\u0627\u0631\u0633\u06cc \u0627\u0633\u062a"))

    def test_english_prefix_with_persian_continuation_renders_rtl(self) -> None:
        view = MarkdownView(
            "Spark \u06cc\u06a9 \u067e\u0631\u062f\u0627\u0632\u0634 \u062c\u062f\u0627\u0633\u062a\n\nexit code 0 \u06cc\u0639\u0646\u06cc \u0645\u0648\u0641\u0642 \u0628\u0648\u062f"
        )
        labels = [child for child in view.get_children() if isinstance(child, Gtk.Label)]
        self.assertEqual(len(labels), 2)
        self.assertTrue(view.message_rtl)
        self.assertTrue(all(label.get_direction() == Gtk.TextDirection.RTL for label in labels))
        self.assertTrue(all(not label.get_layout().get_auto_dir() for label in labels))

    def test_one_persian_term_does_not_flip_english_prose(self) -> None:
        text = "This English explanation mentions the \u0641\u0627\u0631\u0633\u06cc term only once."
        self.assertFalse(content_prefers_rtl(text))
        self.assertFalse(
            content_prefers_rtl(
                "This English dialog uses \u06af\u0641\u062a\u200c\u0648\u06af\u0648 as one localized label."
            )
        )
        view = MarkdownView(text)
        label = view.get_children()[0]
        self.assertEqual(label.get_direction(), Gtk.TextDirection.LTR)

    def test_inline_code_and_url_do_not_outvote_persian_prose(self) -> None:
        text = "`very_long_technical_identifier` \u0627\u06cc\u0646 \u062e\u0637\u0627 \u0628\u0627\u06cc\u062f \u0628\u0631\u0631\u0633\u06cc \u0634\u0648\u062f https://example.com"
        self.assertTrue(content_prefers_rtl(text))
        label = MarkdownView(text).get_children()[0]
        self.assertEqual(label.get_direction(), Gtk.TextDirection.RTL)

    def test_rtl_block_survives_realise_and_allocation(self) -> None:
        window = Gtk.Window()
        window.set_default_size(640, 240)
        view = MarkdownView(
            "\u0627\u06cc\u0646 \u06cc\u06a9 \u0628\u0646\u062f \u0641\u0627\u0631\u0633\u06cc \u0628\u0627 Spark \u0627\u0633\u062a\n\n\u0627\u06cc\u0646 \u0628\u0646\u062f \u062f\u0648\u0645 \u0641\u0627\u0631\u0633\u06cc \u0627\u0633\u062a"
        )
        window.add(view)
        window.show_all()
        while Gtk.events_pending():
            Gtk.main_iteration_do(False)
        try:
            for label in view.get_children():
                self.assertFalse(label.get_layout().get_auto_dir())
                self.assertEqual(
                    label.get_layout().get_alignment(), Pango.Alignment.RIGHT
                )
                # Regression: xalign=1 on an RTL Gtk.Label mirrored the whole
                # layout off the left edge despite Pango being right-aligned.
                self.assertGreaterEqual(label.get_layout_offsets()[0], 0)
        finally:
            window.destroy()

    def test_english_message_stays_ltr_even_for_user(self) -> None:
        view = MarkdownView("This is an English message.", user_message=True)
        label = view.get_children()[0]
        self.assertFalse(view.message_rtl)
        self.assertEqual(label.get_direction(), Gtk.TextDirection.LTR)

    def test_code_fence_does_not_choose_prose_direction(self) -> None:
        view = MarkdownView("```python\nprint('first')\n```\n\n\u0627\u06cc\u0646 \u067e\u0627\u0633\u062e \u0641\u0627\u0631\u0633\u06cc \u0627\u0633\u062a")
        self.assertTrue(view.message_rtl)

    def test_mixed_message_does_not_mirror_english_blocks(self) -> None:
        view = MarkdownView(
            "English heading and controls\n\n"
            "\u0627\u06cc\u0646 \u06cc\u06a9 \u0628\u0646\u062f \u06a9\u0627\u0645\u0644\u0627\u064b \u0641\u0627\u0631\u0633\u06cc \u0627\u0633\u062a\n\n"
            "RTL regression sample (Persian): \u0627\u06cc\u0646 \u0645\u062a\u0646 \u0628\u0627\u06cc\u062f \u062e\u0648\u0627\u0646\u0627 \u0628\u0645\u0627\u0646\u062f"
        )
        labels = [child for child in view.get_children() if isinstance(child, Gtk.Label)]
        self.assertEqual(
            [label.get_direction() for label in labels],
            [Gtk.TextDirection.LTR, Gtk.TextDirection.RTL, Gtk.TextDirection.RTL],
        )
        self.assertEqual(view.get_direction(), Gtk.TextDirection.LTR)

    def test_table_keeps_source_order_and_first_strong_cell_direction(self) -> None:
        view = MarkdownView("| API \u0645\u0642\u062f\u0627\u0631 | value |\n| --- | --- |\n| one | two |")
        grid = view.get_children()[0]
        self.assertIsInstance(grid, Gtk.Grid)
        self.assertEqual(grid.get_direction(), Gtk.TextDirection.LTR)
        first_cell = grid.get_child_at(0, 0)
        self.assertEqual(first_cell.get_direction(), Gtk.TextDirection.LTR)

    def test_formatted_technical_tokens_are_isolated_in_rtl(self) -> None:
        markup = inline_markup("\u0627\u06cc\u0646 **Spark Engine** \u0648 *exit code 0* \u0627\u0633\u062a", rtl_context=True)
        self.assertIn("<b>\u2066Spark Engine\u2069</b>", markup)
        self.assertIn("<i>\u2066exit code 0\u2069</i>", markup)


class InlineMarkdownTests(unittest.TestCase):
    def test_nested_markup_code_spans_and_escaping(self) -> None:
        markup = inline_markup("**bold and *italic*** plus ``a ` b`` and <script>")
        self.assertIn("<b>bold and <i>italic</i></b>", markup)
        self.assertIn("monospace", markup)
        self.assertIn("a ` b", markup)
        self.assertIn("&lt;script&gt;", markup)
        self.assertNotIn("<script>", markup)

    def test_snake_case_is_not_emphasis(self) -> None:
        markup = inline_markup("value_name and __bold__")
        self.assertIn("value_name", markup)
        self.assertIn("<b>bold</b>", markup)
        self.assertNotIn("<i>name", markup)

    def test_web_links_are_safe_and_attributes_are_escaped(self) -> None:
        safe = inline_markup('[docs](https://example.com/a_(b)?x=1&y=2 "title")')
        self.assertIn('href="https://example.com/a_(b)?x=1&amp;y=2"', safe)
        unsafe = inline_markup("[bad](javascript:alert(1))")
        self.assertNotIn("<a href=", unsafe)
        self.assertIn("javascript:alert", unsafe)
        quoted = inline_markup('[x](https://example.com/&quot;><b)')
        self.assertNotIn('"><b>', quoted)

    def test_local_file_link_shows_only_basename(self) -> None:
        destination = (
            "/home/example/Desktop/Example/common-modules/kafka-consumer-service/"
            "src/main/java/com/example/demo/kafka/Buffer.java:6"
        )
        markup = inline_markup(f"[entire ugly path]({destination})", rtl_context=True)
        self.assertIn(f'href="{destination}"', markup)
        self.assertIn("Buffer.java", markup)
        self.assertNotIn("entire ugly path", markup)
        self.assertNotIn("common-modules", markup.split(">", 1)[1])

    def test_local_file_link_supports_spaces_and_line_column(self) -> None:
        destination = "/home/example/My Project/Example File.py:124:9"
        markup = inline_markup(f"[ignored](<{destination}>)")
        self.assertIn('href="/home/example/My Project/Example File.py:124:9"', markup)
        self.assertEqual(
            markdown_view._local_file_reference(destination),
            ("/home/example/My Project/Example File.py", 124, 9),
        )
        label = Gtk.Label()
        label.set_markup(markup)
        self.assertEqual(label.get_text(), "Example File.py")

    def test_local_file_basename_is_not_reparsed_as_markdown(self) -> None:
        underscored = Gtk.Label()
        underscored.set_markup(inline_markup("[module](/tmp/__init__.py:12)"))
        self.assertEqual(underscored.get_text(), "__init__.py")
        marked_up = Gtk.Label()
        marked_up.set_markup(inline_markup("[other](/tmp/**safe**.txt:3)"))
        self.assertEqual(marked_up.get_text(), "**safe**.txt")

    def test_local_path_autolink_also_shows_only_basename(self) -> None:
        destination = "/home/example/project/Buffer.java:124"
        markup = inline_markup(f"<{destination}>")
        self.assertIn(f'href="{destination}"', markup)
        label = Gtk.Label()
        label.set_markup(markup)
        self.assertEqual(label.get_text(), "Buffer.java")

    def test_local_file_activation_strips_source_position(self) -> None:
        destination = "/home/example/project/Buffer.java:124:9"
        view = MarkdownView()
        with mock.patch.object(
            markdown_view.GLib,
            "filename_to_uri",
            return_value="file:///home/example/project/Buffer.java",
        ) as filename_to_uri, mock.patch.object(
            markdown_view.Gtk, "show_uri_on_window"
        ) as show_uri:
            self.assertTrue(view._activate_link(Gtk.Label(), destination))
        filename_to_uri.assert_called_once_with(
            "/home/example/project/Buffer.java", None
        )
        self.assertEqual(
            show_uri.call_args.args[1], "file:///home/example/project/Buffer.java"
        )

    def test_relative_and_unsafe_local_links_remain_plain_text(self) -> None:
        relative = inline_markup("[file](src/main.py:12)")
        self.assertNotIn("<a href=", relative)
        controlled = inline_markup("[file](/tmp/a\x00.py:12)")
        self.assertNotIn("<a href=", controlled)
        for destination in ("/tmp/a\tb.py:12", "/tmp/a\rb.py:12"):
            self.assertIsNone(markdown_view._local_file_reference(destination))
        for destination in ("/", "/.", "/tmp/..", "/tmp/folder/"):
            self.assertIsNone(markdown_view._local_file_reference(destination))

    def test_autolink_and_bare_url_keep_sentence_punctuation_outside(self) -> None:
        explicit = inline_markup("<https://example.com/path>")
        self.assertIn('<a href="https://example.com/path">', explicit)
        bare = inline_markup("\u0628\u0628\u06cc\u0646 https://example.com/a_(b).", rtl_context=True)
        self.assertIn('href="https://example.com/a_(b)"', bare)
        self.assertNotIn(".</a>", bare)

    def test_remote_image_is_not_loaded_as_an_image(self) -> None:
        markup = inline_markup("![\u0646\u0645\u0648\u062f\u0627\u0631](https://example.com/chart.png)")
        self.assertIn("Image:", markup)
        self.assertIn("<a href=", markup)
        self.assertNotIn("<img", markup)

    def test_partial_and_nested_links_remain_valid_during_streaming(self) -> None:
        partial = inline_markup("[docs](https://example.com/incomplete")
        self.assertNotIn("<a href=", partial)
        nested = inline_markup(
            "[outer [inner](https://inner.example)](https://outer.example)"
        )
        self.assertEqual(nested.count("<a href="), 1)
        label = Gtk.Label()
        label.set_markup(nested)
        self.assertIn("outer", label.get_text())

    def test_control_characters_cannot_break_pango_markup(self) -> None:
        markup = inline_markup("ok\x00 <tag> \ud800")
        self.assertNotIn("\x00", markup)
        self.assertNotIn("\ud800", markup)
        self.assertIn("&lt;tag&gt;", markup)


class BlockParsingTests(unittest.TestCase):
    def test_backtick_and_tilde_fences_with_streaming_partial(self) -> None:
        self.assertEqual(
            split_blocks("~~~python extra metadata\nprint(1)\n~~~~"),
            [("code", "python", "print(1)")],
        )
        self.assertEqual(
            split_blocks("````cpp\na ``` token\n````"),
            [("code", "cpp", "a ``` token")],
        )
        self.assertEqual(split_blocks("```py\nprint(1)"), [("code", "py", "print(1)")])

    def test_different_or_short_fence_does_not_close_code(self) -> None:
        blocks = split_blocks("````cpp\na\n```\n~~~\n````")
        self.assertEqual(blocks, [("code", "cpp", "a\n```\n~~~")])

    def test_crlf_is_normalised_and_language_is_sanitised(self) -> None:
        blocks = split_blocks("```<script>\r\nx\r\n```\r\n")
        self.assertEqual(blocks[0], ("code", "", "x"))

    def test_table_supports_escaped_and_code_span_pipes(self) -> None:
        source = (
            "| \u0646\u0627\u0645 | \u0646\u0645\u0648\u0646\u0647 |\n| --- | --- |\n"
            "| API | \u0645\u0642\u062f\u0627\u0631 \\| \u062f\u06cc\u06af\u0631 |\n| code | `a|b` |"
        )
        blocks = split_blocks(source)
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0][0], "table")
        view = MarkdownView(source)
        grid = view.get_children()[0]
        self.assertIsInstance(grid, Gtk.Grid)
        self.assertTrue(grid.get_column_homogeneous())
        self.assertEqual(len(grid.get_children()), 6)

    def test_invalid_table_column_count_is_left_as_prose(self) -> None:
        blocks = split_blocks("| a | b |\n| --- |\n| x | y |")
        self.assertEqual(blocks[0][0], "prose")

    def test_headings_lists_quotes_and_rules_render_without_html(self) -> None:
        source = (
            "# \u0639\u0646\u0648\u0627\u0646 <unsafe>\n\n- [x] \u0627\u0646\u062c\u0627\u0645 \u0634\u062f\n  - \u0632\u06cc\u0631\u0645\u0648\u0631\u062f"
            "\n\n> \u062e\u0637 \u0627\u0648\u0644\n> \u062e\u0637 \u062f\u0648\u0645\n\n---"
        )
        view = MarkdownView(source)
        labels = [widget for widget in view.get_children() if isinstance(widget, Gtk.Label)]
        self.assertGreaterEqual(len(labels), 4)
        self.assertTrue(any("☑" in label.get_text() for label in labels))
        self.assertTrue(any("\u062e\u0637 \u0627\u0648\u0644\n\u062e\u0637 \u062f\u0648\u0645" == label.get_text() for label in labels))
        self.assertTrue(any(isinstance(widget, Gtk.Separator) for widget in view.get_children()))

    def test_heading_preserves_programming_language_hash(self) -> None:
        view = MarkdownView("# \u0622\u0645\u0648\u0632\u0634 C#\n\n## \u0628\u062e\u0634 \u062f\u0648\u0645 ##")
        labels = view.get_children()
        heading = labels[0].get_text().replace("\u2066", "").replace("\u2069", "")
        self.assertEqual(heading, "\u0622\u0645\u0648\u0632\u0634 C#")
        self.assertEqual(labels[1].get_text(), "\u0628\u062e\u0634 \u062f\u0648\u0645")


class WidgetRobustnessTests(unittest.TestCase):
    def test_drag_selection_edge_scroll_is_directional_and_has_dead_zone(self) -> None:
        self.assertLess(MarkdownView._edge_scroll_delta(8, 0, 500), 0)
        self.assertEqual(MarkdownView._edge_scroll_delta(250, 0, 500), 0)
        self.assertGreater(MarkdownView._edge_scroll_delta(492, 0, 500), 0)

    def test_native_selection_widgets_are_mounted_in_the_transcript_scroller(self) -> None:
        window = Gtk.Window()
        window.set_default_size(620, 300)
        scroller = Gtk.ScrolledWindow()
        viewport = Gtk.Viewport()
        view = MarkdownView(
            "\n\n".join(
                f"Selectable paragraph {index} with enough text."
                for index in range(70)
            )
        )
        viewport.add(view)
        scroller.add(viewport)
        window.add(scroller)
        window.show_all()
        while Gtk.events_pending():
            Gtk.main_iteration_do(False)
        try:
            self.assertIs(view._outer_vertical_scroller(), scroller)
            first = view._selection_widgets[0]
            self.assertIsInstance(first, Gtk.Label)
            self.assertTrue(first.get_can_focus())
        finally:
            window.destroy()

    def _code_widgets(self, text: str):
        view = MarkdownView(text)
        outer = view.get_children()[0]
        header, code_view = outer.get_children()
        return view, outer, header, code_view

    def test_code_buffer_contains_pristine_source_without_direction_marks(self) -> None:
        code = "\u0633\u0644\u0627\u0645 = value\nprint(\u0633\u0644\u0627\u0645)"
        _view, _outer, _header, code_view = self._code_widgets(
            f"```python\n{code}\n```"
        )
        buffer = code_view.get_buffer()
        rendered = buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), True)
        self.assertEqual(rendered, code)
        self.assertNotIn("\u200e", rendered)

        _view, _outer, _header, empty_view = self._code_widgets("```\n```")
        empty_buffer = empty_view.get_buffer()
        self.assertEqual(
            empty_buffer.get_text(
                empty_buffer.get_start_iter(), empty_buffer.get_end_iter(), True
            ),
            "",
        )

    def test_long_code_wraps_and_has_no_nested_scroll_region(self) -> None:
        code = "\n".join(f"line_{number} = '{'x' * 300}'" for number in range(200))
        document, outer, _header, code_view = self._code_widgets(
            f"```python\n{code}\n```"
        )
        self.assertEqual(len(outer.get_children()), 2)
        self.assertNotIsInstance(code_view, Gtk.ScrolledWindow)
        self.assertEqual(code_view.get_wrap_mode(), Gtk.WrapMode.WORD_CHAR)
        window = Gtk.Window()
        transcript = Gtk.ScrolledWindow()
        viewport = Gtk.Viewport()
        viewport.add(document)
        transcript.add(viewport)
        window.add(transcript)
        window.set_default_size(620, 300)
        window.show_all()
        while Gtk.events_pending():
            Gtk.main_iteration_do(False)
        try:
            self.assertGreater(code_view.get_allocation().height, 400)
        finally:
            window.destroy()

    def test_short_code_height_tracks_visible_line_count(self) -> None:
        one_document, _outer, _header, one_line = self._code_widgets(
            "```python\nx = 1\n```"
        )
        six_document, _outer, _header, six_lines = self._code_widgets(
            "```python\n1\n2\n3\n4\n5\n6\n```"
        )
        window = Gtk.Window()
        documents = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        documents.pack_start(one_document, False, False, 0)
        documents.pack_start(six_document, False, False, 0)
        transcript = Gtk.ScrolledWindow()
        viewport = Gtk.Viewport()
        viewport.add(documents)
        transcript.add(viewport)
        window.add(transcript)
        window.set_default_size(620, 300)
        window.show_all()
        while Gtk.events_pending():
            Gtk.main_iteration_do(False)
        try:
            self.assertGreater(
                six_lines.get_allocation().height,
                one_line.get_allocation().height,
            )
        finally:
            window.destroy()

    def test_javascript_alias_uses_available_syntax_language(self) -> None:
        if markdown_view.GtkSource is None:
            self.skipTest("GtkSourceView is not installed")
        _view, _outer, _header, code_view = self._code_widgets(
            "```javascript\nconst x = 1;\n```"
        )
        language = code_view.get_buffer().get_language()
        self.assertIsNotNone(language)
        self.assertEqual(language.get_id(), "js")

    def test_code_color_scheme_tracks_light_and_dark_window_themes(self) -> None:
        if markdown_view.GtkSource is None:
            self.skipTest("GtkSourceView is not installed")
        expected = {
            "light": {"Yaru", "solarized-light", "classic"},
            "night": {"Yaru-dark", "solarized-dark", "oblivion"},
            "graphite": {"Yaru-dark", "solarized-dark", "oblivion"},
        }
        for theme, accepted_schemes in expected.items():
            with self.subTest(theme=theme):
                document, _outer, _header, code_view = self._code_widgets(
                    "```python\nvalue = 1\n```"
                )
                window = Gtk.Window()
                window.get_style_context().add_class(f"theme-{theme}")
                window.add(document)
                window.show_all()
                while Gtk.events_pending():
                    Gtk.main_iteration_do(False)
                try:
                    scheme = code_view.get_buffer().get_style_scheme()
                    self.assertIsNotNone(scheme)
                    self.assertIn(scheme.get_id(), accepted_schemes)
                    document.set_code_theme(
                        "light" if theme != "light" else "night"
                    )
                    switched = code_view.get_buffer().get_style_scheme()
                    self.assertIsNotNone(switched)
                    self.assertIn(
                        switched.get_id(),
                        expected["light" if theme != "light" else "night"],
                    )
                finally:
                    window.destroy()

    def test_long_table_wraps_in_document_without_nested_scroller(self) -> None:
        header = "| " + " | ".join(f"column-{n}" for n in range(12)) + " |"
        separator = "| " + " | ".join("---" for _ in range(12)) + " |"
        row = "| " + " | ".join("value" for _ in range(12)) + " |"
        view = MarkdownView("\n".join((header, separator, *([row] * 100))))
        grid = view.get_children()[0]
        self.assertIsInstance(grid, Gtk.Grid)
        self.assertTrue(grid.get_column_homogeneous())
        self.assertEqual(len(grid.get_children()), 12 * 101)
        self.assertTrue(
            all(child.get_line_wrap() for child in grid.get_children())
        )

    def test_streaming_reuses_stable_completed_paragraph_widgets(self) -> None:
        view = MarkdownView("\u067e\u0627\u0631\u0627\u06af\u0631\u0627\u0641 \u067e\u0627\u06cc\u062f\u0627\u0631\n\n\u0642\u0633\u0645\u062a")
        first, old_tail = view.get_children()
        view.set_text("\u067e\u0627\u0631\u0627\u06af\u0631\u0627\u0641 \u067e\u0627\u06cc\u062f\u0627\u0631\n\n\u0642\u0633\u0645\u062a \u062f\u0631 \u062d\u0627\u0644 \u062a\u06a9\u0645\u06cc\u0644")
        self.assertIs(view.get_children()[0], first)
        self.assertIsNot(view.get_children()[1], old_tail)

        completed_tail = view.get_children()[1]
        view.set_text("\u067e\u0627\u0631\u0627\u06af\u0631\u0627\u0641 \u067e\u0627\u06cc\u062f\u0627\u0631\n\n\u0642\u0633\u0645\u062a \u062f\u0631 \u062d\u0627\u0644 \u062a\u06a9\u0645\u06cc\u0644\n\n\u0622\u062e\u0631")
        self.assertIs(view.get_children()[0], first)
        self.assertIsNot(view.get_children()[1], completed_tail)
        promoted = view.get_children()[1]
        view.set_text(
            "\u067e\u0627\u0631\u0627\u06af\u0631\u0627\u0641 \u067e\u0627\u06cc\u062f\u0627\u0631\n\n\u0642\u0633\u0645\u062a \u062f\u0631 \u062d\u0627\u0644 \u062a\u06a9\u0645\u06cc\u0644\n\n\u0622\u062e\u0631\u06cc\u0646 \u0645\u062a\u0646"
        )
        self.assertIs(view.get_children()[0], first)
        self.assertIs(view.get_children()[1], promoted)


class SelectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.window = Gtk.Window()
        self.window.set_default_size(700, 360)

    def tearDown(self) -> None:
        self.window.destroy()
        while Gtk.events_pending():
            Gtk.main_iteration_do(False)

    @staticmethod
    def _drain_events() -> None:
        while Gtk.events_pending():
            Gtk.main_iteration_do(False)

    @staticmethod
    def _control_press(widget: Gtk.Widget) -> Gdk.Event:
        event = Gdk.Event.new(Gdk.EventType.BUTTON_PRESS)
        button = event.button
        button.button = Gdk.BUTTON_PRIMARY
        button.state = Gdk.ModifierType.CONTROL_MASK
        button.window = Gtk.Widget.get_window(widget)
        button.x = 2
        button.y = 2
        return event

    @staticmethod
    def _control_key(widget: Gtk.Widget, keyval: int) -> Gdk.Event:
        event = Gdk.Event.new(Gdk.EventType.KEY_PRESS)
        key = event.key
        key.state = Gdk.ModifierType.CONTROL_MASK
        key.keyval = keyval
        key.window = Gtk.Widget.get_window(widget)
        return event

    @staticmethod
    def _label_local_point(label: Gtk.Label, offset: int):
        rendered = label.get_text()
        byte_offset = len(rendered[:offset].encode("utf-8"))
        position = label.get_layout().index_to_pos(byte_offset)
        layout_x, layout_y = MarkdownView._label_layout_origin(label)
        return (
            layout_x + (position.x / Pango.SCALE),
            layout_y + ((position.y + position.height / 2) / Pango.SCALE),
        )

    def test_real_label_selection_and_clear_selection_api(self) -> None:
        view = MarkdownView("First paragraph\n\nSecond paragraph")
        self.window.add(view)
        self.window.show_all()
        first = view.get_children()[0]
        changes = []
        handler_id = view.connect_selection_changed(
            lambda _view, selected: changes.append(selected)
        )

        first.select_region(0, 5)
        self._drain_events()
        self.assertEqual(view.get_selected_text(), "First")
        self.assertEqual(changes[-1], "First")

        view.clear_selection()
        self._drain_events()
        self.assertEqual(view.get_selected_text(), "")
        self.assertEqual(changes[-1], "")
        view.disconnect(handler_id)

    def test_new_native_prose_selection_replaces_previous_block(self) -> None:
        view = MarkdownView("First paragraph\n\nSecond paragraph")
        self.window.add(view)
        self.window.show_all()
        first, second = view.get_children()
        first.select_region(0, 5)
        self._drain_events()
        self.assertEqual(view.get_selected_text(), "First")

        self.assertFalse(second.emit("button-press-event", self._control_press(second)))
        self.assertIsNone(first.get_attributes())
        second.select_region(0, 6)
        self._drain_events()
        self.assertFalse(first.get_selection_bounds()[0])
        self.assertIsNone(first.get_attributes())
        self.assertEqual(view.get_selected_text(), "Second")

        view.clear_selection()
        self._drain_events()
        self.assertIsNone(first.get_attributes())

    def test_native_selection_exposes_only_the_active_prose_block(self) -> None:
        view = MarkdownView("Alpha first\n\nMiddle whole\n\nOmega last")
        self.window.add(view)
        self.window.show_all()
        first, middle, last = view.get_children()

        first.select_region(6, 11)
        self._drain_events()
        self.assertEqual(view.get_selected_text(), "first")

        last.select_region(0, 5)
        self._drain_events()
        self.assertFalse(first.get_selection_bounds()[0])
        self.assertFalse(middle.get_selection_bounds()[0])
        self.assertEqual(view.get_selected_text(), "Omega")

    def test_ordinary_prose_pointer_events_defer_to_native_selection(self) -> None:
        view = MarkdownView("Alpha first\n\nBeta second")
        self.window.add(view)
        self.window.show_all()
        self._drain_events()
        first, second = view.get_children()
        self.assertEqual(
            first.get_events() & Gdk.EventMask.BUTTON_PRESS_MASK,
            Gdk.EventMask.BUTTON_PRESS_MASK,
        )

        press_x, press_y = self._label_local_point(first, 0)
        first_rect = view._widget_root_rect(first)
        self.assertIsNotNone(first_rect)
        press = Gdk.Event.new(Gdk.EventType.BUTTON_PRESS)
        press.button.button = Gdk.BUTTON_PRIMARY
        press.button.state = Gdk.ModifierType(0)
        press.button.window = first.get_window()
        press.button.x = press_x
        press.button.y = press_y
        press.button.x_root = first_rect[0] + press_x
        press.button.y_root = first_rect[1] + press_y
        self.assertFalse(first.emit("button-press-event", press))
        self.assertIsNone(view._drag_anchor)

        first.select_region(0, 11)
        self._drain_events()
        self.assertEqual(view.get_selected_text(), "Alpha first")

        same_x, same_y = self._label_local_point(first, 5)
        within = Gdk.Event.new(Gdk.EventType.MOTION_NOTIFY)
        within.motion.state = Gdk.ModifierType.BUTTON1_MASK
        within.motion.window = first.get_window()
        within.motion.x = same_x
        within.motion.y = same_y
        within.motion.x_root = first_rect[0] + same_x
        within.motion.y_root = first_rect[1] + same_y
        self.assertFalse(first.emit("motion-notify-event", within))
        self._drain_events()
        self.assertFalse(view._document_selection_active)
        self.assertEqual(view.get_selected_text(), "Alpha first")

        second_rect = view._widget_root_rect(second)
        self.assertIsNotNone(second_rect)
        target_x, target_y = self._label_local_point(second, 4)
        across = Gdk.Event.new(Gdk.EventType.MOTION_NOTIFY)
        across.motion.state = Gdk.ModifierType.BUTTON1_MASK
        across.motion.window = first.get_window()
        across.motion.x = target_x
        across.motion.y = target_y
        across.motion.x_root = second_rect[0] + target_x
        across.motion.y_root = second_rect[1] + target_y
        self.assertFalse(first.emit("motion-notify-event", across))
        self._drain_events()
        self.assertFalse(view._document_selection_active)
        self.assertEqual(view.get_selected_text(), "Alpha first")

        release = Gdk.Event.new(Gdk.EventType.BUTTON_RELEASE)
        release.button.button = Gdk.BUTTON_PRIMARY
        release.button.state = Gdk.ModifierType(0)
        release.button.window = first.get_window()
        release.button.x = target_x
        release.button.y = target_y
        release.button.x_root = across.motion.x_root
        release.button.y_root = across.motion.y_root
        self.assertFalse(first.emit("button-release-event", release))
        self.assertIsNone(view._drag_anchor)
        self.assertEqual(view.get_selected_text(), "Alpha first")

    def test_bidi_markers_are_not_returned_to_reply_callers(self) -> None:
        view = MarkdownView("\u0645\u062a\u0646 Spark \u0627\u06cc\u0646\u062c\u0627\u0633\u062a")
        label = view.get_children()[0]
        label.select_region(0, -1)
        selected = view.get_selected_text()
        self.assertEqual(selected, "\u0645\u062a\u0646 Spark \u0627\u06cc\u0646\u062c\u0627\u0633\u062a")
        self.assertNotRegex(selected, markdown_view.BIDI_CONTROL_RE)

    def test_code_selection_is_exact_and_code_view_stays_ltr(self) -> None:
        source = "Before\n\n```python\nalpha = 1\nbeta = 2\n```\n\nAfter"
        view = MarkdownView(source)
        self.window.add(view)
        self.window.show_all()
        code_view = next(
            widget
            for widget in view._selection_widgets
            if isinstance(widget, Gtk.TextView)
        )
        text_buffer = code_view.get_buffer()
        text_buffer.select_range(
            text_buffer.get_iter_at_offset(0), text_buffer.get_iter_at_offset(9)
        )
        self._drain_events()
        self.assertEqual(view.get_selected_text(), "alpha = 1")
        self.assertEqual(code_view.get_direction(), Gtk.TextDirection.LTR)

    def test_late_old_primary_clear_keeps_new_text_selection(self) -> None:
        # An unrealized selectable label is intentionally left in a reference
        # cycle.  GTK may finalize it only after the newer TextBuffer has taken
        # PRIMARY ownership, historically clearing that fresh range.
        old_view = MarkdownView("\u0645\u062a\u0646 Spark \u0627\u06cc\u0646\u062c\u0627\u0633\u062a")
        old_label = old_view.get_children()[0]
        old_label.select_region(0, -1)
        self.assertTrue(old_view.get_selected_text())
        del old_label, old_view

        view = MarkdownView("```text\nalpha = 1\nbeta = 2\n```")
        self.window.add(view)
        self.window.show_all()
        code_view = next(
            widget
            for widget in view._selection_widgets
            if isinstance(widget, Gtk.TextView)
        )
        text_buffer = code_view.get_buffer()
        text_buffer.select_range(
            text_buffer.get_start_iter(), text_buffer.get_iter_at_offset(9)
        )
        gc.collect()
        self._drain_events()

        self.assertEqual(view.get_selected_text(), "alpha = 1")

    def test_native_code_selection_replaces_prose_selection(self) -> None:
        view = MarkdownView("Before\n\n```text\ncode line\n```\n\nAfter")
        self.window.add(view)
        self.window.show_all()
        prose = view.get_children()[0]
        code_view = next(
            widget
            for widget in view._selection_widgets
            if isinstance(widget, Gtk.TextView)
        )
        prose.select_region(0, 6)
        self._drain_events()
        self.assertEqual(view.get_selected_text(), "Before")
        self.assertFalse(code_view.emit("button-press-event", self._control_press(code_view)))
        text_buffer = code_view.get_buffer()
        text_buffer.select_range(
            text_buffer.get_start_iter(), text_buffer.get_end_iter()
        )
        self._drain_events()
        self.assertEqual(view.get_selected_text(), "code line")

    def test_streaming_replacement_drops_detached_widget_selection(self) -> None:
        view = MarkdownView("Stable\n\nold tail")
        old_tail = view.get_children()[1]
        old_tail.select_region(0, -1)
        self.assertEqual(view.get_selected_text(), "old tail")
        old_widget_count = len(view._selection_widgets)

        view.set_text("Stable\n\nnew tail")
        self._drain_events()
        self.assertEqual(view.get_selected_text(), "")
        self.assertNotIn(old_tail, view._selection_widgets)
        self.assertEqual(len(view._selection_widgets), old_widget_count)

    def test_control_a_is_not_intercepted_for_cross_block_selection(self) -> None:
        view = MarkdownView(
            "First paragraph\n\n- second item\n\n```text\nsecret code\n```\n\nLast"
        )
        self.window.add(view)
        self.window.show_all()
        prose = view.get_children()[0]
        handled = view._on_selectable_key_press(
            prose, self._control_key(prose, Gdk.KEY_a)
        )
        self._drain_events()

        self.assertFalse(handled)
        self.assertFalse(view._document_selection_active)
        self.assertIsNone(prose.get_attributes())


if __name__ == "__main__":
    unittest.main()
