"""CODEX: Retry transport failures without replaying user input, hiding real errors, or defeating Stop."""

import asyncio
import unittest
from unittest import mock

from codex_lan.recovery import CONTINUE_AFTER_CONNECTION, connection_snapshot, network_error
from codex_lan.worker import MAX_AUTOMATIC_RECOVERY_TURNS, WorkerConnection, WorkerError


ERROR = {"message": "Connection interrupted", "codexErrorInfo": {"responseStreamDisconnected": {"httpStatusCode": 503}}}


class RecoveryClassificationTests(unittest.TestCase):
    def test_only_transport_failures_are_retried(self):
        self.assertTrue(network_error(ERROR))
        self.assertTrue(network_error({"codexErrorInfo": "responseTooManyFailedAttempts"}))
        self.assertFalse(network_error(None))
        for kind in ("usageLimitExceeded", "unauthorized", "contextWindowExceeded", "badRequest"):
            self.assertFalse(network_error({"codexErrorInfo": kind, "message": "error sending request for url"}))
        for status in (401, 403, 429):
            self.assertFalse(network_error({"codexErrorInfo": {"httpConnectionFailed": {"httpStatusCode": status}}}))
            self.assertFalse(network_error({"codexErrorInfo": "httpConnectionFailed", "httpStatusCode": status}))

    def test_connection_metadata_does_not_expose_credentials(self):
        value = connection_snapshot({"state": "reconnecting", "mode": "proxy", "attempt": 999,
            "retry_in_seconds": float("nan"), "url": "socks5://secret@host", "password": "secret"})
        self.assertEqual(value["attempt"], 5)
        self.assertNotIn("secret", str(value))
        self.assertNotIn("retry_in_seconds", value)


class WorkerRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.worker = WorkerConnection(None, None, "fixture")
        self.packets = []
        self.worker.channel = mock.Mock(send=mock.AsyncMock(side_effect=self.packets.append))
        self.worker.codex = mock.Mock(request=mock.AsyncMock(return_value={"turn": {"id": "new-turn"}}))
        self.worker.turn_requests["thread"] = {"threadId": "thread", "model": "fixture-model", "effort": "high",
            "serviceTier": "fast", "input": [{"type": "text", "text": "Do something once"},
                                           {"type": "localImage", "path": "/fixture/image.png"}]}
        self.worker.native_turns["thread"] = "old-turn"
        self.worker.thread_connection["thread"] = "connecting"

    async def asyncTearDown(self):
        await self.worker._cancel_tasks()
        await self.worker.gateway.close()

    async def failed(self, turn="old-turn", error=ERROR):
        await self.worker._send_event("turn/completed", {"threadId": "thread",
            "turn": {"id": turn, "status": "failed", "error": error}})

    async def test_two_network_attempts_change_route_and_keep_public_status(self):
        for _index in range(2):
            await self.worker._send_event("error", {"threadId": "thread", "turnId": "old-turn", "error": ERROR, "willRetry": True})
        self.assertEqual(self.worker.gateway.snapshot()["mode"], "proxy")
        connections = [packet["params"]["connection"] for packet in self.packets if packet.get("method") == "lan/connection"]
        self.assertEqual(connections[-1]["state"], "switching")
        self.assertFalse(self.worker.codex.request.called)

    async def test_network_terminal_continues_saved_thread_without_replaying_input(self):
        self.worker.gateway.wait_ready = mock.AsyncMock(side_effect=AssertionError("No anonymous probe"))
        with mock.patch("codex_lan.worker.asyncio.sleep", new=mock.AsyncMock()):
            await self.failed()
            await self.worker.recovery_tasks["thread"]
        method, request = self.worker.codex.request.call_args.args
        self.assertEqual(method, "turn/start")
        self.assertEqual(request["threadId"], "thread")
        self.assertEqual((request["model"], request["effort"], request["serviceTier"]), ("fixture-model", "high", "fast"))
        self.assertEqual(request["input"], [{"type": "text", "text": CONTINUE_AFTER_CONNECTION}])
        self.assertNotIn("Do something once", str(request))
        self.assertEqual(self.worker.native_turns["thread"], "new-turn")
        self.assertTrue(any(packet.get("method") == "lan/turnResuming" for packet in self.packets))
        self.assertFalse(any(packet.get("method") == "turn/completed" for packet in self.packets))

    async def test_only_one_automatic_continuation_can_consume_model_usage(self):
        self.assertEqual(MAX_AUTOMATIC_RECOVERY_TURNS, 1)
        with mock.patch("codex_lan.worker.asyncio.sleep", new=mock.AsyncMock()):
            await self.failed()
            await self.worker.recovery_tasks["thread"]
            await self.failed(turn="new-turn")
        self.assertEqual(self.worker.codex.request.await_count, 1)
        self.assertFalse(self.worker.recovery_tasks)
        self.assertNotIn("thread", self.worker.turn_requests)
        completed = [
            packet for packet in self.packets if packet.get("method") == "turn/completed"
        ]
        self.assertEqual(len(completed), 1)
        self.assertIn("Retry manually", completed[0]["params"]["turn"]["error"]["message"])

    async def test_duplicate_failed_frame_does_not_queue_another_recovery(self):
        release = asyncio.Event()

        async def wait_for_release(_seconds):
            await release.wait()

        with mock.patch("codex_lan.worker.asyncio.sleep", side_effect=wait_for_release):
            await self.failed(turn=None)
            first = self.worker.recovery_tasks["thread"]
            await self.failed(turn=None)
            self.assertIs(self.worker.recovery_tasks["thread"], first)
            release.set()
            await first
        self.assertEqual(self.worker.codex.request.await_count, 1)

    async def test_stop_during_retry_delay_cancels_without_interrupting_already_failed_turn(self):
        await self.failed()
        await self.worker._relay_rpc("stop", {"method": "lan/cancel", "params": {"threadId": "thread"}})
        self.assertFalse(self.worker.codex.request.called)
        self.assertFalse(self.worker.recovery_tasks)
        self.assertNotIn("thread", self.worker.turn_requests)
        result = next(packet for packet in self.packets if packet.get("id") == "stop")
        self.assertTrue(result["result"]["recovering"])
        self.assertNotIn("error", result)

    async def test_late_resumed_turn_is_interrupted_after_stop(self):
        await self.failed()
        await self.worker._relay_rpc("stop", {"method": "lan/cancel", "params": {"threadId": "thread"}})
        await self.worker._send_event("turn/started", {"threadId": "thread", "turn": {"id": "late-turn"}})
        await asyncio.gather(*self.worker.rpc_tasks)
        self.worker.codex.request.assert_awaited_once_with("turn/interrupt", {"threadId": "thread", "turnId": "late-turn"})

    async def test_non_network_failure_is_terminal(self):
        await self.failed(error={"codexErrorInfo": "usageLimitExceeded", "message": "Limit reached"})
        self.assertFalse(self.worker.recovery_tasks)
        self.assertNotIn("thread", self.worker.turn_requests)
        self.assertTrue(any(packet.get("method") == "turn/completed" for packet in self.packets))

    async def test_actual_progress_resets_attempts_without_forgetting_current_turn(self):
        await self.worker.gateway.report_failure(scope="thread")
        await self.worker._send_event("item/agentMessage/delta", {"threadId": "thread", "turnId": "old-turn", "delta": "Working"})
        self.assertEqual(self.worker.gateway.snapshot()["state"], "connected")
        self.assertEqual(self.worker.native_turns["thread"], "old-turn")
        await self.worker._send_event("turn/completed", {"threadId": "thread", "turn": {"id": "old-turn", "status": "completed"}})
        self.assertNotIn("thread", self.worker.turn_requests)

    async def test_uncertain_local_rpc_is_not_blindly_retried(self):
        self.worker.codex.request.side_effect = WorkerError("RPC timeout")
        with mock.patch("codex_lan.worker.asyncio.sleep", new=mock.AsyncMock()):
            await self.failed()
            await self.worker.recovery_tasks["thread"]
        self.assertEqual(self.worker.codex.request.await_count, 1)
        self.assertNotIn("thread", self.worker.turn_requests)
        self.assertFalse(self.worker.recovery_tasks)
        completed = next(packet for packet in self.packets if packet.get("method") == "turn/completed")
        self.assertIsNone(completed["params"]["turn"]["id"])


if __name__ == "__main__":
    unittest.main()
