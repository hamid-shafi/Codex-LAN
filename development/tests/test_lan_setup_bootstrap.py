"""CODEX: Verify portable setup and private migration helpers without altering real services."""

from __future__ import annotations

import io
import json
import sqlite3
import stat
import subprocess
import tarfile
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import backup_lan_history
import install_lan_server
import prepare_lan_connector


class PortableSetupTests(unittest.TestCase):
    def test_setup_snapshot_contains_only_trusted_helpers_and_package(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "checkout"
            source.mkdir()
            for name in install_lan_server.SETUP_FILES:
                (source / name).write_text("fixture")
            (source / "codex_lan").mkdir()
            (source / "codex_lan/__init__.py").write_text("fixture")
            (source / "admin.token").write_text("must not transfer")
            (source / "auth.json").write_text("must not transfer")
            target = install_lan_server.install_setup_source(source, root / "private/setup-source")
            self.assertTrue((target / "prepare_lan_connector.py").is_file())
            self.assertTrue((target / "codex_lan/__init__.py").is_file())
            self.assertFalse((target / "admin.token").exists())
            self.assertFalse((target / "auth.json").exists())
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o700)
            (target / "prepare_lan_connector.py").unlink()
            (target / "prepare_lan_connector.py").symlink_to(source / "prepare_lan_connector.py")
            with self.assertRaisesRegex(ValueError, "symbolic"):
                install_lan_server.install_setup_source(source, target)

    def test_new_install_uses_venv_pip_and_detected_home_not_temporary_bootstrap(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            codex = home / "codex"
            codex.write_text("fixture")
            codex.chmod(0o700)
            calls = []

            def run(command, **kwargs):
                calls.append(command)
                return subprocess.CompletedProcess(command, 3 if "is-active" in command else 0)

            with patch.object(Path, "home", return_value=home), \
                    patch.object(install_lan_server, "prerequisites", return_value=[]), \
                    patch.object(install_lan_server, "resolve_codex", return_value=codex), \
                    patch.object(install_lan_server.shutil, "copytree"), \
                    patch.object(install_lan_server, "install_setup_source", return_value=home / "setup-source"), \
                    patch.object(install_lan_server.subprocess, "run", side_effect=run), redirect_stdout(io.StringIO()):
                install_lan_server.main([])
            pip_call = next(command for command in calls if "pip" in command)
            self.assertEqual(pip_call[1:5], ["-m", "pip", "--isolated", "install"])
            self.assertFalse(any("/tmp/codex-lan-bootstrap-pip.pyz" in str(part) for command in calls for part in command))
            self.assertFalse(any("restart" in command or "enable" in command for command in calls))
            unit = (home / ".config/systemd/user/codex-lan-server.service").read_text()
            self.assertIn(str(home), unit)
            self.assertIn("Restart=always", unit)

    def test_running_host_is_not_reinstalled(self):
        with patch.object(install_lan_server, "prerequisites", return_value=[]), \
                patch.object(install_lan_server, "resolve_codex", return_value=Path("/tmp/codex-test")), \
                patch.object(install_lan_server.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)), \
                patch.object(install_lan_server, "private_directory") as directory:
            with self.assertRaises(SystemExit):
                install_lan_server.main([])
            directory.assert_not_called()

    def test_control_characters_are_rejected_before_unit_rendering(self):
        for value in ("/tmp/path\nExecStart=/bin/false", "/tmp/path\x00", "/tmp/path\x7f"):
            with self.assertRaises(ValueError):
                install_lan_server.service_unit("codex-lan-server", ["python", value], "/tmp/source")

    def test_prerequisite_auth_failure_does_not_disclose_file_contents(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            (home / ".codex").mkdir()
            (home / ".codex/auth.json").write_text('{"OPENAI_API_KEY":"never-display-this"}')
            with patch.object(Path, "home", return_value=home), \
                    patch.object(install_lan_server, "resolve_codex", return_value=home / "codex"), \
                    patch.object(install_lan_server.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)):
                issues = install_lan_server.prerequisites(require_login=True)
            self.assertIn("Sign in on this host", "\n".join(issues))
            self.assertNotIn("never-display-this", "\n".join(issues))


