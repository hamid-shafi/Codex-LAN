"""CODEX: Package only client code, public binaries and dependency wheels, never credentials."""

import argparse
import hashlib
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

for _source_parent in Path(__file__).resolve().parents:
    if (_source_parent / "project_layout.py").is_file():
        sys.path.insert(0, str(_source_parent))
        break
from project_layout import configure_imports, source_file, source_root

configure_imports(__file__)

CLIENT_FILES = ("setup_lan_client.py", "launch_lan_client.py", "requirements-lan.txt",
                "codex_lan/__init__.py", "codex_lan/worker.py", "codex_lan/media.py", "codex_lan/egress.py",
                "codex_lan/recovery.py", "codex_lan/transport.py", "codex_lan/model_registry.py")
SUPPORTED_PYTHONS = ("310", "311", "312", "313", "314")


def validate_offline_wheels(root, pip, runner=subprocess.run):
    """CODEX: Refuse a release unless every advertised interpreter has a complete offline wheel set."""
    root, pip = Path(root), Path(pip)
    for version in SUPPORTED_PYTHONS:
        command = [sys.executable, str(pip), "--isolated", "install", "--dry-run", "--ignore-installed",
                   "--no-index", "--only-binary=:all:", "--implementation", "cp", "--python-version", version,
                   "--abi", "cp" + version, "--platform", "manylinux2014_x86_64", "--platform", "manylinux_2_28_x86_64",
                   "--find-links", str(root / ".lan-wheels"), "-r", str(source_file(root, "requirements-lan.txt")),
                   "--disable-pip-version-check", "--no-input"]
        try:
            runner(command, check=True, capture_output=True, text=True, timeout=45)
        except subprocess.SubprocessError as exc:
            raise ValueError("Offline dependencies are incomplete for Python 3." + version[1:] + ". Rebuild the wheel bundle before publishing.") from exc
        print("Offline dependencies verified for Python 3." + version[1:], flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--codex", type=Path, default=Path(shutil.which("codex") or Path.home() / ".local/bin/codex"))
    parser.add_argument("--pip", type=Path, default=source_root(__file__) / "pip.pyz")
    parser.add_argument("--direct", action="store_true", help="Include the HTTPS connector and its local updater")
    args = parser.parse_args()
    root = source_root(__file__)
    binary = args.codex.resolve()
    files = [(source_file(root, path), path) for path in CLIENT_FILES]
    files += [(binary, "bin/codex"), (binary.parent / "codex-code-mode-host", "bin/codex-code-mode-host"), (args.pip, "pip.pyz")]
    if args.direct:
        files += [(source_file(root, name), name) for name in ("setup_lan_direct.py", "direct_client.py")]
    wheels = sorted((root / ".lan-wheels").glob("*.whl"))
    files += [(path, "wheels/" + path.name) for path in wheels]
    if not wheels or any(not source.is_file() for source, _ in files):
        parser.error("Client source, binary, or dependency bundle is incomplete")
    if args.output.exists():
        parser.error("Output already exists; choose a new archive name")
    if args.direct:
        validate_offline_wheels(root, args.pip)
    with tarfile.open(args.output, "w:gz", dereference=True, compresslevel=3) as archive:
        for source, name in files:
            archive.add(source, arcname="Codex-LAN-Client/" + name, recursive=False)
    digest = hashlib.sha256()
    with args.output.open("rb") as archive:
        while chunk := archive.read(1024 * 1024):
            digest.update(chunk)
    print("Client archive:", args.output)
    print("SHA256:", digest.hexdigest())


if __name__ == "__main__":
    main()
