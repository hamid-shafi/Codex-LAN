"""CODEX: Security and protocol regressions for the local LAN worker."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
import stat
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from aiohttp import web

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from project_layout import configure_imports, source_file
configure_imports(__file__)

from codex_lan.worker import (
    CodexAppServer,
    DeviceAccessError,
    JsonChannel,
    MAX_FORWARDED_COMMAND_OUTPUT,
    MAX_AGENT_THREADS_PER_SESSION,
    MAX_WS_BYTES,
    OUTPUT_OMITTED,
    PayloadLimitError,
    Worker,
    WorkerConfig,
    WorkerConnection,
    WorkerError,
    build_endpoints,
    image_extension,
    load_device_token,
    save_device_token,
)


def fake_jwt(marker="worker-secret-marker"):
    now = int(time.time())

    def encode(value):
        raw = json.dumps(value, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    payload = {
        "iss": "https://auth.openai.com",
        "aud": ["openai-api"],
        "sub": "worker-test-user",
        "iat": now,
        "nbf": now - 1,
        "exp": now + 3600,
        "https://api.openai.com/profile": {
            "email": marker + "@example.invalid",
            "email_verified": True,
            "name": marker,
        },
        "https://api.openai.com/auth": {
            "chatgpt_account_id": "worker-test-account",
            "chatgpt_plan_type": "plus",
            "chatgpt_user_id": "worker-test-user",
        },
    }
    return encode({"alg": "none", "typ": "JWT"}) + "." + encode(payload) + ".signature"


class TemporaryServer:
    def __init__(self, routes):
        self.app = web.Application()
        self.app.add_routes(routes)
        self.runner = web.AppRunner(self.app)
        self.base = ""

    async def __aenter__(self):
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await self.site.start()
        port = self.site._server.sockets[0].getsockname()[1]
        self.base = f"http://127.0.0.1:{port}"
        return self

    async def __aexit__(self, *_args):
        await self.runner.cleanup()


class WorkerUtilityTests(unittest.TestCase):
    def test_plaintext_is_loopback_only_and_all_endpoints_share_origin(self):
        endpoints = build_endpoints("http://127.0.0.1:8765/")
        self.assertEqual(endpoints.origin, "http://127.0.0.1:8765")
        self.assertEqual(endpoints.worker, "ws://127.0.0.1:8765/api/worker")
        self.assertEqual(endpoints.browser, "http://127.0.0.1:8765/")
        secure = build_endpoints("https://central.example/lan/")
        self.assertEqual(secure.worker, "wss://central.example/lan/api/worker")
        self.assertTrue(all(value.startswith("https://central.example/lan/")
                            for value in (secure.enroll, secure.me, secure.attachments)))
        for value in (
            "http://central.example",
            "ftp://127.0.0.1/files",
            "http://user:password@127.0.0.1",
            "http://127.0.0.1/?token=secret",
            "http://127.0.0.1/#secret",
        ):
            with self.subTest(value=value), self.assertRaises(WorkerError):
                build_endpoints(value)

    def test_token_file_is_created_and_required_to_be_exactly_private(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "credentials" / "device.token"
            save_device_token(path, "device-token")
            self.assertEqual(load_device_token(path), "device-token")
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            path.chmod(0o640)
            with self.assertRaises(WorkerError):
                load_device_token(path)
            path.unlink()
            path.symlink_to(Path(directory) / "missing")
            with self.assertRaises(WorkerError):
                load_device_token(path)

    def test_image_type_comes_from_bytes_not_a_remote_filename(self):
        self.assertEqual(image_extension(b"\x89PNG\r\n\x1a\nrest"), ".png")
        self.assertEqual(image_extension(b"\xff\xd8\xffrest"), ".jpg")
        self.assertEqual(image_extension(b"GIF89arest"), ".gif")
        self.assertEqual(image_extension(b"RIFFxxxxWEBPrest"), ".webp")
        with self.assertRaises(WorkerError):
            image_extension(b"<svg onload=alert(1)>")

    def test_command_output_is_bounded_before_forwarding(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = WorkerConfig(
                build_endpoints("http://127.0.0.1:8765"), root / "token", root,
                Path(sys.executable), root / "state", "test-worker",
            )
            connection = WorkerConnection(config, None, "device-token")
            forwarded = connection._bounded_command_output_event(
                "item/commandExecution/outputDelta",
                {"threadId": "thread-a", "itemId": "cmd", "delta": "x" * (MAX_FORWARDED_COMMAND_OUTPUT + 100)},
            )
            self.assertEqual(forwarded["delta"].count("x"), MAX_FORWARDED_COMMAND_OUTPUT)
            self.assertIn(OUTPUT_OMITTED.strip(), forwarded["delta"])
            self.assertIsNone(connection._bounded_command_output_event(
                "item/commandExecution/outputDelta", {"threadId": "thread-a", "itemId": "cmd", "delta": "late"}
            ))
            completed = connection._bounded_command_output_event(
                "item/completed",
                {"threadId": "thread-a", "item": {"id": "cmd", "type": "commandExecution",
                          "aggregatedOutput": "z" * (MAX_FORWARDED_COMMAND_OUTPUT + 100)}},
            )
            self.assertTrue(completed["item"]["aggregatedOutput"].startswith(OUTPUT_OMITTED))
            self.assertLessEqual(
                len(completed["item"]["aggregatedOutput"]),
                MAX_FORWARDED_COMMAND_OUTPUT + len(OUTPUT_OMITTED),
            )

    def test_completing_one_thread_does_not_reset_another_output_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = WorkerConfig(
                build_endpoints("http://127.0.0.1:8765"), root / "token", root,
                Path(sys.executable), root / "state", "test-worker",
            )
            connection = WorkerConnection(config, None, "device-token")
            for thread in ("thread-a", "thread-b"):
                connection._bounded_command_output_event(
                    "item/commandExecution/outputDelta",
                    {"threadId": thread, "itemId": "command", "delta": "x" * MAX_FORWARDED_COMMAND_OUTPUT},
                )
            connection._forget_command_output_thread("thread-b")
            self.assertEqual(connection._bounded_command_output_event(
                "item/commandExecution/outputDelta",
                {"threadId": "thread-a", "itemId": "command", "delta": "late"},
            )["delta"], OUTPUT_OMITTED)
            self.assertIsNone(connection._bounded_command_output_event(
                "item/commandExecution/outputDelta",
                {"threadId": "thread-a", "itemId": "command", "delta": "later"},
            ))
            self.assertEqual(connection._bounded_command_output_event(
                "item/commandExecution/outputDelta",
                {"threadId": "thread-b", "itemId": "command", "delta": "new"},
            )["delta"], "new")


class WorkerEnrollmentTests(unittest.IsolatedAsyncioTestCase):
    async def test_enroll_saves_token_opens_fragment_and_polls_until_approved(self):
        token = "private-device-token"
        seen = {"me": 0, "enroll": None}

        async def enroll(request):
            seen["enroll"] = await request.json()
            return web.json_response(
                {
                    "device": {
                        "id": "device-1",
                        "status": "pending",
                        "verification_code": "ABCD-1234",
                    },
                    "token": token,
                },
                status=201,
            )

        async def me(request):
            self.assertEqual(request.headers.get("Authorization"), f"Bearer {token}")
            seen["me"] += 1
            status_value = "pending" if seen["me"] == 1 else "approved"
            return web.json_response(
                {"device": {"id": "device-1", "status": status_value,
                            "verification_code": "ABCD-1234"}}
            )

        async with TemporaryServer(
            [web.post("/api/enroll", enroll), web.get("/api/me", me)]
        ) as server:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                output = []
                opened = []
                config = WorkerConfig(
                    build_endpoints(server.base),
                    root / "device.token",
                    root,
                    Path(sys.executable),
                    root / "state",
                    "Ubuntu client",
                    enroll=True,
                    open_browser=True,
                )

                async def stop_after_approval(_connection):
                    raise asyncio.CancelledError

                with mock.patch("codex_lan.worker.POLL_SECONDS", 0), mock.patch.object(
                    WorkerConnection, "run", stop_after_approval
                ):
                    with self.assertRaises(asyncio.CancelledError):
                        await Worker(config, output.append, opened.append).run()
                self.assertEqual(seen["enroll"], {"name": "Ubuntu client"})
                self.assertGreaterEqual(seen["me"], 2)
                self.assertEqual(load_device_token(config.token_file), token)
                self.assertEqual(opened, [server.base + "/#login=" + token])
                rendered = "\n".join(output)
                self.assertIn("Device ID: device-1", rendered)
                self.assertIn("Verification code: ABCD-1234", rendered)
                self.assertIn("Browser: " + server.base + "/", rendered)
                self.assertNotIn(token, rendered)

    async def test_existing_token_open_does_not_reenroll(self):
        token = "existing-device-token"
        calls = {"enroll": 0}

        async def enroll(_request):
            calls["enroll"] += 1
            return web.json_response({}, status=500)

        async def me(request):
            self.assertEqual(request.headers.get("Authorization"), f"Bearer {token}")
            return web.json_response(
                {"device": {"id": "device-2", "status": "approved"}}
            )

        async with TemporaryServer(
            [web.post("/api/enroll", enroll), web.get("/api/me", me)]
        ) as server:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                token_file = root / "device.token"
                save_device_token(token_file, token)
                opened = []
                config = WorkerConfig(
                    build_endpoints(server.base), token_file, root, Path(sys.executable),
                    root / "state", "Ubuntu client", open_browser=True,
                )

                async def stop_after_approval(_connection):
                    raise asyncio.CancelledError

                with mock.patch.object(WorkerConnection, "run", stop_after_approval):
                    with self.assertRaises(asyncio.CancelledError):
                        await Worker(config, lambda _line: None, opened.append).run()
                self.assertEqual(calls["enroll"], 0)
                self.assertEqual(opened, [server.base + "/#login=" + token])

    async def test_rejected_existing_token_fails_before_worker_connection(self):
        async def me(_request):
            return web.json_response({"error": "revoked"}, status=403)

        async with TemporaryServer([web.get("/api/me", me)]) as server:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                token_file = root / "device.token"
                save_device_token(token_file, "revoked-device-token")
                config = WorkerConfig(
                    build_endpoints(server.base), token_file, root, Path(sys.executable),
                    root / "state", "Ubuntu client",
                )
                with self.assertRaises(DeviceAccessError):
                    await Worker(config).run()


class WorkerProtocolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def fake_codex(self):
        path = self.root / "fake-codex"
        source = f"""#!{sys.executable}
