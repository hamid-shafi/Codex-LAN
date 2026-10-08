#!/usr/bin/env python3
"""Render an isolated UI fixture for quick visual regression checks.

Usage: python3 tests/visual_smoke.py THEME OUTPUT.png [WIDTH HEIGHT]
The fixture always uses a temporary database and never connects to Codex.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

# Avoid capturing an unmapped/transparent first frame on compositors where GTK
# presents the surface asynchronously.
os.environ.setdefault("GDK_SYNCHRONIZE", "1")

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from project_layout import configure_imports, source_file
configure_imports(__file__)

import app  # noqa: E402
from branch_view import BranchPanel  # noqa: E402
from gi.repository import Gdk, GdkPixbuf, GLib, Gtk  # noqa: E402


def render(theme: str, output: Path, width: int, height: int) -> None:
    temporary = tempfile.TemporaryDirectory(prefix="hamsohbat-visual-")
    os.environ["HAMSOHBAT_DATA_DIR"] = temporary.name
    original_start = app.CodexBridge.start
    app.CodexBridge.start = lambda *_args, **_kwargs: None
    source = app.MainWindow(None)
    app.CodexBridge.start = original_start
    source.apply_theme(theme, persist=False)

    source.on_models_loaded(
        {
            "data": [
                {
                    "id": "gpt-5.6-codex",
                    "displayName": "GPT-5.6 Codex",
                    "description": "Coding model used by the visual fixture.",
                    "isDefault": True,
                    "defaultReasoningEffort": "max",
                    "supportedReasoningEfforts": [
                        {"reasoningEffort": "high"},
                        {"reasoningEffort": "xhigh"},
                        {"reasoningEffort": "max"},
                        {"reasoningEffort": "ultra"},
                    ],
                }
            ]
        }
    )

    room = source.active_room
    assert room is not None
    source.store.rename_room(int(room["id"]), "Selection and side threads")
    source.store.add_message(
        int(room["id"]),
        "user",
        "Check the English interface and preserve the explicit RTL sample below.",
    )
    source.store.add_message(
        int(room["id"]),
        "assistant",
        """The interface remains English while mixed-direction content keeps its own reading order.

**RTL regression sample (Persian):** \u0627\u06cc\u0646 \u0645\u062a\u0646 \u0641\u0627\u0631\u0633\u06cc \u0628\u0627\u06cc\u062f \u0627\u0632 \u0631\u0627\u0633\u062a \u0628\u0647 \u0686\u067e \u062e\u0648\u0627\u0646\u062f\u0647 \u0634\u0648\u062f \u0648 `inline_code` \u062c\u0647\u062a \u062e\u0648\u062f\u0634 \u0631\u0627 \u062d\u0641\u0638 \u06a9\u0646\u062f.

> Select this passage, then reply to it or open a private side thread.

- Continuous selection crosses prose, quotes, and lists.
- Technical names such as `SparkJob` remain readable.

```cpp
#include <iostream>
int main() {
    std::cout << "hello" << std::endl;
}
```

