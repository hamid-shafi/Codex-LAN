"""CODEX: Active-turn follow-ups, durable receipts, migration and delivery ambiguity."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from aiohttp import FormData

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from project_layout import configure_imports, source_file
configure_imports(__file__)

import test_lan_server as fixtures
from codex_lan.runtime import Runtime, SteeringConflict
from codex_lan.store import Store
from codex_lan.worker import AppServerRpcError, WorkerConnection, WorkerError


class SteeringServerTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.LanServerTests.asyncSetUp
    asyncTearDown = fixtures.LanServerTests.asyncTearDown
    request = fixtures.LanServerTests.request
    until = fixtures.LanServerTests.until
    worker = fixtures.LanServerTests.worker
    room = fixtures.LanServerTests.room
    submit_run = fixtures.LanServerTests.submit_run
    ready_run = fixtures.LanServerTests.ready_run
    upload = fixtures.LanServerTests.upload

    async def setup_turn(self):
        self.local_worker = await self.worker(self.a, "A")
        self.local_room = await self.room(self.a)
        self.created = await self.ready_run(self.a, self.local_worker, self.local_room)
        self.run_id = self.created["run"]["id"]
        self.live = self.service.runtime.runs[self.run_id]
        original = self.live.peer.call
        self.steer_calls = []
        self.provider_action = None

        async def call(method, params, timeout=45):
            if method != "turn/steer":
                return await original(method, params, timeout)
            self.steer_calls.append(params)
            if self.provider_action:
                return await self.provider_action()
            return {"turnId": self.local_worker.turn}

        self.live.peer.call = call

    async def steer(self, request_id="follow-one", expected=202, device=None, room=None, **fields):
        return await self.request("POST", f"/api/rooms/{(room or self.local_room)['id']}/steer", device or self.a,
                                  expected=expected, json={"run_id": self.run_id, "request_id": request_id,
                                  "text": "Also check the tests", "tool_read_only": False, **fields})

    async def test_multiple_followups_keep_same_turn_stream_and_usage(self):
        await self.setup_turn()
        await self.local_worker.delta("Existing commentary")
        await self.until(lambda: self.live.text() == "Existing commentary")
        first = await self.steer()
        second = await self.steer("follow-two", text="Keep the current model")
        self.assertTrue(first["steered"])
        self.assertEqual(first["steering"]["status"], "accepted")
        self.assertEqual(len(self.steer_calls), 2)
        self.assertTrue(all(set(call) == {"threadId", "expectedTurnId", "input"} for call in self.steer_calls))
        self.assertEqual(self.live.text(), "Existing commentary")
        runs = self.service.store.list_runs(self.a["device"]["id"], self.local_room["id"])
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["user_message_id"], self.created["user_message"]["id"])
        await self.local_worker.complete()
        await self.until(lambda: self.run_id not in self.service.runtime.runs)
        messages = self.service.store.list_messages(self.a["device"]["id"], self.local_room["id"])
        self.assertEqual([message["role"] for message in messages], ["user", "user", "user", "assistant"])
        self.assertEqual(messages[1]["id"], first["user_message"]["id"])
        self.assertEqual(messages[2]["id"], second["user_message"]["id"])
        events = await self.request("GET", f"/api/rooms/{self.local_room['id']}/events", self.a)
        self.assertEqual(len([event for event in events["events"] if event["kind"] == "steered"]), 2)

    async def test_draining_blocks_new_submissions_without_interrupting_live_turn(self):
        await self.setup_turn()
        self.live.peer.draining = True
        self.assertFalse(self.service.runtime.connected(self.a["device"]["id"]))
        self.assertIn(self.run_id, self.service.runtime.runs)
        self.assertFalse(self.live.stopped)
        await self.local_worker.delta("Still working")
        await self.until(lambda: self.live.text() == "Still working")
        self.assertFalse(any(call["method"] == "turn/interrupt" for call in self.local_worker.calls))

    async def test_duplicate_requests_are_serialized_and_replayed_after_completion(self):
        await self.setup_turn()
        entered, release = asyncio.Event(), asyncio.Event()

        async def delayed():
            entered.set()
            await release.wait()
            return {"turnId": self.local_worker.turn}

        self.provider_action = delayed
        first_task = asyncio.create_task(self.steer())
        await entered.wait()
        second_task = asyncio.create_task(self.steer(expected=200))
        await asyncio.sleep(0.03)
        release.set()
        first, second = await asyncio.gather(first_task, second_task)
        self.assertEqual(first["user_message"]["id"], second["user_message"]["id"])
        self.assertEqual(len(self.steer_calls), 1)
        await self.local_worker.complete()
        await self.until(lambda: self.run_id not in self.service.runtime.runs)
        replay = await self.steer(expected=200)
        self.assertEqual(replay["user_message"]["id"], first["user_message"]["id"])
        await self.steer(expected=400, text="different content")
        self.assertEqual(len(self.steer_calls), 1)

    async def test_owner_room_and_quote_isolation(self):
        await self.setup_turn()
        await self.steer(device=self.b, expected=403)
        await self.steer(device=self.admin, expected=403)
        other = await self.room(self.a)
        await self.steer(room=other, expected=403)
        await self.steer(reply_to_id="foreign-message", reply_quote="quote", expected=400)
        self.assertFalse(self.steer_calls)

    async def test_images_and_reply_quote_are_sent_without_model_overrides(self):
        await self.setup_turn()
        attachment = await self.upload(self.a)
        result = await self.steer(attachment_ids=[attachment["id"]], reply_quote="hello",
                                  reply_to_id=self.created["user_message"]["id"])
        self.assertEqual(self.steer_calls[0]["input"][1]["type"], "localImage")
        self.assertIn("> hello", self.steer_calls[0]["input"][0]["text"])
        self.assertTrue(result["user_message"]["attachments"][0]["url"].startswith("/api/attachments/"))
        foreign = await self.upload(self.b)
        await self.steer("foreign-image", attachment_ids=[foreign["id"]], expected=403)

    async def test_file_followup_passes_a_path_without_contents_or_a_model_override(self):
        await self.setup_turn()
        form = FormData()
        form.add_field("file", b"PRIVATE-ATTACHMENT-CONTENTS", filename="followup.txt", content_type="text/plain")
        attachment = await self.request("POST", "/api/attachments", self.a, expected=201, data=form)
        result = await self.steer(attachment_ids=[attachment["id"]])
        self.assertTrue(result["steered"])
        reference = self.steer_calls[0]["input"][1]
        self.assertEqual(reference["type"], "text")
        self.assertIn("followup.txt", reference["text"])
        self.assertIn("/fake/client/upload.png", reference["text"])
        self.assertNotIn("PRIVATE-ATTACHMENT-CONTENTS", reference["text"])
        self.assertEqual(set(self.steer_calls[0]), {"threadId", "expectedTurnId", "input"})

    async def test_timeout_is_durable_uncertain_and_never_blindly_retried(self):
        await self.setup_turn()

        async def timeout():
            raise asyncio.TimeoutError()

        self.provider_action = timeout
        result = await self.steer()
        self.assertEqual(result["steering"]["status"], "uncertain")
        self.assertFalse(result["steered"])
        self.assertIsNone(result["user_message"])
        replay = await self.steer(expected=200)
        self.assertEqual(replay["steering"]["id"], result["steering"]["id"])
        self.assertEqual(len(self.steer_calls), 1)
        snapshot = self.service.runtime.snapshots(self.a["device"]["id"], self.local_room["id"])[0]
        self.assertEqual(snapshot["steering_receipts"][0]["status"], "uncertain")
        self.assertEqual(len(self.service.store.list_messages(self.a["device"]["id"], self.local_room["id"])), 1)

    async def test_provider_complete_race_rejects_without_creating_new_turn(self):
        await self.setup_turn()

        async def rejected():
            raise RuntimeError("Expected turn is not active")

        self.provider_action = rejected
        result = await self.steer(expected=409)
        self.assertFalse(result["retryable"])
        await self.steer(expected=409)
        self.assertEqual(len(self.steer_calls), 1)
        await self.local_worker.complete()
        await self.until(lambda: self.run_id not in self.service.runtime.runs)
        await self.steer("new-follow", expected=409)
        self.assertEqual(len(self.service.store.list_runs(self.a["device"]["id"], self.local_room["id"])), 1)

    async def test_startup_not_ready_is_retryable_without_provider_call(self):
        await self.setup_turn()
        turn = self.live.run.pop("turn_id")
        with mock.patch("codex_lan.runtime.asyncio.sleep", new=mock.AsyncMock()):
            with self.assertRaises(SteeringConflict) as failure:
                await self.service.runtime.steer(self.a["device"]["id"], self.local_room["id"], self.run_id,
                                                  "follow", [], "pending", tool_read_only=False)
        self.assertTrue(failure.exception.retryable)
        self.assertFalse(self.steer_calls)
        self.live.run["turn_id"] = turn
        result = await self.service.runtime.steer(self.a["device"]["id"], self.local_room["id"], self.run_id,
                                                   "follow", [], "pending", tool_read_only=False)
        self.assertEqual(result["steering"]["status"], "accepted")
        self.assertEqual(len(self.steer_calls), 1)

    async def test_quota_change_after_queue_blocks_final_dispatch_without_provider_call(self):
        await self.setup_turn()
        original = self.service.store.prepare_steering

        def queue_then_disable(*args, **kwargs):
            result = original(*args, **kwargs)
            self.service.store.set_device(
                self.admin_id, self.a["device"]["id"], weekly_share_percent=0
            )
            return result

        with mock.patch.object(self.service.store, "prepare_steering", side_effect=queue_then_disable):
            result = await self.steer(expected=400)
        self.assertIn("weekly account share exhausted", result["error"])
        self.assertFalse(self.steer_calls)
        receipt = self.service.store.db.execute(
            "SELECT status,user_message_id FROM steering_receipts WHERE request_id='follow-one'"
        ).fetchone()
        self.assertEqual((receipt["status"], receipt["user_message_id"]), ("queued", None))
        self.assertEqual(len(self.service.store.list_messages(self.a["device"]["id"], self.local_room["id"])), 1)

    async def test_followup_pagination_and_restart_receipt_recovery(self):
        await self.setup_turn()
        for number in range(5):
            await self.steer("follow-" + str(number))
        values = self.service.store.list_messages(self.a["device"]["id"], self.local_room["id"], limit=2)
        older = self.service.store.list_messages(self.a["device"]["id"], self.local_room["id"], before=values[0]["id"], limit=10)
        self.assertEqual(len(older + values), 6)
        receipt, _ = self.service.store.prepare_steering(self.a["device"]["id"], self.local_room["id"], self.run_id,
                                                        "delivery unknown", [], "restart-receipt",
                                                        tool_read_only=False)
        self.service.store.finish_steering(receipt["id"], "sending")
        self.service.store.recover_runs()
        self.assertEqual(self.service.store.steering_result(receipt["id"])["steering"]["status"], "uncertain")


class SteeringWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_worker_rejects_overrides_and_marks_transport_ambiguity(self):
        connection = object.__new__(WorkerConnection)
        connection.active_rpc_ids = set()
        connection.codex = mock.Mock()
        connection.codex.request = mock.AsyncMock(return_value={"turnId": "turn"})
        connection._send_result = mock.AsyncMock()
        params = {"threadId": "thread", "expectedTurnId": "turn", "input": [{"type": "text", "text": "follow"}]}
        await connection._relay_rpc("one", {"method": "turn/steer", "params": params})
        connection.codex.request.assert_awaited_once_with("turn/steer", params)
        await connection._relay_rpc("two", {"method": "turn/steer", "params": {**params, "model": "other"}})
        self.assertIn("Invalid", connection._send_result.call_args.kwargs["error"])
        for error, uncertain in ((WorkerError("request timed out"), True), (AppServerRpcError("turn ended"), False)):
            connection.codex.request.side_effect = error
            await connection._relay_rpc("three", {"method": "turn/steer", "params": params})
            self.assertEqual(connection._send_result.call_args.kwargs["delivery_uncertain"], uncertain)


class SteeringMigrationTests(unittest.TestCase):
    def test_legacy_unique_constraint_migration_preserves_ids_rowids_and_foreign_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            store = Store(path)
            owner = store.bootstrap_admin("Host")["device"]["id"]
            room = store.create_room(owner, "Room", "/work")
            run = store.create_run(owner, room["id"], "initial", [], "run")["run"]
            store.finish_run(run["id"], "completed", "finished")
            original = [tuple(row) for row in store.db.execute("SELECT rowid,* FROM messages ORDER BY rowid")]
            with store.db:
                store.db.execute("CREATE TABLE old_messages (id TEXT PRIMARY KEY,room_id TEXT NOT NULL REFERENCES rooms(id) ON DELETE CASCADE, "
                                 "run_id TEXT NOT NULL REFERENCES runs(id), role TEXT NOT NULL CHECK(role IN ('user','assistant')),content TEXT NOT NULL, "
                                 "attachment_ids TEXT NOT NULL DEFAULT '[]',duration_seconds REAL,created_at REAL NOT NULL,UNIQUE(run_id,role))")
                store.db.execute("INSERT INTO old_messages SELECT * FROM messages ORDER BY rowid")
                store.db.execute("DROP TABLE messages")
                store.db.execute("ALTER TABLE old_messages RENAME TO messages")
            store.close()
            migrated = Store(path)
            self.assertEqual([tuple(row) for row in migrated.db.execute("SELECT rowid,* FROM messages ORDER BY rowid")], original)
            self.assertFalse(migrated.db.execute("PRAGMA foreign_key_check").fetchall())
            with migrated.db:
                migrated.db.execute("INSERT INTO messages(id,room_id,run_id,role,content,created_at) VALUES('extra',?,?,'user','followup',100)", (room["id"], run["id"]))
            with self.assertRaises(sqlite3.IntegrityError), migrated.db:
                migrated.db.execute("INSERT INTO messages(id,room_id,run_id,role,content,created_at) VALUES('duplicate',?,?,'assistant','bad',101)", (room["id"], run["id"]))
            migrated.close()


if __name__ == "__main__":
    unittest.main()
