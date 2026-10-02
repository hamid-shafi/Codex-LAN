"""CODEX: Exercise the actual self-extracting shell wrapper with harmless fixtures."""

import io
import json
import os
import stat
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from build_lan_installer import build


class ShellInstallerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="codex-lan-shell-test-")
        self.root = Path(self.temp.name)
        self.payload = self.root / "payload.tar.gz"
        self.output = self.root / "Installer with spaces.sh"
        self.called = self.root / "called.json"
        code = (
            "import pathlib,json,sys\n"
            f"pathlib.Path({str(self.called)!r}).write_text(json.dumps(sys.argv[1:]))\n"
            "print('TEST-SETUP-CALLED')\n"
        ).encode()
        with tarfile.open(self.payload, "w:gz") as archive:
            for name, value in {
                "setup_lan_client.py": code,
                "codex_lan/worker.py": b"",
                "codex_lan/media.py": b"",
                "codex_lan/model_registry.py": b"",
                "pip.pyz": b"fixture",
                "bin/codex": b"fixture",
            }.items():
                entry = tarfile.TarInfo("Codex-LAN-Client/" + name)
                entry.size = len(value)
                archive.addfile(entry, io.BytesIO(value))

    def tearDown(self):
        self.temp.cleanup()

    def run_installer(self, *arguments):
        return subprocess.run(["bash", str(self.output), *arguments], capture_output=True, text=True, timeout=15)

    def test_help_and_verification_never_install(self):
        build(self.payload, self.output)
        self.assertEqual(stat.S_IMODE(self.output.stat().st_mode), 0o755)
        result = self.run_installer("--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("SERVER password", result.stdout)
        result = self.run_installer("--verify")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("verified successfully", result.stdout)
        self.assertFalse(self.called.exists())

    def test_damage_is_detected_before_extraction_or_setup(self):
        build(self.payload, self.output)
        with self.output.open("ab") as output:
            output.write(b"corrupt")
        result = self.run_installer("--verify")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("damaged", result.stderr)
        self.assertFalse(self.called.exists())

    def test_unknown_and_incomplete_flags_fail_before_install(self):
        build(self.payload, self.output)
        for args in (("--delete-data",), ("--server",), ("--port", "--no-launch")):
            result = self.run_installer(*args)
            self.assertEqual(result.returncode, 2)
        self.assertFalse(self.called.exists())

    def test_existing_installer_is_never_overwritten(self):
        self.output.write_text("user file")
        with self.assertRaises(ValueError):
            build(self.payload, self.output)
        self.assertEqual(self.output.read_text(), "user file")

    def test_unsafe_archive_is_rejected(self):
        for name, kind in (("../escape", tarfile.REGTYPE), ("Codex-LAN-Client/escape", tarfile.SYMTYPE)):
            with tarfile.open(self.payload, "w:gz") as archive:
                for required in ("setup_lan_client.py", "codex_lan/worker.py", "codex_lan/media.py",
                                 "codex_lan/model_registry.py",
                                 "pip.pyz", "bin/codex"):
                    archive.addfile(tarfile.TarInfo("Codex-LAN-Client/" + required), io.BytesIO())
                entry = tarfile.TarInfo(name)
                entry.type = kind
                if kind == tarfile.SYMTYPE:
                    entry.linkname = "/etc/passwd"
                archive.addfile(entry)
            with self.assertRaises(ValueError):
                build(self.payload, self.output)
            self.assertFalse(self.output.exists())

    @unittest.skipIf(os.geteuid() == 0 or sys.version_info[:2] != (3, 10), "Requires supported desktop user")
    def test_wrapper_hands_arguments_to_setup_and_cleans_temporary_files(self):
        build(self.payload, self.output)
        before = set(Path("/tmp").glob("codex-lan-install.*"))
        result = self.run_installer("--server", "somebody@192.0.2.56", "--port", "8777", "--no-launch")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(self.called.read_text()), ["--server", "somebody@192.0.2.56", "--port", "8777", "--no-launch"])
        self.assertEqual(set(Path("/tmp").glob("codex-lan-install.*")), before)


if __name__ == "__main__":
    unittest.main()