import json, os, subprocess, sys, time
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
model_calls = 0
for line in sys.stdin:
    packet = json.loads(line)
    method = packet.get('method')
    identifier = packet.get('id')
    if method == 'initialize':
        answer = {{'id': identifier, 'result': {{'userAgent': 'fake-codex/1.0'}}}}
    elif method == 'account/login/start':
        answer = {{'id': identifier, 'result': {{'type': 'chatgptAuthTokens'}}}}
    elif method == 'model/list':
        model_calls += 1
        answer = {{'id': identifier, 'result': {{'data': [{{'id': 'fake-model', 'isDefault': True, 'childPid': child.pid, 'requestId': identifier, 'stateDir': os.environ.get('CODEX_HOME'), 'cwd': os.getcwd()}}]}}}}
    elif method == 'thread/resume':
        if packet.get('params', {{}}).get('excludeTurns'):
            answer = {{'id': identifier, 'result': {{'thread': {{'id': 'fake-thread', 'turns': []}}}}}}
        else:
            print('DO-NOT-LOG-STDERR-MARKER', file=sys.stderr, flush=True)
            answer = {{'id': identifier, 'result': {{'thread': {{'id': 'fake-thread', 'turns': ['x' * ({MAX_WS_BYTES} + 65536)]}}}}}}
    elif identifier in (501, 502, 503, 504):
        result = packet.get('result') or {{}}
        answer = {{'method': 'test/appResponse', 'params': {{'id': identifier, 'decision': result.get('decision'), 'keys': sorted(result), 'error': bool(packet.get('error'))}}}}
    elif identifier is not None:
        answer = {{'id': identifier, 'result': {{'method': method, 'requestId': identifier}}}}
    else:
        continue
    print(json.dumps(answer, separators=(',', ':')), flush=True)
    if method == 'model/list' and model_calls == 1:
        for request in (
            {{'id': 501, 'method': 'item/commandExecution/requestApproval', 'params': {{}}}},
            {{'id': 502, 'method': 'item/fileChange/requestApproval', 'params': {{}}}},
            {{'id': 503, 'method': 'item/tool/requestUserInput', 'params': {{'questions': []}}}},
            {{'id': 504, 'method': 'account/chatgptAuthTokens/refresh', 'params': {{'reason': 'unauthorized'}}}},
        ):
            print(json.dumps(request, separators=(',', ':')), flush=True)
