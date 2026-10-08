"""CODEX: Read-only provider quota windows, independent of application token accounting."""

from __future__ import annotations

import asyncio
import copy
import json
import math
import os
import re
import time
from itertools import islice

from .egress import AdaptiveProxy


CACHE_SECONDS = 60.0
RETRY_CACHE_SECONDS = 10.0
FORCED_REFRESH_SECONDS = 5.0
SPAWN_TIMEOUT = 3.0
REQUEST_TIMEOUT = 22.0
PROCESS_STOP_TIMEOUT = 2.0
MAX_PACKET_BYTES = 1024 * 1024
MAX_BUCKETS = 16
UNAVAILABLE = "Provider quota information is currently unavailable."
STALE = "Provider quota could not be refreshed; showing the last successful reading."
RESET_PENDING = "A quota reset time has passed; the provider has not returned a refreshed window yet."


def _number(value, minimum=0, maximum=None):
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        return None
    try:
        valid = math.isfinite(value) and value >= minimum and (maximum is None or value <= maximum)
    except (ValueError, OverflowError):
        return None
    return value if valid else None


def _label(value):
    if not isinstance(value, str) or len(value) > 120:
        return None
    value = " ".join(value.split())
    if (not value or any(ord(char) < 32 for char in value)
            or re.search(r"(?i)(?:bearer\s|\bsk-|\beyJ|authorization|password|access.?token|refresh.?token|@)", value)):
        return None
    return value


def _bucket_id(value):
    if (not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}", value)
            or re.match(r"(?i)(?:sk-|eyJ|org-|acct_|account_|user_)", value)):
        return None
    return value


def normalize_rate_limits(payload, fetched_at=None):
    """CODEX: Preserve only documented quota fields; absent readings are never zero percent."""
    buckets = {}
    if isinstance(payload, dict):
        legacy = payload.get("rateLimits")
        if isinstance(legacy, dict):
            identifier = _bucket_id(legacy.get("limitId")) or "codex"
            buckets[identifier] = legacy
        multiple = payload.get("rateLimitsByLimitId")
        if isinstance(multiple, dict):
            for key, value in islice(multiple.items(), MAX_BUCKETS):
                if not isinstance(value, dict):
                    continue
                identifier = _bucket_id(value.get("limitId")) or _bucket_id(key)
                if identifier:
                    buckets[identifier] = value
    windows = []
    for identifier, bucket in islice(buckets.items(), MAX_BUCKETS):
        for kind in ("primary", "secondary"):
            window = bucket.get(kind)
            if not isinstance(window, dict):
                continue
            percent = _number(window.get("usedPercent"), maximum=100)
            if percent is None:
                continue
            minutes = _number(window.get("windowDurationMins"), minimum=1, maximum=52560000)
            reset = _number(window.get("resetsAt"), maximum=253402300799)
            windows.append({"limit_id": identifier, "limit_name": _label(bucket.get("limitName")),
                            "kind": kind, "used_percent": percent, "window_minutes": minutes, "resets_at": reset})
    result = {"available": bool(windows), "windows": windows,
              "fetched_at": time.time() if fetched_at is None else fetched_at, "stale": False}
    if not windows:
        result["error"] = UNAVAILABLE
    return result


