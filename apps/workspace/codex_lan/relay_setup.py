"""Administrator-controlled SSH rendezvous with a one-time password bootstrap.

The relay carries TLS traffic; it never receives the application database or
the host's Codex credential. Passwords are accepted only for provisioning a
dedicated forwarding-only SSH account and are never persisted.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import selectors
import shlex
import shutil
import stat
import subprocess
import time
from pathlib import Path


UNIT = "codex-lan-ssh-relay.service"
RELAY_USER = "codex-lan-relay"
FORWARD = "127.0.0.1:18767:127.0.0.2:8767"
UNIT_MARKER = "# Managed by Codex-LAN Host admin."
FINGERPRINT = re.compile(r"^SHA256:[A-Za-z0-9+/]{43}$")
USER = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,31}$")
HOST_LABEL = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
PUBKEY = re.compile(r"^ssh-ed25519 ([A-Za-z0-9+/]+={0,2})(?: [A-Za-z0-9_.-]+)?$")


def validate_server(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 253:
        raise ValueError("Enter a valid relay hostname or IPv4 address.")
    if value.startswith("-") or value.endswith(".") or any(not HOST_LABEL.fullmatch(label) for label in value.split(".")):
        raise ValueError("Enter a valid relay hostname or IPv4 address.")
    return value


def validate_user(value, *, bootstrap=False):
    if not isinstance(value, str) or not USER.fullmatch(value) or value.startswith("-"):
        raise ValueError("Enter a valid SSH username.")
    if bootstrap and value != "root":
        raise ValueError("One-time setup currently requires the relay server's root SSH account.")
    return value


def validate_fingerprint(value):
    if not isinstance(value, str) or not FINGERPRINT.fullmatch(value):
        raise ValueError("Enter the independently verified SHA-256 SSH host-key fingerprint.")
    return value


def _run(args, *, data=None, timeout=15, pass_fds=(), capture=True):
    try:
        if not capture:
            options = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
                       "timeout": timeout, "check": False, "pass_fds": pass_fds}
            if data is None:
                options["stdin"] = subprocess.DEVNULL
            else:
                options["input"] = data
            return subprocess.run(args, **options)

        # Do not let a misbehaving SSH endpoint fill server memory with stdout.
        process = subprocess.Popen(args, stdin=subprocess.PIPE if data is not None else subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, pass_fds=pass_fds)
        try:
            deadline = time.monotonic() + timeout
            chunks = []
            size = 0
            offset = 0
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                if data:
                    selector.register(process.stdin, selectors.EVENT_WRITE)
                elif process.stdin:
                    process.stdin.close()
                while selector.get_map():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise subprocess.TimeoutExpired(args, timeout)
                    for key, _ in selector.select(remaining):
                        if key.fileobj is process.stdout:
                            chunk = os.read(process.stdout.fileno(), 4096)
                            if not chunk:
                                selector.unregister(process.stdout)
                            else:
                                size += len(chunk)
                                if size > 65536:
                                    raise ValueError("The SSH relay command returned too much data.")
                                chunks.append(chunk)
                        else:
                            try:
                                offset += os.write(process.stdin.fileno(), data[offset:offset + 4096])
                            except BrokenPipeError:
                                offset = len(data)
                            if offset >= len(data):
                                selector.unregister(process.stdin)
                                process.stdin.close()
            process.wait(timeout=max(0.1, deadline - time.monotonic()))
            return subprocess.CompletedProcess(args, process.returncode, b"".join(chunks))
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            if process.stdout:
                process.stdout.close()
            if process.stdin and not process.stdin.closed:
                process.stdin.close()
    except (OSError, subprocess.SubprocessError):
        raise ValueError("The SSH relay operation could not complete. Check its prerequisites and retry.") from None


def _private_directory(path):
    path = Path(path)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink() or path.resolve() != path.absolute() or path.stat().st_uid != os.getuid():
        raise ValueError("The relay directory must be owned by the host user without symbolic links.")
    if stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise ValueError("The relay directory must not be accessible to other users.")
    return path


def _private_file(path):
    if path.is_symlink() or not path.is_file() or path.stat().st_uid != os.getuid():
        raise ValueError("The relay key or trust file is missing or unsafe.")
    if stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise ValueError("The relay key or trust file is accessible to other users.")


def _exclusive_file(path, content):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        Path(path).unlink(missing_ok=True)
        raise


def _public_key(private_path):
    _private_file(private_path)
    result = _run(["ssh-keygen", "-y", "-P", "", "-f", str(private_path)], timeout=8)
    if result.returncode:
        raise ValueError("The relay identity is unavailable or protected by a passphrase.")
    line = result.stdout.decode("ascii", "replace").strip()
    _validate_public_key(line)
    return line


def _validate_public_key(line):
    match = PUBKEY.fullmatch(line)
    if not match:
        raise ValueError("The relay identity has an unexpected public-key format.")
    try:
        if len(base64.b64decode(match.group(1), validate=True)) != 51:
            raise ValueError()
    except ValueError:
        raise ValueError("The relay identity has an unexpected public-key format.") from None


def _saved_public_key(directory):
    path = Path(directory) / "id_ed25519.pub"
    _private_file(path)
    line = path.read_text(encoding="ascii").strip()
    _validate_public_key(line)
    return line


def _ensure_key(directory):
    directory = _private_directory(directory)
    path = directory / "id_ed25519"
    if path.exists() or path.is_symlink():
        return path, _public_key(path)
    result = _run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "codex-lan-relay",
                   "-f", str(path)], timeout=15)
    if result.returncode:
        raise ValueError("Could not create the relay identity.")
    (directory / "id_ed25519.pub").chmod(0o600)
    return path, _public_key(path)


def probe_host(server):
    """Return an *untrusted* observed key; the operator must verify it out of band."""
    server = validate_server(server)
    result = _run(["ssh-keyscan", "-T", "5", "-t", "ed25519", server], timeout=10)
    if result.returncode and not result.stdout:
        raise ValueError("Could not read the relay's SSH host key.")
    if len(result.stdout) > 8192:
        raise ValueError("The relay returned an invalid SSH host key.")
    keys = set()
    for line in result.stdout.decode("ascii", "replace").splitlines():
        if line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) != 3 or parts[1] != "ssh-ed25519":
            continue
        try:
            raw = base64.b64decode(parts[2], validate=True)
        except ValueError:
            continue
        if len(raw) <= 128:
            keys.add(parts[2])
    if len(keys) != 1:
        raise ValueError("The relay did not present one unambiguous Ed25519 SSH host key.")
    key = next(iter(keys))
    known_host_line = f"{server} ssh-ed25519 {key}\n"
    printed = _run(["ssh-keygen", "-lf", "-", "-E", "sha256"], data=known_host_line.encode(), timeout=8)
    match = re.search(rb"(SHA256:[A-Za-z0-9+/]{43})(?:\s|$)", printed.stdout) if printed.returncode == 0 else None
    if not match:
        raise ValueError("Could not verify the relay's SSH host-key format.")
    return known_host_line, match.group(1).decode("ascii")


def _known_hosts_path(directory, server):
    suffix = hashlib.sha256(server.encode("ascii")).hexdigest()[:16]
    return Path(directory) / f"known_hosts-{suffix}"


def _pin_host(directory, server, expected):
    line, observed = probe_host(server)
    if observed != validate_fingerprint(expected):
        raise ValueError("The SSH host key does not match the independently verified fingerprint. No password was used.")
    directory = _private_directory(directory)
    path = _known_hosts_path(directory, server)
    if path.exists() or path.is_symlink():
        _private_file(path)
        if path.read_bytes() != line.encode("ascii"):
            raise ValueError("A different SSH host key is already pinned for this relay. Review it manually.")
    else:
        _exclusive_file(path, line.encode("ascii"))
    return path


def _ssh_args(server, user, known_hosts, identity=None, *, password=False):
    args = ["ssh", "-F", "/dev/null", "-o", f"UserKnownHostsFile={known_hosts}",
            "-o", "GlobalKnownHostsFile=/dev/null", "-o", "StrictHostKeyChecking=yes",
            "-o", "UpdateHostKeys=no", "-o", "CheckHostIP=no", "-o", "ConnectTimeout=10",
            "-o", "ConnectionAttempts=1", "-o", "NumberOfPasswordPrompts=1",
            "-o", "ForwardAgent=no", "-o", "ForwardX11=no", "-o", "ControlMaster=no"]
    if password:
        args += ["-o", "PreferredAuthentications=password", "-o", "KbdInteractiveAuthentication=no",
                 "-o", "PubkeyAuthentication=no", "-o", "BatchMode=no"]
    else:
        args += ["-o", "IdentitiesOnly=yes", "-i", str(identity), "-o", "BatchMode=yes"]
    return args + [f"{user}@{server}"]


# This runs on the *relay* only after the independently verified host key has
# matched and one password-authenticated root session has succeeded. It refuses
# pre-existing account/configuration paths and does not install a root key.
REMOTE_PROVISION = r'''import base64, os, pathlib, pwd, subprocess
USER = "codex-lan-relay"
HOME = pathlib.Path("/var/lib/codex-lan-relay")
CONFIG = pathlib.Path("/etc/ssh/sshd_config.d/60-codex-lan-relay.conf")
KEY = __PUBLIC_KEY__
def run(*args):
    return subprocess.run(args, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20).stdout
if os.geteuid() != 0:
    raise SystemExit(10)
parts = KEY.split()
if len(parts) < 2 or parts[0] != "ssh-ed25519" or len(base64.b64decode(parts[1], validate=True)) != 51:
    raise SystemExit(11)
try:
    pwd.getpwnam(USER)
except KeyError:
    pass
else:
    raise SystemExit(12)
if CONFIG.exists() or CONFIG.is_symlink() or HOME.exists() or HOME.is_symlink():
    raise SystemExit(13)
run("/usr/sbin/sshd", "-t")
baseline = run("/usr/sbin/sshd", "-T", "-C", "user=root,host=localhost,addr=127.0.0.1")
other_baseline = run("/usr/sbin/sshd", "-T", "-C", "user=codex-lan-unrelated,host=localhost,addr=127.0.0.1")
run("useradd", "--system", "--create-home", "--home-dir", str(HOME), "--shell", "/usr/sbin/nologin", USER)
created_config = False
try:
    account = pwd.getpwnam(USER)
    HOME.chmod(0o700)
    sshdir = HOME / ".ssh"
    sshdir.mkdir(mode=0o700)
    os.chown(sshdir, account.pw_uid, account.pw_gid)
    config = """# Codex-LAN restricted SSH rendezvous; only this account is affected.
