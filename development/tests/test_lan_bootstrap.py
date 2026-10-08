"""CODEX: Offline packaging and launcher checks without SSH or real credentials."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import io
import json
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from project_layout import configure_imports, source_file
configure_imports(__file__)

import build_lan_bundle
import launch_lan_client
import setup_lan_client
from codex_lan.auth import AuthProvider


class LanBootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)
        self.source, self.home = self.path / "bundle-source", self.path / "fake home"
        self.source.mkdir()
        self.home.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def bundle_fixture(self, wheels=True):
        for name in (*build_lan_bundle.CLIENT_FILES, "bin/codex", "bin/codex-code-mode-host", "pip.pyz"):
            file = self.source / name
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text("public fixture " + name)
        (self.source / "bin/codex").chmod(0o755)
        (self.source / "bin/codex-code-mode-host").chmod(0o755)
        (self.source / ".lan-wheels").mkdir()
        if wheels:
            (self.source / ".lan-wheels/aiohttp-3.14.3-cp310-cp310-manylinux2014_x86_64.whl").write_bytes(b"fake public wheel")

    def build(self, output):
        argv = ["build_lan_bundle.py", "--output", str(output), "--codex", str(self.source / "bin/codex"), "--pip", str(self.source / "pip.pyz")]
        with mock.patch.object(build_lan_bundle, "__file__", str(self.source / "build_lan_bundle.py")), mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(io.StringIO()):
            build_lan_bundle.main()

    def test_bundle_contains_both_public_binaries_and_never_local_state(self):
        self.bundle_fixture()
        for name in ("auth.json", "admin.token", "device.token", "server.sqlite3", "connection.json"):
            (self.source / name).write_text("SECRET-STATE-DO-NOT-PACK")
        output = self.path / "client.tar.gz"
        self.build(output)
        with tarfile.open(output) as archive:
            members = archive.getmembers()
            names = {member.name for member in members}
            self.assertIn("Codex-LAN-Client/bin/codex", names)
            self.assertIn("Codex-LAN-Client/bin/codex-code-mode-host", names)
            self.assertIn("Codex-LAN-Client/codex_lan/egress.py", names)
            self.assertIn("Codex-LAN-Client/codex_lan/recovery.py", names)
            self.assertIn("Codex-LAN-Client/codex_lan/transport.py", names)
            self.assertTrue(any("/wheels/" in name for name in names))
            for member in members:
                self.assertTrue(member.isfile())
                self.assertNotIn(b"SECRET-STATE-DO-NOT-PACK", archive.extractfile(member).read())
                self.assertNotIn("..", Path(member.name).parts)
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            self.build(output)

    def test_bundle_rejects_missing_wheels(self):
        self.bundle_fixture(wheels=False)
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            self.build(self.path / "incomplete.tar.gz")

    def test_setup_preserves_existing_pairing_before_any_install_mutation(self):
        target = self.home / ".local/share/codex-lan-client"
        target.mkdir(parents=True)
        old = {"server": "owner@192.0.2.56", "port": 8766}
        (target / "connection.json").write_text(json.dumps(old))
        (target / "device.token").write_text("preserve-device-identity")
        argv = ["setup_lan_client.py", "--server", "owner@192.0.2.57", "--no-launch"]
        with mock.patch.object(sys, "argv", argv), \
             mock.patch.object(sys, "version_info", (3, 10)), \
             mock.patch.object(setup_lan_client.platform, "machine", return_value="x86_64"), \
             mock.patch.object(Path, "home", return_value=self.home), \
             mock.patch.object(setup_lan_client.shutil, "which", return_value="/usr/bin/ssh"), \
             mock.patch.object(setup_lan_client.shutil, "copy2") as copy, \
             mock.patch.object(setup_lan_client.subprocess, "run") as command, \
             contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            setup_lan_client.main()
        copy.assert_not_called()
        command.assert_not_called()
        self.assertEqual(json.loads((target / "connection.json").read_text()), old)
        self.assertEqual((target / "device.token").read_text(), "preserve-device-identity")

    def test_setup_does_not_overwrite_running_client(self):
        argv = ["setup_lan_client.py", "--server", "owner@192.0.2.56", "--no-launch"]
        with mock.patch.object(sys, "argv", argv), \
             mock.patch.object(sys, "version_info", (3, 10)), \
             mock.patch.object(setup_lan_client.platform, "machine", return_value="x86_64"), \
             mock.patch.object(Path, "home", return_value=self.home), \
             mock.patch.object(setup_lan_client.shutil, "which", return_value="/usr/bin/ssh"), \
             mock.patch.object(setup_lan_client.shutil, "copy2") as copy, \
             mock.patch.object(setup_lan_client.subprocess, "run", return_value=SimpleNamespace(returncode=0)) as command, \
             contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            setup_lan_client.main()
        copy.assert_not_called()
        self.assertEqual(len(command.call_args_list), 1)
        self.assertIn("is-active", command.call_args.args[0])
        self.assertFalse((self.home / ".local/share/codex-lan-client").exists())

    def test_setup_rejects_wrong_architecture_before_writing_files(self):
        with mock.patch.object(sys, "argv", ["setup_lan_client.py", "--server", "owner@192.0.2.56", "--no-launch"]), \
             mock.patch.object(sys, "version_info", (3, 12)), \
             mock.patch.object(setup_lan_client.platform, "machine", return_value="aarch64"), \
             mock.patch.object(Path, "home", return_value=self.home), \
             mock.patch.object(setup_lan_client.shutil, "which", return_value="/usr/bin/ssh"), \
             mock.patch.object(setup_lan_client.shutil, "copy2") as copy, \
             mock.patch.object(setup_lan_client.subprocess, "run") as command, \
             contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            setup_lan_client.main()
        copy.assert_not_called()
        command.assert_not_called()
        self.assertFalse((self.home / ".local/share/codex-lan-client").exists())

    def test_reopen_reuses_tunnel_and_never_restarts_active_worker_or_prints_token(self):
        root = self.home / ".local/share/codex-lan-client"
        (root / "bin").mkdir(parents=True)
        (root / "venv/bin").mkdir(parents=True)
        (root / "venv/bin/python").symlink_to(sys.executable)
        (root / "bin/codex").write_bytes(b"fake binary")
        (root / "connection.json").write_text(json.dumps({"server": "owner@192.0.2.56", "port": 8766}))
        token = "FAKE-DEVICE-TOKEN-DO-NOT-LOG"
        (root / "device.token").write_text(token)
        (root / "device.token").chmod(0o600)
        responses = [io.BytesIO(b'{"ok":true}'), io.BytesIO(b'{"device":{"name":"Test","status":"approved","verification_code":"ABCD-EF01"}}')]
        output = io.StringIO()
        with mock.patch.object(launch_lan_client, "__file__", str(root / "launch_lan_client.py")), \
             mock.patch.object(Path, "home", return_value=self.home), \
             mock.patch.object(launch_lan_client.subprocess, "run", return_value=SimpleNamespace(returncode=0)) as command, \
             mock.patch.object(launch_lan_client.urllib.request.OpenerDirector, "open", side_effect=responses), \
             mock.patch.object(launch_lan_client.webbrowser, "open", return_value=True) as browser, \
             contextlib.redirect_stdout(output):
            launch_lan_client.main()
        calls = [call.args[0] for call in command.call_args_list]
        self.assertFalse(any("-fNT" in call for call in calls))
        self.assertFalse(any("restart" in call or "stop" in call for call in calls))
        self.assertTrue(any("start" in call and "codex-lan-client.service" in call for call in calls))
        self.assertNotIn(token, output.getvalue())
        self.assertNotIn(token, json.dumps(calls))
        unit = (self.home / ".config/systemd/user/codex-lan-client.service").read_text()
        self.assertNotIn(token, unit)
        self.assertIn("--token-file", unit)
        self.assertIn('"' + str(root / "device.token") + '"', unit)
        self.assertEqual(browser.call_args.args[0], "http://127.0.0.1:8766/#login=" + token)
        if shutil.which("systemd-analyze"):
            verified = subprocess.run([
                "systemd-analyze", "--user", "verify",
                str(self.home / ".config/systemd/user/codex-lan-client.service"),
            ], capture_output=True, text=True)
            self.assertEqual(verified.returncode, 0, verified.stderr)


class LanAuthTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)

    async def asyncTearDown(self):
        self.temp.cleanup()

    def write_auth(self, expires):
        claims = {"exp": expires, "https://api.openai.com/auth": {"chatgpt_account_id": "fake-account", "chatgpt_plan_type": "test"}}
        encoded = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
        access = "fake." + encoded + ".not-signed"
        (self.path / "auth.json").write_text(json.dumps({"tokens": {
            "access_token": access, "refresh_token": "REFRESH-DO-NOT-SEND", "id_token": "ID-DO-NOT-SEND", "account_id": "fake-account"
        }}))
        return access

    async def test_provider_sends_only_access_account_and_plan(self):
        access = self.write_auth(time.time() + 3600)
        provider = AuthProvider(self.path, codex="must-not-run")
        with mock.patch.object(provider, "_refresh", new_callable=mock.AsyncMock) as refresh:
            result = await provider.tokens()
        refresh.assert_not_awaited()
        self.assertEqual(result, {"accessToken": access, "chatgptAccountId": "fake-account", "chatgptPlanType": "test"})
        self.assertNotIn("REFRESH-DO-NOT-SEND", json.dumps(result))
        self.assertNotIn("ID-DO-NOT-SEND", json.dumps(result))

    async def test_expired_token_is_refreshed_once_under_concurrent_requests(self):
        self.write_auth(time.time() - 100)
        provider = AuthProvider(self.path, codex="must-not-run")

        async def refreshed():
            self.write_auth(time.time() + 3600)

        with mock.patch.object(provider, "_refresh", side_effect=refreshed) as refresh:
            results = await asyncio.gather(*(provider.tokens() for _ in range(5)))
        self.assertEqual(refresh.await_count, 1)
        self.assertEqual(len({result["accessToken"] for result in results}), 1)

    async def test_malformed_signin_is_a_safe_error(self):
        provider = AuthProvider(self.path, codex="must-not-run")
        for value in ("not JSON", "{}", '{"tokens":{"access_token":"malformed"}}'):
            (self.path / "auth.json").write_text(value)
            with self.assertRaises(RuntimeError) as failure:
                await provider.tokens()
            self.assertNotIn(str(self.path), str(failure.exception))


if __name__ == "__main__":
    unittest.main()
