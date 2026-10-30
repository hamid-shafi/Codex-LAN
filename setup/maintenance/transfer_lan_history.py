"""Private, selected-room transfer with native memory and fresh-host safeguards.

This offline maintenance tool never starts a model turn. Export does not stop or
write to the source installation. Restore is intentionally not a merge utility.
"""

from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import zipfile

import lan_history_transfer as lan
import native_history_transfer as native

FORMAT = "codex-lan-private-transfer-v1"
TOOLS = ("transfer_lan_history.py", "lan_history_transfer.py", "native_history_transfer.py", "restore-history.sh")
SERVICES = ("codex-lan-host-worker.service", "codex-lan-server.service")
MUTATORS = ("codex-lan-backend-update.service", "codex-lan-backend-recovery.service")
README = """# Private Codex-LAN history transfer

This archive contains private conversations, native model memory and attachments.
It is NOT encrypted. Do not publish it or add it to a Git repository. Although
login/configuration files are excluded, conversation content can contain secrets.

On the destination Linux computer:
1. Install Codex-LAN using its reviewed GitHub source and bash setup.sh.
2. Sign in to Codex locally. Use the Codex version specified in manifest.json.
3. Do not create rooms or start prompts before importing.
4. Extract this archive into a private directory outside any Git checkout.
5. Run: bash restore-history.sh

Restore verifies the payload before stopping destination services, refuses a
nonempty installation, preserves its administrator/login credentials, and takes
a private rollback backup. The old source host is never modified. Both hosts are
independent after the copy; later messages do not synchronize between them.

Only selected conversations and their native descendant sessions are included.
Project working trees, Git/Gerrit credentials, installed tools, global account
quota calibration and other users are not included. Clone your projects locally
and select the appropriate workspace before asking the agent to change files.
Historical text can mention old computer paths; history is not rewritten.

Runs active at export time are interrupted in the copy, never automatically
replayed. Review their activity before continuing; earlier work may have changed
files on the source computer. Export is a point-in-time copy, not live handoff.

Keep the original host and this private archive until you have checked all rooms
and continued them successfully on the laptop. The bundled executable scripts
must come from a trusted source; checksums detect corruption, not authenticity.
"""


def safe_directory(path: Path, create=False) -> Path:
    path = Path(path).absolute()
    if path.resolve() != path or path.is_symlink():
        raise ValueError("Transfer directories must not contain symbolic links")
    if create:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not path.is_dir():
        raise ValueError("Required transfer directory does not exist: " + str(path))
    return path


def outside_git(path: Path):
    for ancestor in (path, *path.parents):
        if (ancestor / ".git").exists():
            raise ValueError("Private transfer files must stay outside Git checkouts")


def file_record(path: Path, relative: str) -> dict:
    if path.is_symlink() or path.resolve() != path.absolute() or not stat.S_ISREG(path.stat().st_mode):
        raise ValueError("Transfer files must be regular files without symbolic links")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return {"path": relative, "size": path.stat().st_size, "sha256": digest.hexdigest()}


def relative_path(value: str) -> Path:
    if not isinstance(value, str) or not value or "\\" in value:
        raise ValueError("Invalid payload path")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or str(path) != value or value == ".":
        raise ValueError("Invalid payload path")
    return Path(*path.parts)


def write_json(path: Path, value):
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    path.chmod(0o600)


