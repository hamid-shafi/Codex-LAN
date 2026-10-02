"""CODEX: Real host model process and native client tools through our authenticated LAN transport, with local-only fake inference."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import struct
import tempfile
import unittest
import uuid
import zlib
from pathlib import Path
from dataclasses import replace
from unittest import mock

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from codex_lan.server import SERVICE, create_app
from codex_lan.worker import CodexAppServer, ExecutorConnection, WorkerConfig, build_endpoints
from test_lan_worker import TemporaryServer
from test_lan_server import FakeAuth


@unittest.skipUnless((Path.home() / ".local/bin/codex").is_file(), "Installed Codex CLI unavailable")
class NativeExecutorEndToEndTests(unittest.IsolatedAsyncioTestCase):
    async def test_host_session_executes_commands_and_patch_on_client_over_lan(self):
        with tempfile.TemporaryDirectory(prefix="codex-native-e2e-") as directory:
            root = Path(directory)
            client_cwd = root / "client"
            client_cwd.mkdir()
            native_binary = Path.home() / ".local/bin/codex"
            requests = []
            processes = []
            marker = "native-client-file-marker"
            client_task = None
            test_client = None

            def png_chunk(kind, content):
                return struct.pack("!I", len(content)) + kind + content + struct.pack("!I", zlib.crc32(kind + content))

            tiny_png = (b"\x89PNG\r\n\x1a\n" + png_chunk(b"IHDR", struct.pack("!2I5B", 1, 1, 8, 2, 0, 0, 0))
                        + png_chunk(b"IDAT", zlib.compress(b"\x00\x22\x66\xcc")) + png_chunk(b"IEND", b""))

            async def provider(request):
                value = await request.json()
                requests.append(value)
                index = len(requests)
                if index == 1:
                    item = {"type": "function_call", "id": "cmd-side", "call_id": "cmd-side", "name": "exec_command",
                            "arguments": json.dumps({"cmd": "/usr/bin/printenv LAN_EXECUTION_SIDE",
                                "workdir": str(client_cwd), "max_output_tokens": 100, "yield_time_ms": 1000})}
                elif index == 2:
                    item = {"type": "custom_tool_call", "id": "patch-file", "call_id": "patch-file", "name": "apply_patch",
                            "input": "*** Begin Patch\n*** Add File: " + str(client_cwd / "remote-marker.txt")
                                     + "\n+" + marker + "\n*** End Patch"}
                elif index in {3, 5}:
                    item = {"type": "function_call", "id": "read-file", "call_id": "read-file", "name": "exec_command",
                            "arguments": json.dumps({"cmd": "/usr/bin/printenv LAN_EXECUTION_SIDE\n/usr/bin/cat remote-marker.txt",
                                "workdir": str(client_cwd), "max_output_tokens": 100, "yield_time_ms": 1000})}
                else:
                    item = {"type": "message", "id": "final-message", "role": "assistant",
                            "content": [{"type": "output_text", "text": "Native network fixture complete."}]}
                stream = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
                await stream.prepare(request)
                response = {"id": "fixture-" + str(index), "status": "completed", "output": [item],
                            "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}}
                for event in (
                    {"type": "response.created", "response": {"id": response["id"], "status": "in_progress", "output": []}},
                    {"type": "response.output_item.done", "output_index": 0, "item": item},
                    {"type": "response.completed", "response": response},
                ):
                    await stream.write(("data: " + json.dumps(event) + "\n\n").encode())
                await stream.write_eof()
                return stream

            native_spawn = asyncio.create_subprocess_exec

            async def tagged_spawn(*args, **kwargs):
                environment = dict(kwargs["env"])
                environment["LAN_EXECUTION_SIDE"] = "client" if "exec-server" in args else "host"
                kwargs["env"] = environment
                process = await native_spawn(*args, **kwargs)
                processes.append((args, process))
                return process

            async def fixture_initialize(app_server, _tokens):
                # CODEX: Local fake provider requires no account login and can never consume paid inference.
                await app_server.request("initialize", {"clientInfo": {"name": "lan-native-e2e", "version": "0.0.0"},
                                                       "capabilities": {"experimentalApi": True}}, timeout=5)
                await app_server.notify("initialized", {})
                return [{"id": "gpt-5.6-terra", "model": "gpt-5.6-terra", "isDefault": True,
                         "defaultReasoningEffort": "low", "supportedReasoningEfforts": [{"reasoningEffort": "low"}]}], "fixture"

            try:
                async with TemporaryServer([web.route("*", "/{tail:.*}", provider)]) as local_provider:
                    auth = FakeAuth()
                    auth.codex = native_binary
                    application = create_app(root / "host-data", auth=auth)
                    service = application[SERVICE]
                    admin = service.store.bootstrap_admin("Host fixture")
                    device = service.store.enroll("Native client fixture")
                    device_id = device["device"]["id"]
                    service.store.set_device(admin["device"]["id"], device_id, status="approved")
                    host_state = service.data_dir / "remote-workers" / device_id
                    host_state.mkdir(parents=True)
                    (host_state / "config.toml").write_text(
                        'model_provider = "local_fixture"\n'
                        '[model_providers.local_fixture]\nname = "Local fixture only"\n'
                        f'base_url = "{local_provider.base}/v1"\n'
                        'wire_api = "responses"\nrequires_openai_auth = false\n'
                    )
                    test_client = TestClient(TestServer(application))
                    await test_client.start_server()
                    base = str(test_client.make_url("/")).rstrip("/")
                    config = WorkerConfig(build_endpoints(base), root / "token", client_cwd,
                                          native_binary, root / "client-state", "Native client fixture")
                    async with aiohttp.ClientSession(trust_env=False) as session:
                        connector = ExecutorConnection(config, session, device["token"])
                        with mock.patch("codex_lan.worker.asyncio.create_subprocess_exec", tagged_spawn), mock.patch.object(
                            CodexAppServer, "initialize", fixture_initialize
                        ):
                            client_task = asyncio.create_task(connector.run())
                            async def ready():
                                while not service.runtime.connected(device_id):
                                    if client_task.done():
                                        await client_task
                                    await asyncio.sleep(0.01)
                            await asyncio.wait_for(ready(), 12)
                            headers = {"Authorization": "Bearer " + device["token"]}
                            response = await test_client.post("/api/rooms", headers=headers,
                                json={"title": "Native E2E", "cwd": str(client_cwd), "model": "gpt-5.6-terra", "effort": "low"})
                            self.assertEqual(response.status, 201, await response.text())
                            room = await response.json()
                            upload = aiohttp.FormData()
                            upload.add_field("file", tiny_png, filename="tiny-fixture.png", content_type="image/png")
                            response = await test_client.post("/api/attachments", headers=headers, data=upload)
                            self.assertEqual(response.status, 201, await response.text())
                            attachment = await response.json()
                            response = await test_client.post(f"/api/rooms/{room['id']}/runs", headers=headers,
                                json={"text": "Run the isolated native marker fixture.", "request_id": str(uuid.uuid4()),
                                      "attachment_ids": [attachment["id"]]})
                            self.assertEqual(response.status, 202, await response.text())
                            receipt = await response.json()
                            run_id = receipt["run"]["id"]
                            async def completed():
                                while True:
                                    row = service.store.db.execute("SELECT status,error FROM runs WHERE id=?", (run_id,)).fetchone()
                                    if row["status"] not in {"queued", "running"}:
                                        return dict(row)
                                    await asyncio.sleep(0.01)
                            final = await asyncio.wait_for(completed(), 30)
                            self.assertEqual(final["status"], "completed", final)
                            self.assertEqual(len(requests), 4)
                            images = [part for item in requests[0].get("input", []) for part in item.get("content", [])
                                      if isinstance(part, dict) and part.get("type") == "input_image"]
                            self.assertTrue(images, "Uploaded image pixels never reached the local fake provider: " +
                                repr([item for item in requests[0].get("input", []) if item.get("role") == "user"])[-3000:])
                            self.assertTrue(all(str(image.get("image_url", "")).startswith("data:image/") for image in images))
                            outputs = [entry.get("output") for request in requests for entry in request.get("input", [])
                                       if entry.get("type") in {"function_call_output", "custom_tool_call_output"}]
                            output_text = json.dumps(outputs)
                            self.assertTrue(any("\nclient\n" in str(output) or str(output).endswith("\nclient")
                                                for output in outputs), "Native command did not report the client process marker")
                            self.assertFalse(any("\nhost\n" in str(output) or str(output).endswith("\nhost")
                                                 for output in outputs), "Native command ran in the host model process")
                            self.assertIn(marker, output_text)
                            self.assertEqual((client_cwd / "remote-marker.txt").read_text().strip(), marker)
                            self.assertFalse((host_state / "host-workspace" / "remote-marker.txt").exists())
                            self.assertEqual(service.runtime.peers[device_id].execution_mode, "server")
                            self.assertEqual(sum("exec-server" in args for args, _process in processes), 1)
                            self.assertEqual(sum("app-server" in args for args, _process in processes), 1)
                            self.assertFalse((root / "client-state" / "executor" / "auth.json").exists())
                            client_task.cancel()
                            with contextlib.suppress(asyncio.CancelledError):
                                await client_task
                            client_task = None
                            async def cleaned():
                                while service.remote_sessions:
                                    await asyncio.sleep(0.01)
                            await asyncio.wait_for(cleaned(), 8)
                            self.assertTrue(all(process.returncode is not None for _args, process in processes))
                            # CODEX: Resume the same host-native history after the entire client/model connection is rebuilt.
                            original_thread = service.store.db.execute(
                                "SELECT thread_id FROM rooms WHERE id=?", (room["id"],)
                            ).fetchone()[0]
                            self.assertTrue(original_thread)
                            await test_client.close()
                            application = create_app(root / "host-data", auth=auth)
                            service = application[SERVICE]
                            test_client = TestClient(TestServer(application))
                            await test_client.start_server()
                            config = replace(config, endpoints=build_endpoints(str(test_client.make_url("/")).rstrip("/")))
                            connector = ExecutorConnection(config, session, device["token"])
                            client_task = asyncio.create_task(connector.run())
                            await asyncio.wait_for(ready(), 12)
                            response = await test_client.get("/api/rooms", headers=headers)
                            self.assertEqual(response.status, 200, await response.text())
                            response = await test_client.post(f"/api/rooms/{room['id']}/runs", headers=headers,
                                json={"text": "Continue the same fixture after reconnecting.", "request_id": str(uuid.uuid4())})
                            self.assertEqual(response.status, 202, await response.text())
                            receipt = await response.json()
                            run_id = receipt["run"]["id"]
                            final = await asyncio.wait_for(completed(), 30)
                            self.assertEqual(final["status"], "completed", final)
                            self.assertEqual(len(requests), 6)
                            self.assertIn("Run the isolated native marker fixture.", json.dumps(requests[4]))
                            self.assertIn(marker, json.dumps(requests[5]))
                            self.assertEqual(service.store.db.execute(
                                "SELECT thread_id FROM rooms WHERE id=?", (room["id"],)
                            ).fetchone()[0], original_thread)
                            self.assertEqual(sum("exec-server" in args for args, _process in processes), 2)
                            self.assertEqual(sum("app-server" in args for args, _process in processes), 2)
                            client_task.cancel()
                            with contextlib.suppress(asyncio.CancelledError):
                                await client_task
                            client_task = None
                            await asyncio.wait_for(cleaned(), 8)
                            self.assertTrue(all(process.returncode is not None for _args, process in processes))
            finally:
                if client_task is not None:
                    client_task.cancel()
                    await asyncio.gather(client_task, return_exceptions=True)
                if test_client is not None:
                    await test_client.close()


if __name__ == "__main__":
    unittest.main()
