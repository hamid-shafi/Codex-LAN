"""CODEX: HTTPS-only pairing, archive safety and idle maintenance without real services."""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import shutil
import ssl
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from project_layout import configure_imports, source_file
configure_imports(__file__)

import direct_client as client
import setup_lan_direct as setup


def payload(root, version="0.2.0"):
    root = Path(root)
    for name in client.REQUIRED:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"fixture")
    (root / "codex_lan/__init__.py").write_text(f'__version__ = "{version}"\n')
    (root / "wheels").mkdir(exist_ok=True)
    (root / "wheels/fake-1.0-py3-none-any.whl").write_bytes(b"fixture wheel")
    return root


def make_archive(path, source, extra=None):
    with tarfile.open(path, "w:gz") as archive:
        for item in Path(source).rglob("*"):
            if item.is_file():
                archive.add(item, arcname=client.ARCHIVE_ROOT + "/" + item.relative_to(source).as_posix(), recursive=False)
        if extra:
            archive.addfile(*extra)


class DirectUtilityTests(unittest.TestCase):
    def test_supported_ubuntu_python_versions_are_selected_without_exact_minor_gate(self):
        for minor in range(10, 15):
            self.assertEqual(client.validate_runtime((3, minor), "linux", "x86_64", "cpython"), (3, minor))
        for version, system, machine, implementation in (
                ((3, 9), "linux", "x86_64", "cpython"), ((3, 15), "linux", "x86_64", "cpython"),
                ((3, 12), "darwin", "x86_64", "cpython"), ((3, 12), "linux", "aarch64", "cpython"),
                ((3, 12), "linux", "x86_64", "pypy")):
            with self.subTest(version=version, system=system, machine=machine, implementation=implementation), self.assertRaises(client.ConnectorError):
                client.validate_runtime(version, system, machine, implementation)
        with self.assertRaises(client.ConnectorError):
            client.validate_runtime((3, 14), "linux", "x86_64", "cpython", abi="t")

    def test_https_origin_rejects_credentials_public_hosts_paths_and_queries(self):
        self.assertEqual(client.origin("https://192.168.1.20:8767/"), "https://192.168.1.20:8767")
        for value in ("http://192.168.1.20", "https://8.8.8.8", "https://example.com", "https://user:secret@192.168.1.20",
                      "https://192.168.1.20/path", "https://192.168.1.20/?token=x", "https://192.168.1.20/#x",
                      "https://0.0.0.0", "https://192.168.1.20:99999"):
            with self.subTest(value=value), self.assertRaises(client.ConnectorError):
                client.origin(value)

    def test_transport_uses_pinned_client_tls_and_rejects_redirects(self):
        with mock.patch("direct_client.ssl.SSLContext") as context:
            transport = client.Transport("https://192.168.1.20:8767", "CERTIFICATE")
            context.assert_called_once_with(ssl.PROTOCOL_TLS_CLIENT)
            context.return_value.load_verify_locations.assert_called_once_with(cadata="CERTIFICATE")
            with self.assertRaises(client.ConnectorError):
                transport.request("https://attacker.invalid/")
        with self.assertRaises(client.ConnectorError):
            client.NoRedirect().redirect_request(None)

    def test_lan_transport_ignores_client_proxy_environment_for_api_and_downloads(self):
        proxies = {key: "http://127.0.0.1:19876" for key in (
            "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")}
        proxies.update(NO_PROXY="", no_proxy="")
        response = mock.Mock(code=200, msg="OK")
        with mock.patch.dict(os.environ, proxies, clear=True), mock.patch("direct_client.ssl.SSLContext") as context:
            transport = client.Transport("https://192.168.1.20:8767", "PINNED-CA", "FAKE-TOKEN")
            context.return_value.load_verify_locations.assert_called_once_with(cadata="PINNED-CA")
            self.assertFalse(any(isinstance(handler, client.ProxyHandler) and handler.proxies
                                 for handler in transport.opener.handlers))
            self.assertTrue(any(isinstance(handler, client.NoRedirect) for handler in transport.opener.handlers))
            with mock.patch("urllib.request.HTTPSHandler.do_open", return_value=response) as send:
                for path in ("/api/me", "/api/client/claim", "/api/client/release",
                             "/api/client/download/archive", "/api/client/download/installer"):
                    self.assertIs(transport.request(path), response)
                    request = send.call_args.args[1]
                    self.assertEqual(request.host, "192.168.1.20:8767")
                    self.assertEqual(request.selector, path)
                    self.assertIsNone(request._tunnel_host)

    def test_private_atomic_write_and_operation_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "device.token"
            client.private_write(path, "secret")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            with client.exclusive(directory):
                with self.assertRaises(client.ConnectorError):
                    with client.exclusive(directory):
                        pass

    def test_rejected_identity_is_distinct_from_connectivity_and_pairing_errors(self):
        with mock.patch("direct_client.ssl.SSLContext"):
            transport = client.Transport("https://192.168.1.20:8767", "CERTIFICATE")
        for code in (401, 403):
            transport.opener = mock.Mock()
            transport.opener.open.side_effect = client.HTTPError(transport.server, code, "rejected", {}, None)
            with self.assertRaises(client.DeviceAuthorizationError):
                transport.request("/api/client/maintenance", {"pause": True})
            with self.assertRaises(client.ConnectorError) as rejected:
                transport.request("/api/client/claim", {"pairing_code": "fixture"})
            self.assertNotIsInstance(rejected.exception, client.DeviceAuthorizationError)
        transport.opener.open.side_effect = client.URLError("offline")
        with self.assertRaises(client.ConnectorError) as offline:
            transport.request("/api/me")
        self.assertNotIsInstance(offline.exception, client.DeviceAuthorizationError)

    def test_download_checks_size_and_digest(self):
        transport = object.__new__(client.Transport)
        transport.request = lambda path: io.BytesIO(b"archive bytes")
        with tempfile.TemporaryDirectory() as directory:
            digest = hashlib.sha256(b"archive bytes").hexdigest()
            transport.download(Path(directory) / "good", 13, digest)
            for filename, size, checksum in (("bad-hash", 13, "0" * 64), ("bad-size", 2, digest), ("bad-limit", client.MAX_DOWNLOAD + 1, digest)):
                with self.assertRaises(client.ConnectorError):
                    transport.download(Path(directory) / filename, size, checksum)

    def test_archive_rejects_traversal_links_duplicates_and_unexpected_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = payload(root / "source")
            archive = root / "valid.tar.gz"
            make_archive(archive, source)
            extracted = client.extract_release(archive, root / "valid")
            self.assertEqual(client.payload_version(extracted), "0.2.0")
            for index, name in enumerate(("/etc/passwd", "Codex-LAN-Client/../escape", "Other/file",
                                           "Codex-LAN-Client/device.token", "Codex-LAN-Client/direct_client.py")):
                info = tarfile.TarInfo(name)
                info.size = 1
                archive = root / f"bad{index}.tar.gz"
                make_archive(archive, source, (info, io.BytesIO(b"x")))
                with self.assertRaises(client.ConnectorError):
                    client.extract_release(archive, root / f"bad{index}")
            for index, kind in enumerate((tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.FIFOTYPE)):
                info = tarfile.TarInfo("Codex-LAN-Client/wheels/link.whl")
                info.type, info.linkname = kind, "/etc/passwd"
                archive = root / f"link{index}.tar.gz"
                make_archive(archive, source, (info,))
                with self.assertRaises(client.ConnectorError):
                    client.extract_release(archive, root / f"link{index}")
            with mock.patch.object(client, "MAX_UNPACKED", 1), self.assertRaises(client.ConnectorError):
                client.extract_release(root / "valid.tar.gz", root / "too-large")

    def test_offline_prepare_never_activates_or_mutates_existing_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = payload(root / "source")
            destination = root / "installed"
            client.private_write(destination / "device.token", "unchanged")
            runner = mock.Mock(return_value=subprocess.CompletedProcess([], 0))
            prepared = client.install_release(source, destination, "0.2.0", runner)
            self.assertEqual(prepared.name, "0.2.0")
            self.assertEqual((destination / "device.token").read_text(), "unchanged")
            self.assertFalse((destination / "current").exists())
            self.assertIn("--no-index", runner.call_args_list[1].args[0])
            self.assertIn("--only-binary=:all:", runner.call_args_list[1].args[0])
            self.assertIn("--isolated", runner.call_args_list[1].args[0])
            client.switch_release(destination, prepared)
            self.assertEqual((destination / "current").resolve(), prepared)

    def test_failed_venv_and_dependencies_have_actionable_errors_and_preserve_current_release(self):
        for failure_step, expected in ((0, "venv"), (1, "bundled dependencies"), (2, "compatibility check")):
            with self.subTest(step=failure_step), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source = payload(root / "source", "0.2.1")
                installed = root / "installed"
                old = payload(installed / "releases/0.2.0")
                client.switch_release(installed, old)
                responses = [subprocess.CompletedProcess([], 0)] * failure_step
                responses.append(subprocess.CalledProcessError(1, ["fixture"], stderr="private failure text"))
                runner = mock.Mock(side_effect=responses)
                with self.assertRaisesRegex(client.ConnectorError, expected) as error:
                    client.install_release(source, installed, "0.2.1", runner)
                self.assertNotIn("private failure text", str(error.exception))
                self.assertEqual((installed / "current").resolve(), old)
                self.assertFalse((installed / "releases/0.2.1").exists())
                self.assertFalse(list((installed / "releases").glob(".prepare-*")))

    def test_relocated_private_venv_interpreter_uses_final_release_prefix(self):
        with tempfile.TemporaryDirectory() as directory:
            staging = Path(directory) / ".prepare-fixture"
            staging.mkdir()
            subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(staging / "venv")],
                           check=True, capture_output=True, text=True, timeout=20)
            destination = Path(directory) / "release"
            staging.rename(destination)
            result = subprocess.run([str(destination / "venv/bin/python"), "-c", "import sys; print(sys.prefix)"],
                                    check=True, capture_output=True, text=True, timeout=10)
            self.assertEqual(Path(result.stdout.strip()), destination / "venv")


