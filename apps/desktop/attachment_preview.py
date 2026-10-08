"""Safe, bounded image inspection and rendering helpers for attachments."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import threading
from typing import Callable, Optional

import gi

gi.require_version("Gdk", "3.0")
gi.require_version("GdkPixbuf", "2.0")
gi.require_version("Gtk", "3.0")
from gi.repository import GdkPixbuf, GLib, Gtk  # noqa: E402


MAX_IMAGE_DIMENSION = 20_000
MAX_IMAGE_PIXELS = 80_000_000


class AttachmentImageError(ValueError):
    """An attachment cannot be safely inspected or decoded as an image."""


@dataclass(frozen=True)
class AttachmentImageInfo:
    path: str
    format_name: str
    width: int
    height: int

    @property
    def pixels(self) -> int:
        return self.width * self.height


def inspect_attachment_image(
    image_path: str,
    *,
    max_dimension: int = MAX_IMAGE_DIMENSION,
    max_pixels: int = MAX_IMAGE_PIXELS,
) -> AttachmentImageInfo:
    """Read image metadata without decoding its full pixel payload."""
    candidate = Path(image_path)
    if not candidate.is_file():
        raise AttachmentImageError("The image file no longer exists.")
    try:
        result = GdkPixbuf.Pixbuf.get_file_info(str(candidate))
    except (GLib.Error, OSError) as exc:
        raise AttachmentImageError("This file is not a readable image.") from exc
    if not result or result[0] is None:
        raise AttachmentImageError("This file is not a readable image.")
    image_format, width, height = result
    width = int(width)
    height = int(height)
    if width <= 0 or height <= 0:
        raise AttachmentImageError("This image has invalid dimensions.")
    if width > max_dimension or height > max_dimension:
        raise AttachmentImageError(
            f"Image dimensions exceed the {max_dimension:,} px safety limit."
        )
    if width * height > max_pixels:
        raise AttachmentImageError(
            f"Image size exceeds the {max_pixels:,}-pixel safety limit."
        )
    format_name = str(image_format.get_name() or "image")
    return AttachmentImageInfo(str(candidate), format_name, width, height)


def fit_dimensions(
    width: int, height: int, maximum_width: int, maximum_height: int
) -> tuple[int, int]:
    """Return a non-upscaled size that fits within the requested bounds."""
    if width <= 0 or height <= 0:
        raise AttachmentImageError("This image has invalid dimensions.")
    if maximum_width <= 0 or maximum_height <= 0:
        raise ValueError("Preview bounds must be positive.")
    scale = min(1.0, maximum_width / width, maximum_height / height)
    return max(1, round(width * scale)), max(1, round(height * scale))


def load_attachment_image(
    image_path: str,
    maximum_width: int,
    maximum_height: int,
) -> tuple[AttachmentImageInfo, GdkPixbuf.Pixbuf]:
    """Decode one bounded attachment after validating its metadata."""
    info = inspect_attachment_image(image_path)
    width, height = fit_dimensions(
        info.width, info.height, maximum_width, maximum_height
    )
    try:
        pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(
            info.path, width, height, True
        )
    except (GLib.Error, OSError) as exc:
        raise AttachmentImageError("This image could not be decoded.") from exc
    return info, pixbuf


def load_attachment_image_async(
    image_path: str,
    maximum_width: int,
    maximum_height: int,
    on_loaded: Callable[[AttachmentImageInfo, GdkPixbuf.Pixbuf], None],
    on_error: Callable[[str], None],
) -> threading.Thread:
    """Decode an image away from the GTK loop and deliver it on that loop."""
    def worker() -> None:
        try:
            info, pixbuf = load_attachment_image(
                image_path, maximum_width, maximum_height
            )
        except (AttachmentImageError, GLib.Error, OSError) as exc:
            message = str(exc) or "This image could not be decoded."

            def deliver_error() -> bool:
                on_error(message)
                return False

            GLib.idle_add(deliver_error)
            return

        def deliver_image() -> bool:
            on_loaded(info, pixbuf)
            return False

        GLib.idle_add(deliver_image)

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    return thread


def show_attachment_preview(
    parent: Gtk.Window,
    image_path: str,
    label: str,
) -> Gtk.Dialog:
    """Open a non-blocking preview dialog while its bounded image loads."""
    info = inspect_attachment_image(image_path)
    dialog = Gtk.Dialog(
        title=label or "Image preview",
        transient_for=parent,
        modal=True,
        destroy_with_parent=True,
    )
    dialog.add_button("Close", Gtk.ResponseType.CLOSE)
    dialog.set_resizable(True)
    dialog.set_default_size(920, 680)
    area = dialog.get_content_area()
    area.set_spacing(10)
    area.set_border_width(14)

    stack = Gtk.Stack()
    stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
    stack.set_hexpand(True)
    stack.set_vexpand(True)
    loading = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
    loading.set_halign(Gtk.Align.CENTER)
    loading.set_valign(Gtk.Align.CENTER)
    spinner = Gtk.Spinner()
    spinner.start()
    loading.pack_start(spinner, False, False, 0)
    loading_label = Gtk.Label(label="Loading image preview…")
    loading.get_style_context().add_class("attachment-preview-loading")
    loading.pack_start(loading_label, False, False, 0)
    stack.add_named(loading, "loading")

    scroll = Gtk.ScrolledWindow()
    scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
    scroll.set_overlay_scrolling(True)
    scroll.set_hexpand(True)
    scroll.set_vexpand(True)
    stack.add_named(scroll, "image")
    area.pack_start(stack, True, True, 0)

    description = Gtk.Label(
        label=f"{info.width:,} × {info.height:,} · {label or Path(info.path).name}"
    )
    description.set_xalign(0.5)
    description.get_style_context().add_class("attachment-preview-description")
    area.pack_start(description, False, False, 0)

    closed = {"value": False}

    def on_response(preview_dialog: Gtk.Dialog, _response: int) -> None:
        closed["value"] = True
        preview_dialog.destroy()

    def on_destroy(_preview_dialog: Gtk.Dialog) -> None:
        closed["value"] = True

    dialog.connect("response", on_response)
    dialog.connect("destroy", on_destroy)

    def set_image(_loaded_info: AttachmentImageInfo, pixbuf: GdkPixbuf.Pixbuf) -> None:
        if closed["value"]:
            return
        preview = Gtk.Image.new_from_pixbuf(pixbuf)
        preview.set_halign(Gtk.Align.CENTER)
        preview.set_valign(Gtk.Align.CENTER)
        preview.get_style_context().add_class("attachment-preview-image")
        scroll.add_with_viewport(preview)
        scroll.show_all()
        stack.set_visible_child_name("image")

    def set_error(message: str) -> None:
        if closed["value"]:
            return
        spinner.stop()
        loading_label.set_text(f"Could not load image preview: {message}")

    load_attachment_image_async(image_path, 1800, 1280, set_image, set_error)
    dialog.show_all()
    return dialog
