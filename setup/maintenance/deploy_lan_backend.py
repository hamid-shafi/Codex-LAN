"""CODEX: Activate a tested backend only after all prompts finish; retain rollback files."""

import fcntl
import hashlib
import json
import os
import shutil
import sqlite3
import ssl
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.request import Request, urlopen

_layout_root = next((parent for parent in Path(__file__).resolve().parents
                     if (parent / "project_layout.py").is_file()), None)
if _layout_root is not None:
    sys.path.insert(0, str(_layout_root))
    from project_layout import configure_imports, source_file, source_root
    configure_imports(__file__)
else:
    # The immutable recovery helper is copied alone and must run without product imports.
    def source_root(path):
        return Path(path).resolve().parent

    def source_file(root, name):
        return Path(root) / name

FILES = ("__init__.py", "activity.py", "auth.py", "provider_usage.py", "usage_estimator.py", "quota_accounting.py", "network.py", "https_setup.py", "host_setup.py", "relay_setup.py", "request_keys.py", "client_access.py", "model_registry.py", "runtime.py", "store.py", "worker.py", "server.py", "host_worker.py", "health_monitor.py", "media.py", "egress.py", "recovery.py", "transport.py", "systemd_watchdog.py", "companions.py", "migrate.py", "open_browser.py")
SERVICE = "codex-lan-server.service"
WORKER_SERVICE = "codex-lan-host-worker.service"
HEALTH_MONITOR_SERVICE = "codex-lan-health-monitor.service"
UPDATE_SERVICE = "codex-lan-backend-update.service"
RECOVERY_SERVICE = "codex-lan-backend-recovery.service"
ACTIVATION_FLAG = "--activation-child"
RECOVERY_FLAG = "--recovery-child"
RECOVERY_HELPER_FLAG = "--recovery-helper="
WEB_FLAG = "--with-web"
INSTALLATION = Path.home() / ".local/share/codex-lan"
RECOVERY_TIMEOUT_SECONDS = 3600
ACTIVATION_WAIT_SECONDS = 3600
MARKER_FORMAT = 1
MARKER_PHASE_PREPARED = "prepared"
MARKER_PHASE_ROLLBACK_READY = "rollback-ready"
_CURRENT_RELEASE = object()


def active_count(db):
    return db.execute("SELECT COUNT(*) FROM runs WHERE status IN ('queued', 'running')").fetchone()[0]


def _release_version():
    from codex_lan import __version__
    return __version__


def _read_web_assets(source):
    import deploy_lan_web
    return deploy_lan_web.read_assets(source)


def _deploy_web(source, destination):
    import deploy_lan_web
    return deploy_lan_web.deploy(source, destination)


def _fsync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_write(path, content, mode=0o600):
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".codex-atomic-", dir=path.parent)
    temporary = Path(temporary)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            os.fchmod(stream.fileno(), mode)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _systemd_quote(value):
    value = str(value)
    if any(character in value for character in ("\n", "\r", "\0")):
        raise RuntimeError("A recovery service path contains an unsafe control character")
    return '"' + value.replace("%", "%%").replace("\\", "\\\\").replace('"', '\\"').replace("$", "$$") + '"'


def _systemd_command(*values):
    return " ".join(_systemd_quote(value) for value in values)


