"""CODEX: Host-owned model sessions with a client's native execution environment over the LAN."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import stat
import tempfile
import uuid
from pathlib import Path, PurePosixPath
from types import SimpleNamespace

from aiohttp import WSMsgType, web

from .runtime import Peer
from .transport import LAN_WEBSOCKET_HEARTBEAT_SECONDS
from .worker import (
    ARTIFACT_STORE_CAPABILITY,
    FILE_ATTACHMENTS_CAPABILITY,
    MAX_ARTIFACT_TRANSFERS,
    MAX_GENERATED_ARTIFACT_BYTES,
    CodexAppServer,
    WorkerConnection,
    WorkerError,
    ensure_private_directory,
    image_extension,
    provider_tokens,
)


MAX_EXEC_BYTES = 8 * 1024 * 1024
HISTORY_TIMEOUT = 30
ARTIFACT_TIMEOUT = 90
ARTIFACT_RETRY_BATCH = 100
ARTIFACT_RETRY_SECONDS = 10
LOGGER = logging.getLogger(__name__)
ARTIFACT_EXTENSIONS = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
}
HOST_RPC_METHODS = {
    "initialize", "account/login/start", "model/list", "environment/add", "environment/info",
    "environment/status", "thread/start", "thread/resume", "thread/read", "turn/start",
    "turn/steer", "turn/interrupt",
}


def remote_directory(value):
    if (not isinstance(value, str) or not value.startswith("/") or len(value) > 4096
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise WorkerError("The client working directory must be an absolute Linux path.")
    return str(PurePosixPath(value))


class _PeerSocket:
    def __init__(self, session):
        self.session = session

    @property
    def closed(self):
        return self.session.closing or self.session.ws.closed

    async def send_json(self, packet):
        self.session.require_connected()
        await self.session.connection._handle_server_packet(packet)

    async def deliver_artifact(self, attachment, thread_id):
        return await self.session.deliver_artifact(attachment, thread_id)

    async def close(self, **options):
        await self.session.ws.close(**options)


class _LocalChannel:
    def __init__(self, session):
        self.session = session

    async def send(self, packet):
        if self.session.closing:
            return
        kind = packet.get("type")
        if kind == "result":
            self.session.peer.receive(packet)
        elif kind == "event":
            self.session.service.runtime.event(self.session.peer, packet.get("method"), packet.get("params") or {})


class _HostConnection(WorkerConnection):
    def __init__(self, owner):
        route_state = getattr(owner.service, "route_state_path", Path(owner.service.data_dir).parent / "egress.sqlite3")
        config = SimpleNamespace(cwd=owner.host_cwd, state_dir=owner.state_dir,
                                 codex=Path(owner.service.runtime.auth.codex),
                                 route_state=route_state)
        super().__init__(config, None, "")
        self.owner = owner
        self.channel = _LocalChannel(owner)

    async def _download_attachment(self, params):
        attachment = self.owner.service.store.get_attachment(self.owner.device_id, params.get("attachment_id", ""))
        path = Path(attachment["path"]).resolve()
        if not path.is_relative_to(self.owner.service.upload_dir.resolve()) or not path.is_file():
            raise WorkerError("The selected file is unavailable on the server.")
        if attachment["mime"] not in ARTIFACT_EXTENSIONS:
            return await self.owner.deliver_attachment(attachment)
        self.owner.authorized_images.add(path)
        return {"path": str(path)}

    async def _refresh_auth(self, identifier):
        try:
            tokens = provider_tokens(await self.owner.service.runtime.auth.tokens(force=True))
            try:
                await self.codex.respond(identifier, tokens)
            finally:
                tokens.clear()
        except (WorkerError, OSError, RuntimeError, asyncio.TimeoutError):
            await self.codex.respond_error(identifier, "Server-managed authentication could not be refreshed.")


class _RemoteAppServer(CodexAppServer):
    def __init__(self, owner):
        connection = owner.connection
        super().__init__(connection.config.codex, owner.host_cwd, owner.state_dir,
                         connection._send_event, connection._handle_app_request, gateway=connection.gateway)
        self.owner = owner

    async def request(self, method, params=None, timeout=90):
        if method not in HOST_RPC_METHODS:
            raise WorkerError("This host session does not expose local host execution RPCs.")
        value = dict(params or {})
        if method in {"thread/start", "thread/resume", "turn/start", "turn/steer", "turn/interrupt"}:
            self.owner.require_connected()
        if method in {"thread/start", "turn/start"}:
            cwd = remote_directory(value.get("cwd") or self.owner.remote_cwd)
            # CODEX: Remote environments select the real workspace; top-level cwd is only safe host configuration context.
            value["cwd"] = str(self.owner.host_cwd)
            value["environments"] = [{"environmentId": self.owner.environment_id, "cwd": cwd,
                                      "runtimeWorkspaceRoots": [cwd]}]
        elif method == "thread/resume":
            thread_id = value.get("threadId")
            value["path"] = str(await self.owner.history_path(thread_id))
            # CODEX: Native resume probes local configuration before remote binding; never probe a client's path on the host.
            value["cwd"] = str(self.owner.host_cwd)
        if method in {"turn/start", "turn/steer"} and isinstance(value.get("input"), list):
            self.owner.validate_images(value["input"])
        result = await super().request(method, value, timeout=timeout)
        if method in {"thread/start", "thread/resume"}:
            thread = result.get("thread") or {}
            self.owner.remember_thread(thread.get("id"), thread.get("path"))
            result["cwd"] = remote_directory((params or {}).get("cwd") or self.owner.remote_cwd)
            thread["cwd"] = result["cwd"]
        return result


class HostWorkerSession:
    def __init__(self, service, device, websocket, hello):
        self.service, self.ws = service, websocket
        self.device_id = str(uuid.UUID(device["id"]))
        self.remote_cwd = remote_directory(hello.get("cwd"))
        advertised = hello.get("capabilities", [])
        self.capabilities = frozenset(
            value for value in advertised
            if isinstance(value, str) and 0 < len(value) <= 80
            and not any(ord(character) < 32 or ord(character) == 127 for character in value)
        ) if isinstance(advertised, list) and len(advertised) <= 32 else frozenset()
        self.environment_id = "client-" + self.device_id
        self.state_dir = Path(service.data_dir) / "remote-workers" / self.device_id
        self.host_cwd = self.state_dir / "host-workspace"
        self.closing = False
        self._close_task = None
        self.executor_ready = False
        self.reader_task = None
        self.app_task = None
        self.artifact_delivery_task = None
        self.bridge_runner = None
        self.bridge_ws = None
        self.bridge_url = None
        self.history_waiters = {}
        self.artifact_waiters = {}
        self.artifact_delivery_locks = tuple(asyncio.Lock() for _ in range(32))
        self.artifact_delivery_slots = asyncio.Semaphore(MAX_ARTIFACT_TRANSFERS)
        self.thread_paths = {}
        self.history_locks = {}
        self.authorized_images = set()
        self.send_lock = asyncio.Lock()
        self.peer = Peer(self.device_id, _PeerSocket(self))
        self.peer.cwd = self.remote_cwd
        self.peer.client_version = str(hello.get("version", ""))[:80]
        self.peer.execution_mode = "server"
        self.peer.network_recovery = True
        self.connection = _HostConnection(self)

    def require_connected(self):
        if self.closing or self.ws.closed or not self.executor_ready:
            raise ConnectionError("The client executor is disconnected. No command was redirected to the host.")

    def validate_images(self, inputs):
        for item in inputs:
            if not isinstance(item, dict) or item.get("type") != "localImage":
                continue
            path = Path(item.get("path", "")).resolve()
            if path not in self.authorized_images or not path.is_relative_to(self.service.upload_dir.resolve()):
                raise WorkerError("This image has not been authorized for the connected client.")

    async def _send_json(self, packet):
        async with self.send_lock:
            await self.ws.send_json(packet)

    async def _send_binary(self, payload):
        async with self.send_lock:
            await self.ws.send_bytes(payload)

    async def _bridge(self, request):
        if self.closing or self.ws.closed or self.bridge_ws is not None and not self.bridge_ws.closed:
            raise web.HTTPConflict()
        websocket = web.WebSocketResponse(
            max_msg_size=MAX_EXEC_BYTES,
            heartbeat=LAN_WEBSOCKET_HEARTBEAT_SECONDS,
            compress=False,
        )
        await websocket.prepare(request)
        self.bridge_ws = websocket
        try:
            async for frame in websocket:
                if frame.type != WSMsgType.TEXT:
                    if frame.type == WSMsgType.BINARY:
                        raise WorkerError("The native execution transport requires JSON text frames.")
                    continue
                packet = json.loads(frame.data)
                if not isinstance(packet, dict):
                    raise WorkerError("The native execution message is invalid.")
                payload = (json.dumps(packet, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
                if len(payload) > MAX_EXEC_BYTES:
                    raise WorkerError("The native execution message exceeded its size limit.")
                await self._send_binary(payload)
        except (ValueError, OSError, WorkerError):
            await self.ws.close(code=1011, message=b"Client execution transport interrupted")
        finally:
            if self.bridge_ws is websocket:
                self.bridge_ws = None
            if not self.closing and self.executor_ready:
                self.executor_ready = False
                await self.ws.close(code=1011, message=b"Client execution transport interrupted")
        return websocket

    async def _start_bridge(self):
        application = web.Application()
        path = "/" + uuid.uuid4().hex + uuid.uuid4().hex
        application.router.add_get(path, self._bridge)
        self.bridge_runner = web.AppRunner(application, access_log=None)
        await self.bridge_runner.setup()
        listener = web.TCPSite(self.bridge_runner, "127.0.0.1", 0)
        await listener.start()
        port = listener._server.sockets[0].getsockname()[1]
        self.bridge_url = f"ws://127.0.0.1:{port}{path}"

    async def _read_executor(self):
        try:
            async for frame in self.ws:
                if frame.type == WSMsgType.BINARY:
                    if len(frame.data) > MAX_EXEC_BYTES:
                        raise WorkerError("The client execution message exceeded its size limit.")
                    text = frame.data.decode("utf-8").strip()
                    if not isinstance(json.loads(text), dict) or self.bridge_ws is None or self.bridge_ws.closed:
                        raise WorkerError("The client execution response is unexpected.")
                    await self.bridge_ws.send_str(text)
                elif frame.type == WSMsgType.TEXT:
                    packet = json.loads(frame.data)
                    if not isinstance(packet, dict):
                        raise WorkerError("The client sent an unsupported control message.")
                    kind = packet.get("type")
                    if kind == "history/result":
                        pending = self.history_waiters.get(packet.get("id"))
                    elif kind in {"artifact/result", "attachment/result"}:
                        pending = self.artifact_waiters.get(packet.get("id"))
                    else:
                        raise WorkerError("The client sent an unsupported control message.")
                    if pending is not None and not pending.done():
                        pending.set_result(packet)
                elif frame.type in {WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.ERROR}:
                    break
        finally:
            self.executor_ready = False
            for future in self.history_waiters.values():
                if not future.done():
                    future.set_exception(ConnectionError("The client disconnected while transferring conversation history."))
            for future in self.artifact_waiters.values():
                if not future.done():
                    future.set_exception(ConnectionError("The client disconnected while transferring a generated image."))
            if self.bridge_ws is not None:
                await self.bridge_ws.close()

    async def deliver_attachment(self, attachment):
        """CODEX: Wait for a private client-side copy before including its path in a turn."""
        self.require_connected()
        if FILE_ATTACHMENTS_CAPABILITY not in self.capabilities:
            raise WorkerError("This Connector needs the file-attachments update before it can receive non-image files. Update Connector on your computer, then retry the saved message.")
        async with self.artifact_delivery_slots:
            request_id = uuid.uuid4().hex
            future = asyncio.get_running_loop().create_future()
            self.artifact_waiters[request_id] = future
            try:
                await self._send_json({"type": "attachment/store", "id": request_id, "attachment_id": attachment["id"]})
                result = await asyncio.wait_for(future, ARTIFACT_TIMEOUT)
            finally:
                self.artifact_waiters.pop(request_id, None)
            if (result.get("type") != "attachment/result" or result.get("status") != "stored"
                    or result.get("attachment_id") != attachment["id"]):
                raise WorkerError("The attachment could not be stored on your connected computer.")
            return {"path": remote_directory(result.get("path"))}

    def _artifact_metadata(self, attachment, thread_id):
        if not isinstance(attachment, dict):
            raise WorkerError("The generated image record is invalid.")
        try:
            attachment_id = str(uuid.UUID(attachment.get("id")))
            normalized_thread = str(uuid.UUID(thread_id))
        except (ValueError, TypeError, AttributeError):
            raise WorkerError("The generated image identifiers are invalid.") from None
        if attachment_id != attachment.get("id") or normalized_thread != thread_id:
            raise WorkerError("The generated image identifiers are invalid.")
        name = attachment.get("name")
        mime = attachment.get("mime")
        size = attachment.get("size")
        if (not isinstance(name, str) or not name or len(name) > 512
                or any(ord(character) < 32 or ord(character) == 127 for character in name)):
            raise WorkerError("The generated image name is invalid.")
        if mime not in ARTIFACT_EXTENSIONS:
            raise WorkerError("The generated image type is unsupported.")
        if (isinstance(size, bool) or not isinstance(size, int)
                or not 0 < size <= MAX_GENERATED_ARTIFACT_BYTES):
            raise WorkerError("The generated image size is invalid.")
        value = attachment.get("path")
        if not isinstance(value, str) or not value or len(value) > 4096:
            raise WorkerError("The generated image file is unavailable.")
        source = Path(value)
        try:
            resolved = source.resolve(strict=True)
            upload_root = self.service.upload_dir.resolve(strict=True)
            if source.is_symlink() or not resolved.is_relative_to(upload_root):
                raise WorkerError("The generated image file is outside server attachment storage.")
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
            descriptor = os.open(resolved, flags)
            try:
                metadata = os.fstat(descriptor)
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_size != size:
                    raise WorkerError("The generated image size does not match its attachment record.")
                digest = hashlib.sha256()
                prefix = b""
                total = 0
                while True:
                    chunk = os.read(descriptor, 64 * 1024)
                    if not chunk:
                        break
                    if len(prefix) < 12:
                        prefix = (prefix + chunk)[:12]
                    total += len(chunk)
                    if total > MAX_GENERATED_ARTIFACT_BYTES:
                        raise WorkerError("The generated image exceeds its transfer limit.")
                    digest.update(chunk)
            finally:
                os.close(descriptor)
        except WorkerError:
            raise
        except OSError:
            raise WorkerError("The generated image file is unavailable.") from None
        if total != size or image_extension(prefix) != ARTIFACT_EXTENSIONS[mime]:
            raise WorkerError("The generated image content does not match its attachment record.")
        return {
            "attachment_id": attachment_id,
            "thread_id": normalized_thread,
            "name": name,
            "mime": mime,
            "size": size,
            "sha256": digest.hexdigest(),
        }

    async def deliver_artifact(self, attachment, thread_id):
        if ARTIFACT_STORE_CAPABILITY not in self.capabilities:
            return None
        self.require_connected()
        metadata = await asyncio.to_thread(self._artifact_metadata, attachment, thread_id)
        key = (metadata["thread_id"], metadata["attachment_id"])
        stripe = (int(uuid.UUID(key[0])) ^ int(uuid.UUID(key[1]))) % len(self.artifact_delivery_locks)
        lock = self.artifact_delivery_locks[stripe]
        async with lock:
            async with self.artifact_delivery_slots:
                self.require_connected()
                try:
                    intent = self.service.store.prepare_artifact_delivery(
                        self.device_id, metadata["thread_id"], metadata["attachment_id"], metadata["sha256"]
                    )
                except (PermissionError, ValueError):
                    raise WorkerError("The generated image has no committed delivery intent for this client.") from None
                if any(intent.get(field) != attachment.get(field) for field in ("name", "mime", "size", "path")):
                    raise WorkerError("The generated image does not match its committed attachment record.")
                if intent.get("status") == "delivered":
                    return True
                request_id = uuid.uuid4().hex
                future = asyncio.get_running_loop().create_future()
                self.artifact_waiters[request_id] = future
                try:
                    await self._send_json({"type": "artifact/store", "id": request_id, **metadata})
                    result = await asyncio.wait_for(future, ARTIFACT_TIMEOUT)
                finally:
                    self.artifact_waiters.pop(request_id, None)
                if result.get("status") != "stored":
                    raise WorkerError("The generated image could not be stored on the connected computer.")
                expected_receipt = {"type", "id", "status", "attachment_id", "sha256"}
                if (set(result) != expected_receipt
                        or result.get("attachment_id") != metadata["attachment_id"]
                        or result.get("sha256") != metadata["sha256"]):
                    raise WorkerError("The connected computer returned an invalid generated image receipt.")
                try:
                    self.service.store.complete_artifact_delivery(
                        self.device_id, metadata["thread_id"], metadata["attachment_id"], metadata["sha256"]
                    )
                except (PermissionError, ValueError):
                    raise WorkerError("The generated image receipt could not be committed safely.") from None
                return True

    async def _retry_pending_artifacts_once(self):
        """CODEX: Process one fair page of committed image deliveries for the authenticated owner."""
        if ARTIFACT_STORE_CAPABILITY not in self.capabilities:
            return 0, False
        try:
            pending = await asyncio.to_thread(
                self.service.store.pending_artifact_deliveries, self.device_id, ARTIFACT_RETRY_BATCH
            )
        except (PermissionError, ValueError):
            return 0, False
        failed = False
        for intent in pending:
            if self.closing or self.ws.closed or not self.executor_ready:
                return len(pending), True
            attachment = {
                "id": intent["attachment_id"], "name": intent["name"], "mime": intent["mime"],
                "size": intent["size"], "path": intent["path"],
            }
            try:
                await self.deliver_artifact(attachment, intent["thread_id"])
            except asyncio.CancelledError:
                raise
            except ConnectionError:
                await asyncio.to_thread(
                    self.service.store.defer_artifact_delivery,
                    self.device_id,
                    intent["thread_id"],
                    intent["attachment_id"],
                    intent["attempts"],
                )
                return len(pending), True
            except Exception:
                failed = True
                await asyncio.to_thread(
                    self.service.store.defer_artifact_delivery,
                    self.device_id,
                    intent["thread_id"],
                    intent["attachment_id"],
                    intent["attempts"],
                )
                continue
        return len(pending), failed

    async def _retry_pending_artifacts(self):
        """CODEX: Keep retrying pending image delivery on the same healthy connector session."""
        if ARTIFACT_STORE_CAPABILITY not in self.capabilities:
            return
        while not self.closing and not self.ws.closed and self.executor_ready:
            try:
                count, failed = await self._retry_pending_artifacts_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception(
                    "Generated image retry sweep failed for connector %s",
                    self.device_id[-8:],
                )
                count, failed = 0, True
            if self.closing or self.ws.closed or not self.executor_ready:
                return
            if count >= ARTIFACT_RETRY_BATCH and not failed:
                await asyncio.sleep(0)
            else:
                await asyncio.sleep(ARTIFACT_RETRY_SECONDS)

    def remember_thread(self, thread_id, path):
        if not isinstance(thread_id, str) or not isinstance(path, str):
            return
        candidate = Path(path)
        if candidate.is_file() and candidate.resolve().is_relative_to(self.state_dir.resolve()):
            self.thread_paths[thread_id] = candidate

    def _valid_history_file(self, candidate, thread_id):
        try:
            if (candidate.is_symlink() or not candidate.is_file()
                    or not candidate.resolve().is_relative_to(self.state_dir.resolve())):
                return False
            with candidate.open(encoding="utf-8") as stream:
                line = stream.readline(MAX_EXEC_BYTES + 1)
            first = json.loads(line)
            return first.get("type") == "session_meta" and first.get("payload", {}).get("id") == thread_id
        except (OSError, ValueError, AttributeError):
            return False

    async def history_path(self, thread_id):
        try:
            thread_id = str(uuid.UUID(thread_id))
        except (ValueError, TypeError, AttributeError):
            raise WorkerError("The conversation identifier is invalid.") from None
        owned = self.service.store.db.execute("SELECT 1 FROM rooms WHERE owner_id=? AND thread_id=? LIMIT 1",
                                              (self.device_id, thread_id)).fetchone()
        if not owned:
            raise WorkerError("This conversation does not belong to the connected client.")
        lock = self.history_locks.setdefault(thread_id, asyncio.Lock())
        async with lock:
            return await self._history_path(thread_id)

    async def _history_path(self, thread_id):
        cached = self.thread_paths.get(thread_id)
        if cached is not None and self._valid_history_file(cached, thread_id):
            return cached
        for directory in (self.state_dir / "sessions", self.state_dir / "imported"):
            if directory.is_dir():
                for candidate in directory.rglob("*.jsonl"):
                    if thread_id in candidate.name and self._valid_history_file(candidate, thread_id):
                        self.thread_paths[thread_id] = candidate
                        return candidate
        self.require_connected()
        request_id = uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self.history_waiters[request_id] = future
        try:
            await self._send_json({"type": "history/read", "id": request_id, "thread_id": thread_id})
            result = await asyncio.wait_for(future, HISTORY_TIMEOUT)
        finally:
            self.history_waiters.pop(request_id, None)
        content = result.get("content")
        if result.get("error") or not isinstance(content, str) or not content.strip():
            raise WorkerError("The client's earlier conversation history is unavailable; it has not been replaced.")
        encoded = content.encode("utf-8")
        if len(encoded) > MAX_EXEC_BYTES:
            raise WorkerError("The earlier conversation exceeds the history transfer limit.")
        try:
            first = json.loads(next(line for line in content.splitlines() if line.strip()))
            valid = first.get("type") == "session_meta" and first.get("payload", {}).get("id") == thread_id
        except (ValueError, AttributeError, StopIteration):
            valid = False
        if not valid:
            raise WorkerError("The transferred history does not match the requested conversation.")
        if first.get("payload", {}).get("history_mode") == "paginated":
            raise WorkerError("This older conversation uses a paginated native history database that cannot be moved "
                              "as a single file. Your existing chat messages are preserved. Create a new room for "
                              "server-managed execution, or ask the administrator to migrate the full native history.")
        directory = ensure_private_directory(self.state_dir / "imported")
        path = directory / (thread_id + ".jsonl")
        descriptor, temporary = tempfile.mkstemp(prefix=".history-", dir=directory)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(encoded)
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        self.thread_paths[thread_id] = path
        return path

    async def run(self):
        ensure_private_directory(self.state_dir)
        ensure_private_directory(self.host_cwd)
        self.peer.connection = {**self.connection.gateway.snapshot(), "state": "connecting"}
        self.service.runtime.peers[self.device_id] = self.peer
        self.reader_task = asyncio.create_task(self._read_executor())
        try:
            await self._start_bridge()
            await self.connection.gateway.start()
            self.connection.codex = _RemoteAppServer(self)
            await self.connection.codex.launch()
            tokens = provider_tokens(await self.service.runtime.auth.tokens())
            try:
                models, _version = await self.connection.codex.initialize(tokens)
            finally:
                tokens.clear()
            await self.connection.codex.request("environment/add", {"environmentId": self.environment_id,
                "execServerUrl": self.bridge_url, "connectTimeoutMs": 3000})
            await self.connection.codex.request("environment/info", {"environmentId": self.environment_id}, timeout=15)
            if self.ws.closed or self.reader_task.done():
                raise ConnectionError("The client executor disconnected before it became ready.")
            self.executor_ready = True
            self.peer.models = models
            self.peer.ready = True
            if self.peer.connection.get("state") == "connecting" and not self.peer.connection.get("attempt"):
                self.peer.connection = {}
            await self._send_json({"type": "executor_ready", "execution_mode": "server"})
            self.artifact_delivery_task = asyncio.create_task(
                self._retry_pending_artifacts(), name="codex-lan-artifact-delivery-retry"
            )
            self.app_task = asyncio.create_task(self.connection.codex.wait_closed())
            completed, _pending = await asyncio.wait((self.reader_task, self.app_task), return_when=asyncio.FIRST_COMPLETED)
            for task in completed:
                await task
        finally:
            await self.close()

    async def close(self):
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close_impl())
        await asyncio.shield(self._close_task)

    async def _close_impl(self):
        self.closing = True
        self.executor_ready = False
        self.peer.ready = False
        tasks = [task for task in (self.reader_task, self.app_task, self.artifact_delivery_task)
                 if task is not None and task is not asyncio.current_task()]
        for task in tasks:
            if not task.done():
                task.cancel()
        cleanup = [self.service.runtime.disconnect(self.peer), self.ws.close(), self.connection._cancel_tasks()]
        if self.connection.codex is not None:
            cleanup.append(self.connection.codex.close())
        if self.bridge_ws is not None:
            cleanup.append(self.bridge_ws.close())
        await asyncio.gather(*cleanup, *tasks, return_exceptions=True)
        try:
            await self.connection.gateway.close()
        finally:
            if self.bridge_runner is not None:
                await self.bridge_runner.cleanup()
