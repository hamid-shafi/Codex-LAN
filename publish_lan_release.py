"""CODEX: Publish an allowlisted, credential-free connector release on the private server."""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from build_lan_installer import build
from codex_lan import __version__


def publish(data_dir, *, codex=None, pip=None):
    source = Path(__file__).resolve().parent
    releases = Path(data_dir) / "releases"
    releases.mkdir(mode=0o700, parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix="release-", dir=releases))
    archive = stage / "client.tar.gz"
    command = [sys.executable, str(source / "build_lan_bundle.py"), "--direct", "--output", str(archive)]
    if codex is not None:
        command += ["--codex", str(codex)]
    if pip is not None:
        command += ["--pip", str(pip)]
    subprocess.run(command, check=True)
    installer = stage / "Install-Codex-Connector.sh"
    digest = build(archive, installer, source / "install_lan_direct.sh.in")
    manifest = {"version": __version__, "sha256": digest, "size": archive.stat().st_size}
    previous = releases / "release.json"
    if previous.exists():
        backup = Path(tempfile.mkdtemp(prefix="previous-", dir=releases))
        for name in ("release.json", "client.tar.gz", "Install-Codex-Connector.sh"):
            if (releases / name).exists():
                shutil.copy2(releases / name, backup / name)
    for path in (archive, installer):
        os.replace(path, releases / path.name)
    temporary = stage / "release.json"
    temporary.write_text(json.dumps(manifest))
    os.replace(temporary, releases / "release.json")
    stage.rmdir()
    print(f"Published Codex Connector {__version__}. No credentials are included; no services were restarted.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, default=Path.home() / ".local/share/codex-lan/server")
    parser.add_argument("--codex", type=Path)
    parser.add_argument("--pip", type=Path)
    args = parser.parse_args()
    publish(args.data_dir, codex=args.codex, pip=args.pip)