def _health_monitor_unit(installation):
    """CODEX: Keep liveness recovery outside the server process it observes."""
    installation = Path(installation)
    python = installation / "venv/bin/python"
    code = installation / "code"
    command = _systemd_command(
        python, "-m", "codex_lan.health_monitor",
        "--interval", "15", "--timeout", "5",
        "--failure-threshold", "4", "--cooldown", "300",
        "--active-stall-timeout", "60",
    )
    return (
        "[Unit]\n"
        "Description=Monitor and recover Codex LAN health\n"
        f"After=network.target {SERVICE}\n\n"
        "[Service]\n"
        "Type=simple\n"
        f"WorkingDirectory={str(code).replace('%', '%%')}\n"
        f"ExecStart={command}\n"
        "Restart=always\n"
        "RestartSec=5\n"
        "TimeoutStopSec=15\n"
        "UMask=0077\n\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    ).encode()


def _install_health_monitor_unit(*, installation=None, unit_directory=None,
                                 runner=None, start=False):
    """CODEX: Install and optionally start recovery only after activated code is healthy."""
    installation = INSTALLATION if installation is None else Path(installation)
    unit_directory = (Path.home() / ".config/systemd/user"
                      if unit_directory is None else Path(unit_directory))
    runner = subprocess.run if runner is None else runner
    if installation.is_symlink() or unit_directory.is_symlink():
        raise RuntimeError("The health-monitor installation path contains a symbolic link")
    unit = unit_directory / HEALTH_MONITOR_SERVICE
    if unit.is_symlink():
        raise RuntimeError("The health-monitor service is a symbolic link")
    _atomic_write(unit, _health_monitor_unit(installation), mode=0o644)
    runner(["systemctl", "--user", "daemon-reload"], check=True, timeout=30)
    runner(["systemctl", "--user", "enable", HEALTH_MONITOR_SERVICE], check=True, timeout=30)
    if start:
        runner(["systemctl", "--user", "restart", HEALTH_MONITOR_SERVICE], check=True, timeout=30)
    return unit


def _restart_policy_dropin(service):
    """CODEX: Recover crashes while leaving guarded hang recovery to the health monitor."""
    if service == SERVICE:
        return (
            b"[Service]\nType=simple\nWatchdogSec=0\n"
            b"TimeoutStartSec=300s\nTimeoutStopSec=20s\nRestart=always\nRestartSec=5\n"
        )
    return b"[Service]\nRestart=always\nRestartSec=5\n"


def _install_restart_policy_dropins(*, unit_directory=None, runner=None):
    """CODEX: Upgrade restart policy without replacing commands or restarting live services."""
    unit_directory = (Path.home() / ".config/systemd/user"
                      if unit_directory is None else Path(unit_directory))
    runner = subprocess.run if runner is None else runner
    _require_unaliased_directory(unit_directory, "user systemd unit")
    installed = []
    for service in (SERVICE, WORKER_SERVICE):
        dropin_directory = unit_directory / f"{service}.d"
        if dropin_directory.exists():
            _require_unaliased_directory(dropin_directory, f"{service} drop-in")
        else:
            dropin_directory.mkdir(mode=0o700)
            _fsync_directory(unit_directory)
        dropin = dropin_directory / "10-codex-lan-restart.conf"
        if dropin.is_symlink():
            raise RuntimeError(f"The {service} restart-policy drop-in is a symbolic link")
        _atomic_write(dropin, _restart_policy_dropin(service), mode=0o644)
        installed.append(dropin)
    runner(["systemctl", "--user", "daemon-reload"], check=True, timeout=30)
    return tuple(installed)


def _require_unaliased_directory(path, label, must_exist=True):
    path = Path(path)
    if path.is_symlink() or path.absolute() != path.resolve():
        raise RuntimeError(f"The {label} contains a symbolic-link path")
    if must_exist and not path.is_dir():
        raise RuntimeError(f"The {label} directory does not exist")
    return path


def _recovery_unit(installation, dispatcher):
    python = Path(installation) / "venv/bin/python"
    command = _systemd_command(python, dispatcher)
    return (
        "[Unit]\n"
        "Description=Recover an interrupted Codex LAN backend activation\n"
        f"After={SERVICE} {WORKER_SERVICE}\n\n"
        "[Service]\n"
        "Type=oneshot\n"
        f"ExecStart={command}\n"
        f"TimeoutStartSec={RECOVERY_TIMEOUT_SECONDS}\n"
        "Restart=on-failure\n"
        "RestartSec=30\n"
        "UMask=0077\n\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def _recovery_dispatcher_source():
    """CODEX: Return the stable stdlib-only launcher that selects the helper bound into the marker."""
    return f'''"""CODEX: Dispatch recovery to the immutable helper recorded by the interrupted activation."""
import hashlib
import json
import os
import sys
from pathlib import Path


def fail(message):
    print(message, file=sys.stderr, flush=True)
    raise SystemExit(1)


installation = Path(__file__).absolute().parent.parent
recovery = installation / "recovery"
marker = installation / "server/backend-update.maintenance"
if not marker.exists():
    raise SystemExit(0)
if marker.is_symlink():
    fail("The backend recovery marker is a symbolic link")
try:
    content = marker.read_bytes()
    if len(content) > 1024 * 1024:
        fail("The backend recovery marker is unexpectedly large")
    metadata = json.loads(content)
except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
    fail(f"The backend recovery marker cannot be read safely: {{exc}}")
helper_value = metadata.get("recovery_helper")
expected = metadata.get("recovery_helper_sha256")
if not isinstance(helper_value, str) or not isinstance(expected, str) or len(expected) != 64:
    fail("The backend recovery marker is not bound to an immutable recovery helper")
helper = Path(helper_value)
if (not helper.is_absolute() or helper.is_symlink() or not helper.is_file()
        or helper.parent.resolve() != recovery.resolve()
        or helper.resolve() != helper.absolute()):
    fail("The backend recovery helper path is unsafe")
digest = hashlib.sha256(helper.read_bytes()).hexdigest()
if digest != expected or helper.name != f"deploy_lan_backend_recovery-{{digest}}.py":
    fail("The backend recovery helper does not match its marker")
python = installation / "venv/bin/python"
os.execv(str(python), [str(python), str(helper), {RECOVERY_FLAG!r}])
'''.encode()


def _write_immutable(path, content):
    path = Path(path)
    if path.exists() or path.is_symlink():
        if path.is_symlink() or not path.is_file() or path.read_bytes() != content:
            raise RuntimeError(f"An installed recovery helper is not the expected immutable file: {path}")
        return
    _atomic_write(path, content)


def _install_recovery_unit(script_source=None, installation=None, unit_directory=None,
                           runner=None):
    """CODEX: Install a login-time recovery copy before an activation can be queued."""
    installation = INSTALLATION if installation is None else Path(installation)
    runner = subprocess.run if runner is None else runner
    script_source = Path(__file__).resolve() if script_source is None else Path(script_source)
    unit_directory = (Path.home() / ".config/systemd/user"
                      if unit_directory is None else Path(unit_directory))
    _require_unaliased_directory(installation, "installed LAN server")
    _require_unaliased_directory(unit_directory, "user service", must_exist=False)
    recovery_directory = installation / "recovery"
    _require_unaliased_directory(recovery_directory, "installed recovery", must_exist=False)
    python = installation / "venv/bin/python"
    if not python.is_file():
        raise RuntimeError(f"The installed recovery Python was not found: {python}")
    content = script_source.read_bytes()
    compile(content, str(script_source), "exec")
    recovery_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    _require_unaliased_directory(recovery_directory, "installed recovery")
    os.chmod(recovery_directory, 0o700)
    _fsync_directory(recovery_directory.parent)
    digest = _sha256_bytes(content)
    helper = recovery_directory / f"deploy_lan_backend_recovery-{digest}.py"
    _write_immutable(helper, content)
    dispatcher = recovery_directory / "dispatch_recovery_v1.py"
    dispatcher_content = _recovery_dispatcher_source()
    compile(dispatcher_content, str(dispatcher), "exec")
    _write_immutable(dispatcher, dispatcher_content)
    _fsync_directory(recovery_directory)
    unit_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    _require_unaliased_directory(unit_directory, "user service")
    _fsync_directory(unit_directory.parent)
    unit = unit_directory / RECOVERY_SERVICE
    if unit.is_symlink():
        raise RuntimeError("The backend recovery service is a symbolic link")
    _atomic_write(unit, _recovery_unit(installation, dispatcher).encode())
    runner(["systemctl", "--user", "daemon-reload"], check=True, timeout=30)
    runner(["systemctl", "--user", "enable", RECOVERY_SERVICE], check=True, timeout=30)
    runner(["systemctl", "--user", "is-enabled", RECOVERY_SERVICE], check=True, timeout=30)
    wants = unit_directory / "default.target.wants"
    link = wants / RECOVERY_SERVICE
    if not wants.is_dir() or not link.is_symlink() or link.resolve() != unit.resolve():
        raise RuntimeError("The login-time recovery service was not durably enabled")
    _fsync_directory(wants)
    _fsync_directory(unit_directory)
    return helper


def _durable_unlink(path):
    path = Path(path)
    try:
        path.unlink()
    except FileNotFoundError:
        return
    _fsync_directory(path.parent)


def _safe_file_name(name):
    return (isinstance(name, str) and name not in {"", ".", ".."}
            and Path(name).name == name and "/" not in name and "\\" not in name)


def _sha256_bytes(content):
    return hashlib.sha256(content).hexdigest()


def _sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                return digest.hexdigest()
            digest.update(chunk)


def _open_activation_lock(path):
    path = Path(path)
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise RuntimeError("The backend activation lock is not a regular file")
        return os.fdopen(descriptor, "a+")
    except BaseException:
        os.close(descriptor)
        raise


def _recovery_helper_record(helper, recovery_root, expected_digest=None):
    helper = Path(helper)
    recovery_directory = Path(recovery_root).resolve() / "recovery"
    if (not helper.is_absolute() or recovery_directory.is_symlink()
            or helper.is_symlink() or not helper.is_file()
            or helper.parent.resolve() != recovery_directory
            or helper.resolve() != helper.absolute()):
        raise RuntimeError("The recovery helper path is unsafe")
    digest = _sha256_file(helper)
    if (expected_digest is not None and digest != expected_digest
            or helper.name != f"deploy_lan_backend_recovery-{digest}.py"):
        raise RuntimeError("The recovery helper does not match its immutable release digest")
    return {"path": helper.absolute(), "sha256": digest}


def _verify_file_descriptor(descriptor, expected_size, expected_digest, target=None):
    digest = hashlib.sha256()
    size = 0
    while True:
        chunk = os.read(descriptor, 1024 * 1024)
        if not chunk:
            break
        size += len(chunk)
        digest.update(chunk)
        if target is not None:
            target.write(chunk)
    if size != expected_size or digest.hexdigest() != expected_digest:
        raise RuntimeError("The pre-update database backup changed or failed its manifest check")


def _open_verified_backup(path, expected_size, expected_digest):
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode):
            raise RuntimeError("The pre-update database backup is not a regular file")
        _verify_file_descriptor(descriptor, expected_size, expected_digest)
        os.lseek(descriptor, 0, os.SEEK_SET)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _prepare_file_backup(backup_directory, previous):
    backup_directory = Path(backup_directory)
    backup_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(backup_directory, 0o700)
    _fsync_directory(backup_directory.parent)
    entries = []
    for name in sorted(previous):
        if not _safe_file_name(name):
            raise RuntimeError(f"Unsafe backend release file name: {name!r}")
        content = previous[name]
        target = backup_directory / name
        if content is None:
            _durable_unlink(target)
            entries.append({"name": name, "present": False, "size": 0, "sha256": None})
            continue
        if not isinstance(content, bytes):
            raise TypeError(f"Backend release file {name!r} is not bytes")
        _atomic_write(target, content)
        entries.append({
            "name": name,
            "present": True,
            "size": len(content),
            "sha256": _sha256_bytes(content),
        })
    _fsync_directory(backup_directory)
    return entries


def _safe_relative_path(name):
    if not isinstance(name, str) or not name or "\\" in name:
        return False
    path = Path(name)
    return not path.is_absolute() and all(part not in {"", ".", ".."} for part in path.parts)


def _fsync_tree_directories(root):
    root = Path(root)
    directories = [path for path in root.rglob("*") if path.is_dir() and not path.is_symlink()]
    for directory in sorted(directories, key=lambda path: len(path.parts), reverse=True):
        _fsync_directory(directory)
    _fsync_directory(root)


def _prepare_tree_backup(source, backup_directory):
    source = Path(source)
    backup_directory = Path(backup_directory)
    if source.is_symlink() or not source.is_dir():
        raise RuntimeError("The installed browser bundle is missing or unsafe")
    if backup_directory.exists() and any(backup_directory.iterdir()):
        raise RuntimeError("The browser rollback directory is not empty")
    backup_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    directories = []
    files = []
    for path in sorted(source.rglob("*")):
        if path.is_symlink():
            raise RuntimeError(f"The installed browser bundle contains a symbolic link: {path}")
        name = path.relative_to(source).as_posix()
        if not _safe_relative_path(name):
            raise RuntimeError(f"Unsafe browser rollback path: {name!r}")
        target = backup_directory / name
        if path.is_dir():
            target.mkdir(mode=0o700, parents=True, exist_ok=True)
            directories.append(name)
            continue
        if not path.is_file():
            raise RuntimeError(f"Unsupported browser rollback entry: {path}")
        content = path.read_bytes()
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        _atomic_write(target, content)
        files.append({"name": name, "size": len(content), "sha256": _sha256_bytes(content)})
    _fsync_tree_directories(backup_directory)
    return {
        "destination": str(source.resolve()),
        "backup_directory": str(backup_directory.resolve()),
        "mode": source.stat().st_mode & 0o777,
        "directories": directories,
        "files": files,
    }


def replace_files(destination, contents):
    destination = Path(destination)
    for name, content in contents.items():
        if not _safe_file_name(name):
            raise RuntimeError(f"Unsafe backend release file name: {name!r}")
        target = destination / name
        if content is None:
            target.unlink(missing_ok=True)
            continue
        descriptor, temporary = tempfile.mkstemp(prefix=".codex-update-", dir=destination)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
                os.fchmod(stream.fileno(), 0o644)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        finally:
            Path(temporary).unlink(missing_ok=True)
    _purge_package_bytecode(destination)
    _fsync_directory(destination)


def _purge_package_bytecode(destination):
    """CODEX: Remove bytecode that could otherwise survive a same-second release rollback."""
    destination = Path(destination)
    caches = sorted(destination.rglob("__pycache__"), key=lambda path: len(path.parts), reverse=True)
    for cache in caches:
        if cache.is_symlink() or not cache.is_dir():
            raise RuntimeError(f"Unsafe Python bytecode cache in backend installation: {cache}")
        parent = cache.parent
        shutil.rmtree(cache)
        _fsync_directory(parent)
    for bytecode in destination.glob("*.py[co]"):
        if bytecode.is_symlink() or not bytecode.is_file():
            raise RuntimeError(f"Unsafe Python bytecode file in backend installation: {bytecode}")
        _durable_unlink(bytecode)


def service_action(action):
    subprocess.run(["systemctl", "--user", action, SERVICE, WORKER_SERVICE], check=True, timeout=45)


def healthy(expected_version=_CURRENT_RELEASE):
    if expected_version is _CURRENT_RELEASE:
        expected_version = _release_version()
    token = (INSTALLATION / "server/admin.token").read_text().strip()
    identity = Request("http://127.0.0.1:8765/api/me", headers={"Authorization": "Bearer " + token})
    for _ in range(45):
        try:
            with urlopen("http://127.0.0.1:8765/api/health", timeout=1) as response:
                status = json.load(response)
            if (status.get("client_access") is True and status.get("steering") is True
                    and status.get("usage_dashboard") is True
                    and status.get("centralized_execution") is True
                    and status.get("backend_update") == "verifying"
                    and (expected_version is None or status.get("version") == expected_version)):
                server_version = status.get("version")
                if not isinstance(server_version, str) or not server_version:
                    time.sleep(1)
                    continue
                if status.get("public_origin"):
                    config = json.loads((INSTALLATION / "server/network.json").read_text())
                    ca = Path(config["ca"])
                    if not ca.is_absolute():
                        ca = INSTALLATION / "server" / ca
                    with urlopen(status["public_origin"] + "/api/health", context=ssl.create_default_context(cafile=ca), timeout=2) as response:
                        secure_status = json.load(response)
                    if (secure_status.get("client_access") is not True
                            or secure_status.get("centralized_execution") is not True
                            or secure_status.get("backend_update") != "verifying"
                            or secure_status.get("version") != server_version):
                        time.sleep(1)
                        continue
                with urlopen(identity, timeout=2) as response:
                    worker = json.load(response)
                if worker.get("worker_online") is True and worker.get("client_version") == server_version:
                    return True
        except (OSError, ValueError):
            pass
        time.sleep(1)
    return False


def _write_maintenance(marker, metadata):
    content = (json.dumps(metadata, sort_keys=True, separators=(",", ":")) + "\n").encode()
    _atomic_write(marker, content)


def _database_path(db):
    for _, name, filename in db.execute("PRAGMA database_list"):
        if name == "main" and filename:
            return Path(filename).resolve()
    return None


def _maintenance_metadata(destination, backup_directory, entries, database, phase,
                          database_backup=None, database_backup_sha256=None,
                          database_backup_size=None, web=None, recovery_helper=None):
    return {
        "format": MARKER_FORMAT,
        "phase": phase,
        "destination": str(Path(destination).resolve()),
        "backup_directory": str(Path(backup_directory).resolve()),
        "release_files": [entry["name"] for entry in entries],
        "files": entries,
        "database": str(Path(database).resolve()) if database is not None else None,
        "database_backup": str(Path(database_backup).resolve()) if database_backup is not None else None,
        "database_backup_sha256": database_backup_sha256,
        "database_backup_size": database_backup_size,
        "web": web,
        "recovery_helper": (str(recovery_helper["path"])
                            if recovery_helper is not None else None),
        "recovery_helper_sha256": (recovery_helper["sha256"]
                                   if recovery_helper is not None else None),
    }


def _inside(path, root):
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
        return True
    except ValueError:
        return False


def _validated_web_recovery(web, root, rollback_root=None):
    if web is None:
        return None
    if not isinstance(web, dict):
        raise RuntimeError("The browser rollback manifest is invalid")
    destination_value = web.get("destination")
    backup_value = web.get("backup_directory")
    mode = web.get("mode")
    directories = web.get("directories")
    entries = web.get("files")
    if (not isinstance(destination_value, str) or not Path(destination_value).is_absolute()
            or not isinstance(backup_value, str) or not Path(backup_value).is_absolute()
            or type(mode) is not int or not 0 <= mode <= 0o777
            or not isinstance(directories, list) or not isinstance(entries, list)):
        raise RuntimeError("The browser rollback manifest is incomplete")
    destination_source = Path(destination_value)
    backup_source = Path(backup_value)
    if destination_source.is_symlink() or backup_source.is_symlink():
        raise RuntimeError("The browser rollback manifest contains a symbolic-link root")
    destination = destination_source.resolve()
    backup_directory = backup_source.resolve()
    if not _inside(destination, root) or not _inside(backup_directory, root):
        raise RuntimeError("The browser rollback manifest points outside the installation")
    if rollback_root is not None and not _inside(backup_directory, rollback_root):
        raise RuntimeError("The browser rollback manifest points outside its rollback directory")
    if _inside(destination, backup_directory) or _inside(backup_directory, destination):
        raise RuntimeError("The browser installation and rollback directories overlap")
    if not destination.parent.is_dir() or not backup_directory.is_dir():
        raise RuntimeError("The browser rollback directories are missing or unsafe")
    if (len(set(directories)) != len(directories)
            or any(not _safe_relative_path(name) for name in directories)):
        raise RuntimeError("The browser rollback directory manifest is unsafe")
    contents = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise RuntimeError("The browser rollback file manifest is invalid")
        name = entry.get("name")
        size = entry.get("size")
        digest = entry.get("sha256")
        if (not _safe_relative_path(name) or name in contents or type(size) is not int or size < 0
                or not isinstance(digest, str) or len(digest) != 64
                or any(character not in "0123456789abcdef" for character in digest)):
            raise RuntimeError("The browser rollback file manifest contains invalid metadata")
        target = backup_directory / name
        if target.is_symlink() or not target.is_file() or not _inside(target, backup_directory):
            raise RuntimeError(f"Browser rollback file is missing or unsafe: {name}")
        content = target.read_bytes()
        if len(content) != size or _sha256_bytes(content) != digest:
            raise RuntimeError(f"Browser rollback file failed its manifest check: {name}")
        contents[name] = content
    if set(directories) & set(contents):
        raise RuntimeError("The browser rollback manifest has conflicting entries")
    actual_directories = set()
    actual_files = set()
    for path in backup_directory.rglob("*"):
        if path.is_symlink():
            raise RuntimeError("The browser rollback tree contains a symbolic link")
        name = path.relative_to(backup_directory).as_posix()
        (actual_directories if path.is_dir() else actual_files).add(name)
    if actual_directories != set(directories) or actual_files != set(contents):
        raise RuntimeError("The browser rollback tree does not match its full manifest")
    return {
        "destination": destination,
        "mode": mode,
        "directories": directories,
        "contents": contents,
    }


def _validated_recovery(metadata, recovery_root, expected_helper=None):
    if not isinstance(metadata, dict) or metadata.get("format") != MARKER_FORMAT:
        raise RuntimeError("The backend update marker is legacy or invalid; automatic recovery is refusing to guess")
    phase = metadata.get("phase")
    if phase not in {MARKER_PHASE_PREPARED, MARKER_PHASE_ROLLBACK_READY}:
        raise RuntimeError("The backend update marker has an unknown recovery phase")
    root = Path(recovery_root).resolve()
    helper_value = metadata.get("recovery_helper")
    helper_digest = metadata.get("recovery_helper_sha256")
    helper_record = None
    if helper_value is not None or helper_digest is not None or expected_helper is not None:
        if (not isinstance(helper_value, str) or not Path(helper_value).is_absolute()
                or not isinstance(helper_digest, str) or len(helper_digest) != 64
                or any(character not in "0123456789abcdef" for character in helper_digest)):
            raise RuntimeError("The backend update marker is not bound to a valid recovery helper")
        helper_record = _recovery_helper_record(helper_value, root, helper_digest)
        if expected_helper is not None and helper_record["path"] != Path(expected_helper).absolute():
            raise RuntimeError("This recovery helper does not match the interrupted backend release")
    destination_value = metadata.get("destination")
    backup_value = metadata.get("backup_directory")
    if not isinstance(destination_value, str) or not isinstance(backup_value, str):
        raise RuntimeError("The backend update marker is missing rollback paths")
    destination = Path(destination_value)
    backup_directory = Path(backup_value)
    if not destination.is_absolute() or not backup_directory.is_absolute():
        raise RuntimeError("The backend update marker contains a relative rollback path")
    if destination.is_symlink() or backup_directory.is_symlink():
        raise RuntimeError("The backend update marker contains a symbolic-link root")
    destination = destination.resolve()
    backup_directory = backup_directory.resolve()
    if not _inside(destination, root) or not _inside(backup_directory, root):
        raise RuntimeError("The backend update marker points outside the installation")
    if _inside(destination, backup_directory) or _inside(backup_directory, destination):
        raise RuntimeError("The backend update rollback and installation directories overlap")
    if not destination.is_dir() or not backup_directory.is_dir():
        raise RuntimeError("The backend update rollback directories are missing")
    entries = metadata.get("files")
    release_files = metadata.get("release_files")
    if not isinstance(entries, list) or not entries or not isinstance(release_files, list):
        raise RuntimeError("The backend update marker does not contain a complete file manifest")
    contents = {}
    names = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise RuntimeError("The backend update file manifest is invalid")
        name = entry.get("name")
        if not _safe_file_name(name) or name in contents:
            raise RuntimeError("The backend update file manifest contains an unsafe or duplicate name")
        present = entry.get("present")
        size = entry.get("size")
        digest = entry.get("sha256")
        if type(present) is not bool or type(size) is not int or size < 0:
            raise RuntimeError("The backend update file manifest contains invalid metadata")
        names.append(name)
        target = backup_directory / name
        if present:
            if (not isinstance(digest, str) or len(digest) != 64
                    or any(character not in "0123456789abcdef" for character in digest)):
                raise RuntimeError("The backend update file manifest contains an invalid digest")
            if target.is_symlink() or not target.is_file() or not _inside(target, backup_directory):
                raise RuntimeError(f"Rollback file is missing or unsafe: {name}")
            content = target.read_bytes()
            if len(content) != size or _sha256_bytes(content) != digest:
                raise RuntimeError(f"Rollback file failed its manifest check: {name}")
            contents[name] = content
        else:
            if size != 0 or digest is not None:
                raise RuntimeError("The backend update absent-file manifest is invalid")
            contents[name] = None
    if release_files != names or len(set(release_files)) != len(release_files):
        raise RuntimeError("The backend update marker does not match its full file manifest")
    if set(release_files) != set(FILES):
        raise RuntimeError("The backend update marker does not cover the complete managed release")
    database = metadata.get("database")
    database_backup = metadata.get("database_backup")
    database_path = None
    database_backup_path = None
    database_backup_record = None
    if database is not None:
        if (not isinstance(database, str) or not Path(database).is_absolute()
                or Path(database).is_symlink() or not _inside(database, root)):
            raise RuntimeError("The backend update database path is unsafe")
        database_path = Path(database).resolve()
    if database_backup is not None:
        database_backup_source = Path(database_backup) if isinstance(database_backup, str) else None
        if (phase != MARKER_PHASE_ROLLBACK_READY or database_backup_source is None
                or not database_backup_source.is_absolute() or database_backup_source.is_symlink()
                or not _inside(database_backup_source, root)
                or not _inside(database_backup_source, backup_directory)):
            raise RuntimeError("The backend update database backup path is unsafe")
        database_backup_path = database_backup_source.resolve()
        digest = metadata.get("database_backup_sha256")
        size = metadata.get("database_backup_size")
        if (database_backup_path.is_symlink()
                or not isinstance(digest, str) or len(digest) != 64
                or any(character not in "0123456789abcdef" for character in digest)
                or type(size) is not int or size < 0):
            raise RuntimeError("The pre-update database backup failed its manifest check")
        try:
            descriptor = _open_verified_backup(database_backup_path, size, digest)
        except (OSError, RuntimeError) as exc:
            raise RuntimeError("The pre-update database backup failed its manifest check") from exc
        else:
            os.close(descriptor)
        database_backup_record = {"path": database_backup_path, "size": size, "sha256": digest}
        if database_path is None:
            raise RuntimeError("The backend update marker has a database backup without a target")
        if database_backup_path == database_path:
            raise RuntimeError("The backend update database backup overlaps its target")
    elif (metadata.get("database_backup_sha256") is not None
          or metadata.get("database_backup_size") is not None):
        raise RuntimeError("The backend update database manifest is incomplete")
    if phase == MARKER_PHASE_ROLLBACK_READY and database_path is not None and database_backup_path is None:
        raise RuntimeError("The rollback-ready marker does not contain its database backup")
    web = _validated_web_recovery(metadata.get("web"), root, backup_directory)
    return destination, contents, database_path, database_backup_record, web


def _read_maintenance(marker):
    try:
        content = Path(marker).read_bytes()
        if len(content) > 1024 * 1024:
            raise RuntimeError("The backend update marker is unexpectedly large")
        return json.loads(content)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("The backend update marker cannot be read safely") from exc


def _backup_database(db, database_backup):
    destination = Path(database_backup)
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".codex-db-backup-", dir=destination.parent)
    os.close(descriptor)
    temporary = Path(temporary)
    try:
        with sqlite3.connect(temporary) as saved:
            db.backup(saved)
            if saved.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise RuntimeError("The pre-update database backup did not pass its integrity check")
        descriptor = os.open(temporary, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary, destination)
        _fsync_directory(destination.parent)
        return _sha256_file(destination), destination.stat().st_size
    finally:
        temporary.unlink(missing_ok=True)


