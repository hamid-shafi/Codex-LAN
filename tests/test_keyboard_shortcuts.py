"""Headless regression coverage for model and reasoning keyboard shortcuts."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import gi

gi.require_version("Gdk", "3.0")
from gi.repository import Gdk  # noqa: E402


PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR))

from app import MainWindow, model_display_label, preferred_model_id  # noqa: E402
from storage import Store  # noqa: E402


class FakeControl:
    def __init__(self) -> None:
        self.sensitive = True
        self.tooltip = ""

    def set_sensitive(self, value: bool) -> None:
        self.sensitive = bool(value)

    def set_tooltip_text(self, value: str) -> None:
        self.tooltip = str(value)


class FakeCombo(FakeControl):
    def __init__(self) -> None:
        super().__init__()
        self.items: list[tuple[str, str]] = []
        self.active_id: str | None = None
        self.changed_callback = None

    def _emit_changed(self) -> None:
        if self.changed_callback:
            self.changed_callback(self)

    def remove_all(self) -> None:
        self.items.clear()
        self.active_id = None
        self._emit_changed()

    def append(self, item_id: str, label: str) -> None:
        self.items.append((str(item_id), str(label)))

    def get_active(self) -> int:
        return next(
            (
                index
                for index, (item_id, _label) in enumerate(self.items)
                if item_id == self.active_id
            ),
            -1,
        )

    def set_active(self, index: int) -> None:
        self.active_id = (
            self.items[index][0] if 0 <= index < len(self.items) else None
        )
        self._emit_changed()

    def set_active_id(self, item_id: str) -> None:
        value = str(item_id)
        self.active_id = (
            value if any(key == value for key, _label in self.items) else None
        )
        self._emit_changed()

    def get_active_id(self) -> str | None:
        return self.active_id


class FakeInputView:
    def set_editable(self, _value: bool) -> None:
        pass


class ShortcutEvent:
    def __init__(self, keyval: int, state: Gdk.ModifierType) -> None:
        self.keyval = keyval
        self.state = state
        self.hardware_keycode = 0


class ShortcutHarness:
    on_window_key = MainWindow.on_window_key
    update_effort_options = MainWindow.update_effort_options
    update_model_tooltip = MainWindow.update_model_tooltip
    update_effort_tooltip = MainWindow.update_effort_tooltip
    on_model_changed = MainWindow.on_model_changed
    on_effort_changed = MainWindow.on_effort_changed

    def __init__(self, store: Store, room: dict, models: dict) -> None:
        self.store = store
        self.active_room = room
        self.models = models
        self.room_turns = {}
        self._loading_room = False
        self._syncing_controls = False
        self.model_combo = FakeCombo()
        self.effort_combo = FakeCombo()
        self._effort_options_by_id = {}
        self.model_combo.changed_callback = self.on_model_changed
        self.effort_combo.changed_callback = self.on_effort_changed
        self.shortcut_notices: list[str] = []
        self.pending_reply = None
        self.pending_image = None
        self.message_bubbles = {}
        for model_id, model in self.models.items():
            self.model_combo.append(model_id, model_display_label(model_id, model))
        selected = preferred_model_id(self.models, room.get("model"))
        if selected:
            self.model_combo.set_active_id(selected)
        self.update_effort_options()

    def __getattr__(self, name: str):
        implementation = getattr(MainWindow, name, None)
        if callable(implementation):
            return implementation.__get__(self, type(self))
        raise AttributeError(name)

    def _input_text(self) -> str:
        return ""

    def _show_composer_notice(self, text: str) -> None:
        self.shortcut_notices.append(text)


class KeyboardShortcutTests(unittest.TestCase):
    @staticmethod
    def models() -> dict:
        return {
            "alpha": {
                "id": "alpha",
                "displayName": "Alpha",
                "isDefault": True,
                "defaultReasoningEffort": "high",
                "supportedReasoningEfforts": [
                    {"reasoningEffort": "low"},
                    {"reasoningEffort": "high"},
                    {"reasoningEffort": "ultra"},
                ],
            },
            "beta": {
                "id": "beta",
                "displayName": "Beta",
                "defaultReasoningEffort": "medium",
                "supportedReasoningEfforts": [
                    {"reasoningEffort": "minimal"},
                    {"reasoningEffort": "medium"},
                ],
            },
        }

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tempdir.name))
        self.room = self.store.create_room("Shortcuts")
        self.store.update_room(self.room["id"], model="alpha", effort="high")
        self.room = self.store.get_room(self.room["id"])
        self.harness = ShortcutHarness(self.store, self.room, self.models())

    def tearDown(self) -> None:
        self.store.close()
        self.tempdir.cleanup()

    def event(self, keyval: int, modifiers: Gdk.ModifierType) -> ShortcutEvent:
        return ShortcutEvent(keyval, modifiers)

    def persisted_pair(self) -> tuple[str | None, str | None]:
        room = self.store.get_room(self.room["id"])
        return room["model"], room["effort"]

    def test_alt_arrows_step_reasoning_and_persist_only_the_effort(self) -> None:
        alt = Gdk.ModifierType.MOD1_MASK

        self.assertTrue(
            self.harness.on_window_key(None, self.event(Gdk.KEY_Down, alt))
        )
        self.assertEqual(self.harness.model_combo.get_active_id(), "alpha")
        self.assertEqual(self.harness.effort_combo.get_active_id(), "low")
        self.assertEqual(self.persisted_pair(), ("alpha", "low"))

        self.assertTrue(
            self.harness.on_window_key(None, self.event(Gdk.KEY_Up, alt))
        )
        self.assertEqual(self.harness.effort_combo.get_active_id(), "high")
        self.assertEqual(self.persisted_pair(), ("alpha", "high"))

    def test_reasoning_stops_at_the_supported_bounds(self) -> None:
        alt = Gdk.ModifierType.MOD1_MASK
        self.harness.effort_combo.set_active_id("ultra")

        self.assertTrue(
            self.harness.on_window_key(None, self.event(Gdk.KEY_Up, alt))
        )
        self.assertEqual(self.harness.effort_combo.get_active_id(), "ultra")
        self.assertEqual(self.persisted_pair(), ("alpha", "ultra"))

        self.harness.effort_combo.set_active_id("low")
        self.assertTrue(
            self.harness.on_window_key(None, self.event(Gdk.KEY_Down, alt))
        )
        self.assertEqual(self.harness.effort_combo.get_active_id(), "low")
        self.assertEqual(self.persisted_pair(), ("alpha", "low"))

    def test_ctrl_alt_arrows_change_models_and_clamp_incompatible_effort(self) -> None:
        ctrl_alt = Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.MOD1_MASK
        self.harness.effort_combo.set_active_id("ultra")

        self.assertTrue(
            self.harness.on_window_key(None, self.event(Gdk.KEY_Down, ctrl_alt))
        )
        self.assertEqual(self.harness.model_combo.get_active_id(), "beta")
        self.assertEqual(self.harness.effort_combo.get_active_id(), "medium")
        self.assertEqual(self.persisted_pair(), ("beta", "medium"))

        available = {
            entry["reasoningEffort"]
            for entry in self.models()["beta"]["supportedReasoningEfforts"]
        }
        self.assertIn(self.harness.effort_combo.get_active_id(), available)

        self.assertTrue(
            self.harness.on_window_key(None, self.event(Gdk.KEY_Up, ctrl_alt))
        )
        self.assertEqual(self.harness.model_combo.get_active_id(), "alpha")
        self.assertEqual(self.harness.effort_combo.get_active_id(), "high")
        self.assertEqual(self.persisted_pair(), ("alpha", "high"))

    def test_ctrl_alt_never_falls_through_to_the_effort_shortcut(self) -> None:
        ctrl_alt = Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.MOD1_MASK

        self.assertTrue(
            self.harness.on_window_key(None, self.event(Gdk.KEY_Up, ctrl_alt))
        )
        self.assertEqual(self.harness.model_combo.get_active_id(), "beta")
        self.assertEqual(self.harness.effort_combo.get_active_id(), "medium")

    def test_plain_arrows_are_not_consumed_by_header_shortcuts(self) -> None:
        self.assertFalse(
            self.harness.on_window_key(
                None, self.event(Gdk.KEY_Down, Gdk.ModifierType(0))
            )
        )
        self.assertEqual(self.persisted_pair(), ("alpha", "high"))

    def test_search_shortcut_translates_the_physical_key_across_layouts(self) -> None:
        self.harness.get_display = Mock(return_value=None)
        self.harness.sidebar_revealer = Mock()
        self.harness.sidebar_revealer.get_reveal_child.return_value = True
        self.harness.room_search = Mock()
        keymap = Mock()
        keymap.translate_keyboard_state.return_value = (True, Gdk.KEY_k, 0, 0, 0)
        control = Gdk.ModifierType.CONTROL_MASK
        with patch.object(Gdk.Keymap, "get_for_display", return_value=keymap):
            for key in ("ن", "ל", "κ"):
                with self.subTest(key=key):
                    event = self.event(Gdk.unicode_to_keyval(ord(key)), control)
                    event.hardware_keycode = 45
                    self.assertTrue(self.harness.on_window_key(None, event))
                    keymap.translate_keyboard_state.assert_called_with(
                        45, Gdk.ModifierType(0), 0
                    )
        self.assertEqual(self.harness.room_search.grab_focus.call_count, 3)

    def test_shortcuts_do_not_visually_or_persistently_change_a_busy_room(self) -> None:
        self.harness.room_turns = {
            self.room["id"]: {"turn_id": "active-turn", "stop_requested": False}
        }
        alt = Gdk.ModifierType.MOD1_MASK
        ctrl_alt = Gdk.ModifierType.CONTROL_MASK | alt

        self.assertTrue(
            self.harness.on_window_key(None, self.event(Gdk.KEY_Up, alt))
        )
        self.assertTrue(
            self.harness.on_window_key(None, self.event(Gdk.KEY_Down, ctrl_alt))
        )
        self.assertEqual(self.harness.model_combo.get_active_id(), "alpha")
        self.assertEqual(self.harness.effort_combo.get_active_id(), "high")
        self.assertEqual(self.persisted_pair(), ("alpha", "high"))


if __name__ == "__main__":
    unittest.main()
