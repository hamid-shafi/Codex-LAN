@echo off
setlocal EnableExtensions DisableDelayedExpansion
title Codex-LAN - Start here
rem Use Windows-provided tools, never an executable from the extracted folder.
set "codex_system_dir=%SystemRoot%\System32"
if exist "%SystemRoot%\Sysnative\cmd.exe" set "codex_system_dir=%SystemRoot%\Sysnative"
set "codex_guide=%~dp0..\..\docs\getting-started\index.html"
set "codex_check_noninteractive="
rem Accept fixed modes only; arguments are never forwarded to other commands.
if not "%~2"=="" goto invalid_arguments
if "%~1"=="" goto menu
if /I "%~1"=="--help" goto help
if /I "%~1"=="--check" goto check_noninteractive
goto invalid_arguments

:help
echo Codex-LAN Windows starter
echo Usage: start.cmd [--help ^| --check]
echo With no argument: interactive getting-started menu.
echo --check: read-only inventory; no WSL distro or installation is started.
echo Exit 0: inventory completed, even when optional tools are missing.
echo Exit 2: unsupported arguments. No software or settings are changed.
exit /B 0

:invalid_arguments
echo Unsupported arguments. Use start.cmd --help.
exit /B 2

:check_noninteractive
set "codex_check_noninteractive=1"
goto diagnostics

:menu
cls
echo.
echo   Codex-LAN
echo   Share a coding workspace with your team.
echo.
echo   1. Open the getting-started guide
echo      Choose: host a workspace, join a team, or connect through a relay.
echo   2. Check this Windows computer ^(read only^)
echo   3. Show Windows / WSL preparation steps
echo   4. Exit
echo.
echo   No Python or PowerShell script is needed for this starter.
echo   No software is installed and no system settings are changed here.
echo.
"%codex_system_dir%\choice.exe" /C 1234 /N /M "Choose [1-4]: "
if errorlevel 255 goto choice_failed
if errorlevel 4 goto finish
if errorlevel 3 goto preparation
if errorlevel 2 goto diagnostics
if errorlevel 1 goto guide
goto finish

:guide
if not exist "%codex_guide%" goto missing_guide
start "" "%codex_guide%"
if errorlevel 1 goto guide_failed
echo.
echo The guide was opened in your default browser. No connection was started.
pause
goto menu

:missing_guide
echo.
echo The guide is missing. Extract the entire repository ZIP first.
echo Keep setup and docs together; do not copy only this starter.
pause
goto menu

:guide_failed
echo.
echo Windows could not open the guide automatically.
echo Open docs\getting-started\index.html from the extracted repository.
pause
goto menu

:diagnostics
if not defined codex_check_noninteractive cls
echo.
echo   Read-only Windows checks
echo.
echo Your browser can request workspace access without Python or WSL.
echo WSL is needed for a Connector that works on your local files.
echo These checks do not connect to SSH, start a distro, or verify a tunnel.
echo WSL is not executed here: an uninstalled Windows stub can offer installation.
echo.
if /I "%PROCESSOR_ARCHITECTURE%"=="ARM64" goto unsupported_architecture
if /I "%PROCESSOR_ARCHITEW6432%"=="ARM64" goto unsupported_architecture
if /I "%PROCESSOR_ARCHITECTURE%"=="AMD64" goto check_ssh
if /I "%PROCESSOR_ARCHITEW6432%"=="AMD64" goto check_ssh

:unsupported_architecture
echo [NOTE] The provided Connector bundle supports Linux x86-64 only.
echo        Native Windows and Windows on ARM Connector bundles are not provided.
echo        Browser access is separate from local Connector support.
echo.

:check_ssh
if exist "%codex_system_dir%\OpenSSH\ssh.exe" goto ssh_found
echo [MISSING] Windows OpenSSH Client was not found in the standard location.
echo           Ask IT to enable the OpenSSH Client optional feature if you need a relay.
echo           Do not install OpenSSH Server or open inbound firewall ports.
goto check_wsl

:ssh_found
echo [FOUND] Windows OpenSSH Client:
"%codex_system_dir%\OpenSSH\ssh.exe" -V
if errorlevel 1 echo [NOTE] OpenSSH exists but could not run; ask IT to check its installation.

:check_wsl
echo.
if exist "%codex_system_dir%\wsl.exe" goto wsl_found
echo [MISSING] WSL was not found. See preparation steps in option 3.
goto checks_finished

:wsl_found
echo [FOUND] WSL command. Presence alone does not mean Ubuntu is ready.
echo.
echo To inspect installed distributions yourself, run in a Windows terminal:
echo       wsl --status
echo       wsl --list --verbose
echo Confirm your Ubuntu distribution shows VERSION 2.
echo If Wsl/E_ACCESSDENIED appears, ask IT to permit WSL; do not change security policy.

:checks_finished
echo.
echo For Linux prerequisites and systemd, run the checks in option 3 inside Ubuntu.
echo A successful check is not proof of SSH authentication or application approval.
if defined codex_check_noninteractive goto finish
pause
goto menu

:preparation
cls
echo.
echo   Windows / WSL preparation - instructions only
echo.
echo JOIN A WORKSPACE
echo   Get the workspace URL and verified certificate from its administrator.
echo   Open it in your Windows browser, request access, and wait for approval.
echo   The starter contains no account, password, certificate, or pairing token.
echo.
echo USE A SERVER RELAY
echo   Follow the guide for a Windows SSH tunnel and trusted laptop certificate.
echo   SSH access and workspace approval are separate. Use your own SSH login/key.
echo   Windows and WSL may need separate tunnels; keep each required tunnel open.
echo.
echo WORK WITH YOUR LOCAL FILES
echo   The Connector runs in WSL 2 Ubuntu x86-64, not native Windows or Git Bash.
echo   Windows Python is not required; the Connector uses Python inside Ubuntu.
echo   If WSL is missing, ask IT or run this yourself in an Administrator terminal:
echo.
echo       wsl --install -d Ubuntu
echo.
echo   This downloads software and changes Windows features; a restart may be required.
echo   We do not run it automatically. Save your work before restarting Windows.
echo   Launch Ubuntu and create a normal Linux user, not root.
echo   Inside Ubuntu, check:
echo.
echo       uname -m
echo       python3 --version
echo       systemctl --user show-environment ^>/dev/null
echo.
echo   Expect x86_64, supported Python ^(see guide^), and a working systemd user session.
echo   If Python is missing, use the included Linux prerequisite helper from Ubuntu.
echo   If systemd fails, follow Microsoft's WSL systemd guide linked below.
echo   Preserve existing /etc/wsl.conf settings. WSL shutdown stops ALL distros;
echo   save all WSL work before any manual shutdown or restart.
echo   Wsl/E_ACCESSDENIED needs Windows/IT approval, not a different pairing file.
echo.
echo   After workspace approval, download your private Connector installer from
echo   Connector in the browser and run it inside Ubuntu as your normal Linux user.
echo   Pairings expire; download a fresh one if needed. Do not share private installers.
echo.
echo HOST A WORKSPACE
echo   Use the Linux host path in the guide. Native Windows hosting is not supported.
echo   A WSL laptop is not presented as a supported LAN host by this starter.
echo.
echo Official Windows references:
echo   https://learn.microsoft.com/windows/wsl/install
echo   https://learn.microsoft.com/windows/wsl/systemd
echo.
pause
goto menu

:choice_failed
echo.
echo The Windows menu could not read a choice.
echo Open docs\getting-started\index.html directly instead.
exit /B 1

:finish
endlocal
exit /B 0