def _restore_database(db, database_backup):
    if database_backup is None:
        return
    source = Path(database_backup)
    if not source.is_file():
        raise RuntimeError("The pre-update database backup is missing")
    with sqlite3.connect(source) as saved:
        if saved.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise RuntimeError("The pre-update database backup is damaged")
        saved.backup(db)
    if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
        raise RuntimeError("The restored database did not pass its integrity check")


def _restore_database_file(database, database_backup):
    database = Path(database)
    source = Path(database_backup["path"])
    descriptor, temporary = tempfile.mkstemp(prefix=".codex-db-restore-", dir=database.parent)
    temporary = Path(temporary)
    source_descriptor = None
    try:
        source_descriptor = _open_verified_backup(
            source, database_backup["size"], database_backup["sha256"],
        )
        with os.fdopen(descriptor, "wb") as target:
            descriptor = None
            _verify_file_descriptor(
                source_descriptor, database_backup["size"], database_backup["sha256"], target,
            )
            os.fchmod(target.fileno(), 0o600)
            target.flush()
            os.fsync(target.fileno())
        with sqlite3.connect(f"file:{temporary}?mode=ro", uri=True) as restored:
            if restored.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise RuntimeError("The staged database restoration did not pass its integrity check")
        for suffix in ("-wal", "-shm"):
            _durable_unlink(Path(str(database) + suffix))
        os.replace(temporary, database)
        _fsync_directory(database.parent)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if source_descriptor is not None:
            os.close(source_descriptor)
        temporary.unlink(missing_ok=True)


