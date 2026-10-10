#!/usr/bin/env bash
# Restore a trusted private bundle into an independently installed, empty host.
set -euo pipefail
transfer_bundle_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
exec python3 -B "$transfer_bundle_dir/transfer_lan_history.py" restore --bundle "$transfer_bundle_dir" "$@"
