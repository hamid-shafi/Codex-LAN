#!/usr/bin/env python3
"""CODEX: Local Codex app-server worker for the private LAN web client."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import ipaddress
import json
import os
import random
import shutil
import signal
import socket
import ssl
import stat
import sys
import uuid
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional
from urllib.parse import quote, urlsplit, urlunsplit

import aiohttp

from . import __version__
from .egress import AdaptiveProxy
from .media import MAX_GENERATED_IMAGE_BYTES, MAX_USER_FILE_BYTES, attachment_extension, attachment_mime
from .model_registry import ModelRegistryError, read_model_registry
from .recovery import CONTINUE_AFTER_CONNECTION, network_error
from .transport import (
    LAN_WEBSOCKET_HEARTBEAT_SECONDS,
    format_transport_diagnostic,
    websocket_diagnostic,
)


MAX_ATTACHMENT_BYTES = MAX_USER_FILE_BYTES
MAX_GENERATED_ARTIFACT_BYTES = MAX_GENERATED_IMAGE_BYTES
MAX_JSON_BYTES = 1024 * 1024
MAX_WS_BYTES = 8 * 1024 * 1024
MAX_FORWARDED_COMMAND_OUTPUT = 65536
MAX_AUTOMATIC_RECOVERY_TURNS = 1
MAX_ARTIFACT_TRANSFERS = 4
ARTIFACT_STORE_CAPABILITY = "artifact-store-v1"
FILE_ATTACHMENTS_CAPABILITY = "file-attachments-v1"
OUTPUT_OMITTED = "\n… [additional command output omitted]\n"
ARTIFACT_EXTENSIONS = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
}
POLL_SECONDS = 2.0
REFRESH_TIMEOUT_SECONDS = 8.0
RPC_TIMEOUT_SECONDS = 90.0
MODEL_DISCOVERY_TIMEOUT_SECONDS = 30.0
ALLOWED_RPC_METHODS = {
    "thread/start",
    "thread/resume",
    "thread/read",
    "turn/start",
    "turn/steer",
    "turn/interrupt",
    "model/list",
}
COMMAND_APPROVAL_METHODS = {
    "item/commandExecution/requestApproval": "accept",
    "execCommandApproval": "approved",
}
FILE_APPROVAL_METHODS = {
    "item/fileChange/requestApproval": "accept",
    "applyPatchApproval": "approved",
}
PROVIDER_ENVIRONMENT_KEYS = {
    "OPENAI_API_KEY",
    "OPENAI_ORG_ID",
    "OPENAI_ORGANIZATION",
    "OPENAI_PROJECT_ID",
    "CODEX_API_KEY",
    "CODEX_ACCESS_TOKEN",
    "CHATGPT_ACCESS_TOKEN",
}


class WorkerError(RuntimeError):
    """A safe, user-displayable worker failure."""


class DeviceAccessError(WorkerError):
    """The central server rejected or revoked this device credential."""


class TransientServerError(WorkerError):
    """A central service failure that is safe to retry."""


class AppServerRpcError(WorkerError):
    """A local app-server request returned a JSON-RPC error."""


class PayloadLimitError(WorkerError):
    pass


class ExecutorUpgradeRequired(WorkerError):
    """CODEX: An incompatible server must not trigger a local model fallback."""


class ArtifactTransferError(WorkerError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Endpoints:
    base: str
    origin: str
    browser: str
    enroll: str
    me: str
    attachments: str
    worker: str


@dataclass(frozen=True)
class WorkerConfig:
    endpoints: Endpoints
    token_file: Path
    cwd: Path
    codex: Path
    state_dir: Path
    name: str
    enroll: bool = False
    open_browser: bool = False
    server_ca: Optional[Path] = None
    route_state: Optional[Path] = None


def _is_loopback(hostname: str) -> bool:
    value = hostname.rstrip(".").lower()
    if value == "localhost":
        return True
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


def build_endpoints(server: str) -> Endpoints:
    value = str(server or "").strip()
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise WorkerError("--server must be an http:// or https:// URL.")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise WorkerError("--server must not contain credentials, a query, or a fragment.")
    if parsed.scheme == "http" and not _is_loopback(parsed.hostname):
        raise WorkerError("Plain HTTP is allowed only for a loopback or SSH-tunnel URL.")
    path = parsed.path.rstrip("/")
    base = urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))
    origin = urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))
    ws_scheme = "wss" if parsed.scheme == "https" else "ws"
    worker = urlunsplit((ws_scheme, parsed.netloc, path + "/api/worker", "", ""))
    return Endpoints(
        base=base,
        origin=origin,
        browser=base + "/",
        enroll=base + "/api/enroll",
        me=base + "/api/me",
        attachments=base + "/api/attachments",
        worker=worker,
    )


def resolve_codex(value: Optional[str]) -> Path:
    candidate = value or shutil.which("codex")
    if not candidate:
        fallback = Path.home() / ".local" / "bin" / "codex"
        candidate = str(fallback) if fallback.exists() else ""
    path = Path(candidate).expanduser().resolve() if candidate else Path()
    if not candidate or not path.is_file() or not os.access(path, os.X_OK):
        raise WorkerError("Codex CLI was not found or is not executable.")
    return path


def ensure_private_directory(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink() or not path.is_dir():
        raise WorkerError(f"Private state path is not a directory: {path}")
    path.chmod(0o700)
    return path


def load_device_token(path: Path) -> str:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        raise WorkerError("Device token file is missing; run with --enroll first.") from None
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise WorkerError("Device token path must be a regular file, not a symlink.")
    if stat.S_IMODE(metadata.st_mode) != 0o600:
        raise WorkerError("Device token file must have mode 0600.")
    try:
        token = path.read_text(encoding="utf-8").strip()
    except OSError:
        raise WorkerError("Device token file could not be read.") from None
    if not token or len(token) > 8192 or any(character.isspace() for character in token):
        raise WorkerError("Device token file is invalid.")
    return token


def save_device_token(path: Path, token: str) -> None:
    if not isinstance(token, str) or not token or len(token) > 8192:
        raise WorkerError("Enrollment returned an invalid device token.")
    if any(character.isspace() for character in token):
        raise WorkerError("Enrollment returned an invalid device token.")
    ensure_private_directory(path.parent)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError:
        raise WorkerError("Device token file appeared during enrollment; retry safely.") from None
    try:
        payload = (token + "\n").encode("utf-8")
        while payload:
            written = os.write(descriptor, payload)
            payload = payload[written:]
        os.fsync(descriptor)
        os.fchmod(descriptor, 0o600)
    except BaseException:
        with contextlib.suppress(OSError):
            path.unlink()
        raise
    finally:
        os.close(descriptor)


def provider_tokens(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        raise WorkerError("The server supplied invalid provider credentials.")
    access = value.get("accessToken")
    account = value.get("chatgptAccountId")
    plan = value.get("chatgptPlanType")
    if not isinstance(access, str) or not access or len(access) > 100_000:
        raise WorkerError("The server supplied invalid provider credentials.")
    if not isinstance(account, str) or not account or len(account) > 1024:
        raise WorkerError("The server supplied invalid provider credentials.")
    if plan is not None and (not isinstance(plan, str) or len(plan) > 128):
        raise WorkerError("The server supplied invalid provider credentials.")
    result = {"accessToken": access, "chatgptAccountId": account}
    if plan:
        result["chatgptPlanType"] = plan
    return result


def image_extension(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    if len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return ".webp"
    raise WorkerError("The downloaded attachment is not a supported image.")


def write_private_upload(directory: Path, data: bytes, extension: str) -> Path:
    ensure_private_directory(directory)
    path = directory / f"{uuid.uuid4().hex}{extension}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    try:
        remaining = data
        while remaining:
            written = os.write(descriptor, remaining)
            remaining = remaining[written:]
        os.fsync(descriptor)
        os.fchmod(descriptor, 0o600)
    except BaseException:
        with contextlib.suppress(OSError):
            path.unlink()
        raise
    finally:
        os.close(descriptor)
    return path


class JsonChannel:
    def __init__(self, websocket: aiohttp.ClientWebSocketResponse) -> None:
        self.websocket = websocket
        self.lock = asyncio.Lock()

    async def send(self, packet: dict[str, Any]) -> None:
        try:
            message = json.dumps(packet, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError):
            raise WorkerError("Worker channel packet could not be encoded.") from None
        if len(message.encode("utf-8")) > MAX_WS_BYTES:
            raise PayloadLimitError(
                "Worker channel packet exceeded the 8 MiB safety limit."
            )
        async with self.lock:
            await self.websocket.send_str(message)


class CodexAppServer:
    def __init__(
        self,
        codex: Path,
        cwd: Path,
        state_dir: Path,
        on_event: Callable[[str, dict[str, Any]], Awaitable[None]],
        on_request: Callable[[Any, str, dict[str, Any]], Awaitable[None]],
        gateway=None,
    ) -> None:
        self.codex = codex
        self.cwd = cwd
        self.state_dir = state_dir
        self.on_event = on_event
        self.on_request = on_request
        self.gateway = gateway
        self.process: Optional[asyncio.subprocess.Process] = None
        self.reader_task: Optional[asyncio.Task[None]] = None
        self.stderr_task: Optional[asyncio.Task[None]] = None
        self.stderr_bytes = 0
        self.request_tasks: set[asyncio.Task[Any]] = set()
        self.pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self.next_id = 0
        self.write_lock = asyncio.Lock()
        self.closed = asyncio.Event()
        self.process_group: Optional[int] = None

    async def launch(self) -> None:
        ensure_private_directory(self.state_dir)
        if (self.state_dir / "auth.json").exists():
            raise WorkerError("Isolated worker state must not contain auth.json.")
        environment = dict(os.environ)
        environment["CODEX_HOME"] = str(self.state_dir)
        environment["LC_ALL"] = "C.UTF-8"
        for key in PROVIDER_ENVIRONMENT_KEYS:
            environment.pop(key, None)
        if self.gateway is not None:
            environment = self.gateway.environment(environment)
        self.process = await asyncio.create_subprocess_exec(
            str(self.codex),
            "app-server",
            "--stdio",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(self.cwd),
            env=environment,
            limit=MAX_WS_BYTES,
            start_new_session=True,
        )
        self.process_group = self.process.pid
        self.reader_task = asyncio.create_task(
            self._read_stdout(), name="codex-lan-app-server-reader"
        )
        self.stderr_task = asyncio.create_task(
            self._drain_stderr(), name="codex-lan-app-server-stderr"
        )

    async def initialize(self, tokens: dict[str, str]) -> tuple[list[dict[str, Any]], str]:
        result = await self.request(
            "initialize",
            {
                "clientInfo": {
                    "name": "codex_lan_worker",
                    "title": "Codex LAN Worker",
                    "version": __version__,
                },
                "capabilities": {"experimentalApi": True},
            },
            timeout=20,
        )
        await self.notify("initialized", {})
        login = {"type": "chatgptAuthTokens", **provider_tokens(tokens)}
        try:
            await self.request("account/login/start", login, timeout=20)
        except AppServerRpcError:
            raise WorkerError("Codex rejected the server-managed authentication session.") from None
        finally:
            login.clear()
        auth_path = self.state_dir / "auth.json"
        if auth_path.exists():
            with contextlib.suppress(OSError):
                auth_path.unlink()
            raise WorkerError("Codex unexpectedly tried to persist provider credentials.")
        try:
            data = await asyncio.wait_for(
                read_model_registry(self.request, timeout=MODEL_DISCOVERY_TIMEOUT_SECONDS),
                timeout=MODEL_DISCOVERY_TIMEOUT_SECONDS,
            )
        except ModelRegistryError as exc:
            raise WorkerError(str(exc)) from None
        except asyncio.TimeoutError:
            raise WorkerError("Codex model registry discovery timed out.") from None
        version = str(result.get("userAgent") or "") if isinstance(result, dict) else ""
        return data, version[:200]

    async def request(
        self, method: str, params: Optional[dict[str, Any]] = None, timeout: float = RPC_TIMEOUT_SECONDS
    ) -> dict[str, Any]:
        self.next_id += 1
        identifier = self.next_id
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self.pending[identifier] = future

        async def exchange():
            await self._write({"id": identifier, "method": method, "params": params or {}})
            return await future

        try:
            # CODEX: Include pipe backpressure and write-lock contention in the RPC deadline.
            packet = await asyncio.wait_for(exchange(), timeout)
        except asyncio.TimeoutError:
            raise WorkerError(f"Codex {method} request timed out.") from None
        finally:
            self.pending.pop(identifier, None)
            if not future.done():
                future.cancel()
            elif not future.cancelled():
                # CODEX: The reader may fail a request before its blocked write returns.
                future.exception()
        error = packet.get("error")
        if isinstance(error, dict):
            raise AppServerRpcError(str(error.get("message") or "Codex request failed.")[:2000])
        result = packet.get("result")
        return result if isinstance(result, dict) else {}

    async def notify(self, method: str, params: Optional[dict[str, Any]] = None) -> None:
        await self._write({"method": method, "params": params or {}})

    async def respond(self, identifier: Any, result: dict[str, Any]) -> None:
        await self._write({"id": identifier, "result": result})

    async def respond_error(self, identifier: Any, message: str) -> None:
        await self._write(
            {"id": identifier, "error": {"code": -32000, "message": message[:500]}}
        )

    async def _write(self, packet: dict[str, Any]) -> None:
        process = self.process
        if process is None or process.returncode is not None or process.stdin is None:
            raise ConnectionError("Codex app-server is not running.")
        encoded = (json.dumps(packet, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
        async with self.write_lock:
            process.stdin.write(encoded)
            await process.stdin.drain()

    async def _read_stdout(self) -> None:
        process = self.process
        assert process is not None and process.stdout is not None
        failure: Optional[BaseException] = None
        try:
            while True:
                try:
                    line = await process.stdout.readline()
                except ValueError:
                    raise PayloadLimitError(
                        "Codex app-server output exceeded the 8 MiB per-message safety limit; reconnecting safely."
                    ) from None
                if not line:
                    status = await process.wait()
                    suffix = " Diagnostic output was suppressed." if self.stderr_bytes else ""
                    raise WorkerError(
                        f"Codex app-server exited unexpectedly (status {status}).{suffix}"
                    )
                try:
                    packet = json.loads(line)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                if not isinstance(packet, dict):
                    continue
                identifier = packet.get("id")
                method = packet.get("method")
                if identifier is not None and not method:
                    future = self.pending.get(identifier)
                    if future is not None and not future.done():
                        future.set_result(packet)
                elif isinstance(method, str) and identifier is not None:
                    params = packet.get("params")
                    task = asyncio.create_task(
                        self.on_request(
                            identifier, method, params if isinstance(params, dict) else {}
                        )
                    )
                    self.request_tasks.add(task)
                    task.add_done_callback(self._request_task_done)
                elif isinstance(method, str):
                    params = packet.get("params")
                    await self.on_event(method, params if isinstance(params, dict) else {})
                    await asyncio.sleep(0)
        except BaseException as exc:
            failure = exc
            raise
        finally:
            error: BaseException = (
                failure
                if isinstance(failure, WorkerError)
                else ConnectionError("Codex app-server disconnected.")
            )
            for future in list(self.pending.values()):
                if not future.done():
                    future.set_exception(error)
            self.closed.set()
            if failure is not None and process.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    process.terminate()

    async def _drain_stderr(self) -> None:
        process = self.process
        assert process is not None and process.stderr is not None
        while True:
            chunk = await process.stderr.read(64 * 1024)
            if not chunk:
                return
            self.stderr_bytes += len(chunk)

    def _request_task_done(self, task: asyncio.Task[Any]) -> None:
        self.request_tasks.discard(task)
        if not task.cancelled():
            task.exception()

    async def wait_closed(self) -> None:
        await self.closed.wait()
        if self.reader_task is not None:
            await self.reader_task

    async def close(self) -> None:
        process = self.process
        if process is None:
            return
        for task in list(self.request_tasks):
            task.cancel()
        if self.request_tasks:
            await asyncio.gather(*self.request_tasks, return_exceptions=True)
        group = self.process_group
        if group is not None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(group, signal.SIGTERM)
        if process.returncode is None:
            try:
                await asyncio.wait_for(process.wait(), timeout=3)
            except asyncio.TimeoutError:
                if group is not None:
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(group, signal.SIGKILL)
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(process.wait(), timeout=2)
        if group is not None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(group, signal.SIGKILL)
        if self.reader_task is not None and not self.reader_task.done():
            self.reader_task.cancel()
        if self.reader_task is not None:
            await asyncio.gather(self.reader_task, return_exceptions=True)
        if self.stderr_task is not None and not self.stderr_task.done():
            self.stderr_task.cancel()
        if self.stderr_task is not None:
            await asyncio.gather(self.stderr_task, return_exceptions=True)
        self.process = None


class WorkerConnection:
    def __init__(
        self,
        config: WorkerConfig,
        session: aiohttp.ClientSession,
        device_token: str,
    ) -> None:
        self.config = config
        self.session = session
        self.device_token = device_token
        self.channel: Optional[JsonChannel] = None
        self.codex: Optional[CodexAppServer] = None
        self.refresh_waiters: dict[str, asyncio.Future[dict[str, str]]] = {}
        self.rpc_tasks: set[asyncio.Task[Any]] = set()
        self.active_rpc_ids: set[str] = set()
        route_state = getattr(config, "route_state", None)
        if route_state is None and getattr(config, "state_dir", None) is not None:
            route_state = Path(config.state_dir).parent / "egress.sqlite3"
        self.gateway = AdaptiveProxy(on_status=self._connection_status, route_state_path=route_state)
        self.turn_requests = {}
        self.native_turns = {}
        self.ended_turns = {}
        self.network_errors = {}
        self.recovery_tasks = {}
        self.recovery_attempts = {}
        self.stopped_threads = set()
        self.thread_connection = {}
        self.forwarded_command_output = {}
        self.command_output_omitted = set()
        self.transport_diagnostic = websocket_diagnostic()
        self.established = False

    @property
    def authorization(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.device_token}"}

    @staticmethod
    def _request_thread(params: dict[str, Any]) -> Optional[str]:
        """CODEX: Resolve both current and legacy app-server approval envelopes."""
        if not isinstance(params, dict):
            return None
        for key in ("threadId", "conversationId", "thread_id", "conversation_id"):
            value = params.get(key)
            if isinstance(value, str) and value:
                return value
        return None

    def _request_is_read_only(self, params: dict[str, Any]) -> bool:
        """CODEX: Bind an approval to the immutable sandbox snapshot of its active turn."""
        thread = self._request_thread(params)
        request = self.turn_requests.get(thread) if thread else None
        # CODEX: Approval envelopes without a known full-access turn must fail closed.
        return not (
            isinstance(request, dict)
            and isinstance(request.get("sandboxPolicy"), dict)
            and request["sandboxPolicy"].get("type") == "dangerFullAccess"
        )

    async def run(self) -> None:
        async with self.session.ws_connect(
            self.config.endpoints.worker,
            headers=self.authorization,
            origin=self.config.endpoints.origin,
            heartbeat=LAN_WEBSOCKET_HEARTBEAT_SECONDS,
            max_msg_size=MAX_WS_BYTES,
            compress=0,
        ) as websocket:
            self.channel = JsonChannel(websocket)
            first = await asyncio.wait_for(websocket.receive(), timeout=20)
            packet = self._decode_ws(first)
            if packet.get("type") != "auth":
                raise WorkerError("Worker channel did not begin with authentication.")
            tokens = provider_tokens(packet.get("tokens"))
            packet.clear()
            websocket_task = asyncio.create_task(
                self._read_websocket(websocket), name="codex-lan-websocket-reader"
            )
            try:
                await self.gateway.start()
                self.codex = CodexAppServer(
                    self.config.codex,
                    self.config.cwd,
                    self.config.state_dir,
                    self._send_event,
                    self._handle_app_request,
                    gateway=self.gateway,
                )
                await self.codex.launch()
                models, version = await self.codex.initialize(tokens)
                tokens.clear()
                ready: dict[str, Any] = {
                    "type": "ready",
                    "models": models,
                    "cwd": str(self.config.cwd),
                    "client_version": __version__,
                    "network_recovery": True,
                }
                if version:
                    ready["version"] = version
                await self.channel.send(ready)
                self.established = True
                app_task = asyncio.create_task(
                    self.codex.wait_closed(), name="codex-lan-app-server-wait"
                )
                done, pending = await asyncio.wait(
                    {websocket_task, app_task}, return_when=asyncio.FIRST_COMPLETED
                )
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                for task in done:
                    await task
            finally:
                self.transport_diagnostic = websocket_diagnostic(websocket)
                tokens.clear()
                websocket_task.cancel()
                await asyncio.gather(websocket_task, return_exceptions=True)
                await self._cancel_tasks()
                if self.codex is not None:
                    await self.codex.close()
                await self.gateway.close()

    @staticmethod
    def _decode_ws(frame: aiohttp.WSMessage) -> dict[str, Any]:
        if frame.type != aiohttp.WSMsgType.TEXT:
            raise ConnectionError("Worker channel closed.")
        try:
            packet = json.loads(frame.data)
        except (json.JSONDecodeError, TypeError):
            raise WorkerError("Server sent invalid worker-channel JSON.") from None
        if not isinstance(packet, dict):
            raise WorkerError("Server sent an invalid worker-channel packet.")
        return packet

    async def _read_websocket(self, websocket: aiohttp.ClientWebSocketResponse) -> None:
        async for frame in websocket:
            if frame.type == aiohttp.WSMsgType.TEXT:
                packet = self._decode_ws(frame)
                await self._handle_server_packet(packet)
            elif frame.type in {
                aiohttp.WSMsgType.CLOSE,
                aiohttp.WSMsgType.CLOSED,
                aiohttp.WSMsgType.ERROR,
            }:
                break

    async def _handle_server_packet(self, packet: dict[str, Any]) -> None:
        kind = packet.get("type")
        if kind == "auth_response":
            identifier = str(packet.get("id") or "")
            future = self.refresh_waiters.get(identifier)
            if future is not None and not future.done():
                try:
                    future.set_result(provider_tokens(packet.get("tokens")))
                except WorkerError as exc:
                    future.set_exception(exc)
            return
        if kind != "rpc":
            return
        identifier = packet.get("id")
        if not isinstance(identifier, str) or not identifier:
            return
        if identifier in self.active_rpc_ids:
            await self._send_result(identifier, error="Duplicate worker request ID.")
            return
        self.active_rpc_ids.add(identifier)
        task = asyncio.create_task(self._relay_rpc(identifier, packet))
        self.rpc_tasks.add(task)
        task.add_done_callback(self._rpc_task_done)

    def _rpc_task_done(self, task: asyncio.Task[Any]) -> None:
        self.rpc_tasks.discard(task)
        if not task.cancelled():
            task.exception()

    async def _relay_rpc(self, identifier: str, packet: dict[str, Any]) -> None:
        steer_dispatched = False
        try:
            method = packet.get("method")
            params = packet.get("params")
            if not isinstance(method, str) or not isinstance(params, dict):
                raise WorkerError("Invalid worker request.")
            if method == "lan/upload":
                result = await self._download_attachment(params)
            elif method == "lan/cancel":
                thread = params.get("threadId")
                if not isinstance(thread, str) or thread not in self.turn_requests:
                    raise WorkerError("No active response exists on this worker thread.")
                self.stopped_threads.add(thread)
                recovering = self.recovery_tasks.pop(thread, None)
                if recovering is not None:
                    recovering.cancel()
                    await asyncio.gather(recovering, return_exceptions=True)
                turn = self.native_turns.get(thread)
                if turn and turn != self.ended_turns.get(thread) and self.codex is not None:
                    await self.codex.request("turn/interrupt", {"threadId": thread, "turnId": turn})
                if recovering is not None:
                    self.turn_requests.pop(thread, None)
                    self.thread_connection.pop(thread, None)
                    self.network_errors.pop(thread, None)
                    self.recovery_attempts.pop(thread, None)
                result = {"recovering": recovering is not None, "turnId": turn}
            elif method in ALLOWED_RPC_METHODS:
                if method == "turn/steer":
                    if (set(params) != {"threadId", "expectedTurnId", "input"}
                            or not all(isinstance(params.get(key), str) and params[key] for key in ("threadId", "expectedTurnId"))
                            or not isinstance(params.get("input"), list) or not params["input"]):
                        raise WorkerError("Invalid active-turn follow-up request.")
                    for value in params["input"]:
                        if (not isinstance(value, dict) or value.get("type") not in {"text", "localImage"}
                                or not isinstance(value.get("text" if value.get("type") == "text" else "path"), str)):
                            raise WorkerError("Invalid active-turn follow-up input.")
                if self.codex is None:
                    raise WorkerError("Codex app-server is still starting.")
                if method == "turn/start":
                    thread = params.get("threadId")
                    if isinstance(thread, str):
                        self.stopped_threads.discard(thread)
                        self.turn_requests[thread] = dict(params)
                        self.network_errors.pop(thread, None)
                        self.native_turns.pop(thread, None)
                        self.ended_turns.pop(thread, None)
                        self.recovery_attempts.pop(thread, None)
                        self.thread_connection[thread] = "connecting"
                        await self._connection_status({**self.gateway.snapshot(), "state": "connecting", "scope": thread})
                steer_dispatched = method == "turn/steer"
                result = await self.codex.request(method, params)
            else:
                raise WorkerError(f"Worker method is not allowed: {method}")
            await self._send_result(identifier, result=result)
        except asyncio.CancelledError:
            raise
        except (WorkerError, ConnectionError, OSError, aiohttp.ClientError) as exc:
            await self._send_result(identifier, error=str(exc)[:500],
                                    delivery_uncertain=steer_dispatched and not isinstance(exc, AppServerRpcError))
        except Exception:
            await self._send_result(
                identifier,
                error="The connector could not complete this request because of an internal error. Retry after checking its connection.",
                delivery_uncertain=steer_dispatched,
            )
        finally:
            self.active_rpc_ids.discard(identifier)

    async def _send_result(
        self,
        identifier: str,
        result: Optional[dict[str, Any]] = None,
        error: Optional[str] = None,
        delivery_uncertain: bool = False,
    ) -> None:
        if self.channel is None:
            return
        packet: dict[str, Any] = {"type": "result", "id": identifier}
        if error is not None:
            packet["error"] = {"message": error}
            if delivery_uncertain:
                packet["error"]["delivery_uncertain"] = True
        else:
            packet["result"] = result or {}
        await self.channel.send(packet)

    async def _forward_event(self, method, params):
        if self.channel is not None:
            await self.channel.send(
                {"type": "event", "method": method, "params": params}
            )

    async def _connection_status(self, connection):
        scope = connection.get("scope")
        values = [scope] if scope in self.turn_requests else list(self.turn_requests)
        if not values:
            await self._forward_event("lan/connection", {"connection": connection})
        for thread in values:
            if thread in self.stopped_threads:
                continue
            # CODEX: A healthy room's existing tunnel is not disconnected by another room's route change.
            if scope is None and self.thread_connection.get(thread) == "connected":
                continue
            self.thread_connection[thread] = connection["state"]
            await self._forward_event("lan/connection", {"threadId": thread, "connection": connection})

    async def _send_event(self, method: str, params: dict[str, Any]) -> None:
        thread = params.get("threadId")
        turn = params.get("turn") or {}
        turn_id = params.get("turnId") or turn.get("id")
        if method == "turn/started" and isinstance(thread, str):
            self.native_turns[thread] = turn_id
            if thread in self.stopped_threads:
                # CODEX: The reader cannot await its own RPC response; cancel a racing resume asynchronously.
                task = asyncio.create_task(self.codex.request("turn/interrupt", {"threadId": thread, "turnId": turn_id}))
                self.rpc_tasks.add(task)
                task.add_done_callback(self._rpc_task_done)
        current = thread in self.turn_requests and thread not in self.stopped_threads
        if turn_id and self.native_turns.get(thread) and turn_id != self.native_turns[thread]:
            current = False
        if method == "error" and current and network_error(params.get("error")):
            self.network_errors[thread] = params["error"]
            await self.gateway.report_failure(scope=thread)
            if not params.get("willRetry"):
                return
        if method == "turn/completed" and current and turn.get("status") == "failed":
            error = turn.get("error") or self.network_errors.get(thread)
            if network_error(error):
                if thread in self.ended_turns and self.ended_turns[thread] == turn_id:
                    return
                self.ended_turns[thread] = turn_id
                if thread not in self.network_errors:
                    await self.gateway.report_failure(scope=thread)
                attempts = self.recovery_attempts.get(thread, 0)
                if attempts < MAX_AUTOMATIC_RECOVERY_TURNS:
                    self.recovery_attempts[thread] = attempts + 1
                    self._schedule_recovery(thread, turn_id)
                    return
                params = {**params, "turn": {**turn, "error": {"message": (
                    "The connection failed again after one automatic recovery. "
                    "Retry manually after the connection is stable."
                )}}}
        progress = (method in {"item/agentMessage/delta", "item/reasoning/summaryTextDelta", "thread/tokenUsage/updated"}
                    or method in {"item/started", "item/completed"} and (params.get("item") or {}).get("type") not in {None, "userMessage"})
        if current and progress and self.thread_connection.get(thread) != "connected":
            self.network_errors.pop(thread, None)
            await self.gateway.report_success(scope=thread)
        forwarded = self._bounded_command_output_event(method, params)
        if forwarded is not None:
            await self._forward_event(method, forwarded)
        if method == "turn/completed" and thread in self.turn_requests and (not turn_id or self.native_turns.get(thread) == turn_id):
            self.turn_requests.pop(thread, None)
            self.thread_connection.pop(thread, None)
            self.network_errors.pop(thread, None)
            self.native_turns.pop(thread, None)
            self.ended_turns.pop(thread, None)
            self.recovery_attempts.pop(thread, None)
            self._forget_command_output_thread(thread)

    @staticmethod
    def _command_output_key(params, identifier):
        thread = params.get("threadId")
        return (thread if isinstance(thread, str) else "", identifier)

    def _forget_command_output_thread(self, thread):
        """CODEX: Release one completed turn's limits without resetting concurrent turns."""
        if not isinstance(thread, str):
            return
        keys = [key for key in self.forwarded_command_output if key[0] == thread]
        for key in keys:
            self.forwarded_command_output.pop(key, None)
            self.command_output_omitted.discard(key)

    def _bounded_command_output_event(self, method, params):
        """CODEX: Bound noisy command streams before they can starve the LAN server."""
        if method == "item/commandExecution/outputDelta":
            identifier = params.get("itemId")
            delta = params.get("delta")
            if not isinstance(identifier, str) or not identifier or not isinstance(delta, str) or not delta:
                return None
            key = self._command_output_key(params, identifier)
            seen = self.forwarded_command_output.get(key, 0)
            if seen >= MAX_FORWARDED_COMMAND_OUTPUT:
                if key in self.command_output_omitted:
                    return None
                self.command_output_omitted.add(key)
                return {**params, "delta": OUTPUT_OMITTED}
            remaining = MAX_FORWARDED_COMMAND_OUTPUT - seen
            accepted = delta[:remaining]
            self.forwarded_command_output[key] = seen + len(accepted)
            if len(delta) > remaining:
                self.command_output_omitted.add(key)
                accepted += OUTPUT_OMITTED
            return {**params, "delta": accepted}
        if method == "item/completed":
            item = params.get("item")
            if isinstance(item, dict) and item.get("type") == "commandExecution":
                identifier = item.get("id")
                if isinstance(identifier, str):
                    key = self._command_output_key(params, identifier)
                    self.forwarded_command_output.pop(key, None)
                    self.command_output_omitted.discard(key)
                output = item.get("aggregatedOutput")
                if isinstance(output, str) and len(output) > MAX_FORWARDED_COMMAND_OUTPUT:
                    item = {
                        **item,
                        "aggregatedOutput": OUTPUT_OMITTED + output[-MAX_FORWARDED_COMMAND_OUTPUT:],
                    }
                    return {**params, "item": item}
        return params

    def _schedule_recovery(self, thread, turn_id):
        pending = self.recovery_tasks.get(thread)
        if pending is not None and not pending.done():
            return
        task = asyncio.create_task(self._recover_turn(thread, turn_id))
        self.recovery_tasks[thread] = task

    async def _recover_turn(self, thread, previous_turn):
        try:
            await asyncio.sleep(2)
            # CODEX: Real provider traffic is authoritative; anonymous preflight requests may be blocked.
            if thread in self.stopped_threads or thread not in self.turn_requests:
                return
            request = dict(self.turn_requests[thread])
            # CODEX: Continue saved history; never replay the original request or completed tool commands.
            request["input"] = [{"type": "text", "text": CONTINUE_AFTER_CONNECTION}]
            await self._forward_event("lan/turnResuming", {"threadId": thread, "previousTurnId": previous_turn})
            self.thread_connection[thread] = "resuming"
            await self._connection_status({**self.gateway.snapshot(), "state": "resuming", "scope": thread})
            result = await self.codex.request("turn/start", request)
            if (result.get("turn", {}).get("id")
                    and result["turn"]["id"] != self.ended_turns.get(thread)
                    and thread in self.turn_requests):
                self.native_turns[thread] = result["turn"]["id"]
        except asyncio.CancelledError:
            raise
        except (WorkerError, OSError, ConnectionError) as exc:
            # CODEX: Uncertain local RPC delivery is not safe to replay automatically.
            await self._forward_event("turn/completed", {"threadId": thread, "turn": {
                "id": None, "status": "failed",
                "error": {"message": "The local connector could not confirm recovery. Reconnect the connector to continue."}}})
            self.turn_requests.pop(thread, None)
            self.thread_connection.pop(thread, None)
            self.network_errors.pop(thread, None)
            self.recovery_attempts.pop(thread, None)
        finally:
            if self.recovery_tasks.get(thread) is asyncio.current_task():
                self.recovery_tasks.pop(thread, None)

    async def _handle_app_request(
        self, identifier: Any, method: str, params: dict[str, Any]
    ) -> None:
        codex = self.codex
        if codex is None:
            return
        read_only = self._request_is_read_only(params)
        if method in COMMAND_APPROVAL_METHODS:
            if read_only and method == "item/commandExecution/requestApproval":
                await codex.respond(identifier, {"decision": "decline"})
            elif read_only:
                await codex.respond(identifier, {"decision": {"denied": {
                    "rejection": "Read-only mode does not allow commands that require elevated access."
                }}})
            else:
                await codex.respond(identifier, {"decision": COMMAND_APPROVAL_METHODS[method]})
            return
        if method in FILE_APPROVAL_METHODS:
            if read_only and method == "item/fileChange/requestApproval":
                await codex.respond(identifier, {"decision": "decline"})
            elif read_only:
                await codex.respond(identifier, {"decision": {"denied": {
                    "rejection": "Read-only mode does not allow file changes."
                }}})
            else:
                await codex.respond(identifier, {"decision": FILE_APPROVAL_METHODS[method]})
            return
        if method == "item/permissions/requestApproval" and read_only:
            await codex.respond(identifier, {"permissions": {}})
            return
        if method == "account/chatgptAuthTokens/refresh":
            await self._refresh_auth(identifier)
            return
        if self.channel is not None:
            await self.channel.send(
                {
                    "type": "request",
                    "id": str(identifier),
                    "method": method,
                    "params": params,
                }
            )
        if method == "mcpServer/elicitation/request":
            await codex.respond(identifier, {"action": "decline", "content": None})
        else:
            await codex.respond_error(identifier, f"Unsupported client request: {method}")

    async def _refresh_auth(self, app_identifier: Any) -> None:
        if self.channel is None or self.codex is None:
            return
        identifier = uuid.uuid4().hex
        future: asyncio.Future[dict[str, str]] = asyncio.get_running_loop().create_future()
        self.refresh_waiters[identifier] = future
        tokens = {}

        async def exchange():
            nonlocal tokens
            await self.channel.send({"type": "auth_refresh", "id": identifier})
            tokens = await future
            await self.codex.respond(app_identifier, tokens)

        try:
            # CODEX: Bound the entire refresh exchange, including blocked network and native pipe writes.
            await asyncio.wait_for(exchange(), timeout=REFRESH_TIMEOUT_SECONDS)
        except (asyncio.TimeoutError, WorkerError, ConnectionError, aiohttp.ClientError):
            with contextlib.suppress(asyncio.TimeoutError, WorkerError, ConnectionError, aiohttp.ClientError):
                await asyncio.wait_for(self.codex.respond_error(
                    app_identifier, "Server-managed authentication refresh failed."
                ), timeout=REFRESH_TIMEOUT_SECONDS)
        finally:
            tokens.clear()
            self.refresh_waiters.pop(identifier, None)
            if not future.done():
                future.cancel()
            elif not future.cancelled():
                future.exception()

    async def _download_attachment(self, params: dict[str, Any]) -> dict[str, str]:
        attachment_id = params.get("attachment_id")
        if not isinstance(attachment_id, str) or not attachment_id or len(attachment_id) > 256:
            raise WorkerError("Invalid attachment ID.")
        url = self.config.endpoints.attachments + "/" + quote(attachment_id, safe="")
        async with self.session.get(
            url, headers=self.authorization, allow_redirects=False
        ) as response:
            if response.status in {401, 403}:
                raise DeviceAccessError("Device authorization was rejected.")
            if response.status != 200:
                raise WorkerError(f"Attachment download failed with HTTP {response.status}.")
            if response.content_length is not None and response.content_length > MAX_ATTACHMENT_BYTES:
                raise WorkerError("Attachment exceeds the 25 MiB limit.")
            name = response.content_disposition.filename if response.content_disposition else "attachment"
            data = bytearray()
            async for chunk in response.content.iter_chunked(64 * 1024):
                data.extend(chunk)
                if len(data) > MAX_ATTACHMENT_BYTES:
                    raise WorkerError("Attachment exceeds the 25 MiB limit.")
        content = bytes(data)
        extension = attachment_extension(name, attachment_mime(content))
        path = await asyncio.to_thread(write_private_upload, self.config.state_dir / "uploads", content, extension)
        return {"path": str(path)}

    async def _cancel_tasks(self) -> None:
        self.stopped_threads.update(self.turn_requests)
        self.recovery_attempts.clear()
        recovery = list(self.recovery_tasks.values())
        self.recovery_tasks.clear()
        for task in recovery:
            task.cancel()
        if recovery:
            await asyncio.gather(*recovery, return_exceptions=True)
        for future in self.refresh_waiters.values():
            if not future.done():
                future.cancel()
        self.refresh_waiters.clear()
        for task in list(self.rpc_tasks):
            task.cancel()
        if self.rpc_tasks:
            await asyncio.gather(*self.rpc_tasks, return_exceptions=True)
        self.rpc_tasks.clear()
        self.active_rpc_ids.clear()


