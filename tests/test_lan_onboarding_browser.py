"""CODEX: Browser enrollment and explicit connector downloads without local execution."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import unittest
from pathlib import Path

from playwright.async_api import expect

import test_lan_browser as browser_fixture
from codex_lan import __version__


def tagged_release(version):
    tag = version
    return {
        "available": True, "version": version, "size": 1024, "sha256": "0" * 64,
        "installer_url": f"/api/client/download/{tag}/installer",
        "private_installer_url": f"/api/client/installer/{tag}",
        "pairing_url": f"/api/client/pairing/{tag}",
        "installer_filename": f"Install-Codex-Connector-{tag}.sh",
        "private_installer_filename": f"Install-Codex-Connector-{tag}-private.sh",
        "pairing_filename": f"codex-pairing-{tag}.json",
    }


class LanOnboardingBrowserTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = browser_fixture.LanBrowserTests.asyncSetUp
    asyncTearDown = browser_fixture.LanBrowserTests.asyncTearDown
    start_worker = browser_fixture.LanBrowserTests.start_worker
    login = browser_fixture.LanBrowserTests.login
    wait_worker_turn = browser_fixture.LanBrowserTests.wait_worker_turn

    async def test_browser_enrollment_waits_for_approval_without_exposing_credential(self):
        page = self.page
        await page.goto(self.base)
        await expect(page.locator("#enroll-form")).to_be_visible()
        await page.get_by_label("Computer name").fill("New Ubuntu computer")
        async with page.expect_response("**/api/enroll") as response:
            await page.get_by_role("button", name="Request access").click()
        enrolled = await (await response.value).json()
        await expect(page.locator("#gate-title")).to_have_text("Waiting for approval")
        await expect(page.locator("#gate-status")).to_contain_text("Verification code:")
        await expect(page.locator("#workspace")).not_to_be_visible()
        await expect(page.locator("#enroll-form")).not_to_be_visible()
        self.assertTrue(await page.evaluate("credential => !document.documentElement.outerHTML.includes(credential) && !JSON.stringify(localStorage).includes(credential) && !location.href.includes(credential)", enrolled["token"]))
        self.assertTrue(any(cookie["httpOnly"] for cookie in await self.context.cookies() if cookie["name"] == "codex_lan_session"))
        self.service.store.set_device(self.admin_id, enrolled["device"]["id"], status="approved")
        await page.evaluate("checkSession()")
        await expect(page.locator("#workspace")).to_be_visible()
        await expect(page.locator("#connection-setup")).to_be_visible()
        await expect(page.locator("#banner-connector-setup")).to_be_visible()
        offer = page.locator("#easy-signin-offer")
        await expect(offer).to_be_visible()
        await expect(offer).to_contain_text("Your computer is approved")
        await page.locator("#easy-signin-setup").click()
        await expect(page.locator("#browser-login-dialog")).to_be_visible()
        await expect(page.locator("#browser-login-name")).to_have_value("New Ubuntu computer")
        await page.locator("#browser-login-close").click()
        await page.locator("#easy-signin-dismiss").click()
        await expect(offer).to_be_hidden()
        await page.evaluate("checkSession()")
        await expect(offer).to_be_hidden()
        await page.reload()
        await expect(page.locator("#workspace")).to_be_visible()
        await expect(offer).to_be_hidden()
        self.assertTrue(await page.evaluate("credential => !JSON.stringify(localStorage).includes(credential)", enrolled["token"]))
        self.assertFalse(self.errors, self.errors)

    async def test_connector_version_and_pairing_download_only_on_click(self):
        page = self.page
        self.service.runtime.peers[self.admin_id].client_version = "1.2.3"
        await page.route("**/api/client/release", lambda route: route.fulfill(status=200, json=tagged_release("1.2.3")))
        pair_requests = []

        async def pairing(route):
            pair_requests.append(route.request.method)
            await route.fulfill(status=200, content_type="application/json", headers={"Content-Disposition": 'attachment; filename="codex-pairing.json"'}, body=json.dumps({"pairing_code": "FAKE-ONE-TIME-PAIRING-CREDENTIAL", "server": "https://example.test", "expires_at": 9999999999}))

        await page.route("**/api/client/pairing/1.2.3", pairing)
        await self.login(self.admin)
        await expect(page.locator("#room-title")).to_have_text("Working together")
        self.assertEqual(pair_requests, [])
        await page.get_by_role("button", name="Local connector", exact=True).click()
        await expect(page.locator("#connector-dialog")).to_be_visible()
        await expect(page.locator("#connector-version")).to_contain_text("Installed: 1.2.3")
        await expect(page.locator("#connector-version")).to_contain_text("Up to date")
        await expect(page.locator("#connector-already-ready")).to_be_visible()
        await expect(page.locator("#connector-already-ready")).to_contain_text("do not need to install a second connector")
        await expect(page.locator("#connector-dialog")).to_contain_text("cannot run an installer")
        self.assertEqual(pair_requests, [])
        await expect(page.locator("#connector-setup-steps")).to_be_hidden()
        await expect(page.locator("#connector-manual-pairing")).to_be_hidden()
        await expect(page.locator("#connector-pairing-card")).to_be_visible()
        await expect(page.locator("#connector-pairing-primary")).to_have_text("Download codex-pairing-1.2.3.json")
        self.assertEqual(pair_requests, [])
        self.assertTrue(await page.evaluate("!document.documentElement.outerHTML.includes('FAKE-ONE-TIME-PAIRING-CREDENTIAL') && !JSON.stringify(localStorage).includes('FAKE-ONE-TIME-PAIRING-CREDENTIAL')"))
        await page.screenshot(path="/tmp/codex-lan-connector-onboarding.png")
        for width in (400, 1365):
            await page.set_viewport_size({"width": width, "height": 900})
            self.assertTrue(await page.evaluate("document.documentElement.scrollWidth <= innerWidth"))
            self.assertTrue(await page.locator("#connector-dialog").evaluate("node => node.scrollWidth <= node.clientWidth + 1"))
        async with page.expect_download() as download_info:
            await page.locator("#connector-pairing-primary").click()
        download = await download_info.value
        self.assertEqual(download.suggested_filename, "codex-pairing-1.2.3.json")
        self.assertEqual(pair_requests, ["POST"])
        await expect(page.locator("#connector-status")).to_contain_text("Pairing file downloaded")
        await page.get_by_role("button", name="Close connector setup").click()
        await expect(page.locator("#connector-dialog")).not_to_be_visible()
        self.assertFalse(self.errors, self.errors)

    async def test_client_one_step_setup_is_one_download_one_command_and_keeps_manual_fallback(self):
        page = self.page
        release_dir = Path(self.temp.name) / "releases"
        release_dir.mkdir()
        archive = b"browser one-step archive"
        (release_dir / "client.tar.gz").write_bytes(archive)
        (release_dir / "Install-Codex-Connector.sh").write_bytes(b"#!/bin/sh\nexit 0\n")
        (release_dir / "release.json").write_text(json.dumps({
            "version": __version__, "sha256": hashlib.sha256(archive).hexdigest(), "size": len(archive),
        }))
        ca = Path(self.temp.name) / "server-ca.pem"
        ca.write_text("-----BEGIN CERTIFICATE-----\nHARMLESS-BROWSER-FIXTURE\n-----END CERTIFICATE-----\n")
        self.service.network = {"public_origin": "https://192.168.1.56:8767", "ca": ca}

        await self.login(self.client)
        notice = page.locator("#connector-update-notice")
        await expect(notice).to_be_visible()
        await expect(page.locator("#connector-update-title")).to_have_text("Connect Codex Connector")
        await page.locator("#connector-update-details").click()
        await expect(page.locator("#connector-setup-steps")).to_be_visible()
        await expect(page.locator("#connector-setup-steps > li")).to_have_count(2)
        await expect(page.locator("#connector-private-installer")).to_have_text("Download one-step setup")
        await expect(page.locator("#connector-private-command")).to_have_text("Download the one-step setup to show its exact command.")
        self.assertFalse(await page.locator("#connector-manual-pairing").evaluate("node => node.open"))

        async with page.expect_download() as download_info:
            await page.locator("#connector-private-installer").click()
        download = await download_info.value
        self.assertRegex(download.suggested_filename, rf"^Install-Codex-Connector-{re.escape(__version__)}-private-\d{{13}}\.sh$")
        private_command = f"bash ~/Downloads/{download.suggested_filename}"
        await expect(page.locator("#connector-private-command")).to_have_text(private_command)
        script = Path(await download.path()).read_text()
        self.assertIn('"/api/client/bootstrap/installer/" + version', script)
        self.assertIn("--pair-file", script)
        self.assertNotIn(self.client["token"], script)
        encoded = re.search(
            r"<<'CODEX_PAIRING_PAYLOAD'\n([A-Za-z0-9+/=]+)\nCODEX_PAIRING_PAYLOAD",
            script,
        )
        self.assertIsNotNone(encoded)
        pairing = json.loads(base64.b64decode(encoded.group(1), validate=True))
        self.assertEqual(pairing["connector_version"], __version__)
        self.assertNotIn(pairing["pairing_code"], script)
        await expect(page.locator("#connector-status")).to_contain_text("within 10 minutes")
        await expect(page.locator("#connector-status")).to_contain_text("repairs stale or deleted links")
        await page.locator("#connector-manual-pairing > summary").click()
        await expect(page.locator("#connector-installer")).to_be_visible()
        await expect(page.locator("#connector-pairing")).to_be_visible()
        self.assertFalse(self.errors, self.errors)

    async def test_connector_notice_dismisses_without_bypassing_update_and_manual_pairing_stays_available(self):
        page = self.page
        peer = self.service.runtime.peers[self.client_id]
        peer.client_version = "0.2.0"
        release = tagged_release(__version__)
        await page.route("**/api/client/release", lambda route: route.fulfill(status=200, json=release))
        await self.login(self.client)
        await page.get_by_role("button", name="Local connector", exact=True).click()
        await expect(page.locator("#connector-update-help")).to_be_visible()
        await page.get_by_role("button", name="Close connector setup").click()
        notice = page.locator("#connector-update-notice")
        await expect(notice).to_be_visible()
        await page.get_by_role("button", name="Dismiss connector notice").click()
        await expect(notice).to_be_hidden()
        self.assertTrue(await page.evaluate("connectorUpdateState().needed"))
        await page.evaluate("checkSession()")
        await expect(notice).to_be_hidden()
        await page.reload()
        await expect(page.locator("#workspace")).to_be_visible()
        await expect(notice).to_be_hidden()
        await page.get_by_role("button", name="Local connector", exact=True).click()
        await expect(page.locator("#connector-update-help")).to_be_visible()
        await expect(page.locator("#connector-manual-pairing")).to_be_visible()
        await page.locator("#connector-manual-pairing > summary").click()
        await expect(page.locator("#connector-pairing")).to_be_visible()
        await expect(page.locator("#connector-manual-command")).to_contain_text(f"--pair-file ~/Downloads/codex-pairing-{__version__}.json")
        await page.get_by_role("button", name="Close connector setup").click()
        release = tagged_release("0.4.0")
        await page.evaluate("acceptConnectorRelease({available: true, version: '0.4.0'}); renderConnectorUpdateNotice()")
        await expect(notice).to_be_visible()
        self.assertFalse(self.errors, self.errors)

    async def test_unavailable_release_and_pairing_failure_are_recoverable(self):
        page = self.page
        await page.route("**/api/client/release", lambda route: route.fulfill(status=200, json={"available": False}))
        await page.route("**/api/client/pairing/1.2.3", lambda route: route.fulfill(status=503, body="Not configured"))
        storage_key = json.dumps(f"codex-lan-connector:{self.client_id}")
        storage_value = json.dumps(json.dumps({"requiredVersion": "1.2.3"}))
        await page.add_init_script(f"try {{ localStorage.setItem({storage_key}, {storage_value}); }} catch {{}}")
        await self.login(self.client)
        await expect(page.locator("#room-title")).to_have_text("Client-owned room")
        await expect(page.locator("#connector-update-notice")).to_be_visible()
        await page.get_by_role("button", name="Local connector", exact=True).click()
        await expect(page.locator("#connector-version")).to_contain_text("Current release: 1.2.3")
        await expect(page.locator("#connector-status")).to_contain_text("temporarily unavailable")
        await expect(page.locator("#connector-installer")).to_have_attribute("aria-disabled", "true")
        await expect(page.locator("#connector-private-installer")).to_be_disabled()
        await page.locator("#connector-manual-pairing > summary").click()
        await page.locator("#connector-pairing").click()
        await expect(page.locator("#connector-status")).to_contain_text("Could not create a pairing file (503)")
        await expect(page.locator("#connector-pairing")).to_be_enabled()
        self.assertFalse(self.errors, self.errors)

    async def test_connector_explains_host_model_and_local_tools(self):
        self.service.runtime.peers[self.admin_id].execution_mode = "server"
        await self.login(self.admin)
        await self.page.get_by_role("button", name="Local connector", exact=True).click()
        await expect(self.page.locator("#connector-execution")).to_be_visible()
        await expect(self.page.locator("#connector-execution")).to_contain_text("Codex runs on the host server")
        await expect(self.page.locator("#connector-execution")).to_contain_text("no client OpenAI connection or proxy")
        self.assertFalse(self.errors, self.errors)

    async def test_client_update_notice_is_persistent_actionable_and_clears_only_when_confirmed(self):
        release_dir = Path(self.temp.name) / "releases"
        release_dir.mkdir()
        archive = b"browser update archive"
        (release_dir / "client.tar.gz").write_bytes(archive)
        (release_dir / "Install-Codex-Connector.sh").write_bytes(b"#!/bin/sh\nexit 0\n")
        manifest = release_dir / "release.json"
        release = {"version": __version__, "sha256": hashlib.sha256(archive).hexdigest(), "size": len(archive)}
        manifest.write_text(json.dumps(release))
        peer = self.service.runtime.peers[self.client_id]
        peer.client_version = "0.2.0"
        peer.execution_mode = "local"
        await self.page.add_init_script("""(() => {
          window.__connectorNotifications = [];
          class TestNotification {
            static permission = 'granted';
            constructor(title, options) { this.title = title; this.options = options; this.closed = false; window.__connectorNotifications.push(this); }
            close() { this.closed = true; }
          }
          Object.defineProperty(window, 'Notification', {configurable: true, value: TestNotification});
        })()""")

        await self.login(self.client)
        notice = self.page.locator("#connector-update-notice")
        await expect(notice).to_be_visible()
        await expect(notice).to_contain_text("Connector 0.2.0 is installed")
        await expect(notice).to_contain_text("This page cannot start local commands")
        installer_command = f"bash ~/Downloads/Install-Codex-Connector-{__version__}.sh"
        await expect(self.page.locator("#connector-update-command")).to_have_text(installer_command)
        self.assertEqual(await self.page.evaluate("window.__connectorNotifications.map(item => item.title)"), ["Codex Connector update required"])
        async with self.page.expect_download() as download:
            await self.page.locator("#connector-update-download").click()
        self.assertEqual((await download.value).suggested_filename, f"Install-Codex-Connector-{__version__}.sh")
        await expect(notice).to_be_visible()
        await expect(self.page.locator("#toast")).to_contain_text("When it finishes, run bash")
        await self.page.locator("#connector-update-details").click()
        await expect(self.page.locator("#connector-update-help")).to_be_visible()
        await expect(self.page.locator("#connector-update-help")).to_contain_text("existing pairing and settings are kept")
        await expect(self.page.locator("#connector-setup-steps")).to_be_hidden()
        await self.page.get_by_role("button", name="Close connector setup").click()
        await self.page.evaluate("window.__connectorNotifications[0].close(); checkSession()")
        await expect(notice).to_be_visible()
        self.assertEqual(await self.page.evaluate("window.__connectorNotifications.length"), 1)
        await self.page.reload()
        await expect(notice).to_be_visible()
        self.assertEqual(await self.page.evaluate("window.__connectorNotifications.length"), 1)

        peer.client_version = __version__
        peer.execution_mode = "server"
        self.service.store.enable_server_execution(self.client_id)
        await self.page.evaluate("checkSession()")
        await expect(notice).to_be_hidden()
        self.assertTrue(await self.page.evaluate("window.__connectorNotifications[0].closed"))
        peer.ready = False
        await self.page.evaluate("checkSession()")
        await expect(notice).to_be_visible()
        await expect(self.page.locator("#connector-update-title")).to_have_text("Reconnect Codex Connector")
        await expect(notice).to_contain_text("was already linked to the server")
        peer.client_version = "0.2.0"
        peer.ready = True
        await self.page.evaluate("checkSession()")
        await expect(notice).to_be_visible()
        await expect(self.page.locator("#connector-update-title")).to_have_text("Codex Connector update required")
        await expect(notice).to_contain_text("Connector 0.2.0 is installed")

        for theme in ("light", "dark", "midnight"):
            await self.page.locator("#theme-select").select_option(theme)
            await expect(notice).to_be_visible()
        await self.page.set_viewport_size({"width": 360, "height": 800})
        self.assertTrue(await self.page.evaluate("document.documentElement.scrollWidth <= innerWidth"))
        self.assertTrue(await notice.evaluate("node => node.scrollWidth <= node.clientWidth + 1"))

        manifest.unlink()
        await self.page.evaluate("checkSession()")
        await expect(notice).to_be_visible()
        await expect(notice).to_contain_text("download is temporarily unavailable")
        await expect(self.page.locator("#connector-update-download")).to_have_attribute("aria-disabled", "true")
        await self.page.locator("#connector-update-download").click()
        await expect(self.page.locator("#toast")).to_contain_text("temporarily unavailable")
        self.service.store.set_device(self.admin_id, self.pending["device"]["id"], status="approved")
        await self.page.evaluate("token => login(token)", self.pending["token"])
        await expect(notice).to_be_hidden()
        manifest.write_text(json.dumps(release))
        self.service.runtime.peers[self.admin_id].client_version = "0.2.0"
        self.service.runtime.peers[self.admin_id].execution_mode = "local"
        notification_count = await self.page.evaluate("window.__connectorNotifications.length")
        await self.page.evaluate("token => login(token)", self.admin["token"])
        await expect(notice).to_be_hidden()
        self.assertEqual(await self.page.evaluate("window.__connectorNotifications.length"), notification_count)
        self.assertFalse(self.errors, self.errors)

    async def test_cached_connector_version_never_creates_a_false_update_notice(self):
        release_dir = Path(self.temp.name) / "releases"
        release_dir.mkdir()
        archive = b"browser identity release"
        (release_dir / "client.tar.gz").write_bytes(archive)
        (release_dir / "Install-Codex-Connector.sh").write_bytes(b"#!/bin/sh\nexit 0\n")
        (release_dir / "release.json").write_text(json.dumps({
            "version": __version__, "sha256": hashlib.sha256(archive).hexdigest(), "size": len(archive),
        }))
        peer = self.service.runtime.peers.pop(self.client_id)
        storage_key = json.dumps(f"codex-lan-connector:{self.client_id}")
        storage_value = json.dumps(json.dumps({"version": "0.1.0", "requiredVersion": __version__}))
        await self.page.add_init_script(f"try {{ localStorage.setItem({storage_key}, {storage_value}); }} catch {{}}")

        await self.login(self.client)
        notice = self.page.locator("#connector-update-notice")
        await expect(notice).to_be_visible()
        await expect(self.page.locator("#connector-update-title")).to_have_text("Connect Codex Connector")
        await expect(notice).to_contain_text("No Connector is linked to this device identity")
        await expect(notice).not_to_contain_text("update required")
        await self.page.locator("#connector-update-details").click()
        await expect(self.page.locator("#connector-setup-steps")).to_be_visible()
        await expect(self.page.locator("#connector-update-help")).to_be_hidden()
        await self.page.get_by_role("button", name="Close connector setup").click()

        self.service.store.enable_server_execution(self.client_id)
        await self.page.evaluate("checkSession()")
        await expect(self.page.locator("#connector-update-title")).to_have_text("Reconnect Codex Connector")
        await expect(notice).not_to_contain_text("update required")

        peer.client_version = "0.1.0"
        peer.execution_mode = "server"
        peer.ready = True
        self.service.runtime.peers[self.client_id] = peer
        await self.page.evaluate("checkSession()")
        await expect(self.page.locator("#connector-update-title")).to_have_text("Codex Connector update required")
        await expect(notice).to_contain_text("Connector 0.1.0 is installed")
        self.assertFalse(self.errors, self.errors)


if __name__ == "__main__":
    unittest.main()