"""
        path.write_text(source, encoding="utf-8")
        path.chmod(0o700)
        return path

    async def test_app_server_launch_sets_the_session_agent_limit(self):
        async def event(_method, _params):
            return None

        async def request(_identifier, _method, _params):
            return None

        app = CodexAppServer(
            self.fake_codex(), self.root, self.root / "agent-limit-state", event, request
        )
        try:
            await app.launch()
            command = Path(f"/proc/{app.process.pid}/cmdline").read_bytes().split(b"\0")
            self.assertIn(
                f"agents.max_concurrent_threads_per_session={MAX_AGENT_THREADS_PER_SESSION}".encode(),
                command,
            )
        finally:
            await app.close()

    async def test_worker_relay_auth_refresh_approvals_upload_and_group_cleanup(self):
        device_token = "device-transport-secret"
        first_provider_token = fake_jwt("first-provider-secret")
        refreshed_provider_token = fake_jwt("refreshed-provider-secret")
        received = []
        ready_future = asyncio.get_running_loop().create_future()

        async def attachment(request):
            self.assertEqual(request.headers.get("Authorization"), f"Bearer {device_token}")
            self.assertEqual(request.match_info["attachment"], "photo/id")
            return web.Response(body=b"\x89PNG\r\n\x1a\nimage", content_type="image/png")

        async def websocket(request):
            self.assertEqual(request.headers.get("Authorization"), f"Bearer {device_token}")
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            await ws.send_json(
                {"type": "auth", "tokens": {"accessToken": first_provider_token,
                 "chatgptAccountId": "worker-test-account", "chatgptPlanType": "plus"}}
            )
            while True:
                packet = await ws.receive_json()
                received.append(packet)
                if packet.get("type") == "auth_refresh":
                    await ws.send_json(
                        {"type": "auth_response", "id": packet["id"],
                         "tokens": {"accessToken": refreshed_provider_token,
                         "chatgptAccountId": "worker-test-account", "chatgptPlanType": "plus"}}
                    )
                if packet.get("type") == "ready":
                    ready_future.set_result(packet)
                    break
            await ws.send_json({"type": "rpc", "id": "remote-1", "method": "model/list", "params": {}})
            await ws.send_json({"type": "rpc", "id": "remote-2", "method": "account/read", "params": {}})
            await ws.send_json({"type": "rpc", "id": "remote-3", "method": "lan/upload",
                                "params": {"attachment_id": "photo/id"}})
            wanted = {"remote-1", "remote-2", "remote-3"}
            while wanted:
                packet = await ws.receive_json()
                received.append(packet)
                if packet.get("type") == "auth_refresh":
                    await ws.send_json(
                        {"type": "auth_response", "id": packet["id"],
                         "tokens": {"accessToken": refreshed_provider_token,
                         "chatgptAccountId": "worker-test-account"}}
                    )
                if packet.get("type") == "result":
                    wanted.discard(packet.get("id"))
            await ws.close()
            return ws

        async with TemporaryServer(
            [web.get("/api/worker", websocket),
             web.get("/api/attachments/{attachment:.+}", attachment)]
        ) as server:
            state = self.root / "state"
            config = WorkerConfig(
                build_endpoints(server.base), self.root / "device.token", self.root,
                self.fake_codex(), state, "test-worker",
            )
            async with __import__("aiohttp").ClientSession() as session:
                await asyncio.wait_for(
                    WorkerConnection(config, session, device_token).run(), timeout=10
                )
            ready = await ready_future
            self.assertEqual(ready["cwd"], str(self.root))
            self.assertEqual(ready["version"], "fake-codex/1.0")
            model = ready["models"][0]
            self.assertEqual(model["stateDir"], str(state))
            self.assertEqual(model["cwd"], str(self.root))
            self.assertIsInstance(model["requestId"], int)
            by_id = {packet.get("id"): packet for packet in received
                     if packet.get("type") == "result"}
            self.assertIsInstance(by_id["remote-1"]["result"]["data"][0]["requestId"], int)
            self.assertIn("not allowed", by_id["remote-2"]["error"]["message"])
            upload = Path(by_id["remote-3"]["result"]["path"])
            self.assertEqual(upload.suffix, ".png")
            self.assertEqual(stat.S_IMODE(upload.stat().st_mode), 0o600)
            self.assertEqual(upload.read_bytes(), b"\x89PNG\r\n\x1a\nimage")
            requests = [packet for packet in received if packet.get("type") == "request"]
            self.assertTrue(any(packet.get("method") == "item/tool/requestUserInput"
                                for packet in requests))
            responses = [packet["params"] for packet in received
                         if packet.get("type") == "event" and packet.get("method") == "test/appResponse"]
            self.assertTrue(any(value.get("id") == 501 and value.get("decision") == "decline"
                                for value in responses))
            self.assertTrue(any(value.get("id") == 502 and value.get("decision") == "decline"
                                for value in responses))
            self.assertTrue(any(value.get("id") == 503 and value.get("error")
                                for value in responses))
            self.assertTrue(any(value.get("id") == 504 and "accessToken" in value.get("keys", [])
                                for value in responses))
            self.assertFalse((state / "auth.json").exists())
            child_pid = model["childPid"]
            for _attempt in range(50):
                if not Path(f"/proc/{child_pid}").exists():
                    break
                await asyncio.sleep(0.02)
            self.assertFalse(Path(f"/proc/{child_pid}").exists())

    async def test_large_resume_is_bounded_redacted_and_excluded_resume_succeeds(self):
        async def event(_method, _params):
            return None

        async def request(_identifier, _method, _params):
            return None

        async def initialized_app():
            app = CodexAppServer(
                self.fake_codex(), self.root, self.root / "state", event, request
            )
            await app.launch()
            await app.initialize(
                {
                    "accessToken": fake_jwt("resume-provider-secret"),
                    "chatgptAccountId": "worker-test-account",
                }
            )
            return app

        app = await initialized_app()
        try:
            with self.assertRaises(PayloadLimitError) as raised:
                await app.request("thread/resume", {"threadId": "fake-thread"})
            self.assertIn("8 MiB", str(raised.exception))
            self.assertNotIn("DO-NOT-LOG-STDERR-MARKER", str(raised.exception))
            with self.assertRaises(PayloadLimitError):
                await app.wait_closed()
            self.assertGreater(app.stderr_bytes, 0)
        finally:
            await app.close()

        app = await initialized_app()
        try:
            result = await app.request(
                "thread/resume", {"threadId": "fake-thread", "excludeTurns": True}
            )
            self.assertEqual(result["thread"]["id"], "fake-thread")
            self.assertEqual(result["thread"]["turns"], [])
            self.assertFalse((self.root / "state" / "auth.json").exists())
        finally:
            await app.close()

    async def test_worker_channel_rejects_oversized_outbound_json(self):
        class Socket:
            def __init__(self):
                self.messages = []

            async def send_str(self, message):
                self.messages.append(message)

        socket = Socket()
        channel = JsonChannel(socket)
        await channel.send({"type": "event", "params": {"text": "small"}})
        self.assertEqual(json.loads(socket.messages[0])["params"]["text"], "small")
        with self.assertRaises(PayloadLimitError):
            await channel.send({"type": "event", "params": {"text": "x" * MAX_WS_BYTES}})
        self.assertEqual(len(socket.messages), 1)


class WorkerAuthRefreshTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        config = WorkerConfig(build_endpoints("http://127.0.0.1:8765"), root / "token", root,
                              Path(sys.executable), root / "state", "test-refresh")
        self.connection = WorkerConnection(config, None, "test-device")
        self.connection.codex = SimpleNamespace(respond=mock.AsyncMock(), respond_error=mock.AsyncMock())
        self.waiter = None

    def tearDown(self):
        self.temp.cleanup()

    async def blocked_send(self, packet):
        self.waiter = self.connection.refresh_waiters[packet["id"]]
        await asyncio.Event().wait()

    async def deliver_tokens(self, packet):
        self.waiter = self.connection.refresh_waiters[packet["id"]]
        self.waiter.set_result(self.tokens)

    async def test_deadline_covers_blocked_refresh_send_and_cleans_waiter(self):
        self.connection.channel = SimpleNamespace(send=self.blocked_send)
        with mock.patch("codex_lan.worker.REFRESH_TIMEOUT_SECONDS", 0.02):
            await asyncio.wait_for(self.connection._refresh_auth(701), 0.5)
        self.assertEqual(self.connection.refresh_waiters, {})
        self.assertTrue(self.waiter.cancelled())
        self.connection.codex.respond.assert_not_called()
        self.connection.codex.respond_error.assert_awaited_once()

    async def test_deadline_covers_blocked_auth_error_response(self):
        self.connection.channel = SimpleNamespace(send=self.blocked_send)

        async def blocked_error(*args):
            await asyncio.Event().wait()

        self.connection.codex.respond_error.side_effect = blocked_error
        with mock.patch("codex_lan.worker.REFRESH_TIMEOUT_SECONDS", 0.02):
            await asyncio.wait_for(self.connection._refresh_auth(702), 0.5)
        self.assertEqual(self.connection.refresh_waiters, {})
        self.assertTrue(self.waiter.cancelled())

    async def test_cancellation_clears_tokens_even_while_native_response_is_blocked(self):
        self.tokens = {"accessToken": "REPLACE_ME", "chatgptAccountId": "synthetic-account"}
        self.connection.channel = SimpleNamespace(send=self.deliver_tokens)
        began = asyncio.Event()

        async def blocked_response(*args):
            began.set()
            await asyncio.Event().wait()

        self.connection.codex.respond.side_effect = blocked_response
        task = asyncio.create_task(self.connection._refresh_auth(703))
        await asyncio.wait_for(began.wait(), 0.5)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.tokens, {})
        self.assertEqual(self.connection.refresh_waiters, {})
        self.connection.codex.respond_error.assert_not_called()


@unittest.skipUnless(shutil.which("codex") or (Path.home() / ".local/bin/codex").exists(),
                     "Codex CLI is not installed")
class ExternalAuthIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_app_server_keeps_external_tokens_out_of_state(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "isolated-codex-home"
            state.mkdir()

            async def event(_method, _params):
                return None

            async def request(_identifier, _method, _params):
                return None

            codex_path = Path(shutil.which("codex") or Path.home() / ".local/bin/codex")
            app = CodexAppServer(codex_path, Path(directory), state, event, request)
            token = fake_jwt("external-auth-must-stay-in-memory")
            try:
                await app.launch()
                await app.request(
                    "initialize",
                    {"clientInfo": {"name": "codex_lan_worker_test", "version": "0.1.0"},
                     "capabilities": {"experimentalApi": True}},
                    timeout=10,
                )
                await app.notify("initialized", {})
                await app.request(
                    "account/login/start",
                    {"type": "chatgptAuthTokens", "accessToken": token,
                     "chatgptAccountId": "worker-test-account", "chatgptPlanType": "plus"},
                    timeout=10,
                )
            finally:
                await app.close()
            self.assertFalse((state / "auth.json").exists())
            secret = token.encode()
            for path in state.rglob("*"):
                if path.is_file():
                    self.assertNotIn(secret, path.read_bytes(), str(path))


if __name__ == "__main__":
    unittest.main()
