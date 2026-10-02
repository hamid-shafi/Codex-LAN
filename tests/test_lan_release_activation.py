"""CODEX: Frozen release preflight and publication rollback never interrupt backend prompts."""

import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import deploy_lan_centralized as deploy


class CentralizedReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "frozen"
        self.installation = Path(self.temporary.name) / "installed"
        (self.root / "source/codex_lan/web").mkdir(parents=True)
        (self.installation / "server/releases").mkdir(parents=True)
        (self.installation / "code/codex_lan/web").mkdir(parents=True)
        self.old = {}
        for name in deploy.RELEASE_FILES:
            (self.root / name).write_bytes(b"new-" + name.encode())
            target = self.installation / "server/releases" / name
            self.old[target] = b"old-" + name.encode()
        for name in deploy.WEB_FILES:
            (self.root / "source/codex_lan/web" / name).write_bytes(b"new-" + name.encode())
            target = self.installation / "code/codex_lan/web" / name
            self.old[target] = b"old-" + name.encode()
        for target, content in self.old.items():
            target.write_bytes(content)
        payload = (self.root / "client.tar.gz").read_bytes()
        (self.root / "release.json").write_text(json.dumps({"version": deploy.__version__,
            "sha256": hashlib.sha256(payload).hexdigest(), "size": len(payload)}))
        self.runner = patch.object(deploy.subprocess, "run").start()
        self.health = patch.object(
            deploy, "wait_for_installed_backend",
            return_value={"ok": True, "backend_update": "ready", "version": deploy.__version__},
        ).start()
        self.addCleanup(patch.stopall)

    def tearDown(self):
        self.temporary.cleanup()

    def assert_old_files(self):
        for target, content in self.old.items():
            self.assertEqual(target.read_bytes(), content)

    def test_publication_follows_healthy_backend_and_keeps_rollback(self):
        order = []

        def activate():
            self.assert_old_files()
            self.assertEqual(len(list((self.installation / "updates").glob("before-centralized-*"))), 1)
            order.append("backend")

        def health():
            self.assert_old_files()
            self.assertEqual(order, ["backend"])
            order.append("health")
            return {"ok": True, "backend_update": "ready", "version": deploy.__version__}

        original_publish = deploy.publish_prepared

        def ordered_publish(prepared, targets):
            self.assertEqual(order, ["backend", "health"])
            order.append("publish")
            original_publish(prepared, targets)

        with patch.object(deploy, "activate_backend", side_effect=activate) as backend, \
                patch.object(deploy, "wait_for_installed_backend", side_effect=health) as health_check, \
                patch.object(deploy, "publish_prepared", side_effect=ordered_publish):
            deploy.activate_release(self.root, self.installation)
        backend.assert_called_once_with()
        health_check.assert_called_once_with()
        self.assertEqual(order, ["backend", "health", "publish"])
        self.assertEqual(json.loads((self.installation / "server/releases/release.json").read_text())["version"], deploy.__version__)
        self.assertEqual((self.installation / "code/codex_lan/web/app.js").read_bytes(), b"new-app.js")

    def test_missing_browser_asset_and_bad_checksum_cannot_stop_backend(self):
        (self.root / "source/codex_lan/web/app.js").unlink()
        with patch.object(deploy, "activate_backend") as backend:
            with self.assertRaisesRegex(RuntimeError, "missing or empty"):
                deploy.activate_release(self.root, self.installation)
            backend.assert_not_called()
        self.assert_old_files()
        (self.root / "client.tar.gz").write_bytes(b"damaged")
        with patch.object(deploy, "activate_backend") as backend:
            with self.assertRaisesRegex(RuntimeError, "checksum"):
                deploy.activate_release(self.root, self.installation)
            backend.assert_not_called()

    def test_backend_failure_leaves_downloads_and_browser_unchanged(self):
        with patch.object(deploy, "activate_backend", side_effect=RuntimeError("health failure")):
            with self.assertRaisesRegex(RuntimeError, "health failure"):
                deploy.activate_release(self.root, self.installation)
        self.assert_old_files()

    def test_wrong_installed_health_blocks_connector_and_browser_publication(self):
        with patch.object(deploy, "activate_backend") as backend, \
                patch.object(deploy, "wait_for_installed_backend", side_effect=RuntimeError("wrong installed version")) as health:
            with self.assertRaisesRegex(RuntimeError, "wrong installed version"):
                deploy.activate_release(self.root, self.installation)
        backend.assert_called_once_with()
        health.assert_called_once_with()
        self.assert_old_files()

    def test_health_must_be_ready_and_match_source_version_exactly(self):
        valid = {"ok": True, "backend_update": "ready", "version": deploy.__version__}
        self.assertTrue(deploy.installed_health_matches_source(valid))
        for invalid in (
            {**valid, "version": deploy.__version__ + "-other"},
            {**valid, "backend_update": "verifying"},
            {**valid, "ok": False},
            None,
        ):
            with self.subTest(status=invalid):
                self.assertFalse(deploy.installed_health_matches_source(invalid))

    def test_existing_backend_updater_is_joined_instead_of_duplicated(self):
        with patch.object(deploy, "active_backend_update_invocation", return_value="current-invocation"), \
                patch.object(deploy, "wait_for_existing_backend_update") as wait, \
                patch.object(deploy, "_queue_activation") as queue:
            deploy.activate_backend()
        wait.assert_called_once_with("current-invocation")
        queue.assert_not_called()

    def test_no_existing_backend_updater_uses_waiting_transient_activation(self):
        with patch.object(deploy, "active_backend_update_invocation", return_value=None), \
                patch.object(deploy, "_queue_activation") as queue:
            deploy.activate_backend()
        queue.assert_called_once_with(wait=True)

    def test_queue_race_joins_the_updater_that_won(self):
        with patch.object(deploy, "active_backend_update_invocation", side_effect=[None, "winner"]), \
                patch.object(deploy, "_queue_activation", side_effect=subprocess.CalledProcessError(1, ["systemd-run"])), \
                patch.object(deploy, "wait_for_existing_backend_update") as wait:
            deploy.activate_backend()
        wait.assert_called_once_with("winner")

    def test_partial_publication_restores_all_files_without_restarting_backend(self):
        def fail_after_partial_publish(prepared, targets):
            for relative, target in targets[:4]:
                deploy.os.replace(prepared / relative, target)
            raise OSError("fixture publication interruption")
        with patch.object(deploy, "activate_backend") as backend, \
                patch.object(deploy, "publish_prepared", side_effect=fail_after_partial_publish):
            with self.assertRaisesRegex(RuntimeError, "Backend is active.*restored"):
                deploy.activate_release(self.root, self.installation)
        backend.assert_called_once_with()
        self.assert_old_files()

    def add_browser_assets(self):
        assets = {
            "companions.js": b"new-companions-script",
            "companions.css": b"new-companions-styles",
            "controls.js": b"new-controls-script",
            "controls.css": b"new-controls-styles",
            "Vazir-Variable-v30.1.0.woff2": b"wOF2-vazir-font",
            "Vazir-OFL.txt": b"font-license",
            "Vazir-FONT.md": b"font-provenance",
            "fonts/extra.woff2": b"wOF2-other-font",
            "fonts/OFL.txt": b"other-font-license",
        }
        for name, content in assets.items():
            source = self.root / "source/codex_lan/web" / name
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_bytes(content)
        return assets

    def test_complete_browser_bundle_is_published_and_unrelated_files_are_untouched(self):
        assets = self.add_browser_assets()
        source = self.root / "source/codex_lan/web"
        (source / "private.sqlite3").write_bytes(b"not-a-web-asset")
        destination = self.installation / "code/codex_lan/web"
        (destination / "existing-extra.txt").write_bytes(b"keep-existing")
        with patch.object(deploy, "activate_backend") as backend:
            deploy.activate_release(self.root, self.installation)
        backend.assert_called_once_with()
        for name, content in assets.items():
            self.assertEqual((destination / name).read_bytes(), content)
        self.assertFalse((destination / "private.sqlite3").exists())
        self.assertEqual((destination / "existing-extra.txt").read_bytes(), b"keep-existing")

    def test_extended_browser_bundle_is_rolled_back_with_downloads(self):
        assets = self.add_browser_assets()
        destination = self.installation / "code/codex_lan/web"
        existing = destination / "companions.js"
        existing.write_bytes(b"old-companions")
        self.old[existing] = b"old-companions"
        original_publish = deploy.publish_prepared

        def fail_after_publish(prepared, targets):
            original_publish(prepared, targets)
            raise OSError("fixture interruption after publishing extended assets")

        with patch.object(deploy, "activate_backend") as backend, \
                patch.object(deploy, "publish_prepared", side_effect=fail_after_publish):
            with self.assertRaisesRegex(RuntimeError, "Backend is active.*restored"):
                deploy.activate_release(self.root, self.installation)
        backend.assert_called_once_with()
        self.assert_old_files()
        for name in assets:
            if destination / name not in self.old:
                self.assertFalse((destination / name).exists())

    def test_unsafe_font_source_is_rejected_before_backend_activation(self):
        external = Path(self.temporary.name) / "external.woff2"
        external.write_bytes(b"outside-release")
        (self.root / "source/codex_lan/web/font.woff2").symlink_to(external)
        with patch.object(deploy, "activate_backend") as backend:
            with self.assertRaisesRegex(RuntimeError, "escapes its release directory"):
                deploy.activate_release(self.root, self.installation)
        backend.assert_not_called()
        self.assert_old_files()

    def test_nested_destination_cannot_escape_web_installation(self):
        self.add_browser_assets()
        external = Path(self.temporary.name) / "outside-installation"
        external.mkdir()
        (self.installation / "code/codex_lan/web/fonts").symlink_to(external, target_is_directory=True)
        with patch.object(deploy, "activate_backend") as backend:
            with self.assertRaisesRegex(RuntimeError, "escapes its release directory"):
                deploy.activate_release(self.root, self.installation)
        backend.assert_not_called()
        self.assertEqual(list(external.iterdir()), [])
        self.assert_old_files()


if __name__ == "__main__":
    unittest.main()