class PairingTests(unittest.TestCase):
    def pairing(self, path, **fields):
        value = {"format": 1, "server": "https://192.168.1.20:8767", "pairing_code": "p" * 43,
                 "ca_certificate": "-----BEGIN CERTIFICATE-----fake", "expires_at": time.time() + 500,
                 "device_name": "Ubuntu", "connector_version": "0.2.0"}
        value.update(fields)
        path.write_text(json.dumps(value))
        return value

    def test_missing_user_manager_is_bounded_and_gives_wsl_instructions(self):
        failures = (FileNotFoundError("systemctl"), subprocess.CalledProcessError(1, ["systemctl"]),
                    subprocess.TimeoutExpired(["systemctl"], 10))
        with mock.patch("setup_lan_direct.platform.release", return_value="6.6-microsoft-standard-WSL2"):
            for failure in failures:
                with self.subTest(failure=type(failure).__name__):
                    runner = mock.Mock(side_effect=failure)
                    with self.assertRaisesRegex(client.ConnectorError, "/etc/wsl.conf"):
                        setup.require_user_service(runner)
                    runner.assert_called_once_with(
                        ["systemctl", "--user", "show-environment"], check=True,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)

    def test_no_user_manager_does_not_claim_pairing_or_write_installation(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch("setup_lan_direct.Transport"):
            home = Path(directory)
            pair = home / "pair.json"
            self.pairing(pair)
            source = payload(home / "source")
            root = home / "installed"
            runner = mock.Mock(side_effect=subprocess.CalledProcessError(1, ["systemctl"]))
            with mock.patch("setup_lan_direct.install_release") as install, \
                    mock.patch("setup_lan_direct.claim_pairing") as claim, \
                    self.assertRaisesRegex(client.ConnectorError, "systemd user session"):
                setup.install(source, pair, root, runner=runner, allow=lambda _server: True)
            install.assert_not_called()
            claim.assert_not_called()
            self.assertFalse(root.exists())

    def test_update_checks_user_manager_before_modifying_installation(self):
        runner = mock.Mock(side_effect=FileNotFoundError("systemctl"))
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch("setup_lan_direct.private_write") as write, \
                self.assertRaisesRegex(client.ConnectorError, "systemd user session"):
            setup.update_existing(Path(directory) / "not-installed", runner=runner)
        write.assert_not_called()

    def test_expired_pairing_and_consent_rejection_do_not_claim(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch("setup_lan_direct.Transport"):
            root = Path(directory)
            pair = root / "pair.json"
            self.pairing(pair, expires_at=time.time() - 1)
            with self.assertRaises(client.ConnectorError):
                setup.load_pairing(pair)
            self.pairing(pair)
            transport = mock.Mock()
            with self.assertRaises(client.ConnectorError):
                setup.install(payload(root / "source"), pair, root / "installed", allow=lambda _server: False, transport_class=transport)
            transport.assert_not_called()
            self.assertFalse((root / "installed/device.token").exists())

    def test_pairing_release_must_match_installer_before_consent_or_writes(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch("setup_lan_direct.Transport"):
            root = Path(directory)
            pair = root / "pair.json"
            self.pairing(pair, connector_version="0.2.0")
            allow = mock.Mock(return_value=True)
            with self.assertRaisesRegex(client.ConnectorError, "Connector 0.2.0"):
                setup.install(payload(root / "source", "0.2.1"), pair, root / "installed", allow=allow)
            allow.assert_not_called()
            self.assertFalse((root / "installed").exists())

    def test_unsupported_python_stops_before_pairing_or_install_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch("setup_lan_direct.validate_runtime", side_effect=client.ConnectorError("Unsupported Python")), \
                    mock.patch("setup_lan_direct.load_pairing") as pairing, mock.patch("setup_lan_direct.install_release") as release, \
                    self.assertRaises(client.ConnectorError):
                setup.install(root / "source", root / "pair.json", root / "installed")
            pairing.assert_not_called()
            release.assert_not_called()
            self.assertFalse((root / "installed").exists())

    def test_install_claims_after_consent_saves_private_identity_and_scoped_ca(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch("setup_lan_direct.Transport"):
            home = Path(directory)
            pair = home / "pair.json"
            self.pairing(pair)
            source = payload(home / "source")
            root = home / "installation"
            approved = []

            class FakeTransport:
                def __init__(self, *_args):
                    pass

                def json(self, path, data):
                    if not approved:
                        raise AssertionError("Claim happened before consent")
                    return {"token": "private-device-token-123456789", "device": {"id": "approved", "status": "approved", "name": "Ubuntu"}}

            def allow(_server):
                approved.append(True)
                return True

            runner = mock.Mock(return_value=subprocess.CompletedProcess([], 0))
            with mock.patch("setup_lan_direct.Path.home", return_value=home):
                setup.install(source, pair, root, no_launch=True, runner=runner, allow=allow, transport_class=FakeTransport)
            self.assertEqual((root / "device.token").stat().st_mode & 0o777, 0o600)
            self.assertNotIn("private-device-token", (root / "config.json").read_text())
            unit = (home / ".config/systemd/user/codex-lan-direct.service").read_text()
            self.assertIn("--server-ca", unit)
            self.assertNotIn("SSL_CERT_FILE", unit)
            self.assertNotIn("private-device-token", unit)
            self.assertIn("\nWorkingDirectory=" + str(root / "current") + "\n", unit)
            self.assertFalse(any("start" in call.args[0] for call in runner.call_args_list))
            self.assertTrue((home / ".local/share/applications/codex-connector.desktop").is_file())


    def test_fresh_install_cannot_enable_duplicate_host_identity(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch("setup_lan_direct.Transport"):
            home = Path(directory)
            pair = home / "pair.json"
            self.pairing(pair)
            root = home / "installed"
            source = payload(home / "source")
            token = "host-admin-token-fixture-123456789"
            client.private_write(home / ".local/share/codex-lan/server/admin.token", token)
            transport = mock.Mock()
            transport.json.return_value = {"token": token, "device": {"id": "host", "status": "approved"}}
            runner = mock.Mock(return_value=subprocess.CompletedProcess([], 0))
            with mock.patch("setup_lan_direct.Path.home", return_value=home), self.assertRaises(client.ConnectorError):
                setup.install(source, pair, root, runner=runner, allow=lambda _server: True,
                              transport_class=mock.Mock(return_value=transport))
            self.assertFalse((root / "device.token").exists())
            self.assertFalse((home / ".config/systemd/user/codex-lan-direct.service").exists())
            self.assertFalse(any("enable" in call.args[0] or "start" in call.args[0] for call in runner.call_args_list))

    def test_explicit_pairing_repairs_existing_identity_and_verifies_live_connector(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            root = home / "installed"
            source = payload(home / "source")
            existing = payload(root / "releases/0.2.0")
            client.switch_release(root, existing)
            client.private_write(root / "device.token", "old-private-device-token-123456")
            client.private_write(root / "server-ca.pem", "OLD CERTIFICATE")
            client.private_write(root / "config.json", json.dumps({
                "format": 1, "server": "https://192.168.1.20:8767", "device_id": "old-device"
            }))
            pair = home / "pair.json"
            self.pairing(pair)
            events = []

            class ExistingConnector:
                def __init__(self, _root, transport=None, runner=None):
                    self.server = "https://192.168.1.20:8767"
                    self.transport = transport

                def wait_idle(self):
                    events.append("drained")

                def unpause(self):
                    events.append("unpaused-old")

                def wait_ready(self, version):
                    events.append(("verified", version))

            class PairTransport:
                def __init__(self, _server, _certificate, token=None):
                    self.token = token

                def json(self, path, _data=None):
                    if path != "/api/client/claim":
                        raise AssertionError(path)
                    return {"token": "new-private-device-token-123456", "device": {
                        "id": "new-device", "status": "approved", "name": "Alireza"
                    }}

            runner = mock.Mock(return_value=subprocess.CompletedProcess([], 0))
            with mock.patch("setup_lan_direct.Path.home", return_value=home), \
                    mock.patch("setup_lan_direct.Transport"):
                result = setup.repair_pairing(source, pair, root, runner=runner,
                                              allow=lambda _server: True,
                                              transport_class=PairTransport,
                                              connector_class=ExistingConnector)
            self.assertEqual(result["device_id"], "new-device")
            self.assertEqual((root / "device.token").read_text(), "new-private-device-token-123456")
            self.assertEqual(json.loads((root / "config.json").read_text())["device_id"], "new-device")
            self.assertEqual(events, ["drained", "unpaused-old", ("verified", "0.2.0")])
            actions = [call.args[0][2] for call in runner.call_args_list
                       if call.args[0][0] == "systemctl" and len(call.args[0]) > 2]
            self.assertEqual(actions, ["show-environment", "is-active", "stop", "daemon-reload", "enable", "start"])

    def test_failed_repair_restores_old_credentials_release_and_service(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            root = home / "installed"
            source = payload(home / "source", "0.2.1")
            existing = payload(root / "releases/0.2.0")
            client.switch_release(root, existing)
            original = {
                root / "device.token": b"old-private-device-token-123456",
                root / "server-ca.pem": b"OLD CERTIFICATE",
                root / "config.json": json.dumps({
                    "format": 1, "server": "https://192.168.1.20:8767", "device_id": "old-device"
                }).encode(),
            }
            for path, value in original.items():
                client.private_write(path, value)
            pair = home / "pair.json"
            self.pairing(pair, connector_version="0.2.1")

            class ExistingConnector:
                def __init__(self, _root, transport=None, runner=None):
                    self.server = "https://192.168.1.20:8767"

                def wait_idle(self):
                    return None

                def unpause(self):
                    return None

                def wait_ready(self, _version):
                    raise client.ConnectorError("fixture verification failure")

            class PairTransport:
                def __init__(self, _server, _certificate, token=None):
                    self.token = token

                def json(self, _path, _data=None):
                    return {"token": "new-private-device-token-123456", "device": {
                        "id": "new-device", "status": "approved", "name": "Alireza"
                    }}

            def install_fixture(_source, destination, version, _runner):
                release = payload(Path(destination) / "releases" / version, version)
                client.private_write(release / ".ready", version)
                return release

            runner = mock.Mock(return_value=subprocess.CompletedProcess([], 0))
            with mock.patch("setup_lan_direct.Path.home", return_value=home), \
                    mock.patch("setup_lan_direct.Transport"), \
                    mock.patch("setup_lan_direct.install_release", side_effect=install_fixture), \
                    self.assertRaisesRegex(client.ConnectorError, "fixture verification failure"):
                setup.repair_pairing(source, pair, root, runner=runner,
                                     allow=lambda _server: True,
                                     transport_class=PairTransport,
                                     connector_class=ExistingConnector)
            for path, value in original.items():
                self.assertEqual(path.read_bytes(), value)
            self.assertEqual((root / "current").resolve(), existing)
            actions = [call.args[0][2] for call in runner.call_args_list
                       if call.args[0][0] == "systemctl" and len(call.args[0]) > 2]
            self.assertGreaterEqual(actions.count("start"), 2)

    def test_main_routes_an_explicit_pair_file_to_repair_not_update(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "installed"
            client.private_write(root / "config.json", "{}")
            client.private_write(root / "device.token", "old-private-device-token-123456")
            pair = Path(directory) / "pair.json"
            pair.write_text("{}")
            repaired = {"server": "https://192.168.1.20:8767"}
            with mock.patch.object(sys, "argv", ["setup_lan_direct.py", "--root", str(root), "--pair-file", str(pair)]), \
                    mock.patch("setup_lan_direct.os.geteuid", return_value=1000), \
                    mock.patch("setup_lan_direct.shutil.which", return_value="/usr/bin/systemctl"), \
                    mock.patch("setup_lan_direct.validate_runtime"), \
                    mock.patch("setup_lan_direct.exclusive", return_value=contextlib.nullcontext()), \
                    mock.patch("setup_lan_direct.repair_pairing", return_value=repaired) as repair, \
                    mock.patch("setup_lan_direct.update_existing") as update, \
                    mock.patch("setup_lan_direct.gui_available", return_value=False):
                setup.main()
            repair.assert_called_once()
            update.assert_not_called()

    def test_main_existing_pairing_ignores_automatically_found_expired_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "installed"
            client.private_write(root / "config.json", "{}")
            client.private_write(root / "device.token", "old-private-device-token-123456")
            pair = Path(directory) / "codex-pairing-0.3.2.json"
            pair.write_text("expired or malformed old download")
            with mock.patch.object(sys, "argv", ["setup_lan_direct.py", "--root", str(root), "--auto-pair-file", str(pair)]), \
                    mock.patch("setup_lan_direct.os.geteuid", return_value=1000), \
                    mock.patch("setup_lan_direct.shutil.which", return_value="/usr/bin/systemctl"), \
                    mock.patch("setup_lan_direct.validate_runtime"), \
                    mock.patch("setup_lan_direct.exclusive", return_value=contextlib.nullcontext()), \
                    mock.patch("setup_lan_direct.repair_pairing") as repair, \
                    mock.patch("setup_lan_direct.update_existing", return_value="Already current") as update, \
                    mock.patch("setup_lan_direct.gui_available", return_value=False):
                setup.main()
            update.assert_called_once_with(root)
            repair.assert_not_called()

    def test_main_first_install_uses_automatically_found_tagged_pairing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "installed"
            pair = Path(directory) / "codex-pairing-0.3.2.json"
            pair.write_text("fixture")
            with mock.patch.object(sys, "argv", ["setup_lan_direct.py", "--root", str(root), "--auto-pair-file", str(pair)]), \
                    mock.patch("setup_lan_direct.os.geteuid", return_value=1000), \
                    mock.patch("setup_lan_direct.shutil.which", return_value="/usr/bin/systemctl"), \
                    mock.patch("setup_lan_direct.validate_runtime"), \
                    mock.patch("setup_lan_direct.exclusive", return_value=contextlib.nullcontext()), \
                    mock.patch("setup_lan_direct.install", return_value={"server": "https://example.test"}) as install, \
                    mock.patch("setup_lan_direct.update_existing") as update, \
                    mock.patch("setup_lan_direct.gui_available", return_value=False):
                setup.main()
            self.assertEqual(install.call_args.args[1], pair)
            update.assert_not_called()

    def test_repair_deleted_identity_but_never_bypass_network_errors_or_cancellation(self):
        for failure in (client.DeviceAuthorizationError("deleted"), client.ConnectorError("offline"),
                        client.Cancelled("cancelled")):
            with self.subTest(error=type(failure).__name__), tempfile.TemporaryDirectory() as directory:
                home = Path(directory)
                root = home / "installed"
                existing = payload(root / "releases/0.2.0")
                client.switch_release(root, existing)
                client.private_write(root / "device.token", "old-private-device-token-123456")
                client.private_write(root / "server-ca.pem", "OLD CERTIFICATE")
                client.private_write(root / "config.json", json.dumps({
                    "format": 1, "server": "https://192.168.1.20:8767", "device_id": "deleted-device"
                }))
                pair = home / "pair.json"
                self.pairing(pair)
                connector = mock.Mock(server="https://192.168.1.20:8767")
                connector.wait_idle.side_effect = failure
                runner = mock.Mock(return_value=subprocess.CompletedProcess([], 0))
                with mock.patch("setup_lan_direct.Path.home", return_value=home), \
                        mock.patch("setup_lan_direct.Transport"), \
                        mock.patch("setup_lan_direct.claim_pairing", return_value=(
                            "new-private-device-token-123456", {"id": "approved-new-device"})) as claim:
                    arguments = dict(runner=runner, allow=lambda _server: True,
                                     transport_class=mock.Mock(), connector_class=mock.Mock(return_value=connector))
                    if isinstance(failure, client.DeviceAuthorizationError):
                        result = setup.repair_pairing(existing, pair, root, **arguments)
                        self.assertEqual(result["device_id"], "approved-new-device")
                        claim.assert_called_once()
                        connector.wait_ready.assert_called_once_with("0.2.0")
                        self.assertEqual((root / "device.token").read_text(), "new-private-device-token-123456")
                    else:
                        with self.assertRaises(type(failure)):
                            setup.repair_pairing(existing, pair, root, **arguments)
                        claim.assert_not_called()
                        connector.wait_ready.assert_not_called()
                        self.assertEqual((root / "device.token").read_text(), "old-private-device-token-123456")
                        self.assertFalse(any("stop" in call.args[0] for call in runner.call_args_list))


class ServiceUnitTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("systemd-analyze"), "systemd parser is unavailable")
    def test_systemd_accepts_working_directory_and_exec_paths_with_spaces_and_specifiers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'computer space %n $cache "quoted"'
            executable = root / "current/venv/bin/python"
            executable.parent.mkdir(parents=True)
            executable.symlink_to(sys.executable)
            unit = setup.service_unit(root, "https://192.168.1.20:8767", root)
            self.assertIn("\nWorkingDirectory=" + str(root / "current").replace("%", "%%") + "\n", unit)
            path = Path(directory) / "codex-connector-regression.service"
            path.write_text(unit)
            result = subprocess.run(["systemd-analyze", "--user", "verify", str(path)],
                                    capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_working_directory_preserves_literal_backslashes_and_dollars(self):
        value = '/tmp/Codex space/$literal/quote"/back\\slash/%n/current'
        self.assertEqual(setup.working_directory(value), value.replace("%", "%%"))

    def test_invalid_paths_cannot_inject_unit_directives(self):
        for value in ("relative/path", "/tmp/path\nExecStart=/bin/false", "/tmp/path\rnew", "/tmp/path\x00"):
            with self.subTest(value=value), self.assertRaises(client.ConnectorError):
                setup.working_directory(value)
        with self.assertRaises(client.ConnectorError):
            setup.quote("/tmp/path\nRestart=no")


class FakeTransport:
    def __init__(self, archive, version="0.2.1"):
        self.archive, self.version = archive, version
        self.calls = []
        self.active_counts = [0]
        self.me_versions = [version]
        self.metadata = {"available": True, "version": version, "size": 1, "sha256": "0" * 64}

    def json(self, path, data=None):
        self.calls.append((path, data))
        if path == "/api/client/release":
            return self.metadata
        if path == "/api/me":
            version = self.me_versions.pop(0) if len(self.me_versions) > 1 else self.me_versions[0]
            return {"client_version": version, "client_version_live": bool(version),
                    "worker_online": bool(version), "worker_paused": True}
        active = self.active_counts.pop(0) if len(self.active_counts) > 1 else self.active_counts[0]
        return {"paused": bool(data and data.get("pause")), "active_runs": active}

    def download(self, target, *_args):
        shutil.copy2(self.archive, target)


class ConnectorUpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "installed"
        self.old = payload(self.root / "releases/0.2.0")
        client.switch_release(self.root, self.old)
        client.private_write(self.root / "device.token", "private-device-token-123456789")
        client.private_write(self.root / "config.json", json.dumps({"server": "https://192.168.1.20:8767"}))
        source = payload(Path(self.temp.name) / "source", "0.2.1")
        archive = Path(self.temp.name) / "client.tar.gz"
        make_archive(archive, source)
        self.transport = FakeTransport(archive)
        self.runner = mock.Mock(return_value=subprocess.CompletedProcess([], 0))
        self.connector = client.Connector(self.root, self.transport, self.runner)

    def tearDown(self):
        self.temp.cleanup()

    def service_actions(self):
        return [call.args[0][2] for call in self.runner.call_args_list if call.args[0][0] == "systemctl"]

    def test_update_waits_for_idle_then_activates_and_unpauses(self):
        self.transport.active_counts = [2, 1, 0]
        checked = []

        def check(active):
            checked.append(active)
            self.assertNotIn("stop", self.service_actions())

        with mock.patch("direct_client.time.sleep"):
            message = self.connector.update(check)
        self.assertIn("0.2.1", message)
        self.assertEqual(checked, [2, 1, 0])
        self.assertEqual((self.root / "current").resolve().name, "0.2.1")
        self.assertEqual(self.transport.calls[-1], ("/api/client/maintenance", {"pause": False}))
        self.assertEqual((self.root / "device.token").read_text(), "private-device-token-123456789")

    def test_published_server_manifest_remains_compatible_with_installed_updater(self):
        from types import SimpleNamespace
        from codex_lan.client_access import ClientAccess

        server_data = Path(self.temp.name) / "server"
        releases = server_data / "releases"
        releases.mkdir(parents=True)
        archive = self.transport.archive.read_bytes()
        (releases / "client.tar.gz").write_bytes(archive)
        (releases / "Install-Codex-Connector.sh").write_text("# Synthetic installer\n")
        (releases / "release.json").write_text(json.dumps({
            "version": self.transport.version,
            "size": len(archive), "sha256": hashlib.sha256(archive).hexdigest(),
        }))
        self.transport.metadata = ClientAccess.release_info(SimpleNamespace(data_dir=server_data))
        message = self.connector.update()
        self.assertIn("updated to 0.2.1", message)
        self.assertEqual((self.root / "current").resolve().name, "0.2.1")
        self.assertEqual((self.root / "device.token").read_text(), "private-device-token-123456789")

    def test_cancelled_drain_preserves_running_version_and_clears_gate(self):
        self.transport.active_counts = [1]

        def cancel(_active):
            raise client.Cancelled("Cancelled")

        with self.assertRaises(client.Cancelled):
            self.connector.update(cancel)
        self.assertEqual((self.root / "current").resolve(), self.old)
        self.assertNotIn("stop", self.service_actions())
        self.assertEqual(self.transport.calls[-1], ("/api/client/maintenance", {"pause": False}))

    def test_failed_activation_rolls_back_and_restarts_original(self):
        failed = []

        def run(command, **_kwargs):
            if command[:3] == ["systemctl", "--user", "start"] and not failed:
                failed.append(True)
                raise subprocess.CalledProcessError(1, command)
            return subprocess.CompletedProcess(command, 0)

        self.runner.side_effect = run
        with self.assertRaises(subprocess.CalledProcessError):
            self.connector.update()
        self.assertEqual((self.root / "current").resolve(), self.old)
        self.assertEqual(self.service_actions().count("start"), 2)
        self.assertEqual(self.transport.calls[-1], ("/api/client/maintenance", {"pause": False}))

    def test_downgrade_incompatible_and_connected_same_version_never_execute_install(self):
        for value in ("0.1.0", "1.0.0", "../escape"):
            self.transport.metadata["version"] = value
            with self.assertRaises(client.ConnectorError):
                self.connector.update()
        self.transport.metadata["version"] = "0.2.0"
        self.transport.me_versions = ["0.2.0"]
        self.assertIn("up to date", self.connector.update())
        self.runner.assert_not_called()

    def test_same_version_restarts_and_verifies_an_offline_stale_service(self):
        self.transport.metadata["version"] = "0.2.0"
        self.transport.me_versions = ["", "0.2.0"]
        with mock.patch("direct_client.time.sleep"):
            message = self.connector.update()
        self.assertIn("restarted and verified", message)
        self.assertEqual(self.service_actions(), ["stop", "start", "is-active"])
        self.assertIn(("/api/client/maintenance", {"pause": True}), self.transport.calls)
        self.assertEqual(self.transport.calls[-1], ("/api/client/maintenance", {"pause": False}))

    def test_same_version_persisted_offline_report_is_not_treated_as_live(self):
        self.transport.metadata["version"] = "0.2.0"
        responses = iter([
            {"client_version": "0.2.0", "client_version_live": False,
             "connector_online": False, "worker_online": True},
            {"client_version": "0.2.0", "client_version_live": True, "worker_online": True},
        ])
        original = self.transport.json

        def request(path, data=None):
            if path == "/api/me":
                self.transport.calls.append((path, data))
                return next(responses)
            return original(path, data)

        self.transport.json = request
        with mock.patch("direct_client.time.sleep"):
            message = self.connector.update()
        self.assertIn("restarted and verified", message)
        self.assertEqual(self.service_actions(), ["stop", "start", "is-active"])

    def test_same_version_waits_for_connected_old_process_before_restart(self):
        self.transport.metadata["version"] = "0.2.0"
        self.transport.me_versions = ["0.1.9", "0.2.0"]
        self.transport.active_counts = [1, 0]
        with mock.patch("direct_client.time.sleep"):
            message = self.connector.update()
        self.assertIn("restarted and verified", message)
        self.assertEqual(self.service_actions(), ["stop", "start", "is-active"])
        self.assertEqual([call for call in self.transport.calls if call[0] == "/api/client/maintenance"][:2], [
            ("/api/client/maintenance", {"pause": True}),
            ("/api/client/maintenance", {"pause": True}),
        ])

    def test_same_version_does_not_restart_a_deliberately_paused_connector(self):
        self.transport.metadata["version"] = "0.2.0"
        self.transport.me_versions = [""]
        client.private_write(self.root / "paused", "1")
        self.assertIn("remains paused", self.connector.update())
        self.runner.assert_not_called()
        self.assertFalse(any(path == "/api/client/maintenance" for path, _data in self.transport.calls))

    def test_pause_persists_across_login_and_resume_restores_access(self):
        self.connector.pause()
        self.assertTrue((self.root / "paused").exists())
        self.assertEqual(self.service_actions(), ["stop", "disable"])
        self.connector.resume()
        self.assertFalse((self.root / "paused").exists())
        self.assertEqual(self.service_actions(), ["stop", "disable", "enable", "start"])
        self.assertEqual(self.transport.calls[-1], ("/api/client/maintenance", {"pause": False}))

    def test_update_does_not_resume_a_deliberately_paused_connector(self):
        client.private_write(self.root / "paused", "1")
        self.connector.update()
        self.assertNotIn("start", self.service_actions())
        self.assertTrue((self.root / "paused").exists())
        self.assertNotIn(("/api/client/maintenance", {"pause": False}), self.transport.calls)

    def test_cancelled_update_preserves_existing_paused_access(self):
        client.private_write(self.root / "paused", "1")
        self.transport.active_counts = [1]

        def cancel(_active):
            raise client.Cancelled("Cancelled")

        with self.assertRaises(client.Cancelled):
            self.connector.update(cancel)
        self.assertNotIn(("/api/client/maintenance", {"pause": False}), self.transport.calls)
        self.assertNotIn("start", self.service_actions())
        self.assertEqual((self.root / "current").resolve(), self.old)


if __name__ == "__main__":
    unittest.main()
