"""Regression tests for the double-Enter send guard."""

from __future__ import annotations

import unittest
from unittest.mock import patch

import gi

gi.require_version("Gdk", "3.0")
from gi.repository import Gdk  # noqa: E402

from app import COMPOSER_HINT, MainWindow, SEND_ENTER_KEYS


class FakeHint:
    def __init__(self) -> None:
        self.text = COMPOSER_HINT

    def set_text(self, value: str) -> None:
        self.text = value


class EnterHarness:
    _reset_enter_send_confirmation = MainWindow._reset_enter_send_confirmation
    _expire_enter_send_confirmation = MainWindow._expire_enter_send_confirmation
    _confirm_send_with_enter = MainWindow._confirm_send_with_enter

    def __init__(self) -> None:
        self._enter_send_armed_until = 0.0
        self._enter_send_hint_source_id = None
        self.composer_hint = FakeHint()
        self.sent = 0

    def send_message(self) -> None:
        self.sent += 1


class KeyHarness(EnterHarness):
    on_input_key = MainWindow.on_input_key

    pending_image = None

    def _input_text(self) -> str:
        return "prompt"

    def _set_composer_rtl(self, _rtl: bool) -> None:
        pass

    def paste_clipboard_image(self) -> bool:
        return False


class KeyEvent:
    def __init__(self, keyval: int) -> None:
        self.keyval = keyval
        self.state = Gdk.ModifierType(0)
        self.hardware_keycode = 0


class EnterConfirmationTests(unittest.TestCase):
    def tearDown(self) -> None:
        if hasattr(self, "harness"):
            self.harness._reset_enter_send_confirmation()

    def test_second_enter_within_confirmation_window_sends(self) -> None:
        self.harness = EnterHarness()
        with patch("app.time.monotonic", side_effect=(100.0, 100.8)):
            self.assertTrue(self.harness._confirm_send_with_enter())
            self.assertEqual(self.harness.sent, 0)
            self.assertEqual(self.harness.composer_hint.text, "Press Enter again to send")
            self.assertTrue(self.harness._confirm_send_with_enter())
        self.assertEqual(self.harness.sent, 1)
        self.assertEqual(self.harness.composer_hint.text, COMPOSER_HINT)

    def test_enter_after_confirmation_window_only_rearms(self) -> None:
        self.harness = EnterHarness()
        with patch("app.time.monotonic", side_effect=(100.0, 101.4)):
            self.harness._confirm_send_with_enter()
            self.harness._confirm_send_with_enter()
        self.assertEqual(self.harness.sent, 0)
        self.assertEqual(self.harness.composer_hint.text, "Press Enter again to send")

    def test_iso_enter_is_treated_as_a_confirmed_send_key(self) -> None:
        self.harness = KeyHarness()
        self.assertIn(Gdk.KEY_ISO_Enter, SEND_ENTER_KEYS)
        event = KeyEvent(Gdk.KEY_ISO_Enter)
        with patch("app.time.monotonic", side_effect=(100.0, 100.3)):
            self.assertTrue(self.harness.on_input_key(None, event))
            self.assertTrue(self.harness.on_input_key(None, event))
        self.assertEqual(self.harness.sent, 1)


if __name__ == "__main__":
    unittest.main()
