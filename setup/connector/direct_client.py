"""CODEX: Per-user HTTPS connector controls and verified, idle-only offline updates."""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import ipaddress
import json
import os
import platform
import re
import shutil
import ssl
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid
import webbrowser
from pathlib import Path, PurePosixPath
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPSHandler, HTTPRedirectHandler, ProxyHandler, Request, build_opener

_layout_root = next((parent for parent in Path(__file__).resolve().parents
                     if (parent / "project_layout.py").is_file()), None)
if _layout_root is not None:
    sys.path.insert(0, str(_layout_root))
    from project_layout import configure_imports, source_file
    configure_imports(__file__)
else:
    def source_file(root, name):
        return Path(root) / name

UNIT = "codex-lan-direct.service"
MAX_DOWNLOAD = 300 * 1024 * 1024
MAX_UNPACKED = 650 * 1024 * 1024
ARCHIVE_ROOT = "Codex-LAN-Client"
ROOT = Path.home() / ".local/share/codex-lan-direct"
REQUIRED = {"setup_lan_direct.py", "direct_client.py", "requirements-lan.txt", "pip.pyz",
            "codex_lan/__init__.py", "codex_lan/worker.py", "codex_lan/media.py", "codex_lan/egress.py", "codex_lan/recovery.py",
            "codex_lan/transport.py", "codex_lan/model_registry.py",
            "bin/codex", "bin/codex-code-mode-host"}
OPTIONAL = {"setup_lan_client.py", "launch_lan_client.py"}


class ConnectorError(RuntimeError):
    pass


class DeviceAuthorizationError(ConnectorError):
    pass


class Cancelled(ConnectorError):
    pass


def validate_runtime(version=None, system=None, machine=None, implementation=None, abi=None):
    version = tuple(sys.version_info[:2] if version is None else version)
    system = sys.platform if system is None else system
    machine = platform.machine() if machine is None else machine
    implementation = sys.implementation.name if implementation is None else implementation
    abi = getattr(sys, "abiflags", "") if abi is None else abi
    if system != "linux" or machine.lower() not in {"x86_64", "amd64"}:
        raise ConnectorError("This connector supports Ubuntu/Linux on x86-64 computers. On Windows, use Ubuntu on WSL 2, not PowerShell or Git Bash. Other architectures need a matching installer.")
    if implementation != "cpython" or not (3, 10) <= version <= (3, 14) or "t" in abi:
        raise ConnectorError("This offline connector supports standard CPython 3.10 through 3.14. Use Ubuntu's regular Python interpreter; free-threaded builds need a different dependency package.")
    return version


def origin(value):
    if not isinstance(value, str) or len(value) > 500:
        raise ConnectorError("Invalid server address")
    parsed = urlsplit(value)
    try:
        address = ipaddress.ip_address(parsed.hostname or "")
        port = parsed.port
    except ValueError as exc:
        raise ConnectorError("Use the server's private HTTPS IP address") from exc
    if (parsed.scheme != "https" or not address.is_private or address.is_unspecified
            or parsed.username is not None or parsed.password is not None
            or parsed.path not in {"", "/"} or parsed.query or parsed.fragment
            or port is not None and not 1 <= port <= 65535):
        raise ConnectorError("Use a private HTTPS server address without credentials or extra paths")
    return value.rstrip("/")


