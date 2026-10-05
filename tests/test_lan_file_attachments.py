"""CODEX: Files stay inert, private, bounded, and readable on the actual executor."""

import asyncio
import json
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from aiohttp import ClientSession, FormData, web
from aiohttp.test_utils import TestServer

from codex_lan.media import attachment_extension, attachment_mime, attachment_name
from codex_lan.worker import ExecutorConnection, WorkerConnection, build_endpoints
import test_lan_server as fixture


class LanFileAttachmentsTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.LanServerTests.asyncSetUp
    asyncTearDown = fixture.LanServerTests.asyncTearDown
    request = fixture.LanServerTests.request
    until = fixture.LanServerTests.until
    worker = fixture.LanServerTests.worker
    room = fixture.LanServerTests.room
    submit_run = fixture.LanServerTests.submit_run
    ready_run = fixture.LanServerTests.ready_run

    def fake_upload_request(self, device, content=b"tiny"):
        part = SimpleNamespace(
            name="file", filename="fixture.txt", read_chunk=AsyncMock(side_effect=[content, b""]),
        )
        request = MagicMock()
        request.__getitem__.return_value = device["device"]
        request.multipart = AsyncMock(return_value=SimpleNamespace(next=AsyncMock(return_value=part)))
        return request

    async def test_upload_limits_reject_before_reading_and_allow_other_owners(self):
        blocked = asyncio.Event()
        tasks = []
        try:
            for _ in range(4):
                request = self.fake_upload_request(self.a)
                request.multipart.side_effect = blocked.wait
                tasks.append(asyncio.create_task(self.service.upload(request)))
            await self.until(lambda: self.service.active_uploads == 4)
            rejected = self.fake_upload_request(self.a)
            with self.assertRaises(web.HTTPTooManyRequests) as failure:
                await self.service.upload(rejected)
            self.assertEqual(failure.exception.headers["Retry-After"], "2")
            rejected.multipart.assert_not_awaited()
            allowed = await self.service.upload(self.fake_upload_request(self.b))
            self.assertEqual(allowed.status, 201)
            self.assertEqual(self.service.active_uploads, 4)
            for _ in range(4):
                request = self.fake_upload_request(self.b)
                request.multipart.side_effect = blocked.wait
                tasks.append(asyncio.create_task(self.service.upload(request)))
            await self.until(lambda: self.service.active_uploads == 8)
            global_rejected = self.fake_upload_request(self.admin)
            with self.assertRaises(web.HTTPTooManyRequests):
                await self.service.upload(global_rejected)
            global_rejected.multipart.assert_not_awaited()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        self.assertEqual(self.service.active_uploads, 0)
        self.assertEqual(self.service.device_uploads, {})

    async def test_failed_upload_releases_slot_on_validation_and_disk_errors(self):
        invalid = self.fake_upload_request(self.a)
        invalid.multipart.side_effect = ValueError("invalid multipart")
        with self.assertRaises(ValueError):
            await self.service.upload(invalid)
        self.assertEqual(self.service.active_uploads, 0)
        with patch("codex_lan.server.asyncio.to_thread", side_effect=OSError("synthetic disk failure")):
            with self.assertRaises(OSError):
                await self.service.upload(self.fake_upload_request(self.a))
        self.assertEqual(self.service.active_uploads, 0)
        self.assertEqual(self.service.device_uploads, {})
        self.assertEqual(list(self.service.upload_dir.iterdir()), [])

    async def test_cancelled_upload_keeps_slot_until_writer_finishes_and_removes_its_file(self):
        entered, proceed = asyncio.Event(), asyncio.Event()

        async def delayed_write(function, *args, **kwargs):
            entered.set()
            await proceed.wait()
            return function(*args, **kwargs)

        with patch("codex_lan.server.asyncio.to_thread", side_effect=delayed_write):
            task = asyncio.create_task(self.service.upload(self.fake_upload_request(self.a)))
            try:
                await asyncio.wait_for(entered.wait(), 2)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assertEqual(self.service.active_uploads, 1)
            finally:
                proceed.set()
                await self.until(lambda: self.service.active_uploads == 0)
        self.assertEqual(self.service.device_uploads, {})
        self.assertEqual(list(self.service.upload_dir.iterdir()), [])
        self.assertEqual(self.service.store.db.execute("SELECT COUNT(*) FROM attachments").fetchone()[0], 0)

    async def test_attachment_count_limit_does_not_leave_rejected_upload_files(self):
        with patch("codex_lan.store.MAX_ATTACHMENT_COUNT", 1):
            form = FormData()
            form.add_field("file", b"", filename="empty.txt")
            first = await self.request("POST", "/api/attachments", self.a, expected=201, data=form)
            existing = set(self.service.upload_dir.iterdir())
            form = FormData()
            form.add_field("file", b"tiny", filename="next.txt")
            rejected = await self.request("POST", "/api/attachments", self.a, expected=400, data=form)
            self.assertIn("file-count limit", rejected["error"])
            self.assertEqual(set(self.service.upload_dir.iterdir()), existing)
            self.assertEqual(self.service.store.get_attachment(self.a["device"]["id"], first["id"])["size"], 0)
            form = FormData()
            form.add_field("file", b"", filename="other.txt")
            await self.request("POST", "/api/attachments", self.b, expected=201, data=form)

    async def test_files_download_privately_and_native_turn_receives_path_not_contents(self):
        worker = await self.worker(self.a, "file-client")
        room = await self.room(self.a)
        files = []
        for name, content in (("notes.txt", b"PRIVATE-FILE-CONTENTS"), ("page.html", b"<script>alert(1)</script>"), ("empty.csv", b""), ("pixel.png", fixture.PNG)):
            form = FormData()
            form.add_field("file", content, filename=name, content_type="image/png")
            uploaded = await self.request("POST", "/api/attachments", self.a, expected=201, data=form)
            files.append(uploaded)
            response = await self.client.get(uploaded["url"], headers={"Authorization": "Bearer " + self.a["token"]})
            self.assertEqual(await response.read(), content)
            self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
            self.assertTrue(response.headers["Content-Disposition"].startswith("inline" if name.endswith(".png") else "attachment"))
            await self.request("GET", uploaded["url"], self.b, expected=403)
        created = await self.ready_run(self.a, worker, room, attachment_ids=[file["id"] for file in files])
        overview = self.service.runtime.runs[created["run"]["id"]].context_overview
        self.assertEqual(overview["input"]["files"], 3)
        self.assertEqual(overview["input"]["images"], 1)
        inputs = next(call["params"]["input"] for call in worker.calls if call["method"] == "turn/start")
        self.assertEqual([item["type"] for item in inputs], ["text", "text", "text", "text", "localImage"])
        self.assertIn("/fake/client/upload.png", inputs[1]["text"])
        self.assertIn("notes.txt", inputs[1]["text"])
        self.assertNotIn("PRIVATE-FILE-CONTENTS", json.dumps(inputs))
        self.assertNotIn(str(self.service.upload_dir), json.dumps(inputs))


