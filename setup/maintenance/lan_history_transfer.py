"""Selective, credential-free LAN history transfer. The caller owns service locking.

Only the declarations below create payload SQL. Source schema SQL is never run.
All reads of a running source share one SQLite read transaction; importing requires
an existing, empty installation and its own administrator. Native threads are
handled separately by the transfer wrapper.
"""

from __future__ import annotations

from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import tempfile
import time
import uuid


FORMAT = "codex-lan-selected-history-v1"
HOME_MARKER = "@destination-home"
INTERRUPTED = "This run was active when its history was copied. It was interrupted in this copy; send a new message to continue."
STEERING_STOPPED = "Pending follow-up was not transferred for execution. Send it again if needed."
USAGE_POLICY = ("Selected per-run tokens and estimated credits are preserved as historical usage. "
                "Daily totals, quota deltas, acknowledgements and provider calibration are not transferred.")

# Keep this explicitly scoped: schema changes require an intentional format review.
SCHEMA = {
    "rooms": "id TEXT PRIMARY KEY,owner_id TEXT NOT NULL,title TEXT NOT NULL,cwd TEXT NOT NULL,"
             "model TEXT NOT NULL,effort TEXT NOT NULL,thread_id TEXT,parent_room_id TEXT,"
             "reply_quote TEXT NOT NULL,tool_read_only INTEGER NOT NULL,created_at REAL NOT NULL,"
             "updated_at REAL NOT NULL,execution_origin TEXT NOT NULL,read_only_reason TEXT NOT NULL,speed TEXT NOT NULL",
    "runs": "id TEXT PRIMARY KEY,owner_id TEXT NOT NULL,room_id TEXT NOT NULL,request_id TEXT NOT NULL,"
            "request_fingerprint TEXT NOT NULL,thread_id TEXT,turn_id TEXT,status TEXT NOT NULL,error TEXT,"
            "user_message_id TEXT,assistant_message_id TEXT,tool_read_only INTEGER NOT NULL,"
            "created_at REAL NOT NULL,updated_at REAL NOT NULL,finished_at REAL,model TEXT,effort TEXT,speed TEXT",
    "messages": "id TEXT PRIMARY KEY,room_id TEXT NOT NULL,run_id TEXT NOT NULL,role TEXT NOT NULL,"
                "content TEXT NOT NULL,attachment_ids TEXT NOT NULL,duration_seconds REAL,created_at REAL NOT NULL",
    "events": "id INTEGER PRIMARY KEY,run_id TEXT NOT NULL,room_id TEXT NOT NULL,kind TEXT NOT NULL,"
              "payload TEXT NOT NULL,event_key TEXT,created_at REAL NOT NULL",
    "steering_receipts": "id TEXT PRIMARY KEY,owner_id TEXT NOT NULL,room_id TEXT NOT NULL,run_id TEXT NOT NULL,"
                         "request_id TEXT NOT NULL,fingerprint TEXT NOT NULL,status TEXT NOT NULL,content TEXT NOT NULL,"
                         "attachment_ids TEXT NOT NULL,tool_read_only INTEGER NOT NULL,user_message_id TEXT,"
                         "replacement_run_id TEXT,error TEXT,created_at REAL NOT NULL,updated_at REAL NOT NULL",
    "usage_ledger": "run_id TEXT PRIMARY KEY,owner_id TEXT NOT NULL,input_tokens INTEGER NOT NULL,"
                    "output_tokens INTEGER NOT NULL,cached_tokens INTEGER NOT NULL,total_tokens INTEGER NOT NULL,updated_at REAL NOT NULL",
    "quota_usage": "run_id TEXT PRIMARY KEY,owner_id TEXT NOT NULL,credits_micros INTEGER,fingerprint TEXT NOT NULL,updated_at REAL NOT NULL",
    "attachments": "id TEXT PRIMARY KEY,owner_id TEXT NOT NULL,name TEXT NOT NULL,mime TEXT NOT NULL,"
                   "size INTEGER NOT NULL,path TEXT NOT NULL,created_at REAL NOT NULL",
}
COLUMNS = {table: tuple(part.split()[0] for part in declaration.split(",")) for table, declaration in SCHEMA.items()}
HISTORY_TABLES = tuple(SCHEMA)


