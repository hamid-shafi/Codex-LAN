"""CODEX: Preserve legacy client rooms read-only while new host-managed rooms remain durable."""

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from aiohttp import WSServerHandshakeError
from aiohttp.test_utils import TestClient, TestServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from codex_lan.server import SERVICE, create_app
from codex_lan.store import RoomReadOnlyError, Store


class Auth:
    async def tokens(self, force=False):
        return {"accessToken": "test-only", "chatgptAccountId": "test-only"}


class ExecutorFixture:
    def __init__(self, _service, _device, websocket, _hello):
        self.ws = websocket

    async def run(self):
        await self.ws.send_json({"type": "executor_ready"})
        async for _frame in self.ws:
            pass

    async def close(self):
        await self.ws.close()


class RoomExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name)
        self.app = create_app(self.path, auth=Auth())
        self.service = self.app[SERVICE]
        self.store = self.service.store
        self.admin = self.store.bootstrap_admin("Host")
        self.client_device = self.store.enroll("Remote client")
        self.device = self.client_device["device"]["id"]
        self.admin_id = self.admin["device"]["id"]
        self.store.set_device(self.admin_id, self.device, status="approved")
        self.old = self.store.create_room(self.device, "Earlier room", "/home/client")
        self.old_run = self.store.create_run(self.device, self.old["id"], "Earlier question", [], "earlier-run")
        self.store.finish_run(self.old_run["run"]["id"], "completed", "Earlier answer")
        self.http = TestClient(TestServer(self.app))
        await self.http.start_server()

    async def asyncTearDown(self):
        await self.http.close()
        self.temporary.cleanup()

    def counts(self):
        return {name: self.store.db.execute("SELECT COUNT(*) FROM " + name).fetchone()[0]
                for name in ("rooms", "runs", "messages", "usage_ledger", "steering_receipts")}

    async def request(self, method, path, **kwargs):
        return await self.http.request(method, path, headers={"Authorization": "Bearer " + self.client_device["token"]}, **kwargs)

    async def test_only_transitioned_remote_history_is_preserved_read_only(self):
        host = self.store.create_room(self.admin_id, "Host debug", "/home/host")
        host_run = self.store.create_run(self.admin_id, host["id"], "Still working", [], "host-run")
        empty = self.store.create_room(self.device, "Unused room", "/home/client")
        self.store.enable_server_execution(self.device)
        self.store.enable_server_execution(self.admin_id)
        old = self.store.get_room(self.device, self.old["id"])
        self.assertTrue(old["read_only"])
        self.assertEqual(old["execution_origin"], "local")
        self.assertIn("preserved", old["read_only_reason"])
        self.assertFalse(self.store.get_room(self.admin_id, host["id"])["read_only"])
        self.assertEqual(self.store.get_device(self.admin_id)["execution_mode"], "local")
        self.assertEqual(self.store.get_run(self.admin_id, host_run["run"]["id"])["status"], "queued")
        self.assertEqual(self.store.get_room(self.device, empty["id"])["execution_origin"], "server")
        self.assertFalse(self.store.get_room(self.device, empty["id"])["read_only"])
        self.assertEqual([row["content"] for row in self.store.list_messages(self.device, self.old["id"])],
                         ["Earlier question", "Earlier answer"])

    async def test_store_blocks_old_send_steer_branch_and_updates_before_mutation(self):
        self.store.enable_server_execution(self.device)
        before = self.counts()
        operations = (
            lambda: self.store.create_run(self.device, self.old["id"], "New question", [], "new-run"),
            lambda: self.store.prepare_steering(
                self.device, self.old["id"], self.old_run["run"]["id"], "More", [], "steer",
                tool_read_only=False,
            ),
            lambda: self.store.create_room(self.device, "Branch", "/home/client", parent_room_id=self.old["id"], reply_quote="Earlier answer"),
            lambda: self.store.update_room(self.device, self.old["id"], thread_id="other-thread"),
        )
        for operation in operations:
            with self.assertRaises(RoomReadOnlyError):
                operation()
        self.assertEqual(self.counts(), before)

    async def test_http_refuses_old_execution_with_409_but_keeps_history_and_images_readable(self):
        image = self.service.upload_dir / "preserved.png"
        image.write_bytes(b"\x89PNG\r\n\x1a\npreserved")
        attachment = self.store.add_attachment(self.device, "preserved.png", "image/png", image.stat().st_size, str(image))
        self.store.enable_server_execution(self.device)
        before = self.counts()
        for method, path, payload in (
            ("POST", f"/api/rooms/{self.old['id']}/runs", {"text": "New", "request_id": "new"}),
            ("POST", f"/api/rooms/{self.old['id']}/steer", {"text": "More", "run_id": self.old_run["run"]["id"],
                                                                "request_id": "more", "tool_read_only": False}),
            ("POST", "/api/rooms", {"title": "Branch", "cwd": "/home/client", "parent_room_id": self.old["id"], "reply_quote": "Earlier"}),
            ("PATCH", f"/api/rooms/{self.old['id']}", {"title": "Changed"}),
        ):
            response = await self.request(method, path, json=payload)
            self.assertEqual(response.status, 409, await response.text())
            self.assertTrue((await response.json())["read_only"])
        self.assertEqual(self.counts(), before)
        response = await self.request("GET", "/api/rooms")
        self.assertTrue((await response.json())["rooms"][0]["read_only"])
        response = await self.request("GET", f"/api/rooms/{self.old['id']}/messages")
        result = await response.json()
        self.assertTrue(result["room"]["read_only"])
        self.assertEqual(len(result["messages"]), 2)
        response = await self.request("GET", f"/api/attachments/{attachment['id']}")
        self.assertEqual(response.status, 200)
        self.assertEqual(await response.read(), image.read_bytes())

    async def test_new_room_origin_survives_reconnect_and_database_reopen(self):
        self.store.enable_server_execution(self.device)
        created = self.store.create_room(self.device, "New server room", "/home/client")
        run = self.store.create_run(self.device, created["id"], "New native work", [], "server-run")
        self.store.finish_run(run["run"]["id"], "completed", "Done")
        self.store.enable_server_execution(self.device)
        reopened = Store(self.path)
        try:
            reopened.enable_server_execution(self.device)
            self.assertEqual(reopened.get_room(self.device, created["id"])["execution_origin"], "server")
            self.assertFalse(reopened.get_room(self.device, created["id"])["read_only"])
            continued = reopened.create_run(self.device, created["id"], "Continue", [], "server-next")
            self.assertFalse(continued["deduplicated"])
            self.assertTrue(reopened.get_room(self.device, self.old["id"])["read_only"])
        finally:
            reopened.close()

    async def test_runtime_does_not_resume_or_meter_an_archived_room(self):
        self.store.enable_server_execution(self.device)
        before = self.counts()
        with patch.object(self.store, "configure_run_usage") as meter:
            with self.assertRaises(RoomReadOnlyError):
                self.service.runtime.start(self.old_run["run"], self.old, "No replay", [])
            with self.assertRaises(RoomReadOnlyError):
                await self.service.runtime.steer(
                    self.device, self.old["id"], self.old_run["run"]["id"], "No follow-up", [], "none",
                    tool_read_only=False,
                )
            meter.assert_not_called()
        self.assertEqual(self.counts(), before)
        self.assertFalse(self.service.runtime.runs)

    async def test_approved_remote_device_cannot_receive_legacy_worker_credentials(self):
        self.assertEqual(self.store.get_device(self.device)["execution_mode"], "local")
        with patch.object(self.service.runtime.auth, "tokens", new=AsyncMock()) as credentials:
            with self.assertRaises(WSServerHandshakeError) as failure:
                await self.http.ws_connect("/api/worker", headers={"Authorization": "Bearer " + self.client_device["token"]})
            self.assertEqual(failure.exception.status, 426)
            credentials.assert_not_awaited()

    async def test_host_admin_can_still_use_legacy_loopback_worker(self):
        credentials = {"accessToken": "admin-test-only", "chatgptAccountId": "admin-test-only"}
        with patch.object(self.service.runtime.auth, "tokens", new=AsyncMock(return_value=credentials)) as provider:
            socket = await self.http.ws_connect(
                "/api/worker", headers={"Authorization": "Bearer " + self.admin["token"]}
            )
            self.assertEqual(await socket.receive_json(timeout=1), {"type": "auth", "tokens": credentials})
            provider.assert_awaited_once_with()
            await socket.close()

    async def test_only_valid_authenticated_executor_hello_classifies_old_rooms(self):
        headers = {"Authorization": "Bearer " + self.client_device["token"]}
        with patch("codex_lan.server.HostWorkerSession", ExecutorFixture):
            invalid = await self.http.ws_connect("/api/executor", headers=headers)
            await invalid.send_json({"type": "executor_hello", "protocol": 0, "version": "test", "cwd": "/home/client"})
            await invalid.receive(timeout=1)
            await invalid.close()
            for _ in range(50):
                if not self.service.connecting:
                    break
                await asyncio.sleep(0.01)
            self.assertEqual(self.store.get_device(self.device)["execution_mode"], "local")
            self.assertFalse(self.store.get_room(self.device, self.old["id"])["read_only"])
            valid = await self.http.ws_connect("/api/executor", headers=headers)
            await valid.send_json({"type": "executor_hello", "protocol": 1, "version": "test", "cwd": "/home/client"})
            self.assertEqual((await valid.receive_json(timeout=1))["type"], "executor_ready")
            self.assertEqual(self.store.get_device(self.device)["execution_mode"], "server")
            self.assertTrue(self.store.get_room(self.device, self.old["id"])["read_only"])
            response = await self.request("POST", "/api/rooms", json={"title": "Fresh", "cwd": "/home/client"})
            room = await response.json()
            self.assertEqual(response.status, 201)
            self.assertEqual(room["execution_origin"], "server")
            self.assertFalse(room["read_only"])
            await valid.close()


if __name__ == "__main__":
    unittest.main()