def _verify_file_set(destination, contents):
    for name, expected in contents.items():
        target = Path(destination) / name
        if expected is None:
            if target.exists() or target.is_symlink():
                raise RuntimeError(f"A rollback-only file still exists after restoration: {name}")
        elif target.is_symlink() or not target.is_file() or target.read_bytes() != expected:
            raise RuntimeError(f"A backend file did not match the rollback manifest: {name}")


def _restore_web_tree(web):
    if web is None:
        return
    destination = web["destination"]
    parent = destination.parent
    stage = Path(tempfile.mkdtemp(prefix=".codex-web-restore-", dir=parent))
    displaced = Path(tempfile.mkdtemp(prefix=".codex-web-displaced-", dir=parent))
    displaced.rmdir()
    moved = False
    installed = False
    complete = False
    restored_after_error = False
    failed = None
    try:
        for name in web["directories"]:
            (stage / name).mkdir(mode=0o755, parents=True, exist_ok=True)
        for name, content in web["contents"].items():
            target = stage / name
            target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
            _atomic_write(target, content, mode=0o644)
        os.chmod(stage, web["mode"])
        _fsync_tree_directories(stage)
        if destination.exists() or destination.is_symlink():
            if destination.is_symlink() or not destination.is_dir():
                raise RuntimeError("The current browser destination is unsafe")
            os.replace(destination, displaced)
            moved = True
            _fsync_directory(parent)
        os.replace(stage, destination)
        installed = True
        _fsync_directory(parent)
        for name in web["directories"]:
            if not (destination / name).is_dir():
                raise RuntimeError(f"A browser directory was not restored: {name}")
        for name, expected in web["contents"].items():
            target = destination / name
            if target.is_symlink() or not target.is_file() or target.read_bytes() != expected:
                raise RuntimeError(f"A browser file did not match the rollback manifest: {name}")
        complete = True
    except BaseException:
        if moved:
            if installed and (destination.exists() or destination.is_symlink()):
                failed = Path(tempfile.mkdtemp(prefix=".codex-web-failed-", dir=parent))
                failed.rmdir()
                os.replace(destination, failed)
                _fsync_directory(parent)
            if not destination.exists() and not destination.is_symlink():
                os.replace(displaced, destination)
                moved = False
                restored_after_error = True
                _fsync_directory(parent)
        raise
    finally:
        if stage.exists():
            shutil.rmtree(stage)
        if complete and moved and displaced.exists():
            shutil.rmtree(displaced)
            _fsync_directory(parent)
        if failed is not None and failed.exists():
            shutil.rmtree(failed)
            _fsync_directory(parent)
        if restored_after_error:
            _fsync_tree_directories(destination)