class ConnectorPreparationTests(unittest.TestCase):
    def test_valid_offline_pip_reuses_zipapp_without_network(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "pip.pyz"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("__main__.py", "fixture")
            with patch.object(prepare_lan_connector.urllib.request, "urlopen") as download:
                self.assertEqual(prepare_lan_connector.prepare_pip(path, offline=True), path)
                download.assert_not_called()
            actual = path.with_name("actual.pyz")
            path.rename(actual)
            path.symlink_to(actual)
            self.assertFalse(prepare_lan_connector.valid_pip(path))

    def test_offline_missing_pip_errors_without_download(self):
        with tempfile.TemporaryDirectory() as temporary, \
                patch.object(prepare_lan_connector.urllib.request, "urlopen") as download:
            with self.assertRaisesRegex(ValueError, "missing"):
                prepare_lan_connector.prepare_pip(Path(temporary) / "pip.pyz", offline=True)
            download.assert_not_called()

    def test_wheel_preparation_requests_supported_interpreters(self):
        with tempfile.TemporaryDirectory() as temporary:
            calls = []
            prepare_lan_connector.prepare_wheels(Path(temporary), Path(temporary) / "pip.pyz",
                                                  runner=lambda command, **kwargs: calls.append(command))
            versions = [command[command.index("--python-version") + 1] for command in calls]
            self.assertEqual(tuple(versions), prepare_lan_connector.SUPPORTED_PYTHONS)
            self.assertTrue(all("--isolated" in command and "--only-binary=:all:" in command for command in calls))


class PrivateHistoryBackupTests(unittest.TestCase):
    def history(self, root):
        data = root / "server"
        data.mkdir(mode=0o700)
        uploads = data / "uploads"
        uploads.mkdir(mode=0o700)
        attachment = uploads / "attachment"
        attachment.write_bytes(b"private attachment")
        database = sqlite3.connect(data / "server.sqlite3")
        database.executescript(
            "CREATE TABLE devices(id TEXT,token_hash TEXT,status TEXT);"
            "CREATE TABLE rooms(id TEXT,read_only_reason TEXT);"
            "CREATE TABLE messages(id TEXT,content TEXT);"
            "CREATE TABLE runs(id TEXT,status TEXT,error TEXT,finished_at REAL);"
            "CREATE TABLE attachments(id TEXT,path TEXT,size INTEGER);"
            "CREATE TABLE browser_logins(password_verifier TEXT);"
            "CREATE TABLE browser_sessions(token_hash TEXT);"
            "CREATE TABLE client_pairings(code_hash TEXT);"
            "CREATE TABLE device_credentials(token_hash TEXT);"
            "INSERT INTO devices VALUES('person','old-credential-hash','approved');"
            "INSERT INTO rooms VALUES('room','');"
            "INSERT INTO messages VALUES('message','private chat');"
            "INSERT INTO runs VALUES('run','running',NULL,NULL);"
            "INSERT INTO browser_logins VALUES('old-password-verifier');"
            "INSERT INTO browser_sessions VALUES('old-session-hash');"
            "INSERT INTO client_pairings VALUES('old-pairing-hash');"
            "INSERT INTO device_credentials VALUES('old-device-hash');"
        )
        database.execute("INSERT INTO attachments VALUES(?,?,?)", ("attachment-id", str(attachment), attachment.stat().st_size))
        database.commit()
        database.close()
        (data / "admin.token").write_text("private-admin-token")
        (data / "auth.json").write_text("private-openai-login")
        return data

    def test_consistent_archive_has_history_uploads_but_no_usable_credentials(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = self.history(root)
            output = root / "backups/private-history.tar.gz"
            counts = backup_lan_history.create_backup(data, output)
            self.assertEqual(counts["messages"], 1)
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)
            with tarfile.open(output) as archive:
                names = archive.getnames()
                self.assertTrue(all(name == "history.sqlite3" or name == "manifest.json" or name.startswith("uploads/") for name in names))
                self.assertTrue(all(member.isfile() and member.mode == 0o600 for member in archive.getmembers()))
                manifest = json.load(archive.extractfile("manifest.json"))
                self.assertFalse(manifest["resume_supported"])
                copied = root / "inspection.sqlite3"
                copied.write_bytes(archive.extractfile("history.sqlite3").read())
            with sqlite3.connect(copied) as database:
                for table in backup_lan_history.CREDENTIAL_TABLES[:4]:
                    self.assertEqual(database.execute("SELECT count(*) FROM " + table).fetchone()[0], 0)
                device = database.execute("SELECT token_hash,status FROM devices").fetchone()
                self.assertNotEqual(device[0], "old-credential-hash")
                self.assertEqual(device[1], "revoked")
                self.assertEqual(database.execute("SELECT status FROM runs").fetchone()[0], "interrupted")
                self.assertTrue(database.execute("SELECT path FROM attachments").fetchone()[0].startswith("uploads/"))
            with sqlite3.connect(data / "server.sqlite3") as original:
                self.assertEqual(original.execute("SELECT status FROM devices").fetchone()[0], "approved")
                self.assertEqual(original.execute("SELECT status FROM runs").fetchone()[0], "running")
                self.assertEqual(original.execute("SELECT count(*) FROM browser_logins").fetchone()[0], 1)

    def test_existing_backup_and_symlinked_upload_are_not_overwritten_or_read(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = self.history(root)
            backups = root / "backups"
            backups.mkdir(mode=0o700)
            target = backups / "existing.tar.gz"
            target.write_text("existing")
            with self.assertRaisesRegex(ValueError, "already exists"):
                backup_lan_history.create_backup(data, target)
            self.assertEqual(target.read_text(), "existing")
            attachment = data / "uploads/attachment"
            actual = data / "uploads/actual"
            attachment.rename(actual)
            attachment.symlink_to(actual)
            with self.assertRaisesRegex(ValueError, "regular file"):
                backup_lan_history.create_backup(data, backups / "private-refused.tar.gz")
            self.assertFalse((backups / "private-refused.tar.gz").exists())


if __name__ == "__main__":
    unittest.main()