def version_tuple(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{1,4}\.\d{1,4}\.\d{1,4}", value):
        raise ConnectorError("Unsupported connector version format")
    return tuple(int(part) for part in value.split("."))


def payload_version(source):
    text = source_file(source, "codex_lan/__init__.py").read_text()
    match = re.search(r"^__version__\s*=\s*['\"]([\d.]+)['\"]", text, re.M)
    if not match:
        raise ConnectorError("The connector package has no supported version")
    version_tuple(match[1])
    return match[1]


def private_write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, name = tempfile.mkstemp(prefix=".connector-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(data.encode() if isinstance(data, str) else data)
            output.flush()
            os.fsync(output.fileno())
        temporary.chmod(0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        raise ConnectorError("The server redirected a sensitive request; connection stopped")


class Transport:
    def __init__(self, server, ca_certificate, token=None):
        self.server = origin(server)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.load_verify_locations(cadata=ca_certificate)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        # CODEX: Pairing and release traffic stays on the LAN regardless of client proxy variables.
        self.opener = build_opener(ProxyHandler({}), NoRedirect(), HTTPSHandler(context=context))
        self.token = token

    def request(self, path, data=None):
        if path not in {"/api/me", "/api/client/claim", "/api/client/release", "/api/client/maintenance", "/api/client/download/archive", "/api/client/download/installer"}:
            raise ConnectorError("Unsupported connector endpoint")
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        raw = json.dumps(data).encode() if data is not None else None
        if raw is not None:
            headers["Content-Type"] = "application/json"
        try:
            return self.opener.open(Request(self.server + path, data=raw, headers=headers), timeout=30)
        except (HTTPError, URLError, OSError) as exc:
            code = getattr(exc, "code", None)
            if code in {401, 403}:
                if path == "/api/client/claim":
                    raise ConnectorError("Pairing was rejected or expired. Download a fresh pairing file from this computer's approved browser session, then rerun the installer with --pair-file.") from None
                raise DeviceAuthorizationError("This connector's old device access is no longer valid. Download a fresh pairing file from your approved browser session and run the latest installer with --pair-file to reconnect it.") from None
            raise ConnectorError(f"Server request failed{f' (HTTP {code})' if code else ''}. Check the certificate, connection and device approval.") from None

    def json(self, path, data=None):
        with self.request(path, data) as response:
            payload = response.read(1024 * 1024 + 1)
        if len(payload) > 1024 * 1024:
            raise ConnectorError("Server response is too large")
        try:
            value = json.loads(payload)
        except ValueError as exc:
            raise ConnectorError("Server response is not valid JSON") from exc
        if not isinstance(value, dict):
            raise ConnectorError("Server response must be an object")
        return value

    def download(self, target, expected_size, expected_digest):
        if (isinstance(expected_size, bool) or not isinstance(expected_size, int) or not 0 < expected_size <= MAX_DOWNLOAD
                or not isinstance(expected_digest, str) or not re.fullmatch(r"[a-fA-F0-9]{64}", expected_digest)):
            raise ConnectorError("Invalid release size or checksum")
        total, digest = 0, hashlib.sha256()
        with self.request("/api/client/download/archive") as response, Path(target).open("xb") as output:
            while chunk := response.read(65536):
                total += len(chunk)
                if total > expected_size or total > MAX_DOWNLOAD:
                    raise ConnectorError("Downloaded package exceeds its declared size")
                digest.update(chunk)
                output.write(chunk)
        if total != expected_size or digest.hexdigest() != expected_digest.lower():
            raise ConnectorError("Package checksum or size did not match; nothing was activated")


def extract_release(archive_path, destination):
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=False, mode=0o700)
    total, seen = 0, set()
    with tarfile.open(archive_path, "r:gz") as archive:
        for index, member in enumerate(archive):
            path = PurePosixPath(member.name)
            if (index > 2000 or not member.isfile() or path.is_absolute() or ".." in path.parts
                    or "\\" in member.name or len(path.parts) < 2 or path.parts[0] != ARCHIVE_ROOT):
                raise ConnectorError("Package contains an unsafe archive entry")
            relative = path.relative_to(ARCHIVE_ROOT).as_posix()
            if relative in seen or relative not in REQUIRED | OPTIONAL and not re.fullmatch(r"wheels/[A-Za-z0-9_.+-]+\.whl", relative):
                raise ConnectorError("Package contains an unexpected or duplicate file")
            seen.add(relative)
            total += member.size
            if member.size < 0 or total > MAX_UNPACKED:
                raise ConnectorError("Unpacked package exceeds the safety limit")
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            source = archive.extractfile(member)
            if source is None:
                raise ConnectorError("Archive file is missing")
            with source, target.open("xb") as output:
                shutil.copyfileobj(source, output, length=65536)
            target.chmod(0o700 if relative.startswith("bin/") else 0o600)
    if not REQUIRED <= seen or not any(name.startswith("wheels/") for name in seen):
        raise ConnectorError("The connector package is incomplete")
    return destination


def install_release(source, root, version, runner=subprocess.run):
    python_version = validate_runtime()
    source, root = Path(source), Path(root)
    version_tuple(version)
    if payload_version(source) != version or not all(source_file(source, name).is_file() for name in REQUIRED):
        raise ConnectorError("Release contents do not match their declared version")
    destination = root / "releases" / version
    if destination.exists():
        if (destination / ".ready").is_file() and payload_version(destination) == version:
            return destination
        raise ConnectorError("An incomplete release already exists; existing data was preserved")
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    staging = Path(tempfile.mkdtemp(prefix=".prepare-", dir=destination.parent))
    try:
        for name in REQUIRED | OPTIONAL:
            incoming = source_file(source, name)
            if incoming.is_file():
                target = staging / name
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                shutil.copy2(incoming, target)
        shutil.copytree(source / "wheels", staging / "wheels")
        for name in ("bin/codex", "bin/codex-code-mode-host"):
            (staging / name).chmod(0o700)
        minor = ".".join(map(str, python_version))
        try:
            runner([sys.executable, "-m", "venv", "--without-pip", str(staging / "venv")],
                   check=True, capture_output=True, text=True)
        except (OSError, subprocess.SubprocessError):
            raise ConnectorError(f"Could not create a private Python {minor} environment. Check free disk space and ask your Ubuntu administrator to enable python{minor}-venv, then rerun this installer.") from None
        try:
            runner([str(staging / "venv/bin/python"), str(staging / "pip.pyz"), "--isolated", "install", "--no-index", "--no-input", "--only-binary=:all:",
                    "--find-links", str(staging / "wheels"), "--disable-pip-version-check", "-r", str(staging / "requirements-lan.txt")],
                   check=True, capture_output=True, text=True)
        except (OSError, subprocess.SubprocessError):
            raise ConnectorError(f"The bundled dependencies could not be installed for Python {minor}. Check free disk space and download the latest complete connector installer for this computer.") from None
        try:
            runner([str(staging / "venv/bin/python"), "-c", "import aiohttp; import codex_lan.worker"],
                   cwd=staging, check=True, capture_output=True, text=True)
        except (OSError, subprocess.SubprocessError):
            raise ConnectorError(f"The connector did not pass its Python {minor} compatibility check. Download the latest installer; the existing connector was not switched.") from None
        private_write(staging / ".ready", version)
        os.replace(staging, destination)
        return destination
    except BaseException:
        # CODEX: This directory was created solely for this unactivated installation attempt.
        shutil.rmtree(staging)
        raise


def switch_release(root, release):
    root, release = Path(root), Path(release)
    if release.parent.resolve() != (root / "releases").resolve():
        raise ConnectorError("Release must belong to this connector installation")
    current = root / "current"
    if current.exists() and not current.is_symlink():
        raise ConnectorError("The current installation path is not a managed release link")
    temporary = root / (".current-" + uuid.uuid4().hex)
    try:
        temporary.symlink_to(release.relative_to(root))
        os.replace(temporary, current)
    finally:
        temporary.unlink(missing_ok=True)


def service(action, runner=subprocess.run, check=True):
    return runner(["systemctl", "--user", action, UNIT], check=check)


@contextlib.contextmanager
def exclusive(root):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (root / ".operation.lock").open("a") as lock:
        os.chmod(lock.name, 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ConnectorError("Another connector action is already in progress") from exc
        yield


def gui_available():
    return bool(os.environ.get("DISPLAY") and shutil.which("zenity"))


class Progress:
    def __init__(self):
        self.process = None

    def __enter__(self):
        if gui_available():
            self.process = subprocess.Popen(["zenity", "--progress", "--pulsate", "--title=Codex Connector",
                "--text=Waiting for active work to finish. Cancel keeps the connector running."], stdin=subprocess.PIPE, text=True)
        return self

    def check(self, active):
        if self.process and self.process.poll() is not None:
            raise Cancelled("Cancelled. Existing work was not interrupted.")
        if self.process and self.process.stdin:
            try:
                self.process.stdin.write(f"# Waiting for {active} active task(s) to finish…\n")
                self.process.stdin.flush()
            except BrokenPipeError as exc:
                raise Cancelled("Cancelled. Existing work was not interrupted.") from exc

    def __exit__(self, *_args):
        if self.process and self.process.poll() is None:
            self.process.terminate()
            self.process.wait(timeout=3)


class Connector:
    def __init__(self, root=ROOT, transport=None, runner=subprocess.run):
        self.root, self.runner = Path(root), runner
        self.config = json.loads((self.root / "config.json").read_text())
        self.server = origin(self.config["server"])
        token = (self.root / "device.token").read_text().strip()
        if not token or (self.root / "device.token").stat().st_mode & 0o077:
            raise ConnectorError("Connector credentials must be private to your desktop user")
        self.transport = transport or Transport(self.server, (self.root / "server-ca.pem").read_text(), token)
        self.token = token

    def wait_idle(self, cancelled=None):
        while True:
            response = self.transport.json("/api/client/maintenance", {"pause": True})
            active = response.get("active_runs")
            if isinstance(active, bool) or not isinstance(active, int) or active < 0:
                raise ConnectorError("Server did not confirm the active-work count")
            if cancelled:
                cancelled(active)
            if active == 0:
                return
            time.sleep(1)

    def unpause(self):
        try:
            self.transport.json("/api/client/maintenance", {"pause": False})
        except ConnectorError as exc:
            raise ConnectorError("Could not clear paused access. Use Resume when the server is reachable again.") from exc

    def pause(self, cancelled=None):
        stopped = False
        try:
            self.wait_idle(cancelled)
            service("stop", self.runner)
            stopped = True
            private_write(self.root / "paused", "1")
            service("disable", self.runner)
        finally:
            if not stopped:
                self.unpause()

    def resume(self):
        service("enable", self.runner)
        service("start", self.runner)
        self.unpause()
        (self.root / "paused").unlink(missing_ok=True)

    def open_chat(self):
        # CODEX: The browser removes this login fragment immediately; it is never sent in an HTTP URL.
        webbrowser.open(self.server + "/#login=" + quote(self.token, safe=""))

    @staticmethod
    def connector_is_live(state):
        return (state.get("client_version_live") is True
                or state.get("connector_online") is True)

    @classmethod
    def live_version(cls, state, version):
        return state.get("client_version") == version and cls.connector_is_live(state)

    def wait_ready(self, version):
        for _attempt in range(30):
            service("is-active", self.runner)
            state = self.transport.json("/api/me")
            if self.live_version(state, version):
                return
            time.sleep(1)
        raise ConnectorError("The connector service did not report the installed version")

    def verify_current_release(self, version, cancelled=None):
        state = self.transport.json("/api/me")
        if self.live_version(state, version):
            return "The connector is already up to date and connected."
        if (self.root / "paused").exists():
            return f"Connector {version} is installed. Local access remains paused; use Resume when you want it connected."
        maintenance_set = True
        try:
            if self.connector_is_live(state):
                self.wait_idle(cancelled)
            else:
                response = self.transport.json("/api/client/maintenance", {"pause": True})
                active = response.get("active_runs")
                if isinstance(active, bool) or not isinstance(active, int) or active < 0:
                    raise ConnectorError("Server did not confirm the active-work count")
                if cancelled:
                    cancelled(active)
            service("stop", self.runner)
            try:
                service("start", self.runner)
                self.wait_ready(version)
            except BaseException:
                service("start", self.runner, check=False)
                raise
        finally:
            if maintenance_set:
                self.unpause()
        return f"Connector {version} was restarted and verified."

    def update(self, cancelled=None):
        metadata = self.transport.json("/api/client/release")
        if not metadata.get("available"):
            return "No connector release is available yet."
        version = metadata.get("version")
        current = (self.root / "current").resolve()
        installed = payload_version(current)
        old_version, new_version = version_tuple(installed), version_tuple(version)
        if new_version == old_version:
            return self.verify_current_release(version, cancelled)
        if new_version < old_version or new_version[0] != old_version[0]:
            raise ConnectorError("Downgrades and incompatible major versions are not installed automatically")
        if metadata.get("archive_url", "/api/client/download/archive") != "/api/client/download/archive":
            raise ConnectorError("Release download must remain on the approved server endpoint")
        with tempfile.TemporaryDirectory(prefix="codex-connector-update-") as directory:
            archive_path = Path(directory) / "client.tar.gz"
            self.transport.download(archive_path, metadata.get("size"), metadata.get("sha256"))
            source = extract_release(archive_path, Path(directory) / "payload")
            if payload_version(source) != version:
                raise ConnectorError("Downloaded version does not match the release manifest")
            prepared = install_release(source, self.root, version, self.runner)
        was_paused = (self.root / "paused").exists()
        stopped = False
        try:
            self.wait_idle(cancelled)
            service("stop", self.runner)
            stopped = True
            switch_release(self.root, prepared)
            if not was_paused:
                service("start", self.runner)
                try:
                    self.wait_ready(version)
                except ConnectorError as exc:
                    raise ConnectorError("The updated connector did not become ready; restoring the previous version") from exc
        except BaseException:
            if stopped:
                # CODEX: The durable maintenance gate stays set across reconnects until finally below.
                service("stop", self.runner)
                switch_release(self.root, current)
                if not was_paused:
                    service("start", self.runner)
            raise
        finally:
            if not was_paused:
                self.unpause()
        return f"Connector updated to {version}."


def main():
    parser = argparse.ArgumentParser(description="Control the local Codex Connector")
    parser.add_argument("action", nargs="?", choices=("open", "pause", "resume", "update", "status"))
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args()
    action = args.action
    if action is None:
        if not gui_available():
            parser.error("Choose open, pause, resume, update or status")
        result = subprocess.run(["zenity", "--list", "--title=Codex Connector", "--column=Action",
            "Open chat", "Pause local access", "Resume local access", "Check / install update"], capture_output=True, text=True)
        if result.returncode:
            return
        action = {"Open chat": "open", "Pause local access": "pause", "Resume local access": "resume",
                  "Check / install update": "update"}.get(result.stdout.strip())
    try:
        connector = Connector(args.root)
        message = ""
        if action == "open":
            connector.open_chat()
        elif action == "resume":
            with exclusive(args.root):
                connector.resume()
                message = "Local access resumed. Closing Chrome will not stop the connector."
        elif action == "status":
            service("status", check=False)
        else:
            with exclusive(args.root), Progress() as progress:
                if action == "pause":
                    connector.pause(progress.check)
                    message = "Local access is paused. Use Resume to reconnect."
                else:
                    message = connector.update(progress.check)
        if message:
            print(message)
            if gui_available():
                subprocess.run(["zenity", "--info", "--title=Codex Connector", "--text=" + message])
    except (ConnectorError, OSError, ValueError, subprocess.SubprocessError, tarfile.TarError) as exc:
        message = str(exc) or "Connector action failed; existing chats and credentials were preserved."
        print(message, file=sys.stderr)
        if gui_available():
            subprocess.run(["zenity", "--error", "--title=Codex Connector", "--text=" + message])
        raise SystemExit(1)


if __name__ == "__main__":
    main()
