"""CODEX: Import a read-only desktop history snapshot without changing the source."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import shutil
import sqlite3
import time
import uuid
from pathlib import Path

from .media import image_mime
from .store import Store


def migrate(source, destination, admin_token_file, worker_home=None, sessions=None):
    source, destination = Path(source).resolve(), Path(destination).resolve()
    store = Store(destination)
    admin = store.authenticate(Path(admin_token_file).read_text().strip())
    store.list_devices(admin["id"])
    backups = destination / "backups"
    backups.mkdir(mode=0o700, exist_ok=True)
    snapshot = backups / ("desktop-" + str(time.time_ns()) + ".sqlite3")
    original = sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)
    old = sqlite3.connect(snapshot)
    original.backup(old)
    original.close()
    old.row_factory = sqlite3.Row
    uploads = destination / "uploads"
    uploads.mkdir(mode=0o700, exist_ok=True)
    old_uploads = source.parent / "uploads"
    counts = {"rooms": 0, "messages": 0, "attachments": 0, "threads": 0, "missing_images": 0}
    thread_ids = set()

    def key(table, old_id):
        return str(uuid.uuid5(uuid.NAMESPACE_URL, f"codex-lan:{source}:{table}:{old_id}"))

    def room(row, table="rooms", parent=None):
        rid = key(table, row["id"])
        thread = row.get("thread_id")
        if thread:
            thread_ids.add(thread)
        title = row.get("title") or "Side thread"
        params = (rid, admin["id"], title, str(Path.home()), row.get("model") or "", row.get("effort") or "",
                  thread, parent, row.get("selected_quote") or "", row["created_at"], row["updated_at"])
        result = store.db.execute(
            "INSERT OR IGNORE INTO rooms(id,owner_id,title,cwd,model,effort,thread_id,parent_room_id,reply_quote,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)", params)
        counts["rooms"] += result.rowcount
        return rid

    def message(row, rid, table="messages"):
        mid, run_id = key(table, row["id"]), key(table + "-run", row["id"])
        if store.db.execute("SELECT 1 FROM messages WHERE id=?", (mid,)).fetchone():
            return
        role = row["role"]
        if role not in {"user", "assistant"}:
            role = "assistant"
        attachment_ids = []
        paths = json.loads(row.get("image_paths") or "[]")
        if not paths and row.get("image_path"):
            paths = [row["image_path"]]
        for image in paths:
            path = Path(image).resolve()
            if not path.is_relative_to(old_uploads.resolve()) or not path.is_file():
                counts["missing_images"] += 1
                continue
            aid = key("attachment", str(path))
            if not store.db.execute("SELECT 1 FROM attachments WHERE id=?", (aid,)).fetchone():
                data = path.read_bytes()
                try:
                    mime = image_mime(data)
                except ValueError:
                    counts["missing_images"] += 1
                    continue
                target = uploads / aid
                if not target.exists():
                    shutil.copyfile(path, target)
                    target.chmod(0o600)
                store.db.execute("INSERT INTO attachments(id,owner_id,name,mime,size,path,created_at) VALUES(?,?,?,?,?,?,?)",
                                 (aid, admin["id"], path.name, mime, len(data), str(target), row["created_at"]))
                counts["attachments"] += 1
            attachment_ids.append(aid)
        user_id, assistant_id = (mid, None) if role == "user" else (None, mid)
        stamp = row["created_at"]
        store.db.execute(
            "INSERT INTO runs(id,owner_id,room_id,request_id,request_fingerprint,status,user_message_id,assistant_message_id,created_at,updated_at,finished_at) VALUES(?,?,?,?,?,'completed',?,?,?,?,?)",
            (run_id, admin["id"], rid, "legacy:" + mid, "legacy", user_id, assistant_id, stamp, stamp, stamp))
        content = row["content"]
        if row.get("reply_quote"):
            content = "In reply to:\n" + "\n".join("> " + line for line in row["reply_quote"].splitlines()) + "\n\n" + content
        store.db.execute(
            "INSERT INTO messages(id,room_id,run_id,role,content,attachment_ids,duration_seconds,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (mid, rid, run_id, role, content, json.dumps(attachment_ids), row.get("duration_seconds"), stamp))
        counts["messages"] += 1

    try:
        with store._lock, store.db:
            for entry in old.execute("SELECT * FROM rooms ORDER BY id"):
                row = dict(entry)
                rid = room(row)
                for item in old.execute("SELECT * FROM messages WHERE room_id=? ORDER BY created_at,id", (row["id"],)):
                    message(dict(item), rid)
            tables = {row[0] for row in old.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if {"branches", "branch_messages"} <= tables:
                for entry in old.execute("SELECT * FROM branches ORDER BY id"):
                    row = dict(entry)
                    rid = room(row, "branches", key("rooms", row["room_id"]))
                    for item in old.execute("SELECT * FROM branch_messages WHERE branch_id=? ORDER BY created_at,id", (row["id"],)):
                        message(dict(item), rid, "branch_messages")
    finally:
        old.close()
        store.close()
    if worker_home and sessions:
        sessions = Path(sessions).resolve()
        destination_sessions = Path(worker_home) / "sessions"
        for path in sessions.rglob("*.jsonl"):
            if path.is_file() and not path.is_symlink() and any(thread_id in path.name for thread_id in thread_ids):
                target = destination_sessions / path.relative_to(sessions)
                if not target.exists():
                    target.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
                    shutil.copyfile(path, target)
                    target.chmod(0o600)
                    counts["threads"] += 1
    return counts | {"backup": str(snapshot)}


def main():
    parser = argparse.ArgumentParser(description="Import desktop history into the new server, without modifying desktop data")
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--admin-token-file", type=Path, required=True)
    parser.add_argument("--worker-home", type=Path)
    parser.add_argument("--sessions", type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    args.data_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
    with (args.data_dir / "server.lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            parser.error("Stop only the new LAN server before importing; leave the desktop app running")
        print(json.dumps(migrate(args.source, args.data_dir, args.admin_token_file, args.worker_home, args.sessions)))


if __name__ == "__main__":
    main()