def _directory(path: Path) -> Path:
    path = Path(path).absolute()
    if path.resolve() != path or path.is_symlink() or not path.is_dir():
        raise ValueError("Transfer directories must be real directories without symbolic links")
    return path


def _regular(path: Path, root: Path) -> Path:
    path = Path(path).absolute()
    if path.resolve() != path or not path.is_relative_to(root) or path.is_symlink():
        raise ValueError("Transfer files must remain inside their expected directory without symbolic links")
    try:
        info = path.stat()
    except FileNotFoundError:
        raise ValueError("A required history or attachment file is missing") from None
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError("Transfer files must be regular files without hard links")
    return path


def _read_db(path: Path):
    db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA trusted_schema=OFF")
    db.execute("PRAGMA query_only=ON")
    db.execute("BEGIN")
    return db


def _schema(db, payload=False):
    objects = list(db.execute("SELECT type,name,tbl_name,sql FROM sqlite_master"))
    tables = {row["name"] for row in objects if row["type"] == "table"}
    if not set(HISTORY_TABLES) <= tables:
        raise ValueError("Unsupported history schema: required tables are missing")
    if payload:
        if tables != set(HISTORY_TABLES) or any(
            row["type"] not in {"table", "index"}
            or row["type"] == "index" and row["sql"] is not None
            or row["type"] == "table" and "VIRTUAL" in (row["sql"] or "").upper()
            for row in objects
        ):
            raise ValueError("Payload contains unsupported schema objects")
        for row in objects:
            if row["type"] == "table" and row["sql"] != "CREATE TABLE " + row["name"] + "(" + SCHEMA[row["name"]] + ")":
                raise ValueError("Payload table declaration does not match the supported schema")
    for table, expected in COLUMNS.items():
        actual = {row["name"] for row in db.execute("PRAGMA table_info(" + table + ")")}
        if actual != set(expected):
            raise ValueError("Unsupported history columns in " + table)


def _attachment_ids(value):
    result = json.loads(value) if isinstance(value, str) else value
    if not isinstance(result, list) or any(not isinstance(item, str) or not item for item in result):
        raise ValueError("Invalid attachment references in history")
    return set(result)


def _event_attachments(value):
    result = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "attachment_ids":
                result.update(_attachment_ids(item))
            elif key == "attachment_id" and isinstance(item, str) and item:
                result.add(item)
            else:
                result.update(_event_attachments(item))
    elif isinstance(value, list):
        for item in value:
            result.update(_event_attachments(item))
    return result


def _rewrite(value, replacements, home):
    if isinstance(value, dict):
        return {key: home if key == "cwd" else _rewrite(item, replacements, home) for key, item in value.items()}
    if isinstance(value, list):
        return [_rewrite(item, replacements, home) for item in value]
    return replacements.get(value, value) if isinstance(value, str) else value


