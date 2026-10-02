"""CODEX: Native systemd watchdog and resilient service unit regressions."""

from __future__ import annotations

import asyncio
import socket
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from codex_lan.systemd_watchdog import (
    _watchdog_loop,
    _loop_diagnostics,
    notify_systemd,
    start_loop_diagnostics,
    stop_loop_diagnostics,
    start_systemd_watchdog,
    watchdog_interval,
)
from install_lan_server import service_unit


class FakeDatagram:
    def __init__(self):
        self.connected = None
        self.sent = None
        self.closed = False

    def connect(self, address):
        self.connected = address

    def sendall(self, payload):
        self.sent = payload

    def close(self):
        self.closed = True


class SystemdWatchdogTests(unittest.IsolatedAsyncioTestCase):
    def test_notify_supports_abstract_systemd_socket(self):
        channel = FakeDatagram()
        calls = []

        def factory(family, kind):
            calls.append((family, kind))
            return channel

        self.assertTrue(notify_systemd(
            "READY=1", environment={"NOTIFY_SOCKET": "@notify-test"},
            socket_factory=factory,
        ))
        self.assertEqual(calls, [(socket.AF_UNIX, socket.SOCK_DGRAM)])
        self.assertEqual(channel.connected, "\0notify-test")
        self.assertEqual(channel.sent, b"READY=1")
        self.assertTrue(channel.closed)

    def test_notify_is_safe_noop_without_a_valid_socket(self):
        self.assertFalse(notify_systemd("READY=1", environment={}))
        self.assertFalse(notify_systemd(
            "READY=1", environment={"NOTIFY_SOCKET": "relative/socket"}
        ))

    def test_interval_is_half_the_deadline_and_bound_to_the_main_pid(self):
        environment = {
            "NOTIFY_SOCKET": "/run/user/1000/notify",
            "WATCHDOG_USEC": "90000000",
            "WATCHDOG_PID": "42",
        }
        self.assertEqual(watchdog_interval(environment=environment, process_id=42), 45.0)
        self.assertIsNone(watchdog_interval(environment=environment, process_id=41))
        self.assertIsNone(watchdog_interval(
            environment={"NOTIFY_SOCKET": "/tmp/n", "WATCHDOG_USEC": "invalid"},
            process_id=42,
        ))

    async def test_loop_waits_then_sends_watchdog_pulses(self):
        waits = []
        messages = []

        async def sleep_once(value):
            waits.append(value)
            if len(waits) > 1:
                raise asyncio.CancelledError

        def notifier(message, *, environment=None):
            messages.append((message, environment))
            return True

        environment = {"NOTIFY_SOCKET": "/tmp/n"}
        with self.assertRaises(asyncio.CancelledError):
            await _watchdog_loop(
                45.0, environment=environment, sleeper=sleep_once, notifier=notifier
            )
        self.assertEqual(waits, [45.0, 45.0])
        self.assertEqual(messages, [("WATCHDOG=1", environment)])

    async def test_start_is_a_noop_outside_systemd(self):
        self.assertIsNone(start_systemd_watchdog(environment={}))

    async def test_loop_diagnostics_start_without_systemd_and_cancel_cleanly(self):
        handler = mock.Mock()
        task = start_loop_diagnostics(timeout=30, interval=5, handler=handler)
        await asyncio.sleep(0)
        handler.dump_traceback_later.assert_called_once_with(30, repeat=True)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        handler.cancel_dump_traceback_later.assert_called_once_with()

    async def test_diagnostics_rearm_only_after_loop_heartbeat(self):
        handler = mock.Mock()
        waits = []

        async def sleeper(interval):
            waits.append(interval)
            if len(waits) == 3:
                raise asyncio.CancelledError

        with self.assertRaises(asyncio.CancelledError):
            await _loop_diagnostics(5, 30, sleeper=sleeper, handler=handler)
        self.assertEqual(handler.dump_traceback_later.call_args_list,
                         [mock.call(30, repeat=True), mock.call(30, repeat=True)])
        handler.cancel_dump_traceback_later.assert_called_once_with()

    async def test_diagnostics_disarm_when_startup_fails_before_first_task_run(self):
        handler = mock.Mock()
        task = start_loop_diagnostics(timeout=30, interval=5, handler=handler)
        await stop_loop_diagnostics(task, handler=handler)
        handler.cancel_dump_traceback_later.assert_called_once_with()

    def test_native_diagnostics_capture_a_real_loop_stall_without_termination(self):
        script = '''
import asyncio, time
from codex_lan.systemd_watchdog import start_loop_diagnostics
async def main():
    task = start_loop_diagnostics(timeout=.15, interval=.03)
    await asyncio.sleep(.24)
    time.sleep(.24)
    await asyncio.sleep(.06)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    print("still alive")
asyncio.run(main())
'''
        result = subprocess.run([sys.executable, "-c", script], cwd=Path(__file__).resolve().parents[1],
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Timeout", result.stderr)
        self.assertGreaterEqual(result.stderr.count("Timeout"), 1, result.stderr)
        self.assertIn("still alive", result.stdout)


class ServiceUnitTests(unittest.TestCase):
    def test_server_unit_leaves_hang_recovery_to_guarded_monitor(self):
        unit = service_unit("codex-lan-server", ["/tmp/python", "-m", "codex_lan.server"], Path("/tmp/code"))
        self.assertNotIn("Type=notify\n", unit)
        self.assertNotIn("NotifyAccess=main\n", unit)
        self.assertNotIn("WatchdogSec=", unit)
        self.assertIn("TimeoutStartSec=300s\n", unit)
        self.assertIn("TimeoutStopSec=20\n", unit)
        self.assertIn("Restart=always\n", unit)

    def test_worker_always_restarts_without_claiming_server_watchdog(self):
        unit = service_unit("codex-lan-host-worker", ["/tmp/python", "-m", "codex_lan.worker"], Path("/tmp/code"))
        self.assertIn("Restart=always\n", unit)
        self.assertNotIn("WatchdogSec=", unit)
        self.assertNotIn("Type=notify", unit)


if __name__ == "__main__":
    unittest.main()
