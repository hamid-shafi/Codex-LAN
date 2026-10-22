"""CODEX: Explicit-consent installation of a private, per-user HTTPS Codex connector."""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

_layout_root = next((parent for parent in Path(__file__).resolve().parents
                     if (parent / "project_layout.py").is_file()), None)
if _layout_root is not None:
    sys.path.insert(0, str(_layout_root))
    from project_layout import configure_imports, source_root
    configure_imports(__file__)
else:
    # A flat client archive is also supported by previously installed updaters.
    def source_root(path):
        return Path(path).resolve().parent

from direct_client import Connector, ConnectorError, DeviceAuthorizationError, ROOT, Transport, exclusive, gui_available, install_release, origin, payload_version, private_write, service, switch_release, validate_runtime


def load_pairing(path):
    raw = Path(path).read_bytes()
    if len(raw) > 32768:
        raise ConnectorError("Pairing file is too large")
    value = json.loads(raw)
    if not isinstance(value, dict) or value.get("format") != 1:
        raise ConnectorError("Unsupported pairing file format")
    value["server"] = origin(value.get("server"))
    expiry = value.get("expires_at")
    if (isinstance(expiry, bool) or not isinstance(expiry, (int, float)) or not time.time() < expiry <= time.time() + 660):
        raise ConnectorError("Pairing file expired. Download a fresh file from this computer's approved browser session.")
    code = value.get("pairing_code")
    if not isinstance(code, str) or not 32 <= len(code) <= 128:
        raise ConnectorError("Invalid pairing code")
    certificate = value.get("ca_certificate")
    if not isinstance(certificate, str) or "-----BEGIN CERTIFICATE-----" not in certificate:
        raise ConnectorError("Pairing file has no server certificate")
    connector_version = value.get("connector_version")
    if not isinstance(connector_version, str) or not re.fullmatch(r"\d{1,4}\.\d{1,4}\.\d{1,4}", connector_version):
        raise ConnectorError("Pairing file has no valid connector release tag. Download a fresh setup file.")
    Transport(value["server"], certificate)
    return value


def verify_pairing_release(pairing, payload):
    expected = pairing["connector_version"]
    if payload != expected:
        raise ConnectorError(
            f"This pairing file is for Connector {expected}, but this installer contains {payload}. Download both files again."
        )


def consent(server):
    text = ("Allow Codex Connector to read and edit files and run commands as your Ubuntu user on THIS computer?\n\n"
            "It connects only to " + server + ". It stays running when Chrome closes.\n"
            "Codex and its OpenAI connection run on that server; this computer runs file and terminal tools only.\n"
            "You can pause local access from the Codex Connector launcher.\n\n"
            "Use the pairing file downloaded from THIS computer's approved browser session, not the administrator's session.")
    if gui_available():
        return subprocess.run(["zenity", "--question", "--title=Allow local Codex access?", "--width=560", "--text=" + text]).returncode == 0
    if not sys.stdin.isatty():
        raise ConnectorError("Open this installer in your desktop or terminal to confirm local file access")
    print(text)
    return input("Type yes to allow local access: ").strip().lower() == "yes"


def quote(value):
    value = str(value)
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ConnectorError("Installation paths cannot contain control characters")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$") + '"'


def working_directory(value):
    """CODEX: WorkingDirectory is a literal path directive, not an ExecStart argument."""
    value = str(value)
    if not Path(value).is_absolute() or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ConnectorError("The working directory must be an absolute path without control characters")
    return value.replace("%", "%%")


def user_service_help():
    message = "Codex Connector needs a working systemd user session. Run it as your normal Linux user, without sudo."
    if "microsoft" in platform.release().lower() or os.environ.get("WSL_DISTRO_NAME"):
        message += (" In WSL, use Ubuntu on WSL 2 with systemd enabled in /etc/wsl.conf."
                    " Save your work before restarting the distro, then verify: systemctl --user show-environment."
                    " Keep the WSL SSH tunnel terminal open while using the Connector."
                    " See https://learn.microsoft.com/en-us/windows/wsl/systemd")
    return message


