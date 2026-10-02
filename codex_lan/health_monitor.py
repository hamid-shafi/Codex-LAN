"""CODEX: Independently recover Codex LAN services when their health endpoint stalls."""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
import signal
import sqlite3
import subprocess
import threading
import time
from typing import Callable, Sequence
from urllib.request import Request, urlopen


LOGGER = logging.getLogger("codex_lan.health_monitor")
DEFAULT_ENDPOINT = "http://127.0.0.1:8765/api/health"
DEFAULT_MARKER = Path.home() / ".local/share/codex-lan/server/backend-update.maintenance"
DEFAULT_DATABASE = Path.home() / ".local/share/codex-lan/server/server.sqlite3"
DEFAULT_SERVICES = ("codex-lan-server.service",)
MAX_HEALTH_BYTES = 64 * 1024


def probe_health(endpoint: str, timeout: float, opener=urlopen) -> bool:
    """CODEX: Return true only for a bounded, valid health response whose ok flag is true."""
    request = Request(
        endpoint,
        headers={"Accept": "application/json", "Cache-Control": "no-store", "User-Agent": "codex-lan-health-monitor"},
    )
    with opener(request, timeout=timeout) as response:
        status = getattr(response, "status", None)
        if status is None:
            status = response.getcode()
        if status is not None and not 200 <= int(status) < 300:
            return False
        payload = response.read(MAX_HEALTH_BYTES + 1)
    if len(payload) > MAX_HEALTH_BYTES:
        return False
    value = json.loads(payload.decode("utf-8"))
    return isinstance(value, dict) and value.get("ok") is True


def restart_services(
    services: Sequence[str],
    *,
    runner=subprocess.run,
    timeout: float = 90.0,
) -> None:
    """CODEX: Restart the named user services as one systemd transaction."""
    runner(
        ["systemctl", "--user", "restart", *services],
        check=True,
        timeout=timeout,
    )


def active_run_count(database: Path, *, timeout: float = 1.0) -> int:
    """CODEX: Read active work independently before deciding whether recovery can proceed."""
    uri = database.expanduser().absolute().as_uri() + "?mode=ro"
    with sqlite3.connect(uri, uri=True, timeout=timeout) as connection:
        connection.execute("PRAGMA query_only = ON")
        row = connection.execute(
            "SELECT COUNT(*) FROM runs WHERE status IN ('queued', 'running')"
        ).fetchone()
    return int(row[0])


def latest_event_id(database: Path, *, timeout: float = 1.0) -> int:
    """CODEX: Read the indexed event high-water mark without loading conversation content."""
    uri = database.expanduser().absolute().as_uri() + "?mode=ro"
    with sqlite3.connect(uri, uri=True, timeout=timeout) as connection:
        connection.execute("PRAGMA query_only = ON")
        row = connection.execute("SELECT COALESCE(MAX(id), 0) FROM events").fetchone()
    return int(row[0])