| Feature | State |
| --- | --- |
| Markdown | Active |
| RTL isolation | Active |""",
        duration_seconds=13.4,
        token_usage=1842,
    )
    room = source.store.get_room(int(room["id"]))
    assert room is not None
    source.load_room(room)
    source.effort_combo.set_active_id("ultra")

    assistant_bubbles = [
        child
        for child in source.message_box.get_children()
        if isinstance(child, app.MessageBubble) and child.role == "assistant"
    ]
    assert assistant_bubbles
    assistant = assistant_bubbles[-1]
    assistant._selection_changed(
        assistant.content_view,
        "Select this passage, then reply to it or open a private side thread.",
    )
    branch = BranchPanel(
        1,
        "Select this passage, then reply to it or open a private side thread.",
        [
            {"role": "user", "content": "Why is this thread private?"},
            {
                "role": "assistant",
                "content": "It stays attached to this answer and never enters the room timeline.",
            },
        ],
        lambda *_args: None,
    )
    assistant.add_branch_panel(branch)

    fixture_pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(
        str(ROOT / "apps/desktop/assets" / "codex.svg"), 160, 100, True
    )
    source.attach_clipboard_image(fixture_pixbuf)
    source.input_view.get_buffer().set_text(
        "Spark \u0627\u06cc\u0646 \u0627\u062f\u0627\u0645\u0647\u0654 \u0641\u0627\u0631\u0633\u06cc \u0628\u0627\u06cc\u062f \u062f\u0631 \u06a9\u0627\u062f\u0631 \u0646\u0648\u0634\u062a\u0646 \u0631\u0627\u0633\u062a\u200c\u0628\u0647\u200c\u0686\u067e \u0628\u0645\u0627\u0646\u062f"
    )

    content = source.get_child()
    assert content is not None
    source.remove(content)
    titlebar = source.get_titlebar()
    if titlebar is not None:
        source.set_titlebar(None)
    wrapper = Gtk.Window(title="Codex")
    # A smoke fixture must not steal the developer's keyboard while it renders.
    wrapper.set_accept_focus(False)
    wrapper.set_focus_on_map(False)
    wrapper.set_skip_taskbar_hint(True)
    wrapper_context = wrapper.get_style_context()
    for class_name in ("theme-night", "theme-light", "theme-graphite"):
        wrapper_context.remove_class(class_name)
    wrapper_context.add_class(f"theme-{theme}")
    wrapper_context.add_class(f"accent-{source.current_accent}")
    wrapper.set_default_size(width, height)
    if titlebar is not None:
        wrapper.set_titlebar(titlebar)
    wrapper.add(content)
    wrapper.show_all()
    wrapper.resize(width, height)
    screen = Gdk.Screen.get_default()
    if screen:
        Gtk.StyleContext.reset_widgets(screen)

    def reveal_selection_actions() -> bool:
        assistant._selection_changed(
            assistant.content_view,
            "Select this passage, then reply to it or open a private side thread.",
        )
        return False

    GLib.timeout_add(120, reveal_selection_actions)

    capture_attempts = 0

    def frame_has_content(pixbuf: GdkPixbuf.Pixbuf) -> bool:
        pixels = pixbuf.get_pixels()
        channels = pixbuf.get_n_channels()
        stride = pixbuf.get_rowstride()
        width = pixbuf.get_width()
        height = pixbuf.get_height()
        for y in range(0, height, max(1, height // 24)):
            for x in range(0, width, max(1, width // 24)):
                offset = y * stride + x * channels
                red, green, blue = pixels[offset : offset + 3]
                alpha = pixels[offset + 3] if channels >= 4 else 255
                if alpha > 8 and red + green + blue > 8:
                    return True
        return False

    def capture() -> bool:
        nonlocal capture_attempts
        capture_attempts += 1
        window = wrapper.get_window()
        allocation = wrapper.get_allocation()
        pixbuf = Gdk.pixbuf_get_from_window(
            window, 0, 0, allocation.width, allocation.height
        )
        if pixbuf is None or not frame_has_content(pixbuf):
            if capture_attempts < 6:
                GLib.timeout_add(180, capture)
                return False
            raise RuntimeError("GTK returned an empty screenshot after six attempts")
        output.parent.mkdir(parents=True, exist_ok=True)
        pixbuf.savev(str(output), "png", [], [])
        wrapper.destroy()
        Gtk.main_quit()
        return False

    GLib.timeout_add(700, capture)
    Gtk.main()
    source.bridge.close()
    source.store.close()
    temporary.cleanup()


if __name__ == "__main__":
    selected = sys.argv[1] if len(sys.argv) > 1 else "night"
    destination = Path(sys.argv[2] if len(sys.argv) > 2 else "/tmp/hamsohbat-visual.png")
    render(
        selected,
        destination,
        int(sys.argv[3]) if len(sys.argv) > 3 else 1180,
        int(sys.argv[4]) if len(sys.argv) > 4 else 780,
    )