def _file_digest(path, size, destination=None):
    digest = hashlib.sha256()
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as incoming:
        before = os.fstat(incoming.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size != size:
            raise ValueError("An attachment is not a regular file of its recorded size")
        outgoing = None
        try:
            if destination is not None:
                output_descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                outgoing = os.fdopen(output_descriptor, "wb")
            while chunk := incoming.read(1024 * 1024):
                digest.update(chunk)
                if outgoing:
                    outgoing.write(chunk)
            after = os.fstat(incoming.fileno())
            if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise ValueError("An attachment changed during transfer; retry after the active run finishes")
            if outgoing:
                outgoing.flush()
                os.fsync(outgoing.fileno())
        finally:
            if outgoing:
                outgoing.close()
    return digest.hexdigest()


def _insert(db, table, row, preserve_rowid=False):
    columns = (("rowid",) if preserve_rowid else ()) + COLUMNS[table]
    db.execute("INSERT INTO " + table + "(" + ",".join(columns) + ") VALUES(" + ",".join("?" for _ in columns) + ")",
               tuple(row["_rowid"] if column == "rowid" else row[column] for column in columns))


def _room_ids(ids):
    if (not isinstance(ids, list) or not 1 <= len(ids) <= 100
            or any(not isinstance(item, str) or not item for item in ids) or len(set(ids)) != len(ids)):
        raise ValueError("Choose between one and 100 distinct room IDs")
    return ids


def _recover_interrupted(db, active_ids):
    """Materialize the saved partial answer so a terminal run remains visible in the UI."""
    recovered = 0
    for identifier in active_ids:
        db.row_factory = sqlite3.Row
        run = db.execute("SELECT * FROM runs WHERE id=?", (identifier,)).fetchone()
        if run["assistant_message_id"]:
            continue
        fragments, remaining, truncated = [], 2 * 1024 * 1024, False
        for index, event in enumerate(db.execute("SELECT payload FROM events WHERE run_id=? AND kind='delta' ORDER BY id LIMIT 10001", (identifier,))):
            if index >= 10000:
                truncated = True
                break
            payload = json.loads(event["payload"])
            text = payload.get("text") if isinstance(payload, dict) else None
            if not isinstance(text, str):
                continue
            accepted = text[:remaining]
            fragments.append(accepted)
            remaining -= len(accepted)
            if len(accepted) != len(text):
                truncated = True
                break
        content = "".join(fragments)
        if truncated:
            marker = "\n\n… [additional recovered response omitted; original delta events are retained in history]"
            content = content[:2 * 1024 * 1024 - len(marker)] + marker
        message_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "codex-lan-transfer-interrupted:" + identifier))
        db.execute("INSERT INTO messages(id,room_id,run_id,role,content,attachment_ids,duration_seconds,created_at) "
                   "VALUES(?,?,?,'assistant',?,'[]',?,?)",
                   (message_id, run["room_id"], identifier, content or INTERRUPTED,
                    max(0, run["finished_at"] - run["created_at"]), run["finished_at"]))
        db.execute("UPDATE runs SET assistant_message_id=? WHERE id=?", (message_id, identifier))
        recovered += 1
    return recovered


