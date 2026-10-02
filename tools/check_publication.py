#!/usr/bin/env python3
"""CODEX: Check public source and reachable history without printing credential values."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys


MAX_FILE_BYTES = 8 * 1024 * 1024
SECRET = re.compile(
    rb"\b(?:sk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{30,}"
    rb"|github_pat_[A-Za-z0-9_]{30,}|AKIA[0-9A-Z]{16}|xox[baprs]-[A-Za-z0-9-]{20,}"
    rb"|AIza[0-9A-Za-z_-]{35}|eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{24,}\.[A-Za-z0-9_-]{20,})\b"
)
PRIVATE_KEY = re.compile(rb"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----")
CREDENTIAL_NAMES = (
    rb"(?:api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret"
    rb"|password|device[_-]?token|(?:aws[_-]?)?secret[_-]?access[_-]?key)"
)
ASSIGNMENT = re.compile(
    rb"(?i)(?:[\"']" + CREDENTIAL_NAMES + rb"[\"']|\b" + CREDENTIAL_NAMES
    + rb")\s*[:=]\s*[\"']([^\"'\r\n]{8,})[\"']"
)
LOCAL_IDENTITY = re.compile(
    rb"/home/(?!example(?:/|\b)|user(?:/|\b)|test(?:/|\b)|client(?:/|\b)"
    rb"|executor(?:/|\b)|private(?:/|\b)|owner(?:/|\b)|admin(?:/|\b)"
    rb"|alice(?:/|\b)|bob(?:/|\b)|server(?:/|\b)|host(?:/|\b)|project(?:/|\b)"
    rb"|reconnected(?:/|\b)|workspace-owner(?:/|\b)|foreign(?:/|\b)|newest(?:/|\b)"
    rb"|own(?:/|\b)|pinned(?:/|\b))[^/\s\"'<>()]+(?:/|\b)"
    rb"|\b192\.168\.(?!0\.0\b|1\.)\d{1,3}\.\d{1,3}\b"
    rb"|proxy\.my\.[A-Za-z0-9.-]+"
)
FIXTURES = {
    ("tests/test_lan_activity.py", "literal-credential"): {
        "969e0ced2b825c3229f4ad9aa350f49247151f737661bc3c7554dfcb7032480c",
    },
    ("tests/test_lan_browser_password.py", "literal-credential"): {
        "4da30c37ee0ecc0369ee62679f2f78cd3187cacb8d790e899371257c964a90bd",
        "194c2666534979748be3950e3d68d9b5ea38c263313ce7be064acbf239c856f0",
        "ea84604d1573d51eda795c3109388cf3804fc85207a9da1a98e2b68eb54481b0",
    },
    ("tests/test_lan_prepared_context.py", "provider-secret"): {
        "51b2485cd413b0070b3df0cba60468bc40134d9523609f92a239f7e8485e3c46",
        "c22f734382af53ee7954c91a2af2c221f230388c679380d9f3cb8936a33dbbb1",
    },
    ("tests/test_lan_worker.py", "literal-credential"): {
        "dfdbf98d5f2e87b34257ff0c43df555ed8fd84e80f1d1778783d05dd0e5ad1d8",
    },
    ("tests/test_lan_bootstrap.py", "literal-credential"): {
        "ea97e2dba968daa279072c7f94b4c2e8b4ce7b2cca0cfe17e49011be5aad5c46",
        "60ec9bb7299d85e0cdd35d4058fabd7cb6bdc9b788c6efde44427e9bb9234e13",
    },
    ("tests/test_lan_host_worker.py", "literal-credential"): {
        "7edb8aa4a4bec2b88cece00b4efcbfbd56c604d2d7c9b24bd764de0c50794bb4",
    },
    ("tests/test_lan_model_registry.py", "literal-credential"): {
        "e1466187c844c921b622aff2197444cfdc2c87489f7a6e71cef47b31a1602ced",
    },
    ("tests/test_lan_room_execution.py", "literal-credential"): {
        "bb310ae75eb4a77421ea0d52b9db9a10d93be149185126cbfdada35d55c970e9",
        "a7e86e4ba8c86be4bcfc92d5b6e92fb604a6f0a978045a317fd77be4dc488033",
    },
    ("tests/test_lan_server.py", "literal-credential"): {
        "194c2666534979748be3950e3d68d9b5ea38c263313ce7be064acbf239c856f0",
    },
}
PLACEHOLDERS = {
    b"[redacted]", b"your-api-key", b"YOUR_API_KEY", b"YOUR_PASSWORD",
    b"changeme", b"REPLACE_ME", b"example-password",
}
PRIVATE_DIRECTORIES = {
    ".git", ".codex", ".local", ".config", "private-reports", "private-state",
    "sessions", "uploads", "generated_images", "__pycache__", ".lan-wheels",
    ".venv", "venv", "releases", "dist", "build",
}
PRIVATE_NAMES = {
    "auth.json", "admin.token", "host-worker.token", "connector.token",
    "device-token", "connection.json", "network.json", "client-state.json",
    "server.sqlite3", "usage-analysis.json", "pip.pyz",
}
PRIVATE_SUFFIX = re.compile(
    r"\.(?:sqlite3?|db)(?:-.*)?$|\.(?:log|token|pem|key|p12|pfx|bak|backup|whl|pyc)$",
    re.I,
)


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    category: str
    origin: str = "worktree"


def git(repo: Path, *args: str) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, timeout=60,
    )
    if result.returncode:
        raise RuntimeError("Git inspection failed; no command output was disclosed")
    return result.stdout


def private_path(path: str) -> bool:
    parts = PurePosixPath(path).parts
    name = parts[-1] if parts else ""
    return (
        any(part in PRIVATE_DIRECTORIES or part.startswith(".venv") for part in parts)
        or name in PRIVATE_NAMES
        or name == ".env" or (name.startswith(".env.") and name != ".env.example")
        or bool(PRIVATE_SUFFIX.search(name))
        or name.startswith(("codex-pairing", "pairing")) and name.endswith(".json")
        or "private" in name.lower() and name.endswith((".sh", ".tar.gz", ".zip"))
    )


def safe_path(path: str) -> str:
    encoded = path.encode("utf-8", errors="replace")
    return SECRET.sub(b"[redacted-filename]", encoded).decode("utf-8", errors="replace")


def inspect_bytes(path: str, data: bytes, origin: str = "worktree") -> list[Finding]:
    findings = []
    displayed_path = safe_path(path)
    if private_path(path):
        findings.append(Finding(displayed_path, 0, "private-runtime-file", origin))
    if len(data) > MAX_FILE_BYTES:
        return findings + [Finding(displayed_path, 0, "file-exceeds-review-limit", origin)]
    for category, pattern in (
        ("provider-secret", SECRET), ("private-key", PRIVATE_KEY),
        ("literal-credential", ASSIGNMENT), ("personal-machine-identity", LOCAL_IDENTITY),
    ):
        for match in pattern.finditer(data):
            value = match.group(1) if category == "literal-credential" else match.group()
            allowed = hashlib.sha256(value).hexdigest() in FIXTURES.get((path, category), set())
            if allowed or category == "literal-credential" and value in PLACEHOLDERS:
                continue
            findings.append(Finding(
                displayed_path, data.count(b"\n", 0, match.start()) + 1, category, origin,
            ))
    return list(dict.fromkeys(findings))


def source_files(repo: Path) -> list[str]:
    paths = git(repo, "ls-files", "--cached", "--others", "--exclude-standard", "-z")
    return sorted(set(item.decode("utf-8", errors="surrogateescape") for item in paths.split(b"\0") if item))


def inspect_worktree(repo: Path, paths: list[str]) -> list[Finding]:
    findings = []
    for path in paths:
        target = repo / path
        if target.is_symlink():
            findings.append(Finding(safe_path(path), 0, "symlink-needs-review"))
            continue
        if not target.is_file():
            continue
        if target.stat().st_size > MAX_FILE_BYTES:
            findings.append(Finding(safe_path(path), 0, "file-exceeds-review-limit"))
            continue
        findings.extend(inspect_bytes(path, target.read_bytes()))
    return findings


def inspect_history(repo: Path, allow_identity: bool = False) -> tuple[list[Finding], int]:
    revisions = git(repo, "rev-list", "--all").decode("ascii").splitlines()
    blobs: dict[str, set[str]] = {}
    findings = []
    for revision in revisions:
        findings.extend(inspect_bytes("[commit metadata]", git(repo, "cat-file", "commit", revision), revision[:12]))
        for row in git(repo, "ls-tree", "-r", "-z", revision).split(b"\0"):
            if not row:
                continue
            header, raw_path = row.split(b"\t", 1)
            mode, kind, oid = header.split()
            path = raw_path.decode("utf-8", errors="surrogateescape")
            if mode == b"120000" or kind == b"commit":
                findings.append(Finding(safe_path(path), 0, "symlink-or-submodule-needs-review", revision[:12]))
            if kind == b"blob":
                blobs.setdefault(oid.decode("ascii"), set()).add(path)
    for oid, paths in blobs.items():
        size = int(git(repo, "cat-file", "-s", oid))
        if size > MAX_FILE_BYTES:
            findings.extend(Finding(safe_path(p), 0, "file-exceeds-review-limit", oid[:12]) for p in paths)
            continue
        data = git(repo, "cat-file", "blob", oid)
        for path in paths:
            findings.extend(inspect_bytes(path, data, oid[:12]))
    if not allow_identity:
        for revision in revisions:
            identities = git(repo, "show", "-s", "--format=%ae%n%ce", revision).decode("utf-8").splitlines()
            if any(
                email and not email.endswith(("@users.noreply.github.com", "@example.com", "@localhost"))
                for email in identities
            ):
                findings.append(Finding("[commit metadata]", 0, "author-email-needs-review", revision[:12]))
    return list(dict.fromkeys(findings)), len(blobs)


def export_source(repo: Path, paths: list[str], destination: Path) -> None:
    if inspect_worktree(repo, paths):
        raise RuntimeError("Unreviewed or private source cannot be exported")
    destination = destination.absolute()
    if destination.exists() or destination.is_symlink():
        raise RuntimeError("Export destination must be a new directory")
    if destination.resolve() == repo.resolve() or destination.resolve() in repo.resolve().parents:
        raise RuntimeError("Export destination must not replace the source checkout")
    destination.mkdir(parents=True, mode=0o755)
    for path in paths:
        source = repo / path
        if not source.is_file():
            continue
        if private_path(path) or source.is_symlink():
            raise RuntimeError("Private files or symlinks cannot be exported")
        target = destination / path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        target.chmod(0o755 if source.stat().st_mode & 0o111 else 0o644)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--history", action="store_true", help="Inspect all reachable Git revisions")
    parser.add_argument("--allow-identity-metadata", action="store_true", help="Keep public commit identities; still inspect history contents")
    parser.add_argument("--export", type=Path, help="Export reviewed source without local history or ignored state")
    parser.add_argument("--json", action="store_true", help="Output value-free structured findings")
    args = parser.parse_args()
    try:
        repo = args.repo.resolve()
        paths = source_files(repo)
        findings = inspect_worktree(repo, paths)
        blob_count = 0
        if args.history:
            history_findings, blob_count = inspect_history(repo, args.allow_identity_metadata)
            findings.extend(history_findings)
        findings = list(dict.fromkeys(findings))
        if args.json:
            print(json.dumps({
                "files_reviewed": len(paths), "history_blobs_reviewed": blob_count,
                "findings": [asdict(item) for item in findings], "safe": not findings,
            }, indent=2))
        else:
            for item in findings[:200]:
                print(f"{item.path}:{item.line}: {item.category} [{item.origin}]")
            if len(findings) > 200:
                print(f"{len(findings) - 200} additional findings; use --json for all locations")
            print(f"Reviewed {len(paths)} source files and {blob_count} history blobs; {len(findings)} findings")
        if findings:
            return 1
        if args.export:
            export_source(repo, paths, args.export)
            print("Reviewed source exported; local Git history and ignored state were not copied")
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired):
        print("Publication check could not complete safely; inspect local filesystem and Git state", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
