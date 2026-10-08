#!/usr/bin/env bash
set -euo pipefail

SOURCE_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
PROJECT_DIR="$SOURCE_DIR"
if [[ -f "$SOURCE_DIR/../../project_layout.py" ]]; then
  PROJECT_DIR="$(CDPATH= cd -- "$SOURCE_DIR/../.." && pwd)"
fi
DATA_ROOT="${XDG_DATA_HOME:-$HOME/.local/share}"
APP_DIR="$DATA_ROOT/hamsohbat-codex"
DESKTOP_DIR="$DATA_ROOT/applications"
ICON_DIR="$DATA_ROOT/icons/hicolor/scalable/apps"

mkdir -p "$APP_DIR/assets" "$DESKTOP_DIR" "$ICON_DIR"
install -m 0755 "$SOURCE_DIR/app.py" "$APP_DIR/app.py"
install -m 0644 "$SOURCE_DIR/activity_model.py" "$APP_DIR/activity_model.py"
install -m 0644 "$SOURCE_DIR/attachment_preview.py" "$APP_DIR/attachment_preview.py"
install -m 0755 "$SOURCE_DIR/backend.py" "$APP_DIR/backend.py"
install -m 0644 "$SOURCE_DIR/codex_bridge.py" "$APP_DIR/codex_bridge.py"
install -m 0644 "$SOURCE_DIR/branch_controller.py" "$APP_DIR/branch_controller.py"
install -m 0644 "$SOURCE_DIR/branch_view.py" "$APP_DIR/branch_view.py"
install -m 0644 "$SOURCE_DIR/markdown_view.py" "$APP_DIR/markdown_view.py"
install -m 0644 "$SOURCE_DIR/storage.py" "$APP_DIR/storage.py"
install -m 0644 "$SOURCE_DIR/style.css" "$APP_DIR/style.css"
install -m 0644 "$PROJECT_DIR/README.md" "$APP_DIR/README.md"
if [[ -f "$PROJECT_DIR/docs/architecture/ARCHITECTURE.md" ]]; then
  install -m 0644 "$PROJECT_DIR/docs/architecture/ARCHITECTURE.md" "$APP_DIR/ARCHITECTURE.md"
else
  install -m 0644 "$PROJECT_DIR/ARCHITECTURE.md" "$APP_DIR/ARCHITECTURE.md"
fi
install -m 0755 "$SOURCE_DIR/run.sh" "$APP_DIR/run.sh"
install -m 0644 "$SOURCE_DIR/assets/codex.svg" "$APP_DIR/assets/codex.svg"
install -m 0644 "$SOURCE_DIR/assets/codex.svg" "$ICON_DIR/codex.svg"

sed "s|@APP_DIR@|$APP_DIR|g" \
  "$SOURCE_DIR/codex.desktop.in" \
  > "$DESKTOP_DIR/codex.desktop"
chmod 0644 "$DESKTOP_DIR/codex.desktop"
rm -f "$DESKTOP_DIR/hamsohbat-codex.desktop"

if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database "$DESKTOP_DIR" >/dev/null 2>&1 || true
fi

python3 "$SOURCE_DIR/pin_dock.py"
echo "Codex installed: $APP_DIR/run.sh"
