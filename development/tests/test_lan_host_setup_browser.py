"""CODEX: Host setup stays administrator-only, survives reload, and submits bounded actions."""

from __future__ import annotations

import copy
import unittest

from playwright.async_api import expect

import test_lan_browser as browser_fixture


HOST_SETUP = {
    "supported": True,
    "host": {"platform": "Linux", "hostname": "portable-host"},
    "network": {
        "listen": "127.0.0.1", "port": 8765, "port_https": 8767,
        "local_origin": "http://127.0.0.1:8765", "public_origin": "https://192.168.1.20:8767",
        "https_enabled": True, "lan_addresses": ["192.168.1.20", "10.0.0.3"],
        "ca_fingerprint": "AB:CD:EF",
    },
    "services": [{"id": "server", "label": "Server", "active": True, "enabled": True, "available": True}],
    "keep_awake": {"active": True, "supported": True},
    "connector": {"available": True, "version": "0.3.13"},
    "codex": {"available": True, "signed_in": True},
    "active_runs": 0,
    "actions": [
        {"id": "enable_startup", "label": "Enable startup", "description": "Start with your user session.", "enabled": True},
        {"id": "keep_awake_on", "label": "Keep host awake", "enabled": False, "reason": "Already active"},
        {"id": "prepare_connector", "label": "Prepare connector download", "enabled": True},
        {"id": "configure_https", "enabled": True},
        {"id": "arbitrary-command", "label": "DO NOT SHOW", "enabled": True},
    ],
    "notices": [],
}


class LanHostSetupBrowserTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = browser_fixture.LanBrowserTests.asyncSetUp
    asyncTearDown = browser_fixture.LanBrowserTests.asyncTearDown
    start_worker = browser_fixture.LanBrowserTests.start_worker
    login = browser_fixture.LanBrowserTests.login
    open_account_menu = browser_fixture.LanBrowserTests.open_account_menu

    async def setup_api(self):
        self.setup = copy.deepcopy(HOST_SETUP)
        self.actions = []
        self.setup_reads = 0

        async def respond(route):
            if route.request.method == "POST":
                self.actions.append(route.request.post_data_json)
                await route.fulfill(json={"message": "Setup request accepted.", "status": "queued"})
            else:
                self.setup_reads += 1
                await route.fulfill(json=self.setup)

        await self.page.route("**/api/admin/host-setup", respond)

    async def open_setup(self):
        await self.setup_api()
        await self.login(self.admin)
        await self.open_account_menu()
        await self.page.locator("#admin-open").click()
        await self.page.locator("#host-setup-open").click()
        await expect(self.page.locator("#host-setup-name")).to_have_text("portable-host · Linux")

    async def test_setup_route_survives_reload_and_actions_use_only_known_ids(self):
        await self.open_setup()
        await expect(self.page.locator("#host-setup")).to_be_visible()
        await expect(self.page.locator(".host-setup-prerequisite")).to_contain_text("Configured locally")
        await expect(self.page.locator("#admin-content")).to_be_hidden()
        self.assertIn("panel=host-setup", self.page.url)
        await expect(self.page.get_by_text("DO NOT SHOW", exact=True)).to_have_count(0)
        await expect(self.page.get_by_role("button", name="Keep host awake", exact=True)).to_be_disabled()
        await self.page.get_by_role("button", name="Enable startup", exact=True).click()
        await expect(self.page.locator("#host-setup-status")).to_have_text("Setup request accepted.")
        self.assertEqual(self.actions, [{"action": "enable_startup"}])
        await self.page.reload()
        await expect(self.page.locator("#host-setup")).to_be_visible()
        await expect(self.page.locator("#host-setup-name")).to_have_text("portable-host · Linux")
        await expect(self.page.locator("#host-setup-open")).to_have_attribute("aria-current", "page")
        await self.page.locator("#admin-devices-open").click()
        await expect(self.page.locator("#admin-content")).to_be_visible()
        self.assertNotIn("panel=host-setup", self.page.url)
        await self.page.go_back()
        await expect(self.page.locator("#host-setup")).to_be_visible()
        self.assertFalse(self.errors, self.errors)

    async def test_configure_https_keeps_edited_fields_and_disables_actions_during_job(self):
        await self.open_setup()
        await self.page.locator("#host-setup-address").select_option("10.0.0.3")
        await self.page.locator("#host-setup-port").fill("8765")
        await self.page.locator("#host-setup-https-submit").click()
        await expect(self.page.locator("#host-setup-status")).to_contain_text("other than backend port 8765")
        self.assertEqual(self.actions, [])
        await self.page.locator("#host-setup-port").fill("9443")
        await self.page.evaluate("refreshAdmin()")
        await expect(self.page.locator("#host-setup-address")).to_have_value("10.0.0.3")
        await expect(self.page.locator("#host-setup-port")).to_have_value("9443")
        await self.page.locator("#host-setup-https-submit").click()
        await expect(self.page.locator("#host-setup-status")).to_have_text("Setup request accepted.")
        self.assertEqual(self.actions, [{"action": "configure_https", "address": "10.0.0.3", "port": 9443}])
        self.setup["job"] = {"action": "configure_https", "status": "running", "message": "Preparing HTTPS…"}
        self.setup["active_runs"] = 2
        await self.page.evaluate("refreshAdmin()")
        await expect(self.page.locator("#host-setup-https-submit")).to_be_disabled()
        await expect(self.page.get_by_role("button", name="Enable startup", exact=True)).to_be_disabled()
        await expect(self.page.locator("#host-setup-notices")).to_contain_text("2 responses are running")
        await expect(self.page.locator("#host-setup-status")).to_have_text("Preparing HTTPS…")
        for width in (390, 768, 1365):
            await self.page.set_viewport_size({"width": width, "height": 900})
            self.assertTrue(await self.page.evaluate("document.documentElement.scrollWidth <= innerWidth"))
        await self.page.set_viewport_size({"width": 1365, "height": 900})
        await self.page.locator("#theme-select").select_option("cosmic")
        await self.page.locator("#admin-view").evaluate("element => { element.scrollTop = 0; }")
        await self.page.wait_for_timeout(200)
        await self.page.screenshot(path="/tmp/codex-lan-host-setup-cosmic.png", full_page=True)
        await self.page.locator("#admin-view").evaluate("element => { element.scrollTop = element.scrollHeight; }")
        await self.page.screenshot(path="/tmp/codex-lan-host-setup-cosmic-controls.png", full_page=True)
        await self.page.locator("#theme-select").select_option("chatgpt")
        await self.page.locator("#admin-view").evaluate("element => { element.scrollTop = 0; }")
        await self.page.wait_for_timeout(200)
        await self.page.screenshot(path="/tmp/codex-lan-host-setup-chatgpt.png", full_page=True)
        await self.page.locator("#admin-view").evaluate("element => { element.scrollTop = element.scrollHeight; }")
        await self.page.screenshot(path="/tmp/codex-lan-host-setup-chatgpt-controls.png", full_page=True)
        await self.page.set_viewport_size({"width": 390, "height": 900})
        await self.page.wait_for_timeout(250)
        await self.page.locator("#admin-view").evaluate("element => { element.scrollTop = element.scrollHeight; }")
        await self.page.screenshot(path="/tmp/codex-lan-host-setup-narrow.png", full_page=True)
        self.assertFalse(self.errors, self.errors)

    async def test_client_cannot_reach_setup_ui_and_reported_text_is_inert(self):
        await self.setup_api()
        await self.login(self.client)
        await self.open_account_menu()
        await expect(self.page.locator("#admin-open")).to_be_hidden()
        await self.page.goto(self.base + "?view=admin&panel=host-setup")
        await self.page.wait_for_function("() => state.routeReady")
        await expect(self.page.locator("#host-setup")).to_be_hidden()
        self.assertEqual(self.setup_reads, 0)
        await self.login(self.admin)
        self.setup["host"]["hostname"] = "<img src=x onerror=alert(1)>"
        self.setup["notices"] = ["<script>alert('secret')</script>"]
        self.setup["network"]["public_origin"] = "https://private-token:secret@192.168.1.20:8767"
        await self.open_account_menu()
        await self.page.locator("#admin-open").click()
        await self.page.locator("#host-setup-open").click()
        await expect(self.page.locator("#host-setup-name")).to_contain_text("<img src=x")
        await expect(self.page.locator("#host-setup img, #host-setup script")).to_have_count(0)
        self.assertNotIn("private-token", await self.page.locator("#host-setup").inner_text())
        self.assertNotIn("secret@", await self.page.locator("#host-setup").inner_text())
        self.assertFalse(self.errors, self.errors)

    async def test_ssh_loopback_is_labelled_and_submitted_as_an_explicit_choice(self):
        await self.open_setup()
        self.setup["network"].update(public_origin="https://127.0.0.2:8767", https_transport="ssh_tunnel",
                                     https_addresses=["192.168.1.20", "127.0.0.2"])
        await self.page.evaluate("refreshAdmin()")
        await expect(self.page.locator("#host-setup-cards")).to_contain_text("Keep the tunnel connected")
        await self.page.locator("#host-setup-address").select_option("127.0.0.2")
        await expect(self.page.locator('#host-setup-address option[value="127.0.0.2"]')).to_have_text("127.0.0.2 · SSH tunnel")
        await self.page.locator("#host-setup-https-submit").click()
        await expect(self.page.locator("#host-setup-status")).to_have_text("Setup request accepted.")
        self.assertEqual(self.actions, [{"action": "configure_https", "address": "127.0.0.2", "port": 8767}])
        self.assertFalse(self.errors, self.errors)

    async def test_relay_status_controls_and_one_time_password_are_not_retained(self):
        await self.open_setup()
        self.setup["network"]["https_transport"] = "ssh_tunnel"
        self.setup["actions"].extend({"id": action, "enabled": True} for action in (
            "configure_relay", "relay_connect", "relay_disconnect", "probe_relay", "bootstrap_relay"
        ))
        self.setup["relay"] = {
            "supported": True, "manageable": True, "managed": True, "configured": True,
            "enabled": False, "active": False, "state": "inactive",
            "user": "codex-lan-relay", "server": "relay.example.test",
            "remote_port": 18767, "local_origin": "https://127.0.0.2:8767",
            "public_key": "ssh-ed25519 " + "A" * 60 + " codex-lan-relay",
            "auth_method": "ssh_key", "password_saved": False, "bootstrap_available": True,
        }
        await self.page.evaluate("refreshAdmin()")
        await expect(self.page.locator("#host-setup-relay-badge")).to_have_text("Disconnected")
        await expect(self.page.locator("#host-setup-relay-details")).to_contain_text("Automatic reconnect")
        await expect(self.page.locator("#host-setup-relay-server")).to_have_value("relay.example.test")
        await self.page.locator("#host-setup-relay-connect").click()
        self.assertEqual(self.actions[-1], {"action": "relay_connect"})
        self.setup["relay"].update(enabled=True, active=True)
        await self.page.evaluate("refreshAdmin()")
        await expect(self.page.locator("#host-setup-relay-badge")).to_have_text("Tunnel running")
        await expect(self.page.locator("#host-setup-relay-connect")).to_be_disabled()

        await self.page.locator("#host-setup-relay-bootstrap summary").click()
        await expect(self.page.locator("#host-setup-relay-admin-user")).to_have_value("root")
        await self.page.locator("#host-setup-relay-password").fill("example-password")
        await self.page.locator("#host-setup-relay-bootstrap summary").click()
        await expect(self.page.locator("#host-setup-relay-password")).to_have_value("")
        await self.page.locator("#host-setup-relay-bootstrap summary").click()
        await self.page.locator("#host-setup-relay-probe").click()
        self.assertEqual(self.actions[-1], {"action": "probe_relay", "server": "relay.example.test"})
        presented = "SHA256:" + "A" * 43
        verified = "SHA256:" + "B" * 43
        self.setup["job"] = {"action": "probe_relay", "status": "completed", "result": {"host_key_fingerprint": presented}}
        await self.page.evaluate("refreshAdmin()")
        await expect(self.page.locator("#host-setup-relay-observed")).to_contain_text(presented)
        await expect(self.page.locator("#host-setup-relay-fingerprint")).to_have_value("")
        await self.page.locator("#host-setup-relay-fingerprint").fill(verified)
        await self.page.locator("#host-setup-relay-password").fill("example-password")
        await self.page.locator("#host-setup-relay-confirm").check()
        await self.page.locator("#host-setup-relay-bootstrap-submit").click()
        self.assertEqual(self.actions[-1], {
            "action": "bootstrap_relay", "admin_user": "root", "server": "relay.example.test",
            "password": "example-password", "host_key_fingerprint": verified,
        })
        await expect(self.page.locator("#host-setup-relay-password")).to_have_value("")
        self.assertNotIn("example-password", await self.page.locator("#host-setup").inner_text())
        self.assertNotIn("example-password", self.page.url)
        self.assertFalse(await self.page.evaluate("JSON.stringify([localStorage, sessionStorage]).includes('example-password')"))
        await self.page.locator("#host-setup-relay-password").fill("must-clear-when-panel-closes")
        await self.page.locator("#admin-devices-open").click()
        await expect(self.page.locator("#host-setup")).to_be_hidden()
        await expect(self.page.locator("#host-setup-relay-password")).to_have_value("")
        await self.page.locator("#host-setup-open").click()
        for width in (390, 768, 1365):
            await self.page.set_viewport_size({"width": width, "height": 900})
            self.assertTrue(await self.page.evaluate("document.documentElement.scrollWidth <= innerWidth"))
        self.assertFalse(self.errors, self.errors)

    async def test_existing_relay_requires_explicit_migration_consent(self):
        await self.open_setup()
        self.setup["network"]["https_transport"] = "ssh_tunnel"
        self.setup["actions"].extend({"id": action, "enabled": True} for action in (
            "configure_relay", "relay_connect", "relay_disconnect", "probe_relay", "bootstrap_relay"
        ))
        self.setup["relay"] = {
            "supported": True, "manageable": True, "managed": False, "configured": True,
            "enabled": True, "active": True, "state": "active",
            "user": "root", "server": "legacy.example.test", "bootstrap_available": True,
        }
        await self.page.evaluate("refreshAdmin()")
        await expect(self.page.locator("#host-setup-relay-migration")).to_be_visible()
        await expect(self.page.locator("#host-setup-relay-save")).to_be_disabled()
        await expect(self.page.locator("#host-setup-relay-disconnect")).to_be_enabled()
        await self.page.locator("#host-setup-relay-replace-existing").check()
        await expect(self.page.locator("#host-setup-relay-save")).to_be_enabled()
        await self.page.locator("#host-setup-relay-server").fill("new.example.test")
        await self.page.locator("#host-setup-relay-save").click()
        self.assertEqual(self.actions[-1], {
            "action": "configure_relay", "user": "codex-lan-relay", "server": "new.example.test",
            "replace_existing": True,
        })
        await expect(self.page.locator("#host-setup-relay-replace-existing")).not_to_be_checked()
        self.assertFalse(self.errors, self.errors)

    async def test_relay_bootstrap_prerequisites_disable_password_entry(self):
        await self.open_setup()
        self.setup["relay"] = {
            "supported": True, "manageable": True, "managed": True, "configured": False,
            "enabled": False, "active": False, "bootstrap_available": False,
        }
        self.setup["actions"].extend({"id": action, "enabled": False} for action in (
            "bootstrap_relay", "configure_relay"
        ))
        await self.page.evaluate("refreshAdmin()")
        await self.page.locator("#host-setup-relay-bootstrap summary").click()
        await expect(self.page.locator("#host-setup-relay-bootstrap-submit")).to_be_disabled()
        await expect(self.page.locator("#host-setup-relay-password")).to_be_disabled()
        await expect(self.page.locator("#host-setup-relay-prerequisite")).to_contain_text("Set up HTTPS")
        self.setup["network"]["https_transport"] = "ssh_tunnel"
        await self.page.evaluate("refreshAdmin()")
        await expect(self.page.locator("#host-setup-relay-prerequisite")).to_contain_text("sshpass")
        self.assertFalse(self.errors, self.errors)


if __name__ == "__main__":
    unittest.main()
