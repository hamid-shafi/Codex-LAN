"""CODEX: Validate HTTPS on an explicit private interface or the SSH tunnel loopback."""

from __future__ import annotations

import ipaddress
import json
import os
import ssl
import stat
from pathlib import Path
from typing import Any


PRIVATE_NETWORKS = tuple(ipaddress.IPv4Network(value) for value in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
))
# CODEX: A separate loopback host isolates client cookies from their local 127.0.0.1 workspace.
SSH_TUNNEL_ADDRESS = "127.0.0.2"
NETWORK_FIELDS = {"lan_address", "port", "cert", "key", "ca", "public_origin"}


def validate_address(address: str) -> str:
    """CODEX: Allow RFC1918 or exactly 127.0.0.2 for SSH; reject wildcard/public binds."""
    if not isinstance(address, str):
        raise ValueError("HTTPS address must be an explicit private IPv4 address or 127.0.0.2 for SSH tunnels")
    try:
        parsed = ipaddress.IPv4Address(address)
    except (ValueError, TypeError):
        raise ValueError("HTTPS address must be an explicit private IPv4 address or 127.0.0.2 for SSH tunnels") from None
    if str(parsed) != SSH_TUNNEL_ADDRESS and not any(parsed in network for network in PRIVATE_NETWORKS):
        raise ValueError("HTTPS address must be within 10/8, 172.16/12, 192.168/16, or exactly 127.0.0.2")
    return str(parsed)


def validate_port(port: int) -> int:
    if isinstance(port, bool) or not isinstance(port, int) or not 1024 <= port <= 65535 or port == 8765:
        raise ValueError("HTTPS port must be 1024–65535 and different from loopback port 8765")
    return port


def check_file(path: Path, *, private: bool = False) -> None:
    """CODEX: Reject symlinks and writable foreign TLS material before loading credentials."""
    path = Path(path)
    try:
        metadata = path.lstat()
    except OSError:
        raise ValueError(f"Required HTTPS file is unavailable: {path.name}") from None
    if not stat.S_ISREG(metadata.st_mode) or path.is_symlink():
        raise ValueError(f"HTTPS file must be a regular file: {path.name}")
    if metadata.st_uid != os.getuid():
        raise ValueError(f"HTTPS file must belong to the current user: {path.name}")
    permissions = stat.S_IMODE(metadata.st_mode)
    if not permissions & stat.S_IRUSR or permissions & (0o077 if private else 0o022):
        raise ValueError(f"HTTPS file permissions are unsafe: {path.name}")
    for parent in path.parents:
        if parent.is_symlink():
            raise ValueError("HTTPS files must not be accessed through directory symlinks")


def load_network(data_dir: Path) -> dict[str, Any] | None:
    """CODEX: Return validated paths and origin; missing configuration retains tunnel-only mode."""
    data_dir = Path(data_dir).absolute()
    filename = data_dir / "network.json"
    if not filename.exists() and not filename.is_symlink():
        return None
    check_file(filename, private=True)
    try:
        raw = json.loads(filename.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        raise ValueError("HTTPS network.json is not valid JSON") from None
    if not isinstance(raw, dict) or set(raw) != NETWORK_FIELDS:
        raise ValueError("HTTPS network.json has unsupported or missing fields")
    address = validate_address(raw["lan_address"])
    port = validate_port(raw["port"])
    origin = f"https://{address}:{port}"
    if raw["public_origin"] != origin:
        raise ValueError("HTTPS public_origin must exactly match its configured address and port")
    config: dict[str, Any] = {"lan_address": address, "port": port, "public_origin": origin}
    for name in ("cert", "key", "ca"):
        if not isinstance(raw[name], str) or not raw[name]:
            raise ValueError(f"HTTPS {name} path must be a nonempty string")
        path = Path(raw[name])
        path = path if path.is_absolute() else data_dir / path
        if not path.resolve().is_relative_to(data_dir.resolve()):
            raise ValueError("HTTPS certificate paths must stay inside the server data directory")
        config[name] = path
    if len({config[name].resolve() for name in ("cert", "key", "ca")}) != 3:
        raise ValueError("HTTPS certificate, key, and CA must use separate files")
    return config


def ssl_context(config: dict[str, Any]) -> ssl.SSLContext:
    """CODEX: Use authenticated TLS with a private server key and no insecure fallback."""
    for name in ("cert", "key", "ca"):
        check_file(Path(config[name]), private=name == "key")
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    try:
        context.load_cert_chain(str(config["cert"]), str(config["key"]))
        context.load_verify_locations(cafile=str(config["ca"]))
    except (OSError, ssl.SSLError):
        raise ValueError("HTTPS certificate, private key, or CA could not be loaded") from None
    return context
