"""CODEX: Host model isolation and native client-executor transport regressions."""

import asyncio
import hashlib
import json
import sqlite3
import stat
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import aiohttp
from aiohttp import WSMsgType

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from project_layout import configure_imports, source_file
configure_imports(__file__)

from codex_lan.host_worker import HostWorkerSession, _HostConnection, _RemoteAppServer, remote_directory
from codex_lan.worker import CodexAppServer, WorkerError


class Socket:
    def __init__(self):
        self.closed = False
        self.sent = []
        self.binary = asyncio.Queue()
        self.incoming = asyncio.Queue()

    async def send_json(self, value):
        self.sent.append(dict(value))

    async def send_bytes(self, value):
        await self.binary.put(value)

    async def close(self, **_options):
        if not self.closed:
            self.closed = True
            await self.incoming.put(None)

    def __aiter__(self):
        return self

    async def __anext__(self):
        value = await self.incoming.get()
        if value is None:
            raise StopAsyncIteration
        return value


class HostWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.database = sqlite3.connect(":memory:")
        self.database.execute("CREATE TABLE rooms(owner_id TEXT, thread_id TEXT)")
        self.database.execute(
            "CREATE TABLE attachments(id TEXT, owner_id TEXT, name TEXT, mime TEXT, size INTEGER, path TEXT)"
        )
        self.device = str(uuid.uuid4())
        auth = SimpleNamespace(codex=Path("/nonexistent/test-codex"), tokens=AsyncMock(return_value={
            "accessToken": "HOST-ONLY-SECRET", "chatgptAccountId": "HOST-ONLY-ACCOUNT"}))
        self.runtime = SimpleNamespace(auth=auth, peers={}, event=lambda *_args: None,
                                       disconnect=AsyncMock())
        self.artifact_deliveries = {}
        store = SimpleNamespace(
            db=self.database,
            prepare_artifact_delivery=self.prepare_artifact_delivery,
            complete_artifact_delivery=self.complete_artifact_delivery,
            defer_artifact_delivery=self.defer_artifact_delivery,
            pending_artifact_deliveries=self.pending_artifact_deliveries,
        )
        self.service = SimpleNamespace(data_dir=self.directory, upload_dir=self.directory / "uploads",
                                       store=store, runtime=self.runtime)
        self.socket = Socket()
        self.session = HostWorkerSession(self.service, {"id": self.device}, self.socket,
                                        {"cwd": "/remote/client-only", "version": "test", "protocol": 1,
                                         "capabilities": ["artifact-store-v1"]})
        self.session.connection.gateway.start = AsyncMock()
        self.session.connection.gateway.close = AsyncMock()

    async def asyncTearDown(self):
        await self.session.close()
        self.database.close()
        self.temporary.cleanup()

    def own_thread(self):
        identifier = str(uuid.uuid4())
        self.database.execute("INSERT INTO rooms VALUES (?,?)", (self.device, identifier))
        return identifier

    def prepare_artifact_delivery(self, owner_id, thread_id, attachment_id, digest):
        room = self.database.execute(
            "SELECT 1 FROM rooms WHERE owner_id=? AND thread_id=?", (owner_id, thread_id)
        ).fetchone()
        attachment = self.database.execute(
            "SELECT name,mime,size,path FROM attachments WHERE owner_id=? AND id=?",
            (owner_id, attachment_id),
        ).fetchone()
        if room is None or attachment is None:
            raise PermissionError("unowned artifact")
        key = owner_id, thread_id, attachment_id
        existing = self.artifact_deliveries.get(key)
        if existing is not None and existing["sha256"] and existing["sha256"] != digest:
            raise ValueError("digest changed")
        intent = existing or {
            "owner_id": owner_id, "thread_id": thread_id, "attachment_id": attachment_id,
            "status": "pending", "sha256": "", "attempts": 0, "updated_at": 0,
        }
        if intent["status"] == "pending":
            intent["sha256"] = digest
            intent["attempts"] += 1
            intent["updated_at"] += 1
        intent.update(dict(zip(("name", "mime", "size", "path"), attachment)))
        self.artifact_deliveries[key] = intent
        return dict(intent)

    def complete_artifact_delivery(self, owner_id, thread_id, attachment_id, digest):
        key = owner_id, thread_id, attachment_id
        intent = self.artifact_deliveries.get(key)
        if intent is None or intent["sha256"] != digest:
            raise ValueError("unknown artifact receipt")
        intent["status"] = "delivered"
        return dict(intent)

    def defer_artifact_delivery(
        self, owner_id, thread_id, attachment_id, expected_attempts,
    ):
        intent = self.artifact_deliveries.get((owner_id, thread_id, attachment_id))
        if (intent is None or intent["status"] != "pending"
                or intent["attempts"] != expected_attempts):
            return False
        intent["attempts"] += 1
        intent["updated_at"] += 1
        return True

    def pending_artifact_deliveries(self, owner_id, limit):
        pending = [intent for key, intent in self.artifact_deliveries.items()
                   if key[0] == owner_id and intent["status"] == "pending"]
        pending.sort(key=lambda intent: (
            intent["attempts"], intent["updated_at"], intent["attachment_id"]
        ))
        return [dict(intent) for intent in pending[:limit]]

    @staticmethod
    def history(identifier):
        return json.dumps({"type": "session_meta", "payload": {"id": identifier}}) + "\n"

    async def sent_packet(self):
        async def wait_for_packet():
            while not self.socket.sent:
                await asyncio.sleep(0.01)
            return self.socket.sent[0]

        return await asyncio.wait_for(wait_for_packet(), 2)

    async def test_native_requests_always_target_the_client_and_reject_host_command_exec(self):
        self.session.executor_ready = True
        server = _RemoteAppServer(self.session)
        with patch.object(CodexAppServer, "request", new=AsyncMock(return_value={})) as request:
            for method in ("thread/start", "turn/start"):
                await server.request(method, {"cwd": "/remote/other", "environments": [{"environmentId": "host"}]})
                actual = request.await_args.args[1]
                self.assertEqual(actual["cwd"], str(self.session.host_cwd))
                self.assertEqual(actual["environments"], [{"environmentId": "client-" + self.device,
                    "cwd": "/remote/other", "runtimeWorkspaceRoots": ["/remote/other"]}])
            for method in ("command/exec", "fs/readFile", "process/start"):
                with self.assertRaises(WorkerError):
                    await server.request(method, {})
            self.session.executor_ready = False
            with self.assertRaises(ConnectionError):
                await server.request("turn/start", {})
            self.assertEqual(request.await_count, 2)

    async def test_file_upload_waits_for_client_copy_and_never_returns_host_path(self):
        self.session.executor_ready = True
        self.session.capabilities = frozenset({"file-attachments-v1"})
        self.service.upload_dir.mkdir()
        path = self.service.upload_dir / "uploaded-file"
        path.write_text("private attachment")
        attachment = {"id": str(uuid.uuid4()), "path": str(path), "mime": "application/octet-stream"}
        self.service.store.get_attachment = lambda owner, identifier: attachment if owner == self.device and identifier == attachment["id"] else None
        transfer = asyncio.create_task(self.session.connection._download_attachment({"attachment_id": attachment["id"]}))
        packet = await self.sent_packet()
        self.assertEqual(packet["type"], "attachment/store")
        self.assertNotIn(str(path), json.dumps(packet))
        self.session.artifact_waiters[packet["id"]].set_result({
            "type": "attachment/result", "id": packet["id"], "status": "stored",
            "attachment_id": attachment["id"], "path": "/remote/private/uploads/file.txt",
        })
        self.assertEqual(await transfer, {"path": "/remote/private/uploads/file.txt"})
        self.assertFalse(self.session.artifact_waiters)

    async def test_old_connector_rejects_files_instead_of_giving_a_wrong_host_path(self):
        self.session.executor_ready = True
        with self.assertRaisesRegex(WorkerError, "file-attachments update"):
            await self.session.deliver_attachment({"id": str(uuid.uuid4())})
        self.assertEqual(self.socket.sent, [])

    async def test_remote_path_validation_never_resolves_paths_on_host(self):
        self.assertEqual(remote_directory("/remote/client-only"), "/remote/client-only")
        for value in (None, "relative", "/tmp\nunsafe", "/tmp\x00bad"):
            with self.assertRaises(WorkerError):
                remote_directory(value)

    async def test_resume_rejects_unowned_thread_without_contacting_client(self):
        self.session.executor_ready = True
        with self.assertRaisesRegex(WorkerError, "does not belong"):
            await self.session.history_path(str(uuid.uuid4()))
        self.assertEqual(self.socket.sent, [])

    async def test_history_transfer_is_exact_private_and_shared_by_concurrent_resumes(self):
        identifier = self.own_thread()
        self.session.executor_ready = True
        self.session.reader_task = asyncio.create_task(self.session._read_executor())
        first = asyncio.create_task(self.session.history_path(identifier))
        second = asyncio.create_task(self.session.history_path(identifier))
        packet = await self.sent_packet()
        self.assertEqual(packet["type"], "history/read")
        reply = {"type": "history/result", "id": packet["id"], "content": self.history(identifier)}
        await self.socket.incoming.put(SimpleNamespace(type=WSMsgType.TEXT, data=json.dumps(reply)))
        paths = await asyncio.wait_for(asyncio.gather(first, second), 2)
        self.assertEqual(paths[0], paths[1])
        self.assertEqual(paths[0].read_text(), self.history(identifier))
        self.assertEqual(stat.S_IMODE(paths[0].stat().st_mode), 0o600)
        self.assertEqual(len(self.socket.sent), 1)

    async def test_generated_artifact_delivery_sends_metadata_only_and_correlates_receipt(self):
        data = b"\x89PNG\r\n\x1a\n" + b"safe-generated-image"
        self.service.upload_dir.mkdir()
        source = self.service.upload_dir / "canonical-upload"
        source.write_bytes(data)
        attachment_id = str(uuid.uuid4())
        thread_id = str(uuid.uuid4())
        attachment = {
            "id": attachment_id, "name": "generated.png", "mime": "image/png",
            "size": len(data), "path": str(source),
        }
        self.database.execute(
            "INSERT INTO attachments VALUES (?,?,?,?,?,?)",
            (attachment_id, self.device, attachment["name"], attachment["mime"], attachment["size"], attachment["path"]),
        )
        self.database.execute("INSERT INTO rooms VALUES (?,?)", (self.device, thread_id))
        self.session.executor_ready = True
        self.session.reader_task = asyncio.create_task(self.session._read_executor())
        delivery = asyncio.create_task(self.session.peer.ws.deliver_artifact(attachment, thread_id))
        packet = await self.sent_packet()
        self.assertEqual(set(packet), {
            "type", "id", "attachment_id", "thread_id", "name", "mime", "size", "sha256",
        })
        self.assertEqual(packet["type"], "artifact/store")
        self.assertEqual(packet["attachment_id"], attachment_id)
        self.assertEqual(packet["thread_id"], thread_id)
        self.assertEqual(packet["sha256"], hashlib.sha256(data).hexdigest())
        encoded = json.dumps(packet)
        self.assertNotIn(str(source), encoded)
        self.assertNotIn(data.hex(), encoded)
        self.assertLess(len(encoded.encode()), 1024 * 1024)
        await self.socket.incoming.put(SimpleNamespace(type=WSMsgType.TEXT, data=json.dumps({
            "type": "artifact/result", "id": packet["id"], "status": "stored",
            "attachment_id": attachment_id, "sha256": packet["sha256"],
        })))
        self.assertIs(await asyncio.wait_for(delivery, 2), True)
        self.assertEqual(self.session.artifact_waiters, {})

    async def test_generated_artifact_receipt_rejects_client_local_path(self):
        data = b"\x89PNG\r\n\x1a\nfixture"
        self.service.upload_dir.mkdir()
        source = self.service.upload_dir / "canonical-upload"
        source.write_bytes(data)
        attachment = {
            "id": str(uuid.uuid4()), "name": "generated.png", "mime": "image/png",
            "size": len(data), "path": str(source),
        }
        thread_id = self.own_thread()
        self.database.execute(
            "INSERT INTO attachments VALUES (?,?,?,?,?,?)",
            (attachment["id"], self.device, attachment["name"], attachment["mime"],
             attachment["size"], attachment["path"]),
        )
        self.session.executor_ready = True
        self.session.reader_task = asyncio.create_task(self.session._read_executor())
        delivery = asyncio.create_task(self.session.deliver_artifact(attachment, thread_id))
        request = await self.sent_packet()
        await self.socket.incoming.put(SimpleNamespace(type=WSMsgType.TEXT, data=json.dumps({
            "type": "artifact/result", "id": request["id"], "status": "stored",
            "attachment_id": attachment["id"], "sha256": request["sha256"],
            "path": f"/private/client/{attachment['id']}.png",
        })))
        with self.assertRaisesRegex(WorkerError, "invalid generated image receipt"):
            await asyncio.wait_for(delivery, 2)

    async def test_old_connector_skips_artifact_delivery_without_sending_unknown_control_frame(self):
        self.session.capabilities = frozenset()
        self.session.executor_ready = True
        result = await self.session.peer.ws.deliver_artifact({}, str(uuid.uuid4()))
        self.assertIsNone(result)
        self.assertEqual(self.socket.sent, [])
        self.assertEqual(self.session.artifact_waiters, {})

    async def test_pending_generated_artifact_retries_after_transient_failure_without_reconnect(self):
        data = b"\x89PNG\r\n\x1a\nretry-after-reconnect"
        self.service.upload_dir.mkdir()
        source = self.service.upload_dir / "canonical-retry"
        source.write_bytes(data)
        attachment = {
            "id": str(uuid.uuid4()), "name": "generated.png", "mime": "image/png",
            "size": len(data), "path": str(source),
        }
        thread_id = self.own_thread()
        self.database.execute(
            "INSERT INTO attachments VALUES (?,?,?,?,?,?)",
            (attachment["id"], self.device, attachment["name"], attachment["mime"],
             attachment["size"], attachment["path"]),
        )
        digest = hashlib.sha256(data).hexdigest()
        self.prepare_artifact_delivery(self.device, thread_id, attachment["id"], digest)
        self.session.executor_ready = True
        self.session.reader_task = asyncio.create_task(self.session._read_executor())
        retry = asyncio.create_task(self.session._retry_pending_artifacts_once())
        packet = await self.sent_packet()
        await self.socket.incoming.put(SimpleNamespace(type=WSMsgType.TEXT, data=json.dumps({
            "type": "artifact/result", "id": packet["id"], "status": "failed", "error": "busy",
        })))
        count, failed = await asyncio.wait_for(retry, 2)
        self.assertEqual((count, failed), (1, True))
        self.assertEqual(
            self.artifact_deliveries[(self.device, thread_id, attachment["id"])]["status"],
            "pending",
        )
        self.socket.sent.clear()

        retry = asyncio.create_task(self.session._retry_pending_artifacts_once())
        packet = await self.sent_packet()
        await self.socket.incoming.put(SimpleNamespace(type=WSMsgType.TEXT, data=json.dumps({
            "type": "artifact/result", "id": packet["id"], "status": "stored",
            "attachment_id": attachment["id"], "sha256": digest,
        })))
        count, failed = await asyncio.wait_for(retry, 2)
        self.assertEqual((count, failed), (1, False))
        self.assertEqual(
            self.artifact_deliveries[(self.device, thread_id, attachment["id"])]["status"],
            "delivered",
        )

    async def test_preparation_failures_rotate_a_full_retry_page(self):
        thread_id = self.own_thread()
        attachment_ids = [str(uuid.UUID(int=index)) for index in range(1, 102)]
        for attachment_id in attachment_ids:
            self.artifact_deliveries[(self.device, thread_id, attachment_id)] = {
                "owner_id": self.device,
                "thread_id": thread_id,
                "attachment_id": attachment_id,
                "name": "missing.png",
                "mime": "image/png",
                "size": 1,
                "path": str(self.service.upload_dir / attachment_id),
                "status": "pending",
                "sha256": "",
                "attempts": 0,
                "updated_at": 0,
            }
        self.session.executor_ready = True
        failure = AsyncMock(side_effect=WorkerError("unavailable before preparation"))
        with patch.object(self.session, "deliver_artifact", new=failure):
            self.assertEqual(await self.session._retry_pending_artifacts_once(), (100, True))
            self.assertEqual(await self.session._retry_pending_artifacts_once(), (100, True))
        attempted = [call.args[0]["id"] for call in failure.await_args_list]
        self.assertEqual(attempted[:100], attachment_ids[:100])
        self.assertEqual(attempted[100], attachment_ids[100])

    async def test_retry_pump_survives_transient_store_exception_without_reconnect(self):
        calls = 0

        def flaky_pending(owner_id, limit):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise sqlite3.OperationalError("database is temporarily busy")
            self.session.executor_ready = False
            return []

        self.session.executor_ready = True
        with self.assertLogs("codex_lan.host_worker", level="ERROR") as reported:
            with (
                patch.object(
                    self.service.store, "pending_artifact_deliveries", new=flaky_pending
                ),
                patch("codex_lan.host_worker.ARTIFACT_RETRY_SECONDS", 0),
            ):
                await asyncio.wait_for(self.session._retry_pending_artifacts(), 2)
        self.assertEqual(calls, 2)
        self.assertIn("retry sweep failed", reported.output[0])
        self.assertFalse(self.socket.closed)

    async def test_generated_artifact_failure_is_safe_and_does_not_close_connector(self):
        data = b"\x89PNG\r\n\x1a\nfixture"
        self.service.upload_dir.mkdir()
        source = self.service.upload_dir / "canonical-upload"
        source.write_bytes(data)
        attachment = {
            "id": str(uuid.uuid4()), "name": "generated.png", "mime": "image/png",
            "size": len(data), "path": str(source),
        }
        thread_id = self.own_thread()
        self.database.execute(
            "INSERT INTO attachments VALUES (?,?,?,?,?,?)",
            (attachment["id"], self.device, attachment["name"], attachment["mime"], attachment["size"], attachment["path"]),
        )
        self.session.executor_ready = True
        self.session.reader_task = asyncio.create_task(self.session._read_executor())
        delivery = asyncio.create_task(
            self.session.deliver_artifact(attachment, thread_id)
        )
        packet = await self.sent_packet()
        await self.socket.incoming.put(SimpleNamespace(type=WSMsgType.TEXT, data=json.dumps({
            "type": "artifact/result", "id": packet["id"],
            "status": "failed", "error": "hash_mismatch",
        })))
        with self.assertRaisesRegex(WorkerError, "could not be stored") as failure:
            await asyncio.wait_for(delivery, 2)
        self.assertNotIn("hash_mismatch", str(failure.exception))
        self.assertEqual(self.session.artifact_waiters, {})
        self.assertFalse(self.socket.closed)

    async def test_generated_artifact_cannot_cross_device_ownership(self):
        data = b"\x89PNG\r\n\x1a\nprivate"
        self.service.upload_dir.mkdir()
        source = self.service.upload_dir / "canonical-upload"
        source.write_bytes(data)
        attachment = {
            "id": str(uuid.uuid4()), "name": "generated.png", "mime": "image/png",
            "size": len(data), "path": str(source),
        }
        self.database.execute(
            "INSERT INTO attachments VALUES (?,?,?,?,?,?)",
            (attachment["id"], str(uuid.uuid4()), attachment["name"], attachment["mime"],
             attachment["size"], attachment["path"]),
        )
        self.session.executor_ready = True
        with self.assertRaisesRegex(WorkerError, "no committed delivery intent"):
            await self.session.deliver_artifact(attachment, self.own_thread())
        self.assertEqual(self.socket.sent, [])

    async def test_wrong_cached_history_is_not_resumed_as_another_conversation(self):
        identifier = self.own_thread()
        directory = self.session.state_dir / "imported"
        directory.mkdir(parents=True)
        candidate = directory / (identifier + ".jsonl")
        candidate.write_text(self.history(str(uuid.uuid4())))
        self.session.thread_paths[identifier] = candidate
        with self.assertRaises(ConnectionError):
            await self.session.history_path(identifier)

    async def test_paginated_client_history_is_not_silently_replaced_or_incompletely_imported(self):
        identifier = self.own_thread()
        self.session.executor_ready = True
        self.session.reader_task = asyncio.create_task(self.session._read_executor())
        task = asyncio.create_task(self.session.history_path(identifier))
        for _ in range(20):
            if self.socket.sent:
                break
            await asyncio.sleep(0)
        content = json.dumps({"type": "session_meta", "payload": {"id": identifier, "history_mode": "paginated"}})
        response = {"type": "history/result", "id": self.socket.sent[0]["id"], "content": content}
        await self.socket.incoming.put(SimpleNamespace(type=WSMsgType.TEXT, data=json.dumps(response)))
        with self.assertRaisesRegex(WorkerError, "chat messages are preserved"):
            await asyncio.wait_for(task, 2)
        self.assertFalse((self.session.state_dir / "imported" / (identifier + ".jsonl")).exists())

    async def test_missing_history_fails_without_silently_starting_a_new_thread(self):
        identifier = self.own_thread()
        self.session.executor_ready = True
        server = _RemoteAppServer(self.session)
        with patch.object(self.session, "history_path", new=AsyncMock(side_effect=WorkerError("History unavailable"))), \
                patch.object(CodexAppServer, "request", new=AsyncMock()) as request:
            with self.assertRaisesRegex(WorkerError, "History unavailable"):
                await server.request("thread/resume", {"threadId": identifier})
            request.assert_not_awaited()

    async def test_resume_uses_private_host_configuration_then_turn_binds_remote_environment(self):
        identifier = self.own_thread()
        self.session.executor_ready = True
        server = _RemoteAppServer(self.session)
        path = self.session.state_dir / "sessions" / (identifier + ".jsonl")
        result = {"thread": {"id": identifier, "cwd": str(self.session.host_cwd)}}
        with patch.object(self.session, "history_path", new=AsyncMock(return_value=path)), \
                patch.object(CodexAppServer, "request", new=AsyncMock(return_value=result)) as request:
            response = await server.request("thread/resume", {"threadId": identifier, "cwd": "/remote/other"})
            sent = request.await_args.args[1]
            self.assertEqual(sent["cwd"], str(self.session.host_cwd))
            self.assertEqual(sent["path"], str(path))
            self.assertEqual(response["thread"]["cwd"], "/remote/other")

    async def test_uploads_remain_host_local_files_and_are_authorized_per_device(self):
        self.service.upload_dir.mkdir()
        image = self.service.upload_dir / "fixture.png"
        image.write_bytes(b"\x89PNG\r\n\x1a\nfixture")
        self.service.store.get_attachment = lambda owner, identifier: {"path": str(image), "mime": "image/png"}
        uploaded = await self.session.connection._download_attachment({"attachment_id": "owned-image"})
        self.session.executor_ready = True
        server = _RemoteAppServer(self.session)
        inputs = [{"type": "localImage", "path": uploaded["path"]}]
        with patch.object(CodexAppServer, "request", new=AsyncMock(return_value={})) as request:
            await server.request("turn/start", {"input": inputs})
            self.assertEqual(request.await_args.args[1]["input"], inputs)
            with self.assertRaisesRegex(WorkerError, "not been authorized"):
                await server.request("turn/start", {"input": [{"type": "localImage", "path": "/etc/passwd"}]})

    async def test_loopback_bridge_translates_native_json_text_to_binary_json_lines(self):
        await self.session._start_bridge()
        self.assertTrue(self.session.bridge_url.startswith("ws://127.0.0.1:"))
        self.session.executor_ready = True
        self.session.reader_task = asyncio.create_task(self.session._read_executor())
        async with aiohttp.ClientSession(trust_env=False) as http:
            async with http.ws_connect(self.session.bridge_url) as bridge:
                request = {"id": 1, "method": "initialize", "params": {"clientName": "fixture"}}
                await bridge.send_json(request)
                wire = await asyncio.wait_for(self.socket.binary.get(), 2)
                self.assertTrue(wire.endswith(b"\n"))
                self.assertEqual(json.loads(wire), request)
                response = {"id": 1, "result": {"safe": True}}
                await self.socket.incoming.put(SimpleNamespace(type=WSMsgType.BINARY,
                    data=(json.dumps(response) + "\n").encode()))
                received = await bridge.receive(timeout=2)
                self.assertEqual(received.type, WSMsgType.TEXT)
                self.assertEqual(json.loads(received.data), response)
            for _ in range(20):
                if self.socket.closed:
                    break
                await asyncio.sleep(0)
            self.assertTrue(self.socket.closed)
            self.assertFalse(self.session.executor_ready)

    async def test_credentials_stay_on_host_and_peer_waits_for_remote_environment(self):
        entered = asyncio.Event()
        release = asyncio.Event()
        closed = asyncio.Event()
        captured = []

        async def initialize(tokens):
            captured.append(dict(tokens))
            return ([{"id": "test-model"}], "test-version")

        async def request(method, _params=None, timeout=90):
            if method == "environment/info":
                entered.set()
                await release.wait()
            return {}

        async def close_native():
            closed.set()

        with patch.object(_RemoteAppServer, "launch", new=AsyncMock()), \
                patch.object(_RemoteAppServer, "initialize", new=staticmethod(initialize)), \
                patch.object(_RemoteAppServer, "request", new=staticmethod(request)), \
                patch.object(_RemoteAppServer, "wait_closed", new=staticmethod(closed.wait)), \
                patch.object(_RemoteAppServer, "close", new=staticmethod(close_native)):
            task = asyncio.create_task(self.session.run())
            await asyncio.wait_for(entered.wait(), 2)
            self.assertIs(self.runtime.peers[self.device], self.session.peer)
            self.assertFalse(self.session.peer.ready)
            self.assertEqual(self.session.peer.connection["state"], "connecting")
            release.set()
            for _ in range(20):
                if self.session.peer.ready:
                    break
                await asyncio.sleep(0)
            self.assertTrue(self.session.peer.ready)
            self.assertEqual(self.session.peer.connection, {})
            self.assertEqual(captured[0]["accessToken"], "HOST-ONLY-SECRET")
            self.assertNotIn("HOST-ONLY", json.dumps(self.socket.sent))
            self.assertEqual(self.socket.sent[-1]["type"], "executor_ready")
            await self.socket.close()
            await asyncio.wait_for(task, 2)
            self.assertFalse(self.session.peer.ready)
            self.assertTrue(closed.is_set())

    async def test_unexpected_rpc_failure_replies_immediately_and_keeps_connector_usable(self):
        secret = "HOST-INTERNAL-SECRET"
        thread_id = str(uuid.uuid4())
        codex = SimpleNamespace(
            request=AsyncMock(side_effect=[AssertionError(secret), {"thread": {"id": thread_id}}]),
            close=AsyncMock(),
        )
        self.session.connection.codex = codex
        self.session.executor_ready = True
        self.session.peer.ready = True

        with self.assertRaises(RuntimeError) as failure:
            await asyncio.wait_for(self.session.peer.call("thread/start", {"cwd": "/remote/client-only"}), 0.5)
        self.assertIn("internal error", str(failure.exception))
        self.assertNotIn(secret, str(failure.exception))
        self.assertFalse(self.socket.closed)
        self.assertTrue(self.session.executor_ready)

        result = await asyncio.wait_for(
            self.session.peer.call("thread/start", {"cwd": "/remote/client-only"}), 0.5
        )
        self.assertEqual(result, {"thread": {"id": thread_id}})
        await asyncio.sleep(0)
        self.assertFalse(self.session.connection.active_rpc_ids)
        self.assertFalse(self.session.connection.rpc_tasks)

    async def test_cancelled_close_caller_does_not_abandon_owned_cleanup(self):
        gate = asyncio.Event()
        async def disconnect(_peer):
            await gate.wait()

        self.runtime.disconnect = AsyncMock(side_effect=disconnect)
        caller = asyncio.create_task(self.session.close())
        await asyncio.sleep(0)
        caller.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await caller
        self.assertIsNotNone(self.session._close_task)
        gate.set()
        await asyncio.wait_for(self.session.close(), 2)
        self.runtime.disconnect.assert_awaited_once_with(self.session.peer)
        self.session.connection.gateway.close.assert_awaited_once()


class HostAuthRefreshTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.source_tokens = {"accessToken": "REPLACE_ME", "chatgptAccountId": "example-account"}
        self.auth = SimpleNamespace(tokens=AsyncMock(return_value=self.source_tokens))
        self.connection = _HostConnection.__new__(_HostConnection)
        self.connection.owner = SimpleNamespace(service=SimpleNamespace(runtime=SimpleNamespace(auth=self.auth)))
        self.connection.codex = SimpleNamespace(respond=AsyncMock(), respond_error=AsyncMock())

    async def blocked(self, *_args, **_kwargs):
        await asyncio.Event().wait()

    async def test_refresh_bounds_provider_wait(self):
        self.auth.tokens.side_effect = self.blocked
        with patch("codex_lan.host_worker.REFRESH_TIMEOUT_SECONDS", 0.02):
            await asyncio.wait_for(self.connection._refresh_auth(801), 0.5)
        self.connection.codex.respond_error.assert_awaited_once()

    async def test_refresh_bounds_native_response_and_error_response(self):
        self.connection.codex.respond.side_effect = self.blocked
        self.connection.codex.respond_error.side_effect = self.blocked
        with patch("codex_lan.host_worker.REFRESH_TIMEOUT_SECONDS", 0.02):
            await asyncio.wait_for(self.connection._refresh_auth(802), 0.5)
        self.assertEqual(self.connection.codex.respond.call_args.args[1], {})
        self.assertEqual(self.source_tokens["accessToken"], "REPLACE_ME")

    async def test_refresh_cancellation_clears_only_transient_tokens(self):
        started = asyncio.Event()

        async def blocked_response(*_args):
            started.set()
            await asyncio.Event().wait()

        self.connection.codex.respond.side_effect = blocked_response
        task = asyncio.create_task(self.connection._refresh_auth(803))
        await asyncio.wait_for(started.wait(), 0.5)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.connection.codex.respond.call_args.args[1], {})
        self.assertEqual(self.source_tokens["accessToken"], "REPLACE_ME")
        self.connection.codex.respond_error.assert_not_called()


if __name__ == "__main__":
    unittest.main()
