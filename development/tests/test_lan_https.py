"""CODEX: Verify LAN TLS using generated temporary keys and loopback-only test listeners."""

from __future__ import annotations

import asyncio
import hashlib
import json
import ssl
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import aiohttp
from aiohttp import web

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from project_layout import configure_imports, source_file
configure_imports(__file__)

from codex_lan.network import load_network, ssl_context, validate_address, validate_port
from setup_lan_https import ca_fingerprint, prepare_network


class LanNetworkConfigTests(unittest.TestCase):
    def test_missing_config_keeps_loopback_mode_without_writing_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary)
            self.assertIsNone(load_network(path))
            self.assertEqual(list(path.iterdir()), [])

    def test_only_explicit_private_ipv4_and_separate_high_port_are_allowed(self):
        for address in ("10.1.2.3", "172.16.0.2", "172.31.255.254", "192.168.1.56"):
            self.assertEqual(validate_address(address), address)
        for address in ("0.0.0.0", "127.0.0.1", "8.8.8.8", "169.254.1.1", "172.32.0.1", "192.0.2.1", "localhost", "::1", "fc00::1", "192.168.1.2/24", 3232235777):
            with self.subTest(address=address), self.assertRaises(ValueError):
                validate_address(address)
        self.assertEqual(validate_port(8767), 8767)
        for port in (443, 8765, 65536, 0, True, "8767"):
            with self.subTest(port=port), self.assertRaises(ValueError):
                validate_port(port)

    def test_setup_reuses_identity_and_reports_only_public_fingerprint(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary)
            config = prepare_network(path, "192.168.1.56")
            self.assertEqual(config, load_network(path))
            self.assertEqual(config["public_origin"], "https://192.168.1.56:8767")
            self.assertEqual(ssl_context(config).minimum_version, ssl.TLSVersion.TLSv1_2)
            tracked = [path / "network.json", *sorted((path / "tls").iterdir())]
            original = {str(item): (hashlib.sha256(item.read_bytes()).hexdigest(), item.stat().st_mtime_ns) for item in tracked}
            prepare_network(path, "192.168.1.56")
            self.assertEqual(original, {str(item): (hashlib.sha256(item.read_bytes()).hexdigest(), item.stat().st_mtime_ns) for item in tracked})
            self.assertTrue(all(stat.S_IMODE(item.stat().st_mode) == 0o600 for item in tracked))
            self.assertRegex(ca_fingerprint(config["ca"]), r"^(?:[0-9A-F]{2}:){31}[0-9A-F]{2}$")
            command = [sys.executable, str(source_file(Path(__file__).resolve().parents[2], "setup_lan_https.py")), "--data-dir", temporary]
            result = subprocess.run(command, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(ca_fingerprint(config["ca"]), result.stdout)
            self.assertNotIn("PRIVATE KEY", result.stdout + result.stderr)
            with self.assertRaisesRegex(ValueError, "preserved"):
                prepare_network(path, "192.168.1.57")

    def test_missing_server_certificate_is_rebuilt_without_replacing_keys(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary)
            config = prepare_network(path, "192.168.1.56")
            preserved = {item: hashlib.sha256(item.read_bytes()).hexdigest()
                         for item in (config["key"], config["ca"], path / "tls/ca-key.pem", path / "network.json")}
            config["cert"].unlink()
            prepare_network(path, "192.168.1.56")
            self.assertTrue(config["cert"].is_file())
            self.assertEqual(preserved, {item: hashlib.sha256(item.read_bytes()).hexdigest() for item in preserved})
            ssl_context(load_network(path))

    def test_config_cannot_point_outside_the_server_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary)
            raw = {"lan_address": "192.168.1.56", "port": 8767,
                   "public_origin": "https://192.168.1.56:8767",
                   "cert": "tls/server.pem", "ca": "tls/ca.pem", "key": "../outside-key.pem"}
            filename = path / "network.json"
            filename.write_text(json.dumps(raw))
            filename.chmod(0o600)
            with self.assertRaisesRegex(ValueError, "inside"):
                load_network(path)

    def test_unreadable_or_exposed_keys_symlinks_and_origin_mismatch_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary)
            config = prepare_network(path, "192.168.1.56")
            config["key"].chmod(0o644)
            with self.assertRaisesRegex(ValueError, "permissions"):
                ssl_context(config)
            config["key"].chmod(0o600)
            actual_cert = config["cert"].with_name("original.pem")
            config["cert"].rename(actual_cert)
            config["cert"].symlink_to(actual_cert)
            with self.assertRaisesRegex(ValueError, "regular file"):
                ssl_context(config)
            filename = path / "network.json"
            raw = json.loads(filename.read_text())
            raw["public_origin"] = "http://192.168.1.56:8767"
            filename.write_text(json.dumps(raw))
            with self.assertRaisesRegex(ValueError, "public_origin"):
                load_network(path)
            filename.chmod(0o644)
            with self.assertRaisesRegex(ValueError, "permissions"):
                load_network(path)


class LanHttpsListenerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = await asyncio.to_thread(prepare_network, Path(self.temp.name), "192.168.1.56")
        application = web.Application()

        async def health(request):
            return web.json_response({"ok": True, "secure": request.secure})

        async def websocket(request):
            socket = web.WebSocketResponse()
            await socket.prepare(request)
            await socket.send_json({"ok": True, "secure": request.secure})
            await socket.close()
            return socket

        application.add_routes([web.get("/health", health), web.get("/ws", websocket)])
        self.runner = web.AppRunner(application)
        await self.runner.setup()
        self.http = web.TCPSite(self.runner, "127.0.0.1", 0)
        self.https = web.TCPSite(self.runner, "127.0.0.1", 0, ssl_context=ssl_context(self.config))
        await self.http.start()
        await self.https.start()
        self.http_url = f"http://127.0.0.1:{self.http._server.sockets[0].getsockname()[1]}"
        self.https_url = f"https://127.0.0.1:{self.https._server.sockets[0].getsockname()[1]}"

    async def asyncTearDown(self):
        await self.runner.cleanup()
        self.temp.cleanup()

    async def test_loopback_http_and_verified_https_share_one_application(self):
        trusted = ssl.create_default_context(cafile=str(self.config["ca"]))
        async with aiohttp.ClientSession() as client:
            async with client.get(self.http_url + "/health") as response:
                self.assertEqual(await response.json(), {"ok": True, "secure": False})
            async with client.get(self.https_url + "/health", ssl=trusted, server_hostname=self.config["lan_address"]) as response:
                self.assertEqual(await response.json(), {"ok": True, "secure": True})
            async with client.ws_connect(self.https_url + "/ws", ssl=trusted, server_hostname=self.config["lan_address"]) as socket:
                self.assertEqual(await socket.receive_json(), {"ok": True, "secure": True})

    async def test_untrusted_certificate_and_wrong_server_identity_fail_closed(self):
        trusted = ssl.create_default_context(cafile=str(self.config["ca"]))
        async with aiohttp.ClientSession() as client:
            with self.assertRaises(aiohttp.ClientConnectorCertificateError):
                await client.get(self.https_url + "/health", server_hostname=self.config["lan_address"])
            with self.assertRaises(aiohttp.ClientConnectorCertificateError):
                await client.get(self.https_url + "/health", ssl=trusted, server_hostname="192.168.1.57")


if __name__ == "__main__":
    unittest.main()
