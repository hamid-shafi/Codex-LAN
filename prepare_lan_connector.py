"""CODEX: Build credential-free offline Connector downloads from an installed, trusted source snapshot."""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

from build_lan_bundle import SUPPORTED_PYTHONS
from publish_lan_release import publish


PIP_ZIPAPP_URL = "https://bootstrap.pypa.io/pip/pip.pyz"
MAX_PIP_BYTES = 8 * 1024 * 1024


def valid_pip(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or not 0 < path.stat().st_size <= MAX_PIP_BYTES:
        return False
    try:
        with zipfile.ZipFile(path) as archive:
            return "__main__.py" in archive.namelist() and archive.testzip() is None
    except (OSError, ValueError, zipfile.BadZipFile):
        return False


def prepare_pip(path, *, offline=False):
    path = Path(path)
    if valid_pip(path):
        return path
    if offline:
        raise ValueError("The cached pip zipapp is missing; prepare Connector dependencies online once first")
    if path.exists() or path.is_symlink():
        raise ValueError("An invalid pip cache already exists; inspect it locally before rebuilding")
    descriptor, temporary = tempfile.mkstemp(prefix=".pip-download-", dir=path.parent)
    temporary = Path(temporary)
    try:
        with os.fdopen(descriptor, "wb") as output, urllib.request.urlopen(PIP_ZIPAPP_URL, timeout=45) as response:
            final = urllib.parse.urlsplit(response.geturl())
            if final.scheme != "https" or final.hostname != "bootstrap.pypa.io":
                raise ValueError("pip download was redirected outside the official HTTPS source")
            size = 0
            while chunk := response.read(65536):
                size += len(chunk)
                if size > MAX_PIP_BYTES:
                    raise ValueError("pip download exceeded its size limit")
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        if not valid_pip(temporary):
            raise ValueError("The downloaded pip zipapp is invalid")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return path


def prepare_wheels(source, pip, *, offline=False, runner=subprocess.run):
    source = Path(source)
    wheels = source / ".lan-wheels"
    if wheels.is_symlink():
        raise ValueError("The wheel cache must not be a symbolic link")
    wheels.mkdir(mode=0o700, exist_ok=True)
    if offline:
        return wheels
    for version in SUPPORTED_PYTHONS:
        print("Preparing offline dependencies for Python 3." + version[1:] + "…", flush=True)
        runner([sys.executable, str(pip), "--isolated", "download", "--only-binary=:all:",
                "--implementation", "cp", "--python-version", version, "--abi", "cp" + version,
                "--platform", "manylinux2014_x86_64", "--platform", "manylinux_2_28_x86_64",
                "--dest", str(wheels), "--requirement", str(source / "requirements-lan.txt"),
                "--disable-pip-version-check", "--no-input"], check=True, timeout=180)
    return wheels


def main(argv=None):
    parser = argparse.ArgumentParser(description="Prepare downloadable Connector; no services are restarted")
    parser.add_argument("--data-dir", type=Path, default=Path.home() / ".local/share/codex-lan/server")
    parser.add_argument("--codex", type=Path, help="Detected official standalone Codex executable")
    parser.add_argument("--offline", action="store_true", help="Use previously prepared dependencies without network downloads")
    args = parser.parse_args(argv)
    if sys.platform != "linux" or platform.machine() not in {"x86_64", "AMD64"}:
        parser.error("The current offline Connector release supports Linux x86_64; prepare it on an x86_64 host")
    os.umask(0o077)
    source = Path(__file__).resolve().parent
    codex = (args.codex or Path(shutil.which("codex") or Path.home() / ".local/bin/codex")).expanduser().resolve()
    if not codex.is_file() or not os.access(codex, os.X_OK) or not (codex.parent / "codex-code-mode-host").is_file():
        parser.error("A complete official standalone Codex installation is required, including codex-code-mode-host")
    try:
        pip = prepare_pip(source / "pip.pyz", offline=args.offline)
        prepare_wheels(source, pip, offline=args.offline)
        publish(args.data_dir.expanduser(), codex=codex, pip=pip)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        parser.error(str(error))
    print("Connector is ready in Host setup. Existing prompts and services were not restarted.")


if __name__ == "__main__":
    main()