class ExecutorConnection:
    """CODEX: Run only native tool execution locally; the host owns all model work."""

    def __init__(self, config: WorkerConfig, session: aiohttp.ClientSession, device_token: str):
        self.config = config
        self.session = session
        self.device_token = device_token
        self.process: Optional[asyncio.subprocess.Process] = None
        self.process_group: Optional[int] = None
        self.tasks: set[asyncio.Task[Any]] = set()
        self.artifact_tasks: set[asyncio.Task[Any]] = set()
        self.artifact_locks = tuple(asyncio.Lock() for _ in range(MAX_ARTIFACT_TRANSFERS))
        self.channel: Optional[JsonChannel] = None
        self.stderr_bytes = 0
        self.transport_diagnostic = websocket_diagnostic()
        self.established = False

    @property
    def authorization(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.device_token}"}

    @property
    def endpoint(self) -> str:
        parsed = urlsplit(self.config.endpoints.base)
        return urlunsplit((
            "wss" if parsed.scheme == "https" else "ws", parsed.netloc,
            parsed.path.rstrip("/") + "/api/executor", "", "",
        ))

    @staticmethod
    def environment(state_dir: Path) -> dict[str, str]:
        # CODEX: Native exec-server needs no provider identity or client Internet proxy.
        proxy_keys = {"http_proxy", "https_proxy", "all_proxy", "no_proxy"}
        environment = {
            key: value for key, value in os.environ.items()
            if key.lower() not in proxy_keys
            and key.upper() not in PROVIDER_ENVIRONMENT_KEYS
            and not key.upper().startswith(("OPENAI_", "CODEX_", "CHATGPT_", "AZURE_OPENAI_"))
        }
        environment["CODEX_HOME"] = str(state_dir)
        environment["LC_ALL"] = "C.UTF-8"
        return environment

    async def run(self) -> None:
        async def reject_redirect(_session, _context, _params):
            # CODEX: aiohttp WebSocket upgrades otherwise follow HTTP redirects implicitly.
            raise WorkerError("Executor connection refused a server redirect; check the server URL.")

        trace = aiohttp.TraceConfig()
        trace.on_request_redirect.append(reject_redirect)
        # CODEX: Reuse the pinned-CA connector, but never inherit proxy, cookie or credential state.
        async with aiohttp.ClientSession(
            connector=self.session.connector, connector_owner=False,
            timeout=self.session.timeout, trust_env=False, trace_configs=[trace],
        ) as session:
            try:
                async with session.ws_connect(
                    self.endpoint, headers={"Authorization": f"Bearer {self.device_token}"},
                    origin=self.config.endpoints.origin,
                    heartbeat=LAN_WEBSOCKET_HEARTBEAT_SECONDS,
                    max_msg_size=MAX_WS_BYTES, compress=0,
                ) as websocket:
                    self.channel = JsonChannel(websocket)
                    try:
                        await self._launch()
                        await self.channel.send({
                            "type": "executor_hello", "version": __version__,
                            "cwd": str(self.config.cwd.resolve(strict=True)), "protocol": 1,
                            "capabilities": [ARTIFACT_STORE_CAPABILITY, FILE_ATTACHMENTS_CAPABILITY],
                        })
                        self.tasks = {
                            asyncio.create_task(self._read_stdout(), name="codex-lan-executor-output"),
                            asyncio.create_task(self._read_websocket(websocket), name="codex-lan-executor-input"),
                            asyncio.create_task(self._drain_stderr(), name="codex-lan-executor-stderr"),
                        }
                        # CODEX: stderr EOF alone is harmless; stdout or LAN EOF ends the owned executor.
                        active = set(self.tasks)
                        while active:
                            done, active = await asyncio.wait(active, return_when=asyncio.FIRST_COMPLETED)
                            for task in done:
                                await task
                                if task.get_name() != "codex-lan-executor-stderr":
                                    raise ConnectionError("Executor connection closed; waiting to reconnect to the server.")
                    finally:
                        self.transport_diagnostic = websocket_diagnostic(websocket)
                        await self.close()
            except aiohttp.WSServerHandshakeError as exc:
                if exc.status in {401, 403}:
                    raise DeviceAccessError("Device authorization was rejected or revoked.") from None
                if exc.status in {404, 405, 426}:
                    raise ExecutorUpgradeRequired(
                        "This server does not support host-managed execution. Update the server and connector; "
                        "the client will not start a local model session."
                    ) from None
                raise WorkerError(f"Executor connection is unavailable (HTTP {exc.status}).") from None
            finally:
                self.channel = None

    async def _launch(self) -> None:
        state_dir = ensure_private_directory(self.config.state_dir / "executor")
        if os.path.lexists(state_dir / "auth.json"):
            raise WorkerError("Isolated executor state must not contain provider credentials.")
        self.process = await asyncio.create_subprocess_exec(
            str(self.config.codex), "exec-server", "--listen", "stdio",
            "--concurrent-requests", "16", stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            cwd=str(self.config.cwd), env=self.environment(state_dir),
            limit=MAX_WS_BYTES, start_new_session=True,
        )
        self.process_group = self.process.pid

    @staticmethod
    def _native_line(value: bytes) -> bytes:
        if not value or len(value) > MAX_WS_BYTES:
            raise PayloadLimitError("Executor message exceeded the 8 MiB safety limit.")
        if not value.endswith(b"\n") or b"\n" in value[:-1]:
            raise WorkerError("Executor transport received an incomplete or combined RPC message.")
        try:
            packet = json.loads(value)
        except (ValueError, UnicodeDecodeError, RecursionError):
            raise WorkerError("Executor transport received invalid JSON.") from None
        if not isinstance(packet, dict):
            raise WorkerError("Executor transport received an invalid RPC message.")
        return value

    async def _read_stdout(self) -> None:
        assert self.process is not None and self.process.stdout is not None
        assert self.channel is not None
        while True:
            try:
                line = await self.process.stdout.readline()
            except ValueError:
                raise PayloadLimitError("Executor output exceeded the 8 MiB safety limit.") from None
            if not line:
                raise WorkerError("Local executor stopped; reconnecting to the server is required.")
            line = self._native_line(line)
            async with self.channel.lock:
                await self.channel.websocket.send_bytes(line)

    async def _read_websocket(self, websocket: aiohttp.ClientWebSocketResponse) -> None:
        assert self.process is not None and self.process.stdin is not None
        async for message in websocket:
            if message.type == aiohttp.WSMsgType.BINARY:
                line = self._native_line(message.data)
                self.process.stdin.write(line)
                await self.process.stdin.drain()
            elif message.type == aiohttp.WSMsgType.TEXT:
                if len(message.data.encode("utf-8")) > MAX_JSON_BYTES:
                    raise PayloadLimitError("Executor control message exceeded its safety limit.")
                try:
                    packet = json.loads(message.data)
                except (ValueError, UnicodeDecodeError, RecursionError):
                    raise WorkerError("Server sent an invalid executor control message.") from None
                if not isinstance(packet, dict):
                    raise WorkerError("Server sent an invalid executor control message.")
                if packet.get("type") == "history/read":
                    await self._reply_history(packet)
                elif packet.get("type") == "artifact/store":
                    await self._start_artifact_store(packet)
                elif packet.get("type") == "attachment/store":
                    await self._start_attachment_store(packet)
                elif packet.get("type") == "error":
                    # CODEX: Never echo untrusted error payloads that may contain credentials or commands.
                    if packet.get("code") in {"revoked", "unauthorized"}:
                        raise DeviceAccessError("Device authorization was rejected or revoked.")
                    raise WorkerError("The server rejected the executor connection; check server status.")
                elif packet.get("type") == "executor_ready":
                    self.established = True
                elif packet.get("type") != "status":
                    raise WorkerError("Server sent an unsupported executor control message.")
            elif message.type == aiohttp.WSMsgType.ERROR:
                raise ConnectionError("Executor connection was interrupted; reconnecting to the server.")

    async def _start_attachment_store(self, packet: dict[str, Any]) -> None:
        """CODEX: Fetch user uploads on their own executor, never via host filesystem RPCs."""
        assert self.channel is not None
        try:
            if uuid.UUID(hex=packet["id"]).hex != packet["id"] or str(uuid.UUID(packet["attachment_id"])) != packet["attachment_id"]:
                raise ValueError()
        except (KeyError, ValueError, TypeError, AttributeError):
            raise WorkerError("Invalid attachment transfer identifiers.") from None
        if len(self.artifact_tasks) >= MAX_ARTIFACT_TRANSFERS:
            await self.channel.send({"type": "attachment/result", "id": packet["id"], "status": "failed"})
            return
        task = asyncio.create_task(self._store_user_attachment(packet), name="codex-lan-attachment-store")
        self.artifact_tasks.add(task)
        def finished(done):
            self.artifact_tasks.discard(done)
            if not done.cancelled():
                done.exception()
        task.add_done_callback(finished)

    async def _store_user_attachment(self, packet: dict[str, Any]) -> None:
        assert self.channel is not None
        result = {"type": "attachment/result", "id": packet["id"], "attachment_id": packet["attachment_id"]}
        try:
            uploaded = await asyncio.wait_for(WorkerConnection._download_attachment(self, packet), timeout=90)
            result.update(status="stored", path=uploaded["path"])
        except (OSError, WorkerError, aiohttp.ClientError, asyncio.TimeoutError):
            result["status"] = "failed"
        await self.channel.send(result)

    async def _drain_stderr(self) -> None:
        assert self.process is not None and self.process.stderr is not None
        while True:
            value = await self.process.stderr.read(64 * 1024)
            if not value:
                return
            self.stderr_bytes += len(value)

    async def _reply_history(self, packet: dict[str, Any]) -> None:
        assert self.channel is not None
        identifier = packet.get("id")
        if not isinstance(identifier, str) or not identifier or len(identifier) > 128:
            raise WorkerError("Server supplied an invalid history request identifier.")
        result: dict[str, Any] = {"type": "history/result", "id": identifier, "content": None}
        try:
            result["content"] = await asyncio.to_thread(self.read_history, packet.get("thread_id"))
            await self.channel.send(result)
        except (WorkerError, OSError):
            await self.channel.send({
                "type": "history/result", "id": identifier, "content": None,
                "error": "Local session history could not be safely transferred.",
            })

    @staticmethod
    def _artifact_request(packet: dict[str, Any]) -> dict[str, Any]:
        identifier = packet.get("id")
        try:
            valid_request_id = isinstance(identifier, str) and uuid.UUID(hex=identifier).hex == identifier
        except (ValueError, AttributeError):
            valid_request_id = False
        if not valid_request_id:
            raise WorkerError("Server supplied an invalid artifact request identifier.")
        try:
            attachment_id = str(uuid.UUID(packet.get("attachment_id")))
            thread_id = str(uuid.UUID(packet.get("thread_id")))
        except (ValueError, TypeError, AttributeError):
            raise WorkerError("Server supplied invalid artifact identifiers.") from None
        if attachment_id != packet.get("attachment_id") or thread_id != packet.get("thread_id"):
            raise WorkerError("Server supplied invalid artifact identifiers.")
        name = packet.get("name")
        mime = packet.get("mime")
        size = packet.get("size")
        sha256 = packet.get("sha256")
        if (not isinstance(name, str) or not name or len(name) > 512
                or any(ord(character) < 32 or ord(character) == 127 for character in name)):
            raise WorkerError("Server supplied an invalid artifact name.")
        if mime not in ARTIFACT_EXTENSIONS:
            raise WorkerError("Server supplied an unsupported artifact type.")
        if (isinstance(size, bool) or not isinstance(size, int)
                or not 0 < size <= MAX_GENERATED_ARTIFACT_BYTES):
            raise WorkerError("Server supplied an invalid artifact size.")
        if (not isinstance(sha256, str) or len(sha256) != 64
                or any(character not in "0123456789abcdef" for character in sha256)):
            raise WorkerError("Server supplied an invalid artifact digest.")
        return {
            "id": identifier,
            "attachment_id": attachment_id,
            "thread_id": thread_id,
            "name": name,
            "mime": mime,
            "size": size,
            "sha256": sha256,
        }

    async def _start_artifact_store(self, packet: dict[str, Any]) -> None:
        assert self.channel is not None
        request = self._artifact_request(packet)
        if len(self.artifact_tasks) >= MAX_ARTIFACT_TRANSFERS:
            await self.channel.send({
                "type": "artifact/result", "id": request["id"],
                "status": "failed", "error": "busy",
            })
            return
        task = asyncio.create_task(
            self._store_artifact(request), name="codex-lan-artifact-store"
        )
        self.artifact_tasks.add(task)

        def finished(done: asyncio.Task[Any]) -> None:
            self.artifact_tasks.discard(done)
            if not done.cancelled():
                done.exception()

        task.add_done_callback(finished)

    @staticmethod
    def _verified_artifact(
        directory: Path, filename: str, size: int, sha256: str, extension: str,
    ) -> Optional[Path]:
        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(directory, directory_flags)
        try:
            try:
                file_descriptor = os.open(
                    filename,
                    os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0),
                    dir_fd=descriptor,
                )
            except FileNotFoundError:
                return None
            except OSError:
                raise ArtifactTransferError("conflict") from None
            try:
                metadata = os.fstat(file_descriptor)
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_size != size:
                    raise ArtifactTransferError("conflict")
                digest = hashlib.sha256()
                prefix = b""
                total = 0
                while True:
                    chunk = os.read(file_descriptor, 64 * 1024)
                    if not chunk:
                        break
                    if len(prefix) < 12:
                        prefix = (prefix + chunk)[:12]
                    total += len(chunk)
                    if total > MAX_GENERATED_ARTIFACT_BYTES:
                        raise ArtifactTransferError("conflict")
                    digest.update(chunk)
            finally:
                os.close(file_descriptor)
        finally:
            os.close(descriptor)
        try:
            detected = image_extension(prefix)
        except WorkerError:
            raise ArtifactTransferError("conflict") from None
        if total != size or digest.hexdigest() != sha256 or detected != extension:
            raise ArtifactTransferError("conflict")
        return directory / filename

    async def _download_artifact(self, request: dict[str, Any]) -> Path:
        root = ensure_private_directory(self.config.state_dir)
        generated = ensure_private_directory(root / "generated_images")
        directory = ensure_private_directory(generated / request["thread_id"])
        extension = ARTIFACT_EXTENSIONS[request["mime"]]
        filename = request["attachment_id"] + extension
        existing = await asyncio.to_thread(
            self._verified_artifact, directory, filename, request["size"], request["sha256"], extension
        )
        if existing is not None:
            return existing

        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        directory_descriptor = os.open(directory, directory_flags)
        temporary: Optional[str] = ".artifact-" + uuid.uuid4().hex + ".partial"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        try:
            file_descriptor = os.open(temporary, flags, 0o600, dir_fd=directory_descriptor)
        except BaseException:
            os.close(directory_descriptor)
            raise
        total = 0
        digest = hashlib.sha256()
        prefix = b""
        try:
            url = self.config.endpoints.attachments + "/" + quote(request["attachment_id"], safe="")
            headers = {
                "Authorization": f"Bearer {self.device_token}",
                "Accept": request["mime"],
                "Accept-Encoding": "identity",
            }
            async with self.session.get(url, headers=headers, allow_redirects=False) as response:
                if response.status in {401, 403}:
                    raise ArtifactTransferError("unauthorized")
                if response.status != 200:
                    raise ArtifactTransferError("unavailable")
                content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
                if content_type != request["mime"]:
                    raise ArtifactTransferError("type_mismatch")
                if response.content_length is not None and response.content_length != request["size"]:
                    raise ArtifactTransferError("size_mismatch")
                async for chunk in response.content.iter_chunked(64 * 1024):
                    total += len(chunk)
                    if total > request["size"] or total > MAX_GENERATED_ARTIFACT_BYTES:
                        raise ArtifactTransferError("size_mismatch")
                    if len(prefix) < 12:
                        prefix = (prefix + chunk)[:12]
                    digest.update(chunk)
                    remaining = chunk
                    while remaining:
                        written = os.write(file_descriptor, remaining)
                        remaining = remaining[written:]
            if total != request["size"]:
                raise ArtifactTransferError("size_mismatch")
            if digest.hexdigest() != request["sha256"]:
                raise ArtifactTransferError("hash_mismatch")
            try:
                detected = image_extension(prefix)
            except WorkerError:
                raise ArtifactTransferError("type_mismatch") from None
            if detected != extension:
                raise ArtifactTransferError("type_mismatch")
            os.fchmod(file_descriptor, 0o600)
            os.fsync(file_descriptor)
            try:
                os.link(
                    temporary, filename,
                    src_dir_fd=directory_descriptor, dst_dir_fd=directory_descriptor,
                    follow_symlinks=False,
                )
            except FileExistsError:
                existing = await asyncio.to_thread(
                    self._verified_artifact, directory, filename, request["size"], request["sha256"], extension
                )
                if existing is None:
                    raise ArtifactTransferError("conflict")
            os.unlink(temporary, dir_fd=directory_descriptor)
            temporary = None
            with contextlib.suppress(OSError):
                os.fsync(directory_descriptor)
            return directory / filename
        finally:
            os.close(file_descriptor)
            if temporary is not None:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(temporary, dir_fd=directory_descriptor)
            os.close(directory_descriptor)

    async def _store_artifact(self, request: dict[str, Any]) -> None:
        assert self.channel is not None
        try:
            stripe = int(uuid.UUID(request["attachment_id"])) % len(self.artifact_locks)
            async with self.artifact_locks[stripe]:
                await self._download_artifact(request)
            result = {
                "type": "artifact/result", "id": request["id"], "status": "stored",
                "attachment_id": request["attachment_id"], "sha256": request["sha256"],
            }
        except ArtifactTransferError as exc:
            result = {
                "type": "artifact/result", "id": request["id"],
                "status": "failed", "error": exc.code,
            }
        except (WorkerError, aiohttp.ClientError, asyncio.TimeoutError, OSError):
            result = {
                "type": "artifact/result", "id": request["id"],
                "status": "failed", "error": "unavailable",
            }
        await self.channel.send(result)

    def read_history(self, thread_id: Any) -> Optional[str]:
        # CODEX: Only a UUID-selected rollout below this worker's sessions can be migrated.
        if not isinstance(thread_id, str):
            raise WorkerError("Invalid session identifier.")
        try:
            if str(uuid.UUID(thread_id)) != thread_id:
                raise ValueError
        except ValueError:
            raise WorkerError("Invalid session identifier.") from None
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        state_fd = os.open(self.config.state_dir, flags)
        try:
            try:
                sessions_fd = os.open("sessions", flags, dir_fd=state_fd)
            except FileNotFoundError:
                return None
        finally:
            os.close(state_fd)
        remaining = [100_000]

        def search(directory_fd: int, depth: int) -> Optional[str]:
            with os.scandir(directory_fd) as entries:
                for entry in entries:
                    remaining[0] -= 1
                    if remaining[0] < 0:
                        raise WorkerError("Session history search exceeded its safety limit.")
                    if entry.is_dir(follow_symlinks=False) and depth < 6:
                        child_fd = os.open(entry.name, flags, dir_fd=directory_fd)
                        try:
                            content = search(child_fd, depth + 1)
                        finally:
                            os.close(child_fd)
                        if content is not None:
                            return content
                    elif entry.name.endswith(f"-{thread_id}.jsonl") and entry.is_file(follow_symlinks=False):
                        file_fd = os.open(
                            entry.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                            dir_fd=directory_fd,
                        )
                        with os.fdopen(file_fd, "rb") as stream:
                            metadata = os.fstat(stream.fileno())
                            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_WS_BYTES:
                                raise WorkerError("Session history exceeded its safety limit.")
                            content_bytes = stream.read(MAX_WS_BYTES + 1)
                        if len(content_bytes) > MAX_WS_BYTES:
                            raise WorkerError("Session history exceeded its safety limit.")
                        try:
                            content = content_bytes.decode("utf-8")
                            first = json.loads(content.split("\n", 1)[0])
                        except (ValueError, UnicodeDecodeError, RecursionError):
                            raise WorkerError("Session history metadata is invalid.") from None
                        if (
                            not isinstance(first, dict) or first.get("type") != "session_meta"
                            or not isinstance(first.get("payload"), dict)
                            or first["payload"].get("id") != thread_id
                        ):
                            raise WorkerError("Session history identifier does not match.")
                        return content
            return None

        try:
            return search(sessions_fd, 0)
        finally:
            os.close(sessions_fd)

    async def close(self) -> None:
        for task in self.artifact_tasks:
            task.cancel()
        if self.artifact_tasks:
            await asyncio.gather(*self.artifact_tasks, return_exceptions=True)
        self.artifact_tasks.clear()
        for task in self.tasks:
            task.cancel()
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)
        self.tasks.clear()
        process = self.process
        group = self.process_group
        if process is None:
            return
        if process.stdin is not None:
            process.stdin.close()
        if group is not None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(group, signal.SIGTERM)
        if process.returncode is None:
            try:
                await asyncio.wait_for(process.wait(), 3)
            except asyncio.TimeoutError:
                if group is not None:
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(group, signal.SIGKILL)
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(process.wait(), 2)
        if group is not None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(group, signal.SIGKILL)
        self.process = None
        self.process_group = None


