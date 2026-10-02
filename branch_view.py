"""Compact nested side-thread UI used below a parent chat message."""

from __future__ import annotations

from typing import Any, Dict, Iterable, Optional

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gdk, Gtk, Pango  # noqa: E402

from markdown_view import MarkdownView, content_prefers_rtl


class BranchPanel(Gtk.Box):
    """Render a focused conversation without adding it to the room sidebar."""

    def __init__(
        self,
        branch_id: int,
        quote: str,
        messages: Iterable[Dict[str, Any]],
        on_send: Any,
        on_close: Optional[Any] = None,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=9)
        self.branch_id = int(branch_id)
        self.on_send = on_send
        self.answer_view: Optional[MarkdownView] = None
        self.connect("key-press-event", self.on_key_press)
        self.get_style_context().add_class("branch-panel")
        self.set_direction(Gtk.TextDirection.LTR)

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        title = Gtk.Label(label="Side thread")
        title.set_xalign(0.0)
        title.get_style_context().add_class("branch-title")
        subtitle = Gtk.Label(label="Private to this answer")
        subtitle.set_xalign(0.0)
        subtitle.get_style_context().add_class("branch-subtitle")
        heading = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        heading.pack_start(title, False, False, 0)
        heading.pack_start(subtitle, False, False, 0)
        header.pack_start(heading, True, True, 0)
        close = Gtk.Button.new_from_icon_name("window-close-symbolic", Gtk.IconSize.MENU)
        close.set_relief(Gtk.ReliefStyle.NONE)
        close.set_tooltip_text("Collapse side thread")
        close.get_accessible().set_name("Collapse side thread")
        close.get_style_context().add_class("branch-close")
        if on_close:
            close.connect("clicked", lambda _button: on_close(self.branch_id, self))
        else:
            close.connect("clicked", lambda _button: self.hide())
        header.pack_end(close, False, False, 0)
        self.pack_start(header, False, False, 0)

        quote_label = Gtk.Label(label=quote.strip()[:1200] or "Selected passage")
        quote_label.set_selectable(True)
        quote_label.set_line_wrap(True)
        quote_label.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
        quote_label.set_max_width_chars(90)
        quote_rtl = content_prefers_rtl(quote)
        quote_label.set_xalign(1.0 if quote_rtl else 0.0)
        quote_label.set_direction(
            Gtk.TextDirection.RTL if quote_rtl else Gtk.TextDirection.LTR
        )
        quote_label.get_style_context().add_class("branch-quote")
        self.pack_start(quote_label, False, False, 0)

        self.messages = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.messages.get_style_context().add_class("branch-messages")
        self.pack_start(self.messages, False, False, 0)
        for message in messages:
            self.add_message(str(message.get("role") or "assistant"), str(message.get("content") or ""))

        compose = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=7)
        self.entry = Gtk.Entry()
        self.entry.set_placeholder_text("Ask about the selected passage…")
        self.entry.set_hexpand(True)
        self.entry.set_activates_default(False)
        self.entry.connect("activate", lambda _entry: self.submit())
        self.entry.connect("changed", self._update_entry_direction)
        self.entry.get_style_context().add_class("branch-entry")
        compose.pack_start(self.entry, True, True, 0)
        self.send_button = Gtk.Button.new_from_icon_name(
            "mail-send-symbolic", Gtk.IconSize.BUTTON
        )
        self.send_button.set_tooltip_text("Send to side thread")
        self.send_button.get_accessible().set_name("Send to side thread")
        self.send_button.get_style_context().add_class("branch-send")
        self.send_button.connect("clicked", lambda _button: self.submit())
        compose.pack_end(self.send_button, False, False, 0)
        self.pack_start(compose, False, False, 0)

    def submit(self) -> None:
        text = self.entry.get_text().strip()
        if not text or not self.send_button.get_sensitive():
            return
        self.entry.set_text("")
        self.on_send(self.branch_id, text, self)

    def add_message(self, role: str, content: str) -> Gtk.Widget:
        if role == "user":
            widget = Gtk.Label(label=content)
            widget.set_selectable(True)
            widget.set_line_wrap(True)
            rtl = content_prefers_rtl(content)
            widget.set_xalign(1.0 if rtl else 0.0)
            widget.set_direction(
                Gtk.TextDirection.RTL if rtl else Gtk.TextDirection.LTR
            )
            widget.set_halign(Gtk.Align.END)
            widget.get_style_context().add_class("branch-user-message")
        else:
            widget = MarkdownView(content or "…")
            widget.set_hexpand(True)
            widget.get_style_context().add_class("branch-assistant-message")
        self.messages.pack_start(widget, False, False, 0)
        widget.show_all()
        return widget

    def _update_entry_direction(self, entry: Gtk.Entry) -> None:
        # Inputs should switch as soon as the first Persian character arrives,
        # even if the question began with a Latin identifier.
        rtl = any("\u0590" <= char <= "\u08ff" for char in entry.get_text())
        entry.set_direction(
            Gtk.TextDirection.RTL if rtl else Gtk.TextDirection.LTR
        )
        entry.set_alignment(1.0 if rtl else 0.0)

    def start_answer(self) -> MarkdownView:
        self.answer_view = self.add_message("assistant", "")
        assert isinstance(self.answer_view, MarkdownView)
        self.set_busy(True)
        return self.answer_view

    def set_answer(self, text: str) -> None:
        if self.answer_view is None:
            self.start_answer()
        assert self.answer_view is not None
        self.answer_view.set_text(text or "…")

    def finish_answer(self, text: str) -> None:
        self.set_answer(text)
        self.set_busy(False)
        self.answer_view = None
        self.entry.grab_focus()

    def set_busy(self, busy: bool) -> None:
        self.entry.set_sensitive(not busy)
        self.send_button.set_sensitive(not busy)
        self.send_button.set_tooltip_text(
            "Waiting for Codex…" if busy else "Send to side thread"
        )

    def on_key_press(self, _widget: Gtk.Widget, event: Gdk.EventKey) -> bool:
        if event.keyval == Gdk.KEY_Escape:
            self.hide()
            return True
        return False
