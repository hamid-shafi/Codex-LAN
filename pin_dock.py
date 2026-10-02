#!/usr/bin/env python3
"""Add the desktop launcher to GNOME's favorites without removing existing ones."""

from __future__ import annotations

import ast
import shutil
import subprocess
import sys


DESKTOP_ID = "codex.desktop"
LEGACY_DESKTOP_ID = "hamsohbat-codex.desktop"


def main() -> int:
    if not shutil.which("gsettings"):
        print("gsettings was not found; Codex was installed but could not be pinned.")
        return 0
    try:
        raw = subprocess.check_output(
            ["gsettings", "get", "org.gnome.shell", "favorite-apps"],
            text=True,
            encoding="utf-8",
        ).strip()
        if raw.startswith("@as "):
            raw = raw[4:]
        favorites = list(ast.literal_eval(raw))
        original = list(favorites)
        favorites = [
            DESKTOP_ID if item == LEGACY_DESKTOP_ID else item
            for item in favorites
        ]
        if DESKTOP_ID not in favorites:
            favorites.append(DESKTOP_ID)
        favorites = list(dict.fromkeys(favorites))
        changed = favorites != original
        if changed:
            subprocess.run(
                [
                    "gsettings",
                    "set",
                    "org.gnome.shell",
                    "favorite-apps",
                    repr(favorites),
                ],
                check=True,
            )
        print("Codex was added to the Dock.")
        return 0
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"Could not update the Dock favorite: {exc}", file=sys.stderr)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
