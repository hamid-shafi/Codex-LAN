"""Regression coverage for bounded and asynchronous attachment rendering."""

from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path

import gi

gi.require_version("GdkPixbuf", "2.0")
from gi.repository import GdkPixbuf, GLib  # noqa: E402

from attachment_preview import (
    AttachmentImageError,
    fit_dimensions,
    inspect_attachment_image,
    load_attachment_image,
    load_attachment_image_async,
)


class AttachmentPreviewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="codex-attachment-test-")
        self.image_path = str(Path(self.temporary.name) / "wide.png")
        pixbuf = GdkPixbuf.Pixbuf.new(
            GdkPixbuf.Colorspace.RGB, True, 8, 1600, 800
        )
        pixbuf.fill(0x3979BDFF)
        pixbuf.savev(self.image_path, "png", [], [])

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_metadata_validation_avoids_full_decode_and_preserves_dimensions(self) -> None:
        info = inspect_attachment_image(self.image_path)

        self.assertEqual(info.format_name, "png")
        self.assertEqual((info.width, info.height, info.pixels), (1600, 800, 1_280_000))

    def test_fit_dimensions_never_upscales_and_preserves_aspect_ratio(self) -> None:
        self.assertEqual(fit_dimensions(1600, 800, 136, 92), (136, 68))
        self.assertEqual(fit_dimensions(80, 40, 136, 92), (80, 40))

    def test_decode_returns_a_bounded_thumbnail(self) -> None:
        info, pixbuf = load_attachment_image(self.image_path, 136, 92)

        self.assertEqual((info.width, info.height), (1600, 800))
        self.assertEqual((pixbuf.get_width(), pixbuf.get_height()), (136, 68))

    def test_corrupt_or_oversized_attachment_is_rejected_before_rendering(self) -> None:
        corrupt_path = Path(self.temporary.name) / "not-an-image.png"
        corrupt_path.write_text("not an image", encoding="utf-8")

        with self.assertRaises(AttachmentImageError):
            inspect_attachment_image(str(corrupt_path))
        with self.assertRaises(AttachmentImageError):
            inspect_attachment_image(self.image_path, max_pixels=1_000_000)

    def test_async_decode_delivers_only_a_small_pixbuf_on_the_main_context(self) -> None:
        complete = threading.Event()
        result = []
        failures = []

        def loaded(info, pixbuf) -> None:
            result.append((info, pixbuf))
            complete.set()

        def failed(message: str) -> None:
            failures.append(message)
            complete.set()

        load_attachment_image_async(self.image_path, 136, 92, loaded, failed)
        context = GLib.MainContext.default()
        deadline = time.monotonic() + 3.0
        while not complete.is_set() and time.monotonic() < deadline:
            while context.pending():
                context.iteration(False)
            time.sleep(0.01)

        self.assertTrue(complete.is_set())
        self.assertEqual(failures, [])
        self.assertEqual(len(result), 1)
        self.assertEqual(
            (result[0][1].get_width(), result[0][1].get_height()), (136, 68)
        )


if __name__ == "__main__":
    unittest.main()
