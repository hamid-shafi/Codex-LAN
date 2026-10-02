"""CODEX: HTTP and websocket integration checks using entirely fake Codex workers."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import os
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

from aiohttp import ClientSession, CookieJar, FormData, WSMsgType, WSServerHandshakeError
from aiohttp.test_utils import TestClient, TestServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from codex_lan.server import ATTACHMENT_ORPHAN_GRACE_SECONDS, COOKIE, JSON_BODY_MAX, SERVICE, create_app
from codex_lan.runtime import (
    MAX_RUN_CREDITS_MICROS,
    MAX_RUN_SECONDS,
    MAX_RUN_TOTAL_TOKENS,
    RUN_TIME_LIMIT_MESSAGE,
    RUN_USAGE_LIMIT_MESSAGE,
)


PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9ZlS0AAAAASUVORK5CYII=")
MODELS = [{"id": "astra", "model": "astra", "isDefault": True,
           "defaultReasoningEffort": "medium",
           "supportedReasoningEfforts": [{"reasoningEffort": value} for value in ("low", "medium", "ultra")]}]


class FakeAuth:
    async def tokens(self, force=False):
        return {"accessToken": "FAKE-NOT-A-CREDENTIAL", "chatgptAccountId": "fake-account"}


class FakeWorker:
    def __init__(self, ws, name):
        self.ws, self.name = ws, name
        self.calls = []
        self.thread = "thread-" + name
        self.turn = None
        self.next_turn = 0
        self.task = asyncio.create_task(self.serve())

    async def serve(self):
        async for message in self.ws:
            if message.type != WSMsgType.TEXT:
                continue
            packet = json.loads(message.data)
            if packet.get("type") != "rpc":
                continue
            self.calls.append(packet)
            method = packet["method"]
            if method in {"thread/start", "thread/resume"}:
                result = {"thread": {"id": self.thread}}
            elif method == "turn/start":
                self.next_turn += 1
                self.turn = f"turn-{self.name}-{self.next_turn}"
                result = {"turn": {"id": self.turn}}
            elif method == "lan/upload":
                result = {"path": "/fake/client/upload.png"}
            else:
                result = {}
            await self.ws.send_json({"type": "result", "id": packet["id"], "result": result})

    async def event(self, method, **params):
        value = {"threadId": self.thread, "turnId": self.turn, **params}
        await self.ws.send_json({"type": "event", "method": method, "params": value})

    async def delta(self, text):
        await self.event("item/agentMessage/delta", itemId="answer", delta=text)

    async def complete(self, status="completed"):
        await self.event("turn/completed", turn={"id": self.turn, "status": status})

    async def usage(self, input_count, output_count, cached=0):
        counts = {"inputTokens": input_count, "outputTokens": output_count,
                  "cachedInputTokens": cached, "totalTokens": input_count + output_count}
        await self.event("thread/tokenUsage/updated", tokenUsage={"total": counts, "last": counts})

    async def close(self):
        await self.ws.close()
        await self.task


async def host_worker_socket(client, service, admin, device):
    ws = await client.ws_connect("/api/worker", headers={"Authorization": "Bearer " + admin["token"]})
    auth = await ws.receive_json(timeout=2)
    admin_id = admin["device"]["id"]
    device_id = device["device"]["id"]
    peer = service.runtime.peers[admin_id]
    if device_id != admin_id:
        if device_id in service.runtime.peers:
            raise AssertionError("The target fixture already has a worker")
        service.runtime.peers.pop(admin_id)
        service.connecting.discard(admin_id)
        peer.device_id = device_id
        peer.draining = device_id in service.runtime.draining_devices
        service.runtime.peers[device_id] = peer
    return ws, auth


class LanServerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = create_app(Path(self.temp.name), auth=FakeAuth())
        self.service = self.app[SERVICE]
        self.client = TestClient(TestServer(self.app))
        await self.client.start_server()
        self.admin = self.service.store.bootstrap_admin("Server")
        self.a = self.service.store.enroll("Ubuntu A")
        self.b = self.service.store.enroll("Ubuntu B")
        self.pending = self.service.store.enroll("Pending")
        self.admin_id = self.admin["device"]["id"]
        for device in (self.a, self.b):
            self.service.store.set_device(self.admin_id, device["device"]["id"], status="approved")
        self.workers = []
        self.sessions = []

    async def asyncTearDown(self):
        for worker in self.workers:
            await worker.close()
        for session in self.sessions:
            await session.close()
        await self.client.close()
        self.temp.cleanup()

    async def request(self, method, path, device=None, expected=200, **kwargs):
        headers = dict(kwargs.pop("headers", {}))
        if device:
            headers["Authorization"] = "Bearer " + device["token"]
        response = await self.client.request(method, path, headers=headers, **kwargs)
        body = await response.json()
        self.assertEqual(response.status, expected, body)
        return body

    async def until(self, condition):
        async with asyncio.timeout(3) if hasattr(asyncio, "timeout") else _Timeout(3):
            while not condition():
                await asyncio.sleep(0.005)

    async def worker(self, device, name):
        ws, auth = await host_worker_socket(self.client, self.service, self.admin, device)
        self.assertEqual(auth["type"], "auth")
        self.assertEqual(auth["tokens"]["accessToken"], "FAKE-NOT-A-CREDENTIAL")
        await ws.send_json({"type": "ready", "models": MODELS, "cwd": "/home/" + name})
        worker = FakeWorker(ws, name)
        self.workers.append(worker)
        await self.until(lambda: self.service.runtime.connected(device["device"]["id"]))
        return worker

    async def room(self, device, **fields):
        return await self.request("POST", "/api/rooms", device, expected=201,
                                  json={"title": "Room", "cwd": "/home/project", "model": "astra", "effort": "ultra", **fields})

    async def submit_run(self, device, room, request_id="one", **fields):
        return await self.request("POST", f"/api/rooms/{room['id']}/runs", device, expected=202,
                                  json={"text": "hello", "request_id": request_id, **fields})

    async def ready_run(self, device, worker, room, request_id="one", **fields):
        created = await self.submit_run(device, room, request_id, **fields)
        await self.until(lambda: self.service.store.get_run_internal(created["run"]["id"])["turn_id"] is not None)
        return created

    async def upload(self, device):
        form = FormData()
        form.add_field("file", PNG, filename="pixel.png", content_type="image/png")
        return await self.request("POST", "/api/attachments", device, expected=201, data=form)

    async def test_health_identifies_startup_source_not_later_disk_changes(self):
        build = self.service.backend_build
        self.assertEqual(len(build), 64)
        self.assertEqual(int(build, 16).bit_length() <= 256, True)
        with mock.patch("codex_lan.server.backend_source_fingerprint", return_value="later-build") as fingerprint:
            health = await self.request("GET", "/api/health")
        self.assertEqual(health["backend_build"], build)
        fingerprint.assert_not_called()

    async def test_pending_and_unknown_credentials_fail_closed(self):
        for path in ("/api/models", "/api/rooms", "/api/admin/devices", "/api/admin/usage"):
            await self.request("GET", path, self.pending, expected=403)
            await self.request("GET", path, expected=403)
        me = await self.request("GET", "/api/me", self.pending)
        self.assertEqual(me["device"]["status"], "pending")
        with self.assertRaises(WSServerHandshakeError) as failure:
            await self.client.ws_connect("/api/worker", headers={"Authorization": "Bearer " + self.pending["token"]})
        self.assertEqual(failure.exception.status, 403)
        await self.request("PATCH", f"/api/admin/devices/{self.pending['device']['id']}", self.a,
                           expected=403, json={"status": "approved"})

    async def test_host_origin_and_browser_cookie_worker_guard(self):
        for headers in ({"Host": "attacker.invalid"}, {"Origin": "https://attacker.invalid"}, {"Sec-Fetch-Site": "cross-site"}):
            await self.request("GET", "/api/health", expected=403, headers=headers)
        await self.request("GET", "/api/health")
        with self.assertRaises(WSServerHandshakeError) as failure:
            await self.client.ws_connect("/api/worker", headers={"Cookie": COOKIE + "=" + self.a["token"]})
        self.assertEqual(failure.exception.status, 403)
        response = await self.client.post("/api/session", json={"token": self.a["token"]})
        self.assertEqual(response.status, 200)
        cookie = response.cookies[COOKIE]
        self.assertTrue(cookie["httponly"])
        self.assertEqual(cookie["samesite"], "Strict")
        self.assertNotIn("FAKE-NOT-A-CREDENTIAL", await response.text())

    async def test_remote_worker_rejected_before_auth_refresh(self):
        called = False

        class DelayedAuth:
            async def tokens(inner_self, force=False):
                nonlocal called
                called = True
                return await FakeAuth().tokens()

        self.service.runtime.auth = DelayedAuth()
        with self.assertRaises(WSServerHandshakeError) as failure:
            await self.client.ws_connect(
                "/api/worker", headers={"Authorization": "Bearer " + self.a["token"]}
            )
        self.assertEqual(failure.exception.status, 426)
        self.assertFalse(called)

    async def test_json_shapes_and_duplicate_worker_are_rejected(self):
        for value in ([], None, "text"):
            await self.request("POST", "/api/enroll", expected=400, json=value)
            await self.request("POST", "/api/rooms", self.a, expected=400, json=value)
        oversized = b'{"name":"' + b"x" * JSON_BODY_MAX + b'"}'
        response = await self.client.post(
            "/api/enroll", data=io.BytesIO(oversized), headers={"Content-Type": "application/json"}
        )
        self.assertEqual(response.status, 413)
        self.assertIn("Maximum request body size", (await response.json())["error"])
        await self.request("GET", "/api/health")
        await self.worker(self.admin, "admin")
        with self.assertRaises(WSServerHandshakeError) as failure:
            await self.client.ws_connect("/api/worker", headers={"Authorization": "Bearer " + self.admin["token"]})
        self.assertEqual(failure.exception.status, 409)

    async def test_private_routes_attachments_and_parent_quotes(self):
        worker_a = await self.worker(self.a, "a")
        await self.worker(self.b, "b")
        room_a, room_b = await self.room(self.a), await self.room(self.b)
        attachment = await self.upload(self.a)
        self.assertNotIn("path", attachment)
        first = await self.ready_run(self.a, worker_a, room_a, attachment_ids=[attachment["id"]])
        await worker_a.delta("private response")
        await worker_a.complete()
        await self.until(lambda: first["run"]["id"] not in self.service.runtime.runs)
        for suffix in ("messages", "events"):
            await self.request("GET", f"/api/rooms/{room_a['id']}/{suffix}", self.b, expected=403)
        await self.request("PATCH", f"/api/rooms/{room_a['id']}", self.b, expected=403, json={"title": "stolen"})
        await self.request("DELETE", f"/api/rooms/{room_a['id']}", self.b, expected=403)
        await self.request("POST", f"/api/rooms/{room_a['id']}/runs", self.b, expected=403,
                           json={"text": "inject", "request_id": "inject"})
        await self.request("POST", f"/api/runs/{first['run']['id']}/stop", self.b, expected=403)
        await self.request("GET", "/api/attachments/" + attachment["id"], self.b, expected=403)
        for device in (self.a, self.admin):
            response = await self.client.get("/api/attachments/" + attachment["id"], headers={"Authorization": "Bearer " + device["token"]})
            self.assertEqual(response.status, 200)
            self.assertEqual(await response.read(), PNG)
        await self.request("POST", f"/api/rooms/{room_b['id']}/runs", self.b, expected=403,
                           json={"text": "use attachment", "attachment_ids": [attachment["id"]], "request_id": "cross-image"})
        await self.request("POST", "/api/rooms", self.b, expected=403,
                           json={"title": "cross-parent", "cwd": "/tmp", "parent_room_id": room_a["id"], "reply_quote": "private"})
        await self.request("POST", f"/api/rooms/{room_b['id']}/runs", self.b, expected=400,
                           json={"text": "cross-reply", "reply_to_id": first["user_message"]["id"], "reply_quote": "hello", "request_id": "cross-reply"})
        listing = await self.request("GET", "/api/rooms", self.b)
        self.assertEqual([row["id"] for row in listing["rooms"]], [room_b["id"]])
        listing = await self.request("GET", "/api/rooms", self.admin)
        self.assertEqual(len(listing["rooms"]), 2)
        await self.request("POST", f"/api/rooms/{room_a['id']}/runs", self.admin, expected=403,
                           json={"text": "admin executes", "request_id": "admin"})

    async def test_room_delete_unlinks_only_exclusive_attachment_files(self):
        first_room, second_room = await self.room(self.a), await self.room(self.a)
        shared, exclusive = await self.upload(self.a), await self.upload(self.a)
        shared_record = self.service.store.get_attachment(self.a["device"]["id"], shared["id"])
        exclusive_record = self.service.store.get_attachment(self.a["device"]["id"], exclusive["id"])
        first = self.service.store.create_run(
            self.a["device"]["id"], first_room["id"], "first",
            [shared["id"], exclusive["id"]], "first-room",
        )
        self.service.store.finish_run(first["run"]["id"], "completed", "done")
        second = self.service.store.create_run(
            self.a["device"]["id"], second_room["id"], "second", [shared["id"]], "second-room",
        )
        self.service.store.finish_run(second["run"]["id"], "completed", "done")

        await self.request("DELETE", f"/api/rooms/{first_room['id']}", self.a)

        self.assertFalse(Path(exclusive_record["path"]).exists())
        self.assertTrue(Path(shared_record["path"]).is_file())
        with self.assertRaises(PermissionError):
            self.service.store.get_attachment(self.a["device"]["id"], exclusive["id"])
        response = await self.client.get(
            "/api/attachments/" + shared["id"],
            headers={"Authorization": "Bearer " + self.a["token"]},
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(await response.read(), PNG)

    async def test_upload_quota_rejects_incoming_overflow_and_removes_staged_file(self):
        with mock.patch("codex_lan.store.MAX_ATTACHMENT_STORAGE_BYTES", len(PNG)):
            await self.upload(self.a)
            form = FormData()
            form.add_field("file", PNG, filename="overflow.png", content_type="image/png")
            await self.request("POST", "/api/attachments", self.a, expected=400, data=form)

        rows = list(self.service.store.db.execute(
            "SELECT size,path FROM attachments WHERE owner_id=?", (self.a["device"]["id"],)
        ))
        self.assertEqual([row["size"] for row in rows], [len(PNG)])
        self.assertEqual(
            sorted(path.name for path in self.service.upload_dir.iterdir()),
            [Path(rows[0]["path"]).name],
        )

    async def test_attachment_discard_is_owner_only_frees_quota_and_keeps_references(self):
        first = await self.upload(self.a)
        first_record = self.service.store.get_attachment(self.a["device"]["id"], first["id"])
        first_path = Path(first_record["path"])
        await self.request("DELETE", f"/api/attachments/{first['id']}", self.b, expected=403)
        await self.request("DELETE", f"/api/attachments/{first['id']}", self.admin, expected=403)
        self.assertTrue(first_path.is_file())

        deleted = await self.request("DELETE", f"/api/attachments/{first['id']}", self.a)
        self.assertEqual(deleted, {"deleted": True})
        self.assertFalse(first_path.exists())
        with self.assertRaises(PermissionError):
            self.service.store.get_attachment(self.a["device"]["id"], first["id"])

        with mock.patch("codex_lan.store.MAX_ATTACHMENT_STORAGE_BYTES", len(PNG)):
            replacement = await self.upload(self.a)
        room = await self.room(self.a)
        created = self.service.store.create_run(
            self.a["device"]["id"], room["id"], "with image", [replacement["id"]], "referenced"
        )
        self.service.store.finish_run(created["run"]["id"], "completed", "done")
        referenced_path = Path(self.service.store.get_attachment(
            self.a["device"]["id"], replacement["id"]
        )["path"])

        retained = await self.request("POST", f"/api/attachments/{replacement['id']}/discard", self.a)
        self.assertEqual(retained, {"deleted": False})
        self.assertTrue(referenced_path.is_file())
        self.assertEqual(
            self.service.store.get_attachment(self.a["device"]["id"], replacement["id"])["id"],
            replacement["id"],
        )

    async def test_attachment_reconciliation_is_bounded_and_preserves_live_references(self):
        now = time.time()
        abandoned = await self.upload(self.a)
        referenced = await self.upload(self.a)
        abandoned_path = Path(self.service.store.get_attachment(
            self.a["device"]["id"], abandoned["id"]
        )["path"])
        referenced_path = Path(self.service.store.get_attachment(
            self.a["device"]["id"], referenced["id"]
        )["path"])
        room = await self.room(self.a)
        created = self.service.store.create_run(
            self.a["device"]["id"], room["id"], "keep", [referenced["id"]], "gc-reference"
        )
        self.service.store.finish_run(created["run"]["id"], "completed", "done")
        outside = Path(self.temp.name) / "outside.png"
        outside.write_bytes(PNG)
        outside_attachment = self.service.store.add_attachment(
            self.a["device"]["id"], "outside.png", "image/png", len(PNG), str(outside)
        )
        old = now - 8 * 24 * 60 * 60
        with self.service.store.db:
            self.service.store.db.execute(
                "UPDATE attachments SET created_at=? WHERE id IN (?,?,?)",
                (old, abandoned["id"], referenced["id"], outside_attachment["id"]),
            )

        old_orphan = self.service.upload_dir / uuid.uuid4().hex
        fresh_orphan = self.service.upload_dir / uuid.uuid4().hex
        invalid_orphan = self.service.upload_dir / ("z" * 32)
        old_orphan.write_bytes(PNG)
        fresh_orphan.write_bytes(PNG)
        invalid_orphan.write_bytes(PNG)
        os.utime(old_orphan, (old, old))
        os.utime(invalid_orphan, (old, old))
        external_target = Path(self.temp.name) / "symlink-target.png"
        external_target.write_bytes(PNG)
        orphan_symlink = self.service.upload_dir / uuid.uuid4().hex
        orphan_symlink.symlink_to(external_target)
        os.utime(orphan_symlink, (old, old), follow_symlinks=False)

        result = self.service.reconcile_attachments(now=now)

        self.assertEqual(result, {"stale_rows": 2, "orphan_files": 2})
        self.assertFalse(abandoned_path.exists())
        self.assertFalse(old_orphan.exists())
        self.assertFalse(orphan_symlink.exists())
        self.assertTrue(external_target.is_file())
        self.assertTrue(outside.is_file())
        self.assertTrue(fresh_orphan.is_file())
        self.assertTrue(invalid_orphan.is_file())
        self.assertTrue(referenced_path.is_file())
        self.assertEqual(
            self.service.store.get_attachment(self.a["device"]["id"], referenced["id"])["id"],
            referenced["id"],
        )
        for attachment_id in (abandoned["id"], outside_attachment["id"]):
            with self.assertRaises(PermissionError):
                self.service.store.get_attachment(self.a["device"]["id"], attachment_id)

    async def test_attachment_reconciliation_retries_file_left_after_committed_discard(self):
        attachment = await self.upload(self.a)
        record = self.service.store.get_attachment(
            self.a["device"]["id"], attachment["id"]
        )
        path = Path(record["path"])
        old = time.time() - 2 * ATTACHMENT_ORPHAN_GRACE_SECONDS
        os.utime(path, (old, old))

        with mock.patch.object(self.service, "cleanup_attachment_files"):
            deleted = await self.request(
                "DELETE", f"/api/attachments/{attachment['id']}", self.a
            )
        self.assertEqual(deleted, {"deleted": True})
        self.assertTrue(path.is_file())
        with self.assertRaises(PermissionError):
            self.service.store.get_attachment(self.a["device"]["id"], attachment["id"])

        result = self.service.reconcile_attachments(now=time.time())
        self.assertEqual(result, {"stale_rows": 0, "orphan_files": 1})
        self.assertFalse(path.exists())

    async def test_attachment_reconciliation_removes_legacy_hyphenated_uuid_orphan(self):
        legacy_id = uuid.uuid5(uuid.NAMESPACE_URL, "legacy-image")
        legacy_path = self.service.upload_dir / str(legacy_id)
        legacy_path.write_bytes(PNG)
        old = time.time() - 2 * ATTACHMENT_ORPHAN_GRACE_SECONDS
        os.utime(legacy_path, (old, old))

        result = self.service.reconcile_attachments(now=time.time())

        self.assertEqual(result, {"stale_rows": 0, "orphan_files": 1})
        self.assertFalse(legacy_path.exists())

    async def test_normal_room_delete_clears_only_its_runtime_thread_state(self):
        room = await self.room(self.a)
        owner_id = self.a["device"]["id"]
        thread_id = "thread-room-delete"
        unrelated = (self.b["device"]["id"], "thread-survives")
        with self.service.store.db:
            self.service.store.db.execute(
                "UPDATE rooms SET thread_id=? WHERE id=?", (thread_id, room["id"])
            )
        created = self.service.store.create_run(owner_id, room["id"], "history", [], "delete-cache")
        self.service.store.finish_run(created["run"]["id"], "completed", "done")
        key = (owner_id, thread_id)
        self.service.runtime.by_thread[key] = "stale-room"
        self.service.runtime.thread_totals[key] = {"input_tokens": 10}
        self.service.runtime.by_thread[unrelated] = "keep"
        self.service.runtime.thread_totals[unrelated] = {"input_tokens": 20}
        self.service.runtime.activity_cache[created["run"]["id"]] = {"stale": True}

        await self.request("DELETE", f"/api/rooms/{room['id']}", self.admin)

        self.assertNotIn(key, self.service.runtime.by_thread)
        self.assertNotIn(key, self.service.runtime.thread_totals)
        self.assertNotIn(created["run"]["id"], self.service.runtime.activity_cache)
        self.assertEqual(self.service.runtime.by_thread[unrelated], "keep")
        self.assertEqual(self.service.runtime.thread_totals[unrelated], {"input_tokens": 20})

    async def test_attachment_gc_task_is_registered_for_runtime_shutdown(self):
        tasks = [
            task for task in self.service.runtime.tasks
            if getattr(task.get_coro(), "__name__", "") == "attachment_gc_loop"
        ]
        self.assertEqual(len(tasks), 1)
        self.assertFalse(tasks[0].done())
        await self.service.runtime.close()
        self.assertTrue(tasks[0].done())
        self.assertTrue(tasks[0].cancelled())

    async def test_duplicate_requests_dispatch_only_one_rpc_and_ultra_is_kept(self):
        worker = await self.worker(self.a, "a")
        room = await self.room(self.a)
        first, second = await asyncio.gather(self.submit_run(self.a, room), self.submit_run(self.a, room))
        self.assertEqual(first["run"]["id"], second["run"]["id"])
        self.assertEqual(sum(bool(value["deduplicated"]) for value in (first, second)), 1)
        await self.until(lambda: bool(worker.turn))
        turns = [call for call in worker.calls if call["method"] == "turn/start"]
        starts = [call for call in worker.calls if call["method"] == "thread/start"]
        self.assertEqual(starts[0]["params"]["config"], {
            "model_auto_compact_token_limit": 64_000,
            "model_auto_compact_token_limit_scope": "body_after_prefix",
            "tool_output_token_limit": 12_000,
        })
        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0]["params"]["effort"], "ultra")
        self.assertEqual(turns[0]["params"]["cwd"], "/home/project")
        await self.request("POST", f"/api/rooms/{room['id']}/runs", self.a, expected=400,
                           json={"text": "changed", "request_id": "one"})
        await self.request("PATCH", f"/api/rooms/{room['id']}", self.a, expected=400, json={"effort": "invented"})
        await self.request("PATCH", f"/api/rooms/{room['id']}", self.a, expected=400, json={"model": "invented"})
        await self.request("PATCH", f"/api/rooms/{room['id']}", self.a, expected=400, json={"thread_id": "foreign-thread"})

    async def test_run_uses_the_composer_settings_snapshot_atomically(self):
        worker = await self.worker(self.a, "a")
        room = await self.room(self.a)
        created = await self.ready_run(
            self.a, worker, room, request_id="composer-snapshot",
            model="astra", effort="low", speed="standard",
        )
        stored = self.service.store.get_room(self.a["device"]["id"], room["id"])
        turn = next(call for call in worker.calls if call["method"] == "turn/start")
        self.assertEqual((stored["model"], stored["effort"], stored["speed"]),
                         ("astra", "low", "standard"))
        self.assertEqual((created["run"]["model"], created["run"]["effort"], created["run"]["speed"]),
                         ("astra", "low", "standard"))
        self.assertEqual((turn["params"]["model"], turn["params"]["effort"]), ("astra", "low"))

    async def test_default_room_name_and_safe_workspace_changes(self):
        worker = await self.worker(self.a, "workspace-owner")
        room = await self.request(
            "POST", "/api/rooms", self.a, expected=201,
            json={"cwd": "/home/workspace-owner/original", "model": "astra", "effort": "ultra"},
        )
        self.assertEqual(room["title"], "New chat")
        changed = await self.request(
            "PATCH", f"/api/rooms/{room['id']}", self.a,
            json={"cwd": "/home/workspace-owner/next"},
        )
        self.assertEqual(changed["cwd"], "/home/workspace-owner/next")
        await self.request(
            "PATCH", f"/api/rooms/{room['id']}", self.admin, expected=403,
            json={"cwd": "/home/admin/foreign"},
        )
        await self.request(
            "PATCH", f"/api/rooms/{room['id']}", self.a, expected=400,
            json={"cwd": "relative/project"},
        )

        active = await self.ready_run(self.a, worker, room)
        busy = await self.request(
            "PATCH", f"/api/rooms/{room['id']}", self.a, expected=409,
            json={"cwd": "/home/workspace-owner/busy"},
        )
        self.assertTrue(busy["busy"])
        self.assertEqual(self.service.store.get_room(self.a["device"]["id"], room["id"])["cwd"],
                         "/home/workspace-owner/next")
        await worker.complete()
        await self.until(lambda: active["run"]["id"] not in self.service.runtime.runs)
        changed = await self.request(
            "PATCH", f"/api/rooms/{room['id']}", self.a,
            json={"cwd": "/home/workspace-owner/final"},
        )
        self.assertEqual(changed["cwd"], "/home/workspace-owner/final")

    async def test_backend_admission_marker_blocks_new_runs_but_keeps_health_available(self):
        self.service.store.set_device(self.admin_id, self.a["device"]["id"], max_concurrent=2)
        worker = await self.worker(self.a, "maintenance")
        active_room = await self.room(self.a)
        active = await self.ready_run(self.a, worker, active_room)
        turn_starts_before_marker = len([call for call in worker.calls if call["method"] == "turn/start"])
        marker = Path(self.service.backend_update_marker)
        marker.touch()
        try:
            health = await self.request("GET", "/api/health")
            self.assertTrue(health["ok"])
            self.assertEqual(health["backend_update"], "verifying")
            replay = await self.request("POST", f"/api/rooms/{active_room['id']}/runs", self.a, expected=202,
                                        json={"text": "hello", "request_id": "one"})
            self.assertTrue(replay["deduplicated"])
            self.assertEqual(replay["run"]["id"], active["run"]["id"])
            await self.request("POST", f"/api/rooms/{active_room['id']}/runs", self.a, expected=503,
                               json={"text": "Wait for backend maintenance", "request_id": "maintenance"})
            self.assertEqual(self.service.store.list_active_runs(self.a["device"]["id"]),
                             [self.service.store.get_run_internal(active["run"]["id"])])
            self.assertEqual(len([call for call in worker.calls if call["method"] == "turn/start"]),
                             turn_starts_before_marker)
            await worker.delta("The active response is still allowed to finish.")
            await self.until(lambda: self.service.runtime.runs[active["run"]["id"]].text() != "")
        finally:
            marker.unlink(missing_ok=True)
        await worker.complete()
        await self.until(lambda: active["run"]["id"] not in self.service.runtime.runs)
        resumed = await self.ready_run(self.a, worker, active_room, request_id="after-maintenance")
        await worker.complete()
        await self.until(lambda: resumed["run"]["id"] not in self.service.runtime.runs)

    async def test_resume_requests_only_metadata_and_keeps_existing_thread(self):
        worker = await self.worker(self.a, "a")
        room = await self.room(self.a)
        self.service.store.update_room(self.a["device"]["id"], room["id"], thread_id=worker.thread)
        await self.ready_run(self.a, worker, room)
        resumes = [call for call in worker.calls if call["method"] == "thread/resume"]
        self.assertEqual(len(resumes), 1)
        self.assertEqual(resumes[0]["params"]["threadId"], worker.thread)
        self.assertIs(resumes[0]["params"]["excludeTurns"], True)
        self.assertEqual(resumes[0]["params"]["config"], {
            "model_auto_compact_token_limit": 64_000,
            "model_auto_compact_token_limit_scope": "body_after_prefix",
            "tool_output_token_limit": 12_000,
        })
        self.assertFalse(any(call["method"] == "thread/start" for call in worker.calls))

    async def test_concurrent_devices_stream_separately_and_browser_reconnect_retains_run(self):
        worker_a, worker_b = await self.worker(self.a, "a"), await self.worker(self.b, "b")
        room_a, room_b = await self.room(self.a), await self.room(self.b)
        run_a, run_b = await asyncio.gather(self.ready_run(self.a, worker_a, room_a), self.ready_run(self.b, worker_b, room_b))
        await worker_a.delta("alpha")
        await worker_b.delta("beta")
        await self.until(lambda: self.service.runtime.runs[run_a["run"]["id"]].text() == "alpha")
        await self.until(lambda: self.service.runtime.runs[run_b["run"]["id"]].text() == "beta")
        async with ClientSession() as browser:
            async with browser.get(self.client.make_url(f"/api/rooms/{room_a['id']}/messages"), headers={"Authorization": "Bearer " + self.a["token"]}) as response:
                snapshot = await response.json()
                self.assertEqual(snapshot["runs"][0]["output_text"], "alpha")
        self.assertIn(run_a["run"]["id"], self.service.runtime.runs)
        replay = await self.request("GET", f"/api/rooms/{room_a['id']}/events", self.a)
        self.assertEqual("".join(event["payload"].get("text", "") for event in replay["events"] if event["kind"] == "delta"), "alpha")
        self.assertEqual(replay["runs"][0]["output_text"], "alpha")
        await worker_a.complete()
        await self.until(lambda: run_a["run"]["id"] not in self.service.runtime.runs)
        snapshot = await self.request("GET", f"/api/rooms/{room_a['id']}/messages", self.a)
        self.assertEqual(snapshot["messages"][-1]["content"], "alpha")
        self.assertEqual(snapshot["runs"][0]["status"], "completed")
        self.assertIn(run_b["run"]["id"], self.service.runtime.runs)

    async def test_revocation_disconnects_only_target_worker_and_blocks_existing_browser(self):
        worker_a, worker_b = await self.worker(self.a, "a"), await self.worker(self.b, "b")
        room_a, room_b = await self.room(self.a), await self.room(self.b)
        run_a, run_b = await self.ready_run(self.a, worker_a, room_a), await self.ready_run(self.b, worker_b, room_b)
        await worker_a.delta("saved before revoke")
        await self.until(lambda: self.service.runtime.runs[run_a["run"]["id"]].text() != "")
        await self.request("PATCH", f"/api/admin/devices/{self.a['device']['id']}", self.admin, json={"status": "revoked"})
        await self.until(lambda: worker_a.ws.closed)
        self.assertFalse(worker_b.ws.closed)
        self.assertIn(run_b["run"]["id"], self.service.runtime.runs)
        self.assertEqual(self.service.store.get_run_internal(run_a["run"]["id"])["status"], "interrupted")
        await self.request("GET", "/api/rooms", self.a, expected=403)
        with self.assertRaises(WSServerHandshakeError) as failure:
            await self.client.ws_connect("/api/worker", headers={"Authorization": "Bearer " + self.a["token"]})
        self.assertEqual(failure.exception.status, 403)
        contents = await self.request("GET", f"/api/rooms/{room_a['id']}/messages", self.admin)
        self.assertEqual(contents["messages"][-1]["content"], "saved before revoke")

    async def test_device_delete_rejects_active_work_and_cleans_only_target_files(self):
        room = await self.room(self.a)
        target_upload = await self.upload(self.a)
        survivor_upload = await self.upload(self.b)
        target_path = Path(self.service.store.get_attachment(
            self.a["device"]["id"], target_upload["id"]
        )["path"])
        survivor_path = Path(self.service.store.get_attachment(
            self.b["device"]["id"], survivor_upload["id"]
        )["path"])
        external_path = Path(self.temp.name) / "not-server-owned.txt"
        external_path.write_text("keep", encoding="utf-8")
        self.service.store.add_attachment(
            self.a["device"]["id"], "outside.txt", "text/plain", 4, str(external_path)
        )
        created = self.service.store.create_run(
            self.a["device"]["id"], room["id"], "active", [], "delete-active"
        )
        run_id = created["run"]["id"]
        target_id = self.a["device"]["id"]
        remote_state = Path(self.temp.name) / "remote-workers" / target_id
        remote_state.mkdir(parents=True)
        (remote_state / "history.jsonl").write_text("private", encoding="utf-8")
        pending_pairing_hash = hashlib.sha256(b"pending-delete").hexdigest()
        with self.service.store.db:
            self.service.store.db.execute(
                "INSERT INTO client_pairings(code_hash,device_id,expires_at,connector_version) VALUES(?,?,?,?)",
                (pending_pairing_hash, target_id, 9_999_999_999, "0.3.2"),
            )
        broken_session = mock.AsyncMock()
        broken_session.close.side_effect = RuntimeError("connector already disappeared")
        self.service.remote_sessions[target_id] = broken_session

        await self.request("DELETE", f"/api/admin/devices/{target_id}", self.b, expected=403)
        await self.request("DELETE", f"/api/admin/devices/{self.admin_id}", self.admin, expected=400)
        await self.request("DELETE", f"/api/admin/devices/{target_id}", self.admin, expected=409)
        self.assertTrue(target_path.is_file())
        self.assertEqual(self.service.store.get_device(target_id)["id"], target_id)

        self.service.store.finish_run(run_id, "cancelled", "Stopped before device deletion")
        with mock.patch("codex_lan.server.LOGGER.exception") as cleanup_error:
            deleted = await self.request("DELETE", f"/api/admin/devices/{target_id}", self.admin)
        self.assertEqual(deleted, {"ok": True, "device_id": target_id})
        broken_session.close.assert_awaited_once()
        cleanup_error.assert_called_once()
        self.assertFalse(target_path.exists())
        self.assertTrue(survivor_path.is_file())
        self.assertTrue(external_path.is_file())
        self.assertFalse(remote_state.exists())
        self.assertIsNone(self.service.store.db.execute(
            "SELECT 1 FROM client_pairings WHERE code_hash=?", (pending_pairing_hash,)
        ).fetchone())
        await self.request("GET", "/api/rooms", self.a, expected=403)
        devices = await self.request("GET", "/api/admin/devices", self.admin)
        self.assertNotIn(target_id, {device["id"] for device in devices["devices"]})
        self.assertIn(self.b["device"]["id"], {device["id"] for device in devices["devices"]})
        self.assertEqual(list(self.service.store.db.execute("PRAGMA foreign_key_check")), [])

    async def test_stop_is_owner_or_admin_only_and_disconnect_finishes_once(self):
        worker = await self.worker(self.a, "a")
        room = await self.room(self.a)
        first = await self.ready_run(self.a, worker, room)
        await self.request("POST", f"/api/runs/{first['run']['id']}/stop", self.b, expected=403)
        await self.request("POST", f"/api/runs/{first['run']['id']}/stop", self.a)
        self.assertEqual(self.service.store.get_run_internal(first["run"]["id"])["status"], "running")
        await worker.usage(50, 10)
        await worker.complete(status="interrupted")
        await self.until(lambda: first["run"]["id"] not in self.service.runtime.runs)
        self.assertEqual(self.service.store.get_run_internal(first["run"]["id"])["status"], "cancelled")
        messages = self.service.store.list_messages(self.a["device"]["id"], room["id"])
        self.assertEqual(messages[-1]["token_usage"], 60)
        self.assertEqual(len([call for call in worker.calls if call["method"] == "turn/interrupt"]), 1)
        await self.request("POST", f"/api/runs/{first['run']['id']}/stop", self.a)
        second = await self.ready_run(self.a, worker, room, request_id="second")
        await worker.delta("partial")
        await self.until(lambda: self.service.runtime.runs[second["run"]["id"]].text() != "")
        await worker.close()
        await self.until(lambda: second["run"]["id"] not in self.service.runtime.runs)
        self.assertEqual(self.service.store.get_run_internal(second["run"]["id"])["status"], "interrupted")
        messages = self.service.store.list_messages(self.a["device"]["id"], room["id"])
        self.assertEqual(len(messages), 4)
        self.assertEqual(messages[-1]["content"], "partial")

    async def test_true_close_is_terminal_once_and_stale_cleanup_keeps_reconnected_peer(self):
        worker = await self.worker(self.a, "first")
        room = await self.room(self.a)
        created = await self.ready_run(self.a, worker, room)
        await worker.delta("saved before transport close")
        await self.until(lambda: self.service.runtime.runs[created["run"]["id"]].text() != "")
        old_peer = self.service.runtime.runs[created["run"]["id"]].peer

        await asyncio.wait_for(worker.close(), 1)
        await self.until(lambda: created["run"]["id"] not in self.service.runtime.runs)
        await self.until(lambda: self.a["device"]["id"] not in self.service.connecting)
        self.assertEqual(
            self.service.store.get_run_internal(created["run"]["id"])["status"],
            "interrupted",
        )
        completed = self.service.store.db.execute(
            "SELECT COUNT(*) FROM events WHERE run_id = ? AND kind = 'completed'",
            (created["run"]["id"],),
        ).fetchone()[0]
        self.assertEqual(completed, 1)

        replacement = await self.worker(self.a, "replacement")
        fresh_peer = self.service.runtime.peers[self.a["device"]["id"]]
        self.assertIsNot(fresh_peer, old_peer)
        await self.service.runtime.disconnect(old_peer)
        self.assertIs(self.service.runtime.peers[self.a["device"]["id"]], fresh_peer)
        self.assertFalse(any(call["method"] == "turn/start" for call in replacement.calls))
        completed = self.service.store.db.execute(
            "SELECT COUNT(*) FROM events WHERE run_id = ? AND kind = 'completed'",
            (created["run"]["id"],),
        ).fetchone()[0]
        self.assertEqual(completed, 1)
        messages = self.service.store.list_messages(self.a["device"]["id"], room["id"])
        self.assertEqual(messages[-1]["content"], "saved before transport close")

    async def test_usage_replays_and_stale_turn_events_do_not_double_charge(self):
        worker = await self.worker(self.a, "a")
        room = await self.room(self.a)
        first = await self.ready_run(self.a, worker, room)
        await worker.usage(100, 20, 80)
        await worker.usage(100, 20, 80)
        await worker.usage(80, 10, 60)
        await worker.delta("one")
        await worker.complete()
        await self.until(lambda: first["run"]["id"] not in self.service.runtime.runs)
        old_turn = worker.turn
        second = await self.ready_run(self.a, worker, room, request_id="second")
        await worker.event("item/agentMessage/delta", turnId=old_turn, itemId="answer", delta="STALE")
        await worker.usage(120, 30, 90)
        await worker.delta("two")
        await worker.complete()
        await self.until(lambda: second["run"]["id"] not in self.service.runtime.runs)
        report = await self.request("GET", "/api/admin/usage", self.admin)
        usage = next(row for row in report["usage"] if row["device_id"] == self.a["device"]["id"])
        self.assertEqual(usage["total_tokens"], 150)
        messages = await self.request("GET", f"/api/rooms/{room['id']}/messages", self.a)
        self.assertEqual(messages["messages"][-1]["content"], "two")
        self.assertEqual(messages["messages"][-1]["token_usage"], 30)
        self.assertEqual(usage["unknown_usage_runs"], 0)

    async def test_unusually_expensive_single_response_is_stopped_once(self):
        worker = await self.worker(self.a, "a")
        room = await self.room(self.a)
        created = await self.ready_run(self.a, worker, room)
        self.assertEqual(MAX_RUN_CREDITS_MICROS, 250_000_000)
        self.assertEqual(MAX_RUN_TOTAL_TOKENS, 10_000_000)
        with mock.patch(
            "codex_lan.runtime.estimate_usage",
            return_value={"credits_micros": MAX_RUN_CREDITS_MICROS},
        ):
            await worker.usage(1_000_000, 0)
            await self.until(
                lambda: any(call["method"] == "turn/interrupt" for call in worker.calls)
            )
        await worker.complete("interrupted")
        await self.until(lambda: created["run"]["id"] not in self.service.runtime.runs)
        run = self.service.store.get_run_internal(created["run"]["id"])
        self.assertEqual(run["status"], "interrupted")
        self.assertEqual(run["error"], RUN_USAGE_LIMIT_MESSAGE)
        self.assertEqual(sum(call["method"] == "turn/interrupt" for call in worker.calls), 1)

    async def test_unusually_long_response_is_stopped_by_wall_clock(self):
        worker = await self.worker(self.a, "a")
        room = await self.room(self.a)
        self.assertEqual(MAX_RUN_SECONDS, 45 * 60)
        with mock.patch("codex_lan.runtime.MAX_RUN_SECONDS", 0.1):
            created = await self.ready_run(self.a, worker, room)
            await self.until(
                lambda: any(call["method"] == "turn/interrupt" for call in worker.calls)
            )
        await worker.complete("interrupted")
        await self.until(lambda: created["run"]["id"] not in self.service.runtime.runs)
        run = self.service.store.get_run_internal(created["run"]["id"])
        self.assertEqual((run["status"], run["error"]), ("interrupted", RUN_TIME_LIMIT_MESSAGE))
        self.assertEqual(sum(call["method"] == "turn/interrupt" for call in worker.calls), 1)

    async def test_unknown_future_model_is_still_bounded_by_total_tokens(self):
        worker = await self.worker(self.a, "a")
        room = await self.room(self.a)
        created = await self.ready_run(self.a, worker, room)
        await worker.usage(MAX_RUN_TOTAL_TOKENS, 0)
        await self.until(lambda: any(call["method"] == "turn/interrupt" for call in worker.calls))
        await worker.complete("interrupted")
        await self.until(lambda: created["run"]["id"] not in self.service.runtime.runs)
        run = self.service.store.get_run_internal(created["run"]["id"])
        self.assertEqual((run["status"], run["error"]), ("interrupted", RUN_USAGE_LIMIT_MESSAGE))

    async def test_malformed_token_usage_does_not_disconnect_worker(self):
        worker = await self.worker(self.admin, "admin")
        room = await self.room(self.admin)
        created = await self.ready_run(self.admin, worker, room)
        malformed = [
            None,
            [],
            {"total": {"inputTokens": None, "outputTokens": 2, "cachedInputTokens": 0, "totalTokens": 2},
             "last": {"inputTokens": 0, "outputTokens": 2, "cachedInputTokens": 0, "totalTokens": 2}},
            {"total": {"inputTokens": "junk", "outputTokens": 2, "cachedInputTokens": 0, "totalTokens": 2},
             "last": {"inputTokens": 0, "outputTokens": 2, "cachedInputTokens": 0, "totalTokens": 2}},
            {"total": {"inputTokens": 5, "outputTokens": 2, "cachedInputTokens": 0},
             "last": {"inputTokens": 5, "outputTokens": 2, "cachedInputTokens": 0, "totalTokens": 7}},
            {"total": {"inputTokens": -1, "outputTokens": 2, "cachedInputTokens": 0, "totalTokens": 2},
             "last": {"inputTokens": 0, "outputTokens": 2, "cachedInputTokens": 0, "totalTokens": 2}},
            {"total": {"inputTokens": "999999999999999999999999999999999", "outputTokens": 2,
                       "cachedInputTokens": 0, "totalTokens": 2},
             "last": {"inputTokens": 0, "outputTokens": 2, "cachedInputTokens": 0, "totalTokens": 2}},
        ]
        for token_usage in malformed:
            await worker.event("thread/tokenUsage/updated", tokenUsage=token_usage)
        await worker.usage(40, 8, 20)
        await worker.delta("still connected")
        await self.until(lambda: self.service.runtime.runs[created["run"]["id"]].text() == "still connected")
        self.assertTrue(self.service.runtime.connected(self.admin["device"]["id"]))
        await worker.complete()
        await self.until(lambda: created["run"]["id"] not in self.service.runtime.runs)
        messages = await self.request("GET", f"/api/rooms/{room['id']}/messages", self.admin)
        self.assertEqual(messages["messages"][-1]["content"], "still connected")
        self.assertEqual(messages["messages"][-1]["token_usage"], 48)


class _Timeout:
    """CODEX: Bound async polling on Python 3.10 without a new dependency."""

    def __init__(self, seconds):
        self.seconds = seconds

    async def __aenter__(self):
        self.task = asyncio.current_task()
        self.handle = asyncio.get_running_loop().call_later(self.seconds, self.task.cancel)

    async def __aexit__(self, exc_type, exc, traceback):
        self.handle.cancel()
        if exc_type is asyncio.CancelledError:
            raise TimeoutError("Test condition did not become true") from exc


if __name__ == "__main__":
    unittest.main()
