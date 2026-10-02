"""CODEX: Deferred browser activation uses a frozen release, not later workspace edits."""

import os
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import deploy_lan_web


class WebReleaseTests(unittest.TestCase):
    def test_cli_accepts_a_frozen_source(self):
        with patch("sys.argv", ["deploy_lan_web.py", "--source", "/tmp/frozen-release", "--destination", "/tmp/installed-web"]), patch.object(deploy_lan_web, "deploy") as deploy, redirect_stdout(StringIO()):
            deploy_lan_web.main()
        deploy.assert_called_once_with(Path("/tmp/frozen-release"), Path("/tmp/installed-web"))

    def test_publishing_a_snapshot_preserves_the_tested_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, snapshot, installed = [root / name for name in ("source", "snapshot", "installed")]
            for directory in (source, snapshot, installed):
                directory.mkdir()
            for name in ("app.js", "style.css", "index.html"):
                (source / name).write_text("tested release", encoding="utf-8")
            deploy_lan_web.deploy(source, snapshot)
            (source / "app.js").write_text("next unfinished release", encoding="utf-8")
            (installed / "installed-layout-marker").write_text("retain", encoding="utf-8")
            deploy_lan_web.deploy(snapshot, installed)
            self.assertEqual((installed / "app.js").read_text(), "tested release")
            self.assertEqual((installed / "installed-layout-marker").read_text(), "retain")

    def test_failed_directory_activation_restores_the_entire_previous_bundle(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, installed = root / "source", root / "installed"
            source.mkdir()
            installed.mkdir()
            for name in ("app.js", "style.css", "index.html"):
                (source / name).write_text("new-" + name, encoding="utf-8")
                (installed / name).write_text("old-" + name, encoding="utf-8")
            original_replace = os.replace
            attempts = 0

            def fail_when_activating_staged_bundle(source_path, destination_path):
                nonlocal attempts
                attempts += 1
                if attempts == 2:
                    raise OSError("fixture activation interruption")
                return original_replace(source_path, destination_path)

            with patch.object(deploy_lan_web.os, "replace", side_effect=fail_when_activating_staged_bundle):
                with self.assertRaisesRegex(OSError, "fixture activation interruption"):
                    deploy_lan_web.deploy(source, installed)
            for name in ("app.js", "style.css", "index.html"):
                self.assertEqual((installed / name).read_text(encoding="utf-8"), "old-" + name)

    def test_frozen_release_carries_font_binary_license_and_provenance(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, installed = root / "source", root / "installed"
            source.mkdir()
            installed.mkdir()
            (source / "fonts").mkdir()
            assets = {"app.js": b"js", "style.css": b"css", "index.html": b"html",
                      "Vazir-Variable-v30.1.0.woff2": b"wOF2\x00binary", "Vazir-OFL.txt": b"license",
                      "Vazir-FONT.md": b"provenance", "fonts/other.woff2": b"wOF2more",
                      "fonts/OFL.txt": b"nested-license"}
            for name, data in assets.items():
                (source / name).write_bytes(data)
            deploy_lan_web.deploy(source, installed)
            for name, data in assets.items():
                self.assertEqual((installed / name).read_bytes(), data)

    def test_font_symlink_cannot_escape_frozen_release(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, installed = root / "source", root / "installed"
            source.mkdir()
            installed.mkdir()
            for name in ("app.js", "style.css", "index.html"):
                (source / name).write_text("asset")
            (root / "private").write_text("outside")
            (source / "escape.woff2").symlink_to(root / "private")
            with self.assertRaisesRegex(ValueError, "escapes"):
                deploy_lan_web.deploy(source, installed)
            self.assertEqual(list(installed.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
