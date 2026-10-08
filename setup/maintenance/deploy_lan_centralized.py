"""CODEX: Coordinate a frozen backend, Connector and browser release without interrupting prompts."""

import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from urllib.request import urlopen

for _source_parent in Path(__file__).resolve().parents:
    if (_source_parent / "project_layout.py").is_file():
        sys.path.insert(0, str(_source_parent))
        break
from project_layout import configure_imports, source_file

configure_imports(__file__)

from codex_lan import __version__
from deploy_lan_backend import UPDATE_SERVICE, _queue_activation
from deploy_lan_web import read_assets


INSTALLATION = Path.home() / ".local/share/codex-lan"
RELEASE_FILES = ("client.tar.gz", "Install-Codex-Connector.sh", "release.json")
WEB_FILES = ("style.css", "app.js", "index.html")


def activate_backend():
    """CODEX: Wait on the detached updater so publication cannot outrun backend activation."""
    invocation = active_backend_update_invocation()
    if invocation:
        wait_for_existing_backend_update(invocation)
        return
    try:
        _queue_activation(wait=True)
    except subprocess.CalledProcessError:
        invocation = active_backend_update_invocation()
        if not invocation:
            raise
        wait_for_existing_backend_update(invocation)


def active_backend_update_invocation():
    """CODEX: Identify a live updater so a centralized release can join instead of duplicating it."""
    result = subprocess.run(
        ["systemctl", "--user", "show", UPDATE_SERVICE,
         "--property=ActiveState", "--property=InvocationID"],
        check=False, capture_output=True, text=True, timeout=10,
    )
    if result.returncode:
        return None
    properties = dict(
        line.split("=", 1) for line in result.stdout.splitlines() if "=" in line
    )
    if properties.get("ActiveState") not in {"activating", "active", "reloading", "deactivating"}:
        return None
    return properties.get("InvocationID") or None


def backend_update_succeeded(invocation):
    """CODEX: Tie success to this systemd invocation, avoiding stale logs from an older updater."""
    result = subprocess.run(
        ["journalctl", "--user", f"_SYSTEMD_INVOCATION_ID={invocation}",
         "--output=cat", "--no-pager"],
        check=False, capture_output=True, text=True, timeout=30,
    )
    return result.returncode == 0 and "Updated backend activated and health check passed." in result.stdout


def wait_for_existing_backend_update(invocation, timeout=3700):
    """CODEX: Preserve active prompts while requiring the already-running updater's own success record."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        current = active_backend_update_invocation()
        if current == invocation:
            time.sleep(0.5)
            continue
        if current:
            raise RuntimeError("Backend updater invocation changed while the centralized release was waiting")
        journal_deadline = min(deadline, time.monotonic() + 10)
        while time.monotonic() < journal_deadline:
            if backend_update_succeeded(invocation):
                return
            time.sleep(0.25)
        raise RuntimeError("Existing backend updater did not complete successfully; Connector and browser were not published")
    raise RuntimeError("Existing backend updater did not finish within the release timeout")


def read_installed_health():
    """CODEX: Read the installed server rather than trusting the frozen release manifest alone."""
    with urlopen("http://127.0.0.1:8765/api/health", timeout=2) as response:
        return json.load(response)


def installed_health_matches_source(status):
    """CODEX: Accept only a ready, healthy server running this source release."""
    return (isinstance(status, dict)
            and status.get("ok") is True
            and status.get("backend_update") == "ready"
            and status.get("version") == __version__)


def wait_for_installed_backend(timeout=45):
    """CODEX: Require the post-update server to expose the exact source version before publishing."""
    deadline = time.monotonic() + timeout
    last_status = None
    while time.monotonic() < deadline:
        try:
            last_status = read_installed_health()
            if installed_health_matches_source(last_status):
                return last_status
        except (OSError, ValueError):
            pass
        time.sleep(0.25)
    found = last_status.get("version") if isinstance(last_status, dict) else "unavailable"
    raise RuntimeError(
        f"Backend updater completed, but installed /api/health did not report source version "
        f"{__version__!r} in ready state (found {found!r}); Connector and browser were not published"
    )


def publish_prepared(prepared, targets):
    for relative, destination in targets:
        destination.parent.mkdir(parents=True, exist_ok=True)
        os.replace(prepared / relative, destination)


def activate_release(root, installation=INSTALLATION):
    root, installation = Path(root), Path(installation)
    manifest = json.loads((root / "release.json").read_text())
    if manifest["version"] != __version__:
        raise RuntimeError("Frozen backend and Connector versions do not match")
    digest = hashlib.sha256()
    with (root / "client.tar.gz").open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    if digest.hexdigest() != manifest["sha256"] or (root / "client.tar.gz").stat().st_size != manifest["size"]:
        raise RuntimeError("Frozen Connector checksum or size is incorrect")
    try:
        browser_assets = read_assets(source_file(root / "source", "codex_lan/web"))
    except (OSError, ValueError) as error:
        raise RuntimeError("Frozen browser artifact is missing or empty, or unsafe: " + str(error)) from error
    subprocess.run(["bash", str(root / "Install-Codex-Connector.sh"), "--verify"], check=True, timeout=60)
    updates = installation / "updates"
    updates.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (installation / "centralized-update.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        backup = Path(tempfile.mkdtemp(prefix="before-centralized-", dir=updates))
        prepared = Path(tempfile.mkdtemp(prefix="prepared-centralized-", dir=updates))
        targets, existed = [], {}
        for group, names, source, destination in (
            ("release", RELEASE_FILES, root, installation / "server/releases"),
            ("web", browser_assets, source_file(root / "source", "codex_lan/web"), installation / "code/codex_lan/web"),
        ):
            if not destination.is_dir():
                raise RuntimeError("Installed release or browser directory is missing")
            (backup / group).mkdir()
            (prepared / group).mkdir()
            for name in names:
                relative, target = Path(group) / name, destination / name
                if not target.resolve().is_relative_to(destination.resolve()):
                    raise RuntimeError("Installed artifact escapes its release directory: " + name)
                if not (source / name).is_file() or not (source / name).stat().st_size:
                    raise RuntimeError("Frozen artifact is missing or empty: " + name)
                (prepared / relative).parent.mkdir(parents=True, exist_ok=True)
                (backup / relative).parent.mkdir(parents=True, exist_ok=True)
                if group == "web":
                    (prepared / relative).write_bytes(browser_assets[name])
                else:
                    shutil.copy2(source / name, prepared / relative)
                existed[relative] = target.exists()
                if target.exists():
                    shutil.copy2(target, backup / relative)
                targets.append((relative, target))
        print("Release and browser rollback copies: " + str(backup), flush=True)
        activate_backend()
        wait_for_installed_backend()
        try:
            publish_prepared(prepared, targets)
        except BaseException as failure:
            errors = []
            for relative, target in targets:
                try:
                    if existed[relative]:
                        replacement = prepared / relative
                        shutil.copy2(backup / relative, replacement)
                        os.replace(replacement, target)
                    else:
                        target.unlink(missing_ok=True)
                except OSError as error:
                    errors.append(str(error))
            # CODEX: The new backend may already own prompts; never stop it to roll back static downloads.
            message = "Backend is active, but release publication failed; previous downloads and browser assets were restored. "
            if errors:
                message = "Backend is active and publication rollback needs attention: " + "; ".join(errors) + ". "
            raise RuntimeError(message + "Rollback files: " + str(backup)) from failure
        print("Server execution and Connector " + __version__ + " are published. Clients must update their Connector.", flush=True)
