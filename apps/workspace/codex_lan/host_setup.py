"""CODEX: Administrator-only host setup with fixed commands and no credential responses."""

from __future__ import annotations

import asyncio
import json
import os
import platform
import shutil
import signal
import socket
import stat
import subprocess
import sys
import time
from pathlib import Path

from aiohttp import web

from . import __version__
from .network import check_file, ssl_context, validate_address, validate_port


UNITS = (
    ("codex-lan-server.service", "Chat server"),
    ("codex-lan-host-worker.service", "Host Connector"),
    ("codex-lan-health-monitor.service", "Connection recovery"),
    ("codex-lan-stay-awake.service", "Keep host awake"),
)


def command(*arguments):
    """CODEX: Read bounded system status without a shell or inherited stdin."""
    try:
        result = subprocess.run(arguments, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, text=True, timeout=8)
        return result.returncode, result.stdout[:32768]
    except (OSError, subprocess.SubprocessError):
        return 127, ""


def lan_addresses():
    code, output = command("ip", "-j", "-4", "address", "show", "up")
    addresses = set()
    if code == 0:
        try:
            for interface in json.loads(output):
                for item in interface.get("addr_info", []):
                    if item.get("family") == "inet":
                        try:
                            addresses.add(validate_address(item.get("local")))
                        except ValueError:
                            pass
        except (ValueError, TypeError, AttributeError):
            pass
    return sorted(addresses)


def system_status():
    services = []
    for name, label in UNITS:
        code, output = command("systemctl", "--user", "show", name, "--no-pager",
                               "--property=LoadState", "--property=ActiveState", "--property=UnitFileState")
        fields = dict(line.split("=", 1) for line in output.splitlines() if "=" in line)
        services.append({"id": name, "label": label,
                         "available": code == 0 and fields.get("LoadState") == "loaded",
                         "active": fields.get("ActiveState") == "active",
                         "enabled": fields.get("UnitFileState") in {"enabled", "enabled-runtime"}})
    return {"services": services, "lan_addresses": lan_addresses()}


