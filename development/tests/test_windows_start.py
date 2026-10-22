"""Source-contract checks for the Windows starter; not Windows runtime tests."""

from pathlib import Path
import os
import re
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[2]
STARTER = ROOT / "setup" / "windows" / "start.cmd"


class WindowsStartSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = STARTER.read_text(encoding="ascii")
        cls.commands = [
            line.strip() for line in cls.source.splitlines()
            if line.strip() and not re.match(r"(?i)^(?:rem\b|echo(?:\.|\s))", line.strip())
        ]

    def test_menu_has_no_python_or_powershell_requirement(self):
        self.assertIn("choice.exe\" /C 1234 /N", self.source)
        self.assertNotRegex("\n".join(self.commands), r"(?i)\b(?:python\d*|powershell|pwsh)(?:\.exe)?\b")
        self.assertIn("No Python or PowerShell script is needed", self.source)

    def test_guide_is_local_relative_and_checks_for_complete_extraction(self):
        self.assertIn('%~dp0..\\..\\docs\\getting-started\\index.html', self.source)
        self.assertIn('if not exist "%codex_guide%" goto missing_guide', self.source)
        self.assertIn('start "" "%codex_guide%"', self.source)
        self.assertIn("Extract the entire repository ZIP first", self.source)
        self.assertIn("DisableDelayedExpansion", self.source)

    def test_tools_are_windows_system_tools_not_path_or_working_directory(self):
        self.assertIn('set "codex_system_dir=%SystemRoot%\\System32"', self.source)
        self.assertIn('%SystemRoot%\\Sysnative\\cmd.exe', self.source)
        for tool in ("choice.exe", "OpenSSH\\ssh.exe", "wsl.exe"):
            self.assertIn(f'"%codex_system_dir%\\{tool}"', self.source)

    def test_diagnostics_never_start_a_distro_or_connect_to_a_server(self):
        wsl_commands = [line for line in self.commands if line.startswith('"%codex_system_dir%\\wsl.exe"')]
        self.assertEqual(wsl_commands, [])
        ssh_commands = [line for line in self.commands if line.startswith('"%codex_system_dir%\\OpenSSH\\ssh.exe"')]
        self.assertEqual(ssh_commands, ['"%codex_system_dir%\\OpenSSH\\ssh.exe" -V'])
        self.assertIn("an uninstalled Windows stub can offer installation", self.source)
        self.assertIn("A successful check is not proof of SSH authentication", self.source)

    def test_installation_is_shown_only_and_system_policies_stay_unchanged(self):
        commands = "\n".join(self.commands)
        self.assertNotRegex(commands, r"(?i)--install|--update|--shutdown|--unregister|--set-version|\b(?:reg|netsh|dism|certutil|winget|msiexec)(?:\.exe)?\b")
        self.assertNotRegex(self.source, r"(?i)Set-ExecutionPolicy|ExecutionPolicy\s+Bypass|Verb\s+RunAs|--insecure|SkipCertificateCheck")
        self.assertIn("echo       wsl --install -d Ubuntu", self.source)
        self.assertIn("downloads software and changes Windows features", self.source)
        self.assertIn("save all WSL work", self.source)

    def test_only_fixed_flags_are_accepted_and_not_forwarded_to_commands(self):
        self.assertNotRegex(self.source, r"(?i)\bset\s+/p\b|\bcall\s|\bfor\s+/f\b")
        self.assertNotIn("%*", self.source)
        argument_lines = [line for line in self.commands if re.search(r"%~?[1-9]", line)]
        self.assertEqual(argument_lines, [
            'if not "%~2"=="" goto invalid_arguments',
            'if "%~1"=="" goto menu',
            'if /I "%~1"=="--help" goto help',
            'if /I "%~1"=="--check" goto check_noninteractive',
        ])
        self.assertIn("if errorlevel 255 goto choice_failed", self.source)
        self.assertLess(self.source.index("if errorlevel 4"), self.source.index("if errorlevel 1"))

    def test_noninteractive_check_has_no_pause_and_explicit_exit_status(self):
        self.assertIn('set "codex_check_noninteractive=1"', self.source)
        self.assertIn("if defined codex_check_noninteractive goto finish\npause", self.source)
        self.assertIn("Exit 0: inventory completed", self.source)
        self.assertIn("exit /B 2", self.source)

    @unittest.skipUnless(os.name == "nt", "Windows unavailable; source contracts only")
    def test_windows_noninteractive_entrypoints(self):
        executable = str(Path(os.environ["SystemRoot"]) / "System32" / "cmd.exe")
        for flags, expected, marker in (
            (["--help"], 0, "Usage: start.cmd"),
            (["--check"], 0, "Read-only Windows checks"),
            (["--unknown"], 2, "Unsupported arguments"),
            (["--check", "extra"], 2, "Unsupported arguments"),
        ):
            with self.subTest(flags=flags):
                result = subprocess.run(
                    [executable, "/D", "/C", str(STARTER), *flags],
                    text=True, capture_output=True, timeout=15,
                )
                self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
                self.assertIn(marker, result.stdout)

    def test_wsl_errors_and_connector_scope_are_clear(self):
        for text in (
            "Wsl/E_ACCESSDENIED", "ask IT", "VERSION 2", "Linux x86-64 only",
            "WSL 2 Ubuntu x86-64", "not native Windows or Git Bash",
            "Your browser can request workspace access without Python or WSL",
            "Windows Python is not required", "not root", "systemctl --user",
            "Native Windows hosting is not supported", "not presented as a supported LAN host",
            "Preserve existing /etc/wsl.conf", "stops ALL distros",
        ):
            self.assertIn(text, self.source)

    def test_architecture_checks_cover_native_and_emulated_windows(self):
        for variable in ("PROCESSOR_ARCHITECTURE", "PROCESSOR_ARCHITEW6432"):
            self.assertIn(f'"%{variable}%"=="ARM64"', self.source)
            self.assertIn(f'"%{variable}%"=="AMD64"', self.source)

    def test_every_goto_target_exists(self):
        labels = set(re.findall(r"^:([a-z_]+)$", self.source, flags=re.M))
        targets = set(re.findall(r"\bgoto ([a-z_]+)\b", self.source, flags=re.I))
        self.assertFalse(targets - labels)

    def test_redirect_in_printed_linux_command_is_escaped(self):
        self.assertIn("echo       systemctl --user show-environment ^>/dev/null", self.source)
        for line in self.source.splitlines():
            if line.startswith("echo"):
                self.assertNotRegex(line, r"(?<!\^)[><|&]")


if __name__ == "__main__":
    unittest.main()