class FileTransferTests(unittest.IsolatedAsyncioTestCase):
    async def test_executor_downloads_files_privately_without_executing_them(self):
        import uuid
        content = b"print('not executed')\n"
        async def download(request):
            self.assertEqual(request.headers["Authorization"], "Bearer fixture-token")
            return web.Response(body=content, headers={"Content-Disposition": 'attachment; filename="test.py"', "Content-Type": "application/octet-stream"})
        app = web.Application()
        app.router.add_get("/api/attachments/{id}", download)
        async with TestServer(app) as server, ClientSession() as http:
            with tempfile.TemporaryDirectory() as directory:
                config = SimpleNamespace(state_dir=Path(directory), endpoints=build_endpoints(str(server.make_url(""))))
                executor = ExecutorConnection(config, http, "fixture-token")
                executor.channel = SimpleNamespace(send=AsyncMock())
                packet = {"id": uuid.uuid4().hex, "attachment_id": str(uuid.uuid4())}
                await executor._start_attachment_store(packet)
                import asyncio
                await asyncio.gather(*list(executor.artifact_tasks))
                receipt = executor.channel.send.call_args.args[0]
                self.assertEqual(receipt["type"], "attachment/result")
                self.assertEqual(receipt["status"], "stored")
                path = Path(receipt["path"])
                self.assertEqual(path.read_bytes(), content)
                self.assertEqual(path.suffix, ".py")
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
                self.assertTrue(path.is_relative_to(Path(directory) / "uploads"))

    def test_names_and_types_cannot_turn_uploads_into_active_browser_content(self):
        self.assertEqual(attachment_name("../folder/evil\n.html"), "evil.html")
        self.assertEqual(attachment_name("C:\\folder\\report.pdf"), "report.pdf")
        self.assertEqual(attachment_mime(b"<svg onload=alert(1)>"), "application/octet-stream")
        self.assertEqual(attachment_extension("../../photo.svg", "application/octet-stream"), ".svg")
        self.assertEqual(attachment_extension("unsafe.bad;cmd", "application/octet-stream"), ".bin")


if __name__ == "__main__":
    unittest.main()
