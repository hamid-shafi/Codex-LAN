"""CODEX: Publishing validates all supported Python versions without installing or using an index."""

import contextlib
import io
import subprocess
import unittest
from pathlib import Path
from unittest import mock

import build_lan_bundle


class OfflineMatrixTests(unittest.TestCase):
    def test_all_supported_interpreters_resolve_offline_and_never_install(self):
        runner = mock.Mock()
        with contextlib.redirect_stdout(io.StringIO()):
            build_lan_bundle.validate_offline_wheels(Path("/fixture source"), Path("/fixture pip.pyz"), runner)
        calls = runner.call_args_list
        self.assertEqual(len(calls), 5)
        self.assertEqual([call.args[0][call.args[0].index("--python-version") + 1] for call in calls],
                         list(build_lan_bundle.SUPPORTED_PYTHONS))
        for call in calls:
            command = call.args[0]
            for required in ("--isolated", "--dry-run", "--ignore-installed", "--no-index", "--only-binary=:all:", "--no-input"):
                self.assertIn(required, command)
            self.assertEqual(command[command.index("--find-links") + 1], "/fixture source/.lan-wheels")
            self.assertNotIn("--break-system-packages", command)

    def test_incomplete_variant_prevents_publication(self):
        runner = mock.Mock(side_effect=subprocess.CalledProcessError(1, ["fixture"]))
        with self.assertRaisesRegex(ValueError, "Python 3.10"):
            build_lan_bundle.validate_offline_wheels(Path("/fixture"), Path("/pip.pyz"), runner)
        self.assertEqual(runner.call_count, 1)


if __name__ == "__main__":
    unittest.main()