def _validated_web_assets(expected):
    if not isinstance(expected, dict) or not expected:
        raise RuntimeError("The browser release manifest is missing")
    validated = {}
    for name, content in expected.items():
        if (not _safe_relative_path(name) or name in validated
                or not isinstance(content, bytes) or not content):
            raise RuntimeError("The browser release manifest contains an unsafe asset")
        validated[name] = content
    return validated


def _verify_web_release(destination, expected, previous=None):
    expected = _validated_web_assets(expected)
    if previous is not None and Path(destination).resolve() != previous["destination"]:
        raise RuntimeError("The browser release destination changed during activation")
    contents = dict(expected)
    directories = set()
    for name in expected:
        parent = Path(name).parent
        while parent != Path("."):
            directories.add(parent.as_posix())
            parent = parent.parent
    for name, content in contents.items():
        if not _safe_relative_path(name):
            raise RuntimeError(f"Unsafe browser release path: {name!r}")
        target = Path(destination) / name
        if target.is_symlink() or not target.is_file() or target.read_bytes() != content:
            raise RuntimeError(f"Published browser asset failed verification: {name}")
    actual_directories = set()
    actual_files = set()
    for path in Path(destination).rglob("*"):
        if path.is_symlink():
            raise RuntimeError("The published browser bundle contains a symbolic link")
        name = path.relative_to(destination).as_posix()
        (actual_directories if path.is_dir() else actual_files).add(name)
    if actual_directories != directories or actual_files != set(contents):
        raise RuntimeError("The published browser bundle does not match its complete release manifest")


