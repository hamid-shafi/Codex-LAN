"""CODEX: Open an outbound SSH tunnel and a persistent user-owned local worker."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        return None


def main():
    os.umask(0o077)
    root = Path(__file__).resolve().parent
    config = json.loads((root / "connection.json").read_text())
    server, port = config["server"], int(config["port"])
    control = root / "ssh-control"
    check = subprocess.run(["ssh", "-S", str(control), "-O", "check", server], capture_output=True)
    if check.returncode:
        print(f"Connecting to {server}. Only the server password is needed.")
        subprocess.run([
            "ssh", "-fNT", "-M", "-S", str(control),
            "-o", "ExitOnForwardFailure=yes", "-o", "ServerAliveInterval=15",
            "-o", "ServerAliveCountMax=3", "-L", f"127.0.0.1:{port}:127.0.0.1:8765", server,
        ], check=True)
    url = f"http://127.0.0.1:{port}"
    opener = urllib.request.build_opener(NoRedirect())
    with opener.open(url + "/api/health", timeout=5) as response:
        if not json.load(response).get("ok"):
            raise RuntimeError("The server is not ready")
    token = root / "device.token"
    worker_home = root / "worker"
    python = root / "venv/bin/python"
    codex = root / "bin/codex"
    if not codex.exists():
        codex = Path.home() / ".local/bin/codex"
    if not codex.exists():
        raise RuntimeError("The bundle does not contain Codex CLI. Ask the server administrator for the full archive.")
    arguments = [str(python), "-m", "codex_lan.worker", "--server", url, "--token-file", str(token),
                 "--state-dir", str(worker_home), "--cwd", str(Path.home()), "--codex", str(codex),
                 "--name", socket.gethostname(), "--enroll"]

    def systemd_quote(value):
        return '"' + value.replace("%", "%%").replace("\\", "\\\\").replace('"', '\\"') + '"'

    units = Path.home() / ".config/systemd/user"
    units.mkdir(parents=True, exist_ok=True)
    unit = units / "codex-lan-client.service"
    unit.write_text(
        "[Unit]\nDescription=Codex LAN local execution worker\nAfter=network.target\n\n[Service]\n"
        f"WorkingDirectory={str(root).replace('%', '%%')}\nExecStart={' '.join(systemd_quote(value).replace('$', '$$') for value in arguments)}\n"
        "Restart=on-failure\nRestartSec=5\nTimeoutStopSec=15\nKillMode=mixed\nUMask=0077\n\n[Install]\nWantedBy=default.target\n"
    )
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "--user", "start", "codex-lan-client.service"], check=True)
    for _ in range(50):
        if token.exists():
            break
        time.sleep(0.2)
    if not token.exists():
        raise RuntimeError("Enrollment did not finish. Check: journalctl --user -u codex-lan-client -n 30")
    device_token = token.read_text().strip()
    request = urllib.request.Request(url + "/api/me", headers={"Authorization": "Bearer " + device_token})
    with opener.open(request, timeout=5) as response:
        device = json.load(response)["device"]
    print("Device:", device["name"], "Status:", device["status"], "Verification:", device.get("verification_code", ""))
    print("If pending, ask the server administrator to approve this verification code.")
    webbrowser.open(url + "/#login=" + urllib.parse.quote(device_token, safe=""))
    print("The worker keeps running when the browser or this terminal is closed.")
    print("To stop this computer's worker: systemctl --user stop codex-lan-client.service")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        print("Could not start Codex LAN:", str(error), file=sys.stderr)
        input("Press Enter to close…")
        raise SystemExit(1)