def prepare_bundle(stage: Path, lan_metadata: dict, native_metadata: dict, source_home: Path) -> dict:
    """Assemble already-exported payloads, also useful for resource-bounded exports."""
    stage = safe_directory(stage)
    outside_git(stage)
    if set(lan_metadata["thread_ids"]) != set(native_metadata["root_thread_ids"]):
        raise ValueError("LAN and native root threads do not match")
    lan.validate_history(stage / "lan", lan_metadata)
    native.validate_native(stage / "native", native_metadata)
    source = Path(__file__).resolve().parent
    for name in TOOLS:
        with (source / name).open("rb") as incoming, (stage / name).open("xb") as outgoing:
            shutil.copyfileobj(incoming, outgoing)
        (stage / name).chmod(0o600)
    with (stage / "README.txt").open("x", encoding="utf-8") as stream:
        stream.write(README)
    (stage / "README.txt").chmod(0o600)
    records = [file_record(stage / name, name) for name in (*TOOLS, "README.txt", "lan/history.sqlite3")]
    records.extend({"path": "lan/" + item["file"], "size": item["size"], "sha256": item["sha256"]}
                   for item in lan_metadata["attachments"])
    records.extend({**item, "path": "native/" + item["path"]} for item in native_metadata["files"])
    metadata = {"format": FORMAT, "created_at": datetime.now(timezone.utc).isoformat(),
                "source_home": str(source_home), "lan": lan_metadata, "native": native_metadata,
                "files": records, "warning": "Private, unencrypted chat content; never publish this archive."}
    write_json(stage / "manifest.json", metadata)
    return metadata


def pack_bundle(stage: Path, output: Path, metadata: dict) -> dict:
    output = Path(output).absolute()
    outside_git(output)
    parent = safe_directory(output.parent, create=True)
    if parent.stat().st_mode & 0o077:
        raise ValueError("Choose a private output directory (permissions 0700)")
    if output.exists() or output.is_symlink():
        raise ValueError("Archive already exists; choose a new output name")
    # Exclusive creation prevents accidentally replacing an existing backup.
    descriptor = os.open(output, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream, zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED,
                                                                  compresslevel=6, allowZip64=True) as archive:
            for name in ["manifest.json", *(record["path"] for record in metadata["files"])]:
                archive.write(stage / relative_path(name), name)
        return file_record(output, output.name)
    except BaseException:
        output.unlink(missing_ok=True)
        raise


def export_bundle(data_dir: Path, native_home: Path, output: Path, room_ids: list[str], source_home: Path,
                  asset_roots: list[Path] | None = None) -> dict:
    output = Path(output).absolute()
    outside_git(output)
    parent = safe_directory(output.parent, create=True)
    if parent.stat().st_mode & 0o077:
        raise ValueError("Choose a private output directory (permissions 0700)")
    with tempfile.TemporaryDirectory(prefix=".selected-history-", dir=parent) as temporary:
        stage = Path(temporary)
        print("Snapshotting selected LAN conversations...", flush=True)
        lan_metadata = lan.export_history(data_dir, stage / "lan", room_ids)
        print("Snapshotting selected native memory and descendants...", flush=True)
        native_metadata = native.export_native(native_home, stage / "native", lan_metadata["thread_ids"], asset_roots=asset_roots)
        metadata = prepare_bundle(stage, lan_metadata, native_metadata, source_home)
        print("Compressing the private ZIP (large sessions can take several minutes)...", flush=True)
        result = pack_bundle(stage, output, metadata)
    return {"archive": str(output), "bytes": result["size"], "sha256": result["sha256"], "counts": lan_metadata["counts"]}


def validate_bundle(bundle: Path) -> dict:
    bundle = safe_directory(bundle)
    outside_git(bundle)
    manifest = bundle / "manifest.json"
    if manifest.is_symlink() or manifest.stat().st_size > 8 * 1024 * 1024:
        raise ValueError("Invalid or oversized transfer manifest")
    metadata = json.loads(manifest.read_text(encoding="utf-8"))
    if metadata.get("format") != FORMAT or not isinstance(metadata.get("files"), list):
        raise ValueError("Unsupported transfer format")
    old_home = Path(metadata["source_home"])
    if not old_home.is_absolute() or old_home == Path("/"):
        raise ValueError("Invalid original home directory")
    expected = {}
    for record in metadata["files"]:
        relative = relative_path(record["path"])
        if record["path"] in expected:
            raise ValueError("Duplicate file in transfer manifest")
        expected[record["path"]] = record
        if file_record(bundle / relative, record["path"]) != record:
            raise ValueError("Transfer file checksum mismatch: " + record["path"])
    required = set(TOOLS) | {"README.txt", "lan/history.sqlite3"}
    required.update("lan/" + row["file"] for row in metadata["lan"]["attachments"])
    required.update("native/" + row["path"] for row in metadata["native"]["files"])
    if set(expected) != required:
        raise ValueError("Unexpected or missing transfer file")
    if set(metadata["lan"]["thread_ids"]) != set(metadata["native"]["root_thread_ids"]):
        raise ValueError("Native and LAN histories do not match")
    lan.validate_history(bundle / "lan", metadata["lan"])
    native.validate_native(bundle / "native", metadata["native"])
    return metadata


