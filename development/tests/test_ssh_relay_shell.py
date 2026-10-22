"""Check relay arguments without opening a network connection or reading keys."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[2] / 'setup/relay/connect_ssh_relay.sh'


class RelayShellTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.called = self.directory / 'arguments.json'
        mock = self.directory / 'ssh'
        mock.write_text(f'#!{sys.executable}\nimport json, os, pathlib, sys\n'
                        'pathlib.Path(os.environ["RELAY_TEST_ARGS"]).write_text(json.dumps(sys.argv[1:]))\n'
                        'sys.exit(int(os.environ.get("RELAY_TEST_EXIT", "0")))\n')
        mock.chmod(0o700)
        self.environment = {**os.environ, 'PATH': str(self.directory), 'RELAY_TEST_ARGS': str(self.called)}

    def run_script(self, *arguments):
        return subprocess.run(['/bin/bash', str(SCRIPT), *arguments], env=self.environment,
                              capture_output=True, text=True, timeout=5)

    def test_root_login_and_loopback_only(self):
        result = self.run_script('root@example.test')
        self.assertEqual(result.returncode, 0, result.stderr)
        arguments = json.loads(self.called.read_text())
        self.assertIn('127.0.0.2:8767:127.0.0.1:18767', arguments)
        self.assertIn('NumberOfPasswordPrompts=1', arguments)
        self.assertIn('StrictHostKeyChecking=ask', arguments)
        self.assertEqual(arguments[-1], 'root@example.test')
        self.assertNotIn('0.0.0.0', ' '.join(arguments))

    def test_identity_path_is_one_argument(self):
        identity = self.directory / 'key with spaces'
        identity.write_text('NOT-A-REAL-KEY')
        result = self.run_script('person@example.test', '--identity', str(identity))
        self.assertEqual(result.returncode, 0, result.stderr)
        arguments = json.loads(self.called.read_text())
        self.assertEqual(arguments[arguments.index('-i') + 1], str(identity))
        self.assertIn('IdentitiesOnly=yes', arguments)

    def test_invalid_target_never_calls_ssh(self):
        for target in ('-oProxyCommand=bad', 'root@example.test;bad', 'root@example.test\n', 'root@'):
            with self.subTest(target=target):
                self.assertEqual(self.run_script(target).returncode, 2)
                self.assertFalse(self.called.exists())

    def test_bad_options_never_call_ssh(self):
        for arguments in (('root@example.test', '--identity', '/nonexistent/relay-key'),
                          ('root@example.test', '--identity'), ('root@example.test', '--wrong', 'value')):
            self.assertEqual(self.run_script(*arguments).returncode, 2)
            self.assertFalse(self.called.exists())

    def test_authentication_failure_is_not_swallowed_or_retried(self):
        self.environment['RELAY_TEST_EXIT'] = '255'
        self.assertEqual(self.run_script('root@example.test').returncode, 255)

    def test_help_does_not_connect(self):
        result = self.run_script('--help')
        self.assertEqual(result.returncode, 0)
        self.assertIn('silent', result.stdout)
        self.assertFalse(self.called.exists())


if __name__ == '__main__':
    unittest.main()
