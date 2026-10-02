"""CODEX: Install an independent LAN server/host worker, preserving the desktop app."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


def quote(value):
    value = str(value)
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("Installation paths cannot contain control characters")
    return '"' + value.replace("%", "%%").replace("\\", "\\\\").replace('"', '\\"') + '"'


def service_unit(name, command, code):
    after = ("network.target codex-lan-server.service"
             if name in {"codex-lan-host-worker", "codex-lan-health-monitor"}
             else "network.target")
    server_policy = "TimeoutStartSec=300s\n" if name == "codex-lan-server" else ""
    stop_timeout = "20" if name == "codex-lan-server" else "15"
    quote(code)
    return (
        f"[Unit]\nDescription={name}\nAfter={after}\n\n[Service]\n{server_policy}"
        f"WorkingDirectory={str(code).replace('%', '%%')}\n"
        f"ExecStart={' '.join(quote(value).replace('$', '$$') for value in command)}\n"
        f"Restart=always\nRestartSec=5\nTimeoutStopSec={stop_timeout}\nKillMode=mixed\nUMask=0077\n\n"
        "[Install]\nWantedBy=default.target\n"
    )


SETUP_FILES = (
    "prepare_lan_connector.py", "publish_lan_release.py", "build_lan_bundle.py",
    "build_lan_installer.py", "install_lan_direct.sh.in", "setup_lan_direct.py",
    "setup_lan_client.py", "launch_lan_client.py", "direct_client.py",
    "requirements-lan.txt", "deploy_lan_backend.py", "deploy_lan_web.py", "backup_lan_history.py",
)
LAN_SERVICES = ("codex-lan-server.service", "codex-lan-host-worker.service",
                "codex-lan-health-monitor.service")


def resolve_codex(value=None):
    candidate = value or shutil.which("codex") or str(Path.home() / ".local/bin/codex")
    path = Path(candidate).expanduser().resolve()
    if not path.is_file() or not os.access(path, os.X_OK):
        raise ValueError("Install the official Codex CLI first, or pass --codex /path/to/codex")
    quote(path)
    return path


def prerequisites(codex=None, *, require_login=False):
    issues = []
    if sys.platform != "linux" or sys.version_info < (3, 10):
        issues.append("Linux with Python 3.10 or newer is required")
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        issues.append("Run setup as your desktop user, without sudo")
    if importlib.util.find_spec("venv") is None or importlib.util.find_spec("ensurepip") is None:
        issues.append("Install your distribution's Python venv package (Ubuntu/Debian: python3-venv)")
    if not shutil.which("systemctl"):
        issues.append("This setup requires systemd user services")
    else:
        try:
            result = subprocess.run(["systemctl", "--user", "show-environment"],
                                    capture_output=True, timeout=10)
            if result.returncode:
                issues.append("Open setup in your normal desktop login; the systemd user session is unavailable")
        except (OSError, subprocess.SubprocessError):
            issues.append("The systemd user session could not be checked")
    try:
        resolve_codex(codex)
    except ValueError as error:
        issues.append(str(error))
    if require_login:
        try:
            authentication = json.loads((Path.home() / ".codex/auth.json").read_text())
            access_token = authentication.get("tokens", {}).get("access_token")
            if not isinstance(access_token, str) or not access_token.strip():
                raise ValueError
        except (OSError, ValueError, TypeError, AttributeError):
            issues.append('Sign in on this host first: codex -c \'cli_auth_credentials_store="file"\' login')
    return issues


def private_directory(path):
    path = Path(path).absolute()
    if path.exists() and (path.is_symlink() or path.resolve() != path or not path.is_dir()):
        raise ValueError("The installation directory must not contain symbolic links")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.resolve() != path or path.stat().st_uid != os.getuid():
        raise ValueError("The installation directory must belong to the current user")
    path.chmod(0o700)
    return path


def install_setup_source(source, destination):
    source, destination = Path(source).resolve(), private_directory(destination)
    for name in SETUP_FILES:
        target = destination / name
        if target.is_symlink():
            raise ValueError("A setup helper target is a symbolic link")
        if source / name != target:
            shutil.copy2(source / name, target)
    if (destination / "codex_lan").is_symlink():
        raise ValueError("The setup package target is a symbolic link")
    shutil.copytree(source / "codex_lan", destination / "codex_lan", dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    pip = source / "pip.pyz"
    if pip.is_file():
        from prepare_lan_connector import valid_pip
        if not valid_pip(pip) or (destination / "pip.pyz").is_symlink():
            raise ValueError("The optional pip cache is not a valid regular zipapp")
        if pip != destination / "pip.pyz":
            shutil.copy2(pip, destination / "pip.pyz")
    wheels = source / ".lan-wheels"
    if wheels.exists():
        if wheels.is_symlink() or not wheels.is_dir():
            raise ValueError("The optional wheel cache is not a regular directory")
        target_wheels = private_directory(destination / ".lan-wheels")
        for wheel in wheels.glob("*.whl"):
            target = target_wheels / wheel.name
            if wheel.is_symlink() or not wheel.is_file() or target.is_symlink():
                raise ValueError("Offline wheels must be regular files")
            if wheel != target:
                shutil.copy2(wheel, target)
    return destination


def main(argv=None):
    parser = argparse.ArgumentParser(description="Install Codex LAN from a clean source checkout, as your desktop user")
    parser.add_argument("--import-desktop", action="store_true")
    parser.add_argument("--start", action="store_true")
    parser.add_argument("--open", action="store_true")
    parser.add_argument("--check", action="store_true", help="Check prerequisites without writing or starting anything")
    parser.add_argument("--codex", help="Installed official Codex executable (detected automatically)")
    parser.add_argument("--wheels", type=Path, help="Optional complete offline wheel directory; otherwise install from PyPI")
    parser.add_argument("--address", help="Optional explicit private IPv4 address for HTTPS; also configurable in Host setup")
    parser.add_argument("--https-port", type=int, default=8767)
    parser.add_argument("--keep-awake", action="store_true", help="Install a sleep inhibitor; display blanking remains enabled")
    parser.add_argument("--prepare-connector", action="store_true", help="Prepare the downloadable offline Connector (downloads dependencies)")
    args = parser.parse_args(argv)
    issues = prerequisites(args.codex, require_login=args.start)
    if issues:
        parser.error("\n".join(issues))
    if args.check:
        print("Prerequisites passed. No files, services, or settings were changed.")
        return
    codex = resolve_codex(args.codex)
    for unit in LAN_SERVICES:
        if subprocess.run(["systemctl", "--user", "is-active", "--quiet", unit]).returncode == 0:
            parser.error("Codex LAN is already running. Use its Host setup screen or the safe backend updater; do not reinstall during prompts.")
    os.umask(0o077)
    source = Path(__file__).resolve().parent
    root = Path.home() / ".local/share/codex-lan"
    code, data, worker_home = root / "code", root / "server", root / "host-worker"
    private_directory(root)
    private_directory(code)
    private_directory(data)
    private_directory(worker_home)
    python = root / "venv/bin/python"
    if not python.exists():
        subprocess.run([sys.executable, "-m", "venv", str(root / "venv")], check=True)
    elif subprocess.run([str(python), "-c", "import pip"], capture_output=True).returncode:
        subprocess.run([str(python), "-m", "ensurepip"], check=True)
    pip_command = [str(python), "-m", "pip", "--isolated", "install", "--disable-pip-version-check", "--no-input"]
    if args.wheels:
        wheels = args.wheels.expanduser().resolve()
        if not wheels.is_dir():
            parser.error("--wheels must be an existing offline wheel directory")
        pip_command += ["--no-index", "--find-links", str(wheels)]
    subprocess.run([*pip_command, "-r", str(source / "requirements-lan.txt")], check=True)
    shutil.copytree(source / "codex_lan", code / "codex_lan", dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    setup_source = install_setup_source(source, root / "setup-source")
    token = data / "admin.token"
    if not token.exists():
        subprocess.run([str(python), "-m", "codex_lan.server", "--data-dir", str(data), "--init"], cwd=code, check=True)
    if args.import_desktop:
        subprocess.run([str(python), "-m", "codex_lan.migrate", "--source",
                        str(Path.home() / ".local/share/hamsohbat-codex/hamsohbat.sqlite3"),
                        "--data-dir", str(data), "--admin-token-file", str(token),
                        "--worker-home", str(worker_home), "--sessions", str(Path.home() / ".codex/sessions")],
                       cwd=code, check=True)
    if args.address:
        from codex_lan.https_setup import prepare_network
        prepare_network(data, args.address, args.https_port)
    units = Path.home() / ".config/systemd/user"
    units.mkdir(parents=True, exist_ok=True)
    commands = {
        "codex-lan-server": [str(python), "-m", "codex_lan.server", "--data-dir", str(data)],
        "codex-lan-host-worker": [str(python), "-m", "codex_lan.worker", "--server", "http://127.0.0.1:8765",
                                  "--token-file", str(token), "--state-dir", str(worker_home),
                                  "--route-state", str(root / "egress.sqlite3"),
                                  "--cwd", str(Path.home()), "--codex", str(codex)],
        "codex-lan-health-monitor": [str(python), "-m", "codex_lan.health_monitor",
                                     "--interval", "15", "--timeout", "5",
                                     "--failure-threshold", "4", "--cooldown", "300",
                                     "--active-stall-timeout", "60"],
    }
    if args.keep_awake:
        if not shutil.which("systemd-inhibit"):
            parser.error("systemd-inhibit is required for --keep-awake")
        commands["codex-lan-stay-awake"] = [shutil.which("systemd-inhibit"), "--what=sleep:handle-suspend-key",
                                            "--who=Codex-LAN", "--why=Keep-the-shared-host-online-while-locked",
                                            "--mode=block", shutil.which("sleep") or "/usr/bin/sleep", "infinity"]
    for name, command in commands.items():
        content = service_unit(name, command, code)
        path_value = str(codex.parent) + ":" + str(Path.home() / ".local/bin") + ":" + os.defpath
        content = content.replace("[Service]\n", "[Service]\nEnvironment=" + quote("PATH=" + path_value) + "\n", 1)
        target = units / (name + ".service")
        if target.is_symlink():
            parser.error("A Codex LAN service target is a symbolic link")
        target.write_text(content)
    desktop = Path.home() / ".local/share/applications/codex-lan-host.desktop"
    desktop.parent.mkdir(parents=True, exist_ok=True)
    desktop.write_text(
        "[Desktop Entry]\nType=Application\nName=Codex LAN Admin\nComment=Central chat and device administration\n"
        f"Path={code}\nExec={quote(python)} -m codex_lan.open_browser --token-file {quote(token)}\n"
        "Icon=applications-development\nTerminal=false\nCategories=Development;\n"
    )
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    if args.prepare_connector:
        subprocess.run([str(python), str(setup_source / "prepare_lan_connector.py"),
                        "--data-dir", str(data), "--codex", str(codex)], cwd=setup_source, check=True)
    if args.start:
        subprocess.run(["systemctl", "--user", "enable", "--now", *[name + ".service" for name in commands]], check=True)
    if args.open:
        subprocess.run([str(python), "-m", "codex_lan.open_browser", "--token-file", str(token)], cwd=code, check=True)
    print("Codex LAN installed separately. Browser: http://127.0.0.1:8765")
    print("Open Administration > Host setup to configure LAN access, Connector downloads, and keep-awake.")
    print("Private runtime data lives outside the source checkout:", root)


if __name__ == "__main__":
    main()
