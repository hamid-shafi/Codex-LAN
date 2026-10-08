"""GTK layout checks for image attachments in transcript messages."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import GdkPixbuf, GLib, Gtk


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from project_layout import configure_imports, source_file
configure_imports(__file__)

from app import MessageBubble


ASSET_IMAGE = ROOT / "apps/desktop/assets" / "codex.svg"


def drain_events() -> None:
    context = GLib.MainContext.default()
    while context.pending():
        context.iteration(False)


class MessageGalleryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        ready, _args = Gtk.init_check([])
        if not ready:
            raise unittest.SkipTest("GTK display is unavailable")

    def _show(self, bubble: MessageBubble, width: int = 780) -> Gtk.Window:
        window = Gtk.Window()
        window.set_default_size(width, 720)
        window.add(bubble)
        window.show_all()
        drain_events()
        return window

    @staticmethod
    def _gallery(bubble: MessageBubble) -> Gtk.Grid:
        return next(
            child
            for child in bubble.bubble.get_children()
            if isinstance(child, Gtk.Grid)
            and child.get_style_context().has_class("message-image-gallery")
        )

    def test_four_images_form_a_two_column_grid_without_overflow(self) -> None:
        bubble = MessageBubble(
            "user",
            "The message text follows the image grid.",
            image_paths=[str(ASSET_IMAGE)] * 4,
        )
        window = self._show(bubble)
        try:
            gallery = self._gallery(bubble)
            gallery_rect = gallery.get_allocation()
            self.assertEqual(len(gallery.get_children()), 4)
            self.assertEqual(gallery_rect.width, 399)
            positions = sorted(
                (
                    gallery.child_get_property(tile, "left-attach"),
                    gallery.child_get_property(tile, "top-attach"),
                )
                for tile in gallery.get_children()
            )
            self.assertEqual(positions, [(0, 0), (0, 1), (1, 0), (1, 1)])
            for tile in gallery.get_children():
                tile_rect = tile.get_allocation()
                self.assertGreaterEqual(tile_rect.x, gallery_rect.x)
                self.assertLessEqual(
                    tile_rect.x + tile_rect.width,
                    gallery_rect.x + gallery_rect.width,
                )
            content_rect = bubble.content_view.get_allocation()
            self.assertGreaterEqual(
                content_rect.y, gallery_rect.y + gallery_rect.height
            )
        finally:
            window.destroy()
            drain_events()

    def test_three_images_keep_a_compact_second_row(self) -> None:
        bubble = MessageBubble(
            "user", "Three image message.", image_paths=[str(ASSET_IMAGE)] * 3
        )
        window = self._show(bubble)
        try:
            gallery = self._gallery(bubble)
            gallery_rect = gallery.get_allocation()
            self.assertEqual(gallery_rect.width, 399)
            self.assertLessEqual(gallery_rect.height, 280)
            positions = sorted(
                (
                    gallery.child_get_property(tile, "left-attach"),
                    gallery.child_get_property(tile, "top-attach"),
                )
                for tile in gallery.get_children()
            )
            self.assertEqual(positions, [(0, 0), (0, 1), (1, 0)])
        finally:
            window.destroy()
            drain_events()

    def test_single_image_keeps_the_larger_readable_preview(self) -> None:
        bubble = MessageBubble(
            "assistant", "Caption after the image.", image_path=str(ASSET_IMAGE)
        )
        window = self._show(bubble)
        try:
            gallery = self._gallery(bubble)
            tile = gallery.get_children()[0]
            tile_rect = tile.get_allocation()
            self.assertGreaterEqual(tile_rect.width, 420)
            self.assertGreaterEqual(tile_rect.height, 260)
            self.assertEqual(
                (
                    gallery.child_get_property(tile, "left-attach"),
                    gallery.child_get_property(tile, "top-attach"),
                ),
                (0, 0),
            )
        finally:
            window.destroy()
            drain_events()

    def test_image_tile_opens_the_shared_non_blocking_preview(self) -> None:
        bubble = MessageBubble(
            "user", "Open the image.", image_path=str(ASSET_IMAGE)
        )
        window = self._show(bubble)
        try:
            tile = self._gallery(bubble).get_children()[0]
            with patch("app.show_attachment_preview") as preview:
                tile.emit("clicked")
            preview.assert_called_once_with(window, str(ASSET_IMAGE), "codex.svg")
        finally:
            window.destroy()
            drain_events()

    def test_valid_attachment_is_decoded_off_the_gtk_layout_path(self) -> None:
        with patch("app.load_attachment_image_async") as load_async:
            bubble = MessageBubble(
                "assistant", "Load later.", image_path=str(ASSET_IMAGE)
            )
        load_async.assert_called_once()
        arguments = load_async.call_args.args
        self.assertEqual(arguments[:3], (str(ASSET_IMAGE), 412, 252))
        tile = self._gallery(bubble).get_children()[0]
        self.assertIsInstance(tile.get_child(), Gtk.Spinner)

    def test_stale_async_tile_callback_cannot_update_a_removed_message(self) -> None:
        with patch("app.load_attachment_image_async") as load_async:
            bubble = MessageBubble(
                "assistant", "Load later.", image_path=str(ASSET_IMAGE)
            )
        window = self._show(bubble)
        try:
            tile = self._gallery(bubble).get_children()[0]
            loading = tile.get_child()
            on_loaded = load_async.call_args.args[3]
            window.remove(bubble)
            on_loaded(
                None,
                GdkPixbuf.Pixbuf.new_from_file_at_scale(
                    str(ASSET_IMAGE), 20, 20, True
                ),
            )
            self.assertIs(tile.get_child(), loading)
        finally:
            window.destroy()
            drain_events()

    def test_short_user_bubble_hugs_its_content(self) -> None:
        bubble = MessageBubble("user", "Short message")
        window = self._show(bubble)
        try:
            bubble_rect = bubble.bubble.get_allocation()
            window_rect = window.get_allocation()
            self.assertLess(bubble_rect.height, 200)
            self.assertLess(bubble_rect.height, window_rect.height)
        finally:
            window.destroy()
            drain_events()

    def test_missing_attachment_remains_visible_and_described(self) -> None:
        bubble = MessageBubble(
            "user",
            "The attachment is unavailable.",
            image_path="/tmp/codex-gallery-missing-image.png",
        )
        window = self._show(bubble)
        try:
            tile = self._gallery(bubble).get_children()[0]
            self.assertTrue(
                tile.get_style_context().has_class("message-image-tile-missing")
            )
            self.assertEqual(
                tile.get_tooltip_text(),
                "Image unavailable: codex-gallery-missing-image.png",
            )
            self.assertEqual(
                tile.get_accessible().get_name(), tile.get_tooltip_text()
            )
            self.assertFalse(tile.get_sensitive())
        finally:
            window.destroy()
            drain_events()


if __name__ == "__main__":
    unittest.main()