class HealthMonitor:
    """CODEX: Count consecutive failures and recover services without restart storms."""

    def __init__(
        self,
        *,
        endpoint: str = DEFAULT_ENDPOINT,
        timeout: float = 5.0,
        interval: float = 15.0,
        failure_threshold: int = 4,
        cooldown: float = 300.0,
        active_stall_timeout: float = 60.0,
        unknown_state_timeout: float = 180.0,
        marker: Path = DEFAULT_MARKER,
        services: Sequence[str] = DEFAULT_SERVICES,
        probe: Callable[[str, float], bool] = probe_health,
        restart: Callable[[Sequence[str]], None] = restart_services,
        marker_exists: Callable[[Path], bool] = lambda path: path.exists(),
        active_runs: Callable[[], int] | None = None,
        active_progress: Callable[[], int] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        if (timeout <= 0 or interval <= 0 or failure_threshold <= 0
                or cooldown < 0 or active_stall_timeout <= 0 or unknown_state_timeout <= 0):
            raise ValueError("timeouts, interval and threshold must be positive; cooldown cannot be negative")
        if not services:
            raise ValueError("at least one service is required")
        self.endpoint = endpoint
        self.timeout = timeout
        self.interval = interval
        self.failure_threshold = failure_threshold
        self.cooldown = cooldown
        self.active_stall_timeout = active_stall_timeout
        self.unknown_state_timeout = unknown_state_timeout
        self.marker = marker
        self.services = tuple(services)
        self.probe = probe
        self.restart = restart
        self.marker_exists = marker_exists
        self.active_runs = active_runs or (lambda: active_run_count(DEFAULT_DATABASE))
        self.active_progress = active_progress or (lambda: latest_event_id(DEFAULT_DATABASE))
        self.monotonic = monotonic
        self.failures = 0
        self.last_restart_at: float | None = None
        self.maintenance_active = False
        self.stalled_since: float | None = None
        self.progress_token: tuple[int, int] | None = None
        self.unknown_since: float | None = None

    def _reset_stall(self, *, clear_unknown=True) -> None:
        self.stalled_since = None
        self.progress_token = None
        if clear_unknown:
            self.unknown_since = None

    def _in_maintenance(self) -> bool:
        try:
            active = bool(self.marker_exists(self.marker))
        except OSError:
            LOGGER.exception("maintenance_marker_check_failed marker=%s", self.marker)
            active = False
        if active:
            self.failures = 0
            self._reset_stall()
            if not self.maintenance_active:
                LOGGER.info("health_recovery_paused_for_backend_update marker=%s", self.marker)
            self.maintenance_active = True
            return True
        if self.maintenance_active:
            LOGGER.info("health_recovery_resumed_after_backend_update marker=%s", self.marker)
        self.maintenance_active = False
        return False

    def check_once(self) -> str:
        """CODEX: Perform one probe and, only when policy permits, one recovery attempt."""
        if self._in_maintenance():
            return "maintenance"
        try:
            healthy = bool(self.probe(self.endpoint, self.timeout))
        except Exception as exc:
            healthy = False
            LOGGER.warning("health_probe_failed endpoint=%s error=%s", self.endpoint, exc)
        if healthy:
            if self.failures:
                LOGGER.info("health_probe_recovered endpoint=%s previous_failures=%d", self.endpoint, self.failures)
            self.failures = 0
            self._reset_stall()
            return "healthy"

        self.failures = min(self.failures + 1, self.failure_threshold)
        LOGGER.warning(
            "health_probe_unhealthy endpoint=%s consecutive_failures=%d threshold=%d",
            self.endpoint,
            self.failures,
            self.failure_threshold,
        )
        if self.failures < self.failure_threshold:
            return "failed"
        if self._in_maintenance():
            return "maintenance"

        now = self.monotonic()
        try:
            active = self.active_runs()
            if isinstance(active, bool) or not isinstance(active, int) or active < 0:
                raise ValueError("active run count must be a non-negative integer")
            if active:
                progress = self.active_progress()
                if isinstance(progress, bool) or not isinstance(progress, int) or progress < 0:
                    raise ValueError("event progress must be a non-negative integer")
        except Exception:
            LOGGER.exception("health_active_state_unavailable")
            self._reset_stall(clear_unknown=False)
            if self.unknown_since is None:
                self.unknown_since = now
            unknown_for = now - self.unknown_since
            if unknown_for < self.unknown_state_timeout:
                LOGGER.warning("health_restart_suppressed_unknown_state seconds=%.1f", unknown_for)
                return "active_state_unknown"
            LOGGER.error("health_unknown_state_deadline_reached seconds=%.1f", unknown_for)
        else:
            self.unknown_since = None
            if active:
                token = (active, progress)
                if self.progress_token != token or self.stalled_since is None:
                    self.progress_token = token
                    self.stalled_since = now
                stalled_for = now - self.stalled_since
                if stalled_for < self.active_stall_timeout:
                    LOGGER.warning("health_restart_suppressed_active_runs count=%d stalled_seconds=%.1f",
                                   active, stalled_for)
                    return "active_runs"
                LOGGER.error("health_active_runs_stalled count=%d stalled_seconds=%.1f", active, stalled_for)
            else:
                self._reset_stall()

        if self.last_restart_at is not None:
            remaining = self.cooldown - (now - self.last_restart_at)
            if remaining > 0:
                LOGGER.warning("health_restart_suppressed_by_cooldown remaining_seconds=%.1f", remaining)
                self.failures = 0
                return "cooldown"

        LOGGER.error("health_failure_threshold_reached restarting_services=%s", ",".join(self.services))
        try:
            self.restart(self.services)
        except Exception:
            LOGGER.exception("health_service_restart_failed services=%s", ",".join(self.services))
            outcome = "restart_failed"
        else:
            LOGGER.info("health_services_restarted services=%s", ",".join(self.services))
            outcome = "restarted"
        self.last_restart_at = now
        self.failures = 0
        self._reset_stall()
        return outcome

    def run(self, stop_event: threading.Event) -> None:
        """CODEX: Probe until SIGTERM or SIGINT sets the supplied event."""
        LOGGER.info(
            "health_monitor_started endpoint=%s interval_seconds=%.1f timeout_seconds=%.1f threshold=%d "
            "cooldown_seconds=%.1f active_stall_seconds=%.1f unknown_state_seconds=%.1f",
            self.endpoint,
            self.interval,
            self.timeout,
            self.failure_threshold,
            self.cooldown,
            self.active_stall_timeout,
            self.unknown_state_timeout,
        )
        while not stop_event.is_set():
            self.check_once()
            stop_event.wait(self.interval)
        LOGGER.info("health_monitor_stopped")


def parser() -> argparse.ArgumentParser:
    """CODEX: Build the standalone monitor command-line interface."""
    value = argparse.ArgumentParser(description="Monitor and recover Codex LAN user services")
    value.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    value.add_argument("--timeout", type=float, default=5.0)
    value.add_argument("--interval", type=float, default=15.0)
    value.add_argument("--failure-threshold", type=int, default=4)
    value.add_argument("--cooldown", type=float, default=300.0)
    value.add_argument("--active-stall-timeout", type=float, default=60.0,
                       help="Allow recovery after this many unhealthy seconds without persisted event progress")
    value.add_argument("--unknown-state-timeout", type=float, default=180.0,
                       help="Bound recovery deferral when health and active-work metadata both stay unavailable")
    value.add_argument("--marker", type=Path, default=DEFAULT_MARKER)
    value.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    value.add_argument("--service", action="append", dest="services")
    value.add_argument("--log-level", choices=("DEBUG", "INFO", "WARNING", "ERROR"), default="INFO")
    return value


def main(argv: Sequence[str] | None = None) -> int:
    """CODEX: Run the monitor with interruptible signal handling and structured logs."""
    args = parser().parse_args(argv)
    logging.basicConfig(level=getattr(logging, args.log_level), format="%(asctime)s %(levelname)s %(name)s %(message)s")
    stop_event = threading.Event()

    def stop(signum, frame):
        """CODEX: Convert process termination signals into an interruptible shutdown."""
        LOGGER.info("health_monitor_stop_requested signal=%s", signum)
        stop_event.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    monitor = HealthMonitor(
        endpoint=args.endpoint,
        timeout=args.timeout,
        interval=args.interval,
        failure_threshold=args.failure_threshold,
        cooldown=args.cooldown,
        active_stall_timeout=args.active_stall_timeout,
        unknown_state_timeout=args.unknown_state_timeout,
        marker=args.marker,
        services=args.services or DEFAULT_SERVICES,
        active_runs=lambda: active_run_count(args.database),
        active_progress=lambda: latest_event_id(args.database),
    )
    monitor.run(stop_event)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
