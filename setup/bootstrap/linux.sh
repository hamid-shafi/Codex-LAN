#!/usr/bin/env bash
# Guided setup only changes the machine after an explicit action and confirmation.
set -euo pipefail
codex_setup_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"

setup_help() {
    printf '%s\n' \
        'Codex-LAN — share a workspace with your trusted team' \
        '' \
        'Usage: bash setup.sh [ACTION]' \
        '  host [OPTIONS]        Install a new Linux host and open its setup screen.' \
        '  team                  How to join an existing host; no personal Codex login.' \
        '  relay USER@RELAY      Connect using an administrator-provided SSH relay.' \
        '                        Optional: --identity /path/to/private-key' \
        '  check                 Inspect prerequisites and host readiness; no changes.' \
        '  prerequisites         Offer OS packages on Debian/Ubuntu, with confirmation.' \
        '  prerequisites --check Inspect OS requirements without downloading anything.' \
        '  codex                 Offer the official Codex installer, with confirmation.' \
        '  codex --check         Locate Codex; no download or sign-in.' \
        '  codex --login         Start host sign-in only after confirmation.' \
        '  host --help           Advanced host installer options.' \
        '' \
        'No action selected? An interactive terminal shows the setup menu.' \
        'Windows: open setup/windows/start.cmd, or run this script inside WSL2 Ubuntu.' \
        'Initial installation needs internet access unless dependencies are pre-staged.' \
        'Chat history, account credentials, and private pairing files stay out of Git.'
}

confirm_action() {
    local codex_confirmation
    if [[ ! -t 0 || ! -t 1 ]]; then
        printf '%s\n' 'Confirmation requires an interactive terminal. Nothing was installed.' >&2
        return 1
    fi
    printf 'Type %s to continue, or press Enter to cancel: ' "$1"
    IFS= read -r codex_confirmation || return 1
    [[ "$codex_confirmation" == "$1" ]]
}

require_linux_user() {
    if [[ "$(uname -s)" != Linux ]]; then
        printf '%s\n' 'This host/Connector requires Linux. On Windows, use WSL2 Ubuntu; see docs/operations/WINDOWS-WSL.md.' >&2
        return 1
    fi
    if [[ "$(id -u)" == 0 ]]; then
        printf '%s\n' 'Run setup as your normal desktop user, without sudo. Only the OS package step requests sudo.' >&2
        return 1
    fi
}

check_prerequisites() {
    local codex_missing=0 codex_tool
    require_linux_user || return 1
    if command -v python3 >/dev/null 2>&1 && python3 -IB -c \
        'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1; then
        printf '%s\n' '[OK] Python 3.10 or newer'
        if python3 -IB -c 'import venv, ensurepip' >/dev/null 2>&1; then
            printf '%s\n' '[OK] Python venv and ensurepip'
        else
            printf '%s\n' '[NEEDED] Python venv/ensurepip (Debian/Ubuntu: python3-venv)'
            codex_missing=1
        fi
    else
        printf '%s\n' '[NEEDED] Python 3.10 or newer'
        codex_missing=1
    fi
    for codex_tool in openssl curl ssh; do
        if command -v "$codex_tool" >/dev/null 2>&1; then
            printf '[OK] %s\n' "$codex_tool"
        else
            printf '[NEEDED] %s\n' "$codex_tool"
            codex_missing=1
        fi
    done
    if command -v timeout >/dev/null 2>&1 && command -v systemctl >/dev/null 2>&1 && \
        timeout 10 systemctl --user show-environment >/dev/null 2>&1; then
        printf '%s\n' '[OK] systemd user session'
    else
        printf '%s\n' \
            '[NEEDED] A working systemd user session (and coreutils timeout).' \
            'Open a normal Linux desktop login. In WSL2, enable systemd using docs/operations/WINDOWS-WSL.md.' \
            'Installing packages alone does not activate a missing user session.'
        codex_missing=1
    fi
    return "$codex_missing"
}