Match User codex-lan-relay
    AuthenticationMethods publickey
    PasswordAuthentication no
    KbdInteractiveAuthentication no
    PubkeyAuthentication yes
    AuthorizedKeysFile .ssh/authorized_keys
    AllowTcpForwarding remote
    PermitListen 127.0.0.1:18767
    PermitOpen none
    GatewayPorts no
    AllowStreamLocalForwarding no
    AllowAgentForwarding no
    X11Forwarding no
    PermitTunnel no
    PermitTTY no
    PermitUserRC no
    MaxSessions 0
Match all
"""
    fd = os.open(CONFIG, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    created_config = True
    with os.fdopen(fd, "w") as stream:
        stream.write(config)
        stream.flush()
        os.fsync(stream.fileno())
    run("/usr/sbin/sshd", "-t")
    if run("/usr/sbin/sshd", "-T", "-C", "user=root,host=localhost,addr=127.0.0.1") != baseline:
        raise RuntimeError("Existing SSH configuration changed")
    if run("/usr/sbin/sshd", "-T", "-C", "user=codex-lan-unrelated,host=localhost,addr=127.0.0.1") != other_baseline:
        raise RuntimeError("Other SSH users' configuration changed")
    effective = dict(line.split(" ", 1) for line in run("/usr/sbin/sshd", "-T", "-C", "user=codex-lan-relay,host=localhost,addr=127.0.0.1").decode().splitlines())
    required = {"maxsessions": "0", "allowtcpforwarding": "remote", "permitlisten": "127.0.0.1:18767",
                "permitopen": "none", "gatewayports": "no", "passwordauthentication": "no",
                "allowstreamlocalforwarding": "no", "authenticationmethods": "publickey"}
    if any(effective.get(k) != v for k, v in required.items()):
        raise RuntimeError("Relay restrictions were not applied")
    authorized = sshdir / "authorized_keys"
    fd = os.open(authorized, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write('restrict,port-forwarding,permitlisten="127.0.0.1:18767" ' + KEY + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.chown(authorized, account.pw_uid, account.pw_gid)
    try:
        run("systemctl", "reload", "ssh.service")
    except Exception:
        run("systemctl", "reload", "sshd.service")
except BaseException:
    if created_config:
        CONFIG.unlink(missing_ok=True)
        try:
            run("systemctl", "reload", "ssh.service")
        except Exception:
            try:
                run("systemctl", "reload", "sshd.service")
            except Exception:
                pass
    try:
        run("userdel", "--remove", USER)
    except Exception:
        pass
    raise
'''


def _password_provision(server, admin_user, password, known_hosts, public_key):
    if (not isinstance(password, str) or not 1 <= len(password) <= 1024
            or any(ord(char) < 32 or ord(char) == 127 for char in password)):
        raise ValueError("Enter the relay administrator password for this one-time setup.")
    try:
        password_bytes = password.encode("utf-8")
    except UnicodeError:
        raise ValueError("Enter the relay administrator password for this one-time setup.") from None
    if shutil.which("sshpass") is None:
        raise ValueError("One-time password setup needs the sshpass package on this host.")
    read_fd, write_fd = os.pipe()
    try:
        os.write(write_fd, password_bytes + b"\n")
        os.close(write_fd)
        write_fd = -1
        script = REMOTE_PROVISION.replace("__PUBLIC_KEY__", json.dumps(public_key))
        args = ["sshpass", "-d", str(read_fd), *_ssh_args(server, admin_user, known_hosts, password=True),
                "python3", "-"]
        result = _run(args, data=script.encode("utf-8"), timeout=75, pass_fds=(read_fd,), capture=False)
        if result.returncode:
            raise ValueError("Relay account setup failed. Check SSH login and server prerequisites; the password was not saved.")
    finally:
        if write_fd >= 0:
            os.close(write_fd)
        os.close(read_fd)


def _verify_tunnel(server, known_hosts, identity, directory):
    """Open a temporary key-only reverse forward; SSH -f exits only after forwarding succeeds."""
    socket_path = Path(directory) / ("verify-" + hashlib.sha256(os.urandom(16)).hexdigest()[:12])
    args = _ssh_args(server, RELAY_USER, known_hosts, identity)
    target = args.pop()
    verify = [*args, "-M", "-S", str(socket_path), "-fNT", "-o", "ExitOnForwardFailure=yes",
              "-R", FORWARD, target]
    result = _run(verify, timeout=18, capture=False)
    if result.returncode:
        raise ValueError("The relay key or restricted reverse tunnel was rejected; local settings were not changed.")
    try:
        close = ["ssh", "-F", "/dev/null", "-S", str(socket_path), "-O", "exit", target]
        closed = _run(close, timeout=8, capture=False)
        if closed.returncode:
            raise ValueError("Could not close the temporary verification tunnel; local settings were not changed.")
    except ValueError:
        # Keep the private control socket so the operator can close an orphaned tunnel.
        raise
    else:
        socket_path.unlink(missing_ok=True)


def _unit_path():
    return Path.home() / ".config/systemd/user" / UNIT


def _unit_info():
    path = _unit_path()
    if not path.exists() and not path.is_symlink():
        return {"configured": False, "manageable": True, "managed": False, "user": None, "server": None}
    if path.is_symlink() or not path.is_file() or path.stat().st_uid != os.getuid():
        return {"configured": True, "manageable": False, "managed": False, "user": None, "server": None}
    try:
        if path.stat().st_size > 16384:
            raise ValueError()
        content = path.read_text(encoding="utf-8")
        exec_lines = [line[len("ExecStart="):] for line in content.splitlines() if line.startswith("ExecStart=")]
        if len(exec_lines) != 1:
            raise ValueError()
        words = shlex.split(exec_lines[0])
        target = words[-1]
        user, server = target.split("@", 1)
        validate_user(user)
        validate_server(server)
        # Legacy publisher units are recognized for safe start/stop but never silently replaced.
        recognizable = words[0].endswith("/ssh") and ("-NT" in words or "-fNT" in words)
        recognizable = recognizable and FORWARD in words and "-R" in words
        recognizable = recognizable and "StrictHostKeyChecking=yes" in words and "BatchMode=yes" in words
        if not recognizable:
            raise ValueError()
        return {"configured": True, "manageable": True, "managed": UNIT_MARKER in content,
                "user": user, "server": server}
    except (OSError, ValueError, IndexError):
        return {"configured": True, "manageable": False, "managed": False, "user": None, "server": None}


def _systemctl(*args):
    result = _run(["systemctl", "--user", *args], timeout=10, capture=False)
    if result.returncode:
        raise ValueError("The user systemd session could not apply the relay setting.")


def status(data_dir):
    info = _unit_info()
    try:
        result = _run(["systemctl", "--user", "show", UNIT, "--no-pager", "--property=LoadState",
                       "--property=ActiveState", "--property=SubState", "--property=UnitFileState",
                       "--property=NRestarts"], timeout=8)
    except ValueError:
        result = subprocess.CompletedProcess([], 127, b"")
    fields = dict(line.split("=", 1) for line in result.stdout.decode("utf-8", "replace").splitlines() if "=" in line)
    active = fields.get("ActiveState") == "active"
    enabled = fields.get("UnitFileState") in {"enabled", "enabled-runtime"}
    directory = Path(data_dir).parent / "ssh-relay"
    try:
        public_key = _saved_public_key(directory)
    except (OSError, ValueError):
        public_key = None
    server = info["server"]
    pin = _known_hosts_path(directory, server) if server else None
    host_key_fingerprint = None
    if pin and pin.exists() and not pin.is_symlink():
        try:
            _private_file(pin)
            printed = _run(["ssh-keygen", "-lf", str(pin), "-E", "sha256"], timeout=8)
            match = re.search(rb"(SHA256:[A-Za-z0-9+/]{43})(?:\s|$)", printed.stdout)
            if match:
                host_key_fingerprint = match.group(1).decode("ascii")
        except (OSError, ValueError):
            pass
    return {**info, "supported": all(shutil.which(name) for name in ("ssh", "ssh-keygen", "ssh-keyscan", "systemctl")),
            "bootstrap_available": shutil.which("sshpass") is not None,
            "active": active, "enabled": enabled, "state": fields.get("SubState", "unknown") if result.returncode == 0 else "unavailable",
            "public_key": public_key, "host_key_fingerprint": host_key_fingerprint,
            "remote_port": 18767, "local_origin": "https://127.0.0.2:8767",
            "auth_method": "ssh_key", "password_saved": False,
            "error": None if info["manageable"] else "An existing relay unit needs manual review before Host admin can manage it.",
            "reachability": "SSH process state only; remote browser access is not independently verified."}


def render_unit(server, known_hosts, identity):
    validate_server(server)
    _private_file(known_hosts)
    _private_file(identity)
    ssh_binary = shutil.which("ssh")
    if not ssh_binary or any(not re.fullmatch(r"/[A-Za-z0-9_./-]+", str(path))
                             for path in (ssh_binary, known_hosts, identity)):
        raise ValueError("The host installation path cannot be represented safely in a systemd relay unit.")
    # All interpolated tokens are bounded ASCII without systemd specifiers or whitespace.
    return (f"{UNIT_MARKER}\n[Unit]\nDescription=Private SSH rendezvous for Codex-LAN\n"
            "After=network-online.target\nStartLimitIntervalSec=0\n\n[Service]\nType=simple\n"
            f"ExecStart={ssh_binary} -NT -F /dev/null -i {identity} -o IdentitiesOnly=yes "
            f"-o BatchMode=yes -o UserKnownHostsFile={known_hosts} -o GlobalKnownHostsFile=/dev/null "
            "-o StrictHostKeyChecking=yes -o UpdateHostKeys=no -o CheckHostIP=no "
            "-o ExitOnForwardFailure=yes -o ServerAliveInterval=20 -o ServerAliveCountMax=3 "
            f"-o ConnectTimeout=10 -R {FORWARD} {RELAY_USER}@{server}\n"
            "Restart=always\nRestartSec=5\nTimeoutStopSec=10\nUMask=0077\n\n"
            "[Install]\nWantedBy=default.target\n")


def _service_state():
    result = _run(["systemctl", "--user", "show", UNIT, "--no-pager", "--property=ActiveState",
                   "--property=UnitFileState"], timeout=8)
    fields = dict(line.split("=", 1) for line in result.stdout.decode("utf-8", "replace").splitlines() if "=" in line)
    return fields.get("ActiveState") == "active", fields.get("UnitFileState") in {"enabled", "enabled-runtime"}


def _replace_unit(content, *, verify, replace_existing=False):
    from .host_setup import private_unit
    info = _unit_info()
    if not info["manageable"]:
        raise ValueError("An existing relay unit cannot be managed safely from this page.")
    if info["configured"] and not info["managed"] and not replace_existing:
        raise ValueError("Confirm migration of the existing relay unit before replacing it.")
    path = _unit_path()
    previous = path.read_bytes() if info["configured"] else None
    was_active, was_enabled = _service_state()
    replaced = False
    try:
        if was_active:
            _systemctl("stop", UNIT)
        verify()
        private_unit(path, content)
        replaced = True
        _systemctl("daemon-reload")
        _systemctl("enable", "--now", UNIT)
        time.sleep(1)
        _systemctl("is-active", "--quiet", UNIT)
    except (OSError, ValueError):
        if replaced:
            try:
                _systemctl("disable", "--now", UNIT)
            except ValueError:
                pass
            if previous is None:
                path.unlink(missing_ok=True)
            else:
                private_unit(path, previous.decode("utf-8"))
        try:
            if replaced:
                _systemctl("daemon-reload")
            if was_enabled:
                _systemctl("enable", UNIT)
            elif replaced:
                _systemctl("disable", UNIT)
            if was_active:
                _systemctl("start", UNIT)
        except ValueError:
            pass
        raise ValueError("Relay configuration or verification failed. The previous local unit was restored where possible.") from None


def bootstrap(data_dir, *, server, admin_user, password, host_key_fingerprint, replace_existing=False):
    server = validate_server(server)
    admin_user = validate_user(admin_user, bootstrap=True)
    validate_fingerprint(host_key_fingerprint)
    if not isinstance(replace_existing, bool):
        raise ValueError("Invalid relay migration confirmation.")
    info = _unit_info()
    if not info["manageable"] or info["configured"] and not info["managed"] and not replace_existing:
        raise ValueError("Confirm migration of the existing relay unit before replacing it.")
    directory = _private_directory(Path(data_dir).parent / "ssh-relay")
    known_hosts = _pin_host(directory, server, host_key_fingerprint)
    identity, public_key = _ensure_key(directory)
    _password_provision(server, admin_user, password, known_hosts, public_key)
    try:
        _replace_unit(render_unit(server, known_hosts, identity),
                      verify=lambda: _verify_tunnel(server, known_hosts, identity, directory),
                      replace_existing=replace_existing)
    except (OSError, ValueError):
        raise ValueError("The remote relay account may already be prepared, but the local tunnel could not be enabled. "
                         "Review the relay status and key-only configuration before retrying password setup.") from None


def configure(data_dir, *, server, user, replace_existing=False):
    server = validate_server(server)
    user = validate_user(user)
    if user != RELAY_USER:
        raise ValueError("Use the dedicated codex-lan-relay account for unattended connection.")
    if not isinstance(replace_existing, bool):
        raise ValueError("Invalid relay migration confirmation.")
    directory = Path(data_dir).parent / "ssh-relay"
    identity = directory / "id_ed25519"
    _public_key(identity)
    known_hosts = _known_hosts_path(directory, server)
    _private_file(known_hosts)
    _replace_unit(render_unit(server, known_hosts, identity),
                  verify=lambda: _verify_tunnel(server, known_hosts, identity, directory),
                  replace_existing=replace_existing)


def connect():
    info = _unit_info()
    if not info["configured"] or not info["manageable"]:
        raise ValueError("No safe relay service is configured on this host.")
    _systemctl("enable", "--now", UNIT)


def disconnect():
    info = _unit_info()
    if not info["configured"] or not info["manageable"]:
        raise ValueError("No safe relay service is configured on this host.")
    _systemctl("disable", "--now", UNIT)
