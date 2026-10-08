"""CODEX: Approved native connectors get host sessions, never provider credentials."""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from codex_lan.server import COOKIE, SERVICE, create_app
from codex_lan.runtime import Peer
from test_lan_server import FakeAuth


class FakeHostSession:
    def __init__(self, service, device, ws, hello):
        self.service, self.device, self.ws, self.hello = service, device, ws, hello
        self.closed = False

    async def run(self):
        await self.ws.send_json({"type": "ready", "execution_mode": "server"})
        async for message in self.ws:
            if message.type == aiohttp.WSMsgType.BINARY:
                await self.ws.send_bytes(message.data)

    async def close(self):
        self.closed = True
        await self.ws.close()


class ExecutorEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = create_app(Path(self.temp.name), auth=FakeAuth())
        self.service = self.app[SERVICE]
        self.admin = self.service.store.bootstrap_admin("Host")
        self.device = self.service.store.enroll("Client")
        self.pending = self.service.store.enroll("Pending")
        self.service.store.set_device(self.admin["device"]["id"], self.device["device"]["id"], status="approved")
        self.client = TestClient(TestServer(self.app))
        await self.client.start_server()
        self.factory = patch("codex_lan.server.HostWorkerSession", FakeHostSession)
        self.factory.start()
        self.sockets = []

    async def asyncTearDown(self):
        for ws in self.sockets:
            await ws.close()
        await self.client.close()
        self.factory.stop()
        self.temp.cleanup()

    async def connect(self, hello=None):
        ws = await self.client.ws_connect("/api/executor", headers={"Authorization": "Bearer " + self.device["token"]})
        self.sockets.append(ws)
        await ws.send_json(hello or {"type": "executor_hello", "protocol": 1, "version": "0.3.0", "cwd": "/home/client"})
        return ws

    async def test_approved_channel_creates_host_session_without_provider_token(self):
        ws = await self.connect()
        value = await ws.receive_json(timeout=1)
        self.assertEqual(value, {"type": "ready", "execution_mode": "server"})
        self.assertNotIn("FAKE-NOT-A-CREDENTIAL", json.dumps(value))
        session = self.service.remote_sessions[self.device["device"]["id"]]
        self.assertEqual(session.hello["cwd"], "/home/client")
        await ws.send_bytes(b'{"id":1,"method":"initialize"}\n')
        self.assertEqual((await ws.receive(timeout=1)).data, b'{"id":1,"method":"initialize"}\n')
        await ws.close()
        for _ in range(30):
            if not self.service.remote_sessions:
                break
            await asyncio.sleep(0.01)
        self.assertFalse(self.service.remote_sessions)
        self.assertFalse(self.service.connecting)

    async def test_confirmed_version_survives_disconnect_and_live_state_is_explicit(self):
        ws = await self.connect({"type": "executor_hello", "protocol": 1,
                                 "version": "0.3.7", "cwd": "/home/client"})
        await ws.receive_json(timeout=1)
        await ws.close()
        for _ in range(30):
            if not self.service.remote_sessions:
                break
            await asyncio.sleep(0.01)
        headers = {"Authorization": "Bearer " + self.device["token"]}
        persisted = await (await self.client.get("/api/me", headers=headers)).json()
        self.assertEqual(persisted["connector_version"], "0.3.7")
        self.assertEqual(persisted["client_version"], "0.3.7")
        self.assertIsInstance(persisted["connector_confirmed_at"], float)
        self.assertTrue(persisted["connector_linked"])
        self.assertFalse(persisted["connector_online"])
        self.assertFalse(persisted["client_version_live"])
        admin_headers = {"Authorization": "Bearer " + self.admin["token"]}
        admin_devices = await (await self.client.get("/api/admin/devices", headers=admin_headers)).json()
        admin_device = next(item for item in admin_devices["devices"]
                            if item["id"] == self.device["device"]["id"])
        self.assertEqual(admin_device["client_version"], "0.3.7")
        self.assertTrue(admin_device["connector_linked"])
        self.assertFalse(admin_device["connector_online"])
        self.assertFalse(admin_device["client_version_live"])

        socket = SimpleNamespace(closed=False)
        peer = Peer(self.device["device"]["id"], socket, ready=True,
                    client_version="0.3.8", execution_mode="server")
        self.service.runtime.peers[self.device["device"]["id"]] = peer
        live = await (await self.client.get("/api/me", headers=headers)).json()
        self.assertTrue(live["connector_online"])
        self.assertTrue(live["client_version_live"])
        self.assertEqual(live["client_version"], "0.3.8")
        self.assertEqual(live["connector_version"], "0.3.7")
        self.service.runtime.peers.pop(self.device["device"]["id"])

    async def test_cookie_pending_and_cross_origin_cannot_open_executor(self):
        for headers in ({"Cookie": COOKIE + "=" + self.device["token"]},
                        {"Authorization": "Bearer " + self.pending["token"]},
                        {"Authorization": "Bearer " + self.device["token"], "Origin": "https://other.invalid"}):
            with self.subTest(headers=list(headers)), self.assertRaises(aiohttp.WSServerHandshakeError) as failure:
                await self.client.ws_connect("/api/executor", headers=headers)
            self.assertEqual(failure.exception.status, 403)

    async def test_duplicate_and_invalid_handshake_preserve_existing_session(self):
        ws = await self.connect()
        await ws.receive_json(timeout=1)
        with self.assertRaises(aiohttp.WSServerHandshakeError) as failure:
            await self.client.ws_connect("/api/executor", headers={"Authorization": "Bearer " + self.device["token"]})
        self.assertEqual(failure.exception.status, 409)
        self.assertFalse(ws.closed)

    async def test_invalid_cwd_never_creates_host_process(self):
        ws = await self.connect({"type": "executor_hello", "protocol": 1, "version": "0.3.0", "cwd": "relative"})
        await ws.receive(timeout=1)
        await ws.close()
        self.assertFalse(self.service.remote_sessions)

    async def test_legacy_https_model_workers_must_upgrade(self):
        with self.assertRaises(web.HTTPUpgradeRequired):
            await self.service.worker(SimpleNamespace(secure=True))

    async def test_revocation_closes_an_idle_connector_without_waiting_for_client_messages(self):
        with patch("codex_lan.server.EXECUTOR_AUTH_INTERVAL", 0.01):
            ws = await self.connect()
            await ws.receive_json(timeout=1)
            self.service.store.set_device(self.admin["device"]["id"], self.device["device"]["id"], status="revoked")
            value = await ws.receive(timeout=1)
            self.assertIn(value.type, {aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED})


if __name__ == "__main__":
    unittest.main()
