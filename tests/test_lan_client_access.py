"""CODEX: Pairing and connector maintenance use temporary state and fake local workers."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import ssl
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
from aiohttp import web

import test_lan_server as server_fixture
from codex_lan.client_access import ClientAccess
from codex_lan.network import ssl_context
from codex_lan.runtime import Peer, Runtime
from codex_lan.server import COOKIE
from setup_lan_https import prepare_network


class LanClientAccessTests(unittest.IsolatedAsyncioTestCase):
    asyncTearDown = server_fixture.LanServerTests.asyncTearDown
    request = server_fixture.LanServerTests.request
    until = server_fixture.LanServerTests.until
    worker = server_fixture.LanServerTests.worker
    room = server_fixture.LanServerTests.room
    submit_run = server_fixture.LanServerTests.submit_run
    ready_run = server_fixture.LanServerTests.ready_run

    @classmethod
    def setUpClass(cls):
        cls.tls_temp = tempfile.TemporaryDirectory()
        cls.network = prepare_network(Path(cls.tls_temp.name), "192.168.1.56")

    @classmethod
    def tearDownClass(cls):
        cls.tls_temp.cleanup()

    async def asyncSetUp(self):
        await server_fixture.LanServerTests.asyncSetUp(self)
        self.service.network = self.network

    def cookie(self, device):
        return {"Cookie": COOKIE + "=" + device["token"]}

    def connector_credential_count(self):
        return self.service.store.db.execute(
            "SELECT COUNT(*) FROM device_credentials WHERE kind='connector'"
        ).fetchone()[0]

    def publish_release(self, installer=b"harmless installer fixture", version="0.3.0"):
        directory = Path(self.temp.name) / "releases"
        directory.mkdir(exist_ok=True)
        archive = b"harmless archive fixture"
        (directory / "client.tar.gz").write_bytes(archive)
        (directory / "Install-Codex-Connector.sh").write_bytes(installer)
        (directory / "release.json").write_text(json.dumps({
            "version": version, "sha256": hashlib.sha256(archive).hexdigest(), "size": len(archive),
        }))
        return archive, installer

    async def pairing(self, device=None):
        device = device or self.a
        release = self.service.client_access.release_info()
        if release.get("available") is not True:
            self.publish_release()
            release = self.service.client_access.release_info()
        self.client.session.cookie_jar.clear()
        response = await self.client.post(release["pairing_url"], headers=self.cookie(device), json={})
        self.assertEqual(response.status, 200, await response.text())
        rotated = response.cookies[COOKIE].value
        self.assertNotEqual(rotated, device["token"])
        self.assertIsNotNone(
            self.service.store.db.execute(
                "SELECT 1 FROM browser_sessions WHERE token_hash=?",
                (hashlib.sha256(rotated.encode()).hexdigest(),),
            ).fetchone()
        )
        self.assertIn("attachment", response.headers["Content-Disposition"])
        self.assertIn(release["pairing_filename"], response.headers["Content-Disposition"])
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        value = await response.json()
        self.assertEqual(value["server"], self.network["public_origin"])
        self.assertEqual(value["device_id"], device["device"]["id"])
        self.assertEqual(value["device_name"], device["device"]["name"])
        self.assertEqual(value["connector_version"], release["version"])
        self.assertEqual(value["ca_certificate"], self.network["ca"].read_text())
        self.assertNotIn("token", value)
        self.assertNotIn(device["token"], json.dumps(value))
        self.assertNotIn("PRIVATE KEY", json.dumps(value))
        self.assertNotIn("FAKE-NOT-A-CREDENTIAL", json.dumps(value))
        self.client.session.cookie_jar.clear()
        return value

    async def claim(self, value, expected=200):
        return await self.request("POST", "/api/client/claim", expected=expected,
                                  json={"pairing_code": value["pairing_code"]})

    async def test_approved_cookie_pairs_its_own_identity_and_claim_is_single_use(self):
        pairing = await self.pairing()
        result = await self.claim(pairing)
        self.assertEqual(result["device"]["id"], self.a["device"]["id"])
        self.assertNotEqual(result["token"], self.a["token"])
        self.assertEqual(self.service.store.authenticate(result["token"])["id"], self.a["device"]["id"])
        await self.claim(pairing, expected=403)
        await self.request("POST", "/api/client/pairing", expected=403, json={})
        await self.request("POST", "/api/client/pairing", self.pending, expected=403, json={})

    async def test_expiry_wrong_codes_and_regeneration_preserve_recent_pairings(self):
        original_credentials = self.connector_credential_count()
        previous = await self.pairing()
        self.assertEqual(self.connector_credential_count(), original_credentials)
        current = await self.pairing()
        self.assertEqual(self.connector_credential_count(), original_credentials)
        self.assertEqual(
            self.service.client_access._pairing_record(previous["pairing_code"])["device_id"],
            self.a["device"]["id"],
        )
        self.assertEqual(self.connector_credential_count(), original_credentials)
        for invalid in ("short", "x" * 64, "x" * 129, None, 123):
            await self.request("POST", "/api/client/claim", expected=403, json={"pairing_code": invalid})
            self.assertEqual(self.connector_credential_count(), original_credentials)
        with patch("codex_lan.client_access.time.time", return_value=current["expires_at"]):
            await self.claim(current, expected=403)
        self.assertEqual(self.connector_credential_count(), original_credentials)
        fresh = await self.pairing()
        self.assertEqual(self.connector_credential_count(), original_credentials)
        await self.claim(fresh)
        self.assertEqual(self.connector_credential_count(), original_credentials + 1)

    async def test_pairing_survives_client_access_recreation(self):
        self.publish_release()
        pairing = await self.pairing()
        restarted = ClientAccess(self.service, self.service.client_access.data_dir)
        record = restarted._pairing_record(pairing["pairing_code"])
        self.assertEqual(record["device_id"], pairing["device_id"])
        self.assertEqual(record["connector_version"], pairing["connector_version"])
        restarted._pairing_record(pairing["pairing_code"], consume=True)
        with self.assertRaises(web.HTTPForbidden):
            self.service.client_access._pairing_record(pairing["pairing_code"])

    async def test_private_bootstrap_keeps_secret_off_urls_and_preserves_one_time_claim(self):
        _, installer = self.publish_release()
        original_credentials = self.connector_credential_count()
        self.client.session.cookie_jar.clear()
        release = self.service.client_access.release_info()
        response = await self.client.post(
            release["private_installer_url"], headers=self.cookie(self.a), json={}
        )
        self.assertEqual(response.status, 200, await response.text())
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertIn("attachment", response.headers["Content-Disposition"])
        self.assertIn(release["private_installer_filename"], response.headers["Content-Disposition"])
        script = await response.text()
        match = re.search(
            r"<<'CODEX_PAIRING_PAYLOAD'\n([A-Za-z0-9+/=]+)\nCODEX_PAIRING_PAYLOAD",
            script,
        )
        self.assertIsNotNone(match)
        self.assertIn('.read_bytes().strip(), validate=True', script)
        self.assertIn('except urllib.error.HTTPError as error:', script)
        self.assertIn('run the exact filename shown by Codex', script)
        pairing = json.loads(base64.b64decode(match.group(1), validate=True))
        self.assertEqual(pairing["device_id"], self.a["device"]["id"])
        self.assertEqual(pairing["server"], self.network["public_origin"])
        self.assertEqual(pairing["ca_certificate"], self.network["ca"].read_text())
        self.assertEqual(pairing["connector_version"], release["version"])
        self.assertNotIn(pairing["pairing_code"], script)
        self.assertNotIn(self.a["token"], script)
        self.assertNotIn("PRIVATE KEY", script)
        self.assertNotIn("?pairing", script)
        self.assertIn('data=raw', script)
        self.assertIn('chmod 700 -- "$0"', script)
        self.assertEqual(self.connector_credential_count(), original_credentials)

        self.client.session.cookie_jar.clear()
        response = await self.client.post(
            f'/api/client/bootstrap/installer/{release["version"]}', json=pairing
        )
        self.assertEqual(response.status, 200, await response.text())
        self.assertEqual(await response.read(), installer)
        self.assertEqual(self.connector_credential_count(), original_credentials)

        wrong_device = dict(pairing, device_id=self.b["device"]["id"])
        response = await self.client.post("/api/client/bootstrap/installer", json=wrong_device)
        self.assertEqual(response.status, 403, await response.text())
        self.assertEqual(self.connector_credential_count(), original_credentials)

        result = await self.claim(pairing)
        self.assertEqual(result["device"]["id"], self.a["device"]["id"])
        self.assertEqual(self.connector_credential_count(), original_credentials + 1)
        await self.claim(pairing, expected=403)
        response = await self.client.post("/api/client/bootstrap/installer", json=pairing)
        self.assertEqual(response.status, 403, await response.text())

    async def test_private_bootstrap_expires_and_requires_current_approval(self):
        self.publish_release()
        release = self.service.client_access.release_info()
        self.client.session.cookie_jar.clear()
        response = await self.client.post(
            release["private_installer_url"], headers=self.cookie(self.a), json={}
        )
        script = await response.text()
        encoded = re.search(
            r"<<'CODEX_PAIRING_PAYLOAD'\n([A-Za-z0-9+/=]+)\nCODEX_PAIRING_PAYLOAD",
            script,
        ).group(1)
        pairing = json.loads(base64.b64decode(encoded, validate=True))
        self.client.session.cookie_jar.clear()
        with patch("codex_lan.client_access.time.time", return_value=pairing["expires_at"]):
            response = await self.client.post("/api/client/bootstrap/installer", json=pairing)
            self.assertEqual(response.status, 403, await response.text())
        await self.claim(pairing, expected=403)

        response = await self.client.post(
            release["private_installer_url"], headers=self.cookie(self.a), json={}
        )
        script = await response.text()
        encoded = re.search(
            r"<<'CODEX_PAIRING_PAYLOAD'\n([A-Za-z0-9+/=]+)\nCODEX_PAIRING_PAYLOAD",
            script,
        ).group(1)
        pairing = json.loads(base64.b64decode(encoded, validate=True))
        self.service.store.set_device(self.admin_id, self.a["device"]["id"], status="revoked")
        self.client.session.cookie_jar.clear()
        response = await self.client.post("/api/client/bootstrap/installer", json=pairing)
        self.assertEqual(response.status, 403, await response.text())

    async def test_revoked_device_cannot_claim_or_download(self):
        value = await self.pairing()
        self.service.store.set_device(self.admin_id, self.a["device"]["id"], status="revoked")
        await self.claim(value, expected=403)
        await self.request("GET", "/api/client/release", self.a, expected=403)
        await self.request("GET", "/api/client/download/installer", self.a, expected=403)
        self.service.store.set_device(self.admin_id, self.a["device"]["id"], status="approved")
        await self.claim(value, expected=403)

    async def test_pairing_regeneration_does_not_revoke_another_device_code(self):
        old_a, current_b = await self.pairing(self.a), await self.pairing(self.b)
        current_a = await self.pairing(self.a)
        self.assertEqual((await self.claim(old_a))["device"]["id"], self.a["device"]["id"])
        self.assertEqual((await self.claim(current_b))["device"]["id"], self.b["device"]["id"])
        self.assertEqual((await self.claim(current_a))["device"]["id"], self.a["device"]["id"])

    async def test_enrollment_cookie_waits_for_approval_and_preserves_identity(self):
        response = await self.client.post("/api/enroll", json={"name": "My laptop"})
        self.assertEqual(response.status, 201)
        value = await response.json()
        cookie = response.cookies[COOKIE]
        self.assertTrue(cookie["httponly"])
        self.assertEqual(cookie["samesite"], "Strict")
        self.assertEqual(cookie["path"], "/")
        self.assertNotEqual(cookie.value, value["token"])
        self.assertEqual(
            self.service.store.authenticate_browser_session(cookie.value, allow_pending=True)["id"],
            value["device"]["id"],
        )
        headers = {"Cookie": COOKIE + "=" + cookie.value}
        self.client.session.cookie_jar.clear()
        me = await self.request("GET", "/api/me", headers=headers)
        self.assertEqual(me["device"]["status"], "pending")
        self.client.session.cookie_jar.clear()
        await self.request("POST", "/api/client/pairing", headers=headers, expected=403, json={})
        await self.request("PATCH", f'/api/admin/devices/{value["device"]["id"]}', self.admin,
                           json={"status": "approved"})
        self.client.session.cookie_jar.clear()
        me = await self.request("GET", "/api/me", headers=headers)
        self.assertEqual(me["device"]["id"], value["device"]["id"])
        self.assertEqual(me["device"]["status"], "approved")
        self.assertEqual((await self.claim(await self.pairing(value)))["device"]["id"], value["device"]["id"])

    async def test_release_artifacts_require_approval_and_use_fixed_paths(self):
        self.assertEqual(await self.request("GET", "/api/client/release", self.a), {"available": False})
        self.assertEqual((await self.request("GET", "/api/me", self.a))["connector_release"], {"available": False})
        archive, installer = self.publish_release()
        for device in (None, self.pending):
            for endpoint in ("release", "download/archive", "download/installer"):
                await self.request("GET", "/api/client/" + endpoint, device, expected=403)
            await self.request("POST", "/api/client/installer", device, expected=403, json={})
        release = await self.request("GET", "/api/client/release", self.a)
        self.assertEqual(release["sha256"], hashlib.sha256(archive).hexdigest())
        for device in (self.a, self.admin):
            status = (await self.request("GET", "/api/me", device))["connector_release"]
            self.assertEqual(status, release)
            self.assertEqual(status["installer_url"], "/api/client/download/0.3.0/installer")
            self.assertEqual(status["pairing_url"], "/api/client/pairing/0.3.0")
            self.assertEqual(status["installer_filename"], "Install-Codex-Connector-0.3.0.sh")
        for name, content in (("archive", archive), ("installer", installer)):
            response = await self.client.get(
                "/api/client/download/0.3.0/" + name, headers=self.cookie(self.a)
            )
            self.assertEqual(response.status, 200)
            self.assertEqual(await response.read(), content)
            self.assertIn("attachment", response.headers["Content-Disposition"])
        for name in ("release.json", "network.json", "server.sqlite3", "ca-key.pem", "%2E%2E%2Fnetwork.json"):
            response = await self.client.get("/api/client/download/" + name, headers=self.cookie(self.a))
            self.assertEqual(response.status, 404)

    async def test_stale_release_urls_and_pairings_are_rejected(self):
        self.publish_release(version="0.3.0")
        pairing = await self.pairing()
        original_credentials = self.connector_credential_count()
        self.publish_release(version="0.3.1")
        for method, path in (
            ("GET", "/api/client/download/0.3.0/installer"),
            ("POST", "/api/client/installer/0.3.0"),
            ("POST", "/api/client/pairing/0.3.0"),
        ):
            response = await self.client.request(method, path, headers=self.cookie(self.a), json={})
            self.assertEqual(response.status, 409, await response.text())
        await self.claim(pairing, expected=409)
        self.assertEqual(self.connector_credential_count(), original_credentials)
        await self.claim(pairing, expected=403)
        current = self.service.client_access.release_info()
        response = await self.client.get(current["installer_url"], headers=self.cookie(self.a))
        self.assertEqual(response.status, 200, await response.text())

    async def test_invalid_release_manifest_cannot_break_me_or_advertise_an_update(self):
        directory = Path(self.temp.name) / "releases"
        directory.mkdir()
        archive = b"harmless archive fixture"
        (directory / "client.tar.gz").write_bytes(archive)
        (directory / "Install-Codex-Connector.sh").write_bytes(b"harmless installer fixture")
        manifest = directory / "release.json"
        invalid = (
            b"not json",
            json.dumps({"version": "../escape", "sha256": "0" * 64, "size": len(archive)}).encode(),
            json.dumps({"version": "0.3.0", "sha256": "0" * 64, "size": len(archive) + 1}).encode(),
            json.dumps({"version": "0.3.0", "sha256": "short", "size": len(archive)}).encode(),
        )
        for content in invalid:
            with self.subTest(content=content[:20]):
                manifest.write_bytes(content)
                self.assertEqual(await self.request("GET", "/api/client/release", self.a), {"available": False})
                me = await self.request("GET", "/api/me", self.a)
                self.assertEqual(me["connector_release"], {"available": False})

    async def test_maintenance_is_native_only_self_scoped_and_preserves_running_turn(self):
        worker_a = await self.worker(self.a, "a")
        await self.worker(self.b, "b")
        room = await self.room(self.a)
        waiting_room = await self.room(self.a, title="Next request")
        run = await self.ready_run(self.a, worker_a, room)
        await self.request("POST", "/api/client/maintenance", headers=self.cookie(self.a),
                           expected=403, json={"pause": True})
        paused = await self.request("POST", "/api/client/maintenance", self.a,
                                   json={"pause": True, "device_id": self.b["device"]["id"]})
        self.assertEqual(paused, {"paused": True, "active_runs": 1})
        self.assertTrue(self.service.runtime.peers[self.a["device"]["id"]].draining)
        self.assertFalse(self.service.runtime.peers[self.b["device"]["id"]].draining)
        self.assertEqual(self.service.store.get_run_internal(run["run"]["id"])["status"], "running")
        await self.request("POST", f'/api/rooms/{waiting_room["id"]}/runs', self.a, expected=400,
                           json={"text": "Wait for the maintenance window", "request_id": "while-paused"})
        self.assertEqual(len(self.service.store.list_active_runs(self.a["device"]["id"])), 1)
        self.assertNotIn("turn/interrupt", [call["method"] for call in worker_a.calls])
        await worker_a.delta("The existing response continues.")
        await worker_a.complete()
        await self.until(lambda: not self.service.store.list_active_runs(self.a["device"]["id"]))
        resumed = await self.request("POST", "/api/client/maintenance", self.a, json={"pause": False})
        self.assertEqual(resumed, {"paused": False, "active_runs": 0})
        self.assertTrue(self.service.runtime.connected(self.a["device"]["id"]))

    async def test_network_origin_and_maintenance_payload_guards(self):
        for headers in ({"Host": "192.168.1.56:8767"}, {"Origin": "https://attacker.invalid"},
                        {"Sec-Fetch-Site": "cross-site"},
                        {"Host": "192.168.1.56:8767", "X-Forwarded-Proto": "https"}):
            await self.request("GET", "/api/health", expected=403, headers=headers)
        for payload in ({}, {"pause": "true"}, [], {"pause": 1}):
            await self.request("POST", "/api/client/maintenance", self.a, expected=400, json=payload)
        self.service.network = None
        await self.request("POST", "/api/client/pairing", self.a, expected=503, json={})

    async def test_maintenance_pause_survives_worker_reconnect_and_blocks_new_runs(self):
        device_id = self.a["device"]["id"]
        original = await self.worker(self.a, "before-reconnect")
        room = await self.room(self.a)
        await self.request("POST", "/api/client/maintenance", self.a, json={"pause": True})
        await original.close()
        self.workers.remove(original)
        await self.until(lambda: device_id not in self.service.runtime.peers)
        socket, auth = await server_fixture.host_worker_socket(
            self.client, self.service, self.admin, self.a
        )
        self.assertEqual(auth["type"], "auth")
        await socket.send_json({"type": "ready", "models": server_fixture.MODELS, "cwd": "/home/reconnected"})
        reconnected = server_fixture.FakeWorker(socket, "after-reconnect")
        self.workers.append(reconnected)
        await self.until(lambda: device_id in self.service.runtime.peers and self.service.runtime.peers[device_id].ready)
        self.assertTrue(self.service.runtime.peers[device_id].draining)
        self.assertFalse(self.service.runtime.connected(device_id))
        await self.request("POST", f'/api/rooms/{room["id"]}/runs', self.a, expected=400,
                           json={"text": "Keep waiting while the connector updates", "request_id": "after-reconnect"})
        self.assertEqual(self.service.store.list_active_runs(device_id), [])
        self.assertNotIn("turn/start", [call["method"] for call in reconnected.calls])
        await self.request("POST", "/api/client/maintenance", self.a, json={"pause": False})
        self.assertTrue(self.service.runtime.connected(device_id))
        await self.ready_run(self.a, reconnected, room, "after-explicit-resume")
        await reconnected.complete()
        await self.until(lambda: not self.service.store.list_active_runs(device_id))

    async def test_fresh_runtime_restores_durable_pause_until_explicit_resume(self):
        device_id = self.a["device"]["id"]
        await self.worker(self.a, "persistent-pause")
        await self.request("POST", "/api/client/maintenance", self.a, json={"pause": True})
        restored = Runtime(self.service.store, server_fixture.FakeAuth())
        restored_service = SimpleNamespace(store=self.service.store, runtime=restored)
        access = ClientAccess(restored_service, Path(self.temp.name))
        self.assertIn(device_id, restored.draining_devices)
        restored.peers[device_id] = Peer(device_id, SimpleNamespace(closed=False), ready=True)
        self.assertFalse(restored.connected(device_id))
        request = MagicMock()
        request.headers = {"Authorization": "Bearer " + self.a["token"]}
        request.__getitem__.return_value = self.a["device"]
        request.json = AsyncMock(return_value={"pause": False})
        response = await access.maintenance(request)
        self.assertEqual(json.loads(response.body), {"paused": False, "active_runs": 0})
        self.assertNotIn(device_id, restored.draining_devices)
        self.assertTrue(restored.connected(device_id))
        again = Runtime(self.service.store, server_fixture.FakeAuth())
        ClientAccess(SimpleNamespace(store=self.service.store, runtime=again), Path(self.temp.name))
        self.assertNotIn(device_id, again.draining_devices)

    async def test_https_lan_origin_is_allowed_and_enrollment_cookie_is_secure(self):
        site = web.TCPSite(self.client.server.runner, "127.0.0.1", 0, ssl_context=ssl_context(self.network))
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        endpoint = f"https://127.0.0.1:{port}"
        trusted = ssl.create_default_context(cafile=str(self.network["ca"]))
        host = self.network["public_origin"].removeprefix("https://")
        try:
            async with aiohttp.ClientSession() as client:
                async with client.post(endpoint + "/api/enroll", ssl=trusted,
                                       server_hostname=self.network["lan_address"],
                                       headers={"Host": host, "Origin": self.network["public_origin"]},
                                       json={"name": "Secure laptop"}) as response:
                    self.assertEqual(response.status, 201)
                    self.assertTrue(response.cookies[COOKIE]["secure"])
                    self.assertTrue(response.cookies[COOKIE]["httponly"])
                async with client.get(endpoint + "/api/health", ssl=trusted,
                                      server_hostname=self.network["lan_address"],
                                      headers={"Host": "192.168.1.56:9999"}) as response:
                    self.assertEqual(response.status, 403)
        finally:
            await site.stop()


if __name__ == "__main__":
    unittest.main()