def _prune_web_release(destination, expected):
    """CODEX: Remove assets absent from the frozen release so old and new web bundles cannot mix."""
    destination = Path(destination)
    expected = _validated_web_assets(expected)
    for path in sorted(destination.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        if path.is_symlink():
            raise RuntimeError("The published browser bundle contains a symbolic link")
        name = path.relative_to(destination).as_posix()
        if path.is_file() and name not in expected:
            _durable_unlink(path)
        elif path.is_dir():
            try:
                path.rmdir()
            except OSError:
                continue
            _fsync_directory(path.parent)
    _fsync_directory(destination)


def _start_with_retry(action):
    first_error = None
    for _ in range(2):
        try:
            action("start")
            return
        except BaseException as exc:
            first_error = first_error or exc
    raise RuntimeError("Backend services could not be restored after two start attempts") from first_error


def _stop_with_retry(action):
    first_error = None
    for _ in range(2):
        try:
            action("stop")
            return
        except BaseException as exc:
            first_error = first_error or exc
    raise RuntimeError("Backend services could not be stopped safely after two attempts") from first_error


def recover_interrupted_activation(action=service_action, check=None, maintenance_file=None,
                                   recovery_root=None, expected_helper=None):
    """CODEX: Restore a complete prior release before reopening a stale maintenance gate."""
    marker = Path(maintenance_file) if maintenance_file is not None else INSTALLATION / "server/backend-update.maintenance"
    if not marker.exists():
        return False
    metadata = _read_maintenance(marker)
    destination, contents, database, database_backup, web = _validated_recovery(
        metadata, INSTALLATION if recovery_root is None else recovery_root,
        expected_helper=expected_helper,
    )
    try:
        _stop_with_retry(action)
    except BaseException as exc:
        try:
            _start_with_retry(action)
        except BaseException:
            pass
        raise RuntimeError(
            "Interrupted backend update recovery could not stop the managed services safely; "
            "new prompts remain paused"
        ) from exc
    try:
        _replace_with_retry(destination, contents)
        _verify_file_set(destination, contents)
        if database_backup is not None:
            _restore_database_file(database, database_backup)
        _restore_web_tree(web)
    except BaseException as exc:
        raise RuntimeError(
            "Interrupted backend update recovery could not restore the complete previous release; "
            "services remain stopped and new prompts remain paused"
        ) from exc
    try:
        _start_with_retry(action)
    except BaseException as exc:
        raise RuntimeError(
            "Interrupted backend update recovery restored the previous release, but could not restart it; "
            "new prompts remain paused"
        ) from exc
    verify = (lambda: healthy(expected_version=None)) if check is None else check
    if not verify():
        raise RuntimeError(
            "Interrupted backend update recovery restored and restarted the previous release, "
            "but health verification failed; "
            "new prompts remain paused"
        )
    _durable_unlink(marker)
    print("Restored the complete previous backend release and reopened prompt admission.", flush=True)
    return True


def _replace_with_retry(destination, contents):
    first_error = None
    for _ in range(2):
        try:
            replace_files(destination, contents)
            return
        except BaseException as exc:
            first_error = first_error or exc
    raise RuntimeError("Backend files could not be restored after two attempts") from first_error


def activate(db, destination, contents, previous, action=service_action, check=healthy, database_backup=None,
             maintenance_file=None, rollback_directory=None, web_source=None, web_destination=None,
             web_expected=None, rollback_check=None, recovery_helper=None):
    """CODEX: Lock run admission until the idle server stops; never stop a recorded active run."""
    destination = Path(destination)
    if set(contents) != set(previous):
        raise RuntimeError("The new backend and rollback file manifests do not match")
    if maintenance_file is not None and set(contents) != set(FILES):
        raise RuntimeError("A recoverable backend activation must cover the complete managed release")
    for name, content in contents.items():
        if not _safe_file_name(name) or not isinstance(content, bytes):
            raise RuntimeError("The new backend file manifest is unsafe or incomplete")
    for name, content in previous.items():
        if not _safe_file_name(name) or content is not None and not isinstance(content, bytes):
            raise RuntimeError("The previous backend file manifest is unsafe or incomplete")
    web_values = (web_source, web_destination, web_expected)
    if any(value is not None for value in web_values) and not all(value is not None for value in web_values):
        raise RuntimeError("A coordinated browser activation requires source, destination, and expected assets")
    if web_expected is not None:
        web_expected = _validated_web_assets(web_expected)
    marker = Path(maintenance_file) if maintenance_file is not None else None
    recovery_record = None
    if marker is not None:
        if recovery_helper is None:
            raise RuntimeError("A recoverable backend activation requires its immutable recovery helper")
        recovery_record = _recovery_helper_record(recovery_helper, marker.parent.parent)
    if web_source is not None and marker is None:
        raise RuntimeError("A coordinated browser activation requires a durable maintenance marker")
    backup_directory = Path(rollback_directory) if rollback_directory is not None else None
    if marker is not None and marker.exists():
        raise RuntimeError("A previous backend activation requires recovery before another update can begin")
    verify_rollback = (lambda: healthy(expected_version=None)) if rollback_check is None else rollback_check
    stopped = False
    stop_attempted = False
    marker_owned = False
    metadata = None
    web_rollback = None
    db.execute("BEGIN IMMEDIATE")
    try:
        try:
            if active_count(db):
                return False
            if marker is not None:
                if marker.exists():
                    raise RuntimeError("A previous backend activation requires recovery before another update can begin")
                if backup_directory is None:
                    root = marker.parent / "updates"
                    root.mkdir(mode=0o700, parents=True, exist_ok=True)
                    _fsync_directory(root.parent)
                    backup_directory = Path(tempfile.mkdtemp(prefix="before-backend-", dir=root))
                    _fsync_directory(root)
                if (_inside(destination, backup_directory)
                        or _inside(backup_directory, destination)):
                    raise RuntimeError("The backend installation and rollback directories overlap")
                if database_backup is None and _database_path(db) is not None:
                    database_backup = backup_directory / "server.sqlite3"
                if database_backup is not None and not _inside(database_backup, backup_directory):
                    raise RuntimeError("The database rollback file must stay inside the rollback directory")
                entries = _prepare_file_backup(backup_directory, previous)
                web = None
                if web_destination is not None:
                    web = _prepare_tree_backup(web_destination, backup_directory / "web")
                    common = Path(os.path.commonpath([str(destination.resolve()), str(backup_directory.resolve())]))
                    web_rollback = _validated_web_recovery(web, common, backup_directory)
                metadata = _maintenance_metadata(
                    destination, backup_directory, entries, _database_path(db), MARKER_PHASE_PREPARED,
                    web=web, recovery_helper=recovery_record,
                )
                _write_maintenance(marker, metadata)
                marker_owned = True
            stop_attempted = True
            action("stop")
            stopped = True
        finally:
            db.rollback()
    except BaseException:
        # CODEX: A multi-unit stop can fail after stopping one service; restore unchanged code after releasing admission.
        if stop_attempted:
            try:
                _start_with_retry(action)
            except BaseException as exc:
                raise RuntimeError("Backend update was not installed, but the previous services could not be restored after a failed stop") from exc
            if marker_owned:
                try:
                    if not verify_rollback():
                        raise RuntimeError("The previous backend did not recover its health check")
                    _durable_unlink(marker)
                except BaseException as exc:
                    raise RuntimeError(
                        "Backend update was not installed, but restored services did not pass health verification; "
                        "new prompts remain paused"
                    ) from exc
        elif marker_owned:
            _durable_unlink(marker)
        raise
    if stopped:
        backup_ready = False
        try:
            database_digest = None
            database_size = None
            if database_backup is not None:
                database_digest, database_size = _backup_database(db, database_backup)
                backup_ready = True
            if marker is not None:
                metadata = _maintenance_metadata(
                    destination, backup_directory, metadata["files"], _database_path(db),
                    MARKER_PHASE_ROLLBACK_READY,
                    database_backup=database_backup if backup_ready else None,
                    database_backup_sha256=database_digest,
                    database_backup_size=database_size,
                    web=metadata["web"], recovery_helper=recovery_record,
                )
                _write_maintenance(marker, metadata)
            replace_files(destination, contents)
            _verify_file_set(destination, contents)
            action("start")
            if not check():
                raise RuntimeError("Updated backend did not pass its health check")
            if web_source is not None:
                _deploy_web(web_source, web_destination)
                _prune_web_release(web_destination, web_expected)
                _verify_web_release(web_destination, web_expected, web_rollback)
                if not check():
                    raise RuntimeError("Updated backend and browser bundle did not pass their final health check")
            if marker is not None:
                _durable_unlink(marker)
        except BaseException:
            rollback_errors = []
            try:
                _stop_with_retry(action)
            except BaseException as exc:
                rollback_errors.append(("stop", exc))
            if not rollback_errors:
                try:
                    _replace_with_retry(destination, previous)
                    _verify_file_set(destination, previous)
                except BaseException as exc:
                    rollback_errors.append(("files", exc))
                if backup_ready:
                    try:
                        _restore_database(db, database_backup)
                    except BaseException as exc:
                        rollback_errors.append(("database", exc))
                if web_rollback is not None:
                    try:
                        _restore_web_tree(web_rollback)
                    except BaseException as exc:
                        rollback_errors.append(("browser", exc))
            if not rollback_errors or rollback_errors[0][0] == "stop":
                try:
                    _start_with_retry(action)
                except BaseException as exc:
                    rollback_errors.append(("start", exc))
            if not rollback_errors and marker is not None:
                try:
                    if not verify_rollback():
                        raise RuntimeError("The restored backend did not pass its health check")
                except BaseException as exc:
                    rollback_errors.append(("health", exc))
            if not rollback_errors and marker is not None:
                try:
                    _durable_unlink(marker)
                except BaseException as exc:
                    rollback_errors.append(("maintenance marker", exc))
            if rollback_errors:
                details = ", ".join(stage for stage, _ in rollback_errors)
                raise RuntimeError(f"Backend update failed and rollback needs attention ({details}); new prompts remain paused") from rollback_errors[-1][1]
            raise
    return True


def _assert_external_activation():
    """CODEX: Refuse to stop a service from a process that service owns."""
    cgroup = Path("/proc/self/cgroup").read_text(errors="replace")
    unsafe = [service for service in (SERVICE, WORKER_SERVICE) if service in cgroup]
    if unsafe:
        raise RuntimeError(
            "Backend activation must run in its dedicated transient service; "
            f"refusing to stop the owning cgroup ({', '.join(unsafe)})"
        )


def _run_recovery():
    """CODEX: Serialize boot-time and transient recovery against backend activation."""
    _require_unaliased_directory(INSTALLATION, "installed LAN server")
    with _open_activation_lock(INSTALLATION / "backend-update.lock") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("A backend activation is still running; recovery will retry") from exc
        return recover_interrupted_activation(expected_helper=Path(__file__).resolve())


def _queue_activation(wait=False, with_web=False):
    """CODEX: Escape the Worker cgroup before activation can stop either service."""
    _require_unaliased_directory(INSTALLATION, "installed LAN server")
    marker = INSTALLATION / "server/backend-update.maintenance"
    lock_path = INSTALLATION / "backend-update.lock"

    def queue_recovery():
        subprocess.run(
            ["systemctl", "--user", "start", "--no-block", RECOVERY_SERVICE],
            check=True, timeout=30,
        )
        raise RuntimeError(
            "A previous backend activation is being recovered with its matching helper; "
            "retry this update after recovery completes"
        )

    if marker.exists():
        queue_recovery()
    with _open_activation_lock(lock_path) as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(
                "A backend activation is already running; its recovery helper was left unchanged"
            ) from exc
        if marker.exists():
            recovery_helper = None
        else:
            recovery_helper = _install_recovery_unit()
    if recovery_helper is None:
        queue_recovery()
    recovery = _systemd_command(
        INSTALLATION / "venv/bin/python", recovery_helper, RECOVERY_FLAG,
    )
    command = [
        "systemd-run", "--user", "--collect",
    ]
    if wait:
        command.append("--wait")
    command.extend([
        f"--unit={UPDATE_SERVICE}",
        "--property=Type=exec",
        f"--property=TimeoutStopSec={RECOVERY_TIMEOUT_SECONDS}",
        f"--property=ExecStopPost={recovery}",
        sys.executable, str(Path(__file__).resolve()), ACTIVATION_FLAG,
        f"{RECOVERY_HELPER_FLAG}{recovery_helper}",
    ])
    if with_web:
        command.append(WEB_FLAG)
    subprocess.run(command, check=True,
                   timeout=ACTIVATION_WAIT_SECONDS + RECOVERY_TIMEOUT_SECONDS + 120 if wait else 30)
    if wait:
        print(f"Backend activation in {UPDATE_SERVICE} completed successfully.", flush=True)
        return
    print(
        f"Backend activation queued in {UPDATE_SERVICE}; it will wait for every prompt to finish. "
        f"Follow it with: journalctl --user -fu {UPDATE_SERVICE}",
        flush=True,
    )


def activate_release(db, destination, contents, previous, *, web_source=None, web_destination=None, **kwargs):
    """CODEX: Keep backend and browser publication inside one recoverable activation."""
    expected = _read_web_assets(web_source) if web_source is not None else None
    return activate(
        db, destination, contents, previous, web_source=web_source,
        web_destination=web_destination, web_expected=expected, **kwargs,
    )


def _run_activation(with_web=False, recovery_helper=None):
    """CODEX: Perform the destructive activation only from the detached updater service."""
    _assert_external_activation()
    destination = INSTALLATION / "code/codex_lan"
    database = INSTALLATION / "server/server.sqlite3"
    marker = INSTALLATION / "server/backend-update.maintenance"
    backups = INSTALLATION / "updates"
    lock_path = INSTALLATION / "backend-update.lock"
    recovery_directory = INSTALLATION / "recovery"
    guarded_paths = (INSTALLATION, destination, database.parent, marker.parent, backups.parent,
                     recovery_directory)
    if any(path.absolute() != path.resolve() for path in guarded_paths):
        raise RuntimeError("The installed LAN server contains a symbolic-link path")
    if (destination.is_symlink() or not destination.is_dir()
            or database.is_symlink() or not database.is_file()
            or lock_path.is_symlink()
            or recovery_helper is None
            or any((destination / name).is_symlink() for name in FILES)):
        raise RuntimeError("The installed LAN server was not found")
    recovery_record = _recovery_helper_record(recovery_helper, INSTALLATION)
    if recovery_record["sha256"] != _sha256_file(Path(__file__).resolve()):
        raise RuntimeError("The activation child does not match its immutable recovery helper")
    with _open_activation_lock(lock_path) as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if marker.exists():
            print("Recovering the previous interrupted backend activation before updating.", flush=True)
            recover_interrupted_activation(
                maintenance_file=marker, expected_helper=recovery_record["path"],
            )
        source = source_file(source_root(__file__), "codex_lan")
        web_source = source / "web" if with_web else None
        if with_web:
            # CODEX: Validate the frozen web release before stopping anything or waiting on runs.
            _read_web_assets(web_source)
        contents = {name: (source / name).read_bytes() for name in FILES}
        for name, content in contents.items():
            compile(content, name, "exec")
        previous = {name: (destination / name).read_bytes() if (destination / name).exists() else None
                    for name in FILES}
        backups.mkdir(mode=0o700, exist_ok=True)
        if backups.is_symlink() or backups.absolute() != backups.resolve():
            raise RuntimeError("The backend rollback directory is a symbolic link")
        _fsync_directory(backups.parent)
        backup = Path(tempfile.mkdtemp(prefix="before-backend-", dir=backups))
        _fsync_directory(backups)
        print(f"Waiting for every prompt to finish. Rollback files: {backup}", flush=True)
        db = sqlite3.connect(f"file:{database}?mode=rw", uri=True, timeout=5, isolation_level=None)
        try:
            deadline = time.monotonic() + ACTIVATION_WAIT_SECONDS
            while time.monotonic() < deadline:
                if not active_count(db) and activate_release(
                        db, destination, contents, previous, database_backup=backup / "server.sqlite3",
                        maintenance_file=marker,
                        rollback_directory=backup,
                        recovery_helper=recovery_record["path"],
                        web_source=web_source, web_destination=destination / "web" if with_web else None):
                    try:
                        _install_restart_policy_dropins()
                    except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
                        # CODEX: The external monitor still protects the server; report incomplete unit migration.
                        print(f"Warning: backend is healthy but legacy service restart policy was not upgraded: {exc}",
                              file=sys.stderr, flush=True)
                    try:
                        _install_health_monitor_unit(start=True)
                    except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
                        # CODEX: The backend is already verified; preserve it and make monitor failure explicit.
                        print(f"Warning: backend is healthy but its external health monitor did not start: {exc}",
                              file=sys.stderr, flush=True)
                    print("Updated backend activated and health check passed." +
                          (" Matching browser bundle published." if with_web else ""), flush=True)
                    return
                # CODEX: activate() rechecks under a write lock, so an extra idle grace only starves frequent chats.
                time.sleep(0.5)
            raise RuntimeError("Update left unapplied: prompts remained active for one hour")
        finally:
            db.close()


def main(argv=None):
    """CODEX: Queue normal invocations; reserve in-process activation for the transient service."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments in (["--help"], ["-h"]):
        print(f"Usage: {Path(__file__).name} [{WEB_FLAG}]")
        print("Queue a guarded backend update; --with-web includes matching browser assets.")
        return
    helper_arguments = [argument for argument in arguments
                        if argument.startswith(RECOVERY_HELPER_FLAG)]
    plain_arguments = [argument for argument in arguments
                       if not argument.startswith(RECOVERY_HELPER_FLAG)]
    if (len(arguments) != len(set(arguments)) or len(helper_arguments) > 1
            or set(plain_arguments) - {ACTIVATION_FLAG, RECOVERY_FLAG, WEB_FLAG}
            or RECOVERY_FLAG in arguments and arguments != [RECOVERY_FLAG]):
        raise SystemExit(f"Usage: {Path(__file__).name} [{WEB_FLAG}]")
    with_web = WEB_FLAG in arguments
    if RECOVERY_FLAG in arguments:
        _run_recovery()
        return
    if ACTIVATION_FLAG in arguments:
        if len(helper_arguments) != 1 or not helper_arguments[0][len(RECOVERY_HELPER_FLAG):]:
            raise SystemExit(f"Usage: {Path(__file__).name} [{WEB_FLAG}]")
        keyword_arguments = {
            "recovery_helper": Path(helper_arguments[0][len(RECOVERY_HELPER_FLAG):]),
        }
        if with_web:
            keyword_arguments["with_web"] = True
        _run_activation(**keyword_arguments)
        return
    if helper_arguments:
        raise SystemExit(f"Usage: {Path(__file__).name} [{WEB_FLAG}]")
    _queue_activation(**({"with_web": True} if with_web else {}))


if __name__ == "__main__":
    main()
