"""CODEX: Direct installer bootstrap uses local Python and explicit, harmless setup fixtures."""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from project_layout import configure_imports, source_file, source_root

configure_imports(__file__)

from build_lan_installer import build


class DirectShellInstallerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="codex-direct-shell-test-")
        self.root = Path(self.temp.name)
        self.downloads = self.root / "Downloads with spaces"
        self.downloads.mkdir()
        self.bin = self.root / "commands with spaces"
        self.bin.mkdir()
        self.payload = self.root / "fixture.tar.gz"
        self.installer = self.downloads / "Install Codex Connector.sh"
        self.called = self.root / "fixture-setup-call.json"
        self.environment = {**os.environ, "PATH": str(self.bin)}
        for command in ("awk", "tail", "mktemp", "tar", "sha256sum", "rm", "gzip"):
            (self.bin / command).symlink_to(shutil.which(command))
        setup = ("import json, pathlib, sys\n"
                 f"pathlib.Path({str(self.called)!r}).write_text(json.dumps(sys.argv[1:]))\n"
                 "print('HARMLESS-SETUP-FIXTURE')\n").encode()
        with tarfile.open(self.payload, "w:gz") as archive:
            for name, content in {
                "setup_lan_direct.py": setup,
                "setup_lan_client.py": b"",
                "codex_lan/worker.py": b"",
                "codex_lan/media.py": b"",
                "codex_lan/model_registry.py": b"",
                "pip.pyz": b"",
                "bin/codex": b"",
            }.items():
                item = tarfile.TarInfo("Codex-LAN-Client/" + name)
                item.size = len(content)
                archive.addfile(item, io.BytesIO(content))
        template = source_file(source_root(__file__), "install_lan_direct.sh.in")
        build(self.payload, self.installer, template)

    def tearDown(self):
        self.temp.cleanup()

    def python(self, name, version, abi=""):
        executable = str(Path(sys.executable).resolve())
        script = self.bin / name
        script.write_text(
            f"#!{executable}\n"
            "import os, platform, sys\n"
            "if sys.argv[1:3] == ['-I', '-c']:\n"
            f"    sys.version_info = {tuple(version)!r}\n"
            f"    sys.abiflags = {abi!r}\n"
            "    exec(sys.argv[3], {})\n"
            "else:\n"
            f"    os.execv({executable!r}, [{executable!r}, *sys.argv[1:]])\n"
        )
        script.chmod(0o755)
        return script

    def run_installer(self, *arguments):
        return subprocess.run(["/bin/bash", str(self.installer), *arguments],
                              cwd=self.root, env=self.environment, capture_output=True, text=True, timeout=15)

    def test_verify_needs_no_python_and_does_not_call_setup(self):
        before = set(Path("/tmp").glob("codex-direct-install.*"))
        result = self.run_installer("--verify")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Package verified. Nothing installed.", result.stdout)
        self.assertFalse(self.called.exists())
        self.assertEqual(set(Path("/tmp").glob("codex-direct-install.*")), before)
        result = self.run_installer("--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("CPython 3.10–3.14", result.stdout)

    def test_corrupt_payload_fails_without_python_or_setup(self):
        with self.installer.open("ab") as target:
            target.write(b"corrupt")
        result = self.run_installer("--verify")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("checksum failed", result.stderr)
        self.assertFalse(self.called.exists())

    def test_empty_or_missing_pair_file_flag_is_not_auto_discovered(self):
        (self.downloads / "codex-pairing.json").write_text("DO-NOT-AUTO-DISCOVER-FIXTURE")
        for arguments in (("--pair-file",), ("--pair-file", ""), ("--pair-file", "--verify")):
            result = self.run_installer(*arguments)
            self.assertEqual(result.returncode, 2)
            self.assertIn("Missing pairing file", result.stderr)
        self.assertFalse(self.called.exists())

    @unittest.skipIf(os.geteuid() == 0, "Installer must run as a desktop user")
    def test_default_python312_is_preferred_and_nearby_pairing_is_discovered(self):
        preferred = self.python("python3", (3, 12, 8))
        self.python("python3.14", (3, 14, 0))
        pairing = self.downloads / "codex-pairing.json"
        pairing.write_text("HARMLESS-PAIRING-FIXTURE")
        result = self.run_installer()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"Using Python 3.12.8 ({preferred})", result.stdout)
        self.assertIn("Found your pairing file", result.stdout)
        self.assertEqual(json.loads(self.called.read_text()), ["--auto-pair-file", str(pairing)])

    @unittest.skipIf(os.geteuid() == 0, "Installer must run as a desktop user")
    def test_newest_strict_browser_renamed_pairing_file_is_discovered(self):
        self.python("python3", (3, 12, 8))
        original = self.downloads / "codex-pairing.json"
        renamed = self.downloads / "codex-pairing (2).json"
        ignored = self.downloads / "codex-pairing (copy).json"
        original.write_text("OLDER-HARMLESS-PAIRING-FIXTURE")
        renamed.write_text("NEWER-HARMLESS-PAIRING-FIXTURE")
        ignored.write_text("IGNORED-HARMLESS-PAIRING-FIXTURE")
        os.utime(original, (1000, 1000))
        os.utime(ignored, (2000, 2000))
        os.utime(renamed, (3000, 3000))
        result = self.run_installer()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("codex-pairing (2).json", result.stdout)
        self.assertEqual(json.loads(self.called.read_text()), ["--auto-pair-file", str(renamed)])

    @unittest.skipIf(os.geteuid() == 0, "Installer must run as a desktop user")
    def test_tagged_pairing_and_browser_renamed_copy_are_discovered(self):
        self.python("python3", (3, 12, 8))
        for name in ("codex-pairing-0.3.2.json", "codex-pairing-0.3.2 (3).json"):
            pairing = self.downloads / name
            pairing.write_text("HARMLESS-TAGGED-FIXTURE")
            result = self.run_installer()
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(self.called.read_text()), ["--auto-pair-file", str(pairing)])
            pairing.unlink()

    @unittest.skipIf(os.geteuid() == 0, "Installer must run as a desktop user")
    def test_versioned_python312_fallback_and_explicit_pair_path_with_spaces(self):
        self.python("python3", (3, 9, 18))
        selected = self.python("python3.12", (3, 12, 8))
        pairing = self.root / "My own pairing file.json"
        pairing.write_text("HARMLESS-PAIRING-FIXTURE")
        (self.downloads / "codex-pairing.json").write_text("UNSELECTED-FIXTURE")
        result = self.run_installer("--pair-file", str(pairing))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"Using Python 3.12.8 ({selected})", result.stdout)
        self.assertEqual(json.loads(self.called.read_text()), ["--pair-file", str(pairing)])

    @unittest.skipIf(os.geteuid() == 0, "Installer must run as a desktop user")
    def test_missing_pairing_does_not_guess_from_another_directory_or_symlink(self):
        self.python("python3", (3, 12, 8))
        other_pairing = self.root / "codex-pairing.json"
        other_pairing.write_text("DO-NOT-DISCOVER-FIXTURE")
        (self.downloads / "codex-pairing.json").symlink_to(other_pairing)
        result = self.run_installer()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(self.called.read_text()), [])
        self.assertNotIn("Found your pairing file", result.stdout)

    @unittest.skipIf(os.geteuid() == 0, "Installer must run as a desktop user")
    def test_unsupported_python_and_free_threaded_build_do_not_call_setup(self):
        self.python("python3", (3, 9, 18))
        self.python("python3.14", (3, 14, 0), "t")
        result = self.run_installer()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("No compatible Python was found", result.stderr)
        self.assertFalse(self.called.exists())

    @unittest.skipIf(os.geteuid() == 0, "Installer must run as a desktop user")
    def test_bad_explicit_pair_path_fails_before_setup(self):
        self.python("python3", (3, 12, 8))
        result = self.run_installer("--pair-file", str(self.root / "missing.json"))
        self.assertEqual(result.returncode, 2)
        self.assertIn("Pairing file not found", result.stderr)
        self.assertFalse(self.called.exists())


if __name__ == "__main__":
    unittest.main()
