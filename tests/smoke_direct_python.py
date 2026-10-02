"""CODEX: Exercise a real offline connector installation under the invoking Python interpreter."""

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--pip", type=Path, default=Path("/tmp/codex-lan-bootstrap-pip.pyz"))
    args = parser.parse_args()
    workspace = args.workspace.resolve()
    if not workspace.is_dir() or any(workspace.iterdir()):
        parser.error("Use an existing empty temporary workspace")
    project = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(project))
    import direct_client

    payload = workspace / "payload"
    payload.mkdir(mode=0o700)
    for name in direct_client.REQUIRED | direct_client.OPTIONAL:
        destination = payload / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        if name.startswith("bin/"):
            # CODEX: Installation imports modules only; these inert binaries are never executed.
            destination.write_bytes(b"CODEX: inert smoke fixture; do not execute\n")
        else:
            source = args.pip if name == "pip.pyz" else project / name
            if source.is_file():
                shutil.copy2(source, destination)
    shutil.copytree(project / ".lan-wheels", payload / "wheels")
    version = direct_client.payload_version(payload)
    release = direct_client.install_release(payload, workspace / "installation", version)
    result = subprocess.run([str(release / "venv/bin/python"), "-c",
        "import json,sys,aiohttp,frozenlist,multidict,propcache,yarl,codex_lan.worker; "
        "print(json.dumps({'python':sys.version.split()[0],'aiohttp':aiohttp.__version__,"
        "'worker_import':True}))"], cwd=release, check=True, capture_output=True, text=True)
    info = json.loads(result.stdout)
    if tuple(map(int, info["python"].split(".")[:2])) != sys.version_info[:2]:
        raise RuntimeError("Installed virtual environment changed Python versions")
    if not (release / ".ready").is_file():
        raise RuntimeError("Installation was not marked ready")
    print(json.dumps({**info, "release": version, "offline_install": True,
                      "installed_path": str(release)}, indent=2))


if __name__ == "__main__":
    main()
