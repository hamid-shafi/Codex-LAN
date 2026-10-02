"""CODEX: Activity history remains authorized, durable, and cheap to poll."""

import unittest

import test_lan_server as fixture
from codex_lan.runtime import Runtime


class LanActivityHTTPTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.LanServerTests.asyncSetUp
    asyncTearDown = fixture.LanServerTests.asyncTearDown
    request = fixture.LanServerTests.request
    until = fixture.LanServerTests.until
    worker = fixture.LanServerTests.worker
    room = fixture.LanServerTests.room
    submit_run = fixture.LanServerTests.submit_run
    ready_run = fixture.LanServerTests.ready_run

    async def recorded_command(self):
        worker = await self.worker(self.a, "activity")
        room = await self.room(self.a)
        created = await self.ready_run(self.a, worker, room)
        run_id = created["run"]["id"]
        item = {"id": "test-command", "type": "commandExecution", "command": "pytest -q",
                "cwd": "/home/project", "status": "inProgress"}
        await worker.event("item/started", item=item)
        await worker.event("item/commandExecution/outputDelta", itemId=item["id"], delta="3 passed\n")
        await worker.event("item/completed", item={**item, "status": "completed", "aggregatedOutput": "3 passed\n",
                                                   "exitCode": 0, "durationMs": 1234})
        await worker.delta("Checks finished.")
        await worker.complete()
        await self.until(lambda: self.service.store.get_run_internal(run_id)["status"] == "completed")
        return room, run_id

    async def test_activity_history_survives_reload_and_is_private(self):
        room, run_id = await self.recorded_command()
        data = await self.request("GET", f"/api/rooms/{room['id']}/messages", self.a)
        answer = next(message for message in data["messages"] if message["role"] == "assistant")
        entry = next(entry for entry in answer["activities"] if entry["id"] == "test-command")
        self.assertIn("pytest", entry["title"] + entry.get("detail", ""))
        self.assertEqual(entry["status"], "completed")
        self.assertEqual(entry["exit_code"], 0)
        self.assertEqual(entry["output"], "3 passed\n")
        await self.request("GET", f"/api/rooms/{room['id']}/messages", self.b, expected=403)
        await self.request("GET", f"/api/rooms/{room['id']}/events", self.b, expected=403)
        await self.request("GET", f"/api/rooms/{room['id']}/messages", self.admin)
        fresh = Runtime(self.service.store, fixture.FakeAuth())
        self.assertEqual(fresh.activity_snapshot(run_id)["activities"], answer["activities"])

    async def test_idle_poll_does_not_resend_all_terminal_output(self):
        room, run_id = await self.recorded_command()
        data = await self.request("GET", f"/api/rooms/{room['id']}/messages", self.a)
        polled = await self.request("GET", f"/api/rooms/{room['id']}/events?after={data['event_cursor']}", self.a)
        self.assertEqual(polled["events"], [])
        run = next(run for run in polled["runs"] if run["id"] == run_id)
        self.assertEqual(run["status"], "completed")
        self.assertNotIn("activities", run)

    async def test_old_paginated_message_keeps_log_after_latest_run_limit(self):
        room, run_id = await self.recorded_command()
        for index in range(102):
            created = self.service.store.create_run(self.a["device"]["id"], room["id"], "Later", [], f"later-{index}")
            self.service.store.finish_run(created["run"]["id"], "completed", "Done")
        snapshots = self.service.runtime.snapshots(self.a["device"]["id"], room["id"], include_completed_activity=False)
        self.assertNotIn(run_id, [run["id"] for run in snapshots])
        before = ""
        seen = None
        for _ in range(6):
            suffix = f"?before={before}" if before else ""
            page = await self.request("GET", f"/api/rooms/{room['id']}/messages{suffix}", self.a)
            seen = next((message for message in page["messages"] if message["run_id"] == run_id and message["role"] == "assistant"), None)
            if seen or not page["messages"]:
                break
            before = page["messages"][0]["id"]
        self.assertIsNotNone(seen)
        self.assertTrue(any(entry.get("output") == "3 passed\n" for entry in seen["activities"]))


if __name__ == "__main__":
    unittest.main()
