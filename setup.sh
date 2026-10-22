#!/usr/bin/env bash
# Product entry point: no Python is needed to choose a role or inspect prerequisites.
set -euo pipefail
project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec bash "$project_dir/setup/bootstrap/linux.sh" "$@"
