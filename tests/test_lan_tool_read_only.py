"""CODEX: Enforce and preserve per-run tool read-only execution policy."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import test_lan_server as fixture

from codex_lan.store import Store


class ToolReadOnlyStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temporary.name))
        enrolled = self.store.bootstrap_admin("Server")
        self.device_id = enrolled["device"]["id"]
        self.room = self.store.create_room(self.device_id, "Room", "/work")

    def tearDown(self):
        self.store.close()
        self.temporary.cleanup()

    def test_boolean_validation_snapshot_and_request_fingerprint(self):
        self.assertIs(self.room["tool_read_only"], False)
        inherited_default = self.store.create_run(
            self.device_id, self.room["id"], "default", [], "default-request"
        )
        self.assertIs(inherited_default["run"]["tool_read_only"], False)
        self.store.finish_run(
            inherited_default["run"]["id"], "completed", "done"
        )
        for invalid in (None, 0, 1, "true", [], {}):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    self.store.update_room(
                        self.device_id, self.room["id"], tool_read_only=invalid
                    )

        room = self.store.update_room(
            self.device_id, self.room["id"], tool_read_only=True
        )
        self.assertIs(room["tool_read_only"], True)
        for invalid in (0, 1, "true", [], {}):
            with self.subTest(run_invalid=invalid):
                with self.assertRaises(ValueError):
                    self.store.create_run(
                        self.device_id,
                        room["id"],
                        "invalid",
                        [],
                        f"invalid-{type(invalid).__name__}-{repr(invalid)}",
                        invalid,
                    )

        created = self.store.create_run(
            self.device_id, room["id"], "inspect", [], "read-only-request", True
        )
        self.assertIs(created["run"]["tool_read_only"], True)
        replay = self.store.create_run(
            self.device_id, room["id"], "inspect", [], "read-only-request", True
        )
        self.assertTrue(replay["deduplicated"])
        self.assertEqual(replay["run"]["id"], created["run"]["id"])
        with self.assertRaises(ValueError):
            self.store.create_run(
                self.device_id, room["id"], "inspect", [], "read-only-request", False
            )

        self.store.update_room(self.device_id, room["id"], tool_read_only=False)
        persisted = self.store.get_run_internal(created["run"]["id"])
        self.assertIs(persisted["tool_read_only"], True)
        self.store.close()
        self.store = Store(Path(self.temporary.name))
        self.assertIs(
            self.store.get_run_internal(created["run"]["id"])["tool_read_only"], True
        )


class ToolReadOnlyApiAndRuntimeTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.LanServerTests.asyncSetUp
    asyncTearDown = fixture.LanServerTests.asyncTearDown
    request = fixture.LanServerTests.request
    until = fixture.LanServerTests.until
    worker = fixture.LanServerTests.worker
    room = fixture.LanServerTests.room
    submit_run = fixture.LanServerTests.submit_run
    ready_run = fixture.LanServerTests.ready_run

    async def test_http_requires_real_booleans_for_rooms_and_runs(self):
        default_room = await self.room(self.a)
        self.assertIs(default_room["tool_read_only"], False)
        for index, invalid in enumerate((None, 0, 1, "true", [], {})):
            with self.subTest(create_room=invalid):
                await self.request(
                    "POST",
                    "/api/rooms",
                    self.a,
                    expected=400,
                    json={
                        "title": f"Invalid room {index}",
                        "cwd": "/work",
                        "model": "astra",
                        "effort": "ultra",
                        "tool_read_only": invalid,
                    },
                )

        room = await self.room(self.a, tool_read_only=True)
        self.assertIs(room["tool_read_only"], True)
        for invalid in (None, 0, 1, "true", [], {}):
            with self.subTest(update_room=invalid):
                await self.request(
                    "PATCH",
                    f"/api/rooms/{room['id']}",
                    self.a,
                    expected=400,
                    json={"tool_read_only": invalid},
                )

        await self.worker(self.a, "validation")
        for index, invalid in enumerate((None, 0, 1, "true", [], {})):
            with self.subTest(create_run=invalid):
                await self.request(
                    "POST",
                    f"/api/rooms/{room['id']}/runs",
                    self.a,
                    expected=400,
                    json={
                        "text": "inspect",
                        "request_id": f"invalid-mode-{index}",
                        "tool_read_only": invalid,
                    },
                )

    async def test_runtime_uses_snapshotted_policy_and_restores_full_access(self):
        worker = await self.worker(self.a, "policy")
        room = await self.room(self.a, tool_read_only=True)
        read_only = await self.ready_run(
            self.a, worker, room, request_id="read-only", tool_read_only=True
        )
        self.assertIs(read_only["run"]["tool_read_only"], True)

        replay = await self.submit_run(
            self.a,
            room,
            text="hello",
            request_id="read-only",
            tool_read_only=True,
        )
        self.assertEqual(replay["run"]["id"], read_only["run"]["id"])
        self.assertTrue(replay["deduplicated"])
        await self.request(
            "POST",
            f"/api/rooms/{room['id']}/runs",
            self.a,
            expected=400,
            json={
                "text": "hello",
                "request_id": "read-only",
                "tool_read_only": False,
            },
        )

        starts = [call for call in worker.calls if call["method"] == "thread/start"]
        turns = [call for call in worker.calls if call["method"] == "turn/start"]
        self.assertEqual(starts[-1]["params"]["sandbox"], "read-only")
        self.assertEqual(starts[-1]["params"]["approvalPolicy"], "never")
        self.assertEqual(
            turns[-1]["params"]["sandboxPolicy"],
            {"type": "readOnly", "networkAccess": False},
        )
        self.assertEqual(turns[-1]["params"]["approvalPolicy"], "never")

        changed = await self.request(
            "PATCH",
            f"/api/rooms/{room['id']}",
            self.a,
            json={"tool_read_only": False},
        )
        self.assertIs(changed["tool_read_only"], False)
        self.assertIs(
            self.service.store.get_run_internal(read_only["run"]["id"])[
                "tool_read_only"
            ],
            True,
        )
        await worker.complete()
        await self.until(
            lambda: read_only["run"]["id"] not in self.service.runtime.runs
        )

        full_access = await self.ready_run(
            self.a, worker, room, request_id="full-access", tool_read_only=False
        )
        self.assertIs(full_access["run"]["tool_read_only"], False)
        resumes = [call for call in worker.calls if call["method"] == "thread/resume"]
        turns = [call for call in worker.calls if call["method"] == "turn/start"]
        self.assertEqual(resumes[-1]["params"]["sandbox"], "danger-full-access")
        self.assertEqual(resumes[-1]["params"]["approvalPolicy"], "never")
        self.assertEqual(
            turns[-1]["params"]["sandboxPolicy"], {"type": "dangerFullAccess"}
        )
        self.assertEqual(turns[-1]["params"]["approvalPolicy"], "never")
        self.assertIs(
            self.service.store.get_run_internal(read_only["run"]["id"])[
                "tool_read_only"
            ],
            True,
        )
        await worker.complete()
        await self.until(
            lambda: full_access["run"]["id"] not in self.service.runtime.runs
        )


if __name__ == "__main__":
    unittest.main()
