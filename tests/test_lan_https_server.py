"""CODEX: Exercise the real server over verified TLS without opening a LAN listener."""

import asyncio
import json
import socket
import ssl
import tempfile
import unittest
from pathlib import Path

from aiohttp import web

from codex_lan.network import ssl_context
from codex_lan.server import SERVICE, create_app
from setup_lan_https import prepare_network
from test_lan_server import FakeAuth


class HTTPSApplicationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        with socket.socket() as available:
            available.bind(("127.0.0.1", 0))
            self.port = available.getsockname()[1]
        self.network = prepare_network(self.root, "192.168.1.56", self.port)
        self.app = create_app(self.root, auth=FakeAuth())
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        await web.TCPSite(self.runner, "127.0.0.1", self.port, ssl_context=ssl_context(self.network)).start()
        self.context = ssl.create_default_context(cafile=self.network["ca"])

    async def asyncTearDown(self):
        await self.runner.cleanup()
        self.temp.cleanup()

    async def request(self, method, path, data=None, origin=None, cookie=None):
        reader, writer = await asyncio.open_connection("127.0.0.1", self.port, ssl=self.context,
                                                      server_hostname="192.168.1.56")
        encoded = json.dumps(data).encode() if data is not None else b""
        headers = [f"{method} {path} HTTP/1.1", f"Host: 192.168.1.56:{self.port}", "Connection: close",
                   "Content-Type: application/json", f"Content-Length: {len(encoded)}"]
        if origin:
            headers.append("Origin: " + origin)
        if cookie:
            headers.append("Cookie: " + cookie)
        writer.write(("\r\n".join(headers) + "\r\n\r\n").encode() + encoded)
        await writer.drain()
        response = await asyncio.wait_for(reader.read(), 5)
        writer.close()
        await writer.wait_closed()
        head, payload = response.split(b"\r\n\r\n", 1)
        return head.decode(), json.loads(payload)

    async def test_enrollment_cookie_is_secure_and_pending_stays_restricted(self):
        head, info = await self.request("GET", "/api/health")
        self.assertIn("200 OK", head)
        self.assertEqual(info["public_origin"], self.network["public_origin"])
        head, enrolled = await self.request("POST", "/api/enroll", {"name": "Ubuntu teammate"}, self.network["public_origin"])
        self.assertIn("201 Created", head)
        cookie_header = next(line for line in head.split("\r\n") if line.lower().startswith("set-cookie:"))
        self.assertIn("Secure", cookie_header)
        self.assertIn("HttpOnly", cookie_header)
        cookie = cookie_header.split(": ", 1)[1].split(";", 1)[0]
        head, _ = await self.request("GET", "/api/rooms", cookie=cookie)
        self.assertIn("403 Forbidden", head)
        self.assertEqual(enrolled["device"]["status"], "pending")
        head, _ = await self.request("POST", "/api/enroll", {"name": "Blocked"}, "https://other.example")
        self.assertIn("403 Forbidden", head)


if __name__ == "__main__":
    unittest.main()