supported_apt_distribution() {
    local codex_os_line codex_os_id=''
    [[ -r /etc/os-release ]] || return 1
    # Read the ID as data, never execute an os-release file as shell code.
    while IFS= read -r codex_os_line; do
        case "$codex_os_line" in
            ID=*) codex_os_id="${codex_os_line#ID=}"; break ;;
        esac
    done < /etc/os-release
    codex_os_id="${codex_os_id//\"/}"
    codex_os_id="${codex_os_id//\'/}"
    [[ "$codex_os_id" == ubuntu || "$codex_os_id" == debian ]]
}

prerequisites() {
    if (($# > 1)) || { (($# == 1)) && [[ "$1" != --check ]]; }; then
        printf '%s\n' 'Usage: bash setup.sh prerequisites [--check]' >&2
        return 2
    fi
    require_linux_user || return 1
    if check_prerequisites; then
        printf '%s\n' 'OS prerequisites passed. No packages or services were changed.'
        return 0
    fi
    if (($#)); then
        printf '%s\n' 'For guided package help: bash setup.sh prerequisites'
        return 1
    fi
    if ! supported_apt_distribution || ! command -v apt-get >/dev/null 2>&1; then
        printf '%s\n' \
            'Automatic package installation is offered only on Debian/Ubuntu.' \
            'Use your OS software manager to install Python 3.10+, its venv/ensurepip package,' \
            'CA certificates, curl, OpenSSL, OpenSSH client, coreutils, and systemd user-session support.' \
            'Then run: bash setup.sh prerequisites --check'
        return 1
    fi
    if ! command -v sudo >/dev/null 2>&1; then
        printf '%s\n' 'sudo is unavailable. Ask your administrator to install the listed OS prerequisites.' >&2
        return 1
    fi
    printf '%s\n' \
        '' \
        'This optional step changes OS packages using your configured Debian/Ubuntu repositories.' \
        'It requires internet access and OS administrator authorization; no password is stored.' \
        'Commands:' \
        '  sudo apt-get update' \
        '  sudo apt-get install python3 python3-venv python3-pip ca-certificates curl openssl openssh-client coreutils systemd dbus-user-session' \
        'No application services will be created or restarted by this helper.'
    confirm_action INSTALL || return 1
    sudo apt-get update
    sudo apt-get install python3 python3-venv python3-pip ca-certificates curl openssl openssh-client coreutils systemd dbus-user-session
    check_prerequisites
}

find_codex() {
    local codex_detected
    if codex_detected="$(command -v codex)"; then
        printf '%s\n' "$codex_detected"
    elif [[ -x "$HOME/.local/bin/codex" && -f "$HOME/.local/bin/codex" ]]; then
        printf '%s\n' "$HOME/.local/bin/codex"
    else
        return 1
    fi
}

check_codex_companion() {
    local codex_resolved
    codex_resolved="$(readlink -f -- "$1" 2>/dev/null)" || codex_resolved=''
    if [[ -z "$codex_resolved" || ! -f "${codex_resolved%/*}/codex-code-mode-host" ]]; then
        printf '%s\n' \
            '[NOTE] Connector downloads need the complete standalone Codex distribution,' \
            'including codex-code-mode-host beside the resolved binary. That companion was not found.' \
            'An npm shim or a lone codex executable is not sufficient for Connector packaging.' \
            'Review https://learn.chatgpt.com/docs/codex/cli; no existing installation was replaced.'
    fi
}

codex_login() {
    local codex_executable
    require_linux_user || return 1
    if [[ -n "${CODEX_HOME:-}" && "${CODEX_HOME%/}" != "$HOME/.codex" ]]; then
        printf '%s\n' \
            'A custom CODEX_HOME is set, but this host installer reads only ~/.codex/auth.json.' \
            'Sign-in here would save credentials somewhere the host installer does not read.' \
            'No login was started and your environment was not changed.' \
            'To deliberately use the supported default home for this login, run:' \
            '  env -u CODEX_HOME bash setup.sh codex --login' >&2
        return 1
    fi
    if ! codex_executable="$(find_codex)"; then
        printf '%s\n' 'Codex was not found. First run: bash setup.sh codex' >&2
        return 1
    fi
    printf '%s\n' \
        'Host-only sign-in. Team members do not need their own Codex login.' \
        'Codex-LAN needs file-backed authentication on the host.' \
        'The official login flow saves a private credential in your Codex home, outside this repository.' \
        'Never share that file, paste it into chat, or include it in a Connector package.'
    confirm_action LOGIN || return 1
    "$codex_executable" -c 'cli_auth_credentials_store="file"' login
}

install_codex() (
    local codex_temp_dir codex_installer_file
    require_linux_user || return 1
    if ! command -v curl >/dev/null 2>&1; then
        printf '%s\n' 'curl is required. Run: bash setup.sh prerequisites' >&2
        return 1
    fi
    printf '%s\n' \
        'Official Codex instructions: https://learn.chatgpt.com/docs/codex/cli' \
        'This downloads https://chatgpt.com/codex/install.sh to a private temporary directory.' \
        'It is not executed until a second confirmation. Downloading and installation need internet access.'
    confirm_action DOWNLOAD || return 1
    umask 077
    codex_temp_dir="$(mktemp -d "${TMPDIR:-/tmp}/codex-lan-codex.XXXXXXXX")"
    codex_installer_file="$codex_temp_dir/install.sh"
    trap 'rm -f -- "$codex_installer_file"; rmdir -- "$codex_temp_dir"' EXIT
    curl --proto '=https' --proto-redir '=https' --tlsv1.2 --fail --show-error --location \
        --connect-timeout 15 --max-time 120 --max-filesize 2097152 \
        --output "$codex_installer_file" https://chatgpt.com/codex/install.sh
    [[ -s "$codex_installer_file" ]] || { printf '%s\n' 'The downloaded installer is empty.' >&2; return 1; }
    sh -n "$codex_installer_file"
    printf 'Downloaded installer: %s\n' "$codex_installer_file"
    printf '%s\n' \
        'Shell syntax passed; this is not a security audit. You can inspect the file before continuing.' \
        'The official installer runs as your user, downloads Codex, and may configure your user PATH.' \
        'No sudo is used and no sign-in is started by this step.'
    confirm_action INSTALL || return 1
    sh "$codex_installer_file"
    printf '%s\n' \
        'Next: open a new terminal if necessary, then run bash setup.sh codex --check.' \
        'For host sign-in: bash setup.sh codex --login'
)

codex_setup() {
    local codex_executable
    if (($# > 1)) || { (($# == 1)) && [[ "$1" != --check && "$1" != --login ]]; }; then
        printf '%s\n' 'Usage: bash setup.sh codex [--check|--login]' >&2
        return 2
    fi
    if [[ "${1:-}" == --login ]]; then
        codex_login
    elif codex_executable="$(find_codex)"; then
        printf 'Codex found: %s\n' "$codex_executable"
        check_codex_companion "$codex_executable"
        printf '%s\n' \
            'Your existing installation was not changed. This check does not validate sign-in.' \
            'For host sign-in: bash setup.sh codex --login' \
            'For complete host readiness: bash setup.sh check'
    elif [[ "${1:-}" == --check ]]; then
        printf '%s\n' 'Codex was not found. Host only: run bash setup.sh codex to install it.' >&2
        return 1
    else
        install_codex
    fi
}

team_help() {
    printf '%s\n' \
        'Join a team — no separate Codex installation or OpenAI sign-in is required.' \
        '1. Ask the host administrator for the HTTPS address and CA fingerprint.' \
        '2. Verify the fingerprint independently; trust only that public CA certificate.' \
        '3. Open the address, choose Connect a new computer, and request access.' \
        '4. Ask the administrator to approve the matching computer name/verification code.' \
        '5. In that same browser/profile, open Connector and download your private one-step setup.' \
        '6. Run the private installer as your normal Linux user (WSL2 Ubuntu on Windows).' \
        '' \
        'The current offline Connector supports Linux x86_64 and CPython 3.10–3.14.' \
        'A working systemd user session is required. The host supplies the Connector runtime.' \
        'The private installer expires; do not share it or put it in Git.' \
        'Outside the LAN? First use bash setup.sh relay USER@RELAY with your approved SSH identity.' \
        'Windows browser and WSL Connector may each need their own tunnel.' \
        'Full instructions: docs/operations/WINDOWS-WSL.md and docs/operations/SSH-RELAY.md'
}

host_setup() {
    local codex_argument codex_executable
    for codex_argument in "$@"; do
        if [[ "$codex_argument" == --help || "$codex_argument" == -h ]]; then
            exec bash "$codex_setup_root/setup/host/setup.sh" "$@"
        fi
    done
    if ! check_prerequisites; then
        printf '%s\n' 'Prepare this computer first: bash setup.sh prerequisites' >&2
        return 1
    fi
    if codex_executable="$(find_codex)"; then
        check_codex_companion "$codex_executable"
    fi
    # Existing host setup owns login validation, running-host protection, and writes.
    export PYTHONDONTWRITEBYTECODE=1
    exec bash "$codex_setup_root/setup/host/setup.sh" "$@"
}

setup_menu() {
    local codex_choice
    if [[ ! -t 0 || ! -t 1 ]]; then
        setup_help
        printf '\n%s\n' 'No interactive terminal: choose an explicit action. Nothing was changed.'
        return 0
    fi
    printf '%s\n' \
        'Codex-LAN — what would you like to do?' \
        '  1  Host a workspace on this Linux laptop' \
        '  2  Join an existing team' \
        '  3  Connect through an SSH relay' \
        '  4  Check host readiness (no changes)' \
        '  5  Install missing OS prerequisites (confirmation required)' \
        '  6  Install Codex for a host (confirmation required)' \
        '  7  Sign in to Codex for a host (confirmation required)' \
        '  0  Exit'
    printf 'Choose a number [0]: '
    IFS= read -r codex_choice || return 0
    case "$codex_choice" in
        1)
            printf '%s\n' 'This installs a new per-user host, starts its services, and opens the admin browser.'
            confirm_action INSTALL || return 1
            host_setup ;;
        2) team_help ;;
        3)
            printf '%s\n' \
                'Ask your administrator for the relay account and approved key, then run:' \
                '  bash setup.sh relay USER@RELAY --identity /path/to/private-key' \
                'Read docs/operations/SSH-RELAY.md before trusting a new SSH host key.' ;;
        4) host_setup --check ;;
        5) prerequisites ;;
        6) codex_setup ;;
        7) codex_login ;;
        0|'') return 0 ;;
        *) printf '%s\n' 'Unknown selection. No changes were made.' >&2; return 2 ;;
    esac
}

setup_main() {
    if (($# == 0)); then
        setup_menu
        return
    fi
    local codex_action="$1"
    shift
    case "$codex_action" in
        --help|-h|help) setup_help ;;
        host) host_setup "$@" ;;
        prerequisites) prerequisites "$@" ;;
        codex) codex_setup "$@" ;;
        team)
            (($# == 0)) || { printf '%s\n' 'Usage: bash setup.sh team' >&2; return 2; }
            team_help ;;
        relay) exec bash "$codex_setup_root/setup/connector/connect_ssh_relay.sh" "$@" ;;
        check)
            (($# == 0)) || { printf '%s\n' 'Usage: bash setup.sh check' >&2; return 2; }
            host_setup --check ;;
        --*) host_setup "$codex_action" "$@" ;;
        *) printf 'Unknown action: %s\nRun bash setup.sh --help.\n' "$codex_action" >&2; return 2 ;;
    esac
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    setup_main "$@"
fi
