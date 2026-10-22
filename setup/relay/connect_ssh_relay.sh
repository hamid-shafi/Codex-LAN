#!/usr/bin/env bash
# CODEX: Keep a verified SSH connection open for the laptop's HTTPS relay.
set -Eeuo pipefail

usage() {
    printf '%s\n' \
        'Usage: bash connect_ssh_relay.sh USER@RELAY [--identity PRIVATE_KEY]' \
        'Use an existing SSH identity authorized by the relay administrator.' \
        'After SSH connects, open https://127.0.0.2:8767 in your browser.' \
        'Successful SSH authentication leaves this terminal silent; no shell prompt is expected.' \
        'This message alone does not confirm authentication or laptop availability.' \
        'Keep this terminal open; press Ctrl+C to disconnect.'
}

if (($# == 1)) && [[ "$1" == --help || "$1" == -h ]]; then
    usage
    exit 0
fi
if (($# != 1 && $# != 3)); then
    usage >&2
    exit 2
fi
codex_relay_identity=()
if (($# == 3)); then
    if [[ "$2" != --identity || ! -f "$3" || ! -r "$3" ]]; then
        printf '%s\n' 'Use --identity with a readable existing private key file; never send that file to anyone.' >&2
        exit 2
    fi
    codex_relay_identity=(-o IdentitiesOnly=yes -i "$3")
fi
codex_relay_target="$1"
if [[ ! "$codex_relay_target" =~ ^[A-Za-z0-9_][A-Za-z0-9_.-]*@([A-Za-z0-9][A-Za-z0-9._-]*|\[[A-Fa-f0-9:]+\])$ ]]; then
    printf '%s\n' 'Use USER@RELAY with a relay hostname, SSH host alias, or IP address.' >&2
    exit 2
fi
if ! command -v ssh >/dev/null 2>&1; then
    printf '%s\n' 'OpenSSH client (ssh) is required.' >&2
    exit 1
fi

printf '%s\n' \
    'Verify any new SSH host-key fingerprint with the administrator before accepting it.' \
    'After successful authentication SSH stays silent: this is a tunnel, not a remote shell.' \
    'Permission denied means login failed. Stop and check your authorized username/key; do not keep retrying passwords.' \
    'Once connected, open https://127.0.0.2:8767 and verify/trust the laptop CA certificate.' \
    'In WSL, this tunnel serves the WSL Connector. Use the PowerShell launcher separately for a Windows browser.' \
    'SSH stays in this terminal. Ctrl+C closes browser and Connector access through this tunnel.'
exec ssh -NT \
    -o StrictHostKeyChecking=ask \
    -o ExitOnForwardFailure=yes \
    -o ServerAliveInterval=15 \
    -o ServerAliveCountMax=3 \
    -o ConnectTimeout=10 \
    -o NumberOfPasswordPrompts=1 \
    -o ForwardAgent=no \
    -o ForwardX11=no \
    -L 127.0.0.2:8767:127.0.0.1:18767 \
    "${codex_relay_identity[@]}" \
    "$codex_relay_target"
