"""CODEX: Build one executable shell file containing the offline client payload."""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import tarfile
from pathlib import Path, PurePosixPath


def build(payload: Path, output: Path, template: Path | None = None):
    template = template or Path(__file__).with_name("install_lan_client.sh.in")
    if output.exists():
        raise ValueError("Output already exists; choose a new installer path")
    with tarfile.open(payload, "r:gz") as archive:
        members = archive.getmembers()
        names = {member.name for member in members}
        required = {
            "Codex-LAN-Client/setup_lan_client.py",
            "Codex-LAN-Client/codex_lan/worker.py",
            "Codex-LAN-Client/codex_lan/media.py",
            "Codex-LAN-Client/codex_lan/model_registry.py",
            "Codex-LAN-Client/pip.pyz",
            "Codex-LAN-Client/bin/codex",
        }
        if not required <= names:
            raise ValueError("The client archive is incomplete")
        for member in members:
            path = PurePosixPath(member.name)
            if not path.parts or not member.isfile() or path.is_absolute() or ".." in path.parts or path.parts[0] != "Codex-LAN-Client":
                raise ValueError("The client archive contains an unsafe entry")
    digest = hashlib.sha256()
    with payload.open("rb") as package:
        while chunk := package.read(1024 * 1024):
            digest.update(chunk)
    source = template.read_text()
    if source.count("@PAYLOAD_SHA256@") != 1 or not source.endswith("__CODEX_LAN_PAYLOAD_BELOW__\n"):
        raise ValueError("Invalid installer template")
    header = source.replace("@PAYLOAD_SHA256@", digest.hexdigest()).encode()
    created = False
    try:
        target = output.open("xb")
        created = True
        with target, payload.open("rb") as package:
            target.write(header)
            shutil.copyfileobj(package, target, length=1024 * 1024)
            target.flush()
            os.fsync(target.fileno())
        output.chmod(0o755)
    except BaseException:
        if created:
            output.unlink(missing_ok=True)
        raise
    return digest.hexdigest()


def main():
    parser = argparse.ArgumentParser(description="Build a self-contained shell installer")
    parser.add_argument("--payload", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        checksum = build(args.payload, args.output)
    except (OSError, ValueError, tarfile.TarError) as error:
        parser.error(str(error))
    print(f"Shell installer: {args.output}")
    print(f"Embedded package SHA256: {checksum}")


if __name__ == "__main__":
    main()
