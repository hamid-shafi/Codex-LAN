#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
export PYTHONUTF8=1
export LC_CTYPE="${LC_CTYPE:-C.UTF-8}"
exec python3 "$APP_DIR/app.py" "$@"

