#!/usr/bin/env bash
# CODEX: Bootstrap the per-user host from this checkout, then open the browser setup screen.
set -euo pipefail
codex_setup_source="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
if ! command -v python3 >/dev/null 2>&1; then
    printf '%s\n' 'Python 3.10 or newer is required. Install Python and its venv package from your Linux software manager.' >&2
    exit 1
fi
exec python3 "$codex_setup_source/install_lan_server.py" --start --open "$@"
