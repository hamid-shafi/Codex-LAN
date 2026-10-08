"""CODEX: Per-turn speed is explicit, model-safe, and immutable for usage and steering."""

import unittest

import test_lan_server as fixture


class FastApiTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.LanServerTests.asyncSetUp
    asyncTearDown = fixture.LanServerTests.asyncTearDown
    request = fixture.LanServerTests.request
    until = fixture.LanServerTests.until
    worker = fixture.LanServerTests.worker
    room = fixture.LanServerTests.room
    submit_run = fixture.LanServerTests.submit_run
    ready_run = fixture.LanServerTests.ready_run

    async def supported_worker(self):
        worker = await self.worker(self.a, "fast")
        peer = self.service.runtime.peers[self.a["device"]["id"]]
        peer.models = [{**fixture.MODELS[0], "id": name, "model": name} for name in
                       ("gpt-5.6-sol", "gpt-5.4-mini", "astra")]
        return worker

    async def test_explicit_standard_overrides_inherited_fast_and_snapshots_actual_model(self):
        worker = await self.supported_worker()
        room = await self.room(self.a, model="", effort="")
        created = await self.ready_run(self.a, worker, room)
        params = next(call["params"] for call in worker.calls if call["method"] == "turn/start")
        self.assertEqual(params["serviceTierForTurn"], "default")
        self.assertNotIn("serviceTier", params)
        run = self.service.store.get_run_internal(created["run"]["id"])
        self.assertEqual((run["model"], run["effort"], run["speed"]), ("gpt-5.6-sol", "medium", "standard"))

    async def test_fast_pricing_uses_immutable_run_configuration(self):
        worker = await self.supported_worker()
        room = await self.room(self.a, model="gpt-5.6-sol", speed="fast")
        created = await self.ready_run(self.a, worker, room)
        params = next(call["params"] for call in worker.calls if call["method"] == "turn/start")
        self.assertEqual(params["serviceTierForTurn"], "fast")
        await self.request("PATCH", f"/api/rooms/{room['id']}", self.a,
                           json={"model": "gpt-5.4-mini", "effort": "low"})
        await worker.usage(1000, 100, cached=200)
        run_id = created["run"]["id"]
        await self.until(lambda: self.service.store.db.execute("SELECT 1 FROM quota_usage WHERE run_id=?", (run_id,)).fetchone())
        estimate = self.service.store.db.execute("SELECT credits_micros FROM quota_usage WHERE run_id=?", (run_id,)).fetchone()[0]
        self.assertEqual(estimate, (800 * 100 + 200 * 10 + 100 * 500) * 2.5)
        self.assertEqual(self.service.store.get_run_internal(run_id)["speed"], "fast")
        await worker.complete()
        await self.until(lambda: run_id not in self.service.runtime.runs)
        messages = await self.request("GET", f"/api/rooms/{room['id']}/messages", self.a)
        self.assertEqual(messages["messages"][-1]["estimated_usage_credits_micros"], estimate)
        await self.ready_run(self.a, worker, messages["room"], request_id="second")
        turns = [call["params"] for call in worker.calls if call["method"] == "turn/start"]
        self.assertEqual(turns[-1]["serviceTierForTurn"], "default")
        self.assertEqual(turns[-1]["model"], "gpt-5.4-mini")

    async def test_model_metadata_and_invalid_speed_fail_before_admission(self):
        await self.supported_worker()
        models = (await self.request("GET", "/api/models", self.a))["models"]
        self.assertTrue(models[0]["supportsFast"])
        self.assertEqual(models[0]["fastUsageMultiplier"], 2.5)
        self.assertFalse(models[1]["supportsFast"])
        for speed in ("fast", "ultra", None, True):
            await self.request("POST", "/api/rooms", self.a, expected=400,
                               json={"cwd": "/home/project", "model": "gpt-5.4-mini", "speed": speed})
        room = await self.room(self.a, model="gpt-5.6-sol", speed="fast")
        changed = await self.request("PATCH", f"/api/rooms/{room['id']}", self.a,
                                     json={"model": "gpt-5.4-mini"})
        self.assertEqual(changed["speed"], "standard")
        await self.request("PATCH", f"/api/rooms/{room['id']}", self.a, expected=400, json={"speed": "fast"})

    async def test_existing_receipt_survives_offline_worker_and_zero_share(self):
        worker = await self.supported_worker()
        room = await self.room(self.a, model="gpt-5.6-sol", speed="fast")
        original = await self.ready_run(self.a, worker, room)
        await worker.complete()
        await self.until(lambda: original["run"]["id"] not in self.service.runtime.runs)
        self.service.store.set_device(self.admin_id, self.a["device"]["id"], weekly_share_percent=0)
        await worker.close()
        self.workers.remove(worker)
        await self.until(lambda: not self.service.runtime.connected(self.a["device"]["id"]))
        replay = await self.submit_run(self.a, room)
        self.assertTrue(replay["deduplicated"])
        self.assertEqual(replay["run"]["id"], original["run"]["id"])
        self.assertEqual(len([call for call in worker.calls if call["method"] == "turn/start"]), 1)
        await self.request("POST", f"/api/rooms/{room['id']}/runs", self.a, expected=400,
                           json={"text": "different content", "request_id": "one"})


if __name__ == "__main__":
    unittest.main()
