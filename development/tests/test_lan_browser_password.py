"""CODEX: Browser-password authentication, session isolation, and revocation regressions."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from aiohttp import WSServerHandshakeError
from aiohttp.test_utils import TestClient, TestServer

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from project_layout import configure_imports, source_file
configure_imports(__file__)

from codex_lan.server import COOKIE, SERVICE, create_app


class FakeAuth:
    async def tokens(self, force=False):
        return {"accessToken": "FAKE-NOT-A-CREDENTIAL", "chatgptAccountId": "fake-account"}


class BrowserPasswordSecurityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)
        self.app = create_app(self.path, auth=FakeAuth())
        self.service = self.app[SERVICE]
        self.client = TestClient(TestServer(self.app))
        await self.client.start_server()
        self.admin = self.service.store.bootstrap_admin("Server")
        self.admin_id = self.admin["device"]["id"]
        self.login_name = "example"
        self.password = "correct horse battery staple 42"

    async def asyncTearDown(self):
        await self.client.close()
        self.temp.cleanup()

    async def request(self, method, path, expected=200, token=None, **kwargs):
        headers = dict(kwargs.pop("headers", {}))
        if token:
            headers["Authorization"] = "Bearer " + token
        response = await self.client.request(method, path, headers=headers, **kwargs)
        try:
            payload = await response.json()
        except (json.JSONDecodeError, ValueError):
            payload = {"text": await response.text()}
        self.assertEqual(response.status, expected, payload)
        return response, payload

    async def set_password(self, password=None, login_name=None):
        return await self.request(
            "PUT",
            "/api/me/browser-login",
            token=self.admin["token"],
            json={
                "login_name": login_name or self.login_name,
                "password": password or self.password,
                "revoke_other_sessions": True,
            },
        )

    def database_dump(self):
        return "\n".join(self.service.store.db.iterdump())

    async def test_password_uses_scrypt_and_plaintext_is_never_persisted_or_returned(self):
        real_scrypt = hashlib.scrypt
        calls = []

        def observed_scrypt(password, **kwargs):
            calls.append((password, dict(kwargs)))
            return real_scrypt(password, **kwargs)

        with mock.patch("codex_lan.store.hashlib.scrypt", side_effect=observed_scrypt, create=True):
            _, configured = await self.set_password()

        self.assertTrue(calls, "Password verifiers must be derived with hashlib.scrypt")
        password_bytes, parameters = calls[0]
        self.assertEqual(password_bytes, self.password.encode("utf-8"))
        self.assertIsInstance(parameters.get("salt"), bytes)
        self.assertGreaterEqual(len(parameters["salt"]), 16)
        self.assertGreaterEqual(parameters.get("n", 0), 2**14)
        self.assertGreaterEqual(parameters.get("r", 0), 8)
        self.assertGreaterEqual(parameters.get("p", 0), 1)
        self.assertGreaterEqual(parameters.get("dklen", 0), 32)

        _, me = await self.request("GET", "/api/me", token=self.admin["token"])
        _, devices = await self.request("GET", "/api/admin/devices", token=self.admin["token"])
        exposed = json.dumps([configured, me, devices], ensure_ascii=False)
        self.assertNotIn(self.password, exposed)
        for forbidden in ("password_hash", "password_salt", "password_verifier", "lookup_tag"):
            self.assertNotIn(forbidden, exposed)
        self.assertEqual(me["device"]["login_name"], self.login_name)
        self.assertTrue(me["device"]["password_login_enabled"])

        dump = self.database_dump()
        self.assertNotIn(self.password, dump)
        for path in (self.path / "server.sqlite3", self.path / "server.sqlite3-wal"):
            if path.exists():
                self.assertNotIn(self.password.encode("utf-8"), path.read_bytes())

    async def test_password_login_mints_an_opaque_persistent_browser_session(self):
        await self.set_password()
        response, logged_in = await self.request(
            "POST",
            "/api/session",
            json={"login_name": self.login_name, "password": self.password, "remember": True},
        )
        self.assertEqual(logged_in["device"]["id"], self.admin_id)
        cookie = response.cookies[COOKIE]
        session_token = cookie.value
        self.assertTrue(cookie["httponly"])
        self.assertEqual(cookie["samesite"], "Strict")
        self.assertEqual(cookie["path"], "/")
        self.assertGreater(int(cookie["max-age"]), 0)
        self.assertGreaterEqual(len(session_token), 32)
        self.assertNotEqual(session_token, self.password)
        self.assertNotEqual(session_token, self.admin["token"])
        self.assertNotIn(session_token, self.database_dump())

        _, me = await self.request("GET", "/api/me")
        self.assertEqual(me["device"]["id"], self.admin_id)
        self.assertNotIn(self.password, json.dumps(logged_in, ensure_ascii=False))

    async def test_enrollment_uses_an_opaque_persistent_browser_session(self):
        response, enrolled = await self.request(
            "POST", "/api/enroll", expected=201, json={"name": "New Ubuntu"},
        )
        cookie = response.cookies[COOKIE]
        self.assertGreater(int(cookie["max-age"]), 0)
        self.assertTrue(cookie["httponly"])
        self.assertNotEqual(cookie.value, enrolled["token"])
        self.assertNotIn(cookie.value, self.database_dump())

    async def test_wrong_password_is_generic_and_repeated_failures_are_rate_limited(self):
        await self.set_password()
        _, wrong_known = await self.request(
            "POST",
            "/api/session",
            expected=403,
            json={"login_name": self.login_name, "password": "definitely-wrong", "remember": True},
        )
        _, wrong_unknown = await self.request(
            "POST",
            "/api/session",
            expected=403,
            json={"login_name": "no-such-login", "password": "definitely-wrong", "remember": True},
        )
        self.assertEqual(wrong_known.get("error"), wrong_unknown.get("error"))
        self.assertNotIn(self.login_name, json.dumps(wrong_known))

        limited = None
        for _ in range(40):
            response = await self.client.post(
                "/api/session",
                json={"login_name": self.login_name, "password": "definitely-wrong", "remember": True},
            )
            if response.status == 429:
                limited = response
                break
            self.assertEqual(response.status, 403, await response.text())
        self.assertIsNotNone(limited, "Repeated password failures must eventually be throttled")
        self.assertTrue(limited.headers.get("Retry-After"))
        response = await self.client.post(
            "/api/session",
            json={"login_name": self.login_name, "password": self.password, "remember": True},
        )
        self.assertEqual(response.status, 429, await response.text())

    async def test_login_rate_limit_survives_server_restart(self):
        await self.set_password()
        for _ in range(5):
            response = await self.client.post(
                "/api/session",
                json={"login_name": self.login_name, "password": "wrong", "remember": True},
            )
            self.assertEqual(response.status, 403, await response.text())

        await self.client.close()
        self.app = create_app(self.path, auth=FakeAuth())
        self.service = self.app[SERVICE]
        self.client = TestClient(TestServer(self.app))
        await self.client.start_server()
        response = await self.client.post(
            "/api/session",
            json={"login_name": self.login_name, "password": self.password, "remember": True},
        )
        self.assertEqual(response.status, 429, await response.text())
        self.assertTrue(response.headers.get("Retry-After"))

    async def test_oversize_and_malformed_login_bodies_consume_rate_limit_attempts(self):
        await self.set_password()
        requests = (
            (b"x" * 4097, 413),
            (b"{", 400),
            (b"x" * 4097, 413),
            (b"[", 400),
            (b"x" * 4097, 413),
        )
        for payload, expected in requests:
            response = await self.client.post(
                "/api/session", data=payload, headers={"Content-Type": "application/json"}
            )
            self.assertEqual(response.status, expected, await response.text())

        self.assertEqual(
            self.service.store.db.execute("SELECT COUNT(*) FROM login_attempts").fetchone()[0],
            5,
        )
        response = await self.client.post(
            "/api/session",
            json={"login_name": self.login_name, "password": self.password, "remember": True},
        )
        self.assertEqual(response.status, 429, await response.text())
        self.assertTrue(response.headers.get("Retry-After"))

    async def test_logout_revokes_the_server_side_session_instead_of_only_deleting_cookie(self):
        await self.set_password()
        login, _ = await self.request(
            "POST",
            "/api/session",
            json={"login_name": self.login_name, "password": self.password, "remember": True},
        )
        session_token = login.cookies[COOKIE].value
        await self.request("GET", "/api/me")
        logout, _ = await self.request("POST", "/api/logout")
        self.assertIn(COOKIE, logout.cookies)

        replay = await self.client.get(
            "/api/me", headers={"Cookie": f"{COOKIE}={session_token}"}
        )
        self.assertEqual(replay.status, 403, await replay.text())

    async def test_password_rotation_revokes_old_password_and_existing_browser_sessions(self):
        await self.set_password()
        login, _ = await self.request(
            "POST",
            "/api/session",
            json={"login_name": self.login_name, "password": self.password, "remember": True},
        )
        old_session = login.cookies[COOKIE].value
        replacement = "a different memorable password 73"
        await self.set_password(password=replacement)

        self.client.session.cookie_jar.clear()
        replay = await self.client.get(
            "/api/me", headers={"Cookie": f"{COOKIE}={old_session}"}
        )
        self.assertEqual(replay.status, 403, await replay.text())
        await self.request(
            "POST",
            "/api/session",
            expected=403,
            json={"login_name": self.login_name, "password": self.password, "remember": True},
        )
        _, fresh = await self.request(
            "POST",
            "/api/session",
            json={"login_name": self.login_name, "password": replacement, "remember": True},
        )
        self.assertEqual(fresh["device"]["id"], self.admin_id)
        _, machine_auth = await self.request("GET", "/api/me", token=self.admin["token"])
        self.assertEqual(machine_auth["device"]["id"], self.admin_id)

    async def test_browser_session_is_never_accepted_as_a_worker_bearer_credential(self):
        await self.set_password()
        login, _ = await self.request(
            "POST",
            "/api/session",
            json={"login_name": self.login_name, "password": self.password, "remember": True},
        )
        browser_session = login.cookies[COOKIE].value
        with self.assertRaises(WSServerHandshakeError) as failure:
            await self.client.ws_connect(
                "/api/worker", headers={"Authorization": "Bearer " + browser_session}
            )
        self.assertEqual(failure.exception.status, 403)


class LegacyCredentialMigrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)
        self.admin_id = "legacy-admin"
        self.primary_token = "legacy-primary-recovery-token"
        database = sqlite3.connect(self.path / "server.sqlite3")
        database.executescript(
            """
            CREATE TABLE devices (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                token_hash TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL CHECK(status IN ('pending','approved','revoked','rejected')),
                is_admin INTEGER NOT NULL DEFAULT 0,
                max_concurrent INTEGER NOT NULL DEFAULT 1 CHECK(max_concurrent > 0),
                token_budget INTEGER CHECK(token_budget >= 0),
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                last_seen_at REAL
            );
            CREATE UNIQUE INDEX one_admin ON devices(is_admin) WHERE is_admin = 1;
            """
        )
        now = time.time()
        database.execute(
            "INSERT INTO devices(id,name,token_hash,status,is_admin,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (
                self.admin_id,
                "Legacy server",
                hashlib.sha256(self.primary_token.encode()).hexdigest(),
                "approved",
                1,
                now,
                now,
            ),
        )
        database.commit()
        database.close()

        self.app = create_app(self.path, auth=FakeAuth())
        self.service = self.app[SERVICE]
        self.client = TestClient(TestServer(self.app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self.temp.cleanup()

    def browser_session_count(self):
        return self.service.store.db.execute("SELECT COUNT(*) FROM browser_sessions").fetchone()[0]

    async def test_old_database_migrates_and_primary_token_remains_a_recovery_login(self):
        tables = {
            row[0]
            for row in self.service.store.db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        self.assertTrue(
            {"browser_logins", "browser_sessions", "device_credentials", "login_attempts"}
            <= tables
        )
        columns = {
            row[1] for row in self.service.store.db.execute("PRAGMA table_info(devices)")
        }
        self.assertTrue({"connector_version", "connector_confirmed_at"} <= columns)
        self.assertEqual(self.service.store.get_device(self.admin_id)["connector_version"], "")
        self.assertIsNone(self.service.store.get_device(self.admin_id)["connector_confirmed_at"])
        confirmed = self.service.store.confirm_connector(self.admin_id, "0.3.7")
        self.assertEqual(confirmed["connector_version"], "0.3.7")
        self.assertIsInstance(confirmed["connector_confirmed_at"], float)
        self.assertEqual(
            self.service.store.authenticate(self.primary_token)["id"], self.admin_id
        )

        response = await self.client.post(
            "/api/session", json={"token": self.primary_token, "remember": True}
        )
        self.assertEqual(response.status, 200, await response.text())
        payload = await response.json()
        self.assertEqual(payload["device"]["id"], self.admin_id)
        session_token = response.cookies[COOKIE].value
        self.assertNotEqual(session_token, self.primary_token)
        self.assertNotIn(session_token, "\n".join(self.service.store.db.iterdump()))

        me = await self.client.get("/api/me")
        self.assertEqual(me.status, 200, await me.text())
        self.assertEqual((await me.json())["device"]["id"], self.admin_id)

    async def test_connector_credential_is_not_accepted_as_a_browser_cookie(self):
        connector = self.service.store.issue_device_credential(
            self.admin_id, kind="connector"
        )
        self.assertEqual(self.service.store.authenticate(connector)["id"], self.admin_id)
        self.client.session.cookie_jar.clear()

        response = await self.client.get(
            "/api/me", headers={"Cookie": f"{COOKIE}={connector}"}
        )
        self.assertEqual(response.status, 403, await response.text())
        self.assertEqual(self.browser_session_count(), 0)

    async def test_connector_credential_cannot_mint_a_browser_session(self):
        connector = self.service.store.issue_device_credential(
            self.admin_id, kind="connector"
        )
        self.assertEqual(self.service.store.authenticate(connector)["id"], self.admin_id)
        self.client.session.cookie_jar.clear()

        response = await self.client.post(
            "/api/session", json={"token": connector, "remember": True}
        )
        self.assertEqual(response.status, 403, await response.text())
        self.assertNotIn(COOKIE, response.cookies)
        self.assertEqual(self.browser_session_count(), 0)


if __name__ == "__main__":
    unittest.main()
