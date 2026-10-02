"""CODEX: Safely activate personal usage analysis and restore its vetted weekly calibration."""

from pathlib import Path

import deploy_lan_backend
from repair_lan_quota import restore


INSTALLATION = Path.home() / ".local/share/codex-lan"
CALIBRATION_DATABASE = INSTALLATION / "updates/before-backend-7prowid1/server.sqlite3"


def main():
    deploy_lan_backend.main()
    result = restore(
        INSTALLATION / "server/server.sqlite3",
        CALIBRATION_DATABASE,
        replace_existing=True,
        repair_id="usage-analysis-v1-2026-09-09",
    )
    action = "restored" if result["changed"] else "already current"
    print(f"Personal usage analysis is active; trusted quota capacity {action}.", flush=True)


if __name__ == "__main__":
    main()