class UserServices:
    def state(self, name):
        result = subprocess.run(["systemctl", "--user", "show", name, "--property=ActiveState", "--value"],
                                capture_output=True, text=True, timeout=15)
        if result.returncode:
            raise RuntimeError("Cannot inspect user services; run this in your laptop's normal desktop session")
        return result.stdout.strip()

    def run(self, action, names):
        if names:
            subprocess.run(["systemctl", "--user", action, *names], check=True, timeout=90)

    def active(self, name):
        return self.state(name) not in {"inactive", "failed", ""}


def backup_database(source: Path, target: Path):
    with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=5)) as incoming, \
            closing(sqlite3.connect(target)) as outgoing:
        incoming.backup(outgoing, pages=1024, sleep=0.05)
    target.chmod(0o600)


def restore_database(backup: Path, target: Path):
    with closing(sqlite3.connect(backup.as_uri() + "?mode=ro", uri=True)) as incoming, \
            closing(sqlite3.connect(target)) as outgoing:
        incoming.backup(outgoing)


def preflight_uploads(server: Path, metadata: dict):
    uploads = server / "uploads"
    if uploads.exists() or uploads.is_symlink():
        safe_directory(uploads)
    for record in metadata["attachments"]:
        target = uploads / Path(record["file"]).name
        if target.exists() or target.is_symlink():
            raise ValueError("A destination upload already exists; no files were overwritten")