class Worker:
    def __init__(
        self,
        config: WorkerConfig,
        printer: Callable[[str], None] = print,
        browser_opener: Callable[[str], Any] = webbrowser.open,
    ) -> None:
        self.config = config
        self.printer = printer
        self.browser_opener = browser_opener
        self.session: Optional[aiohttp.ClientSession] = None

    async def run(self) -> None:
        ensure_private_directory(self.config.state_dir)
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=15, sock_read=60)
        connector = None
        if self.config.server_ca is not None:
            # CODEX: Trust the LAN CA only for this connection, never change the provider subprocess trust store.
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            context.load_verify_locations(cafile=self.config.server_ca)
            connector = aiohttp.TCPConnector(ssl=context)
        async with aiohttp.ClientSession(
            timeout=timeout, connector=connector, trust_env=False
        ) as session:
            self.session = session
            token: Optional[str] = None
            device: Optional[dict[str, Any]] = None
            if self.config.token_file.exists():
                token = load_device_token(self.config.token_file)
            elif self.config.enroll:
                enrollment = await self._retry_enroll()
                token = enrollment["token"]
                save_device_token(self.config.token_file, token)
                device = enrollment["device"]
            else:
                raise WorkerError("Device token file is missing; run with --enroll first.")
            if device is not None:
                self._print_device(device)
            if self.config.open_browser:
                await self._open_browser(token)
            previous = str(device.get("status") or "") if device is not None else None
            device = await self._wait_until_approved(token, previous)
            delay = 0.5
            reconnect_attempt = 0
            while True:
                connection_type = (
                    WorkerConnection
                    if _is_loopback(urlsplit(self.config.endpoints.base).hostname or "")
                    else ExecutorConnection
                )
                connection = connection_type(self.config, session, token)
                failure = None
                try:
                    if connection_type is ExecutorConnection:
                        self.printer("Connecting to the host; this computer runs tools only, not model sessions.")
                    await connection.run()
                except asyncio.CancelledError:
                    raise
                except (DeviceAccessError, ExecutorUpgradeRequired):
                    raise
                except (WorkerError, ConnectionError, OSError, aiohttp.ClientError) as exc:
                    failure = exc
                if connection.established:
                    delay = 0.5
                    reconnect_attempt = 0
                reconnect_attempt += 1
                diagnostic = dict(getattr(connection, "transport_diagnostic", websocket_diagnostic()))
                if failure is not None:
                    diagnostic["failure_type"] = type(failure).__name__
                role = "legacy-worker" if connection_type is WorkerConnection else "executor"
                self.printer(
                    "Worker connection ended: "
                    + format_transport_diagnostic(role, diagnostic, attempt=reconnect_attempt)
                )
                await self._wait_until_approved(token, device.get("status"))
                wait = random.uniform(delay * 0.75, delay * 1.25)
                self.printer(
                    f"Reconnecting in {wait:.1f} seconds. "
                    + format_transport_diagnostic(
                        role, diagnostic, attempt=reconnect_attempt, retry_seconds=wait
                    )
                )
                await asyncio.sleep(wait)
                delay = min(30.0, delay * 2)

    async def _open_browser(self, token: str) -> None:
        login_url = self.config.endpoints.browser + "#login=" + quote(token, safe="")
        await asyncio.to_thread(self.browser_opener, login_url)
        del login_url

    async def _retry_enroll(self) -> dict[str, Any]:
        delay = 0.5
        while True:
            try:
                return await self._enroll()
            except (TransientServerError, aiohttp.ClientConnectionError, asyncio.TimeoutError):
                wait = random.uniform(delay * 0.75, delay * 1.25)
                self.printer(f"Server unavailable; retrying enrollment in {wait:.1f} seconds.")
                await asyncio.sleep(wait)
                delay = min(15.0, delay * 2)

    async def _enroll(self) -> dict[str, Any]:
        assert self.session is not None
        async with self.session.post(
            self.config.endpoints.enroll,
            json={"name": self.config.name},
            allow_redirects=False,
        ) as response:
            payload = await self._json_response(response, "Enrollment")
        device = self._device(payload)
        token = payload.get("token")
        if not isinstance(token, str) or not token:
            raise WorkerError("Enrollment response did not include a device token.")
        return {"device": device, "token": token}

    async def _me(self, token: str) -> dict[str, Any]:
        assert self.session is not None
        async with self.session.get(
            self.config.endpoints.me,
            headers={"Authorization": f"Bearer {token}"},
            allow_redirects=False,
        ) as response:
            if response.status in {401, 403}:
                raise DeviceAccessError("Device authorization was rejected or revoked.")
            payload = await self._json_response(response, "Device status")
        return self._device(payload)

    async def _wait_until_approved(
        self, token: str, previous_status: Optional[str] = None
    ) -> dict[str, Any]:
        status = previous_status
        unavailable = False
        while True:
            try:
                device = await self._me(token)
                unavailable = False
            except (TransientServerError, aiohttp.ClientConnectionError, asyncio.TimeoutError):
                if not unavailable:
                    self.printer("Server unavailable; waiting to retry device status.")
                    unavailable = True
                await asyncio.sleep(POLL_SECONDS)
                continue
            current = str(device.get("status") or "")
            if current != status:
                self._print_device(device, include_browser=status is None)
                status = current
            if current == "approved":
                return device
            if current != "pending":
                raise DeviceAccessError("Device enrollment was rejected or revoked.")
            await asyncio.sleep(POLL_SECONDS)

    @staticmethod
    async def _json_response(
        response: aiohttp.ClientResponse, operation: str
    ) -> dict[str, Any]:
        if response.status < 200 or response.status >= 300:
            if response.status >= 500:
                raise TransientServerError(
                    f"{operation} is temporarily unavailable (HTTP {response.status})."
                )
            raise WorkerError(f"{operation} failed with HTTP {response.status}.")
        if response.content_length is not None and response.content_length > MAX_JSON_BYTES:
            raise WorkerError(f"{operation} returned an oversized response.")
        raw = bytearray()
        async for chunk in response.content.iter_chunked(64 * 1024):
            raw.extend(chunk)
            if len(raw) > MAX_JSON_BYTES:
                raise WorkerError(f"{operation} returned an oversized response.")
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise WorkerError(f"{operation} returned invalid JSON.") from None
        if not isinstance(payload, dict):
            raise WorkerError(f"{operation} returned an invalid response.")
        return payload

    @staticmethod
    def _device(payload: dict[str, Any]) -> dict[str, Any]:
        device = payload.get("device")
        if not isinstance(device, dict):
            raise WorkerError("Server response did not include a device record.")
        if not isinstance(device.get("id"), str) or not device["id"]:
            raise WorkerError("Server returned an invalid device record.")
        if not isinstance(device.get("status"), str) or not device["status"]:
            raise WorkerError("Server returned an invalid device record.")
        return device

    def _print_device(
        self, device: dict[str, Any], include_browser: bool = True
    ) -> None:
        self.printer(f"Device ID: {device['id']}")
        self.printer(f"Status: {device['status']}")
        code = device.get("verification_code")
        if isinstance(code, str) and code:
            self.printer(f"Verification code: {code}")
        if include_browser:
            self.printer(f"Browser: {self.config.endpoints.browser}")


