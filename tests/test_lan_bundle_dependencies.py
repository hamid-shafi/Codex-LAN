"""CODEX: Ensure released clients contain their imports and old identities survive bootstrap updates."""

import ast
from contextlib import redirect_stdout
from io import StringIO
import json
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import build_lan_bundle
import build_lan_installer
import deploy_lan_backend
import direct_client
import setup_lan_client
import setup_lan_direct


class BundleDependencyTests(unittest.TestCase):
    def test_worker_local_dependencies_are_present_in_both_client_installers_and_backend(self):
        source = Path(__file__).resolve().parents[1]
        required = {"__init__.py", "worker.py"}
        pending = ["worker.py"]
        while pending:
            name = pending.pop()
            tree = ast.parse((source / "codex_lan" / name).read_text())
            for item in ast.walk(tree):
                if isinstance(item, ast.ImportFrom) and item.level == 1 and item.module:
                    dependency = item.module + ".py"
                    if dependency not in required:
                        required.add(dependency)
                        pending.append(dependency)
        self.assertTrue({"egress.py", "recovery.py", "transport.py", "model_registry.py"} <= required)
        for name in required:
            with self.subTest(name=name):
                self.assertIn("codex_lan/" + name, build_lan_bundle.CLIENT_FILES)
                self.assertIn("codex_lan/" + name, direct_client.REQUIRED)
                self.assertIn(name, setup_lan_client.PACKAGE_FILES)
                self.assertIn(name, deploy_lan_backend.FILES)

    def test_shell_installer_rejects_archive_missing_worker_dependency(self):
        for missing in ("codex_lan/media.py", "codex_lan/model_registry.py"):
            with self.subTest(missing=missing), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                package = root / "Codex-LAN-Client"
                (package / "codex_lan").mkdir(parents=True)
                (package / "bin").mkdir()
                for name in (
                    "setup_lan_client.py", "codex_lan/worker.py", "codex_lan/media.py",
                    "codex_lan/model_registry.py", "pip.pyz", "bin/codex",
                ):
                    if name != missing:
                        (package / name).write_text("fixture")
                payload = root / "client.tar.gz"
                with tarfile.open(payload, "w:gz") as archive:
                    archive.add(package, arcname=package.name)
                with self.assertRaisesRegex(ValueError, "archive is incomplete"):
                    build_lan_installer.build(payload, root / "installer.sh")

    def test_paired_bootstrap_uses_new_updater_and_preserves_existing_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            root = home / "existing"
            direct_client.private_write(root / "device.token", "approved-fixture-token")
            direct_client.private_write(root / "config.json", json.dumps({"server": "https://192.168.1.20:8767"}))
            identity = (root / "device.token").read_bytes()
            connector = mock.Mock()
            connector.server = "https://192.168.1.20:8767"
            connector.update.return_value = "Connector updated to 0.2.1."
            runner, factory = mock.Mock(), mock.Mock(return_value=connector)
            with mock.patch("setup_lan_direct.Path.home", return_value=home):
                result = setup_lan_direct.update_existing(root, runner, factory)
            self.assertIn("0.2.1", result)
            self.assertEqual((root / "device.token").read_bytes(), identity)
            connector.update.assert_called_once_with()
            runner.assert_called_once_with(["systemctl", "--user", "daemon-reload"], check=True)
            self.assertIn("\nWorkingDirectory=" + str(root / "current") + "\n",
                          (home / ".config/systemd/user/codex-lan-direct.service").read_text())

    def test_rerun_installer_without_pair_routes_existing_identity_to_update(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "existing"
            direct_client.private_write(root / "device.token", "approved-fixture-token")
            direct_client.private_write(root / "config.json", json.dumps({"server": "https://192.168.1.20:8767"}))
            identity = (root / "device.token").read_bytes()
            arguments = ["setup_lan_direct.py", "--root", str(root)]
            with mock.patch.object(sys, "argv", arguments), \
                    mock.patch("setup_lan_direct.os.geteuid", return_value=1000), \
                    mock.patch("setup_lan_direct.validate_runtime"), \
                    mock.patch("setup_lan_direct.shutil.which", return_value="/usr/bin/systemctl"), \
                    mock.patch("setup_lan_direct.gui_available", return_value=False), \
                    mock.patch("setup_lan_direct.update_existing", return_value="Connector updated to 0.3.0.") as update, \
                    mock.patch("setup_lan_direct.install") as fresh_install, redirect_stdout(StringIO()):
                setup_lan_direct.main()
            update.assert_called_once_with(root)
            fresh_install.assert_not_called()
            self.assertEqual((root / "device.token").read_bytes(), identity)

    def test_host_admin_bootstrap_cannot_enable_duplicate_connector(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            root = home / "existing"
            direct_client.private_write(root / "device.token", "same-private-fixture")
            direct_client.private_write(home / ".local/share/codex-lan/server/admin.token", "same-private-fixture")
            runner, factory = mock.Mock(), mock.Mock()
            with mock.patch("setup_lan_direct.Path.home", return_value=home), self.assertRaises(direct_client.ConnectorError):
                setup_lan_direct.update_existing(root, runner, factory)
            runner.assert_not_called()
            factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
