"""CODEX: Refresh only the installed browser assets; never interrupt workers or runs."""

import argparse
import os
import shutil
import sys
import tempfile
from pathlib import Path

for _source_parent in Path(__file__).resolve().parents:
    if (_source_parent / "project_layout.py").is_file():
        sys.path.insert(0, str(_source_parent))
        break
from project_layout import configure_imports, source_file, source_root

configure_imports(__file__)

FONT_LICENSE_NAMES = {"OFL.txt", "LICENSE.txt", "Vazir-LICENSE.txt", "Vazir-OFL.txt", "Vazir-FONT.md", "KaTeX-LICENSE.txt", "Unicode-LICENSE.txt"}


def read_assets(source):
    """CODEX: Read the complete browser release, including self-hosted fonts and licenses."""
    source = Path(source)
    assets = {}
    names = {"style.css", "app.js", "index.html"}
    names.update(path.name for path in source.iterdir() if path.is_file() and (
        path.suffix in {".css", ".js", ".woff2"} or path.name in FONT_LICENSE_NAMES))
    fonts = source / "fonts"
    if fonts.is_dir():
        names.update(str(path.relative_to(source)) for path in fonts.rglob("*") if path.is_file() and (
            path.suffix == ".woff2" or path.name in FONT_LICENSE_NAMES))
    for name in sorted(names):
        path = source / name
        if not path.resolve().is_relative_to(source.resolve()):
            raise ValueError(f"Source asset escapes its release directory: {name}")
        data = path.read_bytes()
        if not data:
            raise ValueError(f"Source asset is empty: {name}")
        assets[name] = data
    return assets


def deploy(source, destination):
    source, destination = Path(source), Path(destination)
    if not destination.is_dir():
        raise ValueError("The LAN web installation does not exist")
    assets = read_assets(source)
    staged = Path(tempfile.mkdtemp(prefix=".codex-web-stage-", dir=destination.parent))
    backup_root = None
    published = False
    try:
        shutil.copytree(destination, staged, dirs_exist_ok=True)
        for name, data in assets.items():
            target_path = staged / name
            target_path.parent.mkdir(parents=True, exist_ok=True)
            target_path.unlink(missing_ok=True)
            with target_path.open("wb") as target:
                target.write(data)
                target.flush()
                os.fsync(target.fileno())
            os.chmod(target_path, 0o644)
        os.chmod(staged, destination.stat().st_mode & 0o777)
        backup_root = Path(tempfile.mkdtemp(prefix=".codex-web-backup-", dir=destination.parent))
        backup = backup_root / "web"
        os.replace(destination, backup)
        try:
            os.replace(staged, destination)
            published = True
        except BaseException:
            try:
                os.replace(backup, destination)
            except OSError as rollback_error:
                raise RuntimeError(f"Browser bundle publication failed; rollback is at {backup}") from rollback_error
            raise
    finally:
        if staged.exists():
            shutil.rmtree(staged)
        if backup_root is not None and backup_root.exists():
            if published:
                shutil.rmtree(backup_root)
            elif not any(backup_root.iterdir()):
                backup_root.rmdir()


def main():
    parser = argparse.ArgumentParser(description="Deploy browser files without restarting Codex")
    parser.add_argument("--source", type=Path, default=source_file(source_root(__file__), "codex_lan/web"))
    parser.add_argument("--destination", type=Path,
                        default=Path.home() / ".local/share/codex-lan/code/codex_lan/web")
    args = parser.parse_args()
    deploy(args.source, args.destination)
    print("Browser assets updated. Refresh the browser to load them; workers were not restarted.")


if __name__ == "__main__":
    main()
