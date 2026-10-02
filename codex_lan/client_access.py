"""CODEX: Approved-device pairing, release downloads, and idle-only connector maintenance."""

import base64
import hashlib
import json
import re
import secrets
import time
from pathlib import Path

from aiohttp import web

from .request_keys import DEVICE


class ClientAccess:
    def __init__(self, service, data_dir):
        self.service = service
        self.data_dir = Path(data_dir)
        with service.store.db:
            service.store.db.execute("CREATE TABLE IF NOT EXISTS client_maintenance (device_id TEXT PRIMARY KEY REFERENCES devices(id), updated_at REAL NOT NULL)")
        service.runtime.draining_devices = {row[0] for row in service.store.db.execute("SELECT device_id FROM client_maintenance")}

    def release_info(self):
        path = self.data_dir / "releases/release.json"
        archive = self.data_dir / "releases/client.tar.gz"
        installer = self.data_dir / "releases/Install-Codex-Connector.sh"
        try:
            if (not path.is_file() or path.stat().st_size > 32768
                    or not archive.is_file() or not installer.is_file()):
                return {"available": False}
            value = json.loads(path.read_bytes())
            version, digest, size = value["version"], value["sha256"], value["size"]
            if (not isinstance(version, str) or not re.fullmatch(r"\d{1,4}\.\d{1,4}\.\d{1,4}", version)
                    or not isinstance(digest, str) or not re.fullmatch(r"[a-fA-F0-9]{64}", digest)
                    or isinstance(size, bool) or not isinstance(size, int) or not 0 < size <= 300 * 1024 * 1024
                    or archive.stat().st_size != size):
                return {"available": False}
        except (OSError, UnicodeError, ValueError, TypeError, KeyError):
            return {"available": False}
        return {
            "available": True,
            "version": version,
            "sha256": digest.lower(),
            "size": size,
            "archive_url": f"/api/client/download/{version}/archive",
            "installer_url": f"/api/client/download/{version}/installer",
            "private_installer_url": f"/api/client/installer/{version}",
            "pairing_url": f"/api/client/pairing/{version}",
            "archive_filename": f"Codex-Connector-{version}.tar.gz",
            "installer_filename": f"Install-Codex-Connector-{version}.sh",
            "private_installer_filename": f"Install-Codex-Connector-{version}-private.sh",
            "pairing_filename": f"codex-pairing-{version}.json",
        }

    async def release(self, request):
        return web.json_response(self.release_info())

    def _requested_release(self, request):
        release = self.release_info()
        if release.get("available") is not True:
            raise web.HTTPServiceUnavailable(text="The administrator has not published a connector release yet.")
        requested = request.match_info.get("version")
        if requested and requested != release["version"]:
            raise web.HTTPConflict(
                text=f"Connector {requested} is no longer the current release. Download connector {release['version']} and retry."
            )
        return release

    async def download(self, request):
        name = {"archive": "client.tar.gz", "installer": "Install-Codex-Connector.sh"}.get(request.match_info["kind"])
        if not name:
            raise web.HTTPNotFound()
        release = self._requested_release(request)
        path = self.data_dir / "releases" / name
        if not path.is_file():
            raise web.HTTPNotFound(text="The administrator has not published a connector release yet.")
        filename = release["archive_filename" if request.match_info["kind"] == "archive" else "installer_filename"]
        return web.FileResponse(path, headers={"Content-Disposition": f'attachment; filename="{filename}"',
                                              "Content-Type": "application/octet-stream"})

    def _new_pairing(self, device, release=None):
        network = self.service.network
        if not network:
            raise web.HTTPServiceUnavailable(text="Direct HTTPS access has not been configured yet.")
        release = release or self.release_info()
        if release.get("available") is not True:
            raise web.HTTPServiceUnavailable(text="The administrator has not published a connector release yet.")
        now = time.time()
        code = secrets.token_urlsafe(32)
        expires_at = now + 600
        code_hash = hashlib.sha256(code.encode()).hexdigest()
        with self.service.store._lock:
            with self.service.store.db:
                self.service.store.db.execute(
                    "DELETE FROM client_pairings WHERE expires_at<=?",
                    (now,),
                )
                self.service.store.db.execute(
                    "DELETE FROM client_pairings WHERE code_hash IN ("
                    "SELECT code_hash FROM client_pairings WHERE device_id=? "
                    "ORDER BY expires_at DESC LIMIT -1 OFFSET 4)",
                    (device["id"],),
                )
            count = self.service.store.db.execute(
                "SELECT COUNT(*) FROM client_pairings"
            ).fetchone()[0]
            if count >= 100:
                raise web.HTTPTooManyRequests(text="Too many pairing requests. Please try again later.")
            with self.service.store.db:
                self.service.store.db.execute(
                    "INSERT INTO client_pairings(code_hash,device_id,expires_at,connector_version) VALUES(?,?,?,?)",
                    (code_hash, device["id"], expires_at, release["version"]),
                )
        try:
            certificate = network["ca"].read_text()
        except OSError as exc:
            raise web.HTTPServiceUnavailable(text="The server certificate is temporarily unavailable.") from exc
        return {"format": 1, "server": network["public_origin"], "pairing_code": code,
                "device_id": device["id"], "ca_certificate": certificate,
                "device_name": device["name"], "expires_at": expires_at,
                "connector_version": release["version"]}

    def _pairing_record(self, code, *, consume=False):
        if not isinstance(code, str) or not 32 <= len(code) <= 128:
            raise web.HTTPForbidden(text="Invalid or expired pairing file. Download a fresh one.")
        key = hashlib.sha256(code.encode()).hexdigest()
        now = time.time()
        with self.service.store._lock:
            row = self.service.store.db.execute(
                "SELECT device_id,expires_at,connector_version FROM client_pairings WHERE code_hash=?",
                (key,),
            ).fetchone()
            if not row or row["expires_at"] <= now:
                with self.service.store.db:
                    self.service.store.db.execute("DELETE FROM client_pairings WHERE code_hash=?", (key,))
                raise web.HTTPForbidden(text="Invalid, used, or expired pairing file. Download a fresh one.")
            pairing = dict(row)
            if consume:
                with self.service.store.db:
                    deleted = self.service.store.db.execute(
                        "DELETE FROM client_pairings WHERE code_hash=?", (key,)
                    ).rowcount
                if deleted != 1:
                    raise web.HTTPForbidden(text="Invalid, used, or expired pairing file. Download a fresh one.")
        return pairing

    @staticmethod
    def _bootstrap_script(pairing):
        version = pairing.get("connector_version")
        if not isinstance(version, str) or not re.fullmatch(r"\d{1,4}\.\d{1,4}\.\d{1,4}", version):
            raise ValueError("Invalid connector release version")
        payload = json.dumps(pairing, ensure_ascii=True, separators=(",", ":")).encode()
        encoded = base64.b64encode(payload).decode("ascii")
        return f'''#!/usr/bin/env bash
# CODEX: Private ten-minute bootstrap; the one-time pairing value is never placed in a URL or process argument.
set -Eeuo pipefail
umask 077
if [[ -f "$0" && ! -L "$0" ]]; then chmod 700 -- "$0" 2>/dev/null || true; fi
if (( EUID == 0 )); then
    printf '%s\\n' 'Run as your normal Ubuntu desktop user, not with sudo.' >&2
    exit 1
fi
codex_bootstrap_python=""
for codex_bootstrap_candidate in python3 python3.14 python3.13 python3.12 python3.11 python3.10; do
    codex_bootstrap_executable="$(command -v "$codex_bootstrap_candidate" || true)"
    if [[ -z "$codex_bootstrap_executable" ]]; then continue; fi
    if "$codex_bootstrap_executable" -I -c 'import platform,sys; raise SystemExit(not (sys.implementation.name == "cpython" and (3,10) <= sys.version_info[:2] <= (3,14) and sys.platform == "linux" and platform.machine() in {{"x86_64","AMD64"}} and "t" not in getattr(sys,"abiflags","")))' 2>/dev/null; then
        codex_bootstrap_python="$codex_bootstrap_executable"
        break
    fi
done
if [[ -z "$codex_bootstrap_python" ]]; then
    printf '%s\\n' 'Codex Connector needs CPython 3.10–3.14 on Ubuntu/Linux x86_64.' >&2
    exit 1
fi
codex_bootstrap_dir="$(mktemp -d /tmp/codex-connector-bootstrap.XXXXXXXXXX)"
codex_bootstrap_cleanup() {{
    # CODEX: Remove only the private directory created by this bootstrap.
    case "$codex_bootstrap_dir" in /tmp/codex-connector-bootstrap.??????????) rm -rf -- "$codex_bootstrap_dir" ;; esac
}}
trap codex_bootstrap_cleanup EXIT
cat > "$codex_bootstrap_dir/pairing.b64" <<'CODEX_PAIRING_PAYLOAD'
{encoded}
CODEX_PAIRING_PAYLOAD
chmod 600 "$codex_bootstrap_dir/pairing.b64"
"$codex_bootstrap_python" -I - "$codex_bootstrap_dir" <<'CODEX_BOOTSTRAP_PY'
import base64, json, os, re, ssl, sys, urllib.error, urllib.request
from pathlib import Path
from urllib.parse import urlsplit

root = Path(sys.argv[1])
raw = base64.b64decode((root / "pairing.b64").read_bytes().strip(), validate=True)
if len(raw) > 65536:
    raise SystemExit("Invalid private setup file.")
pairing = json.loads(raw)
server = pairing.get("server", "")
parsed = urlsplit(server)
if (pairing.get("format") != 1 or parsed.scheme != "https" or not parsed.hostname
        or parsed.username or parsed.password or parsed.query or parsed.fragment
        or parsed.path not in {{"", "/"}}):
    raise SystemExit("Invalid private setup file.")
for key in ("pairing_code", "device_id", "ca_certificate"):
    if not isinstance(pairing.get(key), str) or not pairing[key]:
        raise SystemExit("Invalid private setup file.")
version = pairing.get("connector_version", "")
if not isinstance(version, str) or not re.fullmatch(r"\d{{1,4}}\.\d{{1,4}}\.\d{{1,4}}", version):
    raise SystemExit("Invalid private setup file.")
pairing_path = root / ("codex-pairing-" + version + ".json")
pairing_path.write_bytes(raw)
os.chmod(pairing_path, 0o600)
ca_path = root / "server-ca.pem"
ca_path.write_text(pairing["ca_certificate"])
os.chmod(ca_path, 0o600)
request = urllib.request.Request(
    server.rstrip("/") + "/api/client/bootstrap/installer/" + version,
    data=raw,
    headers={{"Content-Type": "application/json", "Accept": "application/octet-stream"}},
    method="POST",
)
context = ssl.create_default_context(cafile=str(ca_path))
installer_path = root / ("Install-Codex-Connector-" + version + ".sh")
maximum = 320 * 1024 * 1024
try:
    with urllib.request.urlopen(request, context=context, timeout=45) as response, installer_path.open("wb") as output:
        length = response.headers.get("Content-Length")
        if length and int(length) > maximum:
            raise SystemExit("Connector package is unexpectedly large.")
        total = 0
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > maximum:
                raise SystemExit("Connector package is unexpectedly large.")
            output.write(chunk)
except urllib.error.HTTPError as error:
    if error.code == 403:
        raise SystemExit("This private setup is expired or was replaced by a newer download. Download a fresh one-step setup and run the exact filename shown by Codex.") from None
    raise SystemExit("Connector download failed with HTTP status " + str(error.code) + ".") from None
except urllib.error.URLError as error:
    raise SystemExit("Could not reach the Codex server: " + str(error.reason)) from None
if not installer_path.stat().st_size:
    raise SystemExit("Connector download was empty.")
os.chmod(installer_path, 0o700)
CODEX_BOOTSTRAP_PY
printf '%s\\n' 'Private setup downloaded. Installing or repairing Codex Connector…'
bash "$codex_bootstrap_dir/Install-Codex-Connector-{version}.sh" --pair-file "$codex_bootstrap_dir/codex-pairing-{version}.json"
'''

    async def private_installer(self, request):
        release = self._requested_release(request)
        pairing = self._new_pairing(request[DEVICE], release)
        return web.Response(
            body=self._bootstrap_script(pairing).encode(),
            content_type="application/x-sh",
            headers={"Content-Disposition": f'attachment; filename="{release["private_installer_filename"]}"'},
        )

    async def bootstrap_installer(self, request):
        release = self._requested_release(request)
        if request.content_type != "application/json":
            raise web.HTTPUnsupportedMediaType(text="Expected a JSON pairing file.")
        if request.content_length is not None and request.content_length > 65536:
            raise web.HTTPRequestEntityTooLarge(max_size=65536, actual_size=request.content_length)
        raw = await request.content.read(65537)
        if len(raw) > 65536:
            raise web.HTTPRequestEntityTooLarge(max_size=65536, actual_size=len(raw))
        try:
            data = json.loads(raw)
        except (UnicodeError, ValueError, TypeError) as exc:
            raise web.HTTPBadRequest(text="Invalid pairing file.") from exc
        code = data.get("pairing_code") if isinstance(data, dict) else None
        device_id = data.get("device_id") if isinstance(data, dict) else None
        pairing = self._pairing_record(code)
        if not isinstance(device_id, str) or device_id != pairing["device_id"]:
            raise web.HTTPForbidden(text="Pairing file does not match this device.")
        if (data.get("connector_version") != pairing.get("connector_version")
                or pairing.get("connector_version") != release["version"]):
            raise web.HTTPConflict(text="This pairing file belongs to another connector release. Download a fresh setup file.")
        device = self.service.store.get_device(pairing["device_id"])
        if device.get("status") != "approved":
            raise web.HTTPForbidden(text="Device approval changed. Download a fresh private setup file.")
        path = self.data_dir / "releases/Install-Codex-Connector.sh"
        if not path.is_file():
            raise web.HTTPNotFound(text="The administrator has not published a connector release yet.")
        return web.FileResponse(path, headers={
            "Content-Disposition": f'attachment; filename="{release["installer_filename"]}"',
            "Content-Type": "application/octet-stream",
        })

    async def pair(self, request):
        release = self._requested_release(request)
        value = self._new_pairing(request[DEVICE], release)
        return web.json_response(value, headers={
            "Content-Disposition": f'attachment; filename="{release["pairing_filename"]}"'
        })

    async def claim(self, request):
        data = await request.json()
        code = data.get("pairing_code") if isinstance(data, dict) else None
        pairing = self._pairing_record(code)
        release = self.release_info()
        if (release.get("available") is not True
                or pairing.get("connector_version") != release.get("version")):
            self._pairing_record(code, consume=True)
            raise web.HTTPConflict(
                text="This pairing file belongs to an older connector release. Download the current setup file."
            )
        pairing = self._pairing_record(code, consume=True)
        token = self.service.store.issue_device_credential(pairing["device_id"], kind="connector")
        device = self.service.store.authenticate(token)
        if device["id"] != pairing["device_id"]:
            raise web.HTTPForbidden(text="Device approval changed. Request a new pairing file.")
        return web.json_response({"device": device, "token": token})

    async def maintenance(self, request):
        if not request.headers.get("Authorization", "").startswith("Bearer "):
            raise web.HTTPForbidden(text="Only the local connector can request maintenance.")
        data = await request.json()
        if not isinstance(data, dict) or not isinstance(data.get("pause"), bool):
            raise web.HTTPBadRequest(text="Expected a pause flag.")
        peer = self.service.runtime.peers.get(request[DEVICE]["id"])
        device_id = request[DEVICE]["id"]
        with self.service.store.db:
            if data["pause"]:
                self.service.store.db.execute("INSERT OR REPLACE INTO client_maintenance(device_id,updated_at) VALUES(?,?)", (device_id, time.time()))
                self.service.runtime.draining_devices.add(device_id)
            else:
                self.service.store.db.execute("DELETE FROM client_maintenance WHERE device_id=?", (device_id,))
                self.service.runtime.draining_devices.discard(device_id)
        if peer:
            peer.draining = data["pause"]
        return web.json_response({"paused": device_id in self.service.runtime.draining_devices,
                                  "active_runs": len(self.service.store.list_active_runs(request[DEVICE]["id"]))})

    def routes(self):
        return [web.get("/api/client/release", self.release),
                web.get("/api/client/download/{kind}", self.download),
                web.get("/api/client/download/{version}/{kind}", self.download),
                web.post("/api/client/installer", self.private_installer),
                web.post("/api/client/installer/{version}", self.private_installer),
                web.post("/api/client/bootstrap/installer", self.bootstrap_installer),
                web.post("/api/client/bootstrap/installer/{version}", self.bootstrap_installer),
                web.post("/api/client/pairing", self.pair),
                web.post("/api/client/pairing/{version}", self.pair),
                web.post("/api/client/claim", self.claim),
                web.post("/api/client/maintenance", self.maintenance)]
