"""CODEX: Install the Ubuntu client from a self-contained, offline-capable bundle."""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path

_layout_root = next((parent for parent in Path(__file__).resolve().parents
                     if (parent / "project_layout.py").is_file()), None)
if _layout_root is not None:
    sys.path.insert(0, str(_layout_root))
    from project_layout import configure_imports, source_file, source_root
    configure_imports(__file__)
else:
    # Downloaded client archives retain their historical flat file allowlist.
    def source_root(path):
        return Path(path).resolve().parent

    def source_file(root, name):
        return Path(root) / name

PACKAGE_FILES = ("__init__.py", "worker.py", "media.py", "egress.py", "recovery.py", "transport.py", "model_registry.py")


def main():
    parser = argparse.ArgumentParser(description="Install Codex LAN client; no administrator privileges required")
    parser.add_argument("--server", required=True, help="SSH login for the SERVER, not this client")
    parser.add_argument("--port", type=int, default=8766, help="Loopback port on this client")
    parser.add_argument("--no-launch", action="store_true")
    args = parser.parse_args()
    if not re.fullmatch(r"[a-zA-Z0-9_.-]+@[a-zA-Z0-9_.-]+", args.server) or not 1024 <= args.port <= 65535:
        parser.error("Use an explicit server login USER@HOST and a valid local port")
    if not shutil.which("ssh"):
        parser.error("OpenSSH client is required (Ubuntu package: openssh-client)")
    if sys.version_info[:2] != (3, 10) or platform.machine() != "x86_64":
        parser.error("This offline bundle targets Ubuntu x86-64 with Python 3.10. Ask for a bundle matching this computer.")
    os.umask(0o077)
    source = source_root(__file__)
    target = Path.home() / ".local/share/codex-lan-client"
    config = target / "connection.json"
    if config.exists() and json.loads(config.read_text()) != {"server": args.server, "port": args.port}:
        parser.error("This installation is already paired to another server or port; its device identity was preserved.")
    active = subprocess.run(["systemctl", "--user", "is-active", "--quiet", "codex-lan-client.service"])
    if active.returncode == 0:
        parser.error("The client is running. Finish its prompts and stop codex-lan-client.service before reinstalling.")
    target.mkdir(parents=True, exist_ok=True, mode=0o700)
    package = target / "codex_lan"
    package.mkdir(exist_ok=True)
    for name in PACKAGE_FILES:
        shutil.copy2(source_file(source, "codex_lan/" + name), package / name)
    shutil.copy2(source_file(source, "launch_lan_client.py"), target / "launch_lan_client.py")
    for name in ("codex", "codex-code-mode-host"):
        bundled = source / "bin" / name
        if bundled.exists():
            (target / "bin").mkdir(exist_ok=True)
            shutil.copy2(bundled, target / "bin" / name)
    python = target / "venv/bin/python"
    if not python.exists():
        subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(target / "venv")], check=True)
    wheels, pip = source / "wheels", source / "pip.pyz"
    if not wheels.is_dir() or not pip.is_file():
        parser.error("Use the complete client archive, including wheels/ and pip.pyz")
    subprocess.run([str(python), str(pip), "install", "--no-index", "--find-links", str(wheels),
                    "-r", str(source_file(source, "requirements-lan.txt"))], check=True)
    config.write_text(json.dumps({"server": args.server, "port": args.port}))
    applications = Path.home() / ".local/share/applications"
    applications.mkdir(parents=True, exist_ok=True)
    launcher = applications / "codex-lan-client.desktop"
    launcher.write_text(
        "[Desktop Entry]\nType=Application\nName=Codex LAN\nComment=Private browser chat with local execution\n"
        f'Exec="{python}" "{target / "launch_lan_client.py"}"\n'
        "Icon=utilities-terminal\nTerminal=true\nCategories=Development;\n"
    )
    print("Installed Codex LAN alongside your existing app.")
    print("The next prompt is for the SERVER's SSH password, not this computer's password.")
    if not args.no_launch:
        subprocess.run([str(python), str(target / "launch_lan_client.py")], check=True)


if __name__ == "__main__":
    main()
