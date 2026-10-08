"""CODEX: Regression tests for independent Codex LAN health recovery."""

from __future__ import annotations

import io
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest

import deploy_lan_backend
from codex_lan.health_monitor import (
    DEFAULT_DATABASE,
    DEFAULT_SERVICES,
    HealthMonitor,
    active_run_count,
    latest_event_id,
    parser,
    probe_health,
    restart_services,
)
from install_lan_server import service_unit


class FakeResponse:
    """CODEX: Provide the small urllib response surface used by the health probe."""

    def __init__(self, body: bytes, status: int = 200):
        self.body = body
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self, size=-1):
        return io.BytesIO(self.body).read(size)

    def getcode(self):
        return self.status


class StopAfterWait:
    """CODEX: Stop a monitor loop after its first interruptible wait."""

    def __init__(self):
        self.stopped = False
        self.waits = []

    def is_set(self):
        return self.stopped

    def wait(self, timeout):
        self.waits.append(timeout)
        self.stopped = True
        return True


class HealthProbeTests(unittest.TestCase):
    def test_probe_requires_explicit_ok_true(self):
        requests = []

        def opener(request, timeout):
            requests.append((request, timeout))
            return FakeResponse(b'{"ok": true}')

        self.assertTrue(probe_health("http://127.0.0.1:8765/api/health", 5.0, opener=opener))
        self.assertEqual(requests[0][1], 5.0)
        self.assertEqual(requests[0][0].full_url, "http://127.0.0.1:8765/api/health")
        self.assertFalse(probe_health("http://fixture", 1.0, opener=lambda request, timeout: FakeResponse(b'{"ok": false}')))

    def test_probe_rejects_oversized_responses(self):
        response = FakeResponse(b"x" * (64 * 1024 + 1))
        self.assertFalse(probe_health("http://fixture", 1.0, opener=lambda request, timeout: response))

    def test_restart_uses_one_user_systemd_transaction(self):
        calls = []

        def runner(command, **options):
            calls.append((command, options))
            return subprocess.CompletedProcess(command, 0)

        restart_services(("server.service", "worker.service"), runner=runner, timeout=12)
        self.assertEqual(calls, [
            (["systemctl", "--user", "restart", "server.service", "worker.service"], {"check": True, "timeout": 12})
        ])

    def test_default_policy_restarts_only_the_server_after_two_bounded_checks(self):
        arguments = parser().parse_args([])
        self.assertEqual(DEFAULT_SERVICES, ("codex-lan-server.service",))
        self.assertEqual(arguments.interval, 15.0)
        self.assertEqual(arguments.timeout, 5.0)
        self.assertEqual(arguments.failure_threshold, 4)
        self.assertEqual(arguments.cooldown, 300.0)
        self.assertEqual(arguments.active_stall_timeout, 60.0)
        self.assertEqual(arguments.unknown_state_timeout, 180.0)
        self.assertEqual(arguments.database, DEFAULT_DATABASE)

    def test_active_run_count_reads_only_queued_and_running_work(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "server.sqlite3"
            with sqlite3.connect(database) as connection:
                connection.execute("CREATE TABLE runs (id TEXT, status TEXT)")
                connection.executemany(
                    "INSERT INTO runs VALUES (?, ?)",
                    (("a", "queued"), ("b", "running"), ("c", "completed")),
                )
            self.assertEqual(active_run_count(database), 2)

    def test_progress_reads_latest_committed_event_and_handles_empty_history(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "server.sqlite3"
            with sqlite3.connect(database) as connection:
                connection.execute("CREATE TABLE events (id INTEGER PRIMARY KEY, payload TEXT)")
            self.assertEqual(latest_event_id(database), 0)
            with sqlite3.connect(database) as connection:
                connection.executemany("INSERT INTO events VALUES (?, ?)", ((4, "private"), (9, "private")))
                self.assertEqual(latest_event_id(database), 0)
            self.assertEqual(latest_event_id(database), 9)

    def test_deploy_installs_monitor_disabled_until_activation_is_healthy(self):
        calls = []

        def runner(command, **options):
            calls.append((command, options))
            return subprocess.CompletedProcess(command, 0)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            unit = deploy_lan_backend._install_health_monitor_unit(
                installation=root / "installation", unit_directory=root / "units",
                runner=runner, start=False,
            )
            content = unit.read_text()
        self.assertIn("codex_lan.health_monitor", content)
        self.assertIn("Restart=always", content)
        self.assertIn('"--interval" "15"', content)
        self.assertIn('"--timeout" "5"', content)
        self.assertIn('"--failure-threshold" "4"', content)
        self.assertIn('"--cooldown" "300"', content)
        self.assertIn('"--active-stall-timeout" "60"', content)
        self.assertEqual([call[0][2] for call in calls], ["daemon-reload", "enable"])

    def test_deploy_restarts_only_monitor_after_healthy_activation(self):
        calls = []

        def runner(command, **options):
            calls.append(command)
            return subprocess.CompletedProcess(command, 0)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            deploy_lan_backend._install_health_monitor_unit(
                installation=root / "installation", unit_directory=root / "units",
                runner=runner, start=True,
            )
        self.assertEqual(calls[-1], ["systemctl", "--user", "restart",
                                    deploy_lan_backend.HEALTH_MONITOR_SERVICE])

    def test_backend_deploy_upgrades_legacy_restart_policy_without_restarting_services(self):
        calls = []

        def runner(command, **options):
            calls.append((command, options))
            return subprocess.CompletedProcess(command, 0)

        with tempfile.TemporaryDirectory() as directory:
            units = Path(directory) / "units"
            units.mkdir()
            installed = deploy_lan_backend._install_restart_policy_dropins(
                unit_directory=units, runner=runner,
            )
            contents = [path.read_text() for path in installed]
            modes = [path.stat().st_mode & 0o777 for path in installed]
        self.assertEqual(
            [path.parent.name for path in installed],
            [f"{deploy_lan_backend.SERVICE}.d", f"{deploy_lan_backend.WORKER_SERVICE}.d"],
        )
        self.assertEqual(contents, [
            "[Service]\nType=simple\nWatchdogSec=0\n"
            "TimeoutStartSec=300s\nTimeoutStopSec=20s\nRestart=always\nRestartSec=5\n",
            "[Service]\nRestart=always\nRestartSec=5\n",
        ])
        self.assertEqual(modes, [0o644, 0o644])
        self.assertEqual(calls, [
            (["systemctl", "--user", "daemon-reload"], {"check": True, "timeout": 30}),
        ])
        self.assertFalse(any("restart" in command for command, _options in calls))

    def test_restart_policy_migration_refuses_symlinked_dropin(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            units = root / "units"
            units.mkdir()
            external = root / "external"
            external.mkdir()
            (units / f"{deploy_lan_backend.SERVICE}.d").symlink_to(
                external, target_is_directory=True,
            )
            with self.assertRaisesRegex(RuntimeError, "symbolic-link path"):
                deploy_lan_backend._install_restart_policy_dropins(
                    unit_directory=units, runner=lambda *_args, **_options: None,
                )

    def test_fresh_install_orders_monitor_after_server_without_native_watchdog(self):
        unit = service_unit(
            "codex-lan-health-monitor", ["/tmp/python", "-m", "codex_lan.health_monitor"],
            Path("/tmp/code"),
        )
        self.assertIn("After=network.target codex-lan-server.service", unit)
        self.assertIn("Restart=always", unit)
        self.assertNotIn("WatchdogSec=", unit)


class HealthMonitorTests(unittest.TestCase):
    def monitor(self, outcomes, *, marker_state=None, clock=None, cooldown=120.0,
                active_runs=None, active_stall_timeout=300.0):
        restarts = []
        marker_state = marker_state if marker_state is not None else {"active": False}
        values = iter(outcomes)
        monitor = HealthMonitor(
            endpoint="http://fixture/health",
            failure_threshold=2,
            cooldown=cooldown,
            active_stall_timeout=active_stall_timeout,
            marker=Path("/fixture/backend-update.maintenance"),
            probe=lambda endpoint, timeout: next(values),
            restart=lambda services: restarts.append(tuple(services)),
            marker_exists=lambda path: marker_state["active"],
            active_runs=active_runs or (lambda: 0),
            active_progress=lambda: 0,
            monotonic=(clock or (lambda: 1000.0)),
        )
        return monitor, restarts, marker_state

    def test_two_consecutive_failures_restart_but_transient_failure_does_not(self):
        monitor, restarts, marker = self.monitor([False, False])
        self.assertEqual(monitor.check_once(), "failed")
        self.assertFalse(restarts)
        self.assertEqual(monitor.check_once(), "restarted")
        self.assertEqual(restarts, [("codex-lan-server.service",)])
        self.assertEqual(monitor.failures, 0)

    def test_success_breaks_the_consecutive_failure_streak(self):
        monitor, restarts, marker = self.monitor([False, True, False, False])
        self.assertEqual([monitor.check_once() for _ in range(3)], ["failed", "healthy", "failed"])
        self.assertFalse(restarts)
        self.assertEqual(monitor.check_once(), "restarted")

    def test_backend_update_marker_pauses_and_then_cleanly_resumes(self):
        monitor, restarts, marker = self.monitor([False, False])
        marker["active"] = True
        for _index in range(5):
            self.assertEqual(monitor.check_once(), "maintenance")
        self.assertFalse(restarts)
        marker["active"] = False
        self.assertEqual(
            [monitor.check_once() for _ in range(2)],
            ["failed", "restarted"],
        )
        self.assertEqual(len(restarts), 1)

    def test_cooldown_suppresses_restart_storms_then_allows_recovery(self):
        now = {"value": 100.0}
        monitor, restarts, marker = self.monitor([False] * 6, clock=lambda: now["value"], cooldown=300)
        self.assertEqual([monitor.check_once() for _ in range(2)][-1], "restarted")
        now["value"] = 120.0
        self.assertEqual([monitor.check_once() for _ in range(2)][-1], "cooldown")
        self.assertEqual(len(restarts), 1)
        now["value"] = 401.0
        self.assertEqual([monitor.check_once() for _ in range(2)][-1], "restarted")
        self.assertEqual(len(restarts), 2)

    def test_restart_failure_is_caught_and_rate_limited(self):
        monitor, restarts, marker = self.monitor([False, False])

        def fail(services):
            raise subprocess.CalledProcessError(1, list(services))

        monitor.restart = fail
        self.assertEqual([monitor.check_once() for _ in range(2)][-1], "restart_failed")
        self.assertEqual(monitor.failures, 0)
        self.assertEqual(monitor.last_restart_at, 1000.0)

    def test_active_responses_suppress_restart_until_work_finishes(self):
        state = {"active": 2}
        monitor, restarts, marker = self.monitor(
            [False, False, False, False], active_runs=lambda: state["active"],
        )
        self.assertEqual(monitor.check_once(), "failed")
        self.assertEqual(monitor.check_once(), "active_runs")
        self.assertEqual(monitor.check_once(), "active_runs")
        self.assertFalse(restarts)
        state["active"] = 0
        self.assertEqual(monitor.check_once(), "restarted")
        self.assertEqual(len(restarts), 1)

    def test_unknown_active_state_fails_closed_without_restarting(self):
        def unavailable():
            raise sqlite3.OperationalError("database is locked")

        monitor, restarts, marker = self.monitor(
            [False, False, False], active_runs=unavailable,
        )
        self.assertEqual(monitor.check_once(), "failed")
        self.assertEqual(monitor.check_once(), "active_state_unknown")
        self.assertEqual(monitor.check_once(), "active_state_unknown")
        self.assertFalse(restarts)

    def test_unresponsive_server_recovers_when_active_work_stays_stalled(self):
        now = {"value": 100.0}
        monitor, restarts, _marker = self.monitor(
            [False] * 4, clock=lambda: now["value"], active_runs=lambda: 2,
        )
        self.assertEqual(monitor.check_once(), "failed")
        self.assertEqual(monitor.check_once(), "active_runs")
        now["value"] = 399.0
        self.assertEqual(monitor.check_once(), "active_runs")
        self.assertFalse(restarts)
        now["value"] = 400.0
        self.assertEqual(monitor.check_once(), "restarted")
        self.assertEqual(restarts, [("codex-lan-server.service",)])

    def test_production_grace_recovers_after_sixty_seconds_of_no_event_progress(self):
        now = {"value": 100.0}
        monitor, restarts, _marker = self.monitor(
            [False] * 4, clock=lambda: now["value"], active_runs=lambda: 1,
            active_stall_timeout=60.0,
        )
        monitor.check_once()
        self.assertEqual(monitor.check_once(), "active_runs")
        now["value"] = 159.0
        self.assertEqual(monitor.check_once(), "active_runs")
        now["value"] = 160.0
        self.assertEqual(monitor.check_once(), "restarted")
        self.assertEqual(len(restarts), 1)

    def test_unreadable_active_state_cannot_disable_recovery_forever(self):
        def unavailable():
            raise sqlite3.OperationalError("database is locked")

        for unavailable_field in ("active_runs", "active_progress"):
            with self.subTest(field=unavailable_field):
                now = {"value": 100.0}
                monitor, restarts, _marker = self.monitor(
                    [False] * 4, clock=lambda: now["value"], active_runs=lambda: 1,
                )
                setattr(monitor, unavailable_field, unavailable)
                monitor.check_once()
                self.assertEqual(monitor.check_once(), "active_state_unknown")
                now["value"] = 279.0
                self.assertEqual(monitor.check_once(), "active_state_unknown")
                self.assertFalse(restarts)
                now["value"] = 280.0
                self.assertEqual(monitor.check_once(), "restarted")
                self.assertEqual(len(restarts), 1)

    def test_health_recovery_resets_unknown_state_deadline(self):
        def unavailable():
            raise sqlite3.OperationalError("database is locked")

        now = {"value": 100.0}
        monitor, restarts, _marker = self.monitor(
            [False, False, True, False, False], clock=lambda: now["value"], active_runs=unavailable,
        )
        monitor.check_once()
        self.assertEqual(monitor.check_once(), "active_state_unknown")
        now["value"] = 500.0
        self.assertEqual(monitor.check_once(), "healthy")
        monitor.check_once()
        self.assertEqual(monitor.check_once(), "active_state_unknown")
        self.assertFalse(restarts)

    def test_persisted_progress_gives_active_work_a_new_grace_period(self):
        now = {"value": 100.0}
        progress = {"value": 10}
        monitor, restarts, _marker = self.monitor(
            [False] * 5, clock=lambda: now["value"], active_runs=lambda: 2,
        )
        monitor.active_progress = lambda: progress["value"]
        monitor.check_once()
        self.assertEqual(monitor.check_once(), "active_runs")
        now["value"] = 400.0
        progress["value"] += 1
        self.assertEqual(monitor.check_once(), "active_runs")
        now["value"] = 699.0
        self.assertEqual(monitor.check_once(), "active_runs")
        self.assertFalse(restarts)
        now["value"] = 700.0
        self.assertEqual(monitor.check_once(), "restarted")

    def test_health_recovery_resets_active_stall_deadline(self):
        now = {"value": 100.0}
        monitor, restarts, _marker = self.monitor(
            [False, False, True, False, False], clock=lambda: now["value"], active_runs=lambda: 2,
        )
        monitor.check_once()
        self.assertEqual(monitor.check_once(), "active_runs")
        now["value"] = 800.0
        self.assertEqual(monitor.check_once(), "healthy")
        self.assertEqual(monitor.check_once(), "failed")
        self.assertEqual(monitor.check_once(), "active_runs")
        self.assertFalse(restarts)

    def test_unknown_progress_resets_deadline_without_restarting(self):
        now = {"value": 100.0}
        monitor, restarts, _marker = self.monitor(
            [False] * 4, clock=lambda: now["value"], active_runs=lambda: 2,
        )
        monitor.check_once()
        self.assertEqual(monitor.check_once(), "active_runs")

        def unavailable():
            raise sqlite3.OperationalError("database is locked")

        now["value"] = 800.0
        monitor.active_progress = unavailable
        self.assertEqual(monitor.check_once(), "active_state_unknown")
        monitor.active_progress = lambda: 0
        self.assertEqual(monitor.check_once(), "active_runs")
        self.assertFalse(restarts)

    def test_run_uses_an_interruptible_wait_for_clean_shutdown(self):
        probes = []
        stop = StopAfterWait()
        monitor = HealthMonitor(
            probe=lambda endpoint, timeout: probes.append((endpoint, timeout)) or True,
            marker_exists=lambda path: False,
            active_runs=lambda: 0,
        )
        monitor.run(stop)
        self.assertEqual(len(probes), 1)
        self.assertEqual(stop.waits, [15.0])

    def test_invalid_policy_is_rejected(self):
        with self.assertRaises(ValueError):
            HealthMonitor(interval=0)
        with self.assertRaises(ValueError):
            HealthMonitor(services=())


if __name__ == "__main__":
    unittest.main()
