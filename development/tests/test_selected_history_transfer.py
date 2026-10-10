"""Offline wrapper checks: all installation data and service state are synthetic."""

from contextlib import ExitStack
import fcntl
import importlib.util
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


MAINTENANCE = Path(__file__).resolve().parents[2] / "setup/maintenance"
sys.path.insert(0, str(MAINTENANCE))
SPEC = importlib.util.spec_from_file_location("selected_history_wrapper", MAINTENANCE / "transfer_lan_history.py")
transfer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(transfer)


class FakeServices:
    def __init__(self, active=()):
        self.running = set(active)
        self.calls = []

    def active(self, name):
        return name in self.running

    def run(self, action, names):
        self.calls.append((action, list(names)))
        if action == "stop":
            self.running.difference_update(names)
        elif action == "start":
            self.running.update(names)
        else:
            raise AssertionError(action)


class SelectedHistoryWrapperTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.bundle, self.installation, self.home = (self.root / name for name in ("bundle", "installation", "home"))
        self.server, self.worker = self.installation / "server", self.installation / "host-worker"
        for path in (self.bundle, self.server / "uploads", self.worker, self.home):
            path.mkdir(parents=True, mode=0o700)
        self.database = self.server / "server.sqlite3"
        with sqlite3.connect(self.database) as db:
            db.execute("CREATE TABLE sentinel(value TEXT)")
            db.execute("INSERT INTO sentinel VALUES ('destination state')")
        self.login = self.worker / "auth.json"
        self.login.write_text("synthetic destination login")
        self.attachment = {"id": "attachment", "file": "uploads/" + "a" * 64,
                           "size": 5, "sha256": "b" * 64}
        self.metadata = {
            "format": transfer.FORMAT, "source_home": "/home/example", "files": [],
            "lan": {"source_owner_id": "source-owner", "thread_ids": ["thread"],
                    "attachments": [self.attachment], "counts": {"rooms": 1}},
            "native": {"codex_version": "0.159.3", "root_thread_ids": ["thread"],
                       "thread_ids": ["thread"], "counts": {"threads": 1}, "files": []},
        }
        self.services = FakeServices(transfer.SERVICES)
        self.native_calls = 0
        self.lan_calls = 0

    def mocks(self, *, version="codex-cli 0.159.3", empty_paths=(), lan_failure=False):
        stack = ExitStack()
        stack.enter_context(patch.object(transfer, "validate_bundle", return_value=self.metadata))
        stack.enter_context(patch.object(transfer.subprocess, "run", return_value=subprocess.CompletedProcess(
            [], 0, stdout=version + "\n", stderr="")))
        stack.enter_context(patch.object(transfer.lan, "preflight_history", return_value={"admin_id": "destination-owner"}))
        stack.enter_context(patch.object(transfer.native, "empty_store_paths", return_value=list(empty_paths)))
        stack.enter_context(patch.object(transfer.native, "import_native", side_effect=self.install_native))
        stack.enter_context(patch.object(transfer.lan, "import_history", side_effect=lambda *args: self.install_lan(lan_failure)))
        return stack

    def install_native(self, *args):
        self.native_calls += 1
        state, sessions = self.worker / "state_5.sqlite", self.worker / "sessions"
        state.write_bytes(b"synthetic imported native database")
        sessions.mkdir()
        (sessions / "selected.jsonl").write_text("synthetic memory")
        return {"threads": 1, "created_paths": [str(state), str(sessions)]}

    def install_lan(self, fail):
        self.lan_calls += 1
        target = self.server / self.attachment["file"]
        if target.exists():
            raise ValueError("An attachment destination already exists")
        with sqlite3.connect(self.database) as db:
            db.execute("UPDATE sentinel SET value='imported state'")
        target.write_bytes(b"image")
        if fail:
            # The LAN helper owns cleanup of the attachment it created.
            target.unlink()
            raise ValueError("synthetic late LAN failure")
        return {"counts": {"rooms": 1}, "admin_id": "destination-owner"}

    def restore(self):
        return transfer.restore_bundle(self.bundle, self.installation, self.home, self.root / "codex", self.services)

    def saved_value(self):
        with sqlite3.connect(self.database) as db:
            return db.execute("SELECT value FROM sentinel").fetchone()[0]

    def write_bundle(self):
        """Use real manifest/file validation; inner formats have independent tests."""
        names = [*transfer.TOOLS, "README.txt", "lan/history.sqlite3", "native/state_5.sqlite"]
        self.metadata["lan"]["attachments"] = []
        for name in names:
            path = self.bundle / name
            path.parent.mkdir(exist_ok=True, parents=True)
            path.write_bytes(("synthetic " + name).encode())
        self.metadata["native"]["files"] = [transfer.file_record(self.bundle / "native/state_5.sqlite", "state_5.sqlite")]
        self.metadata["files"] = [transfer.file_record(self.bundle / name, name) for name in names]
        (self.bundle / "manifest.json").write_text(json.dumps(self.metadata))

    def validate(self):
        with patch.object(transfer.lan, "validate_history"), patch.object(transfer.native, "validate_native"):
            return transfer.validate_bundle(self.bundle)

    def test_bundle_hash_and_path_tampering_are_rejected(self):
        self.write_bundle()
        self.assertEqual(self.validate()["format"], transfer.FORMAT)
        target = self.bundle / "lan/history.sqlite3"
        original = target.read_bytes()
        target.write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "checksum"):
            self.validate()
        target.write_bytes(original)
        self.metadata["files"][0]["path"] = "../outside"
        (self.bundle / "manifest.json").write_text(json.dumps(self.metadata))
        with self.assertRaisesRegex(ValueError, "payload path"):
            self.validate()
        self.assertEqual(self.services.calls, [])

    def test_bundle_symlink_and_mismatched_native_roots_are_rejected(self):
        self.write_bundle()
        target = self.bundle / "lan/history.sqlite3"
        outside = self.root / "outside.sqlite3"
        target.rename(outside)
        target.symlink_to(outside)
        with self.assertRaisesRegex(ValueError, "symbolic links"):
            self.validate()
        target.unlink()
        outside.rename(target)
        self.metadata["native"]["root_thread_ids"] = ["unrelated"]
        (self.bundle / "manifest.json").write_text(json.dumps(self.metadata))
        with self.assertRaisesRegex(ValueError, "histories do not match"):
            self.validate()

    def test_git_checkout_and_symlink_destinations_are_rejected(self):
        (self.installation / ".git").mkdir()
        with self.mocks(), self.assertRaisesRegex(ValueError, "Git"):
            self.restore()
        (self.installation / ".git").rmdir()
        alias = self.root / "installation-alias"
        alias.symlink_to(self.installation, target_is_directory=True)
        with self.mocks(), self.assertRaisesRegex(ValueError, "symbolic links"):
            transfer.restore_bundle(self.bundle, alias, self.home, self.root / "codex", self.services)
        self.assertEqual(self.services.calls, [])

    def test_wrong_codex_version_and_source_destination_do_not_stop_services(self):
        with self.mocks(version="codex-cli different"), self.assertRaisesRegex(ValueError, "verified Codex version"):
            self.restore()
        with self.mocks(), patch.object(transfer.lan, "preflight_history", side_effect=ValueError("separate installation")):
            with self.assertRaisesRegex(ValueError, "separate installation"):
                self.restore()
        self.assertEqual(self.services.calls, [])
        self.assertEqual(self.native_calls, 0)

    def test_active_updater_prevents_service_changes(self):
        self.services.running.add(transfer.MUTATORS[0])
        with self.mocks(), self.assertRaisesRegex(ValueError, "update or recovery"):
            self.restore()
        self.assertEqual(self.services.calls, [])
        self.assertEqual(self.native_calls, 0)

    def test_held_server_lock_restores_services_without_import(self):
        with (self.server / "server.lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.mocks(), self.assertRaises(BlockingIOError):
                self.restore()
        self.assertEqual(self.services.running, set(transfer.SERVICES))
        self.assertEqual(self.native_calls, 0)
        self.assertEqual(self.saved_value(), "destination state")

    def test_success_preserves_login_and_prior_inactive_services(self):
        self.services = FakeServices([transfer.SERVICES[0], transfer.SERVICES[2]])
        with self.mocks():
            result = self.restore()
        self.assertEqual(self.saved_value(), "imported state")
        self.assertEqual(self.login.read_text(), "synthetic destination login")
        self.assertEqual(self.services.running, {transfer.SERVICES[0], transfer.SERVICES[2]})
        self.assertEqual(self.services.calls[-1], ("start", [transfer.SERVICES[2], transfer.SERVICES[0]]))
        self.assertTrue((Path(result["backup"]) / "server.sqlite3").is_file())
        self.assertTrue((Path(result["backup"]) / "import-complete.json").is_file())

    def test_lan_failure_restores_database_native_files_and_services(self):
        original = self.worker / "state_5.sqlite"
        original.write_bytes(b"empty destination native state")
        with self.mocks(empty_paths=[original], lan_failure=True), self.assertRaisesRegex(ValueError, "late LAN failure"):
            self.restore()
        self.assertEqual(self.saved_value(), "destination state")
        self.assertEqual(original.read_bytes(), b"empty destination native state")
        self.assertFalse((self.worker / "sessions").exists())
        self.assertFalse((self.server / self.attachment["file"]).exists())
        self.assertEqual(self.login.read_text(), "synthetic destination login")
        self.assertEqual(self.services.running, set(transfer.SERVICES))

    def test_preexisting_attachment_collision_never_deletes_user_file(self):
        target = self.server / self.attachment["file"]
        target.write_bytes(b"preexisting orphaned upload")
        with self.mocks(), self.assertRaises(ValueError):
            self.restore()
        self.assertTrue(target.exists(), "Rollback must not delete a file that predated this import")
        self.assertEqual(target.read_bytes(), b"preexisting orphaned upload")
        self.assertEqual(self.saved_value(), "destination state")
        self.assertEqual(self.services.running, set(transfer.SERVICES))

    def test_failed_rollback_leaves_destination_services_stopped(self):
        with self.mocks(lan_failure=True), patch.object(transfer, "restore_database", side_effect=OSError("synthetic disk error")):
            with self.assertRaisesRegex(RuntimeError, "services remain stopped"):
                self.restore()
        self.assertFalse(self.services.running.intersection(transfer.SERVICES))
        self.assertFalse(any(action == "start" for action, names in self.services.calls))
        self.assertTrue(list((self.installation / "transfer-backups").glob("before-import-*/server.sqlite3")))

    def test_restart_failure_reports_committed_history_without_retry(self):
        original_run = self.services.run

        def fail_start(action, names):
            if action == "start":
                raise RuntimeError("synthetic service start failure")
            original_run(action, names)

        self.services.run = fail_start
        with self.mocks(), self.assertRaisesRegex(RuntimeError, "History was imported; do not retry"):
            self.restore()
        self.assertEqual(self.saved_value(), "imported state")
        self.assertTrue((self.worker / "sessions").exists())
        self.assertTrue((self.server / self.attachment["file"]).exists())
        self.assertEqual(self.login.read_text(), "synthetic destination login")


if __name__ == "__main__":
    unittest.main()
