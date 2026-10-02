"""CODEX: Files stay inert, private, bounded, and readable on the actual executor."""

import json
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

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