def export_history(data_dir: Path, destination: Path, room_ids: list[str]) -> dict:
    """Create a new payload directory, selecting only root rooms of one administrator."""
    data_dir = _directory(data_dir)
    source_path = _regular(data_dir / "server.sqlite3", data_dir)
    destination = Path(destination).absolute()
    _directory(destination.parent)
    if destination.exists() or destination.is_symlink() or destination.is_relative_to(data_dir):
        raise ValueError("History payload needs a new destination outside server state")
    room_ids = _room_ids(room_ids)
    placeholders = ",".join("?" for _ in room_ids)
    destination.mkdir(mode=0o700)
    try:
        with closing(_read_db(source_path)) as source, closing(sqlite3.connect(destination / "history.sqlite3")) as copied:
            source.row_factory = sqlite3.Row
            _schema(source)
            rooms = list(source.execute("SELECT * FROM rooms WHERE id IN (" + placeholders + ")", room_ids))
            owners = {row["owner_id"] for row in rooms}
            if len(rooms) != len(room_ids) or len(owners) != 1 or any(row["parent_room_id"] for row in rooms):
                raise ValueError("Select existing root rooms belonging to one administrator")
            owner = next(iter(owners))
            if not source.execute("SELECT 1 FROM devices WHERE id=? AND is_admin=1 AND status='approved'", (owner,)).fetchone():
                raise ValueError("Selected rooms must belong to the approved administrator")
            if source.execute("SELECT 1 FROM rooms WHERE parent_room_id IN (" + placeholders + ") LIMIT 1", room_ids).fetchone():
                raise ValueError("Rooms with branches or companions are not supported by this transfer format")
            tables = {row[0] for row in source.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "room_companions" in tables and source.execute(
                "SELECT 1 FROM room_companions WHERE parent_room_id IN (" + placeholders + ") OR room_id IN (" + placeholders + ") LIMIT 1",
                room_ids + room_ids,
            ).fetchone():
                raise ValueError("Companion rooms are not supported by this transfer format")
            for table in ("messages", "events", "steering_receipts"):
                if source.execute(
                    "SELECT 1 FROM " + table + " t JOIN runs r ON t.run_id=r.id "
                    "WHERE r.room_id IN (" + placeholders + ") AND t.room_id!=r.room_id LIMIT 1", room_ids,
                ).fetchone():
                    raise ValueError("Selected run history crosses room boundaries")
            for table, declaration in SCHEMA.items():
                copied.execute("CREATE TABLE " + table + "(" + declaration + ")")
            (destination / "history.sqlite3").chmod(0o600)
            attachments, thread_ids, active_ids = set(), set(), []
            for table in HISTORY_TABLES:
                if table in {"attachments", "events"}:
                    continue
                if table == "rooms":
                    condition = "id IN (" + placeholders + ")"
                elif table in {"usage_ledger", "quota_usage"}:
                    condition = "run_id IN (SELECT id FROM runs WHERE room_id IN (" + placeholders + "))"
                else:
                    condition = "room_id IN (" + placeholders + ")"
                fields = "rowid AS _rowid,*" if table == "messages" else "*"
                for incoming in source.execute("SELECT " + fields + " FROM " + table + " WHERE " + condition + " ORDER BY rowid", room_ids):
                    row = dict(incoming)
                    if row.get("thread_id"):
                        thread_ids.add(row["thread_id"])
                    if table == "rooms":
                        row["cwd"] = HOME_MARKER
                    if table == "runs" and row["status"] in {"running", "queued"}:
                        row.update(status="interrupted", error=INTERRUPTED, finished_at=row["finished_at"] or time.time())
                        active_ids.append(row["id"])
                    if table == "steering_receipts" and row["status"] in {"queued", "sending"}:
                        row.update(status="uncertain" if row["status"] == "sending" else "rejected", error=STEERING_STOPPED)
                    if "attachment_ids" in row:
                        attachments.update(_attachment_ids(row["attachment_ids"]))
                    _insert(copied, table, row, table == "messages")
            # Events are streamed twice: first discover references, then rewrite only attachment paths.
            event_sql = "SELECT * FROM events WHERE room_id IN (" + placeholders + ") ORDER BY id"
            for event in source.execute(event_sql, room_ids):
                attachments.update(_event_attachments(json.loads(event["payload"])))
            uploads = destination / "uploads"
            uploads.mkdir(mode=0o700)
            manifest, paths = [], {}
            for identifier in sorted(attachments):
                incoming = source.execute("SELECT * FROM attachments WHERE id=?", (identifier,)).fetchone()
                if incoming is None or incoming["owner_id"] != owner:
                    raise ValueError("A referenced attachment is missing or belongs to another owner")
                row = dict(incoming)
                original = _regular(Path(row["path"]), _directory(data_dir / "uploads"))
                relative = "uploads/" + hashlib.sha256(identifier.encode()).hexdigest()
                digest = _file_digest(original, row["size"], destination / relative)
                paths[row["path"]] = relative
                row["path"] = relative
                _insert(copied, "attachments", row)
                manifest.append({"id": identifier, "file": relative, "size": row["size"], "sha256": digest})
            for incoming in source.execute(event_sql, room_ids):
                row = dict(incoming)
                row["payload"] = json.dumps(_rewrite(json.loads(row["payload"]), paths, HOME_MARKER), ensure_ascii=False, separators=(",", ":"))
                _insert(copied, "events", row)
            recovered = _recover_interrupted(copied, active_ids)
            copied.commit()
            counts = {table: copied.execute("SELECT COUNT(*) FROM " + table).fetchone()[0] for table in HISTORY_TABLES}
        metadata = {"format": FORMAT, "source_owner_id": owner, "room_ids": list(room_ids), "thread_ids": sorted(thread_ids),
                    "counts": counts, "attachments": manifest, "interrupted_runs": len(active_ids),
                    "recovered_messages": recovered, "usage_policy": USAGE_POLICY}
        validate_history(destination, metadata)
        return metadata
    except BaseException:
        shutil.rmtree(destination)
        raise


def _validate_rows(db, metadata):
    owner, rooms = metadata["source_owner_id"], set(_room_ids(metadata["room_ids"]))
    actual_rooms = list(db.execute("SELECT id,owner_id,parent_room_id,cwd,thread_id FROM rooms"))
    if {row["id"] for row in actual_rooms} != rooms or any(
        row["owner_id"] != owner or row["parent_room_id"] is not None or row["cwd"] != HOME_MARKER for row in actual_rooms
    ):
        raise ValueError("Payload room selection or ownership is inconsistent")
    for table in ("runs", "steering_receipts", "usage_ledger", "quota_usage", "attachments"):
        if db.execute("SELECT 1 FROM " + table + " WHERE owner_id!=? LIMIT 1", (owner,)).fetchone():
            raise ValueError("Payload contains history from another owner")
    for table in ("runs", "messages", "events", "steering_receipts"):
        if db.execute("SELECT 1 FROM " + table + " t LEFT JOIN rooms r ON t.room_id=r.id WHERE r.id IS NULL LIMIT 1").fetchone():
            raise ValueError("History references an unselected room")
    for table in ("messages", "events", "steering_receipts", "usage_ledger", "quota_usage"):
        match = " OR t.room_id!=r.room_id" if table in {"messages", "events", "steering_receipts"} else ""
        if db.execute("SELECT 1 FROM " + table + " t LEFT JOIN runs r ON t.run_id=r.id WHERE r.id IS NULL" + match + " LIMIT 1").fetchone():
            raise ValueError("History references an absent or different-room run")
    for field, role in (("user_message_id", "user"), ("assistant_message_id", "assistant")):
        if db.execute("SELECT 1 FROM runs r LEFT JOIN messages m ON m.id=r." + field
                      + " WHERE r." + field + " IS NOT NULL AND (m.id IS NULL OR m.run_id!=r.id OR m.role!=?) LIMIT 1", (role,)).fetchone():
            raise ValueError("Run references an inconsistent message")
    if db.execute("SELECT 1 FROM steering_receipts s LEFT JOIN runs r ON s.replacement_run_id=r.id "
                  "WHERE s.replacement_run_id IS NOT NULL AND (r.id IS NULL OR r.room_id!=s.room_id) LIMIT 1").fetchone():
        raise ValueError("Steering replacement references a different room")
    if db.execute("SELECT 1 FROM steering_receipts s LEFT JOIN messages m ON s.user_message_id=m.id "
                  "WHERE s.user_message_id IS NOT NULL AND (m.id IS NULL OR m.room_id!=s.room_id OR m.role!='user') LIMIT 1").fetchone():
        raise ValueError("Steering receipt references an inconsistent message")
    if db.execute("SELECT 1 FROM runs WHERE status NOT IN ('completed','failed','cancelled','interrupted') LIMIT 1").fetchone():
        raise ValueError("Payload must not contain runnable work")
    if db.execute("SELECT 1 FROM steering_receipts WHERE status NOT IN ('accepted','rejected','uncertain') LIMIT 1").fetchone():
        raise ValueError("Payload must not contain pending follow-ups")
    if db.execute("SELECT 1 FROM messages WHERE role NOT IN ('user','assistant') LIMIT 1").fetchone():
        raise ValueError("Payload contains an unsupported message role")
    for table in ("rooms", "runs", "steering_receipts"):
        if db.execute("SELECT 1 FROM " + table + " WHERE tool_read_only NOT IN (0,1) LIMIT 1").fetchone():
            raise ValueError("Payload contains an unsupported access mode")
    if db.execute("SELECT 1 FROM rooms WHERE speed NOT IN ('standard','fast') LIMIT 1").fetchone():
        raise ValueError("Payload contains an unsupported room speed")
    references = set()
    for table in ("messages", "steering_receipts"):
        for row in db.execute("SELECT attachment_ids FROM " + table):
            references.update(_attachment_ids(row[0]))
    for row in db.execute("SELECT payload FROM events ORDER BY id"):
        references.update(_event_attachments(json.loads(row[0])))
    if references != {row[0] for row in db.execute("SELECT id FROM attachments")}:
        raise ValueError("Attachment references do not match the selected history")
    threads = {row[0] for row in db.execute("SELECT thread_id FROM rooms UNION SELECT thread_id FROM runs") if row[0]}
    if threads != set(metadata["thread_ids"]):
        raise ValueError("Native thread IDs do not match the selected history")
    counts = {table: db.execute("SELECT COUNT(*) FROM " + table).fetchone()[0] for table in HISTORY_TABLES}
    if counts != metadata["counts"]:
        raise ValueError("Payload record counts do not match the manifest")
    return counts


def validate_history(payload: Path, metadata: dict) -> dict:
    """Validate the untrusted payload before stopping destination services or writing state."""
    payload = _directory(payload)
    if (not isinstance(metadata, dict) or metadata.get("format") != FORMAT
            or not isinstance(metadata.get("source_owner_id"), str) or not metadata["source_owner_id"]
            or not isinstance(metadata.get("thread_ids"), list) or not isinstance(metadata.get("counts"), dict)
            or not isinstance(metadata.get("attachments"), list) or "room_ids" not in metadata):
        raise ValueError("Unsupported selected history manifest")
    if (any(not isinstance(thread, str) or not thread for thread in metadata["thread_ids"])
            or len(set(metadata["thread_ids"])) != len(metadata["thread_ids"])
            or set(metadata["counts"]) != set(HISTORY_TABLES)
            or any(type(count) is not int or count < 0 for count in metadata["counts"].values())):
        raise ValueError("Invalid history counts or native thread IDs")
    with closing(_read_db(_regular(payload / "history.sqlite3", payload))) as db:
        _schema(db, payload=True)
        if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError("History database failed its integrity check")
        counts = _validate_rows(db, metadata)
        records = {row["id"]: row for row in db.execute("SELECT id,path,size FROM attachments")}
        seen = set()
        for item in metadata["attachments"]:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str) or item["id"] in seen:
                raise ValueError("Invalid or duplicate attachment manifest entry")
            seen.add(item["id"])
            row = records.get(item["id"])
            expected_file = "uploads/" + hashlib.sha256(item["id"].encode()).hexdigest()
            if (row is None or item.get("file") != expected_file or row["path"] != expected_file
                    or type(item.get("size")) is not int or item["size"] < 0 or row["size"] != item["size"]
                    or not isinstance(item.get("sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", item["sha256"])):
                raise ValueError("Attachment manifest metadata does not match history")
            path = _regular(payload / expected_file, payload)
            if _file_digest(path, item["size"]) != item["sha256"]:
                raise ValueError("Attachment hash does not match the manifest")
        if seen != set(records):
            raise ValueError("Attachment manifest is incomplete")
    return counts


def _preflight(db, metadata):
    _schema(db)
    admins = list(db.execute("SELECT id FROM devices WHERE is_admin=1 AND status='approved'"))
    if len(admins) != 1 or admins[0]["id"] == metadata["source_owner_id"]:
        raise ValueError("Import requires a separate installation with its own approved administrator")
    for table in HISTORY_TABLES:
        if db.execute("SELECT 1 FROM " + table + " LIMIT 1").fetchone():
            raise ValueError("Destination history must be empty; existing rooms, runs or attachments cannot be merged")
    return admins[0]["id"]


def preflight_history(target_data_dir: Path, metadata: dict) -> dict:
    """Read-only destination check. The wrapper must repeat the import under server.lock."""
    target_data_dir = _directory(target_data_dir)
    with closing(_read_db(_regular(target_data_dir / "server.sqlite3", target_data_dir))) as db:
        owner = _preflight(db, metadata)
    return {"admin_id": owner}


def import_history(payload: Path, target_data_dir: Path, metadata: dict, new_home: Path) -> dict:
    """Import atomically while the caller holds server.lock and has stopped the host worker.

    Existing credentials and global quota state are never changed. Newly created
    attachment files are removed if the database transaction fails. The wrapper
    provides an installation backup for recovery from process or power failure.
    """
    counts = validate_history(payload, metadata)
    payload, target_data_dir, new_home = _directory(payload), _directory(target_data_dir), _directory(new_home)
    preflight_history(target_data_dir, metadata)
    database = _regular(target_data_dir / "server.sqlite3", target_data_dir)
    uploads = target_data_dir / "uploads"
    if uploads.exists() or uploads.is_symlink():
        _directory(uploads)
    else:
        uploads.mkdir(mode=0o700)
    installed, committed = [], False
    with tempfile.TemporaryDirectory(prefix=".history-import-", dir=target_data_dir) as temporary:
        stage = Path(temporary)
        for item in metadata["attachments"]:
            target = uploads / Path(item["file"]).name
            if target.exists() or target.is_symlink():
                raise ValueError("An attachment destination already exists; no files were overwritten")
            if _file_digest(_regular(payload / item["file"], payload), item["size"], stage / target.name) != item["sha256"]:
                raise ValueError("An attachment changed after validation")
        try:
            with closing(_read_db(_regular(payload / "history.sqlite3", payload))) as source, closing(sqlite3.connect(database, timeout=10)) as target:
                _schema(source, payload=True)
                _validate_rows(source, metadata)
                target.row_factory = sqlite3.Row
                target.execute("PRAGMA foreign_keys=ON")
                target.execute("PRAGMA trusted_schema=OFF")
                target.execute("BEGIN IMMEDIATE")
                target.execute("PRAGMA defer_foreign_keys=ON")
                owner = _preflight(target, metadata)
                replacements = {metadata["source_owner_id"]: owner}
                replacements.update({item["file"]: str(uploads / Path(item["file"]).name) for item in metadata["attachments"]})
                for table in HISTORY_TABLES:
                    for incoming in source.execute("SELECT rowid AS _rowid,* FROM " + table + " ORDER BY rowid"):
                        row = dict(incoming)
                        if "owner_id" in row:
                            row["owner_id"] = owner
                        if table == "rooms":
                            row["cwd"] = str(new_home)
                        elif table == "attachments":
                            row["path"] = replacements[row["path"]]
                        elif table == "events":
                            row["payload"] = json.dumps(_rewrite(json.loads(row["payload"]), replacements, str(new_home)),
                                                        ensure_ascii=False, separators=(",", ":"))
                        _insert(target, table, row, table == "messages")
                # New pending intents use only copied attachment records and local paths.
                for row in target.execute("SELECT m.attachment_ids,m.created_at,r.thread_id FROM messages m "
                                          "JOIN runs r ON r.id=m.run_id WHERE m.role='assistant' AND r.thread_id IS NOT NULL"):
                    for identifier in _attachment_ids(row["attachment_ids"]):
                        target.execute("INSERT OR IGNORE INTO artifact_deliveries(owner_id,thread_id,attachment_id,status,created_at,updated_at) "
                                       "VALUES(?,?,?,'pending',?,?)", (owner, row["thread_id"], identifier, row["created_at"], row["created_at"]))
                if target.execute("PRAGMA foreign_key_check").fetchone() or target.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise ValueError("Imported history failed database integrity checks")
                for item in metadata["attachments"]:
                    path = uploads / Path(item["file"]).name
                    os.link(stage / path.name, path, follow_symlinks=False)
                    installed.append(path)
                target.commit()
                committed = True
        except BaseException:
            if not committed:
                for path in installed:
                    path.unlink()
            raise
    return {"counts": counts, "admin_id": owner, "usage_policy": USAGE_POLICY}
