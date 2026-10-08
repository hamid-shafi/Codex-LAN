#!/usr/bin/env bash
# Keep the first-run entry point simple; implementation lives with host setup.
set -euo pipefail
project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec bash "$project_dir/setup/host/setup.sh" "$@"
