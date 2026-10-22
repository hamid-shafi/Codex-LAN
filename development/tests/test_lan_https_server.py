"""CODEX: Exercise the real server over verified TLS without opening a LAN listener."""

import asyncio
import hashlib
import json
import socket
import ssl
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import aiohttp
from aiohttp import web

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from project_layout import configure_imports
configure_imports(__file__)

from codex_lan.network import ssl_context
from codex_lan.server import COOKIE, SERVICE, create_app
from direct_client import ConnectorError, Transport
from setup_lan_https import prepare_network
from test_lan_executor_endpoint import FakeHostSession
from test_lan_server import FakeAuth


class HTTPSApplicationTests(unittest.IsolatedAsyncioTestCase):
    network_address = "192.168.1.56"
    listen_address = "127.0.0.1"

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        with socket.socket() as available:
            available.bind((self.listen_address, 0))
            self.port = available.getsockname()[1]
        self.network = await asyncio.to_thread(prepare_network, self.root, self.network_address, self.port)
        self.app = create_app(self.root, auth=FakeAuth())
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        await web.TCPSite(self.runner, self.listen_address, self.port, ssl_context=ssl_context(self.network)).start()
        self.context = ssl.create_default_context(cafile=self.network["ca"])

    async def asyncTearDown(self):
        await self.runner.cleanup()
        self.temp.cleanup()

    async def request(self, method, path, data=None, origin=None, cookie=None):
        reader, writer = await asyncio.open_connection(self.listen_address, self.port, ssl=self.context,
                                                      server_hostname=self.network_address)
        encoded = json.dumps(data).encode() if data is not None else b""
        headers = [f"{method} {path} HTTP/1.1", f"Host: {self.network_address}:{self.port}", "Connection: close",
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


class SSHTunnelHTTPSApplicationTests(HTTPSApplicationTests):
    network_address = "127.0.0.2"
    listen_address = "127.0.0.2"

    async def test_tunnel_requires_trusted_tls_exact_host_and_same_origin(self):
        origin = self.network["public_origin"]
        async with aiohttp.ClientSession() as client:
            with self.assertRaises(aiohttp.ClientConnectorCertificateError):
                await client.get(origin + "/api/health")
            async with client.get(origin + "/api/health", ssl=self.context) as response:
                self.assertEqual(response.status, 200)
            for headers in ({"Host": f"127.0.0.2:{self.port + 1}"},
                            {"Host": f"127.0.0.3:{self.port}"},
                            {"Origin": f"https://127.0.0.1:{self.port}"},
                            {"Sec-Fetch-Site": "cross-site"}):
                async with client.get(origin + "/api/health", ssl=self.context, headers=headers) as response:
                    self.assertEqual(response.status, 403)
        plaintext = web.TCPSite(self.runner, self.listen_address, 0)
        await plaintext.start()
        try:
            port = plaintext._server.sockets[0].getsockname()[1]
            async with aiohttp.ClientSession() as client:
                async with client.get(f"http://127.0.0.2:{port}/api/health",
                                      headers={"Host": origin.removeprefix("https://")}) as response:
                    self.assertEqual(response.status, 403)
        finally:
            await plaintext.stop()

    async def test_two_approved_clients_pair_with_same_ca_and_use_executor_only(self):
        service = self.app[SERVICE]
        admin = service.store.bootstrap_admin("Host")
        admin_headers = {"Authorization": "Bearer " + admin["token"]}
        release = self.root / "releases"
        release.mkdir()
        archive = b"fixture connector archive"
        (release / "client.tar.gz").write_bytes(archive)
        (release / "Install-Codex-Connector.sh").write_bytes(b"fixture installer")
        (release / "release.json").write_text(json.dumps({"version": "0.3.0", "size": len(archive),
                                                        "sha256": hashlib.sha256(archive).hexdigest()}))
        origin = self.network["public_origin"]
        certificate = self.network["ca"].read_text()
        pairings, credentials = [], []
        async with aiohttp.ClientSession() as client:
            for name in ("First tunnel client", "Second tunnel client"):
                async with client.post(origin + "/api/enroll", ssl=self.context,
                                       headers={"Origin": origin}, json={"name": name}) as response:
                    self.assertEqual(response.status, 201)
                    enrolled = await response.json()
                    cookie = {"Cookie": COOKIE + "=" + response.cookies[COOKIE].value}
                async with client.post(origin + "/api/client/pairing", ssl=self.context,
                                       headers=cookie, json={}) as response:
                    self.assertEqual(response.status, 403)
                device_id = enrolled["device"]["id"]
                async with client.patch(origin + "/api/admin/devices/" + device_id, ssl=self.context,
                                        headers=admin_headers, json={"status": "approved"}) as response:
                    self.assertEqual(response.status, 200)
                async with client.post(origin + "/api/client/pairing", ssl=self.context,
                                       headers=cookie, json={}) as response:
                    self.assertEqual(response.status, 200)
                    pairing = await response.json()
                self.assertEqual(pairing["server"], origin)
                self.assertEqual(pairing["ca_certificate"], certificate)
                self.assertEqual(pairing["device_id"], device_id)
                self.assertNotIn("PRIVATE KEY", json.dumps(pairing))
                transport = Transport(pairing["server"], pairing["ca_certificate"])
                claimed = await asyncio.to_thread(transport.json, "/api/client/claim",
                                                 {"pairing_code": pairing["pairing_code"]})
                self.assertEqual(claimed["device"]["id"], device_id)
                with self.assertRaises(ConnectorError):
                    await asyncio.to_thread(transport.json, "/api/client/claim",
                                            {"pairing_code": pairing["pairing_code"]})
                transport.token = claimed["token"]
                self.assertEqual((await asyncio.to_thread(transport.json, "/api/me"))["device"]["id"], device_id)
                pairings.append(pairing)
                credentials.append(claimed["token"])
            self.assertNotEqual(pairings[0]["pairing_code"], pairings[1]["pairing_code"])
            self.assertNotEqual(credentials[0], credentials[1])
            async with client.get(origin + "/api/worker", ssl=self.context, headers=admin_headers) as response:
                self.assertEqual(response.status, 426)
                self.assertNotIn("FAKE-NOT-A-CREDENTIAL", await response.text())
            with patch("codex_lan.server.HostWorkerSession", FakeHostSession):
                async with client.ws_connect(origin + "/api/executor", ssl=self.context,
                                             headers={"Authorization": "Bearer " + credentials[0]}) as websocket:
                    await websocket.send_json({"type": "executor_hello", "protocol": 1,
                                               "version": "0.3.0", "cwd": "/home/client"})
                    ready = await websocket.receive_json(timeout=2)
                    self.assertEqual(ready, {"type": "ready", "execution_mode": "server"})
                    self.assertNotIn("FAKE-NOT-A-CREDENTIAL", json.dumps(ready))


if __name__ == "__main__":
    unittest.main()