class ProviderUsage:
    def __init__(self, codex_home, codex, route_state_path=None):
        self.home, self.codex = str(codex_home), str(codex)
        self.lock = asyncio.Lock()
        self.cached = None
        self.expires_at = 0.0
        self.last_attempt_at = None
        self.last_good = None
        self.gateway = AdaptiveProxy(connect_timeout=2.0, retry_delay=1.0,
                                     route_state_path=route_state_path)

    async def connection_environment(self, base_env, scope="usage"):
        # CODEX: The real RPC determines connectivity; an anonymous HEAD can fail independently.
        await asyncio.wait_for(self.gateway.start(), timeout=SPAWN_TIMEOUT)
        return self.gateway.environment(base_env)

    async def close(self):
        await self.gateway.close()

    @staticmethod
    def _reset_passed(snapshot, now):
        return any(window["resets_at"] is not None and window["resets_at"] <= now
                   and not window.get("stale")
                   for window in snapshot.get("windows", []))

    async def read(self, force=False):
        async with self.lock:
            now = time.monotonic()
            may_force = force and (self.last_attempt_at is None or now - self.last_attempt_at >= FORCED_REFRESH_SECONDS)
            if (not may_force and self.cached is not None and now < self.expires_at
                    and (self.cached.get("stale") or not self._reset_passed(self.cached, time.time()))):
                return copy.deepcopy(self.cached)
            self.last_attempt_at = now
            try:
                # CODEX: Quota polling must not hold the separate live-worker auth-refresh lock.
                payload = await self._fetch()
                result = normalize_rate_limits(payload)
                for window in result["windows"]:
                    if window["resets_at"] is not None and window["resets_at"] <= time.time():
                        window["stale"] = True
                # CODEX: An expired ancillary limit must not hide a fresh weekly allowance.
                if result["windows"] and all(window.get("stale") for window in result["windows"]):
                    result.update(stale=True, error=RESET_PENDING)
                self.last_good = copy.deepcopy(result) if result["available"] else None
            except (OSError, ValueError, TypeError, RuntimeError, asyncio.TimeoutError):
                if self.last_good is not None:
                    result = copy.deepcopy(self.last_good)
                    result.update(stale=True, error=STALE)
                else:
                    result = {"available": False, "windows": [], "fetched_at": None,
                              "stale": False, "error": UNAVAILABLE}
            self.cached = result
            self.last_attempt_at = time.monotonic()
            retry = result["stale"] or not result["available"] or any(window.get("stale") for window in result["windows"])
            self.expires_at = time.monotonic() + (RETRY_CACHE_SECONDS if retry else CACHE_SECONDS)
            if not result["stale"]:
                future_resets = [window["resets_at"] for window in result["windows"]
                                 if window["resets_at"] is not None and window["resets_at"] > time.time()]
                if future_resets:
                    self.expires_at = min(self.expires_at, time.monotonic() + max(0, min(future_resets) - time.time()))
            return copy.deepcopy(result)

    async def _fetch(self):
        process = None
        try:
            # CODEX: Keep route selection across short-lived probes, including a cancelled refresh.
            env = await self.connection_environment(dict(os.environ, CODEX_HOME=self.home))
            process = await asyncio.wait_for(asyncio.create_subprocess_exec(
                self.codex, "app-server", "--stdio", env=env,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL, limit=MAX_PACKET_BYTES,
            ), timeout=SPAWN_TIMEOUT)

            async def request(identifier, method, params=None):
                packet = {"id": identifier, "method": method}
                if params is not None:
                    packet["params"] = params
                process.stdin.write((json.dumps(packet) + "\n").encode())
                await process.stdin.drain()
                for _attempt in range(128):
                    line = await process.stdout.readline()
                    if not line or len(line) > MAX_PACKET_BYTES:
                        raise RuntimeError(UNAVAILABLE)
                    response = json.loads(line)
                    if not isinstance(response, dict) or response.get("method"):
                        continue
                    if response.get("id") == identifier:
                        if "error" in response:
                            raise RuntimeError(UNAVAILABLE)
                        result = response.get("result")
                        if not isinstance(result, dict):
                            raise RuntimeError(UNAVAILABLE)
                        return result
                raise RuntimeError(UNAVAILABLE)

            async def handshake():
                await request(1, "initialize", {"clientInfo": {"name": "codex_lan_quota", "version": "0.1.0"}})
                process.stdin.write(b'{"method":"initialized"}\n')
                await process.stdin.drain()
                return await request(2, "account/rateLimits/read")
            try:
                result = await asyncio.wait_for(handshake(), timeout=REQUEST_TIMEOUT)
                await self.gateway.report_success(scope="usage")
                return result
            except (OSError, asyncio.TimeoutError):
                await self.gateway.report_failure(scope="usage")
                raise
        finally:
            if process is not None and process.returncode is None:
                try:
                    process.terminate()
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(process.wait(), timeout=PROCESS_STOP_TIMEOUT)
                except asyncio.TimeoutError:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                    try:
                        await asyncio.wait_for(process.wait(), timeout=PROCESS_STOP_TIMEOUT)
                    except asyncio.TimeoutError:
                        pass