def restore_bundle(bundle: Path, data_root: Path, new_home: Path, codex: Path, services=None) -> dict:
    """Restore a trusted extracted bundle into an empty, independently installed host."""
    os.umask(0o077)
    bundle, data_root, new_home = safe_directory(bundle), safe_directory(data_root), safe_directory(new_home)
    outside_git(data_root)
    metadata = validate_bundle(bundle)  # Expensive checks happen before service interruption.
    version = subprocess.run([str(codex), "--version"], capture_output=True, text=True, check=True, timeout=15).stdout.strip()
    if version != "codex-cli " + metadata["native"]["codex_version"]:
        raise ValueError("Install the verified Codex version " + metadata["native"]["codex_version"] + " before restoring")
    server, worker = data_root / "server", data_root / "host-worker"
    lan.preflight_history(server, metadata["lan"])  # Also rejects the source administrator.
    preflight_uploads(server, metadata["lan"])
    services = services or UserServices()
    if any(services.active(name) for name in MUTATORS):
        raise ValueError("An update or recovery job is active; wait for it to finish")
    active = [name for name in SERVICES if services.active(name)]
    backup = None
    native_result = None
    moved_native = []
    changed = False
    success = False
    lock = None
    recovery_failed = False
    try:
        # Stop the independent recovery monitor first, then worker and server.
        services.run("stop", [SERVICES[0]])
        services.run("stop", list(SERVICES[1:]))
        if any(services.active(name) for name in (*SERVICES, *MUTATORS)):
            raise RuntimeError("Destination services did not stop; nothing was imported")
        descriptor = os.open(server / "server.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        lock = os.fdopen(descriptor, "r+")
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        lan.preflight_history(server, metadata["lan"])
        preflight_uploads(server, metadata["lan"])
        empty_paths = native.empty_store_paths(worker)
        backup_root = safe_directory(data_root / "transfer-backups", create=True)
        backup = Path(tempfile.mkdtemp(prefix="before-import-", dir=backup_root))
        backup_database(server / "server.sqlite3", backup / "server.sqlite3")
        write_json(backup / "recovery.json", {"status": "preparing", "previously_active_services": active,
                                            "source_bundle": str(bundle), "created_at": datetime.now(timezone.utc).isoformat()})
        for source in empty_paths:
            destination = backup / source.name
            source.rename(destination)
            moved_native.append((source, destination))
        print("Importing native memory without starting any model turns...", flush=True)
        native_result = native.import_native(bundle / "native", worker, metadata["native"], metadata["source_home"], str(new_home))
        changed = True
        print("Importing selected chats and attachments...", flush=True)
        result = lan.import_history(bundle / "lan", server, metadata["lan"], new_home)
        success = True
    except BaseException:
        try:
            if changed:
                restore_database(backup / "server.sqlite3", server / "server.sqlite3")
                # The LAN helper owns attachment rollback, including late link failures.
            if native_result:
                for value in native_result["created_paths"]:
                    target = Path(value)
                    if target.parent != worker or target.is_symlink():
                        raise RuntimeError("Unexpected native rollback path; services left stopped")
                    if target.is_dir():
                        shutil.rmtree(target)
                    else:
                        target.unlink(missing_ok=True)
            for original, saved in reversed(moved_native):
                saved.rename(original)
        except BaseException as exc:
            recovery_failed = True
            raise RuntimeError("Import rollback needs review; destination services remain stopped. Backup: " + str(backup)) from exc
        raise
    finally:
        if lock:
            lock.close()
        if not recovery_failed:
            # Server first, worker next, recovery monitor last. Preserve prior inactive units.
            try:
                services.run("start", [name for name in reversed(SERVICES) if name in active])
            except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
                state = "History was imported; do not retry the import." if success else "Import did not complete."
                raise RuntimeError(state + " Destination services need attention. Backup: " + str(backup)) from exc
        if backup:
            try:
                write_json(backup / ("import-complete.json" if success else "import-rolled-back.json"),
                           {"success": success, "recovery_failed": recovery_failed})
            except OSError as exc:
                state = "History was imported; do not retry the import." if success else "Review the failed import."
                raise RuntimeError(state + " Cannot record final backup status: " + str(backup)) from exc
    return {**result, "backup": str(backup), "native": native_result, "services_restarted": active}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    export = sub.add_parser("export", help="Create a private ZIP without changing the running source")
    export.add_argument("--data-dir", type=Path, default=Path.home() / ".local/share/codex-lan/server")
    export.add_argument("--native-home", type=Path, default=Path.home() / ".local/share/codex-lan/host-worker")
    export.add_argument("--output", type=Path, required=True)
    export.add_argument("--room", action="append", required=True)
    export.add_argument("--asset-root", action="append", type=Path, default=[],
                        help="Explicit additional legacy upload directory; only referenced native images are copied")
    restore = sub.add_parser("restore", help="Import an extracted ZIP into an empty laptop installation")
    restore.add_argument("--bundle", type=Path, default=Path(__file__).resolve().parent)
    restore.add_argument("--data-root", type=Path, default=Path.home() / ".local/share/codex-lan")
    restore.add_argument("--codex", type=Path, default=Path.home() / ".local/bin/codex")
    verify = sub.add_parser("verify", help="Verify an extracted bundle without changing an installation")
    verify.add_argument("--bundle", type=Path, default=Path(__file__).resolve().parent)
    args = parser.parse_args(argv)
    os.umask(0o077)
    if os.geteuid() == 0:
        parser.error("Run as your normal desktop user, not root")
    try:
        if args.command == "export":
            result = export_bundle(args.data_dir, args.native_home, args.output, args.room, Path.home(), args.asset_root)
        elif args.command == "verify":
            metadata = validate_bundle(args.bundle)
            result = {"verified": True, "counts": metadata["lan"]["counts"], "native": metadata["native"]["counts"]}
        else:
            result = restore_bundle(args.bundle, args.data_root, Path.home(), args.codex)
        print(json.dumps(result, indent=2))
        return 0
    except (ValueError, OSError, RuntimeError, sqlite3.Error, subprocess.SubprocessError, KeyError) as exc:
        print("Transfer stopped: " + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
