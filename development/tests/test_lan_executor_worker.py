"""CODEX: Client-only executor framing, credential isolation and rollout migration tests."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import stat
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import aiohttp
from aiohttp import web

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from project_layout import configure_imports, source_file
configure_imports(__file__)

from codex_lan.worker import (
    DeviceAccessError, ExecutorConnection, ExecutorUpgradeRequired,
    MAX_ATTACHMENT_BYTES, MAX_GENERATED_ARTIFACT_BYTES, MAX_JSON_BYTES, MAX_WS_BYTES,
    PayloadLimitError, Worker,
    WorkerConfig, WorkerConnection, WorkerError,
    build_endpoints, save_device_token,
)
from test_lan_worker import TemporaryServer


class ExecutorHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.state = self.root / "state"
        self.history = self.state / "sessions" / "2026" / "09" / "09"
        self.history.mkdir(parents=True)
        self.identifier = str(uuid.uuid4())
        self.file = self.history / f"rollout-2026-09-09T01-00-00-{self.identifier}.jsonl"
        self.config = WorkerConfig(
            build_endpoints("https://host.invalid/prefix"), self.root / "token",
            self.root, Path(sys.executable), self.state, "Test client",
        )
        self.connection = ExecutorConnection(self.config, None, "device-token")

    def test_history_is_exact_metadata_verified_and_unmodified(self):
        content = json.dumps({"type": "session_meta", "payload": {"id": self.identifier}}) + "\n"
        content += '{"type":"response_item","payload":{"text":"hello \u0633\u0644\u0627\u0645"}}\n'
        self.file.write_text(content)
        self.assertEqual(self.connection.read_history(self.identifier), content)
        self.assertIsNone(self.connection.read_history(str(uuid.uuid4())))
        self.assertEqual(self.connection.endpoint, "wss://host.invalid/prefix/api/executor")

    def test_history_rejects_traversal_wrong_metadata_and_oversize(self):
        for value in (None, "../auth.json", "/etc/passwd", self.identifier.upper()):
            with self.subTest(value=value), self.assertRaises(WorkerError):
                self.connection.read_history(value)
        self.file.write_text(json.dumps({"type": "session_meta", "payload": {"id": str(uuid.uuid4())}}))
        with self.assertRaisesRegex(WorkerError, "does not match"):
            self.connection.read_history(self.identifier)
        with self.file.open("wb") as stream:
            stream.truncate(MAX_WS_BYTES + 1)
        with self.assertRaises(WorkerError):
            self.connection.read_history(self.identifier)

    def test_history_does_not_follow_file_or_directory_symlinks(self):
        external = self.root / "external.jsonl"
        external.write_text(json.dumps({"type": "session_meta", "payload": {"id": self.identifier}}))
        self.file.symlink_to(external)
        self.assertIsNone(self.connection.read_history(self.identifier))
        self.file.unlink()
        external_dir = self.root / "external"
        external_dir.mkdir()
        (external_dir / self.file.name).write_text(external.read_text())
        (self.history / "outside").symlink_to(external_dir, target_is_directory=True)
        self.assertIsNone(self.connection.read_history(self.identifier))

    def test_history_does_not_follow_sessions_symlink(self):
        linked_state = self.root / "linked-state"
        linked_state.mkdir()
        (linked_state / "sessions").symlink_to(self.state / "sessions", target_is_directory=True)
        config = WorkerConfig(self.config.endpoints, self.config.token_file, self.root,
                              self.config.codex, linked_state, "Test client")
        with self.assertRaises(OSError):
            ExecutorConnection(config, None, "token").read_history(self.identifier)

    def test_environment_has_no_provider_identity_or_proxy(self):
        with mock.patch.dict(os.environ, {
            "OPENAI_API_KEY": "secret", "openai_auth_token": "secret",
            "CODEX_API_KEY": "secret", "CODEX_HOME": "/old-auth", "CODEX_THREAD_ID": "thread",
            "CHATGPT_ACCOUNT_ID": "account", "AZURE_OPENAI_API_KEY": "secret",
            "HTTP_PROXY": "http://proxy.invalid", "https_proxy": "http://proxy.invalid",
            "ALL_PROXY": "socks5://proxy.invalid", "NO_PROXY": "localhost", "PATH": "/bin",
        }, clear=True):
            environment = self.connection.environment(self.state / "executor")
        self.assertEqual(environment, {
            "PATH": "/bin", "CODEX_HOME": str(self.state / "executor"), "LC_ALL": "C.UTF-8",
        })

    def test_native_frames_are_complete_single_json_lines(self):
        line = b'{"id":1,"method":"initialize","params":{}}\n'
        self.assertIs(self.connection._native_line(line), line)
        for bad in (b"", b"{}", b"{}\n{}\n", b"[]\n", b"invalid\n"):
            with self.subTest(bad=bad), self.assertRaises(WorkerError):
                self.connection._native_line(bad)
        with self.assertRaises(PayloadLimitError):
            self.connection._native_line(b"x" * (MAX_WS_BYTES + 1))


class ExecutorConnectionTests(unittest.IsolatedAsyncioTestCase):
    def config(self, root, base):
        return WorkerConfig(build_endpoints(base), root / "token", root,
                            Path(sys.executable), root / "state", "Test client")

    async def test_real_stdio_roundtrip_does_not_start_model_or_gateway(self):
        seen = {}
        finished = asyncio.Event()
        line = b'{"id":1,"method":"initialize","params":{"text":"hello"}}\n'

        async def executor(request):
            seen["authorization"] = request.headers.get("Authorization")
            seen["origin"] = request.headers.get("Origin")
            websocket = web.WebSocketResponse()
            await websocket.prepare(request)
            seen["hello"] = await websocket.receive_json()
            await websocket.send_json({"type": "executor_ready"})
            await websocket.send_bytes(line)
            seen["native"] = await websocket.receive()
            await websocket.send_json({"type": "history/read", "id": "missing", "thread_id": str(uuid.uuid4())})
            seen["history"] = await websocket.receive_json()
            await websocket.close()
            finished.set()
            return websocket

        native_spawn = asyncio.create_subprocess_exec
        spawned = []

        async def harmless_executor(*args, **kwargs):
            seen["args"] = args
            seen["env"] = kwargs["env"]
            process = await native_spawn(sys.executable, "-u", "-c",
                                        "import sys\nfor line in sys.stdin.buffer:\n sys.stdout.buffer.write(line); sys.stdout.buffer.flush()",
                                        **kwargs)
            spawned.append(process)
            return process

        async with TemporaryServer([web.get("/api/executor", executor)]) as server:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                async with aiohttp.ClientSession(trust_env=False) as session:
                    connection = ExecutorConnection(self.config(root, server.base), session, "private-device-token")
                    with mock.patch("codex_lan.worker.asyncio.create_subprocess_exec", harmless_executor), mock.patch(
                        "codex_lan.worker.AdaptiveProxy", side_effect=AssertionError("No client gateway")
                    ), mock.patch("codex_lan.worker.CodexAppServer", side_effect=AssertionError("No client model")):
                        with self.assertRaises(ConnectionError):
                            await asyncio.wait_for(connection.run(), 5)
                    await asyncio.wait_for(finished.wait(), 2)
                    self.assertFalse(session.closed)
                self.assertEqual(seen["authorization"], "Bearer private-device-token")
                self.assertEqual(seen["origin"], server.base)
                self.assertEqual(seen["hello"]["type"], "executor_hello")
                self.assertEqual(seen["hello"]["protocol"], 1)
                self.assertEqual(seen["hello"]["capabilities"], ["artifact-store-v1", "file-attachments-v1"])
                self.assertEqual(seen["hello"]["cwd"], str(root))
                self.assertEqual(seen["args"][1:], ("exec-server", "--listen", "stdio", "--concurrent-requests", "16"))
                self.assertEqual(seen["native"].type, aiohttp.WSMsgType.BINARY)
                self.assertEqual(seen["native"].data, line)
                self.assertEqual(seen["history"], {"type": "history/result", "id": "missing", "content": None})
                self.assertTrue(connection.established)
                self.assertIsNotNone(spawned[0].returncode)
                self.assertIsNone(connection.process)
                self.assertEqual(connection.tasks, set())
                self.assertEqual(Path(seen["env"]["CODEX_HOME"]), root / "state" / "executor")
                self.assertEqual((root / "state" / "executor").stat().st_mode & 0o777, 0o700)

    async def test_generated_artifact_download_is_authenticated_private_atomic_and_idempotent(self):
        data = b"\x89PNG\r\n\x1a\n" + b"generated-image-content"
        digest = hashlib.sha256(data).hexdigest()
        attachment_id = str(uuid.uuid4())
        thread_id = str(uuid.uuid4())
        requests = []

        async def attachment(request):
            requests.append((request.match_info["attachment"], request.headers.get("Authorization")))
            return web.Response(body=data, content_type="image/png")

        async with TemporaryServer([web.get("/api/attachments/{attachment}", attachment)]) as server:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                async with aiohttp.ClientSession(trust_env=False) as session:
                    connection = ExecutorConnection(self.config(root, server.base), session, "private-device-token")
                    connection.channel = SimpleNamespace(send=mock.AsyncMock())
                    base = {
                        "type": "artifact/store", "attachment_id": attachment_id,
                        "thread_id": thread_id, "name": "render.png", "mime": "image/png",
                        "size": len(data), "sha256": digest,
                    }
                    for _ in range(2):
                        await connection._start_artifact_store({**base, "id": uuid.uuid4().hex})
                    await asyncio.gather(*list(connection.artifact_tasks))
                    results = [call.args[0] for call in connection.channel.send.await_args_list]

                expected = root / "state" / "generated_images" / thread_id / f"{attachment_id}.png"
                self.assertEqual(requests, [(attachment_id, "Bearer private-device-token")])
                self.assertEqual([result["status"] for result in results], ["stored", "stored"])
                self.assertEqual([set(result) for result in results], [
                    {"type", "id", "status", "attachment_id", "sha256"},
                    {"type", "id", "status", "attachment_id", "sha256"},
                ])
                self.assertEqual([result["attachment_id"] for result in results], [attachment_id, attachment_id])
                self.assertEqual([result["sha256"] for result in results], [digest, digest])
                self.assertEqual(expected.read_bytes(), data)
                self.assertEqual(stat.S_IMODE(expected.stat().st_mode), 0o600)
                self.assertEqual(stat.S_IMODE(expected.parent.stat().st_mode), 0o700)
                self.assertEqual(stat.S_IMODE(expected.parent.parent.stat().st_mode), 0o700)
                self.assertEqual([path.name for path in expected.parent.iterdir()], [expected.name])

    def test_artifact_size_boundary_leaves_native_frame_headroom(self):
        self.assertEqual(MAX_ATTACHMENT_BYTES, 25 * 1024 * 1024)
        self.assertEqual(MAX_GENERATED_ARTIFACT_BYTES, 5 * 1024 * 1024)
        packet = {
            "type": "artifact/store", "id": uuid.uuid4().hex,
            "attachment_id": str(uuid.uuid4()), "thread_id": str(uuid.uuid4()),
            "name": "boundary.png", "mime": "image/png", "size": MAX_GENERATED_ARTIFACT_BYTES,
            "sha256": "0" * 64,
        }
        self.assertEqual(
            ExecutorConnection._artifact_request(packet)["size"], MAX_GENERATED_ARTIFACT_BYTES
        )
        with self.assertRaisesRegex(WorkerError, "invalid artifact size"):
            ExecutorConnection._artifact_request(
                {**packet, "size": MAX_GENERATED_ARTIFACT_BYTES + 1}
            )
        encoded_ceiling = ((MAX_GENERATED_ARTIFACT_BYTES + 2) // 3) * 4
        self.assertLessEqual(encoded_ceiling + MAX_JSON_BYTES, MAX_WS_BYTES)

    async def test_generated_artifact_rejects_hash_and_magic_without_publishing_partial_files(self):
        valid = b"\x89PNG\r\n\x1a\n" + b"valid-image"
        invalid_magic = b"<svg>not a raster image</svg>"
        attachment_ids = [str(uuid.uuid4()), str(uuid.uuid4())]
        bodies = {attachment_ids[0]: valid, attachment_ids[1]: invalid_magic}

        async def attachment(request):
            return web.Response(body=bodies[request.match_info["attachment"]], content_type="image/png")

        async with TemporaryServer([web.get("/api/attachments/{attachment}", attachment)]) as server:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                thread_id = str(uuid.uuid4())
                async with aiohttp.ClientSession(trust_env=False) as session:
                    connection = ExecutorConnection(self.config(root, server.base), session, "private-device-token")
                    connection.channel = SimpleNamespace(send=mock.AsyncMock())
                    packets = [
                        {
                            "type": "artifact/store", "id": uuid.uuid4().hex,
                            "attachment_id": attachment_ids[0], "thread_id": thread_id,
                            "name": "bad-hash.png", "mime": "image/png", "size": len(valid),
                            "sha256": "0" * 64,
                        },
                        {
                            "type": "artifact/store", "id": uuid.uuid4().hex,
                            "attachment_id": attachment_ids[1], "thread_id": thread_id,
                            "name": "bad-magic.png", "mime": "image/png", "size": len(invalid_magic),
                            "sha256": hashlib.sha256(invalid_magic).hexdigest(),
                        },
                    ]
                    for packet in packets:
                        await connection._start_artifact_store(packet)
                        await asyncio.gather(*list(connection.artifact_tasks))
                    results = [call.args[0] for call in connection.channel.send.await_args_list]

                self.assertEqual([result["status"] for result in results], ["failed", "failed"])
                self.assertEqual([result["error"] for result in results], ["hash_mismatch", "type_mismatch"])
                self.assertTrue(all("path" not in result for result in results))
                target = root / "state" / "generated_images" / thread_id
                self.assertEqual(list(target.iterdir()), [])

    async def test_old_server_and_denied_device_never_spawn_executor(self):
        for status, failure in ((404, ExecutorUpgradeRequired), (403, DeviceAccessError)):
            async def reject(_request):
                return web.Response(status=status)

            async with TemporaryServer([web.get("/api/executor", reject)]) as server:
                with tempfile.TemporaryDirectory() as directory:
                    async with aiohttp.ClientSession() as session:
                        connection = ExecutorConnection(self.config(Path(directory), server.base), session, "secret")
                        with mock.patch.object(connection, "_launch", new_callable=mock.AsyncMock) as launch:
                            with self.assertRaises(failure):
                                await connection.run()
                            launch.assert_not_called()

    async def test_redirect_is_rejected_before_any_target_request(self):
        reached = []

        async def target(request):
            reached.append(request.headers.get("Authorization"))
            return web.Response(status=403)

        async def redirect(_request):
            return web.Response(status=307, headers={"Location": "/redirect-target"})

        async with TemporaryServer([web.get("/api/executor", redirect), web.get("/redirect-target", target)]) as server:
            with tempfile.TemporaryDirectory() as directory:
                async with aiohttp.ClientSession() as session:
                    connection = ExecutorConnection(self.config(Path(directory), server.base), session, "secret")
                    with self.assertRaisesRegex(WorkerError, "redirect"):
                        await connection.run()
        self.assertEqual(reached, [])

    async def test_cancellation_reaps_only_the_spawned_process_group(self):
        ready = asyncio.Event()
        stopped = asyncio.Event()

        async def executor(request):
            websocket = web.WebSocketResponse()
            await websocket.prepare(request)
            await websocket.receive_json()
            ready.set()
            async for _message in websocket:
                pass
            stopped.set()
            return websocket

        native_spawn = asyncio.create_subprocess_exec
        spawned = []

        async def harmless_executor(*args, **kwargs):
            process = await native_spawn(sys.executable, "-u", "-c", "import time; time.sleep(60)", **kwargs)
            spawned.append(process)
            return process

        async with TemporaryServer([web.get("/api/executor", executor)]) as server:
            with tempfile.TemporaryDirectory() as directory:
                async with aiohttp.ClientSession() as session:
                    connection = ExecutorConnection(self.config(Path(directory), server.base), session, "secret")
                    with mock.patch("codex_lan.worker.asyncio.create_subprocess_exec", harmless_executor):
                        task = asyncio.create_task(connection.run())
                        await asyncio.wait_for(ready.wait(), 5)
                        task.cancel()
                        with self.assertRaises(asyncio.CancelledError):
                            await asyncio.wait_for(task, 5)
                        await asyncio.wait_for(stopped.wait(), 2)
                    self.assertIsNotNone(spawned[0].returncode)
                    self.assertIsNone(connection.process)
                    self.assertFalse(connection.tasks)

    async def test_worker_remote_mode_retries_lan_without_legacy_model_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.config(root, "https://host.invalid")
            save_device_token(config.token_file, "device-token")
            output = []
            worker = Worker(config, output.append)
            with mock.patch.object(worker, "_wait_until_approved", new_callable=mock.AsyncMock) as approved, mock.patch.object(
                ExecutorConnection, "run", new_callable=mock.AsyncMock
            ) as run, mock.patch.object(WorkerConnection, "run", new_callable=mock.AsyncMock) as legacy, mock.patch(
                "codex_lan.worker.asyncio.sleep", new_callable=mock.AsyncMock
            ):
                approved.return_value = {"id": "device", "status": "approved"}
                run.side_effect = [ConnectionError("LAN interrupted"), asyncio.CancelledError()]
                with self.assertRaises(asyncio.CancelledError):
                    await worker.run()
                self.assertEqual(run.await_count, 2)
                legacy.assert_not_called()
            self.assertTrue(any("runs tools only" in line for line in output))
            self.assertTrue(any("Worker connection ended: role=executor" in line for line in output))
            retry = next(line for line in output if "Reconnecting in" in line)
            self.assertIn("failure_type=ConnectionError", retry)
            self.assertIn("attempt=1", retry)
            self.assertIn("retry_in_seconds=", retry)
            self.assertNotIn("LAN interrupted", "\n".join(output))
            self.assertNotIn("device-token", "\n".join(output))

    async def test_established_connections_reset_backoff_but_failed_handshakes_do_not(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.config(root, "https://host.invalid")
            save_device_token(config.token_file, "device-token")
            output = []
            worker = Worker(config, output.append)
            connection_states = [False, False, True, False, True, None]
            waits = []

            async def run_connection(connection):
                state = connection_states.pop(0)
                if state is None:
                    raise asyncio.CancelledError()
                connection.established = state
                raise ConnectionError("LAN interrupted")

            async def record_sleep(delay):
                waits.append(delay)

            with mock.patch.object(worker, "_wait_until_approved", new_callable=mock.AsyncMock) as approved, mock.patch.object(
                ExecutorConnection, "run", run_connection
            ), mock.patch.object(WorkerConnection, "run", new_callable=mock.AsyncMock) as legacy, mock.patch(
                "codex_lan.worker.asyncio.sleep", new=record_sleep
            ), mock.patch("codex_lan.worker.random.uniform", side_effect=lambda low, high: (low + high) / 2):
                approved.return_value = {"id": "device", "status": "approved"}
                with self.assertRaises(asyncio.CancelledError):
                    await worker.run()

            self.assertEqual(waits, [0.5, 1.0, 0.5, 1.0, 0.5])
            retries = [line for line in output if "Reconnecting in" in line]
            self.assertEqual(
                [next(part for part in line.split() if part.startswith("attempt=")) for line in retries],
                ["attempt=1", "attempt=2", "attempt=1", "attempt=2", "attempt=1"],
            )
            legacy.assert_not_called()

    async def test_worker_incompatible_host_requires_upgrade_without_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.config(root, "https://host.invalid")
            save_device_token(config.token_file, "device-token")
            worker = Worker(config, lambda _line: None)
            with mock.patch.object(worker, "_wait_until_approved", new_callable=mock.AsyncMock) as approved, mock.patch.object(
                ExecutorConnection, "run", new_callable=mock.AsyncMock
            ) as run, mock.patch.object(WorkerConnection, "run", new_callable=mock.AsyncMock) as legacy:
                approved.return_value = {"id": "device", "status": "approved"}
                run.side_effect = ExecutorUpgradeRequired("Update the host.")
                with self.assertRaises(ExecutorUpgradeRequired):
                    await worker.run()
                run.assert_awaited_once()
                legacy.assert_not_called()

    @unittest.skipUnless((Path.home() / ".local/bin/codex").is_file(), "Installed Codex CLI unavailable")
    async def test_real_native_executor_initialize_without_provider_auth_or_inference(self):
        seen = {}

        async def executor(request):
            websocket = web.WebSocketResponse()
            await websocket.prepare(request)
            seen["hello"] = await websocket.receive_json()
            initialize = {"id": 1, "method": "initialize", "params": {
                "clientName": "codex-environment", "resumeSessionId": None,
            }}
            await websocket.send_bytes(json.dumps(initialize).encode() + b"\n")
            first = await asyncio.wait_for(websocket.receive(), 5)
            seen["initialize"] = json.loads(first.data)
            await websocket.send_bytes(b'{"method":"initialized","params":{}}\n')
            await websocket.send_bytes(b'{"id":2,"method":"environment/info","params":{}}\n')
            second = await asyncio.wait_for(websocket.receive(), 5)
            seen["info"] = json.loads(second.data)
            await websocket.close()
            return websocket

        async with TemporaryServer([web.get("/api/executor", executor)]) as server:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                config = WorkerConfig(build_endpoints(server.base), root / "token", root,
                                      Path.home() / ".local/bin/codex", root / "state", "Native executor test")
                async with aiohttp.ClientSession() as session:
                    connection = ExecutorConnection(config, session, "fixture-token")
                    with self.assertRaises(ConnectionError):
                        await asyncio.wait_for(connection.run(), 12)
                self.assertEqual(seen["initialize"].get("id"), 1)
                self.assertNotIn("error", seen["initialize"])
                self.assertEqual(seen["info"].get("id"), 2)
                self.assertNotIn("error", seen["info"])
                self.assertFalse((root / "state" / "executor" / "auth.json").exists())


if __name__ == "__main__":
    unittest.main()
