"""CODEX: Host setup authorization and fixed actions never restart active conversations."""

import asyncio
import json
import signal
import unittest
from unittest.mock import AsyncMock, patch

import test_lan_server as fixture


SYSTEM = {"services": [
    {"id": name, "label": name, "active": True, "available": True, "enabled": True}
    for name in ("codex-lan-server.service", "codex-lan-host-worker.service", "codex-lan-stay-awake.service")
], "lan_addresses": ["192.168.1.20"]}


class HostSetupTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.LanServerTests.asyncSetUp
    asyncTearDown = fixture.LanServerTests.asyncTearDown
    request = fixture.LanServerTests.request

    async def test_only_admin_can_inspect_or_change_host_settings(self):
        with patch("codex_lan.host_setup.system_status", return_value=SYSTEM):
            await self.request("GET", "/api/admin/host-setup", self.a, expected=403)
            await self.request("POST", "/api/admin/host-setup", self.a, expected=403, json={"action": "enable_startup"})
            await self.request("GET", "/api/admin/host-setup/ca", self.a, expected=403)
            result = await self.request("GET", "/api/admin/host-setup", self.admin)
            self.assertEqual(result["network"]["listen"], "127.0.0.1")
            self.assertNotIn(self.admin["token"], json.dumps(result))
            self.assertFalse(result["connector"]["available"])
            self.assertEqual(set(result["codex"]), {"available", "signed_in"})
            self.assertEqual(result["network"]["https_addresses"], ["192.168.1.20", "127.0.0.2"])
            self.assertIsNone(result["network"]["https_transport"])

    async def test_relay_bootstrap_is_admin_only_and_never_echoes_password(self):
        setup = self.service.host_setup
        self.service.network = {"lan_address": "127.0.0.2", "port": 8767,
                                "public_origin": "https://127.0.0.2:8767", "ca": self.service.data_dir / "ca.pem"}
        setup.https_site = object()
        relay = {"supported": True, "bootstrap_available": True, "configured": False,
                 "manageable": True, "active": False, "enabled": False, "public_key": None}
        payload = {"action": "bootstrap_relay", "admin_user": "root", "server": "relay.example",
                   "password": "example-password", "host_key_fingerprint": "SHA256:" + "A" * 43}
        with patch("codex_lan.host_setup.system_status", return_value=SYSTEM), \
                patch("codex_lan.host_setup.relay_setup.status", return_value=relay), \
                patch("codex_lan.host_setup.relay_setup.bootstrap") as bootstrap:
            await self.request("POST", "/api/admin/host-setup", self.a, expected=403, json=payload)
            bootstrap.assert_not_called()
            response = await self.request("POST", "/api/admin/host-setup", self.admin, expected=202, json=payload)
            self.assertNotIn(payload["password"], json.dumps(response))
            await setup.task
            result = await self.request("GET", "/api/admin/host-setup", self.admin)
            self.assertNotIn(payload["password"], json.dumps(result))
            self.assertEqual(setup.job["status"], "completed")
            bootstrap.assert_called_once()

    async def test_ssh_tunnel_https_is_explicit_even_without_lan_interfaces(self):
        setup = self.service.host_setup
        setup.runner = object()
        config = {"lan_address": "127.0.0.2", "port": 8767, "public_origin": "https://127.0.0.2:8767",
                  "ca": self.service.data_dir / "tls/ca.pem"}
        with patch("codex_lan.host_setup.system_status", return_value={**SYSTEM, "lan_addresses": []}), \
                patch("codex_lan.host_setup.lan_addresses", return_value=[]), \
                patch.object(setup, "_perform", new_callable=AsyncMock) as perform:
            payload = {"action": "configure_https", "address": "127.0.0.2", "port": 8767}
            await self.request("POST", "/api/admin/host-setup", self.admin, expected=202, json=payload)
            await setup.task
            perform.assert_awaited_once_with(payload)
            self.service.network = config
            status = await self.request("GET", "/api/admin/host-setup", self.admin)
            self.assertEqual(status["network"]["https_transport"], "ssh_tunnel")
            self.assertEqual(status["network"]["https_addresses"], ["127.0.0.2"])
            self.assertEqual(status["network"]["lan_addresses"], [])
            self.assertIn("SSH tunnel access", " ".join(status["notices"]))

    async def test_malformed_release_manifest_does_not_break_setup(self):
        release = self.service.data_dir / "releases"
        release.mkdir(exist_ok=True)
        (release / "release.json").write_text("[]")
        with patch("codex_lan.host_setup.system_status", return_value=SYSTEM):
            result = await self.request("GET", "/api/admin/host-setup", self.admin)
            self.assertFalse(result["connector"]["available"])

    async def test_failed_https_bind_rolls_back_only_a_new_configuration(self):
        setup = self.service.host_setup
        filename = self.service.data_dir / "network.json"
        config = {"lan_address": "192.168.1.20", "port": 8767}
        def prepare(*arguments):
            filename.write_text(json.dumps(config))
            filename.chmod(0o600)
            return config
        with patch("codex_lan.https_setup.prepare_network", side_effect=prepare), patch.object(setup, "start_network_site", side_effect=OSError("Occupied port")):
            await setup._perform({"action": "configure_https", "address": "192.168.1.20", "port": 8767})
            self.assertEqual(setup.job["status"], "failed")
            self.assertFalse(filename.exists())
            prepare()
            await setup._perform({"action": "configure_https", "address": "192.168.1.20", "port": 8767})
            self.assertTrue(filename.exists())

    async def test_unknown_commands_and_unassigned_network_addresses_are_rejected(self):
        setup = self.service.host_setup
        setup.runner = object()
        with patch("codex_lan.host_setup.system_status", return_value=SYSTEM), patch("codex_lan.host_setup.lan_addresses", return_value=["192.168.1.20"]):
            for payload in ({"action": "restart"}, {"action": "enable_startup", "command": "touch unsafe"},
                            {"action": "configure_https", "address": "192.168.1.99", "port": 8767},
                            {"action": "configure_https", "address": "0.0.0.0", "port": 8767},
                            {"action": "configure_https", "address": "127.0.0.1", "port": 8767},
                            {"action": "configure_https", "address": "127.0.0.3", "port": 8767},
                            {"action": "configure_https", "address": "192.168.1.20", "port": 8765}):
                await self.request("POST", "/api/admin/host-setup", self.admin, expected=400, json=payload)
            self.assertIsNone(setup.task)
            self.assertFalse((self.service.data_dir / "network.json").exists())

    async def test_startup_enables_units_without_stopping_or_restarting_them(self):
        calls = []
        def run(*arguments):
            calls.append(arguments)
            return 0, ""
        with patch("codex_lan.host_setup.system_status", return_value=SYSTEM), patch("codex_lan.host_setup.command", side_effect=run):
            await self.request("POST", "/api/admin/host-setup", self.admin, expected=202, json={"action": "enable_startup"})
            await self.service.host_setup.task
            self.assertEqual(self.service.host_setup.job["status"], "completed")
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0][:3], ("systemctl", "--user", "enable"))
            self.assertNotIn("--now", calls[0])
            self.assertNotIn("restart", calls[0])

    async def test_setup_is_single_flight_and_does_not_block_health(self):
        started, release = asyncio.Event(), asyncio.Event()
        async def slow(payload):
            started.set()
            await release.wait()
        with patch("codex_lan.host_setup.system_status", return_value=SYSTEM), patch.object(self.service.host_setup, "_perform", side_effect=slow):
            await self.request("POST", "/api/admin/host-setup", self.admin, expected=202, json={"action": "enable_startup"})
            await started.wait()
            await self.request("POST", "/api/admin/host-setup", self.admin, expected=409, json={"action": "enable_startup"})
            response = await asyncio.wait_for(self.request("GET", "/api/health", self.admin), timeout=1)
            self.assertTrue(response["ok"])
            release.set()
            await self.service.host_setup.task

    async def test_connector_cancellation_stops_its_dedicated_process_group(self):
        setup = self.service.host_setup
        for failure in (asyncio.CancelledError(), asyncio.TimeoutError()):
            process = AsyncMock()
            process.pid = 12345
            process.wait.side_effect = [failure, 0]
            with patch.object(setup, "helper_available", return_value=True), \
                    patch("codex_lan.host_setup.asyncio.create_subprocess_exec", return_value=process) as start, \
                    patch("codex_lan.host_setup.os.killpg") as stop:
                if isinstance(failure, asyncio.CancelledError):
                    with self.assertRaises(asyncio.CancelledError):
                        await setup._perform({"action": "prepare_connector"})
                    self.assertEqual(setup.job["status"], "interrupted")
                else:
                    await setup._perform({"action": "prepare_connector"})
                    self.assertEqual(setup.job["status"], "failed")
                self.assertTrue(start.call_args.kwargs["start_new_session"])
                stop.assert_called_once_with(process.pid, signal.SIGKILL)
                self.assertEqual(process.wait.await_count, 2)

    async def test_lan_listener_can_be_added_without_replacing_the_http_runner(self):
        setup = self.service.host_setup
        setup.runner = object()
        config = {"lan_address": "192.168.1.20", "port": 8767, "public_origin": "https://192.168.1.20:8767"}
        site = AsyncMock()
        with patch("codex_lan.host_setup.ssl_context", return_value="fixture-context"), patch("codex_lan.host_setup.web.TCPSite", return_value=site) as factory:
            await setup.start_network_site(config)
            factory.assert_called_once_with(setup.runner, "192.168.1.20", 8767, ssl_context="fixture-context")
            site.start.assert_awaited_once()
            self.assertIs(self.service.network, config)
            await setup.start_network_site(config)
            self.assertEqual(factory.call_count, 1)


if __name__ == "__main__":
    unittest.main()