def require_user_service(runner=subprocess.run):
    """Check the user manager before claiming pairing or changing an installation."""
    try:
        result = runner(["systemctl", "--user", "show-environment"], check=True,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
        if result.returncode:
            raise ConnectorError(user_service_help())
    except (OSError, subprocess.SubprocessError):
        raise ConnectorError(user_service_help()) from None


def service_unit(root, server, home):
    root, home = Path(root), Path(home)
    command = ["/usr/bin/env", "--", str(root / "current/venv/bin/python"), "-m", "codex_lan.worker", "--server", server,
               "--server-ca", str(root / "server-ca.pem"),
               "--token-file", str(root / "device.token"), "--state-dir", str(root / "state"),
               "--route-state", str(root / "egress.sqlite3"),
               "--cwd", str(home), "--codex", str(root / "current/bin/codex")]
    return ("[Unit]\nDescription=Codex direct local connector\nAfter=network-online.target\n\n[Service]\n"
            "Type=simple\nWorkingDirectory=" + working_directory(root / "current") + "\nExecStart=" + " ".join(quote(value) for value in command) + "\n"
            "Restart=on-failure\nRestartSec=5\nTimeoutStopSec=15\nKillMode=mixed\nUMask=0077\n\n[Install]\nWantedBy=default.target\n")


def reject_duplicate_host_identity(token):
    host_token = Path.home() / ".local/share/codex-lan/server/admin.token"
    if host_token.is_file() and host_token.read_bytes().strip() == token.strip():
        raise ConnectorError("This server already uses its host connector. A second local connector is not needed.")


def claim_pairing(pairing, transport_class=Transport):
    transport = transport_class(pairing["server"], pairing["ca_certificate"])
    result = transport.json("/api/client/claim", {"pairing_code": pairing["pairing_code"]})
    token, device = result.get("token"), result.get("device")
    if (not isinstance(token, str) or not 20 <= len(token) <= 4096 or any(char.isspace() for char in token)
            or not isinstance(device, dict) or device.get("status") != "approved" or not device.get("id")):
        raise ConnectorError("Server did not return an approved device identity")
    reject_duplicate_host_identity(token.encode("utf-8"))
    return token, device


def desktop_entry(root):
    root = Path(root)
    return ("[Desktop Entry]\nType=Application\nName=Codex Connector\nComment=Control local access and updates\n"
            "Exec=" + quote(root / "current/venv/bin/python") + " " + quote(root / "current/direct_client.py") + " --root " + quote(root) + "\n"
            "Icon=applications-development\nTerminal=false\nCategories=Development;\n")


def install(source, pair_file, root=ROOT, no_launch=False, runner=subprocess.run, allow=consent, transport_class=Transport):
    validate_runtime()
    root, source = Path(root).absolute(), Path(source)
    if (root / "config.json").exists() or (root / "device.token").exists():
        raise ConnectorError("This computer is already paired. Use Codex Connector to resume or update it.")
    pairing = load_pairing(pair_file)
    version = payload_version(source)
    verify_pairing_release(pairing, version)
    unit = service_unit(root, pairing["server"], Path.home())
    if not allow(pairing["server"]):
        raise ConnectorError("Local access was not allowed. No pairing code was claimed.")
    require_user_service(runner)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.chmod(0o700)
    release = install_release(source, root, version, runner)
    token, device = claim_pairing(pairing, transport_class)
    private_write(root / "server-ca.pem", pairing["ca_certificate"])
    private_write(root / "device.token", token)
    private_write(root / "config.json", json.dumps({"format": 1, "server": pairing["server"],
                  "device_id": device["id"], "device_name": device.get("name", "Ubuntu computer")}, indent=2))
    switch_release(root, release)
    units = Path.home() / ".config/systemd/user"
    private_write(units / "codex-lan-direct.service", unit)
    private_write(Path.home() / ".local/share/applications/codex-connector.desktop", desktop_entry(root))
    runner(["systemctl", "--user", "daemon-reload"], check=True)
    service("enable", runner)
    if not no_launch:
        service("start", runner)
    return {"version": version, "device_id": device["id"], "server": pairing["server"]}


def update_existing(root, runner=subprocess.run, connector_class=Connector):
    """CODEX: Bootstrap an old updater without replacing its approved device identity."""
    validate_runtime()
    require_user_service(runner)
    root = Path(root).absolute()
    reject_duplicate_host_identity((root / "device.token").read_bytes())
    connector = connector_class(root)
    private_write(Path.home() / ".config/systemd/user/codex-lan-direct.service", service_unit(root, connector.server, Path.home()))
    runner(["systemctl", "--user", "daemon-reload"], check=True)
    return connector.update()


def repair_pairing(source, pair_file, root=ROOT, no_launch=False, runner=subprocess.run,
                   allow=consent, transport_class=Transport, connector_class=Connector):
    """CODEX: Atomically attach an existing installation to the browser identity in an explicit pairing file."""
    validate_runtime()
    root, source = Path(root).absolute(), Path(source)
    required = [root / "config.json", root / "device.token", root / "server-ca.pem", root / "current"]
    if not all(path.exists() for path in required):
        raise ConnectorError("The existing connector installation is incomplete; its identity was not changed.")
    was_paused = (root / "paused").exists()
    if no_launch and not was_paused:
        raise ConnectorError("Pause the existing connector before repairing it with --no-launch.")
    pairing = load_pairing(pair_file)
    version = payload_version(source)
    verify_pairing_release(pairing, version)
    old_connector = connector_class(root)
    if old_connector.server != pairing["server"]:
        raise ConnectorError("This pairing file belongs to a different server. Remove the old connector explicitly before migrating servers.")
    if not allow(pairing["server"]):
        raise ConnectorError("Local access was not allowed. No pairing code was claimed.")
    require_user_service(runner)
    current = (root / "current").resolve()
    prepared = current if payload_version(current) == version else install_release(source, root, version, runner)
    unit_path = Path.home() / ".config/systemd/user/codex-lan-direct.service"
    desktop_path = Path.home() / ".local/share/applications/codex-connector.desktop"
    saved = {path: path.read_bytes() if path.is_file() else None for path in (
        root / "server-ca.pem", root / "device.token", root / "config.json", unit_path, desktop_path, root / "paused"
    )}
    status = service("is-active", runner, check=False)
    was_running = getattr(status, "returncode", 1) == 0
    old_maintenance = False
    stopped = False
    try:
        try:
            old_maintenance = True
            old_connector.wait_idle()
        except DeviceAuthorizationError:
            # CODEX: A deleted identity cannot drain; a network error must never bypass active-work checks.
            old_maintenance = False
        token, device = claim_pairing(pairing, transport_class)
        service("stop", runner)
        stopped = True
        private_write(root / "server-ca.pem", pairing["ca_certificate"])
        private_write(root / "device.token", token)
        private_write(root / "config.json", json.dumps({"format": 1, "server": pairing["server"],
                      "device_id": device["id"], "device_name": device.get("name", "Ubuntu computer")}, indent=2))
        switch_release(root, prepared)
        private_write(unit_path, service_unit(root, pairing["server"], Path.home()))
        private_write(desktop_path, desktop_entry(root))
        runner(["systemctl", "--user", "daemon-reload"], check=True)
        if old_maintenance:
            old_connector.unpause()
            old_maintenance = False
        if was_paused:
            private_write(root / "paused", "1")
            service("disable", runner)
        else:
            (root / "paused").unlink(missing_ok=True)
            service("enable", runner)
            service("start", runner)
            verified_transport = transport_class(pairing["server"], pairing["ca_certificate"], token)
            connector_class(root, verified_transport, runner).wait_ready(version)
        return {"version": version, "device_id": device["id"], "server": pairing["server"]}
    except BaseException:
        if stopped:
            service("stop", runner, check=False)
            for path, data in saved.items():
                if data is None:
                    path.unlink(missing_ok=True)
                else:
                    private_write(path, data)
            switch_release(root, current)
            runner(["systemctl", "--user", "daemon-reload"], check=False)
            if was_running and not was_paused:
                service("enable", runner, check=False)
                service("start", runner, check=False)
        raise
    finally:
        if old_maintenance:
            old_connector.unpause()


def main():
    parser = argparse.ArgumentParser(description="Install a local Ubuntu connector without SSH or sudo")
    parser.add_argument("--pair-file", type=Path)
    parser.add_argument("--auto-pair-file", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--no-launch", action="store_true")
    args = parser.parse_args()
    if os.geteuid() == 0:
        parser.error("Run as your Ubuntu desktop user, not with sudo")
    try:
        validate_runtime()
    except ConnectorError as exc:
        parser.error(str(exc))
    if not shutil.which("systemctl"):
        parser.error(user_service_help())
    pair_file = args.pair_file
    already_paired = (args.root / "config.json").is_file() and (args.root / "device.token").is_file()
    if pair_file is None and not already_paired:
        pair_file = args.auto_pair_file
    if pair_file is None and not already_paired:
        if not gui_available():
            parser.error("First setup needs pairing. In the browser open Connector → Manual setup / repair → Download pairing file, or download the one-step private setup. Updates of an already paired connector do not need a new pairing file.")
        selected = subprocess.run(["zenity", "--file-selection", "--title=Select codex-pairing.json", "--file-filter=JSON files | *.json"], capture_output=True, text=True)
        if selected.returncode:
            return
        pair_file = Path(selected.stdout.strip())
    try:
        with exclusive(args.root):
            if already_paired:
                if pair_file is not None:
                    result = repair_pairing(source_root(__file__), pair_file, args.root, args.no_launch)
                    text = "Codex Connector repaired and attached to this browser identity."
                    text += "\nServer: " + result["server"]
                else:
                    if args.no_launch and not (args.root / "paused").exists():
                        raise ConnectorError("Pause the existing connector before updating with --no-launch.")
                    text = update_existing(args.root)
            else:
                result = install(source_root(__file__), pair_file, args.root, args.no_launch)
                text = "Codex Connector installed. Use its launcher to open chat, pause local access, or install updates."
                text += "\nServer: " + result["server"]
        print(text)
        if gui_available():
            subprocess.run(["zenity", "--info", "--title=Codex Connector", "--text=" + text])
    except (ConnectorError, OSError, ValueError, subprocess.SubprocessError) as exc:
        print(str(exc), file=sys.stderr)
        if gui_available():
            subprocess.run(["zenity", "--error", "--title=Codex Connector", "--text=" + str(exc)])
        raise SystemExit(1)


if __name__ == "__main__":
    main()
