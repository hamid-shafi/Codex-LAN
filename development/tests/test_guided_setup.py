"""Exercise the first-run shell workflow using isolated command shims, never OS installs."""

from __future__ import annotations

import errno
import os
from pathlib import Path
import pty
import select
import shutil
import subprocess
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless(os.name == "posix" and Path("/bin/bash").exists(), "Linux shell workflow")
class GuidedSetupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.checkout = self.root / "checkout with spaces"
        (self.checkout / "setup/bootstrap").mkdir(parents=True)
        (self.checkout / "setup/host").mkdir()
        (self.checkout / "setup/connector").mkdir()
        (self.checkout / "setup/relay").mkdir()
        for name in ("setup.sh", "setup/bootstrap/linux.sh", "setup/relay/connect_ssh_relay.sh"):
            shutil.copy2(ROOT / name, self.checkout / name)
        (self.checkout / "setup/host/setup.sh").write_text(
            '#!/bin/bash\nprintf "host" >> "$SETUP_TEST_LOG"\n'
            'printf " <%s>" "$@" >> "$SETUP_TEST_LOG"\nprintf "\\n" >> "$SETUP_TEST_LOG"\n'
        )
        self.bin = self.root / "bin"
        self.bin.mkdir()
        for name in ("bash", "dirname", "readlink", "timeout", "mktemp", "rm", "rmdir", "sh"):
            (self.bin / name).symlink_to(shutil.which(name))
        self.log = self.root / "commands.log"
        self.home = self.root / "user"
        self.home.mkdir()
        self.scratch = self.root / "scratch"
        self.scratch.mkdir()
        self.environment = {
            **os.environ,
            "PATH": str(self.bin),
            "HOME": str(self.home),
            "TMPDIR": str(self.scratch),
            "SETUP_TEST_LOG": str(self.log),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        # The outer test runner may itself use a private Codex home.
        self.environment.pop("CODEX_HOME", None)
        self.shim("id", 'printf "1000\\n"')
        self.shim("uname", 'printf "Linux\\n"')
        self.shim("python3", "exit 0")
        self.shim("systemctl", 'printf "PRIVATE_ENVIRONMENT=must-not-be-displayed\\n"')
        for name in ("openssl", "curl", "ssh", "sudo", "apt-get"):
            self.shim(name, f'printf "{name}" >> "$SETUP_TEST_LOG"; '
                      'printf " <%s>" "$@" >> "$SETUP_TEST_LOG"; '
                      'printf "\\n" >> "$SETUP_TEST_LOG"')

    def shim(self, name, body):
        path = self.bin / name
        path.write_text("#!/bin/bash\n" + body + "\n")
        path.chmod(0o700)
        return path

    def command_log(self):
        return self.log.read_text() if self.log.exists() else ""

    def run_setup(self, *arguments):
        return subprocess.run(
            ["/bin/bash", str(self.checkout / "setup.sh"), *arguments],
            env=self.environment, capture_output=True, text=True, timeout=5,
        )

    def run_function(self, function, *, overrides="", answers=None):
        script = 'source "$1"; ' + overrides + function
        command = ["/bin/bash", "-c", script, "test", str(self.checkout / "setup/bootstrap/linux.sh")]
        if answers is None:
            return subprocess.run(command, env=self.environment, capture_output=True, text=True, timeout=5)
        return self.run_terminal(command, answers)

    def run_terminal(self, command, answers):
        master, slave = pty.openpty()
        process = subprocess.Popen(command, env=self.environment, stdin=slave, stdout=slave, stderr=slave)
        os.close(slave)
        output = bytearray()
        try:
            os.write(master, ("\n".join(answers) + "\n").encode())
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if select.select([master], [], [], 0.05)[0]:
                    try:
                        chunk = os.read(master, 65536)
                    except OSError as error:
                        if error.errno != errno.EIO:
                            raise
                        break
                    if not chunk:
                        break
                    output.extend(chunk)
                elif process.poll() is not None:
                    break
            process.wait(timeout=max(0.1, deadline - time.monotonic()))
            return subprocess.CompletedProcess(command, process.returncode, output.decode(), "")
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=2)
            os.close(master)

    def fake_download(self, *, valid=True):
        installer = 'printf "installer-ran\\n" >> "$SETUP_TEST_LOG"' if valid else "if then"
        self.shim("curl", 'printf "curl\\n" >> "$SETUP_TEST_LOG"\n'
                  'while (($#)); do\n'
                  '  if [[ "$1" == --output ]]; then shift; target="$1"; fi\n'
                  '  shift\n'
                  'done\n'
                  f"printf '%s\\n' '{installer}' > \"$target\"")

    def test_shell_syntax(self):
        result = subprocess.run(["/bin/bash", "-n", str(ROOT / "setup/bootstrap/linux.sh")], capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_no_arguments_without_terminal_only_displays_menu_help(self):
        result = self.run_setup()
        self.assertEqual(result.returncode, 0)
        self.assertIn("Nothing was changed", result.stdout)
        self.assertEqual(self.command_log(), "")

    def test_help_and_team_do_not_require_python_or_run_commands(self):
        (self.bin / "python3").unlink()
        for arguments in (("--help",), ("team",)):
            result = self.run_setup(*arguments)
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("no separate Codex installation", result.stdout)
        self.assertEqual(self.command_log(), "")

    def test_check_and_legacy_flags_delegate_without_altering_arguments(self):
        for arguments in (("check",), ("--check",), ("host", "--check")):
            result = self.run_setup(*arguments)
            self.assertEqual(result.returncode, 0, result.stderr)
        result = self.run_setup("--check", "--codex", "/tmp/selected codex", "--https-port", "9000")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.command_log().count("host <--check>\n"), 3)
        self.assertIn("host <--check> <--codex> </tmp/selected codex> <--https-port> <9000>", self.command_log())
        self.assertNotIn("PRIVATE_ENVIRONMENT", result.stdout + result.stderr)

    def test_host_help_does_not_check_or_install_prerequisites(self):
        (self.bin / "python3").unlink()
        result = self.run_setup("host", "--help")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self.command_log(), "host <--help>\n")

    def test_missing_python_check_is_nonmutating(self):
        (self.bin / "python3").unlink()
        result = self.run_setup("prerequisites", "--check")
        self.assertEqual(result.returncode, 1)
        self.assertIn("[NEEDED] Python 3.10", result.stdout)
        self.assertEqual(self.command_log(), "")

    def test_missing_venv_and_user_session_are_actionable_without_environment_leak(self):
        self.shim("python3", '[[ "$*" != *"import venv"* ]]')
        self.shim("systemctl", 'printf "PRIVATE_ENVIRONMENT=must-not-be-displayed\\n"; exit 1')
        result = self.run_setup("prerequisites", "--check")
        self.assertEqual(result.returncode, 1)
        self.assertIn("python3-venv", result.stdout)
        self.assertIn("Installing packages alone does not activate", result.stdout)
        self.assertNotIn("PRIVATE_ENVIRONMENT", result.stdout + result.stderr)
        self.assertEqual(self.command_log(), "")

    def test_missing_requirements_prevent_host_install(self):
        (self.bin / "python3").unlink()
        result = self.run_setup("host")
        self.assertEqual(result.returncode, 1)
        self.assertIn("setup.sh prerequisites", result.stderr)
        self.assertEqual(self.command_log(), "")

    def test_unsupported_linux_distribution_has_manual_help_only(self):
        (self.bin / "python3").unlink()
        result = self.run_function("prerequisites", overrides="supported_apt_distribution() { return 1; }; ")
        self.assertEqual(result.returncode, 1)
        self.assertIn("software manager", result.stdout)
        self.assertEqual(self.command_log(), "")

    def test_package_install_requires_terminal_and_explicit_confirmation(self):
        (self.bin / "python3").unlink()
        overrides = "supported_apt_distribution() { return 0; }; "
        result = self.run_function("prerequisites", overrides=overrides)
        self.assertEqual(result.returncode, 1)
        self.assertIn("interactive terminal", result.stderr)
        result = self.run_function("prerequisites", overrides=overrides, answers=["no"])
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.command_log(), "")

    def test_confirmed_package_install_uses_only_fixed_apt_arguments(self):
        (self.bin / "python3").unlink()
        result = self.run_function("prerequisites", overrides="supported_apt_distribution() { return 0; }; ", answers=["INSTALL"])
        self.assertEqual(result.returncode, 1)  # Shims do not actually install missing Python.
        self.assertEqual(self.command_log(),
                         "sudo <apt-get> <update>\n"
                         "sudo <apt-get> <install> <python3> <python3-venv> <python3-pip> <ca-certificates>"
                         " <curl> <openssl> <openssh-client> <coreutils> <systemd> <dbus-user-session>\n")

    def test_existing_codex_is_never_overwritten_and_companion_is_checked(self):
        self.shim("codex", 'printf "codex-ran\\n" >> "$SETUP_TEST_LOG"')
        result = self.run_setup("codex")
        self.assertEqual(result.returncode, 0)
        self.assertIn("existing installation was not changed", result.stdout)
        self.assertIn("codex-code-mode-host", result.stdout)
        self.assertEqual(self.command_log(), "")
        (self.bin / "codex-code-mode-host").write_text("fixture")
        result = self.run_setup("codex", "--check")
        self.assertEqual(result.returncode, 0)
        self.assertNotIn("companion was not found", result.stdout)

    def test_codex_check_missing_is_nonmutating(self):
        result = self.run_setup("codex", "--check")
        self.assertEqual(result.returncode, 1)
        self.assertIn("not found", result.stderr)
        self.assertEqual(self.command_log(), "")
        self.assertEqual(list(self.scratch.iterdir()), [])

    def test_codex_download_without_terminal_is_refused(self):
        result = self.run_setup("codex")
        self.assertEqual(result.returncode, 1)
        self.assertIn("interactive terminal", result.stderr)
        self.assertEqual(self.command_log(), "")

    def test_codex_download_requires_separate_execution_confirmation_and_cleans_up(self):
        self.fake_download()
        result = self.run_function("install_codex", answers=["DOWNLOAD", "no"])
        self.assertEqual(result.returncode, 1)
        self.assertIn("not a security audit", result.stdout)
        self.assertEqual(self.command_log(), "curl\n")
        self.assertEqual(list(self.scratch.iterdir()), [])

    def test_codex_invalid_shell_download_is_not_executed(self):
        self.fake_download(valid=False)
        result = self.run_function("install_codex", answers=["DOWNLOAD", "INSTALL"])
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.command_log(), "curl\n")
        self.assertEqual(list(self.scratch.iterdir()), [])

    def test_confirmed_codex_download_executes_without_sudo_or_automatic_login(self):
        self.fake_download()
        result = self.run_function("install_codex", answers=["DOWNLOAD", "INSTALL"])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.command_log(), "curl\ninstaller-ran\n")
        self.assertEqual(list(self.scratch.iterdir()), [])

    def test_codex_login_requires_explicit_confirmation_and_uses_private_file_store(self):
        self.shim("codex", 'printf "codex" >> "$SETUP_TEST_LOG"; printf " <%s>" "$@" >> "$SETUP_TEST_LOG"')
        result = self.run_setup("codex", "--login")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.command_log(), "")
        result = self.run_function("codex_login", answers=["LOGIN"])
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self.command_log(), 'codex <-c> <cli_auth_credentials_store="file"> <login>')

    def test_custom_codex_home_blocks_misleading_host_login_without_changing_it(self):
        self.shim("codex", 'printf "codex-ran\\n" >> "$SETUP_TEST_LOG"')
        custom_home = self.root / "different codex home"
        self.environment["CODEX_HOME"] = str(custom_home)
        result = self.run_function("codex_login", answers=["LOGIN"])
        self.assertEqual(result.returncode, 1)
        self.assertIn("reads only ~/.codex/auth.json", result.stdout)
        self.assertIn("env -u CODEX_HOME bash setup.sh codex --login", result.stdout)
        self.assertEqual(self.environment["CODEX_HOME"], str(custom_home))
        self.assertFalse(custom_home.exists())
        self.assertEqual(self.command_log(), "")

    def test_explicit_default_codex_home_still_allows_confirmed_login(self):
        self.shim("codex", 'printf "codex-ran\\n" >> "$SETUP_TEST_LOG"')
        self.environment["CODEX_HOME"] = str(self.home / ".codex") + "/"
        result = self.run_function("codex_login", answers=["LOGIN"])
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self.command_log(), "codex-ran\n")

    def test_host_labels_recommend_linux_not_wsl_hosting(self):
        result = self.run_setup("--help")
        self.assertIn("Install a new Linux host", result.stdout)
        self.assertNotIn("Linux/WSL host", result.stdout)

    def test_root_user_cannot_install_os_packages_codex_or_host(self):
        self.shim("id", 'printf "0\\n"')
        for arguments in (("prerequisites",), ("codex",), ("host",)):
            result = self.run_setup(*arguments)
            self.assertEqual(result.returncode, 1)
            self.assertIn("without sudo", result.stderr)
        self.assertEqual(self.command_log(), "")

    def test_relay_delegates_to_validated_existing_tunnel_script(self):
        result = self.run_setup("relay", "member@relay.example")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("<-NT>", self.command_log())
        self.assertIn("<127.0.0.2:8767:127.0.0.1:18767>", self.command_log())
        self.assertIn("<member@relay.example>", self.command_log())
        self.log.unlink()
        result = self.run_setup("relay", "-oProxyCommand=anything")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(self.command_log(), "")

    def test_unknown_arguments_do_not_install(self):
        for arguments in (("unknown",), ("team", "unexpected"), ("check", "--yes"),
                          ("prerequisites", "--yes"), ("codex", "--yes")):
            result = self.run_setup(*arguments)
            self.assertEqual(result.returncode, 2, arguments)
        self.assertEqual(self.command_log(), "")


if __name__ == "__main__":
    unittest.main()
