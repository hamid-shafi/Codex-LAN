"""Locate product sources while preserving the flat installed release format."""

from __future__ import annotations

from pathlib import Path, PurePosixPath
import sys


_SOURCE_PATHS = {
    "requirements-lan.txt": "apps/workspace/requirements.txt",
    "install_lan_server.py": "setup/host/install_lan_server.py",
    "setup_lan_https.py": "setup/host/setup_lan_https.py",
    "setup.sh": "setup/host/setup.sh",
    "setup_lan_client.py": "setup/connector/setup_lan_client.py",
    "setup_lan_direct.py": "setup/connector/setup_lan_direct.py",
    "launch_lan_client.py": "setup/connector/launch_lan_client.py",
    "direct_client.py": "setup/connector/direct_client.py",
    "install_lan_client.sh.in": "setup/connector/install_lan_client.sh.in",
    "install_lan_direct.sh.in": "setup/connector/install_lan_direct.sh.in",
    "build_lan_bundle.py": "setup/release-tools/build_lan_bundle.py",
    "build_lan_installer.py": "setup/release-tools/build_lan_installer.py",
    "prepare_lan_connector.py": "setup/release-tools/prepare_lan_connector.py",
    "publish_lan_release.py": "setup/release-tools/publish_lan_release.py",
    "deploy_lan_backend.py": "setup/maintenance/deploy_lan_backend.py",
    "deploy_lan_web.py": "setup/maintenance/deploy_lan_web.py",
    "deploy_lan_centralized.py": "setup/maintenance/deploy_lan_centralized.py",
    "deploy_usage_analysis_fix.py": "setup/maintenance/deploy_usage_analysis_fix.py",
    "repair_lan_quota.py": "setup/maintenance/repair_lan_quota.py",
    "backup_lan_history.py": "setup/maintenance/backup_lan_history.py",
}
_DESKTOP_FILES = {
    "app.py", "activity_model.py", "attachment_preview.py", "backend.py",
    "branch_controller.py", "branch_view.py", "codex_bridge.py", "markdown_view.py",
    "pin_dock.py", "storage.py", "style.css", "codex.desktop.in", "run.sh", "install.sh",
}


def source_root(path):
    """Find a checkout or a self-contained, flat setup/client source directory."""
    path = Path(path).resolve()
    directory = path if path.is_dir() else path.parent
    candidates = (directory, *directory.parents)
    # A checkout contains a nested package that also resembles an installed
    # payload. Prefer the explicit checkout boundary before the flat fallback.
    for candidate in candidates:
        if ((candidate / "project_layout.py").is_file()
                or (candidate / "apps/workspace/codex_lan/__init__.py").is_file()):
            return candidate
    for candidate in candidates:
        if (candidate / "codex_lan/__init__.py").is_file():
            return candidate
    return directory


def source_file(root, legacy_name):
    """Map stable distribution names to checkout files, or use flat payload files."""
    root = Path(root)
    name = PurePosixPath(legacy_name)
    if name.is_absolute() or ".." in name.parts:
        raise ValueError("Source names must be relative paths inside the product")
    flat = root / name
    if flat.exists() or not (root / "apps/workspace").is_dir():
        return flat
    if name.parts and name.parts[0] == "codex_lan":
        return root / "apps/workspace" / name
    if str(name) in _DESKTOP_FILES or name.parts and name.parts[0] == "assets":
        return root / "apps/desktop" / name
    return root / _SOURCE_PATHS.get(str(name), str(name))


def configure_imports(root_or_file):
    """Make the product modules importable from any working directory."""
    root = source_root(root_or_file)
    locations = (
        root,
        root / "apps/workspace",
        root / "apps/desktop",
        root / "setup/host",
        root / "setup/connector",
        root / "setup/release-tools",
        root / "setup/maintenance",
        root / "development",
        root / "development/tests",
    )
    for directory in reversed(locations):
        value = str(directory)
        if directory.is_dir() and value not in sys.path:
            sys.path.insert(0, value)
    return root
