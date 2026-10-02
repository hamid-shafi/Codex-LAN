"""CODEX: Connection status and native-turn recovery retain one browser-visible response."""

import asyncio
import base64
import json
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import test_lan_server as fixture
from codex_lan.runtime import MAX_GENERATED_SOURCE_RECORDS, Peer, Runtime
from codex_lan.store import Store


class ConnectionRuntimeTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.LanServerTests.asyncSetUp
    asyncTearDown = fixture.LanServerTests.asyncTearDown
    request = fixture.LanServerTests.request
    until = fixture.LanServerTests.until
    worker = fixture.LanServerTests.worker
    room = fixture.LanServerTests.room
    submit_run = fixture.LanServerTests.submit_run
    ready_run = fixture.LanServerTests.ready_run

    async def prepared(self):
        worker = await self.worker(self.a, "a")
        room = await self.room(self.a)
        run = (await self.ready_run(self.a, worker, room))["run"]
        peer = self.service.runtime.peers[self.a["device"]["id"]]
        return worker, room, run, peer

    async def test_connection_and_latest_line_survive_room_snapshot_without_private_fields(self):
        worker, room, run, peer = await self.prepared()
        runtime = self.service.runtime
        runtime.event(peer, "item/agentMessage/delta", {"threadId": worker.thread, "turnId": worker.turn,
            "itemId": "progress", "delta": "First update\nChecking the current files"})
        runtime.event(peer, "lan/connection", {"threadId": worker.thread, "connection": {
            "state": "reconnecting", "mode": "proxy", "attempt": 3, "retry_in_seconds": 2,
            "credentials": "PRIVATE-SECRET", "endpoint": "PRIVATE-HOST"}})
        snapshot = await self.request("GET", f"/api/rooms/{room['id']}/messages", self.a)
        active = next(value for value in snapshot["runs"] if value["id"] == run["id"])
        self.assertEqual(active["latest_status_line"], "Checking the current files")
        self.assertEqual(active["connection"]["state"], "reconnecting")
        self.assertEqual(active["connection"]["mode"], "proxy")
        self.assertNotIn("PRIVATE", str(active))
        rooms = await self.request("GET", "/api/rooms", self.a)
        self.assertEqual(rooms["rooms"][0]["connection"]["attempt"], 3)
        me = await self.request("GET", "/api/me", self.a)
        self.assertEqual(me["connection"]["state"], "reconnecting")

    async def test_new_native_turn_keeps_logical_run_and_prior_output(self):
        worker, room, run, peer = await self.prepared()
        runtime = self.service.runtime
        old_turn = worker.turn
        runtime.event(peer, "item/agentMessage/delta", {"threadId": worker.thread, "turnId": old_turn,
            "itemId": "before", "delta": "Saved progress"})
        runtime.event(peer, "lan/turnResuming", {"threadId": worker.thread, "previousTurnId": old_turn})
        self.assertIsNone(self.service.store.get_run_internal(run["id"])["turn_id"])
        runtime.event(peer, "turn/started", {"threadId": worker.thread, "turn": {"id": "replacement-turn"}})
        runtime.event(peer, "item/agentMessage/delta", {"threadId": worker.thread, "turnId": "replacement-turn",
            "itemId": "after", "delta": "Continued answer"})
        self.assertEqual(self.service.store.get_run_internal(run["id"])["turn_id"], "replacement-turn")
        self.assertEqual(runtime.runs[run["id"]].text(), "Saved progress\n\nContinued answer")
        self.assertEqual(len(self.service.store.list_runs(self.a["device"]["id"], room["id"])), 1)
        runtime.event(peer, "turn/completed", {"threadId": worker.thread, "turn": {"id": old_turn, "status": "failed"}})
        self.assertIn(run["id"], runtime.runs)
        runtime.event(peer, "turn/completed", {"threadId": worker.thread, "turn": {"id": "replacement-turn", "status": "completed"}})
        self.assertEqual(self.service.store.get_run_internal(run["id"])["status"], "completed")

    async def test_retired_turn_cannot_finish_run_during_resume_gap(self):
        worker, _room, run, peer = await self.prepared()
        runtime = self.service.runtime
        runtime.event(peer, "lan/turnResuming", {"threadId": worker.thread, "previousTurnId": worker.turn})
        runtime.event(peer, "turn/completed", {"threadId": worker.thread,
            "turn": {"id": worker.turn, "status": "failed"}})
        self.assertIn(run["id"], runtime.runs)

    async def test_completed_message_without_stream_deltas_updates_latest_line(self):
        worker, room, run, peer = await self.prepared()
        self.service.runtime.event(peer, "item/completed", {"threadId": worker.thread, "turnId": worker.turn,
            "item": {"type": "agentMessage", "id": "completed-progress", "phase": "commentary",
                     "text": "Checked the files\nRunning the focused tests"}})
        snapshot = await self.request("GET", f"/api/rooms/{room['id']}/messages", self.a)
        active = next(value for value in snapshot["runs"] if value["id"] == run["id"])
        self.assertEqual(active["latest_status_line"], "Running the focused tests")

    async def test_tiny_response_frames_are_coalesced_without_delaying_live_text(self):
        worker, room, run, peer = await self.prepared()
        runtime = self.service.runtime
        expected = "word" * 6000
        started = time.monotonic()
        for _index in range(6000):
            runtime.event(peer, "item/agentMessage/delta", {
                "threadId": worker.thread, "turnId": worker.turn,
                "itemId": "answer", "delta": "word",
            })
        elapsed = time.monotonic() - started
        self.assertEqual(runtime.runs[run["id"]].text(), expected)
        self.assertLess(elapsed, 5.0)

        snapshot = await self.request("GET", f"/api/rooms/{room['id']}/messages", self.a)
        active = next(value for value in snapshot["runs"] if value["id"] == run["id"])
        self.assertEqual(active["output_text"], expected)
        deltas = [event for event in self.service.store.events(
            self.a["device"]["id"], room["id"], limit=1000
        ) if event["kind"] == "delta"]
        self.assertEqual("".join(event["payload"]["text"] for event in deltas), expected)
        self.assertLessEqual(len(deltas), 2)

    async def test_generated_image_becomes_private_owner_attachment_and_host_path_is_removed(self):
        worker, room, run, peer = await self.prepared()
        runtime = self.service.runtime
        source_dir = (self.service.data_dir / "remote-workers" / self.a["device"]["id"]
                      / "generated_images" / "native-thread")
        source_dir.mkdir(parents=True)
        source = source_dir / "generated.png"
        source.write_bytes(fixture.PNG)
        encoded = base64.b64encode(fixture.PNG).decode("ascii")
        item = {"type": "Extension", "kind": "image_gen.generation", "id": "image-tool-1",
                "status": "completed", "failure": None, "result": encoded,
                "savedPath": str(source)}

        async def verify_committed(_attachment, _thread_id):
            committed = self.service.store.get_run_internal(run["id"])
            self.assertIsNotNone(committed["assistant_message_id"])
            return True

        peer.ws.deliver_artifact = AsyncMock(side_effect=verify_committed)

        runtime.event(peer, "item/completed", {
            "threadId": worker.thread, "turnId": worker.turn, "item": item,
        })
        live = runtime.runs[run["id"]]
        await self.until(lambda: bool(live.generated_attachments))
        self.assertTrue(source.exists())
        runtime.event(peer, "item/completed", {
            "threadId": worker.thread, "turnId": worker.turn,
            "item": {"type": "agentMessage", "id": "answer", "phase": "final_answer",
                     "text": "Here it is: " + str(source)},
        })
        runtime.event(peer, "turn/completed", {
            "threadId": worker.thread, "turn": {"id": worker.turn, "status": "completed"},
        })
        await asyncio.wait_for(live.finished_event.wait(), 3)
        await self.until(lambda: peer.ws.deliver_artifact.await_count == 1)

        snapshot = await self.request("GET", f"/api/rooms/{room['id']}/messages", self.a)
        answer = next(message for message in snapshot["messages"] if message["role"] == "assistant")
        self.assertEqual(len(answer["attachments"]), 1)
        self.assertEqual(answer["attachments"][0]["mime"], "image/png")
        self.assertNotIn(str(self.service.data_dir), answer["content"])
        self.assertIn("attached to this response", answer["content"])
        self.assertFalse(source.exists())
        events = json.dumps(self.service.store.events(self.a["device"]["id"], room["id"]))
        self.assertNotIn(encoded, events)
        self.assertNotIn(str(source), events)

        response = await self.client.get(
            answer["attachments"][0]["url"],
            headers={"Authorization": "Bearer " + self.a["token"]},
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(await response.read(), fixture.PNG)
        denied = await self.client.get(
            answer["attachments"][0]["url"],
            headers={"Authorization": "Bearer " + self.b["token"]},
        )
        self.assertEqual(denied.status, 403)

    async def test_duplicate_or_invalid_generated_image_event_cannot_create_extra_attachment(self):
        worker, room, run, peer = await self.prepared()
        runtime = self.service.runtime
        peer.ws.deliver_artifact = AsyncMock(return_value=True)
        source_dir = (self.service.data_dir / "remote-workers" / self.a["device"]["id"]
                      / "generated_images" / "invalid")
        source_dir.mkdir(parents=True)
        invalid_source = source_dir / "invalid.png"
        invalid_source.write_bytes(fixture.PNG)
        item = {"type": "Extension", "kind": "image_gen.generation", "id": "same-image",
                "status": "completed", "failure": None,
                "result": base64.b64encode(fixture.PNG).decode("ascii")}
        payload = {"threadId": worker.thread, "turnId": worker.turn, "item": item}
        runtime.event(peer, "item/completed", payload)
        runtime.event(peer, "item/completed", payload)
        runtime.event(peer, "item/completed", {
            "threadId": worker.thread, "turnId": worker.turn,
            "item": {**item, "id": "bad-image", "result": "not base64",
                     "savedPath": str(invalid_source)},
        })
        runtime.event(peer, "turn/completed", {
            "threadId": worker.thread, "turn": {"id": worker.turn, "status": "completed"},
        })
        await asyncio.wait_for(runtime.runs[run["id"]].finished_event.wait(), 3)
        snapshot = await self.request("GET", f"/api/rooms/{room['id']}/messages", self.a)
        answer = next(message for message in snapshot["messages"] if message["role"] == "assistant")
        self.assertEqual(len(answer["attachments"]), 1)
        self.assertIn("could not be safely attached", answer["content"])
        self.assertFalse(invalid_source.exists())

    async def test_generated_image_limit_is_reserved_before_capture(self):
        worker, room, run, peer = await self.prepared()
        runtime = self.service.runtime
        peer.ws.deliver_artifact = AsyncMock(return_value=True)
        encoded = base64.b64encode(fixture.PNG).decode("ascii")
        source_dir = (self.service.data_dir / "remote-workers" / self.a["device"]["id"]
                      / "generated_images" / "limit")
        source_dir.mkdir(parents=True)
        sources = []
        for index in range(13):
            source = source_dir / f"image-{index}.png"
            source.write_bytes(fixture.PNG)
            sources.append(source)
            runtime.event(peer, "item/completed", {
                "threadId": worker.thread, "turnId": worker.turn,
                "item": {"type": "Extension", "kind": "image_gen.generation",
                         "id": f"image-{index}", "status": "completed", "failure": None,
                         "result": encoded, "savedPath": str(source)},
            })
        live = runtime.runs[run["id"]]
        self.assertEqual(len(live.generated_item_ids), 12)
        runtime.event(peer, "turn/completed", {
            "threadId": worker.thread, "turn": {"id": worker.turn, "status": "completed"},
        })
        await asyncio.wait_for(live.finished_event.wait(), 3)
        snapshot = await self.request("GET", f"/api/rooms/{room['id']}/messages", self.a)
        answer = next(message for message in snapshot["messages"] if message["role"] == "assistant")
        self.assertEqual(len(answer["attachments"]), 12)
        self.assertIn("could not be safely attached", answer["content"])
        self.assertFalse(any(source.exists() for source in sources))

    async def test_failed_generated_image_is_cleaned_and_reported(self):
        worker, room, run, peer = await self.prepared()
        runtime = self.service.runtime
        source = (self.service.data_dir / "remote-workers" / self.a["device"]["id"]
                  / "generated_images" / "failed" / "partial.png")
        source.parent.mkdir(parents=True)
        source.write_bytes(fixture.PNG)

        runtime.event(peer, "item/completed", {
            "threadId": worker.thread, "turnId": worker.turn,
            "item": {"type": "Extension", "kind": "image_gen.generation",
                     "id": "failed-image", "status": "failed",
                     "failure": "provider failed", "savedPath": str(source)},
        })
        live = runtime.runs[run["id"]]
        runtime.event(peer, "turn/completed", {
            "threadId": worker.thread,
            "turn": {"id": worker.turn, "status": "completed"},
        })
        await asyncio.wait_for(live.finished_event.wait(), 3)

        snapshot = await self.request("GET", f"/api/rooms/{room['id']}/messages", self.a)
        answer = next(message for message in snapshot["messages"] if message["role"] == "assistant")
        self.assertEqual(answer["attachments"], [])
        self.assertIn("could not be safely attached", answer["content"])
        self.assertFalse(source.exists())

    async def test_generated_source_tracking_is_bounded(self):
        live = SimpleNamespace(generated_sources={}, generated_image_error=False)
        for index in range(MAX_GENERATED_SOURCE_RECORDS + 7):
            self.service.runtime._remember_generated_source(
                live, {"savedPath": f"/untrusted/generated-{index}.png"}
            )

        self.assertEqual(len(live.generated_sources), MAX_GENERATED_SOURCE_RECORDS)
        self.assertTrue(live.generated_image_error)
        self.assertNotIn(
            f"/untrusted/generated-{MAX_GENERATED_SOURCE_RECORDS}.png",
            live.generated_sources,
        )

    async def test_source_deletion_rejects_owner_symlink_to_another_owner(self):
        data_root = self.service.data_dir / "cross-owner-boundary"
        workers_root = data_root / "remote-workers"
        owner_a, owner_b = str(uuid.uuid4()), str(uuid.uuid4())
        owner_b_root = workers_root / owner_b
        generated = owner_b_root / "generated_images"
        generated.mkdir(parents=True)
        victim = generated / "owner-b.png"
        victim.write_bytes(fixture.PNG)
        (workers_root / owner_a).symlink_to(owner_b_root, target_is_directory=True)
        runtime = Runtime(SimpleNamespace(data_dir=data_root), None)

        removed = runtime._remove_generated_source(
            owner_a, {"path": str(workers_root / owner_a / "generated_images" / victim.name)}
        )

        self.assertIsNone(removed)
        self.assertTrue(victim.exists())

    async def test_source_cleanup_rejects_symlinked_trust_boundaries(self):
        owner_id = str(uuid.uuid4())
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            cases = []

            real_data = base / "real-data"
            victim = real_data / "remote-workers" / owner_id / "generated_images" / "data.png"
            victim.parent.mkdir(parents=True)
            victim.write_bytes(fixture.PNG)
            data_link = base / "data-link"
            data_link.symlink_to(real_data, target_is_directory=True)
            cases.append(("data", data_link, victim,
                          data_link / "remote-workers" / owner_id / "generated_images" / victim.name))

            real_parent = base / "real-parent"
            victim = real_parent / "data" / "remote-workers" / owner_id / "generated_images" / "ancestor.png"
            victim.parent.mkdir(parents=True)
            victim.write_bytes(fixture.PNG)
            linked_parent = base / "linked-parent"
            linked_parent.symlink_to(real_parent, target_is_directory=True)
            ancestor_data = linked_parent / "data"
            cases.append(("data-ancestor", ancestor_data, victim,
                          ancestor_data / "remote-workers" / owner_id / "generated_images" / victim.name))

            workers_data = base / "workers-data"
            workers_data.mkdir()
            external_workers = base / "external-workers"
            victim = external_workers / owner_id / "generated_images" / "workers.png"
            victim.parent.mkdir(parents=True)
            victim.write_bytes(fixture.PNG)
            (workers_data / "remote-workers").symlink_to(external_workers, target_is_directory=True)
            cases.append(("remote-workers", workers_data, victim,
                          workers_data / "remote-workers" / owner_id / "generated_images" / victim.name))

            owner_data = base / "owner-data"
            workers_root = owner_data / "remote-workers"
            workers_root.mkdir(parents=True)
            external_owner = base / "external-owner"
            victim = external_owner / "generated_images" / "owner.png"
            victim.parent.mkdir(parents=True)
            victim.write_bytes(fixture.PNG)
            (workers_root / owner_id).symlink_to(external_owner, target_is_directory=True)
            cases.append(("owner", owner_data, victim,
                          workers_root / owner_id / "generated_images" / victim.name))

            generated_data = base / "generated-data"
            owner_root = generated_data / "remote-workers" / owner_id
            owner_root.mkdir(parents=True)
            external_generated = base / "external-generated"
            external_generated.mkdir()
            victim = external_generated / "generated.png"
            victim.write_bytes(fixture.PNG)
            (owner_root / "generated_images").symlink_to(
                external_generated, target_is_directory=True
            )
            cases.append(("generated_images", generated_data, victim,
                          owner_root / "generated_images" / victim.name))

            for boundary, data_root, victim, source in cases:
                with self.subTest(boundary=boundary):
                    runtime = Runtime(SimpleNamespace(data_dir=data_root), None)
                    self.assertIsNone(runtime._remove_generated_source(
                        owner_id, {"path": str(source)}
                    ))
                    self.assertEqual(runtime._cleanup_stale_generated_sources_sync(), 0)
                    self.assertTrue(victim.exists())

    async def test_startup_sweep_removes_only_scoped_stale_generated_files(self):
        device_root = self.service.data_dir / "remote-workers" / self.a["device"]["id"]
        generated = device_root / "generated_images" / "old-thread"
        generated.mkdir(parents=True)
        stale = generated / "stale.png"
        stale.write_bytes(fixture.PNG)
        outside = device_root / "host-workspace" / "keep.png"
        outside.parent.mkdir(parents=True)
        outside.write_bytes(fixture.PNG)
        target = self.service.data_dir / "keep-target.png"
        target.write_bytes(fixture.PNG)
        link = generated / "keep-link.png"
        link.symlink_to(target)

        removed = await self.service.runtime.cleanup_stale_generated_sources()

        self.assertEqual(removed, 1)
        self.assertFalse(stale.exists())
        self.assertTrue(link.is_symlink())
        self.assertTrue(target.exists())
        self.assertTrue(outside.exists())

    async def test_host_path_is_redacted_when_split_across_stream_deltas(self):
        worker, room, _run, peer = await self.prepared()
        runtime = self.service.runtime
        path = str(self.service.data_dir / "remote-workers" / self.a["device"]["id"]
                   / "generated_images" / "thread" / "image.png")
        split = path.index("remote-workers") + 6
        for delta in ("Saved at " + path[:split], path[split:] + "\nReady"):
            runtime.event(peer, "item/agentMessage/delta", {
                "threadId": worker.thread, "turnId": worker.turn,
                "itemId": "answer", "delta": delta,
            })
        runtime.event(peer, "item/completed", {
            "threadId": worker.thread, "turnId": worker.turn,
            "item": {"type": "agentMessage", "id": "answer", "phase": "final_answer",
                     "text": "Saved at " + path + "\nReady"},
        })
        runtime.event(peer, "turn/completed", {
            "threadId": worker.thread, "turn": {"id": worker.turn, "status": "completed"},
        })

        snapshot = await self.request("GET", f"/api/rooms/{room['id']}/messages", self.a)
        answer = next(message for message in snapshot["messages"] if message["role"] == "assistant")
        self.assertEqual(answer["content"], "Saved at the generated image attached to this response\nReady")
        events = json.dumps(self.service.store.events(self.a["device"]["id"], room["id"]))
        self.assertNotIn(str(self.service.data_dir), events)
        self.assertNotIn("remote-workers", events)


class ManagedConnectorFailureTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temporary.name))
        enrolled = self.store.bootstrap_admin("Server")
        self.device_id = enrolled["device"]["id"]
        self.room = self.store.create_room(self.device_id, "Room", "/work", model="test-model")
        self.runtime = Runtime(self.store, None)
        self.socket = SimpleNamespace(closed=False, close=AsyncMock())
        self.peer = Peer(self.device_id, self.socket, ready=True, models=[{
            "id": "test-model", "isDefault": True, "supportedReasoningEfforts": []
        }], execution_mode="server")
        self.runtime.peers[self.device_id] = self.peer

    async def asyncTearDown(self):
        await self.runtime.close()
        self.store.close()
        self.temporary.cleanup()

    async def test_unexpected_managed_connector_error_is_safe_terminal_and_runtime_recovers(self):
        secret = "RPC-INTERNAL-SECRET"
        created = self.store.create_run(self.device_id, self.room["id"], "hello", [], "first")
        self.peer.call = AsyncMock(side_effect=RuntimeError(secret))
        self.runtime.start(created["run"], self.room, "hello", [])
        live = self.runtime.runs[created["run"]["id"]]
        await asyncio.wait_for(live.task, 0.5)

        failed = self.store.get_run_internal(created["run"]["id"])
        self.assertEqual(failed["status"], "failed")
        self.assertNotIn(created["run"]["id"], self.runtime.runs)
        self.assertNotIn(secret, json.dumps(self.store.events(self.device_id, self.room["id"])))
        self.assertIn("connector", failed["error"].lower())
        self.assertIsNone(live.task.exception())

        second = self.store.create_run(self.device_id, self.room["id"], "again", [], "second")
        thread_id = "healthy-thread"
        self.peer.call = AsyncMock(side_effect=[{"thread": {"id": thread_id}}, {"turn": {"id": "healthy-turn"}}])
        self.runtime.start(second["run"], self.room, "again", [])
        next_live = self.runtime.runs[second["run"]["id"]]
        await asyncio.wait_for(next_live.task, 0.5)
        self.assertEqual(self.store.get_run_internal(second["run"]["id"])["status"], "running")
        self.assertIs(self.runtime.by_thread[(self.device_id, thread_id)], next_live)
        self.runtime.finish(next_live, "cancelled")


if __name__ == "__main__":
    unittest.main()
