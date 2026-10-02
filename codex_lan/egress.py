"""CODEX: Retryable loopback CONNECT gateway with direct and SOCKS5 egress routes."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import ipaddress
import os
import sqlite3
import threading
import time
from pathlib import Path
from urllib.parse import unquote, urlsplit

import aiohttp


DEFAULT_PROXY_URL = ""
MAX_HEADER_BYTES = 16 * 1024
MAX_EARLY_BYTES = 64 * 1024
PROBE_URL = "https://chatgpt.com/backend-api/codex/responses"
BYPASS_HOSTS = "localhost,127.0.0.1,::1,.localhost,.local,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,169.254.0.0/16,fc00::/7,fe80::/10"
FAILURES_BEFORE_SWITCH = 2
MAX_RETRY_DELAY = 30.0


class EgressError(OSError):
    """CODEX: Public-safe connection failure without target credentials or provider payloads."""

    def __init__(self, message, route=None):
        super().__init__(message)
        self.route = route if route in {"direct", "proxy"} else None


class RoutePreferenceStore:
    """CODEX: Small process-safe SQLite record of the last route that actually opened."""

    def __init__(self, path):
        self.path = Path(path).expanduser().absolute()
        self._lock = threading.RLock()
        self._available = False
        self._initialize()

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=5)
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    def _initialize(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if self.path.is_symlink():
                return
            with self._lock, self._connect() as connection:
                connection.execute(
                    """CREATE TABLE IF NOT EXISTS provider_route_state (
                           route_key TEXT PRIMARY KEY,
                           preferred_mode TEXT NOT NULL CHECK(preferred_mode IN ('direct','proxy')),
                           generation INTEGER NOT NULL DEFAULT 0,
                           last_success_at REAL,
                           direct_success_at REAL,
                           proxy_success_at REAL,
                           updated_at REAL NOT NULL
                       )"""
                )
                now = time.time()
                connection.execute(
                    """INSERT OR IGNORE INTO provider_route_state
                           (route_key, preferred_mode, generation, updated_at)
                       VALUES ('provider', 'direct', 0, ?)""",
                    (now,),
                )
            self.path.chmod(0o600)
            self._available = True
        except (OSError, sqlite3.Error):
            self._available = False

    def read(self):
        if not self._available:
            return {"preferred_mode": "direct", "generation": 0, "persistent": False}
        try:
            with self._lock, self._connect() as connection:
                row = connection.execute(
                    """SELECT preferred_mode, generation, last_success_at,
                              direct_success_at, proxy_success_at
                       FROM provider_route_state WHERE route_key = 'provider'"""
                ).fetchone()
            if row is None or row[0] not in {"direct", "proxy"}:
                raise ValueError
            return {"preferred_mode": row[0], "generation": max(0, int(row[1])),
                    "last_success_at": row[2], "direct_success_at": row[3],
                    "proxy_success_at": row[4], "persistent": True}
        except (OSError, sqlite3.Error, TypeError, ValueError, OverflowError):
            return {"preferred_mode": "direct", "generation": 0, "persistent": False}

    def record_success(self, mode):
        if mode not in {"direct", "proxy"} or not self._available:
            return self.read()
        now = time.time()
        try:
            with self._lock, self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                current = connection.execute(
                    """SELECT generation, direct_success_at, proxy_success_at
                       FROM provider_route_state WHERE route_key = 'provider'"""
                ).fetchone() or (0, None, None)
                direct_at, proxy_at = current[1], current[2]
                if mode == "direct":
                    direct_at = now
                else:
                    proxy_at = now
                connection.execute(
                    """INSERT INTO provider_route_state
                           (route_key, preferred_mode, generation, last_success_at,
                            direct_success_at, proxy_success_at, updated_at)
                       VALUES ('provider', ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(route_key) DO UPDATE SET
                           preferred_mode=excluded.preferred_mode,
                           generation=excluded.generation,
                           last_success_at=excluded.last_success_at,
                           direct_success_at=excluded.direct_success_at,
                           proxy_success_at=excluded.proxy_success_at,
                           updated_at=excluded.updated_at""",
                    (mode, max(0, int(current[0])) + 1, now, direct_at, proxy_at, now),
                )
            return self.read()
        except (OSError, sqlite3.Error, TypeError, ValueError, OverflowError):
            return self.read()


def _bypass(host):
    value = host.rstrip(".").lower()
    if value == "localhost" or value.endswith((".localhost", ".local")) or "." not in value and ":" not in value:
        return True
    try:
        address = ipaddress.ip_address(value)
        return address.is_loopback or address.is_private or address.is_link_local
    except ValueError:
        return False


class AdaptiveProxy:
    def __init__(self, proxy_url=None, on_status=None, *, open_connection=None,
                 allowed_ports=(443,), retry_delay=2.0, connect_timeout=10.0, probe_url=PROBE_URL,
                 route_state_path=None, route_store=None):
        # CODEX: Proxy addresses are installation-private settings, never a shared source default.
        if proxy_url is None:
            proxy_url = os.environ.get("CODEX_LAN_PROXY_URL", DEFAULT_PROXY_URL)
        if not isinstance(proxy_url, str):
            raise ValueError("The SOCKS5 proxy configuration is invalid")
        self._proxy_configured = bool(proxy_url)
        self._proxy_host = self._proxy_port = None
        self._username = self._password = b""
        if self._proxy_configured:
            try:
                parsed = urlsplit(proxy_url)
                if (parsed.scheme not in {"socks5", "socks5h"} or not parsed.hostname or not parsed.port
                        or parsed.path not in {"", "/"} or parsed.query or parsed.fragment):
                    raise ValueError
                self._proxy_host, self._proxy_port = parsed.hostname, parsed.port
                self._username = unquote(parsed.username or "").encode("utf-8")
                self._password = unquote(parsed.password or "").encode("utf-8")
                if len(self._username) > 255 or len(self._password) > 255 or parsed.password is not None and not self._username:
                    raise ValueError
            except (ValueError, TypeError, UnicodeError, AttributeError):
                raise ValueError("The SOCKS5 proxy configuration is invalid") from None
        self._on_status = on_status
        self._open_connection = open_connection or asyncio.open_connection
        self._allowed_ports = frozenset(allowed_ports)
        self._retry_delay = max(0.0, float(retry_delay))
        self._connect_timeout = max(0.05, float(connect_timeout))
        self._probe_url = probe_url
        self._route_store = route_store or (RoutePreferenceStore(route_state_path) if route_state_path else None)
        preference = self._read_preference()
        self._mode = preference["preferred_mode"]
        self._preferred_mode = self._mode
        self._preference_generation = preference["generation"]
        self._last_recorded_at = preference["last_success_at"]
        self._state = "connecting"
        self._attempt = 0
        self._switches = 0
        self._route_generation = 0
        self._failed_modes = set()
        self._failed_cycles = 0
        self._retry_at = 0.0
        self._updated_at = time.time()
        self._route_lock = asyncio.Lock()
        self._server = None
        self._tasks = set()
        self._closed = False
        self._url = None

    def _read_preference(self):
        value = self._route_store.read() if self._route_store is not None else {}
        mode = value.get("preferred_mode")
        generation = value.get("generation")
        return {"preferred_mode": mode if mode in {"direct", "proxy"} and (mode != "proxy" or self._proxy_configured) else "direct",
                "generation": generation if isinstance(generation, int) and generation >= 0 else 0,
                "last_success_at": value.get("last_success_at") if isinstance(value.get("last_success_at"), (int, float)) else None}

    @property
    def url(self):
        if self._url is None:
            raise RuntimeError("The connection gateway has not started")
        return self._url

    async def start(self):
        if self._closed:
            raise RuntimeError("The connection gateway is closed")
        if self._server is None:
            preference = self._read_preference()
            self._mode = self._preferred_mode = preference["preferred_mode"]
            self._preference_generation = preference["generation"]
            self._last_recorded_at = preference["last_success_at"]
            self._server = await asyncio.start_server(self._accept, "127.0.0.1", 0, limit=MAX_HEADER_BYTES)
            self._url = "http://127.0.0.1:" + str(self._server.sockets[0].getsockname()[1])
        return self

    def environment(self, base_env):
        result = {key: value for key, value in base_env.items()
                  if key.lower() not in {"http_proxy", "https_proxy", "all_proxy", "no_proxy"}}
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
            result[key] = self.url
        result["NO_PROXY"] = result["no_proxy"] = BYPASS_HOSTS
        return result

    def snapshot(self):
        return {"state": self._state, "mode": self._mode, "attempt": self._attempt,
                "attempt_limit": FAILURES_BEFORE_SWITCH, "preferred_mode": self._preferred_mode,
                "switches": self._switches, "retry_in_seconds": round(max(0.0, self._retry_at - time.monotonic()), 2),
                "updated_at": self._updated_at}

    async def _notify(self, scope):
        snapshot = self.snapshot()
        if scope is not None:
            snapshot["scope"] = scope
        if self._on_status is not None:
            with contextlib.suppress(Exception):
                result = self._on_status(snapshot)
                if inspect.isawaitable(result):
                    await result
        return snapshot

    async def report_failure(self, scope=None, message=None, *, route=None, immediate=False):
        failed_mode = route if route in {"direct", "proxy"} else self._mode
        async with self._route_lock:
            if failed_mode != self._mode:
                return self.snapshot()
            self._attempt += 1
            self._state = "reconnecting"
            should_switch = self._proxy_configured and (immediate or self._attempt >= FAILURES_BEFORE_SWITCH)
            if should_switch:
                self._failed_modes.add(failed_mode)
                preference = self._read_preference()
                self._preferred_mode = preference["preferred_mode"]
                self._preference_generation = preference["generation"]
                alternate = self._preferred_mode if self._preferred_mode != failed_mode else (
                    "proxy" if failed_mode == "direct" else "direct"
                )
                both_failed = alternate in self._failed_modes
                if both_failed:
                    self._failed_cycles += 1
                    self._failed_modes.clear()
                self._mode = alternate
                self._route_generation += 1
                self._attempt = 0
                self._switches += 1
                self._state = "switching"
                delay = min(MAX_RETRY_DELAY, self._retry_delay * (2 ** min(self._failed_cycles, 4))) if both_failed else 0
            else:
                delay = self._retry_delay
            self._retry_at = time.monotonic() + delay
            self._updated_at = time.time()
        return await self._notify(scope)

    async def report_success(self, scope=None):
        async with self._route_lock:
            self._attempt = 0
            self._state = "connected"
            self._failed_modes.clear()
            self._failed_cycles = 0
            self._retry_at = 0.0
            self._updated_at = time.time()
        return await self._notify(scope)

    async def _route_verified(self, mode, generation):
        """CODEX: Remember a route only after the remote endpoint sends data through it."""
        async with self._route_lock:
            if mode != self._mode or generation != self._route_generation:
                return
            now = time.time()
            should_record = (self._route_store is not None and
                             (mode != self._preferred_mode or self._last_recorded_at is None
                              or now - self._last_recorded_at >= 300))
            if should_record:
                value = self._route_store.record_success(mode)
                if value.get("persistent") is True:
                    self._preferred_mode = value.get("preferred_mode", mode)
                    self._preference_generation = value.get("generation", self._preference_generation)
                    self._last_recorded_at = value.get("last_success_at", now)
                else:
                    self._preferred_mode = mode
            else:
                self._preferred_mode = mode
            self._attempt = 0
            self._failed_modes.clear()
            self._failed_cycles = 0
            self._retry_at = 0.0

    async def _delay(self):
        delay = self._retry_at - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)
        if self._closed:
            raise asyncio.CancelledError

    async def _socks(self, host, port):
        reader, writer = await self._open_connection(self._proxy_host, self._proxy_port)
        try:
            writer.write(b"\x05\x02\x00\x02" if self._username else b"\x05\x01\x00")
            await writer.drain()
            version, method = await reader.readexactly(2)
            if version != 5 or method not in ({0, 2} if self._username else {0}):
                raise EgressError("The SOCKS5 proxy did not accept the connection")
            if method == 2:
                writer.write(b"\x01" + bytes([len(self._username)]) + self._username
                             + bytes([len(self._password)]) + self._password)
                await writer.drain()
                if await reader.readexactly(2) != b"\x01\x00":
                    raise EgressError("The SOCKS5 proxy did not accept authentication")
            try:
                address = ipaddress.ip_address(host)
                destination = bytes([1 if address.version == 4 else 4]) + address.packed
            except ValueError:
                encoded = host.encode("idna")
                if not 1 <= len(encoded) <= 255:
                    raise EgressError("The connection destination is invalid")
                destination = b"\x03" + bytes([len(encoded)]) + encoded
            writer.write(b"\x05\x01\x00" + destination + port.to_bytes(2, "big"))
            await writer.drain()
            version, result, reserved, kind = await reader.readexactly(4)
            if version != 5 or result != 0 or reserved != 0 or kind not in {1, 3, 4}:
                raise EgressError("The SOCKS5 proxy could not reach the destination")
            size = {1: 4, 4: 16}.get(kind)
            if kind == 3:
                size = (await reader.readexactly(1))[0]
            await reader.readexactly(size + 2)
            return reader, writer
        except BaseException:
            writer.close()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(writer.wait_closed(), 1.0)
            raise

    async def _open_target(self, host, port):
        bypass = _bypass(host)
        mode = "direct" if bypass else self._mode
        generation = self._route_generation

        async def open_route():
            if mode == "proxy":
                return await self._socks(host, port)
            return await self._open_connection(host, port)
        try:
            streams = await asyncio.wait_for(open_route(), self._connect_timeout)
        except (OSError, asyncio.TimeoutError, asyncio.IncompleteReadError, UnicodeError):
            raise EgressError("The connection route is temporarily unavailable", None if bypass else mode) from None
        return (*streams, mode, generation, not bypass)

    def _accept(self, reader, writer):
        task = asyncio.create_task(self._handle(reader, writer))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _dial(self, host, port):
        while not self._closed:
            await self._delay()
            try:
                return await self._open_target(host, port)
            except EgressError as exc:
                if exc.route is None:
                    self._retry_at = time.monotonic() + self._retry_delay
                else:
                    await self.report_failure(route=exc.route, immediate=True)
        raise asyncio.CancelledError

    async def _handle(self, reader, writer):
        remote = None
        dial = None
        jobs = []
        try:
            try:
                header = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), self._connect_timeout)
                if len(header) > MAX_HEADER_BYTES:
                    raise ValueError
                method, target, version = header.split(b"\r\n", 1)[0].decode("ascii").split()
                parsed = urlsplit("//" + target)
                if (method != "CONNECT" or version not in {"HTTP/1.0", "HTTP/1.1"} or not parsed.hostname
                        or parsed.port not in self._allowed_ports or parsed.username is not None or parsed.path
                        or parsed.query or parsed.fragment):
                    raise ValueError
                host, port = parsed.hostname, parsed.port
            except (ValueError, UnicodeError, asyncio.LimitOverrunError):
                writer.write(b"HTTP/1.1 400 Bad Request\r\nConnection: close\r\n\r\n")
                await writer.drain()
                return
            early = bytearray()

            async def watch_client():
                while True:
                    chunk = await reader.read(1024)
                    if not chunk:
                        return
                    early.extend(chunk)
                    if len(early) > MAX_EARLY_BYTES:
                        return

            watcher = asyncio.create_task(watch_client())
            dial = asyncio.create_task(self._dial(host, port))
            jobs.extend((watcher, dial))
            done, _pending = await asyncio.wait((watcher, dial), return_when=asyncio.FIRST_COMPLETED)
            if watcher in done:
                if dial.done() and not dial.cancelled() and dial.exception() is None:
                    _remote_reader, remote, _route_mode, _route_generation, _tracked_route = dial.result()
                return
            remote_reader, remote, route_mode, route_generation, tracked_route = dial.result()
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
            writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await writer.drain()
            upstream_seen = False

            async def relay_client():
                pending = bytes(early)
                while True:
                    if pending:
                        data, pending = pending, b""
                    else:
                        try:
                            data = await reader.read(65536)
                        except (OSError, asyncio.IncompleteReadError):
                            return "client_error"
                    if not data:
                        return "client_eof"
                    try:
                        remote.write(data)
                        await remote.drain()
                    except (OSError, asyncio.IncompleteReadError):
                        return "remote_error"

            async def relay_remote():
                nonlocal upstream_seen
                while True:
                    try:
                        data = await remote_reader.read(65536)
                    except (OSError, asyncio.IncompleteReadError):
                        return "remote_error"
                    if not data:
                        return "remote_eof"
                    if not upstream_seen:
                        upstream_seen = True
                        if tracked_route:
                            await self._route_verified(route_mode, route_generation)
                    try:
                        writer.write(data)
                        await writer.drain()
                    except (OSError, asyncio.IncompleteReadError):
                        return "client_error"

            outgoing = asyncio.create_task(relay_client())
            incoming = asyncio.create_task(relay_remote())
            jobs.extend((outgoing, incoming))
            await asyncio.wait((outgoing, incoming), return_when=asyncio.FIRST_COMPLETED)
            await asyncio.sleep(0)
            outcomes = [job.result() for job in (outgoing, incoming)
                        if job.done() and not job.cancelled()]
            remote_failed = any(outcome in {"remote_eof", "remote_error"} for outcome in outcomes)
            client_ended = reader.at_eof() or any(outcome in {"client_eof", "client_error"} for outcome in outcomes)
            if tracked_route and not upstream_seen and remote_failed and not client_ended:
                await self.report_failure(route=route_mode)
        except (OSError, asyncio.TimeoutError, asyncio.IncompleteReadError):
            pass
        finally:
            for job in jobs:
                if not job.done():
                    job.cancel()
            if jobs:
                await asyncio.gather(*jobs, return_exceptions=True)
            if remote is None and dial is not None and dial.done() and not dial.cancelled() and dial.exception() is None:
                _remote_reader, remote, _route_mode, _route_generation, _tracked_route = dial.result()
            for stream in (remote, writer):
                if stream is not None:
                    stream.close()
                    with contextlib.suppress(Exception):
                        await asyncio.wait_for(stream.wait_closed(), 1.0)

    async def _probe(self):
        timeout = aiohttp.ClientTimeout(total=self._connect_timeout + 5, sock_read=self._connect_timeout)
        async with aiohttp.ClientSession(timeout=timeout, trust_env=False) as session:
            async with session.head(self._probe_url, proxy=self.url, allow_redirects=False) as response:
                if response.status >= 500 or response.status == 407:
                    raise EgressError("The provider is temporarily unavailable")

    async def wait_ready(self, scope=None):
        await self.start()
        while not self._closed:
            await self._delay()
            try:
                await self._probe()
            except (OSError, asyncio.TimeoutError, aiohttp.ClientError):
                await self.report_failure(scope=scope)
            else:
                return await self.report_success(scope=scope)
        raise asyncio.CancelledError

    async def close(self):
        self._closed = True
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
        tasks = tuple(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def __aenter__(self):
        return await self.start()

    async def __aexit__(self, *_exc):
        await self.close()
