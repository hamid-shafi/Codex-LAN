"""Exercise organized source commands without starting an app or touching an installation."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from project_layout import configure_imports, source_file, source_root

configure_imports(__file__)

from development import project


class ProductSourceTests(unittest.TestCase):
    def test_stable_distribution_names_resolve_inside_their_products(self):
        cases = {
            "codex_lan/server.py": "apps/workspace/codex_lan/server.py",
            "requirements-lan.txt": "apps/workspace/requirements.txt",
            "app.py": "apps/desktop/app.py",
            "assets/codex.svg": "apps/desktop/assets/codex.svg",
            "install_lan_server.py": "setup/host/install_lan_server.py",
            "setup_lan_direct.py": "setup/connector/setup_lan_direct.py",
            "build_lan_bundle.py": "setup/release-tools/build_lan_bundle.py",
            "backup_lan_history.py": "setup/maintenance/backup_lan_history.py",
        }
        for stable_name, source_name in cases.items():
            with self.subTest(name=stable_name):
                resolved = source_file(ROOT, stable_name)
                self.assertEqual(resolved, ROOT / source_name)
                self.assertTrue(resolved.is_file())
                self.assertEqual(source_root(resolved), ROOT)

    def test_existing_flat_payloads_keep_their_own_source_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            flat = Path(directory)
            (flat / "codex_lan").mkdir()
            (flat / "codex_lan/__init__.py").write_text("# Synthetic flat release.\n")
            installer = flat / "setup_lan_direct.py"
            installer.write_text("# Synthetic flat installer.\n")
            self.assertEqual(source_root(installer), flat)
            self.assertEqual(source_file(flat, "setup_lan_direct.py"), installer)
            self.assertEqual(source_file(flat, "codex_lan/__init__.py"), flat / "codex_lan/__init__.py")

    def test_source_names_cannot_escape_the_selected_payload(self):
        for name in ("../private-state/auth.json", "codex_lan/../../auth.json", "/etc/passwd"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                source_file(ROOT, name)


class ProjectCommandTests(unittest.TestCase):
    def run_project(self, *arguments, cwd):
        environment = {**os.environ, "PYTHONPATH": ""}
        return subprocess.run(
            [sys.executable, str(ROOT / "development/project.py"), *arguments],
            cwd=cwd, env=environment, capture_output=True, text=True, timeout=30,
        )

    def test_workflow_help_resolves_from_an_unrelated_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            for command in ("check", "host", "https", "connector", "release", "backend", "web", "backup"):
                with self.subTest(command=command):
                    result = self.run_project(command, "--help", cwd=directory)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn("usage:", result.stdout.lower())

    def test_desktop_check_validates_syntax_without_starting_the_app(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self.run_project("desktop", "--check", cwd=directory)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Desktop source syntax passed", result.stdout)
            self.assertIn("no application or services were started", result.stdout)

    def test_named_tests_import_relocated_tools_from_an_unrelated_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self.run_project("test", "test_publication_tools", "-q", cwd=directory)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("OK", result.stderr)

    def test_option_only_tests_use_default_discovery_and_checkout_directory(self):
        with mock.patch.object(project.subprocess, "call", return_value=0) as runner:
            self.assertEqual(project.main(["test", "-q", "-k", "publication"]), 0)
        command = runner.call_args.args[0]
        self.assertEqual(command, [sys.executable, "-m", "unittest", "discover", "-s",
                                   "development/tests", "-q", "-k", "publication"])
        self.assertEqual(runner.call_args.kwargs["cwd"], ROOT)
        environment = runner.call_args.kwargs["env"]
        self.assertIn(str(ROOT / "apps/workspace"), environment["PYTHONPATH"].split(os.pathsep))
        self.assertIn(str(ROOT / "development/tests"), environment["PYTHONPATH"].split(os.pathsep))

    def test_publication_command_scans_the_selected_source_and_refuses_private_files(self):
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            source = temporary / "source"
            source.mkdir()
            subprocess.run(["git", "-C", str(source), "init", "-q"],
                           check=True, capture_output=True, timeout=10)
            (source / "source.py").write_text("# Reviewed synthetic source.\n")
            result = self.run_project("check", "--repo", str(source), "--json", cwd=temporary)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(json.loads(result.stdout)["safe"])
            (source / "auth.json").write_text('{"fixture": "private installation state"}\n')
            result = self.run_project("check", "--repo", str(source), "--json", cwd=temporary)
            self.assertEqual(result.returncode, 1, result.stderr)
            report = json.loads(result.stdout)
            self.assertFalse(report["safe"])
            self.assertIn("private-runtime-file", {finding["category"] for finding in report["findings"]})
            self.assertNotIn("private installation state", result.stdout)


if __name__ == "__main__":
    unittest.main()
