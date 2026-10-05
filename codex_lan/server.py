"""CODEX: Loopback-only central web service, intended for SSH port forwarding."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import fcntl
import hashlib
import json
import logging
import os
import shutil
import signal
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from aiohttp import WSMsgType, web
from aiohttp.helpers import content_disposition_header

from . import __version__
from .auth import AuthProvider
from .client_access import ClientAccess
from .companions import DETAILS_PROMPT, DraftConflictError
from .host_worker import HostWorkerSession
from .host_setup import HostSetup
from .media import MAX_USER_FILE_BYTES, attachment_mime, attachment_name
from .network import load_network, ssl_context
from .provider_usage import CACHE_SECONDS as PROVIDER_SAMPLE_SECONDS
from .provider_usage import RETRY_CACHE_SECONDS as PROVIDER_RETRY_SECONDS
from .request_keys import DEVICE
from .runtime import LIVE, MODEL_REFRESH_SECONDS, Peer, Runtime, SteeringConflict
from .store import DeviceBusyError, RoomBusyError, RoomReadOnlyError, Store
from .systemd_watchdog import start_loop_diagnostics, start_systemd_watchdog, stop_loop_diagnostics
from .transport import (
    EXPECTED_WEBSOCKET_CLOSE_CODES,
    LAN_WEBSOCKET_HEARTBEAT_SECONDS,
    device_id_suffix,
    websocket_diagnostic,
)
from .usage_estimator import supports_fast


MAX_IMAGE = MAX_USER_FILE_BYTES
MAX_PARALLEL_UPLOADS = 8
MAX_DEVICE_UPLOADS = 4
ATTACHMENT_GC_AGE_SECONDS = 7 * 24 * 60 * 60
ATTACHMENT_ORPHAN_GRACE_SECONDS = 60 * 60
ATTACHMENT_GC_INTERVAL_SECONDS = 60 * 60
LOGIN_BODY_MAX = 4096
JSON_BODY_MAX = 5 * 1024 * 1024
EXECUTOR_AUTH_INTERVAL = 10
COOKIE = "codex_lan_session"
SERVICE = web.AppKey("service", object)
LOGGER = logging.getLogger(__name__)


def backend_source_fingerprint():
    """CODEX: Identify the recovery modules loaded at startup, even without a version bump."""
    root = Path(__file__).parent
    digest = hashlib.sha256()
    for name in ("activity.py", "health_monitor.py", "server.py", "systemd_watchdog.py"):
        digest.update(name.encode("ascii") + b"\0" + (root / name).read_bytes() + b"\0")
    return digest.hexdigest()


def log_transport_close(role, device_id, websocket, failure=None):
    diagnostic = websocket_diagnostic(websocket, failure)
    log = (LOGGER.info if diagnostic["close_code"] in EXPECTED_WEBSOCKET_CLOSE_CODES
           and diagnostic["websocket_error"] == "none" and failure is None else LOGGER.warning)
    log(
        "transport_closed role=%s device_suffix=%s close_code=%s websocket_error=%s failure_type=%s",
        role, device_id_suffix(device_id), diagnostic["close_code"] or "none",
        diagnostic["websocket_error"], diagnostic["failure_type"],
    )


def provider_sample_interval(provider):
    windows = provider.get("windows") if isinstance(provider, dict) else None
    retry = (not isinstance(provider, dict) or provider.get("available") is not True
             or provider.get("stale") is True
             or isinstance(windows, list) and any(
                 isinstance(window, dict) and window.get("stale") is True for window in windows
             ))
    return PROVIDER_RETRY_SECONDS if retry else PROVIDER_SAMPLE_SECONDS


def token_from(request):
    header = request.headers.get("Authorization", "")
    if header.startswith("Bearer "):
        return header[7:].strip()
    return request.cookies.get(COOKIE, "")


def credential_from(request):
    header = request.headers.get("Authorization", "")
    if header:
        return ("device", header[7:].strip()) if header.startswith("Bearer ") else ("invalid", "")
    return "browser", request.cookies.get(COOKIE, "")


async def body(request):
    return await limited_body(request, JSON_BODY_MAX)


async def limited_body(request, maximum):
    length = request.content_length
    if length is not None and length > maximum:
        raise web.HTTPRequestEntityTooLarge(max_size=maximum, actual_size=length)
    payload = bytearray()
    async for chunk in request.content.iter_chunked(min(maximum, 1024)):
        payload.extend(chunk)
        if len(payload) > maximum:
            raise web.HTTPRequestEntityTooLarge(max_size=maximum, actual_size=len(payload))
    value = json.loads(payload)
    if not isinstance(value, dict):
        raise ValueError("Expected a JSON object")
    return value


@web.middleware
async def guard(request, handler):
    service = request.app[SERVICE]
    legacy_browser_device = None
    try:
        host = urlsplit("http://" + request.host).hostname
        local = host in {"localhost", "127.0.0.1", "::1"}
        lan = service.network and request.secure and f"{request.scheme}://{request.host}" == service.network["public_origin"]
        if not local and not lan:
            raise web.HTTPForbidden(text="Use the configured HTTPS address or the local tunnel URL.")
        origin = request.headers.get("Origin")
        if origin and origin != f"{request.scheme}://{request.host}":
            raise web.HTTPForbidden(text="Cross-origin requests are not allowed.")
        if request.headers.get("Sec-Fetch-Site") == "cross-site":
            raise web.HTTPForbidden(text="Cross-site requests are not allowed.")
        public_bootstrap = (request.path == "/api/client/bootstrap/installer"
                            or request.path.startswith("/api/client/bootstrap/installer/"))
        if request.path.startswith("/api/") and request.path not in {
                "/api/enroll", "/api/session", "/api/health", "/api/client/claim",
        } and not public_bootstrap:
            credential_kind, credential = credential_from(request)
            if credential_kind == "device":
                request[DEVICE] = service.store.authenticate(credential, allow_pending=request.path == "/api/me")
            elif credential_kind == "browser":
                request[DEVICE], legacy_browser = service.store.authenticate_browser_cookie(
                    credential, allow_pending=request.path == "/api/me"
                )
                if legacy_browser:
                    legacy_browser_device = request[DEVICE]["id"]
            else:
                raise PermissionError("Invalid authorization header")
        response = await handler(request)
    except DraftConflictError as exc:
        response = web.json_response({"error": str(exc), "draft_conflict": True}, status=409)
    except RoomReadOnlyError as exc:
        response = web.json_response({"error": str(exc), "read_only": True}, status=409)
    except RoomBusyError as exc:
        response = web.json_response({"error": str(exc), "busy": True}, status=409)
    except PermissionError as exc:
        response = web.json_response({"error": str(exc)}, status=403)
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        response = web.json_response({"error": str(exc)[:500]}, status=400)
    except web.HTTPException as exc:
        response = web.json_response({"error": exc.text}, status=exc.status)
        if exc.headers and exc.headers.get("Retry-After"):
            response.headers["Retry-After"] = exc.headers["Retry-After"]
    except (ConnectionError, asyncio.TimeoutError, RuntimeError):
        response = web.json_response({"error": "The local worker or server sign-in is unavailable. Check its status and retry."}, status=503)
    if (legacy_browser_device and not response.prepared and response.status < 400
            and COOKIE not in response.cookies):
        try:
            session_token, lifetime = service.store.create_browser_session(legacy_browser_device, remember=True)
            response.set_cookie(COOKIE, session_token, httponly=True, samesite="Strict", secure=request.secure,
                                max_age=lifetime, path="/")
        except PermissionError:
            pass
    if not response.prepared:
        embedded_document = request.path in {"/", "/index.html"}
        response.headers.update({
            "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer", "X-Frame-Options": "SAMEORIGIN" if embedded_document else "DENY",
            "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' blob: data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-src 'self'; frame-ancestors "
                                       + ("'self'" if embedded_document else "'none'") + "; form-action 'self'",
        })
    return response


class Service:
    def __init__(self, data_dir, auth=None, *, batch_events=False):
        self.data_dir = Path(data_dir)
        self.backend_build = backend_source_fingerprint()
        self.route_state_path = self.data_dir.parent / "egress.sqlite3"
        self.store = Store(data_dir)
        self.runtime = Runtime(
            self.store,
            auth or AuthProvider(route_state_path=self.route_state_path),
            batch_events=batch_events,
        )
        self.network = load_network(data_dir)
        self.host_setup = HostSetup(self)
        self.client_access = ClientAccess(self, data_dir)
        self.backend_update_marker = self.data_dir / "backend-update.maintenance"
        self.upload_dir = Path(data_dir) / "uploads"
        self.upload_dir.mkdir(mode=0o700, exist_ok=True)
        self.active_uploads = 0
        self.device_uploads = {}
        self.enrollments = []
        self.connecting = set()
        self.remote_sessions = {}
        self.usage_sampler = {"running": False, "last_success": None, "last_error": None,
                              "last_error_at": None, "consecutive_failures": 0}

    def message(self, device_id, message):
        value = dict(message)
        value["attachments"] = [self.attachment(self.store.get_attachment(device_id, key))
                                for key in value.get("attachment_ids", [])]
        if value.get("role") == "assistant" and value.get("run_id"):
            self.store.get_run(device_id, value["run_id"])
            value.update(self.runtime.activity_snapshot(value["run_id"]))
        return value

    @staticmethod
    def attachment(value):
        return {key: value[key] for key in ("id", "name", "mime", "size")} | {"url": "/api/attachments/" + value["id"]}

    def cleanup_attachment_files(self, paths, scope):
        """CODEX: Unlink committed attachment deletions only inside the managed upload root."""
        upload_root = self.upload_dir.resolve()
        for value in paths:
            candidate = Path(value)
            try:
                parent = candidate.parent.resolve()
                if (parent == upload_root or parent.is_relative_to(upload_root)) and (
                    candidate.is_file() or candidate.is_symlink()
                ):
                    candidate.unlink(missing_ok=True)
            except OSError:
                LOGGER.exception("attachment_file_cleanup_failed scope=%s", scope)

    def cleanup_deleted_room_runtime(self, result):
        """CODEX: Forget deleted runs and native-thread state after the database commit succeeds."""
        for run_id in result.get("run_ids", []):
            self.runtime.activity_cache.pop(run_id, None)
        for thread in result.get("threads", []):
            key = (thread["owner_id"], thread["thread_id"])
            self.runtime.by_thread.pop(key, None)
            self.runtime.thread_totals.pop(key, None)

    def reconcile_attachments(self, now=None):
        """CODEX: Reclaim stale rows and UUID-named orphan files while preserving every live reference."""
        observed_at = time.time() if now is None else float(now)
        stale_paths = self.store.prune_unreferenced_attachments(
            observed_at - ATTACHMENT_GC_AGE_SECONDS
        )
        self.cleanup_attachment_files(stale_paths, "stale")

        upload_root = self.upload_dir.resolve()
        known_names = set()
        for value in self.store.attachment_paths():
            candidate = Path(value)
            try:
                if candidate.parent.resolve() == upload_root:
                    known_names.add(candidate.name)
            except OSError:
                continue

        orphan_count = 0
        try:
            candidates = list(self.upload_dir.iterdir())
        except OSError:
            LOGGER.exception("attachment_orphan_scan_failed")
            candidates = []
        for candidate in candidates:
            if candidate.name in known_names:
                continue
            try:
                identifier = uuid.UUID(candidate.name)
            except ValueError:
                continue
            try:
                canonical_names = {identifier.hex, str(identifier)}
                if (
                    candidate.name.lower() not in canonical_names
                    or candidate.parent.resolve() != upload_root
                ):
                    continue
                metadata = candidate.lstat()
                if metadata.st_mtime > observed_at - ATTACHMENT_ORPHAN_GRACE_SECONDS:
                    continue
                if not candidate.is_file() and not candidate.is_symlink():
                    continue
                candidate.unlink(missing_ok=True)
                orphan_count += 1
            except OSError:
                LOGGER.exception("attachment_orphan_cleanup_failed")
        if stale_paths or orphan_count:
            LOGGER.info(
                "attachment_gc_complete stale_rows=%s orphan_files=%s",
                len(stale_paths), orphan_count,
            )
        return {"stale_rows": len(stale_paths), "orphan_files": orphan_count}

    async def attachment_gc_loop(self):
        """CODEX: Retry managed attachment reconciliation so unlink failures cannot leak forever."""
        while True:
            await asyncio.sleep(ATTACHMENT_GC_INTERVAL_SECONDS)
            try:
                self.reconcile_attachments()
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception("attachment_gc_failed")

    async def health(self, request):
        sampler = dict(self.usage_sampler)
        sampler["enabled"] = callable(getattr(self.runtime.auth, "rate_limits", None))
        if not sampler["enabled"]:
            sampler["status"] = "disabled"
        elif sampler["consecutive_failures"]:
            sampler["status"] = "degraded"
        elif sampler["last_success"] is not None:
            sampler["status"] = "healthy"
        else:
            sampler["status"] = "starting" if sampler["running"] else "stopped"
        return web.json_response({"ok": True, "version": __version__, "backend_build": self.backend_build,
                                  "activity_logs": True, "room_indicators": True, "room_companions": True,
                                  "steering": True, "client_access": True, "usage_dashboard": True, "connection_recovery": True,
                                  "centralized_execution": True,
                                  "backend_update": "verifying" if self.backend_update_marker.exists() else "ready",
                                  "usage_sampler": sampler,
                                  "public_origin": self.network["public_origin"] if self.network else None})

    def require_run_admission(self):
        if self.backend_update_marker.exists():
            raise web.HTTPServiceUnavailable(
                text="A server update is being verified. Your message was not submitted; retry shortly.",
                headers={"Retry-After": "2"},
            )

    async def enroll(self, request):
        now = time.monotonic()
        self.enrollments = [stamp for stamp in self.enrollments if now - stamp < 60]
        if len(self.enrollments) >= 20:
            raise web.HTTPTooManyRequests(text="Too many enrollment attempts. Try again in a minute.")
        # Reserve the attempt before reading: concurrent/invalid bodies cannot bypass the cap.
        self.enrollments.append(now)
        data = await body(request)
        enrolled = self.store.enroll(data.get("name", "Ubuntu client"))
        session_token, lifetime = self.store.create_browser_session(enrolled["device"]["id"], remember=True)
        response = web.json_response(enrolled, status=201)
        response.set_cookie(COOKIE, session_token, httponly=True, samesite="Strict", secure=request.secure,
                            max_age=lifetime, path="/")
        return response

    def _login_source(self, request):
        peer = request.transport.get_extra_info("peername") if request.transport else None
        return str(peer[0]) if isinstance(peer, (tuple, list)) and peer else "unknown"

    def _check_login_rate(self, request):
        attempt_id = self.store.begin_login_attempt(self._login_source(request))
        if attempt_id is None:
            raise web.HTTPTooManyRequests(text="Too many sign-in attempts. Wait a minute and try again.",
                                          headers={"Retry-After": "60"})
        return attempt_id

    async def session(self, request):
        attempt_id = self._check_login_rate(request)
        successful = False
        try:
            data = await limited_body(request, LOGIN_BODY_MAX)
            try:
                if "token" in data:
                    device = self.store.authenticate_primary_token(data.get("token", ""), allow_pending=True)
                else:
                    device = self.store.authenticate_password(data.get("login_name"), data.get("password"))
            except (PermissionError, ValueError, TypeError):
                raise PermissionError("Sign-in details are incorrect") from None
            remember = data.get("remember", "token" in data)
            if not isinstance(remember, bool):
                raise ValueError("Invalid remember setting")
            session_token, lifetime = self.store.create_browser_session(device["id"], remember=remember)
            response = web.json_response({"device": device})
            cookie_options = {"httponly": True, "samesite": "Strict", "secure": request.secure, "path": "/"}
            if remember:
                cookie_options["max_age"] = lifetime
            response.set_cookie(COOKIE, session_token, **cookie_options)
            successful = True
            return response
        finally:
            self.store.finish_login_attempt(attempt_id, successful=successful)

    async def logout(self, request):
        self.store.revoke_browser_session(request.cookies.get(COOKIE, ""))
        response = web.json_response({"ok": True})
        response.del_cookie(COOKIE, path="/")
        return response

    async def browser_login(self, request):
        device = request[DEVICE]
        if request.method == "DELETE":
            self.store.clear_browser_login(device["id"])
            response = web.json_response({"ok": True, "password_login_enabled": False, "login_name": ""})
            response.del_cookie(COOKIE, path="/")
            return response
        data = await body(request)
        profile = self.store.set_browser_login(
            device["id"], data.get("login_name"), data.get("password"),
            revoke_sessions=data.get("revoke_other_sessions", True),
        )
        session_token, lifetime = self.store.create_browser_session(device["id"], remember=True)
        response = web.json_response(profile)
        response.set_cookie(COOKIE, session_token, httponly=True, samesite="Strict", secure=request.secure,
                            max_age=lifetime, path="/")
        return response

    async def me(self, request):
        device = request[DEVICE] | self.store.browser_login_profile(request[DEVICE]["id"])
        peer = self.runtime.peers.get(device["id"])
        worker_online = self.runtime.connected(device["id"])
        connector_online = bool(peer and peer.ready and not getattr(peer.ws, "closed", True))
        live_version = peer.client_version if connector_online and peer.client_version else ""
        confirmed_version = device.get("connector_version") or ""
        return web.json_response({"device": device, "worker_online": worker_online,
                                  "connector_online": connector_online,
                                  "cwd": peer.cwd if peer else "", "server_time": time.time(),
                                  "client_version": live_version if connector_online else confirmed_version,
                                  "client_version_live": bool(live_version),
                                  "connector_version": confirmed_version,
                                  "connector_confirmed_at": device.get("connector_confirmed_at"),
                                  "connector_linked": bool(device.get("connector_confirmed_at")
                                                           or device.get("execution_mode") == "server"),
                                  "execution_mode": peer.execution_mode if connector_online else device.get("execution_mode"),
                                  "worker_paused": device["id"] in self.runtime.draining_devices,
                                  "connection": peer.connection if peer else None,
                                  "active_runs": len(self.store.list_active_runs(device["id"])),
                                  "connector_release": self.client_access.release_info(),
                                  "quota": self.store.device_quota(device["id"]) if device["status"] == "approved" else None})

    async def rooms(self, request):
        device_id = request[DEVICE]["id"]
        if request.method == "POST":
            data = await body(request)
            peer = self.runtime.peers.get(device_id)
            cwd = data.get("cwd") or (peer.cwd if peer else "")
            if not cwd.startswith("/"):
                raise ValueError("Connect your worker and choose an absolute project folder on your computer.")
            model, effort, speed = data.get("model", ""), data.get("effort", ""), data.get("speed", "standard")
            if speed == "fast":
                model, effort = self.runtime.validate_model(device_id, model, effort)
            self.runtime.validate_speed(model, speed)
            room = self.store.create_room(device_id, data.get("title") or "New chat", cwd,
                                          model, effort, data.get("parent_room_id"), data.get("reply_quote", ""),
                                          speed=speed, tool_read_only=data.get("tool_read_only", False))
            return web.json_response(room, status=201)
        rooms = self.store.list_rooms(device_id)
        active_by_room = {live.run["room_id"]: live for live in self.runtime.runs.values()}
        for room in rooms:
            active = active_by_room.get(room["id"])
            room["status"] = "running" if active else "idle"
            room["connection"] = active.connection if active else None
        return web.json_response({"rooms": rooms})

    async def room(self, request):
        device_id, room_id = request[DEVICE]["id"], request.match_info["room"]
        if request.method == "DELETE":
            result = self.store.delete_room_with_cleanup(device_id, room_id)
            self.cleanup_attachment_files(result["attachment_paths"], "room")
            self.cleanup_deleted_room_runtime(result)
            return web.json_response({"deleted": result["deleted"]})
        data = await body(request)
        if set(data) - {"title", "cwd", "model", "effort", "speed", "tool_read_only"}:
            raise ValueError("Unsupported room settings")
        room = self.store.get_room(device_id, room_id)
        self.store.assert_room_writable(device_id, room_id)
        if {"model", "effort", "speed"} & set(data):
            model, effort = self.runtime.validate_model(room["owner_id"], data.get("model", room["model"]), data.get("effort", room["effort"]))
            speed = data.get("speed", room.get("speed", "standard"))
            if "model" in data and "speed" not in data and not supports_fast(model):
                speed = "standard"
            self.runtime.validate_speed(model, speed)
            data.update(model=model, effort=effort, speed=speed)
        return web.json_response(self.store.update_room(device_id, room_id, **data))

    async def companions(self, request):
        device_id, parent_id = request[DEVICE]["id"], request.match_info["room"]
        if request.method == "GET":
            return web.json_response({"companions": self.store.list_companions(device_id, parent_id)})
        data = await body(request)
        if set(data) - {"kind", "source_message_id", "selected_text", "request_id"}:
            raise ValueError("Unsupported selection panel fields")
        source_id = data.get("source_message_id", "")
        live_source = None
        self.store._room(device_id, parent_id, own=True)
        if isinstance(source_id, str) and source_id.startswith("run:"):
            run_id = source_id[4:]
            try:
                run = self.store.get_run(device_id, run_id)
            except PermissionError:
                run = None
            if run and (run["room_id"] != parent_id or run["owner_id"] != device_id):
                run = None
            live = self.runtime.runs.get(run_id) if run else None
            if live:
                live_source = {"id": source_id, "role": "assistant", "content": live.text(), "streaming_snapshot": True}
            elif run and run.get("assistant_message_id"):
                message = self.store._message(run["assistant_message_id"])
                live_source = {"id": source_id, "role": "assistant", "content": message["content"]}
        result = self.store.create_companion(
            device_id, parent_id, data.get("kind"), source_id, data.get("selected_text"),
            data.get("request_id"), live_source=live_source,
        )
        return web.json_response(result, status=200 if result["deduplicated"] else 201)

    async def companion(self, request):
        device_id, parent_id = request[DEVICE]["id"], request.match_info["room"]
        companion_id = request.match_info["companion"]
        companion = self.store.get_companion(device_id, parent_id, companion_id, own=True)
        if request.method == "PATCH":
            data = await body(request)
            if "draft" not in data or set(data) - {"draft", "draft_revision"}:
                raise ValueError("Only a side thread draft can be updated here")
            return web.json_response({"companion": self.store.update_companion(
                device_id, parent_id, companion_id, data["draft"], data.get("draft_revision"),
            )})
        companion = self.store.begin_companion_close(device_id, parent_id, companion_id)
        task = self.runtime.track(self.close_companion(device_id, parent_id, companion))
        return web.json_response(await asyncio.shield(task))

    async def close_companion(self, device_id, parent_id, companion):
        """CODEX: Complete an explicitly requested close even if the browser disconnects."""
        run_ids = [row[0] for row in self.store.db.execute("SELECT id FROM runs WHERE room_id=?", (companion["room_id"],))]
        for run_id in run_ids:
            live = self.runtime.runs.get(run_id)
            if live:
                await self.runtime.stop(run_id)
                if run_id in self.runtime.runs:
                    try:
                        await asyncio.wait_for(live.finished_event.wait(), timeout=14)
                    except asyncio.TimeoutError:
                        self.runtime.finish(live, "interrupted", "The selection panel was closed.")
                if live.task and not live.task.done():
                    live.task.cancel()
                    await asyncio.gather(live.task, return_exceptions=True)
            elif self.store.get_run_internal(run_id)["status"] in LIVE:
                self.store.finish_run(run_id, "cancelled", "", "The selection panel was closed.")
        result = self.store.delete_companion(device_id, parent_id, companion["id"])
        self.cleanup_attachment_files(result["attachment_paths"], "companion")
        self.cleanup_deleted_room_runtime(result)
        return {"deleted": True}

    async def messages(self, request):
        device_id, room_id = request[DEVICE]["id"], request.match_info["room"]
        room = self.store.get_room(device_id, room_id)
        values = self.store.list_messages(device_id, room_id, before=request.query.get("before"), limit=50)
        snapshots = await self.runtime.durable_snapshots(
            device_id, room_id, include_completed_activity=False
        )
        cursor = self.store.db.execute("SELECT COALESCE(MAX(id),0) FROM events WHERE room_id=?", (room_id,)).fetchone()[0]
        return web.json_response({"room": room, "messages": [self.message(device_id, item) for item in values],
                                  "runs": snapshots, "event_cursor": cursor, "has_more": len(values) == 50})

    async def models(self, request):
        return web.json_response({"models": await self.runtime.refresh_models(request[DEVICE]["id"])})

    async def runs(self, request):
        device_id, room_id = request[DEVICE]["id"], request.match_info["room"]
        room = self.store.get_room(device_id, room_id)
        if room["owner_id"] != device_id:
            raise PermissionError("Only the owning computer can run prompts in this room.")
        self.store.assert_room_writable(device_id, room_id, own=True)
        data = await body(request)
        text = data.get("text", "")
        if not isinstance(text, str) or len(text) > 1_000_000:
            raise ValueError("Message is too large")
        quote = data.get("reply_quote") or ""
        parent_id = data.get("reply_to_id")
        context_source = "selected_parent_context" if quote else "message"
        if quote:
            if not isinstance(quote, str) or len(quote) > 50_000:
                raise ValueError("Reply quote is too large")
            if parent_id:
                row = self.store.db.execute("SELECT content FROM messages WHERE id=? AND room_id=?", (parent_id, room_id)).fetchone()
                if row is None:
                    raise ValueError("Select a quote from a message in this room.")
            text = "In reply to this message:\n" + "\n".join("> " + line for line in quote.splitlines()) + "\n\n" + text
        attachments = data.get("attachment_ids", [])
        if not isinstance(attachments, list) or len(attachments) > 10:
            raise ValueError("Attach at most 10 files per message")
        request_id = data.get("request_id", "")
        tool_read_only = data.get("tool_read_only", room.get("tool_read_only", False))
        if not isinstance(tool_read_only, bool):
            raise ValueError("Invalid read-only setting")
        self.store.enforce_companion_run(room_id, tool_read_only, attachments)
        if room.get("companion_kind") == "more_details":
            text = DETAILS_PROMPT
            context_source = "companion"
        if isinstance(request_id, str) and self.store.db.execute(
                "SELECT 1 FROM runs WHERE owner_id=? AND request_id=?", (device_id, request_id)).fetchone():
            # CODEX: Saved receipts remain readable after quotas, worker availability or room settings change.
            result = self.store.create_run(device_id, room_id, text, attachments, request_id, tool_read_only)
            result["user_message"] = self.message(device_id, result["user_message"])
            return web.json_response(result, status=202)
        await self.runtime.flush_events()
        self.require_run_admission()
        if not self.runtime.connected(device_id):
            raise ValueError("Your local worker is offline. Start it on your computer before sending.")
        model, effort = self.runtime.validate_model(
            device_id, data.get("model", room["model"]), data.get("effort", room["effort"])
        )
        speed = self.runtime.validate_speed(model, data.get("speed", room.get("speed", "standard")))
        if (model, effort, speed) != (room["model"], room["effort"], room.get("speed", "standard")):
            room = {**room, **self.store.update_room(device_id, room_id, model=model, effort=effort, speed=speed)}
        result = self.store.create_run(device_id, room_id, text, attachments, request_id, tool_read_only)
        # CODEX: Preserve companion metadata while refreshing the room after admission and automatic naming.
        room = {**room, **result.get("room", {})}
        if not result.get("deduplicated"):
            result["run"] = self.store.configure_run_usage(result["run"]["id"], model, effort, speed)
            context = text
            if room.get("companion_kind") and self.store.companion_needs_context(room):
                context = self.store.companion_prompt(room_id, text)
                context_source = "companion"
            elif room.get("reply_quote") and not room.get("thread_id"):
                context = "Context selected in a parent conversation:\n" + room["reply_quote"] + "\n\n" + text
                context_source = "selected_parent_context"
            self.runtime.start(
                result["run"], room, context, attachments, context_source=context_source
            )
        result["user_message"] = self.message(device_id, result["user_message"])
        return web.json_response(result, status=202)

    async def stop(self, request):
        run = self.store.get_run(request[DEVICE]["id"], request.match_info["run"])
        await self.runtime.stop(run["id"])
        return web.json_response({"ok": True})

    async def steer(self, request):
        device_id, room_id = request[DEVICE]["id"], request.match_info["room"]
        room = self.store.get_room(device_id, room_id)
        if room["owner_id"] != device_id:
            raise PermissionError("Only the owning computer can send follow-ups in this room.")
        self.store.assert_room_writable(device_id, room_id, own=True)
        await self.runtime.flush_events()
        self.require_run_admission()
        data = await body(request)
        if "tool_read_only" not in data or not isinstance(data["tool_read_only"], bool):
            raise ValueError("An explicit read-only setting is required.")
        if room.get("companion_kind") == "more_details":
            raise ValueError("More details is an explanation-only panel; use Side thread for a conversation")
        text = data.get("text", "")
        if not isinstance(text, str) or len(text) > 1_000_000:
            raise ValueError("Message is too large")
        quote, parent_id = data.get("reply_quote") or "", data.get("reply_to_id")
        if quote:
            if not isinstance(quote, str) or len(quote) > 50_000:
                raise ValueError("Reply quote is too large")
            if parent_id and self.store.db.execute("SELECT id FROM messages WHERE id=? AND room_id=?", (parent_id, room_id)).fetchone() is None:
                raise ValueError("Select a quote from a message in this room.")
            text = "In reply to this message:\n" + "\n".join("> " + line for line in quote.splitlines()) + "\n\n" + text
        task = self.runtime.track(self.runtime.steer(device_id, room_id, data.get("run_id", ""), text,
                                                   data.get("attachment_ids", []), data.get("request_id", ""),
                                                   data["tool_read_only"]))
        try:
            result = await asyncio.shield(task)
        except SteeringConflict as exc:
            return web.json_response({"error": str(exc), "retryable": exc.retryable}, status=409)
        if result.get("user_message"):
            result["user_message"] = self.message(device_id, result["user_message"])
        result["mode_changed"] = bool(result.get("mode_changed") or result["steering"].get("replacement_run_id"))
        result["steered"] = result["steering"]["status"] == "accepted" and not result["mode_changed"]
        if result["steering"]["status"] == "rejected":
            result.update(error=result["steering"].get("error") or "The follow-up was not sent.", retryable=False)
            return web.json_response(result, status=409)
        return web.json_response(result, status=200 if result.get("deduplicated") else 202)

    async def events(self, request):
        device_id, room_id = request[DEVICE]["id"], request.match_info["room"]
        snapshots = await self.runtime.durable_snapshots(
            device_id, room_id, include_completed_activity=False
        )
        events = self.store.events(device_id, room_id, after=int(request.query.get("after", "0")))
        for event in events:
            if event["kind"] == "completed" and event["payload"].get("message"):
                event["payload"]["message"] = self.message(device_id, event["payload"]["message"])
            elif event["kind"] == "steered" and event["payload"].get("user_message"):
                event["payload"]["user_message"] = self.message(device_id, event["payload"]["user_message"])
        return web.json_response({"events": events, "runs": snapshots})

    async def upload(self, request):
        device_id = request[DEVICE]["id"]
        # CODEX: Reserve without awaiting; reject excess uploads before allocating their bodies.
        if (self.active_uploads >= MAX_PARALLEL_UPLOADS
                or self.device_uploads.get(device_id, 0) >= MAX_DEVICE_UPLOADS):
            raise web.HTTPTooManyRequests(
                text="Too many file uploads are in progress. Retry shortly.",
                headers={"Retry-After": "2"},
            )
        self.active_uploads += 1
        self.device_uploads[device_id] = self.device_uploads.get(device_id, 0) + 1
        path, writer, committed = None, None, False

        def release_slot():
            self.active_uploads -= 1
            remaining = self.device_uploads[device_id] - 1
            if remaining:
                self.device_uploads[device_id] = remaining
            else:
                self.device_uploads.pop(device_id, None)

        def cleanup_file():
            if path is not None and not committed:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    LOGGER.exception("rejected_upload_cleanup_failed")

        try:
            reader = await request.multipart()
            part = await reader.next()
            if part is None or part.name != "file":
                raise ValueError("Upload a file in the file field")
            name = attachment_name(part.filename)
            data = bytearray()
            while chunk := await part.read_chunk(65536):
                data.extend(chunk)
                if len(data) > MAX_IMAGE:
                    raise ValueError("Each file must be no larger than 25 MiB")
            mime = attachment_mime(data)
            path = self.upload_dir / uuid.uuid4().hex

            def save_upload():
                with path.open("xb") as target:
                    os.chmod(path, 0o600)
                    target.write(data)

            writer = asyncio.create_task(asyncio.to_thread(save_upload))
            # CODEX: Cancellation must not drop the reservation while its disk writer still owns the body.
            await asyncio.shield(writer)
            attachment = self.store.add_attachment(device_id, name, mime, len(data), str(path))
            committed = True
            return web.json_response(self.attachment(attachment), status=201)
        finally:
            if writer is not None and not writer.done():
                def finish_cancelled_upload(done):
                    if not done.cancelled():
                        done.exception()
                    cleanup_file()
                    release_slot()
                writer.add_done_callback(finish_cancelled_upload)
            else:
                cleanup_file()
                release_slot()

    async def download(self, request):
        attachment = self.store.get_attachment(request[DEVICE]["id"], request.match_info["attachment"])
        path = Path(attachment["path"]).resolve()
        if not path.is_relative_to(self.upload_dir.resolve()) or not path.is_file():
            raise web.HTTPNotFound(text="Attachment is not available")
        disposition = "inline" if attachment["mime"] in {"image/png", "image/jpeg", "image/gif", "image/webp"} else "attachment"
        return web.FileResponse(path, headers={
            "Content-Type": attachment["mime"],
            "Content-Disposition": content_disposition_header(disposition, filename=attachment_name(attachment["name"])),
            "X-Content-Type-Options": "nosniff",
        })

    async def discard_attachment(self, request):
        """CODEX: Let an authenticated owner release only an upload that no durable message uses."""
        result = self.store.discard_attachment(
            request[DEVICE]["id"], request.match_info["attachment"]
        )
        if result["path"]:
            self.cleanup_attachment_files([result["path"]], "discard")
        return web.json_response({"deleted": result["deleted"]})

    async def devices(self, request):
        values = self.store.list_devices(request[DEVICE]["id"])
        for value in values:
            peer = self.runtime.peers.get(value["id"])
            connector_online = bool(peer and peer.ready and not getattr(peer.ws, "closed", True))
            live_version = peer.client_version if connector_online and peer.client_version else ""
            value["worker_online"] = self.runtime.connected(value["id"])
            value["connector_online"] = connector_online
            value["client_version"] = (live_version if connector_online
                                       else value.get("connector_version") or "")
            value["client_version_live"] = bool(live_version)
            value["connector_linked"] = bool(value.get("connector_confirmed_at")
                                               or value.get("execution_mode") == "server")
        return web.json_response({"devices": values})

    async def device(self, request):
        data = await body(request)
        if set(data) - {"status", "max_concurrent", "token_budget", "weekly_share_percent"}:
            raise ValueError("Unsupported device setting")
        device = self.store.set_device(request[DEVICE]["id"], request.match_info["device"], **data)
        if device["status"] != "approved":
            peer = self.runtime.peers.get(device["id"])
            if peer:
                await peer.ws.close(code=1008, message=b"Device access revoked")
                await self.runtime.disconnect(peer)
        return web.json_response(device)

    def cleanup_deleted_device_files(self, result):
        """CODEX: Remove only committed device files inside the two server-managed roots."""
        self.cleanup_attachment_files(result["attachment_paths"], "device")

        try:
            canonical_id = str(uuid.UUID(result["device_id"]))
        except (ValueError, TypeError, AttributeError):
            return
        if canonical_id != result["device_id"]:
            return
        data_root = self.data_dir.resolve()
        remote_root_path = self.data_dir / "remote-workers"
        if remote_root_path.is_symlink():
            return
        remote_root = remote_root_path.resolve()
        if not remote_root.is_relative_to(data_root):
            return
        remote_state = remote_root / canonical_id
        try:
            if remote_state.is_symlink() or remote_state.is_file():
                remote_state.unlink(missing_ok=True)
            elif remote_state.is_dir():
                shutil.rmtree(remote_state)
        except OSError:
            LOGGER.exception("device_remote_state_cleanup_failed device_suffix=%s", device_id_suffix(canonical_id))

    async def delete_device(self, request):
        device_id = request.match_info["device"]
        try:
            result = self.store.delete_device(request[DEVICE]["id"], device_id)
        except DeviceBusyError as exc:
            raise web.HTTPConflict(text=str(exc)) from exc

        try:
            self.runtime.draining_devices.discard(device_id)
            self.connecting.discard(device_id)
        except Exception:
            LOGGER.exception("deleted_device_memory_cleanup_failed device_suffix=%s", device_id_suffix(device_id))
        session = self.remote_sessions.pop(device_id, None)
        peer = self.runtime.peers.get(device_id)
        if session is not None:
            try:
                await asyncio.wait_for(session.close(), timeout=2)
            except Exception:
                LOGGER.exception("deleted_device_session_close_failed device_suffix=%s", device_id_suffix(device_id))
        if peer is not None:
            try:
                await asyncio.wait_for(peer.ws.close(code=1008, message=b"Device deleted"), timeout=2)
            except Exception:
                LOGGER.exception("deleted_device_socket_close_failed device_suffix=%s", device_id_suffix(device_id))
            try:
                await asyncio.wait_for(self.runtime.disconnect(peer), timeout=2)
            except Exception:
                LOGGER.exception("deleted_device_runtime_disconnect_failed device_suffix=%s", device_id_suffix(device_id))
        if self.runtime.peers.get(device_id) is peer:
            self.runtime.peers.pop(device_id, None)
        try:
            for run_id in result["run_ids"]:
                self.runtime.activity_cache.pop(run_id, None)
            self.runtime.thread_totals = {
                key: value for key, value in self.runtime.thread_totals.items() if key[0] != device_id
            }
            self.runtime.by_thread = {
                key: value for key, value in self.runtime.by_thread.items() if key[0] != device_id
            }
        except Exception:
            LOGGER.exception("deleted_device_runtime_cleanup_failed device_suffix=%s", device_id_suffix(device_id))
        try:
            self.cleanup_deleted_device_files(result)
        except Exception:
            LOGGER.exception("deleted_device_file_cleanup_failed device_suffix=%s", device_id_suffix(device_id))
        return web.json_response({"ok": True, "device_id": device_id})

    async def stop_device(self, request):
        self.store.list_devices(request[DEVICE]["id"])
        await self.runtime.stop_device(request.match_info["device"])
        return web.json_response({"ok": True})

    async def acknowledge_unknown_usage(self, request):
        result = self.store.acknowledge_unknown_usage(
            request[DEVICE]["id"], request.match_info["device"]
        )
        return web.json_response(result)

    async def usage(self, request):
        admin_id = request[DEVICE]["id"]
        await self.runtime.flush_events()
        usage = self.store.usage(admin_id)
        provider = await self.read_provider_usage()
        return web.json_response({"usage": usage, "provider_usage": provider,
                                  "quota": self.store.quota_report(admin_id)})

    async def setup_host(self, request):
        self.store._actor(request[DEVICE]["id"], admin=True)
        if request.method == "GET":
            return web.json_response(await self.host_setup.status())
        return web.json_response(await self.host_setup.execute(await body(request)), status=202)

    async def setup_ca(self, request):
        self.store._actor(request[DEVICE]["id"], admin=True)
        return self.host_setup.ca_response()

    async def own_usage(self, request):
        await self.runtime.flush_events()
        await self.read_provider_usage(force=True)
        return web.json_response(self.store.device_quota(request[DEVICE]["id"]))

    async def read_provider_usage(self, force=False):
        provider = {"available": False, "windows": [], "fetched_at": None, "stale": False,
                    "error": "OpenAI account usage is currently unavailable."}
        read_limits = getattr(self.runtime.auth, "rate_limits", None)
        if callable(read_limits):
            try:
                provider = await read_limits(force=True) if force else await read_limits()
            except (OSError, RuntimeError, ValueError, TypeError, asyncio.TimeoutError):
                pass
        if not isinstance(provider, dict):
            provider = {"available": False, "windows": [], "fetched_at": None, "stale": False}
        self.store.observe_provider_usage(provider)
        return provider

    async def sample_provider_usage(self):
        # CODEX: Calibration continues without an open administrator dashboard; no inference requests are made.
        state = self.usage_sampler

        def failed(exc):
            state["consecutive_failures"] += 1
            state["last_error"] = type(exc).__name__
            state["last_error_at"] = time.time()
            multiplier = 2 ** min(state["consecutive_failures"] - 1, 3)
            return min(PROVIDER_SAMPLE_SECONDS, PROVIDER_RETRY_SECONDS * multiplier)

        async def sampled():
            provider = await self.read_provider_usage()
            state["last_success"] = time.time()
            state["consecutive_failures"] = 0
            return provider_sample_interval(provider)

        state["running"] = True
        try:
            try:
                delay = await sampled()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                delay = failed(exc)
            while True:
                await asyncio.sleep(delay)
                try:
                    limited = self.store.db.execute(
                        "SELECT 1 FROM devices WHERE weekly_share_percent IS NOT NULL LIMIT 1"
                    ).fetchone()
                    if self.runtime.runs or limited or state["consecutive_failures"]:
                        delay = await sampled()
                    else:
                        delay = PROVIDER_SAMPLE_SECONDS
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    delay = failed(exc)
        finally:
            state["running"] = False

    async def executor(self, request):
        """CODEX: Accept only approved outbound connectors; model processes stay on the host."""
        if not request.headers.get("Authorization", "").startswith("Bearer "):
            raise PermissionError("A connector credential is required; browser cookies are not accepted here.")
        device = request[DEVICE]
        device_id = device["id"]
        existing = self.runtime.peers.get(device_id)
        if device_id in self.connecting or device_id in self.remote_sessions or (existing and not existing.ws.closed):
            raise web.HTTPConflict(text="This device already has a connector connected.")
        self.connecting.add(device_id)
        ws = web.WebSocketResponse(
            heartbeat=LAN_WEBSOCKET_HEARTBEAT_SECONDS,
            max_msg_size=8 * 1024 * 1024,
            compress=False,
        )
        session = None
        failure = None

        async def authorize_until_closed():
            while not ws.closed:
                await asyncio.sleep(EXECUTOR_AUTH_INTERVAL)
                try:
                    self.store.authenticate(token_from(request))
                except PermissionError:
                    await ws.close(code=1008, message=b"Device access revoked")
                    return

        authorization = None
        try:
            await ws.prepare(request)
            hello = await asyncio.wait_for(ws.receive_json(), timeout=15)
            if (not isinstance(hello, dict) or hello.get("type") != "executor_hello"
                    or hello.get("protocol") != 1 or isinstance(hello.get("protocol"), bool)
                    or not isinstance(hello.get("version"), str) or not 1 <= len(hello["version"]) <= 80
                    or not isinstance(hello.get("cwd"), str) or not hello["cwd"].startswith("/")
                    or len(hello["cwd"]) > 4096 or any(ord(char) < 32 for char in hello["cwd"])):
                raise ValueError("Invalid connector handshake. Install the current Connector release.")
            self.store.authenticate(token_from(request))
            device = self.store.enable_server_execution(device_id, hello["version"])
            session = HostWorkerSession(self, device, ws, hello)
            self.remote_sessions[device_id] = session
            authorization = asyncio.create_task(authorize_until_closed())
            await session.run()
        except (PermissionError, ValueError, ConnectionError, RuntimeError, OSError, asyncio.TimeoutError) as exc:
            failure = exc
            if not ws.closed:
                with contextlib.suppress(ConnectionError, RuntimeError):
                    await ws.send_json({"type": "status", "state": "reconnecting",
                                        "message": "The server session is reconnecting. Local execution will not switch to the server's files."})
                await ws.close(code=1011, message=b"Reconnect to the server session")
        finally:
            log_transport_close("executor", device_id, ws, failure)

            async def cleanup_executor():
                # CODEX: Closing the browser transport can cancel its handler during cleanup.
                try:
                    if authorization is not None:
                        authorization.cancel()
                        await asyncio.gather(authorization, return_exceptions=True)
                    if session is not None:
                        await session.close()
                    if not ws.closed:
                        await ws.close()
                finally:
                    self.connecting.discard(device_id)
                    if self.remote_sessions.get(device_id) is session:
                        self.remote_sessions.pop(device_id, None)

            await asyncio.shield(self.runtime.track(cleanup_executor()))
        return ws

    async def worker(self, request):
        if request.secure:
            raise web.HTTPUpgradeRequired(text="Update Codex Connector: Codex now runs on the host, not on your computer's Internet connection.")
        if not request.headers.get("Authorization", "").startswith("Bearer "):
            raise PermissionError("A worker credential is required; browser cookies are not accepted here.")
        device = request[DEVICE]
        if not device["is_admin"]:
            raise web.HTTPUpgradeRequired(text="Update Codex Connector: remote devices use server-managed execution and cannot run the legacy model worker.")
        existing = self.runtime.peers.get(device["id"])
        if device["id"] in self.connecting or (existing and not existing.ws.closed):
            raise web.HTTPConflict(text="This device already has a worker connected.")
        self.connecting.add(device["id"])
        try:
            tokens = await self.runtime.auth.tokens()
            self.store.authenticate(token_from(request))
        except BaseException:
            self.connecting.discard(device["id"])
            raise
        ws = web.WebSocketResponse(
            heartbeat=LAN_WEBSOCKET_HEARTBEAT_SECONDS,
            max_msg_size=8 * 1024 * 1024,
            compress=False,
        )
        peer = Peer(device["id"], ws)
        peer.filesystem_local = request.remote in {"127.0.0.1", "::1"}
        peer.draining = device["id"] in self.runtime.draining_devices
        failure = None
        try:
            await ws.prepare(request)
            self.runtime.peers[device["id"]] = peer
            self.store.authenticate(token_from(request))
            await ws.send_json({"type": "auth", "tokens": tokens})
            del tokens
            async for frame in ws:
                self.store.authenticate(token_from(request))
                if frame.type != WSMsgType.TEXT:
                    continue
                packet = json.loads(frame.data)
                if not isinstance(packet, dict):
                    raise ValueError("Expected an object")
                kind = packet.get("type")
                if kind == "ready":
                    reported_version = packet.get("client_version", "")
                    if not isinstance(reported_version, str):
                        raise ValueError("Invalid connector version")
                    peer.client_version = reported_version
                    if peer.client_version:
                        self.store.confirm_connector(device["id"], peer.client_version)
                    peer.network_recovery = packet.get("network_recovery") is True
                    models = packet.get("models", [])
                    if not isinstance(models, list) or len(models) > 100 or not all(isinstance(model, dict) and isinstance(model.get("id"), str) for model in models):
                        raise ValueError("Invalid model registry")
                    peer.models = models
                    peer.model_refresh_after = time.monotonic() + MODEL_REFRESH_SECONDS
                    peer.cwd = str(packet.get("cwd", ""))
                    peer.ready = True
                elif kind == "result":
                    peer.receive(packet)
                elif kind == "event":
                    self.runtime.event(peer, packet.get("method"), packet.get("params") or {})
                    await asyncio.sleep(0)
                elif kind == "auth_refresh":
                    tokens = await self.runtime.auth.tokens(force=True)
                    self.store.authenticate(token_from(request))
                    await ws.send_json({"type": "auth_response", "id": packet.get("id"), "tokens": tokens})
                    del tokens
        except (PermissionError, ValueError, ConnectionError, RuntimeError) as exc:
            failure = exc
            await ws.close(code=1008, message=b"Worker disconnected; check authorization and server sign-in")
        finally:
            log_transport_close("legacy-worker", device["id"], ws, failure)
            self.connecting.discard(device["id"])
            await self.runtime.disconnect(peer)
        return ws

    async def static(self, request):
        name = request.match_info.get("name") or "index.html"
        if name not in {"index.html", "app.js", "host-setup.js", "theme-init.js", "style.css", "companions.js", "companions.css", "controls.js", "controls.css",
                        "math.js", "math.css", "katex-0.18.7.min.js", "KaTeX-LICENSE.txt",
                        "Vazir-Variable-v30.1.0.woff2", "Vazir-OFL.txt", "Vazir-FONT.md"}:
            raise web.HTTPNotFound()
        headers = {"Content-Type": "font/woff2"} if name.endswith(".woff2") else None
        return web.FileResponse(Path(__file__).parent / "web" / name, headers=headers)


def create_app(data_dir, auth=None, *, batch_events=False):
    service = Service(data_dir, auth, batch_events=batch_events)
    service.store.recover_runs()
    app = web.Application(middlewares=[guard], client_max_size=MAX_IMAGE + 1024 * 1024)
    app[SERVICE] = service
    app.add_routes(service.client_access.routes())
    app.add_routes([
        web.get("/api/health", service.health), web.post("/api/enroll", service.enroll),
        web.post("/api/session", service.session), web.post("/api/logout", service.logout),
        web.put("/api/me/browser-login", service.browser_login), web.delete("/api/me/browser-login", service.browser_login),
        web.get("/api/me", service.me), web.get("/api/rooms", service.rooms), web.post("/api/rooms", service.rooms),
        web.post("/api/usage/me", service.own_usage),
        web.patch("/api/rooms/{room}", service.room), web.delete("/api/rooms/{room}", service.room),
        web.get("/api/rooms/{room}/companions", service.companions), web.post("/api/rooms/{room}/companions", service.companions),
        web.patch("/api/rooms/{room}/companions/{companion}", service.companion),
        web.delete("/api/rooms/{room}/companions/{companion}", service.companion),
        web.get("/api/rooms/{room}/messages", service.messages), web.get("/api/models", service.models),
        web.post("/api/rooms/{room}/runs", service.runs), web.post("/api/runs/{run}/stop", service.stop),
        web.post("/api/rooms/{room}/steer", service.steer),
        web.get("/api/rooms/{room}/events", service.events), web.post("/api/attachments", service.upload),
        web.post("/api/attachments/{attachment}/discard", service.discard_attachment),
        web.delete("/api/attachments/{attachment}", service.discard_attachment),
        web.get("/api/attachments/{attachment}", service.download), web.get("/api/admin/devices", service.devices),
        web.patch("/api/admin/devices/{device}", service.device),
        web.delete("/api/admin/devices/{device}", service.delete_device),
        web.post("/api/admin/devices/{device}/quota/acknowledge-unknown", service.acknowledge_unknown_usage),
        web.post("/api/admin/devices/{device}/stop", service.stop_device), web.get("/api/admin/usage", service.usage),
        web.get("/api/admin/host-setup", service.setup_host), web.post("/api/admin/host-setup", service.setup_host),
        web.get("/api/admin/host-setup/ca", service.setup_ca),
        web.get("/api/worker", service.worker), web.get("/", service.static), web.get("/{name}", service.static),
        web.get("/api/executor", service.executor),
    ])

    async def startup(application):
        try:
            await service.runtime.cleanup_stale_generated_sources()
        except Exception:
            LOGGER.exception("generated_image_startup_gc_failed")
        try:
            service.reconcile_attachments()
        except Exception:
            LOGGER.exception("attachment_startup_gc_failed")
        service.runtime.track(service.attachment_gc_loop())
        if callable(getattr(service.runtime.auth, "rate_limits", None)):
            service.runtime.track(service.sample_provider_usage())

    async def shutdown(application):
        await service.host_setup.close()
        await asyncio.gather(*(session.close() for session in list(service.remote_sessions.values())), return_exceptions=True)
        await service.runtime.close()
        close = getattr(service.runtime.auth, "close", None)
        if callable(close):
            await close()

    async def cleanup(application):
        service.store.close()

    app.on_startup.append(startup)
    app.on_shutdown.append(shutdown)
    app.on_cleanup.append(cleanup)
    return app


async def serve(data_dir, port):
    app = create_app(data_dir, batch_events=True)
    runner = web.AppRunner(app, access_log=None)
    watchdog = None
    diagnostics = start_loop_diagnostics()
    try:
        await runner.setup()
        app[SERVICE].host_setup.runner = runner
        app[SERVICE].host_setup.loopback_port = port
        await web.TCPSite(runner, "127.0.0.1", port).start()
        network = app[SERVICE].network
        if network:
            await app[SERVICE].host_setup.start_network_site(network)
        watchdog = start_systemd_watchdog()
        stopping = asyncio.Event()
        loop = asyncio.get_running_loop()
        for value in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(value, stopping.set)
        await stopping.wait()
    finally:
        if watchdog is not None:
            watchdog.cancel()
            await asyncio.gather(watchdog, return_exceptions=True)
        try:
            await runner.cleanup()
        finally:
            await stop_loop_diagnostics(diagnostics)


def main():
    parser = argparse.ArgumentParser(description="Codex LAN server with optional private-network HTTPS")
    parser.add_argument("--data-dir", type=Path, default=Path.home() / ".local/share/codex-lan/server")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--init", action="store_true", help="Create the first administrator credential, then exit")
    args = parser.parse_args()
    os.umask(0o077)
    args.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (args.data_dir / "server.lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            parser.error("A server is already using this data directory")
        if args.init:
            store = Store(args.data_dir)
            result = store.bootstrap_admin("Host administrator")
            credential = args.data_dir / "admin.token"
            with credential.open("x") as target:
                target.write(result["token"])
            store.close()
            print(f"Administrator initialized. Credential file: {credential}")
            return
        asyncio.run(serve(args.data_dir, args.port))


if __name__ == "__main__":
    main()