def private_unit(path, content):
    """CODEX: Replace only a regular current-user unit using a private temporary file."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.parent.resolve() != path.parent.absolute() or path.parent.stat().st_uid != os.getuid():
        raise ValueError("The power service directory must belong to the host user without symbolic links.")
    if path.is_symlink() or path.exists() and (not path.is_file() or path.stat().st_uid != os.getuid()):
        raise ValueError("The power service file must belong to the host user.")
    temporary = path.with_name(path.name + ".setup-new")
    descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class HostSetup:
    def __init__(self, service):
        self.service = service
        self.runner = None
        self.https_site = None
        self.loopback_port = 8765
        self.task = None
        self.job = None
        self.cached = None
        self.cached_at = 0
        self.status_lock = asyncio.Lock()
        self.action_lock = asyncio.Lock()

    @property
    def supported(self):
        return sys.platform.startswith("linux") and shutil.which("systemctl") is not None

    @property
    def preparation_script(self):
        return self.service.data_dir.parent / "setup-source/prepare_lan_connector.py"

    def helper_available(self):
        path = self.preparation_script
        try:
            return (path.is_file() and not path.is_symlink() and path.stat().st_uid == os.getuid()
                    and not stat.S_IMODE(path.stat().st_mode) & 0o022
                    and path.parent.resolve() == path.parent.absolute())
        except OSError:
            return False

    async def status(self):
        async with self.status_lock:
            if self.cached is None or time.monotonic() - self.cached_at > 5:
                self.cached = await asyncio.to_thread(system_status)
                self.cached_at = time.monotonic()
        system = self.cached
        network = self.service.network
        fingerprint = None
        if network:
            from .https_setup import ca_fingerprint
            try:
                fingerprint = await asyncio.to_thread(ca_fingerprint, network["ca"])
            except (OSError, ValueError):
                pass
        release = {}
        try:
            release = json.loads((self.service.data_dir / "releases/release.json").read_text())
        except (OSError, ValueError):
            pass
        if not isinstance(release, dict):
            release = {}
        codex = await asyncio.to_thread(self.codex_status)
        with self.service.store._lock:
            active = self.service.store.db.execute("SELECT count(*) FROM runs WHERE status IN ('queued','running')").fetchone()[0]
        busy = self.task is not None and not self.task.done()
        actions = [
            ("enable_startup", "Enable automatic startup", "Start the host services when you sign in.", self.supported),
            ("keep_awake_on", "Keep host awake", "Prevent system sleep while allowing display-off and screen lock.", self.supported and bool(shutil.which("systemd-inhibit"))),
            ("keep_awake_off", "Allow system sleep", "Connections stop while the host is asleep.", self.supported),
            ("configure_https", "Set up LAN HTTPS", "Choose a private address on this computer and share its CA certificate.", self.runner is not None and bool(shutil.which("openssl"))),
            ("prepare_connector", "Prepare Connector download", "Build a credential-free installer for Linux clients; internet is needed for dependencies.", self.helper_available()),
        ]
        notices = ["Only trusted people should be approved: their requests can run tools on their connected computers.",
                   "Display-off and screen lock are allowed. System sleep interrupts network access.",
                   "Startup here runs after host sign-in. Running after logout or before sign-in may require Linux linger permission.",
                   "Use your LAN HTTPS address on other computers and verify the CA fingerprint before trusting it."]
        if not self.helper_available():
            notices.append("Connector build tools are missing. Run the repository's setup entrypoint on this host once.")
        return {"supported": self.supported, "version": __version__,
                "host": {"platform": platform.system(), "hostname": socket.gethostname()},
                "network": {"listen": "127.0.0.1", "port": self.loopback_port,
                            "local_origin": f"http://127.0.0.1:{self.loopback_port}",
                            "public_origin": network["public_origin"] if network else None,
                            "https_enabled": self.https_site is not None,
                            "port_https": network["port"] if network else 8767,
                            "lan_addresses": system["lan_addresses"], "ca_fingerprint": fingerprint},
                "services": system["services"],
                "keep_awake": {"active": next(item["active"] for item in system["services"] if item["id"] == "codex-lan-stay-awake.service"),
                               "supported": self.supported and bool(shutil.which("systemd-inhibit"))},
                "connector": {"available": bool(release.get("version") and (self.service.data_dir / "releases/client.tar.gz").is_file()),
                              "version": str(release.get("version", ""))[:40]},
                "codex": codex, "active_runs": active, "job": self.job,
                "actions": [{"id": key, "label": label, "description": description,
                             "enabled": enabled and not busy,
                             "reason": "Another setup operation is running." if busy else "Prerequisites are missing." if not enabled else ""}
                            for key, label, description, enabled in actions], "notices": notices}

    def codex_status(self):
        auth = self.service.runtime.auth
        path = Path(getattr(auth, "codex", None) or shutil.which("codex") or Path.home() / ".local/bin/codex")
        available = path.is_file() and os.access(path, os.X_OK)
        try:
            tokens, _ = auth._read()
            signed_in = bool(tokens.get("accessToken"))
        except (AttributeError, RuntimeError):
            signed_in = False
        return {"available": available, "signed_in": signed_in}

    async def start_network_site(self, config):
        if self.runner is None:
            raise ValueError("The host listener is not available in this server mode.")
        if self.https_site is not None:
            return
        context = await asyncio.to_thread(ssl_context, config)
        site = web.TCPSite(self.runner, config["lan_address"], config["port"], ssl_context=context)
        await site.start()
        self.https_site = site
        self.service.network = config

    async def execute(self, payload):
        async with self.action_lock:
            return await self._execute(payload)

    async def _execute(self, payload):
        action = payload.get("action")
        if action not in {"enable_startup", "keep_awake_on", "keep_awake_off", "configure_https", "prepare_connector"}:
            raise ValueError("Unsupported host setup action.")
        allowed = {"action", "address", "port"} if action == "configure_https" else {"action"}
        if set(payload) - allowed:
            raise ValueError("Unexpected host setup fields.")
        if self.task is not None and not self.task.done():
            raise web.HTTPConflict(text="Another host setup operation is running.")
        status = await self.status()
        if not next(item["enabled"] for item in status["actions"] if item["id"] == action):
            raise ValueError("This setup action is unavailable on this host.")
        if action == "configure_https":
            address, port = validate_address(payload.get("address")), validate_port(payload.get("port", 8767))
            if address not in await asyncio.to_thread(lan_addresses):
                raise ValueError("Choose a private address currently assigned to this host.")
            payload = {"action": action, "address": address, "port": port}
        self.job = {"action": action, "status": "running", "message": "Host setup is running."}
        self.task = asyncio.create_task(self._perform(payload), name="codex-lan-host-setup")
        return {"message": "Host setup started. Its status will appear here.", "status": "running"}

    async def _perform(self, payload):
        action = payload["action"]
        try:
            if action == "configure_https":
                from .https_setup import prepare_network
                filename = self.service.data_dir / "network.json"
                existed = filename.exists() or filename.is_symlink()
                config = await asyncio.to_thread(prepare_network, self.service.data_dir, payload["address"], payload["port"])
                created = None if existed else (filename.stat(), filename.read_bytes())
                try:
                    await self.start_network_site(config)
                except BaseException:
                    if created is not None:
                        check_file(filename, private=True)
                        metadata, content = created
                        current = filename.stat()
                        if (current.st_dev, current.st_ino, current.st_mtime_ns) == (metadata.st_dev, metadata.st_ino, metadata.st_mtime_ns) and filename.read_bytes() == content:
                            filename.unlink()
                    raise
                message = "LAN HTTPS is ready. Download the CA certificate and verify its fingerprint on each client."
            elif action == "prepare_connector":
                if not self.helper_available():
                    raise ValueError("The installed Connector build helper is unavailable.")
                process = await asyncio.create_subprocess_exec(
                    sys.executable, str(self.preparation_script), "--data-dir", str(self.service.data_dir),
                    stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL, start_new_session=True)
                try:
                    code = await asyncio.wait_for(process.wait(), timeout=900)
                except BaseException:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    await process.wait()
                    raise
                if code:
                    raise ValueError("Connector preparation failed. Check host internet access and Python prerequisites, then retry.")
                message = "The credential-free Connector installer is ready for approved clients."
            else:
                if action == "keep_awake_on":
                    inhibitor, sleeper = shutil.which("systemd-inhibit"), shutil.which("sleep")
                    if not inhibitor or not sleeper:
                        raise ValueError("The host power inhibitor is unavailable.")
                    unit = ("[Unit]\nDescription=Keep Codex LAN awake\nAfter=graphical-session.target\n\n"
                            "[Service]\nType=simple\n" +
                            f"ExecStart={inhibitor} --what=sleep:handle-suspend-key --who=Codex-LAN --why=Keep-host-online --mode=block {sleeper} infinity\n" +
                            "Restart=always\nRestartSec=3\n\n[Install]\nWantedBy=default.target\n")
                    await asyncio.to_thread(private_unit, Path.home() / ".config/systemd/user/codex-lan-stay-awake.service", unit)
                    commands = [("systemctl", "--user", "daemon-reload"),
                                ("systemctl", "--user", "enable", "--now", "codex-lan-stay-awake.service")]
                elif action == "keep_awake_off":
                    commands = [("systemctl", "--user", "disable", "--now", "codex-lan-stay-awake.service")]
                else:
                    commands = [("systemctl", "--user", "enable", *[item[0] for item in UNITS[:3]])]
                for arguments in commands:
                    code, _ = await asyncio.to_thread(command, *arguments)
                    if code:
                        raise ValueError("Linux could not apply this setting. Check that your user systemd session is available.")
                message = "The host setting was applied."
            self.job = {"action": action, "status": "completed", "message": message}
        except asyncio.CancelledError:
            self.job = {"action": action, "status": "interrupted", "message": "Host setup was interrupted; review the setting before retrying."}
            raise
        except asyncio.TimeoutError:
            self.job = {"action": action, "status": "failed", "message": "Connector preparation timed out. Check host internet access, then retry."}
        except (OSError, ValueError, RuntimeError) as error:
            self.job = {"action": action, "status": "failed", "message": str(error)[:240] if isinstance(error, ValueError) else "Host setup failed. Check the selected address and host prerequisites."}
        finally:
            self.cached = None

    def ca_response(self):
        network = self.service.network
        if not network:
            raise web.HTTPNotFound(text="LAN HTTPS has not been configured.")
        check_file(network["ca"])
        return web.FileResponse(network["ca"], headers={"Content-Type": "application/x-x509-ca-cert",
                                                      "Content-Disposition": 'attachment; filename="Codex-LAN-CA.crt"'})

    async def close(self):
        if self.task is not None and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