def parse_args(argv: Optional[list[str]] = None) -> WorkerConfig:
    parser = argparse.ArgumentParser(description="Codex LAN local execution worker")
    parser.add_argument("--server", required=True, help="Central server URL")
    parser.add_argument("--token-file", required=True, type=Path)
    parser.add_argument("--server-ca", type=Path, help="CA certificate for this server only")
    parser.add_argument("--route-state", type=Path, help="Shared SQLite state for the last working provider route")
    parser.add_argument("--cwd", required=True, type=Path)
    parser.add_argument("--codex")
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=Path.home() / ".local" / "share" / "codex-lan" / "worker",
    )
    parser.add_argument("--name", default=socket.gethostname())
    parser.add_argument("--enroll", action="store_true")
    parser.add_argument("--open", dest="open_browser", action="store_true")
    args = parser.parse_args(argv)
    cwd = args.cwd.expanduser().resolve()
    if not cwd.is_dir():
        parser.error("--cwd must be an existing directory")
    name = str(args.name or "").strip()
    if not name or len(name) > 128:
        parser.error("--name must contain 1 to 128 characters")
    try:
        endpoints = build_endpoints(args.server)
        codex = resolve_codex(args.codex)
    except WorkerError as exc:
        parser.error(str(exc))
    return WorkerConfig(
        endpoints=endpoints,
        token_file=Path(os.path.abspath(args.token_file.expanduser())),
        cwd=cwd,
        codex=codex,
        state_dir=Path(os.path.abspath(args.state_dir.expanduser())),
        name=name,
        enroll=bool(args.enroll),
        open_browser=bool(args.open_browser),
        server_ca=args.server_ca.expanduser().resolve() if args.server_ca else None,
        route_state=Path(os.path.abspath(args.route_state.expanduser())) if args.route_state else None,
    )


async def async_main(argv: Optional[list[str]] = None) -> None:
    config = parse_args(argv)
    await Worker(config).run()


def main(argv: Optional[list[str]] = None) -> int:
    os.umask(0o077)
    try:
        asyncio.run(async_main(argv))
    except KeyboardInterrupt:
        return 130
    except WorkerError as exc:
        print(f"worker: {exc}", file=sys.stderr)
        return 1
    except (aiohttp.ClientError, OSError, asyncio.TimeoutError):
        print("worker: connection or local process failure; retry after checking the service.", file=sys.stderr)
        return 1
    except Exception:
        print("worker: unexpected local worker failure.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
