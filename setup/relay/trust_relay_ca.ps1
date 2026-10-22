# Explicitly trust the verified laptop CA for the current Windows user only.
# This script never requests elevation or changes execution/security policies.
[CmdletBinding()]
param(
    [string]$CaFile = (Join-Path $PSScriptRoot 'Codex-LAN-CA.crt'),
    [string]$ExpectedFingerprint,
    [string]$FingerprintFile = (Join-Path $PSScriptRoot 'CA-fingerprint.txt'),
    [switch]$Help
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

function Get-NormalizedFingerprint([string]$Value) {
    $normalized = $Value.Trim() -replace '(?i)^SHA256\s+Fingerprint\s*=\s*', ''
    if ($normalized -notmatch '^(?:[A-Fa-f0-9]{64}|(?:[A-Fa-f0-9]{2}:){31}[A-Fa-f0-9]{2})$') {
        throw 'Expected exactly one full SHA-256 certificate fingerprint (64 hexadecimal digits, optionally colon-separated).'
    }
    return ($normalized -replace ':', '').ToUpperInvariant()
}

if ($Help) {
    Write-Host 'Usage: .\trust_relay_ca.ps1 [-ExpectedFingerprint SHA256]'
    Write-Host 'Verify the full fingerprint independently with the administrator before typing TRUST.'
    Write-Host 'Only Cert:\CurrentUser\Root is modified. No administrator rights are requested.'
    Write-Host 'If company policy blocks trust, ask your IT administrator; do not bypass certificate checks.'
    exit 0
}

$certificate = $null
try {
    $caPath = (Resolve-Path -LiteralPath $CaFile -ErrorAction Stop).ProviderPath
    if (-not $ExpectedFingerprint) {
        $ExpectedFingerprint = Get-Content -LiteralPath $FingerprintFile -Raw -ErrorAction Stop
    }
    $expected = Get-NormalizedFingerprint $ExpectedFingerprint
    $certificateBytes = [IO.File]::ReadAllBytes($caPath)
    $certificateText = [Text.Encoding]::ASCII.GetString($certificateBytes)
    if ($certificateText.Contains('-----BEGIN')) {
        # Decode exactly one certificate, never a private key or certificate bundle.
        $pem = [regex]::Match($certificateText, '\A\s*-----BEGIN CERTIFICATE-----\s*([A-Za-z0-9+/=\s]+?)\s*-----END CERTIFICATE-----\s*\z')
        if (-not $pem.Success) {
            throw 'The CA file must contain exactly one public certificate and no private key.'
        }
        $certificateBytes = [Convert]::FromBase64String($pem.Groups[1].Value)
    }
    $contentType = [Security.Cryptography.X509Certificates.X509Certificate2]::GetCertContentType($certificateBytes)
    if ($contentType -ne [Security.Cryptography.X509Certificates.X509ContentType]::Cert) {
        throw 'Only a single public X.509 certificate is accepted, not a private-key container or certificate bundle.'
    }
    $certificate = [Security.Cryptography.X509Certificates.X509Certificate2]::new($certificateBytes)
    if ($certificate.HasPrivateKey) {
        throw 'Only a public CA certificate may be imported.'
    }
    $sha256 = [Security.Cryptography.SHA256]::Create()
    try {
        $actual = [BitConverter]::ToString($sha256.ComputeHash($certificate.RawData)).Replace('-', '')
    }
    finally {
        $sha256.Dispose()
    }
    if ($actual -cne $expected) {
        throw 'CA SHA-256 fingerprint mismatch. Nothing was imported. Obtain a verified bundle from the administrator.'
    }
    $constraintsExtension = $certificate.Extensions | Where-Object { $_.Oid.Value -eq '2.5.29.19' } | Select-Object -First 1
    if ($null -eq $constraintsExtension) {
        throw 'The certificate does not contain CA basic constraints.'
    }
    $constraints = [Security.Cryptography.X509Certificates.X509BasicConstraintsExtension]::new($constraintsExtension, $constraintsExtension.Critical)
    if (-not $constraints.CertificateAuthority) {
        throw 'The supplied certificate is not a certificate authority.'
    }
    $now = Get-Date
    if ($now -lt $certificate.NotBefore -or $now -gt $certificate.NotAfter) {
        throw 'The CA certificate is outside its validity period. Check your clock and contact the administrator.'
    }

    # Format the already-computed full DER SHA-256 without using SHA-1 Thumbprint.
    $displayFingerprint = ([regex]::Matches($actual, '.{2}') | ForEach-Object { $_.Value }) -join ':'
    Write-Host "CA subject: $($certificate.Subject)"
    Write-Host "SHA-256: $displayFingerprint"
    Write-Host 'A matching bundled fingerprint alone is not proof of trust.'
    Write-Host 'Compare this full fingerprint with the administrator through an independent trusted channel.'
    Write-Host 'Trusting a CA permits it to authenticate TLS servers for this Windows user.'
    Write-Host 'Only the current user Trusted Root store will change; no system-wide trust is installed.'
    $confirmation = Read-Host 'After independent verification, type TRUST to continue (anything else cancels)'
    if ($confirmation -cne 'TRUST') {
        Write-Host 'Cancelled. No certificate was imported.'
        exit 1
    }

    # Add the exact in-memory certificate whose DER fingerprint was verified.
    # Re-reading the path here could trust a file changed after verification.
    $store = [Security.Cryptography.X509Certificates.X509Store]::new('Root', [Security.Cryptography.X509Certificates.StoreLocation]::CurrentUser)
    try {
        $store.Open([Security.Cryptography.X509Certificates.OpenFlags]::ReadWrite)
        $store.Add($certificate)
    }
    finally {
        $store.Close()
    }
    Write-Host 'Verified CA installed in Cert:\CurrentUser\Root. Reopen your Windows browser if necessary.'
}
catch {
    [Console]::Error.WriteLine($_.Exception.Message)
    [Console]::Error.WriteLine('No elevation was attempted. If trust is restricted by organization policy, ask your IT administrator.')
    exit 1
}
finally {
    if ($null -ne $certificate) {
        $certificate.Dispose()
    }
}
