"""CODEX: Preserve private history without copying usable application credentials or OpenAI login."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import sqlite3
import tarfile
import tempfile
import time
from pathlib import Path


FORMAT = "codex-lan-private-history-v1"
CREDENTIAL_TABLES = (
    "browser_logins", "browser_sessions", "device_credentials", "client_pairings",
    "client_maintenance", "login_attempts",
)
BACKUP_REASON = "Private history snapshot. Native sessions and old credentials were not transferred; automatic resume is disabled."


def regular_file(path, root):
    path, root = Path(path).absolute(), Path(root).resolve()
    if path.is_symlink() or not path.is_file() or path.resolve() != path or not path.is_relative_to(root):
        raise ValueError("A backup source is not a regular file inside its expected private directory")
    return path


def private_parent(path):
    path = Path(path).absolute()
    if path.is_symlink() or path.resolve() != path:
        raise ValueError("The private backup directory must not contain symbolic links")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not path.is_dir() or path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o077:
        raise ValueError("Choose a private backup directory owned by you with mode 0700")
    return path


def create_backup(data_dir, output):
    data_dir, output = Path(data_dir).absolute(), Path(output).absolute()
    source_root = Path(__file__).resolve().parent
    if output.is_relative_to(source_root):
        raise ValueError("Private history must be saved outside the source checkout")
    if output.exists() or output.is_symlink():
        raise ValueError("The backup target already exists; choose a new filename")
    if "private" not in output.name.casefold() or not output.name.endswith(".tar.gz"):
        raise ValueError("Use a filename containing 'private' and ending in .tar.gz to prevent accidental publication")
    if data_dir.is_symlink() or data_dir.resolve() != data_dir or data_dir.stat().st_uid != os.getuid():
        raise ValueError("Server state must be a real directory owned by the current user")
    database = regular_file(data_dir / "server.sqlite3", data_dir)
    parent = private_parent(output.parent)
    deadline = time.monotonic() + 60

    def progress(status, remaining, total):
        if time.monotonic() > deadline:
            raise TimeoutError("SQLite backup did not finish within 60 seconds; try again after prompts finish")

    with tempfile.TemporaryDirectory(prefix=".history-backup-", dir=parent) as temporary:
        stage = Path(temporary)
        snapshot = stage / "history.sqlite3"
        with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=5) as incoming:
            with sqlite3.connect(snapshot) as copied:
                incoming.backup(copied, pages=256, progress=progress, sleep=0.05)
        snapshot.chmod(0o600)
        with sqlite3.connect(snapshot) as copied:
            copied.row_factory = sqlite3.Row
            tables = {row[0] for row in copied.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not {"devices", "rooms", "messages", "runs", "attachments"} <= tables:
                raise ValueError("This is not a supported Codex LAN history database")
            for table in CREDENTIAL_TABLES:
                if table in tables:
                    copied.execute("DELETE FROM " + table)
            for device in copied.execute("SELECT id FROM devices").fetchall():
                copied.execute("UPDATE devices SET token_hash=?,status='revoked' WHERE id=?",
                               (hashlib.sha256(secrets.token_bytes(32)).hexdigest(), device["id"]))
            room_columns = {row[1] for row in copied.execute("PRAGMA table_info(rooms)")}
            if "read_only_reason" in room_columns:
                copied.execute("UPDATE rooms SET read_only_reason=?", (BACKUP_REASON,))
            copied.execute("UPDATE runs SET status='interrupted',error=?,finished_at=COALESCE(finished_at,?) "
                           "WHERE status IN ('running','queued')", (BACKUP_REASON, time.time()))
            uploads = stage / "uploads"
            uploads.mkdir(mode=0o700)
            attachment_manifest = []
            for item in copied.execute("SELECT id,path,size FROM attachments ORDER BY id").fetchall():
                original = regular_file(item["path"], data_dir / "uploads")
                name = hashlib.sha256(item["id"].encode("utf-8")).hexdigest()
                target = uploads / name
                digest = hashlib.sha256()
                with original.open("rb") as incoming, target.open("xb") as outgoing:
                    while chunk := incoming.read(65536):
                        digest.update(chunk)
                        outgoing.write(chunk)
                target.chmod(0o600)
                if target.stat().st_size != item["size"]:
                    raise ValueError("An attachment changed while being backed up; try after active prompts finish")
                copied.execute("UPDATE attachments SET path=? WHERE id=?", ("uploads/" + name, item["id"]))
                attachment_manifest.append({"id": item["id"], "file": "uploads/" + name,
                                            "size": item["size"], "sha256": digest.hexdigest()})
            counts = {table: copied.execute("SELECT count(*) FROM " + table).fetchone()[0]
                      for table in ("devices", "rooms", "messages", "runs", "attachments")}
            copied.commit()
            copied.execute("PRAGMA journal_mode=DELETE")
            copied.execute("VACUUM")
        manifest = {"format": FORMAT, "created_at": time.time(), "counts": counts,
                    "attachments": attachment_manifest, "resume_supported": False,
                    "excluded": ["OpenAI login", "admin token", "browser passwords and sessions", "pairing credentials",
                                 "TLS keys", "native Codex rollouts", "logs", "release installers"],
                    "notice": "Private chat contents may themselves contain sensitive information. Never publish this archive."}
        (stage / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            with os.fdopen(descriptor, "wb") as archive_file:
                with tarfile.open(fileobj=archive_file, mode="w:gz") as archive:
                    for path in sorted(stage.rglob("*")):
                        if path.is_file():
                            info = archive.gettarinfo(str(path), str(path.relative_to(stage)))
                            info.uid = info.gid = 0
                            info.uname = info.gname = ""
                            info.mode = 0o600
                            with path.open("rb") as incoming:
                                archive.addfile(info, incoming)
                archive_file.flush()
                os.fsync(archive_file.fileno())
        except BaseException:
            output.unlink(missing_ok=True)
            raise
    return counts


def main(argv=None):
    parser = argparse.ArgumentParser(description="Create a private, credential-free history snapshot; no services change")
    parser.add_argument("--data-dir", type=Path, default=Path.home() / ".local/share/codex-lan/server")
    parser.add_argument("--output", type=Path, required=True, help="New private-*.tar.gz filename outside the checkout")
    args = parser.parse_args(argv)
    os.umask(0o077)
    try:
        result = create_backup(args.data_dir.expanduser(), args.output.expanduser())
    except (OSError, ValueError, sqlite3.Error, TimeoutError) as error:
        parser.error(str(error))
    print("Private history snapshot created:", args.output)
    print(json.dumps(result))
    print("No credentials/native sessions were copied. This is not an automatic executable-history restore; keep the old host.")


if __name__ == "__main__":
    main()
