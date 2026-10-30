"""CODEX: Idle-only activation, admission exclusion, and automatic file rollback."""

import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch, sentinel
from urllib.request import Request

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from project_layout import configure_imports, source_file, source_root

configure_imports(__file__)

import deploy_lan_backend
from codex_lan import __version__
from deploy_lan_backend import activate as _activate


def activate(db, destination, contents, previous, *args, **kwargs):
    if kwargs.get("maintenance_file") is not None:
        complete_contents = {name: f"new:{name}".encode() for name in deploy_lan_backend.FILES}
        complete_previous = {name: None for name in deploy_lan_backend.FILES}
        complete_contents.update(contents)
        complete_previous.update(previous)
        contents, previous = complete_contents, complete_previous
        marker = Path(kwargs["maintenance_file"])
        recovery = marker.parent.parent / "recovery"
        recovery.mkdir(parents=True, exist_ok=True)
        source = Path(deploy_lan_backend.__file__).read_bytes()
        digest = deploy_lan_backend._sha256_bytes(source)
        helper = recovery / f"deploy_lan_backend_recovery-{digest}.py"
        helper.write_bytes(source)
        kwargs.setdefault("recovery_helper", helper)
    return _activate(db, destination, contents, previous, *args, **kwargs)


class BackendDeploymentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.installation = self.root / "installation"
        self.destination = self.installation / "code/codex_lan"
        self.destination.mkdir(parents=True)
        server = self.installation / "server"
        server.mkdir()
        self.database = server / "server.sqlite3"
        self.db = sqlite3.connect(self.database, isolation_level=None)
        self.db.execute("CREATE TABLE runs(status TEXT)")
        (self.destination / "runtime.py").write_bytes(b"old")
        self.marker = server / "backend-update.maintenance"
        self.rollback = self.installation / "updates/before"
        self.calls = []

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def activate(self, check=lambda: True):
        return activate(self.db, self.destination, {"runtime.py": b"new", "activity.py": b"activity"},
                        {"runtime.py": b"old", "activity.py": None}, self.action, check,
                        maintenance_file=self.marker, rollback_directory=self.rollback,
                        rollback_check=lambda: True)

    def action(self, action):
        self.calls.append(action)
        if action == "stop" and len(self.calls) == 1:
            writer = sqlite3.connect(self.database, timeout=0)
            try:
                with self.assertRaises(sqlite3.OperationalError):
                    writer.execute("INSERT INTO runs VALUES ('queued')")
            finally:
                writer.close()

    def test_managed_release_manifest_covers_every_top_level_package_module(self):
        package = source_file(source_root(deploy_lan_backend.__file__), "codex_lan")
        self.assertEqual(set(deploy_lan_backend.FILES), {path.name for path in package.glob("*.py")})

    def test_active_and_queued_prompts_are_never_stopped(self):
        for status in ("running", "queued"):
            self.db.execute("INSERT INTO runs VALUES (?)", (status,))
            self.assertFalse(self.activate())
            self.assertEqual(self.calls, [])
            self.assertEqual((self.destination / "runtime.py").read_bytes(), b"old")
            self.db.execute("DELETE FROM runs")

    def test_idle_activation_holds_admission_lock_until_stopped(self):
        self.assertTrue(self.activate())
        self.assertEqual(self.calls, ["stop", "start"])
        self.assertEqual((self.destination / "runtime.py").read_bytes(), b"new")
        self.assertFalse(self.marker.exists())
        self.assertFalse(self.db.in_transaction)

    def test_native_watchdog_policy_is_loaded_after_stop_and_before_first_start(self):
        events = []

        def action(command):
            events.append(command)

        def prepare_start():
            events.append("watchdog-policy")
            self.assertEqual((self.destination / "runtime.py").read_bytes(), b"new")

        self.assertTrue(activate(
            self.db, self.destination, {"runtime.py": b"new"}, {"runtime.py": b"old"},
            action=action, check=lambda: True, prepare_start=prepare_start,
        ))
        self.assertEqual(events, ["stop", "watchdog-policy", "start"])

    def test_admission_marker_remains_through_start_and_health_verification(self):
        calls = []

        def action(command):
            calls.append(command)
            self.assertTrue(self.marker.exists())
            metadata = json.loads(self.marker.read_text())
            self.assertRegex(metadata["recovery_helper_sha256"], r"^[0-9a-f]{64}$")
            self.assertTrue(metadata["recovery_helper"].endswith(
                f'-{metadata["recovery_helper_sha256"]}.py'
            ))

        def check():
            self.assertTrue(self.marker.exists())
            return True

        self.assertTrue(activate(
            self.db, self.destination, {"runtime.py": b"new"}, {"runtime.py": b"old"},
            action=action, check=check, maintenance_file=self.marker,
            rollback_directory=self.rollback,
        ))
        self.assertEqual(calls, ["stop", "start"])
        self.assertFalse(self.marker.exists())

    def test_partial_initial_stop_restores_old_services_after_unlocking(self):
        for error in (subprocess.CalledProcessError(1, ["systemctl"]),
                      subprocess.TimeoutExpired(["systemctl"], 45), KeyboardInterrupt()):
            with self.subTest(error=type(error).__name__):
                calls = []
                def action(command):
                    calls.append(command)
                    if command == "stop":
                        self.assertTrue(self.db.in_transaction)
                        raise error
                    self.assertFalse(self.db.in_transaction)
                    self.assertEqual((self.destination / "runtime.py").read_bytes(), b"old")
                with self.assertRaises(type(error)):
                    activate(self.db, self.destination, {"runtime.py": b"new"},
                             {"runtime.py": b"old"}, action=action)
                self.assertEqual(calls, ["stop", "start"])
                self.assertFalse((self.destination / "activity.py").exists())

    def test_partial_stop_recovery_failure_reports_unrestored_services(self):
        calls = []
        def action(command):
            calls.append(command)
            raise subprocess.CalledProcessError(1, ["systemctl", command])
        with self.assertRaisesRegex(RuntimeError, "previous services could not be restored"):
            activate(self.db, self.destination, {"runtime.py": b"new"},
                     {"runtime.py": b"old"}, action=action)
        self.assertEqual(calls, ["stop", "start", "start"])
        self.assertFalse(self.db.in_transaction)
        self.assertEqual((self.destination / "runtime.py").read_bytes(), b"old")

    def test_failed_initial_stop_and_both_restarts_keep_admission_closed(self):
        calls = []

        def action(command):
            calls.append(command)
            raise subprocess.CalledProcessError(1, ["systemctl", command])

        with self.assertRaisesRegex(RuntimeError, "previous services could not be restored"):
            activate(
                self.db, self.destination, {"runtime.py": b"new"}, {"runtime.py": b"old"},
                action=action, maintenance_file=self.marker, rollback_directory=self.rollback,
            )
        self.assertEqual(calls, ["stop", "start", "start"])
        self.assertEqual((self.destination / "runtime.py").read_bytes(), b"old")
        self.assertTrue(self.marker.exists())
        self.assertFalse(self.db.in_transaction)

    def test_database_backup_preserves_finished_runs_before_installing(self):
        self.db.execute("INSERT INTO runs VALUES ('completed')")
        backup = self.root / "before-update.sqlite3"

        def check():
            with sqlite3.connect(backup) as saved:
                self.assertEqual(saved.execute("SELECT status FROM runs").fetchall(), [("completed",)])
                self.assertEqual(saved.execute("PRAGMA quick_check").fetchone()[0], "ok")
            self.db.execute("INSERT INTO runs VALUES ('completed')")
            return True

        self.assertTrue(activate(self.db, self.destination, {"runtime.py": b"new"},
                                 {"runtime.py": b"old"}, self.action, check,
                                 database_backup=backup))
        with sqlite3.connect(backup) as saved:
            self.assertEqual(saved.execute("SELECT COUNT(*) FROM runs").fetchone()[0], 1)
        self.assertEqual(self.calls, ["stop", "start"])

    def test_unhealthy_update_restores_old_files(self):
        with self.assertRaisesRegex(RuntimeError, "health check"):
            self.activate(check=lambda: False)
        self.assertEqual(self.calls, ["stop", "start", "stop", "start"])
        self.assertEqual((self.destination / "runtime.py").read_bytes(), b"old")
        self.assertFalse((self.destination / "activity.py").exists())
        self.assertFalse(self.marker.exists())

    def test_health_failure_restores_database_before_reopening_admission(self):
        backup = self.rollback / "server.sqlite3"

        def check():
            self.assertTrue(self.marker.exists())
            self.db.execute("ALTER TABLE runs ADD COLUMN deployment_only TEXT")
            self.db.execute("INSERT INTO runs VALUES ('running', 'new schema')")
            return False

        with self.assertRaisesRegex(RuntimeError, "health check"):
            activate(self.db, self.destination, {"runtime.py": b"new", "activity.py": b"activity"},
                     {"runtime.py": b"old", "activity.py": None}, self.action, check,
                     database_backup=backup, maintenance_file=self.marker,
                     rollback_directory=self.rollback, rollback_check=lambda: True)
        self.assertEqual(self.calls, ["stop", "start", "stop", "start"])
        self.assertEqual(self.db.execute("SELECT * FROM runs").fetchall(), [])
        self.assertEqual([column[1] for column in self.db.execute("PRAGMA table_info(runs)")], ["status"])
        self.assertEqual((self.destination / "runtime.py").read_bytes(), b"old")
        self.assertFalse(self.marker.exists())

    def test_keyboard_interrupt_during_health_restores_files_database_and_admission(self):
        backup = self.rollback / "server.sqlite3"

        def check():
            self.assertTrue(self.marker.exists())
            self.db.execute("ALTER TABLE runs ADD COLUMN deployment_only TEXT")
            raise KeyboardInterrupt()

        with self.assertRaises(KeyboardInterrupt):
            activate(
                self.db, self.destination, {"runtime.py": b"new", "activity.py": b"activity"},
                {"runtime.py": b"old", "activity.py": None}, self.action, check,
                database_backup=backup, maintenance_file=self.marker,
                rollback_directory=self.rollback, rollback_check=lambda: True,
            )
        self.assertEqual(self.calls, ["stop", "start", "stop", "start"])
        self.assertEqual([column[1] for column in self.db.execute("PRAGMA table_info(runs)")], ["status"])
        self.assertEqual((self.destination / "runtime.py").read_bytes(), b"old")
        self.assertFalse((self.destination / "activity.py").exists())
        self.assertFalse(self.marker.exists())

    def test_transient_rollback_start_failure_is_retried_before_admission_reopens(self):
        calls = []

        def action(command):
            calls.append(command)
            if calls == ["stop", "start", "stop", "start"]:
                raise subprocess.CalledProcessError(1, ["systemctl", command])

        with self.assertRaisesRegex(RuntimeError, "health check"):
            activate(self.db, self.destination, {"runtime.py": b"new"}, {"runtime.py": b"old"},
                     action=action, check=lambda: False, maintenance_file=self.marker,
                     rollback_directory=self.rollback, rollback_check=lambda: True)
        self.assertEqual(calls, ["stop", "start", "stop", "start", "start"])
        self.assertEqual((self.destination / "runtime.py").read_bytes(), b"old")
        self.assertFalse(self.marker.exists())

    def test_failed_rollback_stop_recovers_services_but_keeps_admission_closed(self):
        calls = []

        def action(command):
            calls.append(command)
            if command == "stop" and len(calls) > 1:
                raise subprocess.CalledProcessError(1, ["systemctl", command])

        with self.assertRaisesRegex(RuntimeError, "rollback needs attention \\(stop\\)"):
            activate(self.db, self.destination, {"runtime.py": b"new"}, {"runtime.py": b"old"},
                     action=action, check=lambda: False, maintenance_file=self.marker,
                     rollback_directory=self.rollback, rollback_check=lambda: True)
        self.assertEqual(calls, ["stop", "start", "stop", "stop", "start"])
        self.assertEqual((self.destination / "runtime.py").read_bytes(), b"new")
        self.assertTrue(self.marker.exists())

    def prepare_recovery_marker(self, phase, with_database=False, with_web=False):
        previous = {name: None for name in deploy_lan_backend.FILES}
        previous["runtime.py"] = b"old"
        entries = deploy_lan_backend._prepare_file_backup(self.rollback, previous)
        web = None
        if with_web:
            web_destination = self.destination / "web"
            (web_destination / "fonts").mkdir(parents=True)
            (web_destination / "app.js").write_bytes(b"old app")
            (web_destination / "fonts/font.woff2").write_bytes(b"old font")
            web = deploy_lan_backend._prepare_tree_backup(web_destination, self.rollback / "web")
        database_backup = None
        digest = None
        size = None
        if with_database:
            database_backup = self.rollback / "server.sqlite3"
            digest, size = deploy_lan_backend._backup_database(self.db, database_backup)
        metadata = deploy_lan_backend._maintenance_metadata(
            self.destination, self.rollback, entries, self.database, phase,
            database_backup=database_backup, database_backup_sha256=digest,
            database_backup_size=size, web=web,
        )
        deploy_lan_backend._write_maintenance(self.marker, metadata)
        return web

    def test_prepared_phase_recovery_restores_complete_files_and_web_before_reopening(self):
        self.prepare_recovery_marker(deploy_lan_backend.MARKER_PHASE_PREPARED, with_web=True)
        metadata = json.loads(self.marker.read_text())
        self.assertIsNone(metadata["database_backup"])
        (self.destination / "runtime.py").write_bytes(b"mixed release")
        (self.destination / "activity.py").write_bytes(b"unexpected")
        web = self.destination / "web"
        (web / "app.js").write_bytes(b"mixed app")
        (web / "extra.js").write_bytes(b"unexpected")
        calls = []

        def check():
            self.assertTrue(self.marker.exists())
            calls.append("healthy")
            self.assertEqual((self.destination / "runtime.py").read_bytes(), b"old")
            self.assertFalse((self.destination / "activity.py").exists())
            self.assertEqual((web / "app.js").read_bytes(), b"old app")
            self.assertEqual((web / "fonts/font.woff2").read_bytes(), b"old font")
            self.assertFalse((web / "extra.js").exists())
            return True

        self.assertTrue(deploy_lan_backend.recover_interrupted_activation(
            action=calls.append, check=check, maintenance_file=self.marker,
            recovery_root=self.installation,
        ))
        self.assertEqual(calls, ["stop", "start", "healthy"])
        self.assertFalse(self.marker.exists())

    def test_rollback_ready_crash_before_web_publish_restores_backend_database_and_keeps_old_web(self):
        self.db.execute("INSERT INTO runs VALUES ('completed')")
        self.prepare_recovery_marker(
            deploy_lan_backend.MARKER_PHASE_ROLLBACK_READY,
            with_database=True, with_web=True,
        )
        (self.destination / "runtime.py").write_bytes(b"new runtime")
        self.db.execute("ALTER TABLE runs ADD COLUMN new_release TEXT")
        web = self.destination / "web"
        self.assertTrue(deploy_lan_backend.recover_interrupted_activation(
            action=self.calls.append, check=lambda: True, maintenance_file=self.marker,
            recovery_root=self.installation,
        ))
        self.assertEqual((self.destination / "runtime.py").read_bytes(), b"old")
        self.assertEqual((web / "app.js").read_bytes(), b"old app")
        with sqlite3.connect(self.database) as restored:
            self.assertEqual(restored.execute("SELECT * FROM runs").fetchall(), [("completed",)])
        self.assertFalse(self.marker.exists())

    def test_replacement_removes_equal_size_stale_bytecode_before_service_start(self):
        cache = self.destination / "__pycache__"
        cache.mkdir()
        stale = cache / "runtime.cpython-310.pyc"
        stale.write_bytes(b"compiled-new-release")
        os.utime(stale, (1_700_000_000, 1_700_000_000))
        deploy_lan_backend.replace_files(self.destination, {"runtime.py": b"old"})
        self.assertFalse(cache.exists())

    def test_mixed_release_crash_restores_backend_database_and_web_from_full_manifest(self):
        self.db.execute("INSERT INTO runs VALUES ('completed')")
        self.prepare_recovery_marker(
            deploy_lan_backend.MARKER_PHASE_ROLLBACK_READY,
            with_database=True, with_web=True,
        )
        (self.destination / "runtime.py").write_bytes(b"new runtime")
        (self.destination / "activity.py").write_bytes(b"half installed")
        self.db.execute("ALTER TABLE runs ADD COLUMN mixed_release TEXT")
        self.db.execute("INSERT INTO runs VALUES ('running', 'new')")
        web = self.destination / "web"
        (web / "app.js").write_bytes(b"new app")
        (web / "partial.js").write_bytes(b"partial")
        calls = []

        def check():
            calls.append("healthy")
            self.assertEqual((self.destination / "runtime.py").read_bytes(), b"old")
            self.assertFalse((self.destination / "activity.py").exists())
            self.assertEqual((web / "app.js").read_bytes(), b"old app")
            self.assertFalse((web / "partial.js").exists())
            with sqlite3.connect(self.database) as restored:
                self.assertEqual(restored.execute("SELECT * FROM runs").fetchall(), [("completed",)])
                self.assertEqual(
                    [column[1] for column in restored.execute("PRAGMA table_info(runs)")],
                    ["status"],
                )
            return True

        self.assertTrue(deploy_lan_backend.recover_interrupted_activation(
            action=calls.append, check=check, maintenance_file=self.marker,
            recovery_root=self.installation,
        ))
        self.assertEqual(calls, ["stop", "start", "healthy"])
        self.assertFalse(self.marker.exists())

    def test_interrupted_activation_keeps_gate_closed_when_health_does_not_recover(self):
        self.prepare_recovery_marker(deploy_lan_backend.MARKER_PHASE_PREPARED)
        calls = []
        with self.assertRaisesRegex(RuntimeError, "health verification failed"):
            deploy_lan_backend.recover_interrupted_activation(
                action=calls.append, check=lambda: False, maintenance_file=self.marker,
                recovery_root=self.installation,
            )
        self.assertEqual(calls, ["stop", "start"])
        self.assertTrue(self.marker.exists())

    def test_corrupt_or_legacy_marker_never_stops_services_or_reopens_admission(self):
        for content in (b"verifying\n", b'{"format":1,"destination":"../../escape"}'):
            with self.subTest(content=content):
                self.marker.write_bytes(content)
                calls = []
                with self.assertRaises(RuntimeError):
                    deploy_lan_backend.recover_interrupted_activation(
                        action=calls.append, check=lambda: True, maintenance_file=self.marker,
                        recovery_root=self.installation,
                    )
                self.assertEqual(calls, [])
                self.assertTrue(self.marker.exists())

    def test_wrong_release_helper_cannot_recover_a_bound_marker(self):
        self.prepare_recovery_marker(deploy_lan_backend.MARKER_PHASE_PREPARED)
        recovery = self.installation / "recovery"
        recovery.mkdir()
        helpers = []
        for content in (b"value = 'first'\n", b"value = 'second'\n"):
            digest = deploy_lan_backend._sha256_bytes(content)
            helper = recovery / f"deploy_lan_backend_recovery-{digest}.py"
            helper.write_bytes(content)
            helpers.append(helper)
        metadata = json.loads(self.marker.read_text())
        metadata["recovery_helper"] = str(helpers[0])
        metadata["recovery_helper_sha256"] = deploy_lan_backend._sha256_file(helpers[0])
        deploy_lan_backend._write_maintenance(self.marker, metadata)
        calls = []
        with self.assertRaisesRegex(RuntimeError, "does not match the interrupted"):
            deploy_lan_backend.recover_interrupted_activation(
                action=calls.append, check=lambda: True, maintenance_file=self.marker,
                recovery_root=self.installation, expected_helper=helpers[1],
            )
        self.assertEqual(calls, [])
        self.assertTrue(self.marker.exists())

    def test_recovery_is_a_noop_without_a_stale_marker(self):
        calls = []
        self.assertFalse(deploy_lan_backend.recover_interrupted_activation(
            action=calls.append, check=lambda: True, maintenance_file=self.marker,
            recovery_root=self.installation,
        ))
        self.assertEqual(calls, [])

    def test_browser_publication_failure_rolls_back_backend_database_and_complete_web_tree(self):
        web = self.destination / "web"
        (web / "fonts").mkdir(parents=True)
        (web / "app.js").write_bytes(b"old app")
        (web / "fonts/font.woff2").write_bytes(b"old font")
        source = self.root / "release-web"
        source.mkdir()
        expected = {"app.js": b"new app", "style.css": b"new style"}

        def fail_publish(_source, destination):
            (Path(destination) / "app.js").write_bytes(b"partial new app")
            (Path(destination) / "partial.js").write_bytes(b"partial")
            raise OSError("simulated web publication crash")

        with patch.object(deploy_lan_backend, "_deploy_web", side_effect=fail_publish):
            with self.assertRaisesRegex(OSError, "publication crash"):
                activate(
                    self.db, self.destination, {"runtime.py": b"new"}, {"runtime.py": b"old"},
                    action=self.action, check=lambda: True, maintenance_file=self.marker,
                    rollback_directory=self.rollback, web_source=source, web_destination=web,
                    web_expected=expected, rollback_check=lambda: True,
                )
        self.assertEqual(self.calls, ["stop", "start", "stop", "start"])
        self.assertEqual((self.destination / "runtime.py").read_bytes(), b"old")
        self.assertEqual((web / "app.js").read_bytes(), b"old app")
        self.assertEqual((web / "fonts/font.woff2").read_bytes(), b"old font")
        self.assertFalse((web / "partial.js").exists())
        self.assertFalse(self.marker.exists())

    def test_successful_web_release_is_exact_and_marker_survives_final_health_check(self):
        web = self.destination / "web"
        web.mkdir()
        (web / "app.js").write_bytes(b"old app")
        (web / "removed.js").write_bytes(b"removed asset")
        source = self.root / "release-web"
        source.mkdir()
        expected = {"app.js": b"new app", "fonts/current.woff2": b"new font"}
        checks = []

        def publish(_source, destination):
            self.assertTrue(self.marker.exists())
            for name, content in expected.items():
                target = Path(destination) / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)

        def check():
            self.assertTrue(self.marker.exists())
            checks.append(True)
            if len(checks) == 2:
                self.assertFalse((web / "removed.js").exists())
                self.assertEqual((web / "app.js").read_bytes(), b"new app")
            return True

        with patch.object(deploy_lan_backend, "_deploy_web", side_effect=publish):
            self.assertTrue(activate(
                self.db, self.destination, {"runtime.py": b"new"}, {"runtime.py": b"old"},
                action=self.action, check=check, maintenance_file=self.marker,
                rollback_directory=self.rollback, web_source=source, web_destination=web,
                web_expected=expected, rollback_check=lambda: True,
            ))
        self.assertEqual(len(checks), 2)
        self.assertFalse(self.marker.exists())

    def test_database_backup_swap_after_validation_fails_closed(self):
        self.prepare_recovery_marker(
            deploy_lan_backend.MARKER_PHASE_ROLLBACK_READY, with_database=True,
        )
        backup = self.rollback / "server.sqlite3"
        replacement = self.root / "replacement.sqlite3"
        replacement.write_bytes(backup.read_bytes())
        calls = []

        def action(command):
            calls.append(command)
            if command == "stop":
                backup.unlink()
                os.symlink(replacement, backup)

        with self.assertRaisesRegex(RuntimeError, "complete previous release"):
            deploy_lan_backend.recover_interrupted_activation(
                action=action, check=lambda: True, maintenance_file=self.marker,
                recovery_root=self.installation,
            )
        self.assertEqual(calls, ["stop"])
        self.assertTrue(self.marker.exists())

    def test_existing_marker_refuses_a_second_activation_without_side_effects(self):
        self.marker.write_text("stale marker")
        with self.assertRaisesRegex(RuntimeError, "requires recovery"):
            self.activate()
        self.assertEqual(self.calls, [])
        self.assertEqual((self.destination / "runtime.py").read_bytes(), b"old")
        self.assertEqual(self.marker.read_text(), "stale marker")


class BackendDispatcherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.installation = Path(self.temp.name) / "installation"
        self.installation.mkdir()
        self.helper = self.installation / "recovery/deploy_lan_backend_recovery-helper.py"
        self.installation_patch = patch.object(
            deploy_lan_backend, "INSTALLATION", self.installation,
        )
        self.installation_patch.start()
        # CODEX: Keep dispatcher tests from touching the real legacy user unit.
        self.legacy_monitor_patch = patch.object(
            deploy_lan_backend, "_retire_legacy_health_monitor",
            return_value=subprocess.CompletedProcess([], 0),
        )
        self.legacy_monitor_retire = self.legacy_monitor_patch.start()
        # CODEX: Prevent successful activation tests from reloading the real user manager.
        self.restart_policy_patch = patch.object(
            deploy_lan_backend, "_install_restart_policy_dropins",
        )
        self.restart_policy_install = self.restart_policy_patch.start()

    def tearDown(self):
        self.restart_policy_patch.stop()
        self.legacy_monitor_patch.stop()
        self.installation_patch.stop()
        self.temp.cleanup()

    def test_with_web_flag_reaches_detached_child_and_publisher(self):
        with patch.object(deploy_lan_backend, "_queue_activation") as queue:
            deploy_lan_backend.main(["--with-web"])
        queue.assert_called_once_with(with_web=True)
        with patch.object(deploy_lan_backend, "_run_activation") as activation:
            deploy_lan_backend.main([
                "--with-web", deploy_lan_backend.ACTIVATION_FLAG,
                f"{deploy_lan_backend.RECOVERY_HELPER_FLAG}{self.helper}",
            ])
        activation.assert_called_once_with(with_web=True, recovery_helper=self.helper)
        with patch.object(deploy_lan_backend, "_install_recovery_unit", return_value=self.helper), \
                patch.object(deploy_lan_backend.subprocess, "run") as run, redirect_stdout(io.StringIO()):
            deploy_lan_backend._queue_activation(with_web=True)
        self.assertEqual(run.call_args.args[0][-3:], [
            deploy_lan_backend.ACTIVATION_FLAG,
            f"{deploy_lan_backend.RECOVERY_HELPER_FLAG}{self.helper}", "--with-web",
        ])

    def test_unknown_or_duplicate_flags_do_not_queue(self):
        for flags in (["--with-web", "--with-web"], ["--unknown"]):
            with patch.object(deploy_lan_backend, "_queue_activation") as queue, self.assertRaises(SystemExit):
                deploy_lan_backend.main(flags)
            queue.assert_not_called()

    def test_normal_main_queues_without_running_activation_inline(self):
        with patch.object(deploy_lan_backend, "_queue_activation") as queue, \
                patch.object(deploy_lan_backend, "_run_activation") as activate:
            deploy_lan_backend.main([])
        queue.assert_called_once_with()
        activate.assert_not_called()

    def test_queue_uses_a_separate_transient_service_and_returns_after_exec(self):
        output = io.StringIO()
        with patch.object(deploy_lan_backend, "_install_recovery_unit", return_value=self.helper) as install, \
                patch.object(deploy_lan_backend.subprocess, "run") as run, redirect_stdout(output):
            deploy_lan_backend._queue_activation()
        install.assert_called_once_with()
        self.legacy_monitor_retire.assert_not_called()
        run.assert_called_once()
        command = run.call_args.args[0]
        self.assertEqual(command[:3], ["systemd-run", "--user", "--collect"])
        self.assertIn(f"--unit={deploy_lan_backend.UPDATE_SERVICE}", command)
        self.assertIn("--property=Type=exec", command)
        self.assertIn(
            f"--property=TimeoutStopSec={deploy_lan_backend.RECOVERY_TIMEOUT_SECONDS}", command,
        )
        recovery = next(part for part in command if part.startswith("--property=ExecStopPost="))
        self.assertIn(deploy_lan_backend.RECOVERY_FLAG, recovery)
        self.assertIn(str(self.helper), recovery)
        self.assertNotIn(str(Path(deploy_lan_backend.__file__).resolve()), recovery)
        self.assertEqual(command[-4:], [
            deploy_lan_backend.sys.executable,
            str(Path(deploy_lan_backend.__file__).resolve()),
            deploy_lan_backend.ACTIVATION_FLAG,
            f"{deploy_lan_backend.RECOVERY_HELPER_FLAG}{self.helper}",
        ])
        self.assertNotIn("--wait", command)
        self.assertNotIn("--no-block", command)
        self.assertEqual(run.call_args.kwargs, {"check": True, "timeout": 30})
        self.assertIn(deploy_lan_backend.UPDATE_SERVICE, output.getvalue())

    def test_waiting_queue_propagates_updater_completion_before_returning(self):
        output = io.StringIO()
        with patch.object(deploy_lan_backend, "_install_recovery_unit", return_value=self.helper), \
                patch.object(deploy_lan_backend.subprocess, "run") as run, redirect_stdout(output):
            deploy_lan_backend._queue_activation(wait=True)
        command = run.call_args.args[0]
        self.assertIn("--wait", command)
        self.assertEqual(run.call_args.kwargs, {
            "check": True,
            "timeout": (deploy_lan_backend.ACTIVATION_WAIT_SECONDS
                        + deploy_lan_backend.RECOVERY_TIMEOUT_SECONDS + 120),
        })
        self.assertIn("completed successfully", output.getvalue())
        self.assertNotIn("activation queued", output.getvalue())

    def test_recovery_timeout_has_headroom_for_all_bounded_service_and_health_retries(self):
        service_seconds = 4 * 45
        health_seconds = 45 * (1 + 2 + 2 + 1)
        self.assertGreaterEqual(
            deploy_lan_backend.RECOVERY_TIMEOUT_SECONDS,
            service_seconds + health_seconds + 1800,
        )

    def test_child_flag_runs_activation_without_queuing_again(self):
        with patch.object(deploy_lan_backend, "_queue_activation") as queue, \
                patch.object(deploy_lan_backend, "_run_activation") as activate:
            deploy_lan_backend.main([
                deploy_lan_backend.ACTIVATION_FLAG,
                f"{deploy_lan_backend.RECOVERY_HELPER_FLAG}{self.helper}",
            ])
        activate.assert_called_once_with(recovery_helper=self.helper)
        queue.assert_not_called()

    def test_recovery_child_does_not_queue_or_activate_again(self):
        with patch.object(deploy_lan_backend, "_queue_activation") as queue, \
                patch.object(deploy_lan_backend, "_run_activation") as activate, \
                patch.object(deploy_lan_backend, "_run_recovery") as recover:
            deploy_lan_backend.main([deploy_lan_backend.RECOVERY_FLAG])
        recover.assert_called_once_with()
        activate.assert_not_called()
        queue.assert_not_called()

    def test_recovery_flag_cannot_be_combined_with_activation_or_web(self):
        for flags in ([deploy_lan_backend.RECOVERY_FLAG, deploy_lan_backend.ACTIVATION_FLAG],
                      [deploy_lan_backend.RECOVERY_FLAG, deploy_lan_backend.WEB_FLAG]):
            with self.subTest(flags=flags), patch.object(deploy_lan_backend, "_run_recovery") as recover, \
                    self.assertRaises(SystemExit):
                deploy_lan_backend.main(flags)
            recover.assert_not_called()

    def test_recovery_unit_is_durable_enabled_and_installed_before_queue(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            installation = root / "installation"
            python = installation / "venv/bin/python"
            python.parent.mkdir(parents=True)
            os.symlink(sys.executable, python)
            units = root / "units"
            events = []

            def runner(command, **kwargs):
                events.append((command, kwargs))
                if command[2] == "enable":
                    wants = units / "default.target.wants"
                    wants.mkdir()
                    os.symlink(units / deploy_lan_backend.RECOVERY_SERVICE,
                               wants / deploy_lan_backend.RECOVERY_SERVICE)

            helper = deploy_lan_backend._install_recovery_unit(
                script_source=Path(deploy_lan_backend.__file__), installation=installation,
                unit_directory=units, runner=runner,
            )
            unit = units / deploy_lan_backend.RECOVERY_SERVICE
            self.assertEqual(helper.read_bytes(), Path(deploy_lan_backend.__file__).read_bytes())
            self.assertRegex(
                helper.name, r"^deploy_lan_backend_recovery-[0-9a-f]{64}\.py$",
            )
            self.assertEqual(helper.stat().st_mode & 0o777, 0o600)
            content = unit.read_text()
            dispatcher = installation / "recovery/dispatch_recovery_v1.py"
            self.assertTrue(dispatcher.is_file())
            self.assertIn(f"After={deploy_lan_backend.SERVICE} {deploy_lan_backend.WORKER_SERVICE}", content)
            self.assertIn(f"TimeoutStartSec={deploy_lan_backend.RECOVERY_TIMEOUT_SECONDS}", content)
            self.assertIn(f'ExecStart="{python}" "{dispatcher}"', content)
            self.assertNotIn(str(helper), content)
            self.assertIn("WantedBy=default.target", content)
            self.assertEqual(events, [
                (["systemctl", "--user", "daemon-reload"], {"check": True, "timeout": 30}),
                (["systemctl", "--user", "enable", deploy_lan_backend.RECOVERY_SERVICE],
                 {"check": True, "timeout": 30}),
                (["systemctl", "--user", "is-enabled", deploy_lan_backend.RECOVERY_SERVICE],
                 {"check": True, "timeout": 30}),
            ])

    def test_queue_installs_persistent_recovery_before_starting_transient_update(self):
        events = []

        def install():
            events.append("install")
            return self.helper

        def run(_command, **_kwargs):
            events.append("queue")

        with patch.object(deploy_lan_backend, "_install_recovery_unit", side_effect=install), \
                patch.object(deploy_lan_backend.subprocess, "run", side_effect=run), \
                redirect_stdout(io.StringIO()):
            deploy_lan_backend._queue_activation()
        self.assertEqual(events, ["install", "queue"])

    def test_stale_marker_runs_matching_installed_helper_before_any_helper_overwrite(self):
        with tempfile.TemporaryDirectory() as raw:
            installation = Path(raw) / "installation"
            marker = installation / "server/backend-update.maintenance"
            marker.parent.mkdir(parents=True)
            marker.write_text("old release marker")
            with patch.object(deploy_lan_backend, "INSTALLATION", installation), \
                    patch.object(deploy_lan_backend, "_install_recovery_unit") as install, \
                    patch.object(deploy_lan_backend.subprocess, "run") as run:
                with self.assertRaisesRegex(RuntimeError, "matching helper"):
                    deploy_lan_backend._queue_activation()
            install.assert_not_called()
            self.legacy_monitor_retire.assert_not_called()
            run.assert_called_once_with(
                ["systemctl", "--user", "start", "--no-block", deploy_lan_backend.RECOVERY_SERVICE],
                check=True, timeout=30,
            )

    def test_busy_activation_lock_refuses_queue_before_recovery_helper_is_touched(self):
        lock_path = self.installation / "backend-update.lock"
        with deploy_lan_backend._open_activation_lock(lock_path) as lock:
            deploy_lan_backend.fcntl.flock(lock, deploy_lan_backend.fcntl.LOCK_EX)
            with patch.object(deploy_lan_backend, "_install_recovery_unit") as install, \
                    patch.object(deploy_lan_backend.subprocess, "run") as run, \
                    self.assertRaisesRegex(RuntimeError, "already running"):
                deploy_lan_backend._queue_activation()
        install.assert_not_called()
        self.legacy_monitor_retire.assert_not_called()
        run.assert_not_called()

    def test_queue_rejects_intermediate_installation_symlink_without_side_effects(self):
        root = Path(self.temp.name) / "aliased-root"
        external = Path(self.temp.name) / "external"
        external.mkdir()
        root.mkdir()
        os.symlink(external, root / "share")
        installation = root / "share/codex-lan"
        installation.mkdir()
        with patch.object(deploy_lan_backend, "INSTALLATION", installation), \
                patch.object(deploy_lan_backend, "_install_recovery_unit") as install, \
                patch.object(deploy_lan_backend.subprocess, "run") as run, \
                self.assertRaisesRegex(RuntimeError, "symbolic-link path"):
            deploy_lan_backend._queue_activation()
        install.assert_not_called()
        run.assert_not_called()
        self.assertFalse((installation / "backend-update.lock").exists())
        self.assertFalse((installation / "recovery").exists())

    def test_recovery_installer_rejects_intermediate_unit_symlink_before_writing_helper(self):
        python = self.installation / "venv/bin/python"
        python.parent.mkdir(parents=True)
        os.symlink(sys.executable, python)
        root = Path(self.temp.name) / "unit-root"
        external = Path(self.temp.name) / "external-units"
        root.mkdir()
        external.mkdir()
        os.symlink(external, root / "config")
        units = root / "config/systemd/user"
        with patch.object(deploy_lan_backend.subprocess, "run") as run, \
                self.assertRaisesRegex(RuntimeError, "symbolic-link path"):
            deploy_lan_backend._install_recovery_unit(
                Path(deploy_lan_backend.__file__), self.installation, units,
            )
        run.assert_not_called()
        self.assertFalse((self.installation / "recovery").exists())

    def test_recovery_helpers_are_content_addressed_and_never_overwrite_each_other(self):
        python = self.installation / "venv/bin/python"
        python.parent.mkdir(parents=True)
        os.symlink(sys.executable, python)
        units = Path(self.temp.name) / "units"

        def runner(command, **_kwargs):
            if command[2] == "enable":
                wants = units / "default.target.wants"
                wants.mkdir(exist_ok=True)
                link = wants / deploy_lan_backend.RECOVERY_SERVICE
                if not link.exists():
                    os.symlink(units / deploy_lan_backend.RECOVERY_SERVICE, link)

        first_source = Path(self.temp.name) / "first.py"
        second_source = Path(self.temp.name) / "second.py"
        first_source.write_text('value = "first"\n')
        second_source.write_text('value = "second"\n')
        first = deploy_lan_backend._install_recovery_unit(
            first_source, self.installation, units, runner,
        )
        second = deploy_lan_backend._install_recovery_unit(
            second_source, self.installation, units, runner,
        )
        self.assertNotEqual(first, second)
        self.assertEqual(first.read_bytes(), first_source.read_bytes())
        self.assertEqual(second.read_bytes(), second_source.read_bytes())

    def test_login_dispatcher_executes_the_exact_helper_bound_into_the_marker(self):
        python = self.installation / "venv/bin/python"
        python.parent.mkdir(parents=True)
        os.symlink(sys.executable, python)
        units = Path(self.temp.name) / "units"

        def runner(command, **_kwargs):
            if command[2] == "enable":
                wants = units / "default.target.wants"
                wants.mkdir(exist_ok=True)
                os.symlink(units / deploy_lan_backend.RECOVERY_SERVICE,
                           wants / deploy_lan_backend.RECOVERY_SERVICE)

        source = Path(self.temp.name) / "release-helper.py"
        source.write_text(
            'import sys\nfrom pathlib import Path\n'
            'Path(__file__).with_suffix(".ran").write_text(sys.argv[1])\n'
        )
        helper = deploy_lan_backend._install_recovery_unit(
            source, self.installation, units, runner,
        )
        marker = self.installation / "server/backend-update.maintenance"
        marker.parent.mkdir()
        marker.write_text(json.dumps({
            "recovery_helper": str(helper),
            "recovery_helper_sha256": deploy_lan_backend._sha256_file(helper),
        }))
        dispatcher = self.installation / "recovery/dispatch_recovery_v1.py"
        result = subprocess.run(
            [sys.executable, str(dispatcher)], cwd="/", text=True,
            capture_output=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(helper.with_suffix(".ran").read_text(), deploy_lan_backend.RECOVERY_FLAG)

    def test_copied_recovery_helper_runs_without_project_imports_after_login(self):
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw)
            installation = home / ".local/share/codex-lan"
            python = installation / "venv/bin/python"
            python.parent.mkdir(parents=True)
            os.symlink(sys.executable, python)
            units = home / ".config/systemd/user"
            wants = units / "default.target.wants"
            wants.mkdir(parents=True)
            os.symlink(units / deploy_lan_backend.RECOVERY_SERVICE,
                       wants / deploy_lan_backend.RECOVERY_SERVICE)
            helper = deploy_lan_backend._install_recovery_unit(
                script_source=Path(deploy_lan_backend.__file__), installation=installation,
                unit_directory=units, runner=lambda *_args, **_kwargs: None,
            )
            poison = home / "broken-package"
            poison.mkdir()
            (poison / "codex_lan.py").write_text('raise RuntimeError("broken package imported")\n')
            (poison / "deploy_lan_web.py").write_text('raise RuntimeError("broken web package imported")\n')
            environment = dict(os.environ, HOME=str(home), PYTHONPATH=str(poison))
            result = subprocess.run(
                [sys.executable, str(helper), deploy_lan_backend.RECOVERY_FLAG], cwd="/",
                env=environment, text=True, capture_output=True, timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((installation / "backend-update.lock").exists())

    @unittest.skipUnless(shutil.which("systemd-analyze"), "systemd-analyze is unavailable")
    def test_recovery_unit_passes_systemd_249_verification(self):
        version = subprocess.run(
            ["systemd-analyze", "--version"], check=True, text=True, capture_output=True,
        ).stdout.splitlines()[0]
        if not version.startswith("systemd 249 "):
            self.skipTest(f"systemd 249 is required for this compatibility check, found {version}")
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            installation = root / "installation"
            python = installation / "venv/bin/python"
            helper = installation / "recovery/recovery.py"
            python.parent.mkdir(parents=True)
            helper.parent.mkdir(parents=True)
            os.symlink(sys.executable, python)
            helper.write_text("pass\n")
            unit = root / deploy_lan_backend.RECOVERY_SERVICE
            unit.write_text(deploy_lan_backend._recovery_unit(installation, helper))
            dependencies = []
            for name in (deploy_lan_backend.SERVICE, deploy_lan_backend.WORKER_SERVICE):
                dependency = root / name
                dependency.write_text("[Service]\nType=oneshot\nExecStart=/bin/true\n")
                dependencies.append(dependency)
            result = subprocess.run(
                ["systemd-analyze", "--user", "verify", str(unit),
                 *(str(path) for path in dependencies)],
                text=True, capture_output=True,
                env={**os.environ, "XDG_CONFIG_HOME": str(root / "config")},
            )
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_cgroup_guard_rejects_both_managed_services(self):
        for service in (deploy_lan_backend.SERVICE, deploy_lan_backend.WORKER_SERVICE):
            cgroup = f"0::/user.slice/user-1000.slice/app.slice/{service}\n"
            with self.subTest(service=service), patch.object(Path, "read_text", return_value=cgroup):
                with self.assertRaisesRegex(RuntimeError, "refusing to stop the owning cgroup"):
                    deploy_lan_backend._assert_external_activation()

    def test_cgroup_guard_accepts_dedicated_updater_service(self):
        cgroup = f"0::/user.slice/user-1000.slice/app.slice/{deploy_lan_backend.UPDATE_SERVICE}\n"
        with patch.object(Path, "read_text", return_value=cgroup):
            deploy_lan_backend._assert_external_activation()

    def test_activation_checks_cgroup_before_any_update_side_effect(self):
        with patch.object(deploy_lan_backend, "_assert_external_activation", side_effect=RuntimeError("unsafe")) as guard, \
                patch.object(deploy_lan_backend, "_write_maintenance") as marker, \
                patch.object(deploy_lan_backend, "service_action") as action:
            with self.assertRaisesRegex(RuntimeError, "unsafe"):
                deploy_lan_backend._run_activation()
        guard.assert_called_once_with()
        marker.assert_not_called()
        action.assert_not_called()

    def test_activation_rejects_symlinked_installed_package_before_lock_or_service_action(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            installation = root / "installation"
            external = root / "external-package"
            external.mkdir()
            (installation / "code").mkdir(parents=True)
            os.symlink(external, installation / "code/codex_lan")
            server = installation / "server"
            server.mkdir()
            sqlite3.connect(server / "server.sqlite3").close()
            with patch.object(deploy_lan_backend, "INSTALLATION", installation), \
                    patch.object(deploy_lan_backend, "_assert_external_activation"), \
                    patch.object(deploy_lan_backend, "service_action") as action:
                with self.assertRaisesRegex(RuntimeError, "symbolic-link path"):
                    deploy_lan_backend._run_activation()
            action.assert_not_called()
            self.assertFalse((installation / "backend-update.lock").exists())


class CoordinatedReleaseTests(unittest.TestCase):
    def test_web_manifest_is_frozen_and_passed_into_the_coordinated_activation(self):
        expected = {"app.js": b"new app", "style.css": b"new style"}
        source = Path("/frozen/web")
        destination = Path("/installed/web")
        with patch.object(deploy_lan_backend, "_read_web_assets", return_value=expected) as read, \
                patch.object(deploy_lan_backend, "activate", return_value=True) as activation:
            self.assertTrue(deploy_lan_backend.activate_release(
                sentinel.db, sentinel.backend, sentinel.contents, sentinel.previous,
                web_source=source, web_destination=destination, action=sentinel.action,
            ))
        read.assert_called_once_with(source)
        activation.assert_called_once_with(
            sentinel.db, sentinel.backend, sentinel.contents, sentinel.previous,
            web_source=source, web_destination=destination, web_expected=expected,
            action=sentinel.action,
        )

    def test_busy_or_failed_backend_does_not_escape_the_coordinated_activation(self):
        for outcome in (False, RuntimeError("health failed")):
            with self.subTest(outcome=outcome), \
                    patch.object(deploy_lan_backend, "_read_web_assets", return_value={"app.js": b"new"}), \
                    patch.object(
                        deploy_lan_backend, "activate",
                        side_effect=outcome if isinstance(outcome, Exception) else None,
                        return_value=False,
                    ):
                if isinstance(outcome, Exception):
                    with self.assertRaisesRegex(RuntimeError, "health failed"):
                        deploy_lan_backend.activate_release(
                            None, None, {}, {}, web_source=Path("/web"), web_destination=Path("/dest"),
                        )
                else:
                    self.assertFalse(deploy_lan_backend.activate_release(
                        None, None, {}, {}, web_source=Path("/web"), web_destination=Path("/dest"),
                    ))

    def test_default_backend_release_does_not_touch_web(self):
        with patch.object(deploy_lan_backend, "activate", return_value=True), \
                patch.object(deploy_lan_backend, "_read_web_assets") as read:
            self.assertTrue(deploy_lan_backend.activate_release(None, None, {}, {}))
        read.assert_not_called()


class BackendHealthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        server = self.root / "server"
        server.mkdir(mode=0o700)
        self.token = "fake-deployment-admin-token"
        (server / "admin.token").write_text(self.token + "\n")
        (server / "admin.token").chmod(0o600)
        self.status = {"client_access": True, "steering": True, "usage_dashboard": True,
                       "centralized_execution": True, "backend_update": "verifying",
                       "version": __version__, "public_origin": None}
        self.worker = {"worker_online": True, "client_version": __version__}

    def tearDown(self):
        self.temp.cleanup()

    def check(self, status=None, workers=None, https_error=False, secure_status=None):
        status = self.status if status is None else status
        remaining = list(workers or [self.worker])
        calls = []

        def open_response(request, **options):
            url = request.full_url if isinstance(request, Request) else request
            calls.append(url)
            if url == "http://127.0.0.1:8765/api/me":
                self.assertIsInstance(request, Request)
                self.assertEqual(request.get_header("Authorization"), "Bearer " + self.token)
                value = remaining.pop(0) if len(remaining) > 1 else remaining[0]
            else:
                if isinstance(request, Request):
                    self.assertIsNone(request.get_header("Authorization"))
                self.assertTrue(url.endswith("/api/health"), url)
                if url.startswith("https://"):
                    self.assertIs(options["context"], sentinel.verified_context)
                    if https_error:
                        raise OSError("Temporary TLS endpoint unavailable")
                value = secure_status if url.startswith("https://") and secure_status is not None else status
            return io.StringIO(json.dumps(value))

        output = io.StringIO()
        with patch.object(deploy_lan_backend, "INSTALLATION", self.root), \
                patch.object(deploy_lan_backend, "urlopen", side_effect=open_response), \
                patch.object(deploy_lan_backend.time, "sleep") as sleep, \
                patch.object(deploy_lan_backend.ssl, "create_default_context", return_value=sentinel.verified_context) as tls, \
                redirect_stdout(output), redirect_stderr(output):
            result = deploy_lan_backend.healthy()
        self.assertNotIn(self.token, output.getvalue())
        return result, calls, sleep, tls

    def test_matching_server_and_ready_host_worker_are_healthy(self):
        healthy, calls, sleep, tls = self.check()
        self.assertTrue(healthy)
        self.assertEqual(calls, ["http://127.0.0.1:8765/api/health", "http://127.0.0.1:8765/api/me"])
        sleep.assert_not_called()
        tls.assert_not_called()

    def test_offline_or_stale_host_worker_exhausts_bounded_retries(self):
        for worker in ({"worker_online": False, "client_version": __version__},
                       {"worker_online": True, "client_version": "previous-version"},
                       {"worker_online": True}):
            with self.subTest(worker=worker):
                healthy, calls, sleep, _ = self.check(workers=[worker])
                self.assertFalse(healthy)
                self.assertEqual(calls.count("http://127.0.0.1:8765/api/health"), 45)
                self.assertEqual(calls.count("http://127.0.0.1:8765/api/me"), 45)
                self.assertEqual(sleep.call_count, 45)
                self.assertTrue(all(call.args == (1,) for call in sleep.call_args_list))

    def test_wrong_server_version_or_missing_capabilities_are_not_healthy(self):
        for changed in ({"version": "previous-version"}, {"client_access": False}, {"steering": False}, {"usage_dashboard": False}, {"centralized_execution": False}):
            with self.subTest(status=changed):
                healthy, calls, sleep, _ = self.check(status=self.status | changed)
                self.assertFalse(healthy)
                self.assertEqual(calls.count("http://127.0.0.1:8765/api/health"), 45)
                self.assertEqual(sleep.call_count, 45)

    def test_loopback_health_must_confirm_admission_is_still_gated(self):
        for backend_update in ("ready", None):
            with self.subTest(backend_update=backend_update):
                status = dict(self.status)
                if backend_update is None:
                    status.pop("backend_update")
                else:
                    status["backend_update"] = backend_update
                healthy, calls, sleep, _ = self.check(status=status)
                self.assertFalse(healthy)
                self.assertEqual(calls.count("http://127.0.0.1:8765/api/health"), 45)
                self.assertNotIn("http://127.0.0.1:8765/api/me", calls)
                self.assertEqual(sleep.call_count, 45)

    def test_worker_must_become_ready_before_reporting_success(self):
        healthy, calls, sleep, _ = self.check(workers=[
            {"worker_online": False, "client_version": __version__},
            {"worker_online": True, "client_version": "previous-version"},
            self.worker,
        ])
        self.assertTrue(healthy)
        self.assertEqual(calls.count("http://127.0.0.1:8765/api/me"), 3)
        self.assertEqual(sleep.call_count, 2)

    def test_missing_centralized_execution_capability_is_not_healthy(self):
        status = dict(self.status)
        status.pop("centralized_execution")
        healthy, calls, _, _ = self.check(status=status)
        self.assertFalse(healthy)
        self.assertNotIn("http://127.0.0.1:8765/api/me", calls)

    def test_https_endpoint_must_also_report_centralized_execution(self):
        origin = "https://192.168.1.56:8767"
        (self.root / "server/network.json").write_text(json.dumps({"ca": "tls/ca.pem"}))
        status = self.status | {"public_origin": origin}
        secure_status = dict(status)
        secure_status.pop("centralized_execution")
        healthy, calls, sleep, _ = self.check(status=status, secure_status=secure_status)
        self.assertFalse(healthy)
        self.assertEqual(calls.count(origin + "/api/health"), 45)
        self.assertNotIn("http://127.0.0.1:8765/api/me", calls)
        self.assertEqual(sleep.call_count, 45)

    def test_https_health_must_confirm_admission_is_still_gated(self):
        origin = "https://192.168.1.56:8767"
        (self.root / "server/network.json").write_text(json.dumps({"ca": "tls/ca.pem"}))
        status = self.status | {"public_origin": origin}
        for backend_update in ("ready", None):
            with self.subTest(backend_update=backend_update):
                secure_status = dict(status)
                if backend_update is None:
                    secure_status.pop("backend_update")
                else:
                    secure_status["backend_update"] = backend_update
                healthy, calls, sleep, _ = self.check(status=status, secure_status=secure_status)
                self.assertFalse(healthy)
                self.assertEqual(calls.count(origin + "/api/health"), 45)
                self.assertNotIn("http://127.0.0.1:8765/api/me", calls)
                self.assertEqual(sleep.call_count, 45)

    def test_configured_https_is_verified_as_well_as_host_worker(self):
        origin = "https://192.168.1.56:8767"
        (self.root / "server/network.json").write_text(json.dumps({"ca": "tls/ca.pem"}))
        healthy, calls, _, tls = self.check(status=self.status | {"public_origin": origin})
        self.assertTrue(healthy)
        self.assertIn(origin + "/api/health", calls)
        self.assertIn("http://127.0.0.1:8765/api/me", calls)
        tls.assert_called_with(cafile=self.root / "server/tls/ca.pem")
        healthy, calls, sleep, _ = self.check(status=self.status | {"public_origin": origin}, https_error=True)
        self.assertFalse(healthy)
        self.assertEqual(calls.count(origin + "/api/health"), 45)
        self.assertEqual(sleep.call_count, 45)


if __name__ == "__main__":
    unittest.main()
