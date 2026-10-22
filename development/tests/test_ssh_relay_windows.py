"""Source-contract checks for Windows relay helpers; not Windows execution tests."""

from pathlib import Path
import re
import shutil
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "setup" / "connector"


class WindowsRelaySourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tunnel = (SCRIPTS / "connect_ssh_relay.ps1").read_text()
        cls.trust = (SCRIPTS / "trust_relay_ca.ps1").read_text()

    def test_tunnel_keeps_exact_loopback_origin_and_verification(self):
        self.assertIn("'127.0.0.2:8767:127.0.0.1:18767'", self.tunnel)
        self.assertIn("$origin = 'https://127.0.0.2:8767'", self.tunnel)
        self.assertIn("'-NT'", self.tunnel)
        for option in (
            "StrictHostKeyChecking=ask", "ExitOnForwardFailure=yes",
            "NumberOfPasswordPrompts=1", "ConnectionAttempts=1",
            "ConnectTimeout=15", "ServerAliveInterval=15",
            "ServerAliveCountMax=3", "ForwardAgent=no", "ForwardX11=no",
        ):
            self.assertIn(option, self.tunnel)
        self.assertNotIn("0.0.0.0", self.tunnel)

    def test_target_and_key_are_explicit_without_shell_interpolation(self):
        self.assertIn("[Parameter(Position = 0)]", self.tunnel)
        self.assertIn("[string]$Relay", self.tunnel)
        self.assertIn("[string]$IdentityFile", self.tunnel)
        self.assertIn("if ($Relay -notmatch", self.tunnel)
        self.assertIn("Resolve-Path -LiteralPath $IdentityFile", self.tunnel)
        self.assertIn("'IdentitiesOnly=yes', '-i', $identityPath", self.tunnel)
        self.assertIn("& $ssh.Source @sshArguments", self.tunnel)
        self.assertNotIn("Invoke-Expression", self.tunnel)
        self.assertNotRegex(self.tunnel, r"(?i)(?:password|token)\s*=")

    def test_launch_is_not_reported_as_ready_and_errors_propagate(self):
        self.assertIn("intentionally silent", self.tunnel)
        self.assertIn("Permission denied is an authentication failure", self.tunnel)
        self.assertIn("Windows browsers need this Windows tunnel", self.tunnel)
        self.assertIn("WSL needs its own WSL tunnel", self.tunnel)
        self.assertIn("exit $sshExitCode", self.tunnel)
        self.assertIn("the tunnel is no longer running", self.tunnel)

    def test_check_verifies_tls_and_expected_health_instead_of_only_a_listener(self):
        self.assertIn("Get-Command curl.exe -CommandType Application", self.tunnel)
        self.assertIn("--noproxy '*'", self.tunnel)
        self.assertIn('--max-time 8 --cacert $caPath "$origin/api/health"', self.tunnel)
        self.assertIn("if ($LASTEXITCODE -ne 0)", self.tunnel)
        self.assertIn("$health.ok -ne $true -or $health.public_origin -ne $origin", self.tunnel)
        self.assertNotIn("--insecure", self.tunnel)
        self.assertNotIn("SkipCertificateCheck", self.tunnel)

    def test_ca_uses_full_der_sha256_not_sha1(self):
        self.assertIn("[Security.Cryptography.SHA256]::Create()", self.trust)
        self.assertIn("ComputeHash($certificate.RawData)", self.trust)
        self.assertIn("[A-Fa-f0-9]{64}", self.trust)
        self.assertIn("(?:[A-Fa-f0-9]{2}:){31}", self.trust)
        self.assertIn("if ($actual -cne $expected)", self.trust)
        self.assertNotIn("$certificate.Thumbprint", self.trust)
        self.assertNotIn("GetFileHash", self.trust)

    def test_ca_rejects_private_key_containers_and_non_ca_or_expired_certs(self):
        for required in (
            "GetCertContentType($certificateBytes)", "X509ContentType]::Cert",
            "$certificate.HasPrivateKey", "'2.5.29.19'",
            "$constraints.CertificateAuthority", "$certificate.NotBefore",
            "$certificate.NotAfter",
        ):
            self.assertIn(required, self.trust)
        self.assertLess(self.trust.index("GetCertContentType"), self.trust.index("X509Certificate2]::new"))

    def test_trust_requires_independent_verification_and_explicit_interactive_consent(self):
        self.assertIn("independent trusted channel", self.trust)
        self.assertIn("Read-Host", self.trust)
        self.assertIn("if ($confirmation -cne 'TRUST')", self.trust)
        self.assertLess(self.trust.index("if ($actual -cne $expected)"), self.trust.index("Read-Host"))
        self.assertLess(self.trust.index("if ($confirmation -cne 'TRUST')"), self.trust.index("$store.Open"))
        self.assertNotRegex(self.trust, r"\[switch\]\$(?:Yes|Force|Confirm)")

    def test_import_is_current_user_only_and_uses_the_verified_in_memory_certificate(self):
        self.assertIn("X509Store]::new('Root'", self.trust)
        self.assertIn("StoreLocation]::CurrentUser", self.trust)
        self.assertIn("$store.Add($certificate)", self.trust)
        self.assertNotIn("StoreLocation]::LocalMachine", self.trust)
        self.assertNotIn("Import-Certificate -FilePath", self.trust)
        for text in (self.trust, self.tunnel):
            self.assertNotRegex(text, r"(?i)Set-ExecutionPolicy|ExecutionPolicy\s+Bypass|Verb\s+RunAs|ServerCertificateValidationCallback")
            text.encode("ascii")

    @unittest.skipUnless(shutil.which("pwsh") or shutil.which("powershell"), "PowerShell unavailable; source contracts only")
    def test_powershell_parser_accepts_both_scripts(self):
        executable = shutil.which("pwsh") or shutil.which("powershell")
        for name in ("connect_ssh_relay.ps1", "trust_relay_ca.ps1"):
            path = str(SCRIPTS / name).replace("'", "''")
            command = (
                "$tokens = $null; $errors = $null; "
                f"[System.Management.Automation.Language.Parser]::ParseFile('{path}', [ref]$tokens, [ref]$errors) | Out-Null; "
                "if ($errors.Count) { $errors | Out-String | Write-Error; exit 1 }"
            )
            result = subprocess.run([executable, "-NoProfile", "-Command", command], text=True, capture_output=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
