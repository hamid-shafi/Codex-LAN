"""CODEX: Bound RPC lifetime when either transport writes or replies stall."""

import asyncio
import gc
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from project_layout import configure_imports, source_file
configure_imports(__file__)

from codex_lan.runtime import Peer
from codex_lan.worker import CodexAppServer, WorkerError


class RpcLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def client(self, kind, sender):
        if kind == "peer":
            client = Peer("device", SimpleNamespace(send_json=sender))
            return client, client.call, asyncio.TimeoutError
        client = CodexAppServer(Path("unused"), Path("."), Path("."), None, None)
        client._write = sender
        return client, client.request, WorkerError

    async def test_timeout_includes_blocked_transport_send(self):
        for kind in ("peer", "codex"):
            with self.subTest(kind=kind):
                entered = asyncio.Event()
                cancelled = asyncio.Event()
                futures = []

                async def blocked_send(_packet):
                    futures.extend(client.pending.values())
                    entered.set()
                    try:
                        await asyncio.Event().wait()
                    finally:
                        cancelled.set()

                client, call, error = self.client(kind, AsyncMock(side_effect=blocked_send))
                task = asyncio.create_task(call("test", {}, timeout=0.03))
                try:
                    await asyncio.wait_for(entered.wait(), 0.5)
                    done, _ = await asyncio.wait({task}, timeout=0.5)
                    self.assertIn(task, done, "RPC timeout must cover a blocked send")
                    with self.assertRaises(error):
                        await task
                    self.assertTrue(cancelled.is_set())
                    self.assertEqual(client.pending, {})
                    self.assertTrue(futures[0].cancelled())
                finally:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

    async def test_external_cancellation_releases_unsent_request(self):
        for kind in ("peer", "codex"):
            with self.subTest(kind=kind):
                entered = asyncio.Event()
                futures = []

                async def blocked_send(_packet):
                    futures.extend(client.pending.values())
                    entered.set()
                    await asyncio.Event().wait()

                client, call, _ = self.client(kind, AsyncMock(side_effect=blocked_send))
                task = asyncio.create_task(call("test", {}, timeout=10))
                await asyncio.wait_for(entered.wait(), 0.5)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assertEqual(client.pending, {})
                self.assertTrue(futures[0].cancelled())

    async def test_reply_can_arrive_before_send_returns(self):
        for kind in ("peer", "codex"):
            with self.subTest(kind=kind):
                async def send_and_reply(packet):
                    if kind == "peer":
                        client.receive({"id": packet["id"], "result": {"ready": True}})
                    else:
                        client.pending[packet["id"]].set_result({"result": {"ready": True}})
                    await asyncio.sleep(0)

                client, call, _ = self.client(kind, AsyncMock(side_effect=send_and_reply))
                self.assertEqual(await call("test", {}, timeout=0.5), {"ready": True})
                self.assertEqual(client.pending, {})

    async def test_disconnect_during_send_does_not_leave_an_unhandled_future(self):
        loop = asyncio.get_running_loop()
        unhandled = []
        original_handler = loop.get_exception_handler()
        loop.set_exception_handler(lambda _loop, context: unhandled.append(context))
        try:
            for kind in ("peer", "codex"):
                with self.subTest(kind=kind):
                    entered = asyncio.Event()

                    async def disconnected_send(_packet):
                        for future in client.pending.values():
                            future.set_exception(ConnectionError("Transport closed"))
                        entered.set()
                        await asyncio.Event().wait()

                    client, call, _ = self.client(kind, AsyncMock(side_effect=disconnected_send))
                    task = asyncio.create_task(call("test", {}, timeout=10))
                    await asyncio.wait_for(entered.wait(), 0.5)
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                    self.assertEqual(client.pending, {})
                    del task
            gc.collect()
            await asyncio.sleep(0)
            self.assertEqual(unhandled, [])
        finally:
            loop.set_exception_handler(original_handler)


if __name__ == "__main__":
    unittest.main()
