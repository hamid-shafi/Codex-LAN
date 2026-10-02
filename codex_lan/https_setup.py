"""CODEX: Prepare private-LAN TLS certificates without starting or restarting any service."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import ssl
import stat
import subprocess
import tempfile
from pathlib import Path

from .network import check_file, load_network, ssl_context, validate_address, validate_port


def private_directory(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    metadata = path.lstat()
    if path.is_symlink() or not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid():
        raise ValueError("HTTPS state must be a real directory owned by the current user")
    if stat.S_IMODE(metadata.st_mode) & 0o077:
        raise ValueError("HTTPS state directories must have mode 0700")


def openssl(*arguments: str) -> bytes:
    try:
        result = subprocess.run(["openssl", *map(str, arguments)], check=True,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=45)
    except (OSError, subprocess.SubprocessError):
        raise ValueError("OpenSSL could not prepare or verify the HTTPS certificates") from None
    return result.stdout


def install_missing(source: Path, target: Path) -> None:
    """CODEX: Never overwrite an existing private key, certificate, or configured identity."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(target, flags, 0o600)
    with os.fdopen(descriptor, "wb") as output, source.open("rb") as incoming:
        shutil.copyfileobj(incoming, output)
        output.flush()
        os.fsync(output.fileno())


def prepare_network(data_dir: Path, address: str, port: int = 8767) -> dict:
    address, port = validate_address(address), validate_port(port)
    data_dir = Path(data_dir).absolute()
    private_directory(data_dir)
    config = load_network(data_dir)
    if config and (config["lan_address"] != address or config["port"] != port):
        raise ValueError("Existing HTTPS address and port were preserved; use their configured values")
    tls_dir = data_dir / "tls"
    private_directory(tls_dir)
    if config is None:
        config = {"lan_address": address, "port": port, "public_origin": f"https://{address}:{port}",
                  "cert": tls_dir / "server.pem", "key": tls_dir / "server-key.pem", "ca": tls_dir / "ca.pem"}
    elif any(config[name].parent != tls_dir for name in ("cert", "key", "ca")):
        raise ValueError("Existing custom HTTPS certificate paths were preserved; manage them separately")
    ca_key = tls_dir / "ca-key.pem"
    for path in (ca_key, config["ca"], config["key"], config["cert"]):
        if path.exists() or path.is_symlink():
            check_file(path, private=path in (ca_key, config["key"]))
    if config["ca"].exists() and not ca_key.exists():
        raise ValueError("Existing CA certificate has no private key; restore its original key")
    if config["cert"].exists() and not config["key"].exists():
        raise ValueError("Existing server certificate has no private key; restore its original key")
    with tempfile.TemporaryDirectory(prefix=".https-setup-", dir=data_dir) as temporary:
        stage = Path(temporary)
        if not ca_key.exists():
            generated = stage / "ca-key.pem"
            openssl("genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:3072", "-out", generated)
            install_missing(generated, ca_key)
        if not config["ca"].exists():
            generated = stage / "ca.pem"
            openssl("req", "-new", "-x509", "-sha256", "-key", ca_key, "-out", generated,
                    "-days", "3650", "-subj", "/CN=Codex LAN Local CA",
                    "-addext", "basicConstraints=critical,CA:TRUE",
                    "-addext", "keyUsage=critical,keyCertSign,cRLSign")
            install_missing(generated, config["ca"])
        if not config["key"].exists():
            generated = stage / "server-key.pem"
            openssl("genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:3072", "-out", generated)
            install_missing(generated, config["key"])
        if not config["cert"].exists():
            csr, generated, extensions = stage / "server.csr", stage / "server.pem", stage / "server.ext"
            openssl("req", "-new", "-sha256", "-key", config["key"], "-out", csr,
                    "-subj", f"/CN={address}")
            extensions.write_text(
                "basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\n"
                f"extendedKeyUsage=serverAuth\nsubjectAltName=IP:{address}\n", encoding="ascii")
            openssl("x509", "-req", "-sha256", "-in", csr, "-CA", config["ca"], "-CAkey", ca_key,
                    "-set_serial", "0x" + secrets.token_hex(16), "-days", "825", "-out", generated,
                    "-extfile", extensions)
            install_missing(generated, config["cert"])
        openssl("verify", "-CAfile", config["ca"], "-purpose", "sslserver", "-verify_ip", address, config["cert"])
        ssl_context(config)
        if not (data_dir / "network.json").exists():
            staged_config = stage / "network.json"
            staged_config.write_text(json.dumps({key: str(value) for key, value in config.items()
                                                 if key in ("cert", "key", "ca")} | {
                "lan_address": address, "port": port, "public_origin": config["public_origin"],
            }, indent=2) + "\n", encoding="utf-8")
            install_missing(staged_config, data_dir / "network.json")
    return config


def ca_fingerprint(path: Path) -> str:
    certificate = ssl.PEM_cert_to_DER_cert(Path(path).read_text(encoding="ascii"))
    digest = hashlib.sha256(certificate).hexdigest().upper()
    return ":".join(digest[index:index + 2] for index in range(0, len(digest), 2))
