"""CODEX: Read-only steering contracts, secure mode handoff, and browser payload coverage."""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path

from aiohttp import WSMsgType
from playwright.async_api import expect

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from project_layout import configure_imports, source_file
configure_imports(__file__)

import test_lan_browser as browser_fixture
import test_lan_server as server_fixture
from codex_lan.runtime import access_mode_continuation
from codex_lan.store import Store
from test_lan_server import MODELS, host_worker_socket


class ModeAwareWorker(server_fixture.FakeWorker):
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
            elif method == "turn/steer":
                result = {"turnId": self.turn}
            elif method == "lan/upload":
                result = {"path": "/fake/client/update.png"}
            else:
                result = {}
            await self.ws.send_json(
                {"type": "result", "id": packet["id"], "result": result}
            )


class ReadOnlySteeringStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temporary.name))
        self.device_id = self.store.bootstrap_admin("Host")["device"]["id"]
        self.room = self.store.create_room(
            self.device_id, "Room", "/work", tool_read_only=False
        )
        self.run = self.store.create_run(
            self.device_id,
            self.room["id"],
            "Initial request",
            [],
            "initial-request",
            False,
        )["run"]

    def tearDown(self):
        self.store.close()
        self.temporary.cleanup()

    def prepare(self, request_id, tool_read_only):
        return self.store.prepare_steering(
            self.device_id,
            self.room["id"],
            self.run["id"],
            "Follow-up",
            [],
            request_id,
            tool_read_only=tool_read_only,
        )

    def test_receipt_persists_strict_mode_and_mode_is_part_of_idempotency(self):
        for index, invalid in enumerate((None, 0, 1, "true", [], {})):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    self.prepare(f"invalid-{index}", invalid)

        receipt, created = self.prepare("same-mode", False)
        self.assertTrue(created)
        self.assertIs(receipt["tool_read_only"], False)
        self.assertIsNone(receipt["replacement_run_id"])
        replay, replay_created = self.prepare("same-mode", False)
        self.assertFalse(replay_created)
        self.assertEqual(replay["id"], receipt["id"])
        with self.assertRaises(ValueError):
            self.prepare("same-mode", True)

        transition, transition_created = self.prepare("mode-transition", True)
        self.assertTrue(transition_created)
        self.assertIs(transition["tool_read_only"], True)
        unresolved = {
            item["id"]: item
            for item in self.store.unresolved_steering(
                self.device_id, self.room["id"]
            )
        }
        self.assertIs(unresolved[transition["id"]]["tool_read_only"], True)
        self.store.finish_steering(transition["id"], "sending")
        self.store.finish_steering(
            transition["id"], "uncertain", "Delivery was not confirmed"
        )
        self.assertIs(
            self.store.get_run_internal(self.run["id"])["tool_read_only"], False
        )

        self.store.close()
        self.store = Store(Path(self.temporary.name))
        persisted = self.store.steering_result(transition["id"])["steering"]
        self.assertIs(persisted["tool_read_only"], True)
        self.assertIs(
            self.store.get_run_internal(self.run["id"])["tool_read_only"], False
        )

    def test_follow_up_request_id_cannot_alias_an_existing_prompt(self):
        with self.assertRaisesRegex(ValueError, "another prompt"):
            self.prepare("initial-request", True)
        count = self.store.db.execute(
            "SELECT COUNT(*) FROM steering_receipts"
        ).fetchone()[0]
        self.assertEqual(count, 0)


class ReadOnlySteeringApiRuntimeTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = server_fixture.LanServerTests.asyncSetUp
    asyncTearDown = server_fixture.LanServerTests.asyncTearDown
    request = server_fixture.LanServerTests.request
    until = server_fixture.LanServerTests.until
    room = server_fixture.LanServerTests.room
    submit_run = server_fixture.LanServerTests.submit_run
    ready_run = server_fixture.LanServerTests.ready_run

    async def mode_worker(self, device, name):
        ws, auth = await host_worker_socket(
            self.client, self.service, self.admin, device
        )
        self.assertEqual(auth["type"], "auth")
        await ws.send_json(
            {"type": "ready", "models": MODELS, "cwd": "/home/" + name}
        )
        worker = ModeAwareWorker(ws, name)
        self.workers.append(worker)
        await self.until(
            lambda: self.service.runtime.connected(device["device"]["id"])
        )
        return worker

    async def setup_turn(self, tool_read_only=False):
        worker = await self.mode_worker(
            self.a, "read-only" if tool_read_only else "full-access"
        )
        room = await self.room(self.a, tool_read_only=tool_read_only)
        created = await self.ready_run(
            self.a,
            worker,
            room,
            request_id="initial-mode",
            tool_read_only=tool_read_only,
        )
        return worker, room, created

    async def post_steer(
        self, room, run_id, request_id, tool_read_only,
        text="Change the access mode safely", expected=202,
    ):
        return await self.request(
            "POST",
            f"/api/rooms/{room['id']}/steer",
            self.a,
            expected=expected,
            json={
                "run_id": run_id,
                "request_id": request_id,
                "text": text,
                "tool_read_only": tool_read_only,
            },
        )

    async def test_server_requires_an_explicit_real_boolean_before_dispatch(self):
        worker, room, created = await self.setup_turn(False)
        base = {
            "run_id": created["run"]["id"],
            "request_id": "strict-mode",
            "text": "Inspect this request",
        }
        invalid_values = (None, 0, 1, "true", [], {})
        payloads = [dict(base)] + [
            {**base, "request_id": f"strict-mode-{index}", "tool_read_only": value}
            for index, value in enumerate(invalid_values)
        ]
        for payload in payloads:
            with self.subTest(payload=payload):
                result = await self.request(
                    "POST",
                    f"/api/rooms/{room['id']}/steer",
                    self.a,
                    expected=400,
                    json=payload,
                )
                self.assertIn("read-only", result["error"].lower())

        methods = [call["method"] for call in worker.calls]
        self.assertNotIn("turn/steer", methods)
        self.assertNotIn("turn/interrupt", methods)
        count = self.service.store.db.execute(
            "SELECT COUNT(*) FROM steering_receipts"
        ).fetchone()[0]
        self.assertEqual(count, 0)
        self.assertIs(
            self.service.store.get_run_internal(created["run"]["id"])[
                "tool_read_only"
            ],
            False,
        )

    async def test_same_mode_update_uses_native_steer_without_policy_overrides(self):
        worker, room, created = await self.setup_turn(False)
        result = await self.post_steer(
            room, created["run"]["id"], "same-mode-update", False
        )
        self.assertTrue(result["steered"])
        self.assertIs(result["mode_changed"], False)
        self.assertIs(result["steering"]["tool_read_only"], False)
        self.assertIsNone(result["steering"]["replacement_run_id"])
        steer_calls = [
            call for call in worker.calls if call["method"] == "turn/steer"
        ]
        self.assertEqual(len(steer_calls), 1)
        self.assertEqual(
            set(steer_calls[0]["params"]),
            {"threadId", "expectedTurnId", "input"},
        )
        self.assertFalse(
            any(call["method"] == "turn/interrupt" for call in worker.calls)
        )
        self.assertIs(
            self.service.store.get_run_internal(created["run"]["id"])[
                "tool_read_only"
            ],
            False,
        )

    async def test_same_mode_update_without_content_remains_rejected(self):
        worker, room, created = await self.setup_turn(False)
        result = await self.post_steer(
            room,
            created["run"]["id"],
            "empty-same-mode",
            False,
            text="",
            expected=400,
        )
        self.assertIn("empty", result["error"].lower())
        self.assertFalse(
            any(
                call["method"] in {"turn/steer", "turn/interrupt"}
                for call in worker.calls
            )
        )
        count = self.service.store.db.execute(
            "SELECT COUNT(*) FROM steering_receipts WHERE request_id=?",
            ("empty-same-mode",),
        ).fetchone()[0]
        self.assertEqual(count, 0)

    async def assert_secure_mode_handoff(self, initial, requested, text=""):
        worker, room, created = await self.setup_turn(initial)
        old_run_id = created["run"]["id"]
        expected_text = text or access_mode_continuation(requested)
        request_task = asyncio.create_task(
            self.post_steer(
                room,
                old_run_id,
                f"handoff-{int(initial)}-{int(requested)}",
                requested,
                text=text,
            )
        )
        await self.until(
            lambda: any(
                call["method"] == "turn/interrupt" for call in worker.calls
            )
        )
        interrupts = [
            call for call in worker.calls if call["method"] == "turn/interrupt"
        ]
        self.assertEqual(len(interrupts), 1)
        self.assertEqual(
            interrupts[0]["params"],
            {"threadId": worker.thread, "turnId": worker.turn},
        )
        starts_before_completion = [
            call for call in worker.calls if call["method"] == "turn/start"
        ]
        self.assertEqual(len(starts_before_completion), 1)
        self.assertEqual(
            len(
                self.service.store.list_runs(
                    self.a["device"]["id"], room["id"]
                )
            ),
            1,
        )
        self.assertIs(
            self.service.store.get_run_internal(old_run_id)["tool_read_only"],
            initial,
        )

        await worker.complete("interrupted")
        result = await asyncio.wait_for(request_task, timeout=3)
        await self.until(
            lambda: len(
                [call for call in worker.calls if call["method"] == "turn/start"]
            )
            == 2
        )
        runs = self.service.store.list_runs(
            self.a["device"]["id"], room["id"]
        )
        self.assertEqual(len(runs), 2)
        old_run = next(run for run in runs if run["id"] == old_run_id)
        new_run = next(run for run in runs if run["id"] != old_run_id)
        self.assertIs(result["mode_changed"], True)
        self.assertIs(result["steered"], False)
        self.assertEqual(result["previous_run_id"], old_run_id)
        self.assertEqual(result["run"]["id"], new_run["id"])
        self.assertIs(result["run"]["tool_read_only"], requested)
        self.assertEqual(result["steering"]["run_id"], old_run_id)
        self.assertEqual(
            result["steering"]["replacement_run_id"], new_run["id"]
        )
        self.assertIs(result["steering"]["tool_read_only"], requested)
        self.assertIs(old_run["tool_read_only"], initial)
        self.assertIn(old_run["status"], {"cancelled", "interrupted"})
        self.assertIs(new_run["tool_read_only"], requested)
        self.assertEqual(new_run["thread_id"], old_run["thread_id"])
        self.assertFalse(
            any(call["method"] == "turn/steer" for call in worker.calls)
        )

        starts = [call for call in worker.calls if call["method"] == "turn/start"]
        expected_policy = (
            {"type": "readOnly", "networkAccess": False}
            if requested
            else {"type": "dangerFullAccess"}
        )
        self.assertEqual(starts[-1]["params"]["sandboxPolicy"], expected_policy)
        self.assertEqual(starts[-1]["params"]["approvalPolicy"], "never")
        self.assertEqual(
            starts[-1]["params"]["input"],
            [{"type": "text", "text": expected_text}],
        )
        resumes = [
            call for call in worker.calls if call["method"] == "thread/resume"
        ]
        self.assertEqual(
            resumes[-1]["params"]["sandbox"],
            "read-only" if requested else "danger-full-access",
        )
        messages = self.service.store.list_messages(
            self.a["device"]["id"], room["id"], limit=20
        )
        self.assertTrue(
            any(
                message["run_id"] == new_run["id"]
                and message["role"] == "user"
                and message["content"] == expected_text
                for message in messages
            )
        )
        self.assertEqual(result["user_message"]["run_id"], new_run["id"])

        replay = await self.post_steer(
            room,
            old_run_id,
            f"handoff-{int(initial)}-{int(requested)}",
            requested,
            text=text,
            expected=200,
        )
        self.assertTrue(replay["deduplicated"])
        self.assertIs(replay["mode_changed"], True)
        self.assertEqual(replay["run"]["id"], new_run["id"])
        self.assertEqual(
            len([call for call in worker.calls if call["method"] == "turn/start"]),
            2,
        )

    async def test_full_access_to_read_only_starts_a_new_sandboxed_turn(self):
        await self.assert_secure_mode_handoff(False, True)

    async def test_read_only_to_full_access_starts_a_new_full_access_turn(self):
        await self.assert_secure_mode_handoff(True, False)

    async def test_failed_handoff_never_claims_the_requested_mode(self):
        worker, room, created = await self.setup_turn(False)
        old_run_id = created["run"]["id"]
        live = self.service.runtime.runs[old_run_id]
        original = live.peer.call

        async def fail_interrupt(method, params, timeout=45):
            if method == "turn/interrupt":
                raise asyncio.TimeoutError()
            return await original(method, params, timeout)

        live.peer.call = fail_interrupt
        response = await self.client.post(
            f"/api/rooms/{room['id']}/steer",
            headers={"Authorization": "Bearer " + self.a["token"]},
            json={
                "run_id": old_run_id,
                "request_id": "failed-mode-handoff",
                "text": "Do not weaken the policy",
                "tool_read_only": True,
            },
        )
        await response.json()
        self.assertIn(response.status, {409, 503})
        self.assertEqual(
            len(
                self.service.store.list_runs(
                    self.a["device"]["id"], room["id"]
                )
            ),
            1,
        )
        self.assertIs(
            self.service.store.get_run_internal(old_run_id)["tool_read_only"],
            False,
        )
        self.assertEqual(
            len([call for call in worker.calls if call["method"] == "turn/start"]),
            1,
        )
        receipt = self.service.store.db.execute(
            "SELECT * FROM steering_receipts WHERE request_id=?",
            ("failed-mode-handoff",),
        ).fetchone()
        self.assertIsNotNone(receipt)
        self.assertIn(receipt["status"], {"rejected", "uncertain"})
        self.assertIsNone(receipt["replacement_run_id"])


class ReadOnlySteeringBrowserTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = browser_fixture.LanBrowserTests.asyncSetUp
    asyncTearDown = browser_fixture.LanBrowserTests.asyncTearDown
    login = browser_fixture.LanBrowserTests.login
    wait_worker_turn = browser_fixture.LanBrowserTests.wait_worker_turn

    async def start_worker(self, device, name):
        ws, _auth = await host_worker_socket(
            self.http, self.service, self.admin, device
        )
        await ws.send_json(
            {
                "type": "ready",
                "models": browser_fixture.BROWSER_MODELS,
                "cwd": "/home/" + name,
            }
        )
        worker = ModeAwareWorker(ws, name)
        self.workers.append(worker)
        for _ in range(100):
            if self.service.runtime.connected(device["device"]["id"]):
                break
            await asyncio.sleep(0.01)
        return worker

    async def wait_request_count(self, requests, count):
        for _ in range(200):
            if len(requests) >= count:
                return
            await asyncio.sleep(0.01)
        self.fail(f"Expected {count} steering requests, received {len(requests)}")

    async def test_active_checkbox_can_send_mode_only_update_and_reverting_disables_it(self):
        await self.login(self.admin)
        await expect(self.page.locator("#room-title")).to_have_text(
            "Working together"
        )
        message = self.page.locator("#message-input")
        await message.fill("Start a long response")
        await self.page.get_by_role(
            "button", name="Send message", exact=True
        ).click()
        await self.wait_worker_turn()

        checkbox = self.page.locator("#tool-read-only")
        await expect(checkbox).to_be_enabled(timeout=5000)
        requests = []

        async def keep_update_uncertain(route):
            requests.append(route.request.post_data_json)
            await route.fulfill(
                status=202,
                content_type="application/json",
                body=json.dumps(
                    {
                        "steering": {
                            "status": "uncertain",
                            "error": "Test keeps the mode-only update pending.",
                        }
                    }
                ),
            )

        await self.page.route("**/api/rooms/*/steer", keep_update_uncertain)
        await checkbox.check()
        await self.page.wait_for_function(
            "() => roomState().toolReadOnlySaving !== true"
        )
        await expect(self.page.locator("#tool-read-only-detail")).to_have_text(
            "Send update to apply"
        )
        await expect(self.page.locator("#composer-hint")).to_contain_text(
            "Send update to apply Read only"
        )
        checked_title = await checkbox.locator("xpath=..").get_attribute("title")
        self.assertIsNotNone(checked_title)
        self.assertIn("Read only", checked_title)
        self.assertIn("Send an update to apply", checked_title)
        send = self.page.locator("#send-button")
        await expect(message).to_have_value("")
        await expect(send).to_have_attribute("aria-label", "Send update")
        await expect(send).to_be_enabled()
        await send.click()
        await self.wait_request_count(requests, 1)
        await self.page.wait_for_function("() => roomState().sending !== true")
        await self.page.evaluate(
            """() => {
                clearTimeout(state.eventTimer);
                for (const run of roomState().runs.values()) run.status = "completed";
                updateComposer();
            }"""
        )
        await expect(send).to_have_attribute("aria-label", "Check update")
        await expect(send).to_be_enabled()
        await send.click()
        await self.wait_request_count(requests, 2)
        await self.page.wait_for_function("() => roomState().sending !== true")
        await self.page.evaluate(
            """() => {
                clearTimeout(state.eventTimer);
                for (const run of roomState().runs.values()) run.status = "running";
                updateComposer();
            }"""
        )

        await checkbox.uncheck()
        await self.page.wait_for_function(
            "() => roomState().toolReadOnlySaving !== true"
        )
        unchecked_title = await checkbox.locator("xpath=..").get_attribute("title")
        self.assertIsNotNone(unchecked_title)
        self.assertNotEqual(checked_title, unchecked_title)
        self.assertIn("Send an update to apply", unchecked_title)
        await expect(message).to_have_value("")
        await expect(send).to_be_disabled()

        self.assertIs(requests[0]["tool_read_only"], True)
        self.assertIs(requests[1]["tool_read_only"], True)
        self.assertEqual(requests[0]["text"], "")
        self.assertEqual(requests[1]["text"], "")
        self.assertEqual(requests[0]["request_id"], requests[1]["request_id"])
        self.assertEqual(len(requests), 2)
        self.assertFalse(self.errors, self.errors)


if __name__ == "__main__":
    unittest.main()
