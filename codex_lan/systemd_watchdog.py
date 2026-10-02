"""CODEX: Keep the server observable to systemd without adding a dependency."""

from __future__ import annotations

import asyncio
import faulthandler
import os
import socket
from collections.abc import Mapping


async def _loop_diagnostics(interval: float, timeout: float, *, sleeper=None,
                            handler=faulthandler) -> None:
    """CODEX: Reset a native stack-dump timer only when the event loop is making progress."""
    sleeper = asyncio.sleep if sleeper is None else sleeper
    try:
        while True:
            await sleeper(interval)
            handler.dump_traceback_later(timeout, repeat=True)
    finally:
        handler.cancel_dump_traceback_later()


def start_loop_diagnostics(*, timeout: float = 30.0, interval: float = 5.0,
                           handler=faulthandler):
    """CODEX: Capture all Python stacks on a stall without killing or signalling the server."""
    if timeout <= 0 or interval <= 0 or interval >= timeout:
        raise ValueError("diagnostic interval must be positive and shorter than its timeout")
    handler.dump_traceback_later(timeout, repeat=True)
    return asyncio.create_task(
        _loop_diagnostics(interval, timeout, handler=handler),
        name="codex-lan-loop-diagnostics",
    )


async def stop_loop_diagnostics(task, *, handler=faulthandler):
    """CODEX: Disarm the native timer even if startup failed before its task first ran."""
    task.cancel()
    try:
        await asyncio.gather(task, return_exceptions=True)
    finally:
        handler.cancel_dump_traceback_later()


def _notify_address(environment: Mapping[str, str]) -> str | None:
    value = environment.get("NOTIFY_SOCKET", "")
    if value.startswith("@") and len(value) > 1:
        return "\0" + value[1:]
    if value.startswith("/"):
        return value
    return None


def notify_systemd(message: str, *, environment: Mapping[str, str] | None = None,
                   socket_factory=None) -> bool:
    environment = os.environ if environment is None else environment
    address = _notify_address(environment)
    if address is None or not isinstance(message, str) or not message or "\0" in message:
        return False
    factory = socket.socket if socket_factory is None else socket_factory
    channel = None
    try:
        channel = factory(socket.AF_UNIX, socket.SOCK_DGRAM)
        channel.connect(address)
        channel.sendall(message.encode("utf-8"))
        return True
    except (OSError, UnicodeError, ValueError, TypeError):
        return False
    finally:
        if channel is not None:
            try:
                channel.close()
            except OSError:
                pass


def watchdog_interval(*, environment: Mapping[str, str] | None = None,
                      process_id: int | None = None) -> float | None:
    environment = os.environ if environment is None else environment
    if _notify_address(environment) is None:
        return None
    expected_pid = environment.get("WATCHDOG_PID", "").strip()
    current_pid = os.getpid() if process_id is None else int(process_id)
    if expected_pid:
        try:
            if int(expected_pid) != current_pid:
                return None
        except ValueError:
            return None
    try:
        microseconds = int(environment.get("WATCHDOG_USEC", "0"))
    except (TypeError, ValueError):
        return None
    if microseconds <= 0:
        return None
    return microseconds / 2_000_000


async def _watchdog_loop(interval: float, *, environment: Mapping[str, str] | None = None,
                         sleeper=None, notifier=None) -> None:
    sleeper = asyncio.sleep if sleeper is None else sleeper
    notifier = notify_systemd if notifier is None else notifier
    while True:
        await sleeper(interval)
        notifier("WATCHDOG=1", environment=environment)


def start_systemd_watchdog(*, environment: Mapping[str, str] | None = None):
    notify_systemd("READY=1", environment=environment)
    interval = watchdog_interval(environment=environment)
    if interval is None:
        return None
    return asyncio.create_task(
        _watchdog_loop(interval, environment=environment),
        name="codex-lan-systemd-watchdog",
    )
