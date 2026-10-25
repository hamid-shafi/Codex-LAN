"""Regression tests for the host's private SSH relay controls."""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_lan import relay_setup


class RelaySetupTests(unittest.TestCase):
    def test_ssh_target_and_fingerprint_reject_option_or_shell_injection(self):
        for value in ("-oProxyCommand=evil", "relay;touch /tmp/x", "relay host", "relay\nHost", "user@relay", "[::1]"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                relay_setup.validate_server(value)
        for value in ("-root", "root;id", "root@server", "root\n", "root user"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                relay_setup.validate_user(value)
        with self.assertRaises(ValueError):
            relay_setup.validate_user("operator", bootstrap=True)
        for value in ("SHA256:abc", "SHA256:" + "a" * 43 + "\n", "MD5:" + "a" * 43):
            with self.subTest(value=value), self.assertRaises(ValueError):
                relay_setup.validate_fingerprint(value)

    def test_host_key_mismatch_never_uses_password_or_writes_pin(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder) / "ssh-relay"
            with patch.object(relay_setup, "probe_host", return_value=("relay.example ssh-ed25519 AAAA\n", "SHA256:" + "A" * 43)), \
                    patch.object(relay_setup, "_password_provision") as password:
                with self.assertRaisesRegex(ValueError, "does not match"):
                    relay_setup._pin_host(directory, "relay.example", "SHA256:" + "B" * 43)
                password.assert_not_called()
                self.assertFalse(directory.exists())

    def test_one_time_password_is_passed_only_on_anonymous_pipe(self):
        captured = []
        secret = "temporary-password-1729"

        def fake_run(args, *, data=None, timeout=None, pass_fds=(), capture=True):
            captured.append((args, data, timeout))
            self.assertFalse(capture)
            self.assertNotIn(secret, repr(args))
            self.assertNotIn(secret, repr(data))
            self.assertEqual(len(pass_fds), 1)
            self.assertEqual(os.read(pass_fds[0], 4096), secret.encode() + b"\n")
            return type("Result", (), {"returncode": 0})()

        with patch.object(relay_setup.shutil, "which", return_value="/bin/sshpass"), \
                patch.object(relay_setup, "_run", side_effect=fake_run):
            relay_setup._password_provision("relay.example", "root", secret,
                                            Path("/tmp/pinned-known-hosts"), "ssh-ed25519 AAAA codex-lan-relay")
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0][0][:2], ["sshpass", "-d"])
        self.assertIn("PubkeyAuthentication=no", captured[0][0])

    def test_invalid_password_unicode_and_controls_never_start_ssh(self):
        with patch.object(relay_setup, "_run") as runner:
            for secret in ("bad\ud800", "bad\rpassword", "bad\x1bpassword"):
                with self.subTest(secret=repr(secret)), self.assertRaisesRegex(ValueError, "administrator password"):
                    relay_setup._password_provision("relay.example", "root", secret,
                                                    Path("/tmp/pinned-known-hosts"), "ssh-ed25519 AAAA")
            runner.assert_not_called()

    def test_command_output_is_bounded_while_process_runs(self):
        with self.assertRaisesRegex(ValueError, "too much data"):
            relay_setup._run([sys.executable, "-c", "import os; os.write(1, b'x' * 1000000)"], timeout=3)

    def test_legacy_service_is_not_replaced_without_explicit_migration(self):
        legacy = ("[Unit]\nDescription=Private SSH rendezvous for Codex-LAN\n[Service]\n"
                  "ExecStart=/usr/bin/ssh -NT -F /dev/null -o BatchMode=yes "
                  "-o StrictHostKeyChecking=yes -R 127.0.0.1:18767:127.0.0.2:8767 "
                  "codex-lan-relay@relay.example\n")
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / relay_setup.UNIT
            path.write_text(legacy)
            with patch.object(relay_setup, "_unit_path", return_value=path), \
                    patch.object(relay_setup, "_systemctl") as ctl:
                self.assertTrue(relay_setup._unit_info()["manageable"])
                self.assertFalse(relay_setup._unit_info()["managed"])
                with self.assertRaisesRegex(ValueError, "Confirm migration"):
                    relay_setup._replace_unit("replacement", verify=lambda: None)
                self.assertEqual(path.read_text(), legacy)
                ctl.assert_not_called()

    def test_failed_migration_restores_active_legacy_service(self):
        legacy = ("[Service]\nExecStart=/usr/bin/ssh -NT -o BatchMode=yes "
                  "-o StrictHostKeyChecking=yes -R 127.0.0.1:18767:127.0.0.2:8767 "
                  "codex-lan-relay@relay.example\n")
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / relay_setup.UNIT
            path.write_text(legacy)
            commands = []
            with patch.object(relay_setup, "_unit_path", return_value=path), \
                    patch.object(relay_setup, "_service_state", return_value=(True, True)), \
                    patch.object(relay_setup, "_systemctl", side_effect=lambda *args: commands.append(args)):
                with self.assertRaisesRegex(ValueError, "previous local unit was restored"):
                    relay_setup._replace_unit("replacement", verify=lambda: (_ for _ in ()).throw(ValueError("failed")),
                                              replace_existing=True)
                self.assertEqual(path.read_text(), legacy)
                self.assertEqual(commands, [("stop", relay_setup.UNIT), ("enable", relay_setup.UNIT),
                                            ("start", relay_setup.UNIT)])


if __name__ == "__main__":
    unittest.main()
