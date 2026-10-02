"""CODEX: WebSocket liveness policy and secret-free diagnostics regressions."""

from __future__ import annotations

import ast
import logging
import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from codex_lan.server import log_transport_close
from codex_lan.transport import (
    LAN_WEBSOCKET_HEARTBEAT_SECONDS,
    device_id_suffix,
    format_transport_diagnostic,
    websocket_diagnostic,
)


class FakeWebSocket:
    def __init__(self, close_code, error=None):
        self.close_code = close_code
        self.error = error

    def exception(self):
        return self.error


class TransportPolicyTests(unittest.TestCase):
    def test_all_lan_websockets_share_the_recovery_heartbeat(self):
        self.assertEqual(LAN_WEBSOCKET_HEARTBEAT_SECONDS, 30.0)
        root = Path(__file__).resolve().parents[1] / "codex_lan"
        expected = {"server.py": 2, "worker.py": 2, "host_worker.py": 1}
        for name, count in expected.items():
            tree = ast.parse((root / name).read_text())
            heartbeat_values = [
                keyword.value
                for node in ast.walk(tree)
                if isinstance(node, ast.Call)
                for keyword in node.keywords
                if keyword.arg == "heartbeat"
            ]
            with self.subTest(name=name):
                self.assertEqual(len(heartbeat_values), count)
                self.assertTrue(all(
                    isinstance(value, ast.Name)
                    and value.id == "LAN_WEBSOCKET_HEARTBEAT_SECONDS"
                    for value in heartbeat_values
                ))

    def test_diagnostics_keep_types_and_timing_but_never_error_messages(self):
        websocket = FakeWebSocket(1006, ConnectionResetError("PRIVATE websocket detail"))
        diagnostic = websocket_diagnostic(websocket, RuntimeError("PRIVATE bearer token"))
        self.assertEqual(diagnostic, {
            "close_code": 1006,
            "websocket_error": "ConnectionResetError",
            "failure_type": "RuntimeError",
        })
        rendered = format_transport_diagnostic(
            "executor", diagnostic, attempt=3, retry_seconds=2.75
        )
        self.assertEqual(
            rendered,
            "role=executor close_code=1006 websocket_error=ConnectionResetError "
            "failure_type=RuntimeError attempt=3 retry_in_seconds=2.8",
        )
        self.assertNotIn("PRIVATE", rendered)

    def test_server_close_log_identifies_only_a_device_suffix(self):
        identifier = "private-device-prefix-12345678"
        websocket = FakeWebSocket(1006, ConnectionResetError("PRIVATE websocket detail"))
        with self.assertLogs("codex_lan.server", logging.WARNING) as captured:
            log_transport_close(
                "executor", identifier, websocket, RuntimeError("PRIVATE bearer token")
            )
        message = "\n".join(captured.output)
        self.assertEqual(device_id_suffix(identifier), "12345678")
        self.assertIn("role=executor", message)
        self.assertIn("device_suffix=12345678", message)
        self.assertIn("close_code=1006", message)
        self.assertIn("failure_type=RuntimeError", message)
        self.assertNotIn(identifier, message)
        self.assertNotIn("PRIVATE", message)


if __name__ == "__main__":
    unittest.main()
