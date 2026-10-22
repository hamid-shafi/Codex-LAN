# Keep a Windows-local, verified SSH tunnel open for Codex-LAN.
# Compatible with Windows PowerShell 5.1; no administrator privileges needed.
[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [string]$Relay,
    [string]$IdentityFile,
    [switch]$Check,
    [string]$CaFile = (Join-Path $PSScriptRoot 'Codex-LAN-CA.crt'),
    [switch]$Help
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'
$origin = 'https://127.0.0.2:8767'

if ($Help) {
    Write-Host 'Usage: .\connect_ssh_relay.ps1 USER@RELAY [-IdentityFile PATH]'
    Write-Host 'Verify from a second PowerShell window: .\connect_ssh_relay.ps1 -Check'
    Write-Host 'Use your own SSH account/key provided by the relay administrator.'
    Write-Host 'Keep this terminal open. Silence after successful authentication is normal.'
    Write-Host 'Windows browsers need this Windows tunnel; a WSL tunnel is separate.'
    exit 0
}

try {
    if ($Check) {
        $curl = Get-Command curl.exe -CommandType Application -ErrorAction Stop
        $caPath = (Resolve-Path -LiteralPath $CaFile -ErrorAction Stop).ProviderPath
        if (-not (Test-Path -LiteralPath $caPath -PathType Leaf)) {
            throw 'The public laptop CA certificate is missing.'
        }
        # curl.exe (not PowerShell's curl alias) verifies the laptop CA and IP SAN.
        $body = & $curl.Source --noproxy '*' --silent --show-error --fail `
            --max-time 8 --cacert $caPath "$origin/api/health"
        if ($LASTEXITCODE -ne 0) {
            throw 'HTTPS check failed. Keep the SSH tunnel open, verify authentication, and ask the administrator whether the laptop is online. Do not bypass certificate verification.'
        }
        $health = ($body -join "`n") | ConvertFrom-Json -ErrorAction Stop
        if ($health.ok -ne $true -or $health.public_origin -ne $origin) {
            throw 'The response is not a healthy Codex-LAN endpoint at the expected origin.'
        }
        Write-Host "Verified laptop HTTPS through this tunnel: $origin"
        Write-Host 'Browser access also requires trusting the verified laptop CA in Windows.'
        exit 0
    }

    if ($Relay -notmatch '\A[A-Za-z0-9_][A-Za-z0-9_.-]*@([A-Za-z0-9][A-Za-z0-9._-]*|\[[A-Fa-f0-9:]+\])\z') {
        throw 'Use USER@RELAY, or use -Help for instructions.'
    }
    $ssh = Get-Command ssh.exe -CommandType Application -ErrorAction Stop
    $sshArguments = @(
        '-NT',
        '-o', 'StrictHostKeyChecking=ask',
        '-o', 'ExitOnForwardFailure=yes',
        '-o', 'NumberOfPasswordPrompts=1',
        '-o', 'ConnectionAttempts=1',
        '-o', 'ConnectTimeout=15',
        '-o', 'ServerAliveInterval=15',
        '-o', 'ServerAliveCountMax=3',
        '-o', 'ForwardAgent=no',
        '-o', 'ForwardX11=no',
        '-L', '127.0.0.2:8767:127.0.0.1:18767'
    )
    if ($IdentityFile) {
        $identityPath = (Resolve-Path -LiteralPath $IdentityFile -ErrorAction Stop).ProviderPath
        if (-not (Test-Path -LiteralPath $identityPath -PathType Leaf)) {
            throw 'IdentityFile must identify your own SSH private-key file.'
        }
        $sshArguments += @('-o', 'IdentitiesOnly=yes', '-i', $identityPath)
    }
    $sshArguments += $Relay

    Write-Host 'Verify any new SSH host-key fingerprint with the administrator before accepting it.'
    Write-Host 'After successful authentication SSH is intentionally silent: no shell or prompt appears.'
    Write-Host "Keep this terminal open, then open $origin in a Windows browser."
    Write-Host 'In a second PowerShell window run: .\connect_ssh_relay.ps1 -Check'
    Write-Host 'Permission denied is an authentication failure, not a working tunnel. Do not keep guessing passwords.'
    Write-Host 'Ctrl+C closes this tunnel. A Connector running in WSL needs its own WSL tunnel.'
    & $ssh.Source @sshArguments
    $sshExitCode = $LASTEXITCODE
    if ($sshExitCode -ne 0) {
        Write-Warning "SSH exited with code $sshExitCode; this command no longer provides a tunnel. Check the SSH error above."
        exit $sshExitCode
    }
    Write-Host 'SSH connection closed; the tunnel is no longer running.'
    exit 0
}
catch {
    [Console]::Error.WriteLine($_.Exception.Message)
    exit 1
}
