"""CODEX: Central persistence and device authorization for the LAN application."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import stat
import threading
import time
import unicodedata
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .quota_accounting import MAX_INTEGER, QuotaAccounting, share_percent
from .companions import CompanionStoreMixin


LIVE_STATUSES = frozenset({"queued", "running"})
TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled", "interrupted"})
DEVICE_STATUSES = frozenset({"pending", "approved", "revoked", "rejected"})
MAX_ATTACHMENT_STORAGE_BYTES = 512 * 1024 * 1024
# CODEX: A byte quota alone does not bound empty/tiny files or their database rows.
MAX_ATTACHMENT_COUNT = 2048
MAX_RECOVERED_RESPONSE_CHARACTERS = 2 * 1024 * 1024
MAX_RECOVERED_DELTA_EVENTS = 10_000
RECOVERED_RESPONSE_OMITTED = "\n\n… [additional recovered response omitted]"
LEGACY_ROOM_REASON = ("This room belongs to the previous client-run version and is now read-only. "
                      "Your messages and images are preserved. Create a new room to use server-managed execution.")
_UNSET = object()
DEFAULT_ROOM_TITLE = "New chat"
MAX_AUTO_ROOM_TITLE_CHARACTERS = 72
MAX_AUTO_ROOM_TITLE_WORDS = 9


def title_from_first_prompt(text: str) -> str:
    """CODEX: Derive a compact local title without spending another model request."""
    normalized = unicodedata.normalize("NFKC", text or "").replace("\r", "\n")
    lines = [" ".join(line.split()) for line in normalized.splitlines()]
    if lines and lines[0].casefold() == "in reply to this message:":
        lines = [line for line in lines[1:] if line and not line.startswith(">")]
    candidate = next((line for line in lines if line and line not in {"```", "~~~"}), "")
    if not candidate:
        candidate = " ".join(normalized.split())
    candidate = re.sub(r"^(?:#{1,6}|[-+*>•])\s+", "", candidate).strip()
    candidate = "".join(
        character if not unicodedata.category(character).startswith("C") else " "
        for character in candidate
    )
    candidate = " ".join(candidate.split()).strip(" \t\n.,;:!?؟،؛-_`'\"")
    words = candidate.split()
    if len(words) > MAX_AUTO_ROOM_TITLE_WORDS:
        candidate = " ".join(words[:MAX_AUTO_ROOM_TITLE_WORDS])
    if len(candidate) > MAX_AUTO_ROOM_TITLE_CHARACTERS:
        shortened = candidate[:MAX_AUTO_ROOM_TITLE_CHARACTERS + 1]
        boundary = shortened.rfind(" ")
        if boundary >= MAX_AUTO_ROOM_TITLE_CHARACTERS // 2:
            shortened = shortened[:boundary]
        candidate = shortened[:MAX_AUTO_ROOM_TITLE_CHARACTERS].rstrip(" \t\n.,;:!?؟،؛-_`'\"")
    return candidate or DEFAULT_ROOM_TITLE


class RoomReadOnlyError(ValueError):
    """CODEX: A preserved legacy room cannot start or modify execution."""


class RoomBusyError(ValueError):
    """CODEX: A room with queued or running work cannot change execution context."""


class DeviceBusyError(ValueError):
    """CODEX: A device with queued or running work cannot be deleted safely."""


def _prepare_private_database(data_dir: Path) -> None:
    """CODEX: Protect chat/password state even when callers have a permissive umask."""
    # Refuse directory indirection before tightening permissions on any existing path.
    if any(path.is_symlink() for path in (data_dir, *data_dir.absolute().parents)):
        raise ValueError("Server data directories must not use symlinks")
    data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory = os.open(data_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        metadata = os.fstat(directory)
        if metadata.st_uid != os.getuid():
            raise ValueError("Server data directory must belong to the current user")
        os.fchmod(directory, 0o700)
        # SQLite inherits the main database's mode for new WAL/SHM files. Repair
        # existing sidecars too, and never chmod/follow a substituted link or FIFO.
        for name in ("server.sqlite3", "server.sqlite3-wal", "server.sqlite3-shm", "server.sqlite3-journal"):
            flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK
            if name == "server.sqlite3":
                flags |= os.O_CREAT
            try:
                descriptor = os.open(name, flags, 0o600, dir_fd=directory)
            except FileNotFoundError:
                continue
            except OSError:
                raise ValueError("Private database state must use regular owned files, not links") from None
            try:
                metadata = os.fstat(descriptor)
                if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid()
                        or metadata.st_nlink != 1):
                    raise ValueError("Private database state must use regular owned files, not links")
                os.fchmod(descriptor, 0o600)
            finally:
                os.close(descriptor)
    finally:
        os.close(directory)


class Store(CompanionStoreMixin):
    """CODEX: Trusted server operations; never expose internal methods as RPC."""

    def __init__(self, data_dir: Optional[Path] = None) -> None:
        self.data_dir = Path(data_dir) if data_dir is not None else Path(
            os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share"))
        ) / "codex-lan"
        _prepare_private_database(self.data_dir)
        self.db_path = self.data_dir / "server.sqlite3"
        self._lock = threading.RLock()
        self.db = sqlite3.connect(
            self.db_path, check_same_thread=False, timeout=30
        )
        self.db.row_factory = sqlite3.Row
        with self._lock, self.db:
            self.db.executescript(
                """
                PRAGMA journal_mode=WAL;
                PRAGMA synchronous=NORMAL;
                PRAGMA wal_autocheckpoint=4000;
                PRAGMA journal_size_limit=67108864;
                PRAGMA foreign_keys=ON;
                PRAGMA busy_timeout=30000;
                CREATE TABLE IF NOT EXISTS devices (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    token_hash TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL CHECK(status IN ('pending','approved','revoked','rejected')),
                    is_admin INTEGER NOT NULL DEFAULT 0,
                    response_limits_enabled INTEGER NOT NULL DEFAULT 1 CHECK(response_limits_enabled IN (0,1)),
                    max_concurrent INTEGER NOT NULL DEFAULT 1 CHECK(max_concurrent > 0),
                    token_budget INTEGER CHECK(token_budget >= 0),
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    last_seen_at REAL,
                    connector_version TEXT NOT NULL DEFAULT '',
                    connector_confirmed_at REAL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS one_admin
                    ON devices(is_admin) WHERE is_admin = 1;
                CREATE TABLE IF NOT EXISTS browser_logins (
                    device_id TEXT PRIMARY KEY REFERENCES devices(id) ON DELETE CASCADE,
                    login_name TEXT NOT NULL,
                    login_key TEXT NOT NULL UNIQUE,
                    password_verifier TEXT NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS browser_sessions (
                    token_hash TEXT PRIMARY KEY,
                    device_id TEXT NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    last_seen_at REAL
                );
                CREATE INDEX IF NOT EXISTS browser_session_device
                    ON browser_sessions(device_id, expires_at);
                CREATE TABLE IF NOT EXISTS device_credentials (
                    id TEXT PRIMARY KEY,
                    device_id TEXT NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
                    token_hash TEXT NOT NULL UNIQUE,
                    kind TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    last_used_at REAL,
                    revoked_at REAL
                );
                CREATE INDEX IF NOT EXISTS device_credential_device
                    ON device_credentials(device_id, kind, revoked_at);
                CREATE TABLE IF NOT EXISTS client_maintenance (
                    device_id TEXT PRIMARY KEY REFERENCES devices(id) ON DELETE CASCADE,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS client_pairings (
                    code_hash TEXT PRIMARY KEY,
                    device_id TEXT NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
                    expires_at REAL NOT NULL,
                    connector_version TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS client_pairing_expiry
                    ON client_pairings(expires_at);
                CREATE TABLE IF NOT EXISTS login_attempts (
                    id TEXT PRIMARY KEY,
                    source TEXT NOT NULL,
                    attempted_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS login_attempt_source_time
                    ON login_attempts(source, attempted_at);
                CREATE INDEX IF NOT EXISTS login_attempt_time
                    ON login_attempts(attempted_at);
                CREATE TABLE IF NOT EXISTS rooms (
                    id TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL REFERENCES devices(id),
                    title TEXT NOT NULL,
                    cwd TEXT NOT NULL,
                    model TEXT NOT NULL DEFAULT '',
                    effort TEXT NOT NULL DEFAULT '',
                    thread_id TEXT,
                    parent_room_id TEXT REFERENCES rooms(id) ON DELETE SET NULL,
                    reply_quote TEXT NOT NULL DEFAULT '',
                    tool_read_only INTEGER NOT NULL DEFAULT 0 CHECK(tool_read_only IN (0,1)),
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS room_thread_owner
                    ON rooms(owner_id, thread_id) WHERE thread_id IS NOT NULL;
                CREATE INDEX IF NOT EXISTS room_owner ON rooms(owner_id, updated_at);
                CREATE TABLE IF NOT EXISTS attachments (
                    id TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL REFERENCES devices(id),
                    name TEXT NOT NULL,
                    mime TEXT NOT NULL,
                    size INTEGER NOT NULL CHECK(size >= 0),
                    path TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS attachment_owner_size ON attachments(owner_id, size);
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL REFERENCES devices(id),
                    room_id TEXT REFERENCES rooms(id) ON DELETE SET NULL,
                    request_id TEXT NOT NULL,
                    request_fingerprint TEXT NOT NULL,
                    thread_id TEXT,
                    turn_id TEXT,
                    status TEXT NOT NULL CHECK(status IN ('queued','running','completed','failed','cancelled','interrupted')),
                    error TEXT,
                    user_message_id TEXT,
                    assistant_message_id TEXT,
                    tool_read_only INTEGER NOT NULL DEFAULT 0 CHECK(tool_read_only IN (0,1)),
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    finished_at REAL,
                    UNIQUE(owner_id, request_id)
                );
                CREATE INDEX IF NOT EXISTS run_owner_status ON runs(owner_id, status);
                CREATE INDEX IF NOT EXISTS run_room ON runs(room_id, created_at);
                CREATE TABLE IF NOT EXISTS messages (
                    id TEXT PRIMARY KEY,
                    room_id TEXT NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
                    run_id TEXT NOT NULL REFERENCES runs(id),
                    role TEXT NOT NULL CHECK(role IN ('user','assistant')),
                    content TEXT NOT NULL,
                    attachment_ids TEXT NOT NULL DEFAULT '[]',
                    duration_seconds REAL,
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS message_room ON messages(room_id, created_at, id);
                CREATE TABLE IF NOT EXISTS artifact_deliveries (
                    owner_id TEXT NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
                    thread_id TEXT NOT NULL,
                    attachment_id TEXT NOT NULL REFERENCES attachments(id) ON DELETE CASCADE,
                    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','delivered')),
                    sha256 TEXT NOT NULL DEFAULT '',
                    attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY(owner_id,thread_id,attachment_id)
                );
                CREATE INDEX IF NOT EXISTS artifact_delivery_pending
                    ON artifact_deliveries(owner_id,status,created_at,attachment_id);
                INSERT OR IGNORE INTO artifact_deliveries(
                    owner_id,thread_id,attachment_id,status,created_at,updated_at
                )
                SELECT r.owner_id,r.thread_id,json_each.value,'pending',m.created_at,m.created_at
                FROM messages AS m
                JOIN runs AS r ON r.id=m.run_id
                JOIN json_each(CASE WHEN json_valid(m.attachment_ids) THEN m.attachment_ids ELSE '[]' END)
                JOIN attachments AS a ON a.id=json_each.value AND a.owner_id=r.owner_id
                WHERE m.role='assistant' AND r.thread_id IS NOT NULL AND r.thread_id!='';
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL REFERENCES runs(id),
                    room_id TEXT NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    event_key TEXT,
                    created_at REAL NOT NULL,
                    UNIQUE(run_id, event_key)
                );
                CREATE INDEX IF NOT EXISTS event_room ON events(room_id, id);
                CREATE INDEX IF NOT EXISTS event_run_kind_id ON events(run_id, kind, id);
                CREATE TABLE IF NOT EXISTS usage_ledger (
                    run_id TEXT PRIMARY KEY REFERENCES runs(id),
                    owner_id TEXT NOT NULL REFERENCES devices(id),
                    input_tokens INTEGER NOT NULL DEFAULT 0,
                    output_tokens INTEGER NOT NULL DEFAULT 0,
                    cached_tokens INTEGER NOT NULL DEFAULT 0,
                    total_tokens INTEGER NOT NULL DEFAULT 0,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS usage_daily (
                    owner_id TEXT NOT NULL REFERENCES devices(id),
                    day TEXT NOT NULL,
                    input_tokens INTEGER NOT NULL DEFAULT 0,
                    output_tokens INTEGER NOT NULL DEFAULT 0,
                    cached_tokens INTEGER NOT NULL DEFAULT 0,
                    total_tokens INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(owner_id,day)
                );
                CREATE TABLE IF NOT EXISTS steering_receipts (
                    id TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL REFERENCES devices(id),
                    room_id TEXT NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
                    run_id TEXT NOT NULL REFERENCES runs(id),
                    request_id TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('queued','sending','accepted','rejected','uncertain')),
                    content TEXT NOT NULL,
                    attachment_ids TEXT NOT NULL,
                    tool_read_only INTEGER NOT NULL DEFAULT 0 CHECK(tool_read_only IN (0,1)),
                    user_message_id TEXT,
                    replacement_run_id TEXT REFERENCES runs(id),
                    error TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    UNIQUE(owner_id,request_id)
                );
                CREATE INDEX IF NOT EXISTS steering_room_status ON steering_receipts(room_id,status,created_at);
                """
            )
        self._migrate_message_steering()
        self._migrate_execution_origins()
        self._migrate_connector_confirmations()
        self._migrate_response_limits()
        self._migrate_tool_read_only()
        self._migrate_steering_access_mode()
        self._migrate_companions()
        self._dummy_password_verifier = self._make_password_verifier(secrets.token_urlsafe(24))
        self.quota = QuotaAccounting(self.db)
        with self._lock, self.db:
            self.db.execute("BEGIN IMMEDIATE")
            self.quota.migrate()
        # CODEX: Runtime telemetry uses its own writer so WAL waits and checkpoints never freeze HTTP or WebSocket work.
        self._runtime_lock = threading.Lock()
        self.runtime_db = sqlite3.connect(self.db_path, check_same_thread=False, timeout=5)
        self.runtime_db.row_factory = sqlite3.Row
        with self._runtime_lock, self.runtime_db:
            self.runtime_db.executescript(
                """
                PRAGMA journal_mode=WAL;
                PRAGMA synchronous=NORMAL;
                PRAGMA wal_autocheckpoint=4000;
                PRAGMA journal_size_limit=67108864;
                PRAGMA foreign_keys=ON;
                PRAGMA busy_timeout=5000;
                """
            )
        # CODEX: Only the background writer may run automatic checkpoints.
        with self._lock:
            self.db.execute("PRAGMA wal_autocheckpoint=0")
        self.runtime_quota = QuotaAccounting(self.runtime_db)

    def _migrate_execution_origins(self):
        """CODEX: Existing rows stay local until their authenticated connector explicitly changes architecture."""
        with self._lock, self.db:
            for table, additions in {
                "devices": {"execution_mode": "TEXT NOT NULL DEFAULT 'local'"},
                "rooms": {"execution_origin": "TEXT NOT NULL DEFAULT 'local'",
                          "read_only_reason": "TEXT NOT NULL DEFAULT ''"},
            }.items():
                columns = {row[1] for row in self.db.execute("PRAGMA table_info(" + table + ")")}
                for column, declaration in additions.items():
                    if column not in columns:
                        self.db.execute("ALTER TABLE " + table + " ADD COLUMN " + column + " " + declaration)

    def _migrate_connector_confirmations(self):
        """CODEX: Preserve the last authenticated connector handshake across browser and worker disconnects."""
        with self._lock, self.db:
            columns = {row[1] for row in self.db.execute("PRAGMA table_info(devices)")}
            for column, declaration in {
                "connector_version": "TEXT NOT NULL DEFAULT ''",
                "connector_confirmed_at": "REAL",
            }.items():
                if column not in columns:
                    self.db.execute("ALTER TABLE devices ADD COLUMN " + column + " " + declaration)

    def _migrate_tool_read_only(self):
        """CODEX: Persist both the room preference and the immutable access mode of every run."""
        with self._lock, self.db:
            for table in ("rooms", "runs"):
                columns = {row[1] for row in self.db.execute("PRAGMA table_info(" + table + ")")}
                if "tool_read_only" not in columns:
                    self.db.execute(
                        "ALTER TABLE " + table
                        + " ADD COLUMN tool_read_only INTEGER NOT NULL DEFAULT 0 CHECK(tool_read_only IN (0,1))"
                    )

    def _migrate_response_limits(self):
        """CODEX: Keep existing client response guards while permanently exempting the administrator."""
        with self._lock, self.db:
            columns = {row[1] for row in self.db.execute("PRAGMA table_info(devices)")}
            if "response_limits_enabled" not in columns:
                self.db.execute(
                    "ALTER TABLE devices ADD COLUMN response_limits_enabled INTEGER NOT NULL DEFAULT 1 "
                    "CHECK(response_limits_enabled IN (0,1))"
                )
            self.db.execute(
                "UPDATE devices SET response_limits_enabled=0 "
                "WHERE is_admin=1 AND response_limits_enabled!=0"
            )

    def _migrate_steering_access_mode(self):
        """CODEX: Keep requested access changes and their replacement runs durable across reconnects."""
        with self._lock, self.db:
            columns = {row[1] for row in self.db.execute("PRAGMA table_info(steering_receipts)")}
            if "tool_read_only" not in columns:
                self.db.execute(
                    "ALTER TABLE steering_receipts ADD COLUMN tool_read_only INTEGER NOT NULL DEFAULT 0 "
                    "CHECK(tool_read_only IN (0,1))"
                )
            if "replacement_run_id" not in columns:
                self.db.execute(
                    "ALTER TABLE steering_receipts ADD COLUMN replacement_run_id TEXT REFERENCES runs(id)"
                )

    @staticmethod
    def _connector_version(value):
        if (not isinstance(value, str) or not 1 <= len(value) <= 80
                or any(ord(char) < 32 or ord(char) == 127 for char in value)):
            raise ValueError("Invalid connector version")
        version = value.strip()
        if not version:
            raise ValueError("Invalid connector version")
        return version

    def confirm_connector(self, device_id, client_version):
        """CODEX: Record only an authenticated connector handshake, never a browser-side version claim."""
        version = self._connector_version(client_version)
        with self._lock, self.db:
            self._actor(device_id)
            now = time.time()
            self.db.execute(
                "UPDATE devices SET connector_version=?,connector_confirmed_at=?,updated_at=? WHERE id=?",
                (version, now, now, device_id),
            )
            return self._row(self._device(device_id))

    def enable_server_execution(self, device_id, client_version=None):
        """CODEX: Called only after a valid executor hello; preserve old client conversations without migration."""
        version = None if client_version is None else self._connector_version(client_version)
        with self._lock, self.db:
            device = self._actor(device_id)
            if device["is_admin"]:
                if version is not None:
                    now = time.time()
                    self.db.execute(
                        "UPDATE devices SET connector_version=?,connector_confirmed_at=?,updated_at=? WHERE id=?",
                        (version, now, now, device_id),
                    )
                    return self._row(self._device(device_id))
                return self._row(device)
            if device["execution_mode"] != "server":
                self.db.execute("UPDATE rooms SET execution_origin='server' WHERE owner_id=? AND execution_origin='local' "
                                "AND thread_id IS NULL AND read_only_reason='' "
                                "AND NOT EXISTS (SELECT 1 FROM messages WHERE room_id=rooms.id) "
                                "AND NOT EXISTS (SELECT 1 FROM runs WHERE room_id=rooms.id)", (device_id,))
                self.db.execute("UPDATE rooms SET read_only_reason=? WHERE owner_id=? AND execution_origin='local'",
                                (LEGACY_ROOM_REASON, device_id))
            now = time.time()
            if version is None:
                self.db.execute("UPDATE devices SET execution_mode='server',updated_at=? WHERE id=?", (now, device_id))
            else:
                self.db.execute(
                    "UPDATE devices SET execution_mode='server',connector_version=?,connector_confirmed_at=?,updated_at=? WHERE id=?",
                    (version, now, now, device_id),
                )
            return self._row(self._device(device_id))

    @staticmethod
    def _assert_room_writable(room):
        if room["read_only_reason"]:
            raise RoomReadOnlyError(room["read_only_reason"])

    def assert_room_writable(self, device_id, room_id, own=False):
        with self._lock:
            room = self._room(device_id, room_id, own=own)
            self._assert_room_writable(room)
            return self._row(room)

    def _migrate_message_steering(self):
        """CODEX: Preserve message IDs and pagination rowids while allowing follow-up user messages."""
        with self._lock, self.db:
            self.db.execute("BEGIN IMMEDIATE")
            sql = self.db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='messages'").fetchone()[0]
            if "unique(run_id,role)" in "".join(sql.lower().split()):
                self.db.execute(
                    "CREATE TABLE messages_steering_upgrade (id TEXT PRIMARY KEY, "
                    "room_id TEXT NOT NULL REFERENCES rooms(id) ON DELETE CASCADE, "
                    "run_id TEXT NOT NULL REFERENCES runs(id), role TEXT NOT NULL CHECK(role IN ('user','assistant')), "
                    "content TEXT NOT NULL, attachment_ids TEXT NOT NULL DEFAULT '[]', "
                    "duration_seconds REAL, created_at REAL NOT NULL)"
                )
                self.db.execute("INSERT INTO messages_steering_upgrade(rowid,id,room_id,run_id,role,content,attachment_ids,duration_seconds,created_at) "
                                "SELECT rowid,id,room_id,run_id,role,content,attachment_ids,duration_seconds,created_at FROM messages")
                self.db.execute("DROP TABLE messages")
                self.db.execute("ALTER TABLE messages_steering_upgrade RENAME TO messages")
            self.db.execute("CREATE INDEX IF NOT EXISTS message_room ON messages(room_id,created_at,id)")
            self.db.execute("CREATE INDEX IF NOT EXISTS message_room_sequence ON messages(room_id)")
            self.db.execute("CREATE UNIQUE INDEX IF NOT EXISTS message_assistant_run ON messages(run_id) WHERE role='assistant'")

    @staticmethod
    def _id() -> str:
        return str(uuid.uuid4())

    @staticmethod
    def _text(value: Any, name: str, maximum: int = 512, empty: bool = False) -> str:
        if not isinstance(value, str) or "\x00" in value:
            raise ValueError(f"Invalid {name}")
        clean = value.strip()
        if (not clean and not empty) or len(value) > maximum:
            raise ValueError(f"Invalid {name}")
        return clean

    @staticmethod
    def _count(value: Any, name: str, minimum: int = 0, maximum: Optional[int] = None) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
            raise ValueError(f"Invalid {name}")
        if maximum is not None and value > maximum:
            raise ValueError(f"Invalid {name}")
        return value

    @staticmethod
    def _speed(value):
        if not isinstance(value, str) or value not in {"standard", "fast"}:
            raise ValueError("Invalid speed")
        return value

    @staticmethod
    def _boolean(value, name):
        if not isinstance(value, bool):
            raise ValueError(f"Invalid {name}")
        return int(value)

    @staticmethod
    def _login_identity(value: Any) -> tuple[str, str]:
        if not isinstance(value, str) or "\x00" in value:
            raise ValueError("Invalid sign-in name")
        display = unicodedata.normalize("NFKC", value).strip()
        if not 3 <= len(display) <= 64 or any(unicodedata.category(char).startswith("C") for char in display):
            raise ValueError("Sign-in name must be between 3 and 64 characters")
        return display, display.casefold()

    @staticmethod
    def _password(value: Any) -> str:
        if not isinstance(value, str) or "\x00" in value or not 8 <= len(value) <= 256:
            raise ValueError("Password must be between 8 and 256 characters")
        return value

    @staticmethod
    def _b64(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")

    @staticmethod
    def _unb64(value: str) -> bytes:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))

    @classmethod
    def _make_password_verifier(cls, password: str) -> str:
        password = cls._password(password)
        salt = secrets.token_bytes(16)
        digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=16384, r=8, p=1, dklen=32,
                                maxmem=64 * 1024 * 1024)
        return "$".join(("scrypt", "v1", "16384", "8", "1", cls._b64(salt), cls._b64(digest)))

    @classmethod
    def _password_matches(cls, password: Any, verifier: str) -> bool:
        candidate = password if isinstance(password, str) and "\x00" not in password and len(password) <= 256 else ""
        try:
            algorithm, version, n, r, p, salt, expected = verifier.split("$")
            if algorithm != "scrypt" or version != "v1":
                raise ValueError("Unsupported password verifier")
            actual = hashlib.scrypt(candidate.encode("utf-8"), salt=cls._unb64(salt), n=int(n), r=int(r), p=int(p),
                                    dklen=len(cls._unb64(expected)), maxmem=64 * 1024 * 1024)
            return hmac.compare_digest(actual, cls._unb64(expected)) and 8 <= len(candidate) <= 256
        except (ValueError, TypeError):
            return False

    @staticmethod
    def _row(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        token_hash = result.pop("token_hash", None)
        if token_hash:
            result["verification_code"] = token_hash[:4].upper() + "-" + token_hash[4:8].upper()
        result.pop("request_fingerprint", None)
        if "is_admin" in result:
            result["is_admin"] = bool(result["is_admin"])
            result["role"] = "admin" if result["is_admin"] else "client"
        if "response_limits_enabled" in result:
            result["response_limits_enabled"] = (
                not result.get("is_admin", False) and bool(result["response_limits_enabled"])
            )
        if "read_only_reason" in result:
            result["read_only"] = bool(result["read_only_reason"])
        if "tool_read_only" in result:
            result["tool_read_only"] = bool(result["tool_read_only"])
        for field in ("attachment_ids", "payload"):
            if field in result:
                result[field] = json.loads(result[field])
        return result

    def _device(self, device_id: str) -> sqlite3.Row:
        row = self.db.execute("SELECT * FROM devices WHERE id = ?", (device_id,)).fetchone()
        if row is None:
            raise PermissionError("Unknown device")
        return row

    def _actor(self, device_id: str, admin: bool = False) -> sqlite3.Row:
        row = self._device(device_id)
        if row["status"] != "approved" or (admin and not row["is_admin"]):
            raise PermissionError("Device is not authorized")
        return row

    def _room(self, device_id: str, room_id: str, own: bool = False) -> sqlite3.Row:
        actor = self._actor(device_id)
        room = self.db.execute("SELECT * FROM rooms WHERE id = ?", (room_id,)).fetchone()
        if room is None or (room["owner_id"] != device_id and (own or not actor["is_admin"])):
            raise PermissionError("Room is not accessible")
        return room

    def _run(self, run_id: str) -> sqlite3.Row:
        row = self.db.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            raise ValueError("Unknown run")
        return row

    def _attachment(self, device_id: str, attachment_id: str, own: bool = False) -> sqlite3.Row:
        actor = self._actor(device_id)
        row = self.db.execute("SELECT * FROM attachments WHERE id = ?", (attachment_id,)).fetchone()
        if row is None or (row["owner_id"] != device_id and (own or not actor["is_admin"])):
            raise PermissionError("Attachment is not accessible")
        return row

    def _enroll(self, name: str, admin: bool) -> dict[str, Any]:
        name = self._text(name, "device name", 128)
        device_id, token, now = self._id(), secrets.token_urlsafe(32), time.time()
        digest = hashlib.sha256(token.encode()).hexdigest()
        self.db.execute(
            "INSERT INTO devices(id,name,token_hash,status,is_admin,response_limits_enabled,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (device_id, name, digest, "approved" if admin else "pending", int(admin), int(not admin), now, now),
        )
        return {"device": self._row(self._device(device_id)), "token": token}

    def bootstrap_admin(self, name: str) -> dict[str, Any]:
        with self._lock, self.db:
            if self.db.execute("SELECT 1 FROM devices WHERE is_admin = 1").fetchone():
                raise ValueError("Administrator already exists")
            try:
                return self._enroll(name, True)
            except sqlite3.IntegrityError as exc:
                raise ValueError("Administrator already exists") from exc

    def enroll(self, name: str) -> dict[str, Any]:
        with self._lock, self.db:
            return self._enroll(name, False)

    def authenticate(self, token: str, allow_pending: bool = False) -> dict[str, Any]:
        if not isinstance(token, str) or not token or len(token) > 1024:
            raise PermissionError("Invalid device credential")
        digest = hashlib.sha256(token.encode()).hexdigest()
        with self._lock, self.db:
            row = self.db.execute("SELECT * FROM devices WHERE token_hash = ?", (digest,)).fetchone()
            credential = None
            if row is None:
                credential = self.db.execute(
                    "SELECT device_id FROM device_credentials WHERE token_hash=? AND revoked_at IS NULL",
                    (digest,),
                ).fetchone()
                if credential is not None:
                    row = self.db.execute("SELECT * FROM devices WHERE id=?", (credential["device_id"],)).fetchone()
            allowed = {"approved", "pending"} if allow_pending else {"approved"}
            if row is None or row["status"] not in allowed:
                raise PermissionError("Device is not authorized")
            if credential is not None:
                self.db.execute("UPDATE device_credentials SET last_used_at=? WHERE token_hash=?", (time.time(), digest))
            self.db.execute("UPDATE devices SET last_seen_at = ? WHERE id = ?", (time.time(), row["id"]))
            return self._row(self._device(row["id"]))

    def authenticate_primary_token(self, token: str, allow_pending: bool = False) -> dict[str, Any]:
        if not isinstance(token, str) or not token or len(token) > 1024:
            raise PermissionError("Invalid device credential")
        digest = hashlib.sha256(token.encode()).hexdigest()
        with self._lock, self.db:
            row = self.db.execute("SELECT * FROM devices WHERE token_hash=?", (digest,)).fetchone()
            allowed = {"approved", "pending"} if allow_pending else {"approved"}
            if row is None or row["status"] not in allowed:
                raise PermissionError("Device is not authorized")
            self.db.execute("UPDATE devices SET last_seen_at=? WHERE id=?", (time.time(), row["id"]))
            return self._row(self._device(row["id"]))

    def issue_device_credential(self, device_id: str, kind: str = "connector") -> str:
        if not isinstance(kind, str) or not 1 <= len(kind) <= 40 or not kind.replace("-", "").isalnum():
            raise ValueError("Invalid credential kind")
        with self._lock, self.db:
            self._actor(device_id)
            token, now = secrets.token_urlsafe(32), time.time()
            self.db.execute(
                "INSERT INTO device_credentials(id,device_id,token_hash,kind,created_at) VALUES(?,?,?,?,?)",
                (self._id(), device_id, hashlib.sha256(token.encode()).hexdigest(), kind, now),
            )
            return token

    def browser_login_profile(self, device_id: str) -> dict[str, Any]:
        with self._lock:
            self._device(device_id)
            row = self.db.execute(
                "SELECT login_name FROM browser_logins WHERE device_id=?", (device_id,)
            ).fetchone()
            return {"login_name": row["login_name"] if row else "", "password_login_enabled": row is not None}

    def set_browser_login(
        self, device_id: str, login_name: Any, password: Any, revoke_sessions: bool = True,
    ) -> dict[str, Any]:
        display, key = self._login_identity(login_name)
        verifier = self._make_password_verifier(self._password(password))
        if not isinstance(revoke_sessions, bool):
            raise ValueError("Invalid session setting")
        with self._lock, self.db:
            self._actor(device_id)
            try:
                self.db.execute(
                    "INSERT INTO browser_logins(device_id,login_name,login_key,password_verifier,updated_at) "
                    "VALUES(?,?,?,?,?) ON CONFLICT(device_id) DO UPDATE SET "
                    "login_name=excluded.login_name,login_key=excluded.login_key,"
                    "password_verifier=excluded.password_verifier,updated_at=excluded.updated_at",
                    (device_id, display, key, verifier, time.time()),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError("That sign-in name is already in use") from exc
            if revoke_sessions:
                self.db.execute("DELETE FROM browser_sessions WHERE device_id=?", (device_id,))
            return {"login_name": display, "password_login_enabled": True}

    def clear_browser_login(self, device_id: str) -> None:
        with self._lock, self.db:
            self._actor(device_id)
            self.db.execute("DELETE FROM browser_logins WHERE device_id=?", (device_id,))
            self.db.execute("DELETE FROM browser_sessions WHERE device_id=?", (device_id,))

    def authenticate_password(self, login_name: Any, password: Any) -> dict[str, Any]:
        try:
            _display, key = self._login_identity(login_name)
        except ValueError:
            key = ""
        with self._lock:
            login = self.db.execute(
                "SELECT device_id,password_verifier FROM browser_logins WHERE login_key=?", (key,)
            ).fetchone()
            verifier = login["password_verifier"] if login else self._dummy_password_verifier
            matches = self._password_matches(password, verifier)
            row = self._device(login["device_id"]) if login else None
            if not matches or row is None or row["status"] != "approved":
                raise PermissionError("Sign-in details are incorrect")
        with self._lock, self.db:
            self.db.execute("UPDATE devices SET last_seen_at=? WHERE id=?", (time.time(), row["id"]))
            return self._row(self._device(row["id"]))

    def create_browser_session(self, device_id: str, remember: bool = False) -> tuple[str, int]:
        if not isinstance(remember, bool):
            raise ValueError("Invalid remember setting")
        lifetime = 30 * 86400 if remember else 24 * 3600
        with self._lock, self.db:
            row = self._device(device_id)
            if row["status"] not in {"approved", "pending"}:
                raise PermissionError("Device is not authorized")
            token, now = secrets.token_urlsafe(32), time.time()
            self.db.execute("DELETE FROM browser_sessions WHERE expires_at<=?", (now,))
            self.db.execute(
                "INSERT INTO browser_sessions(token_hash,device_id,created_at,expires_at) VALUES(?,?,?,?)",
                (hashlib.sha256(token.encode()).hexdigest(), device_id, now, now + lifetime),
            )
            return token, lifetime

    def authenticate_browser_cookie(
        self, token: str, allow_pending: bool = False,
    ) -> tuple[dict[str, Any], bool]:
        if not isinstance(token, str) or not token or len(token) > 1024:
            raise PermissionError("Invalid browser session")
        digest, now = hashlib.sha256(token.encode()).hexdigest(), time.time()
        with self._lock:
            session = self.db.execute(
                "SELECT device_id,expires_at FROM browser_sessions WHERE token_hash=?", (digest,)
            ).fetchone()
            if session is None:
                return self.authenticate_primary_token(token, allow_pending=allow_pending), True
            row = self._device(session["device_id"])
            allowed = {"approved", "pending"} if allow_pending else {"approved"}
            if session["expires_at"] <= now or row["status"] in {"revoked", "rejected"}:
                with self.db:
                    self.db.execute("DELETE FROM browser_sessions WHERE token_hash=?", (digest,))
                raise PermissionError("Browser session is no longer authorized")
            if row["status"] not in allowed:
                raise PermissionError("Device is not authorized")
            with self.db:
                self.db.execute("UPDATE browser_sessions SET last_seen_at=? WHERE token_hash=?", (now, digest))
                self.db.execute("UPDATE devices SET last_seen_at=? WHERE id=?", (now, row["id"]))
            return self._row(self._device(row["id"])), False

    def authenticate_browser_session(self, token: str, allow_pending: bool = False) -> dict[str, Any]:
        return self.authenticate_browser_cookie(token, allow_pending=allow_pending)[0]

    def begin_login_attempt(
        self, source: str, window: float = 60, per_source: int = 5, global_limit: int = 50,
    ) -> Optional[str]:
        if not isinstance(source, str) or not source or len(source) > 255:
            source = "unknown"
        if window <= 0 or per_source <= 0 or global_limit <= 0:
            raise ValueError("Invalid sign-in rate limit")
        now, attempt_id = time.time(), self._id()
        with self._lock, self.db:
            self.db.execute("DELETE FROM login_attempts WHERE attempted_at<=?", (now - float(window),))
            source_count = self.db.execute(
                "SELECT COUNT(*) FROM login_attempts WHERE source=?", (source,)
            ).fetchone()[0]
            global_count = self.db.execute("SELECT COUNT(*) FROM login_attempts").fetchone()[0]
            if source_count >= per_source or global_count >= global_limit:
                return None
            self.db.execute(
                "INSERT INTO login_attempts(id,source,attempted_at) VALUES(?,?,?)",
                (attempt_id, source, now),
            )
            return attempt_id

    def finish_login_attempt(self, attempt_id: str, successful: bool = False) -> None:
        if not successful or not isinstance(attempt_id, str) or not attempt_id:
            return
        with self._lock, self.db:
            self.db.execute("DELETE FROM login_attempts WHERE id=?", (attempt_id,))

    def revoke_browser_session(self, token: str) -> None:
        if not isinstance(token, str) or not token:
            return
        with self._lock, self.db:
            self.db.execute(
                "DELETE FROM browser_sessions WHERE token_hash=?", (hashlib.sha256(token.encode()).hexdigest(),)
            )

    def get_device(self, device_id: str) -> dict[str, Any]:
        """CODEX: Internal lookup; HTTP callers must not freely enumerate IDs."""
        with self._lock:
            return self._row(self._device(device_id))

    def list_devices(self, admin_id: str) -> list[dict[str, Any]]:
        with self._lock:
            self._actor(admin_id, admin=True)
            return [self._row(row) for row in self.db.execute("SELECT * FROM devices ORDER BY created_at,id")]

    def set_device(
        self, admin_id: str, device_id: str, status: Optional[str] = None,
        max_concurrent: Optional[int] = None, token_budget: Any = _UNSET,
        weekly_share_percent: Any = _UNSET,
        response_limits_enabled: Any = _UNSET,
    ) -> dict[str, Any]:
        with self._lock, self.db:
            self._actor(admin_id, admin=True)
            target = self._device(device_id)
            changes: dict[str, Any] = {}
            if status is not None:
                if not isinstance(status, str) or status not in DEVICE_STATUSES:
                    raise ValueError("Invalid device status")
                if target["is_admin"] and status != "approved":
                    raise ValueError("Cannot disable the administrator")
                if status == "pending" and target["status"] != "pending":
                    raise ValueError("Approved or rejected devices cannot return to pending")
                changes["status"] = status
            if max_concurrent is not None:
                changes["max_concurrent"] = self._count(
                    max_concurrent, "concurrency", 1, MAX_INTEGER
                )
            if token_budget is not _UNSET:
                changes["token_budget"] = None if token_budget is None else self._count(token_budget, "token budget", maximum=MAX_INTEGER)
            if weekly_share_percent is not _UNSET:
                changes["weekly_share_percent"] = share_percent(weekly_share_percent)
            if response_limits_enabled is not _UNSET:
                enabled = self._boolean(response_limits_enabled, "response limits")
                if target["is_admin"] and enabled:
                    raise ValueError("Response limits cannot be enabled for the administrator")
                changes["response_limits_enabled"] = enabled
            if changes:
                changes["updated_at"] = time.time()
                self.db.execute(
                    "UPDATE devices SET " + ",".join(f"{key} = ?" for key in changes) + " WHERE id = ?",
                    (*changes.values(), device_id),
                )
            return self._row(self._device(device_id))

    def delete_device(self, admin_id: str, device_id: str) -> dict[str, Any]:
        """CODEX: Atomically remove one non-admin device and every database row tied to it."""
        with self._lock, self.db:
            self._actor(admin_id, admin=True)
            target = self._device(device_id)
            if device_id == admin_id or target["is_admin"]:
                raise ValueError("The host administrator cannot be deleted")
            if self.db.execute(
                "SELECT 1 FROM runs WHERE owner_id=? AND status IN ('queued','running') LIMIT 1",
                (device_id,),
            ).fetchone():
                raise DeviceBusyError("Stop this device's active responses before deleting it")

            attachment_paths = [
                row["path"] for row in self.db.execute(
                    "SELECT path FROM attachments WHERE owner_id=? ORDER BY id", (device_id,)
                )
            ]
            run_ids = [
                row["id"] for row in self.db.execute(
                    "SELECT id FROM runs WHERE owner_id=? ORDER BY id", (device_id,)
                )
            ]

            self.db.execute(
                "DELETE FROM quota_unknown_acknowledgements WHERE owner_id=? OR acknowledged_by=? "
                "OR run_id IN (SELECT id FROM runs WHERE owner_id=?)",
                (device_id, device_id, device_id),
            )
            self.db.execute(
                "DELETE FROM quota_deltas WHERE owner_id=? "
                "OR run_id IN (SELECT id FROM runs WHERE owner_id=?)",
                (device_id, device_id),
            )
            self.db.execute(
                "DELETE FROM quota_usage WHERE owner_id=? "
                "OR run_id IN (SELECT id FROM runs WHERE owner_id=?)",
                (device_id, device_id),
            )
            self.quota.checkpoint_after_device_deletion()
            self.db.execute(
                "DELETE FROM steering_receipts WHERE owner_id=? "
                "OR room_id IN (SELECT id FROM rooms WHERE owner_id=?) "
                "OR run_id IN (SELECT id FROM runs WHERE owner_id=?) "
                "OR replacement_run_id IN (SELECT id FROM runs WHERE owner_id=?)",
                (device_id, device_id, device_id, device_id),
            )
            self.db.execute(
                "DELETE FROM messages WHERE room_id IN (SELECT id FROM rooms WHERE owner_id=?) "
                "OR run_id IN (SELECT id FROM runs WHERE owner_id=?)",
                (device_id, device_id),
            )
            self.db.execute(
                "DELETE FROM events WHERE room_id IN (SELECT id FROM rooms WHERE owner_id=?) "
                "OR run_id IN (SELECT id FROM runs WHERE owner_id=?)",
                (device_id, device_id),
            )
            self.db.execute(
                "DELETE FROM usage_ledger WHERE owner_id=? "
                "OR run_id IN (SELECT id FROM runs WHERE owner_id=?)",
                (device_id, device_id),
            )
            self.db.execute("DELETE FROM usage_daily WHERE owner_id=?", (device_id,))
            self.db.execute("DELETE FROM runs WHERE owner_id=?", (device_id,))
            self.db.execute("DELETE FROM rooms WHERE owner_id=?", (device_id,))
            self.db.execute("DELETE FROM attachments WHERE owner_id=?", (device_id,))
            self.db.execute("DELETE FROM client_maintenance WHERE device_id=?", (device_id,))
            self.db.execute("DELETE FROM browser_sessions WHERE device_id=?", (device_id,))
            self.db.execute("DELETE FROM browser_logins WHERE device_id=?", (device_id,))
            self.db.execute("DELETE FROM device_credentials WHERE device_id=?", (device_id,))
            deleted = self.db.execute("DELETE FROM devices WHERE id=?", (device_id,)).rowcount
            if deleted != 1:
                raise PermissionError("Unknown device")
            attachment_paths = sorted({
                path for path in attachment_paths
                if not self.db.execute(
                    "SELECT 1 FROM attachments WHERE path=? LIMIT 1", (path,)
                ).fetchone()
            })
            return {"device_id": device_id, "attachment_paths": attachment_paths, "run_ids": run_ids}

    def list_rooms(self, device_id: str) -> list[dict[str, Any]]:
        with self._lock:
            actor = self._actor(device_id)
            where, args = ("", ()) if actor["is_admin"] else (" WHERE rooms.owner_id = ?", (device_id,))
            # CODEX: Expose content-free message recency so clients can lift active unpinned rooms reliably.
            rows = self.db.execute(
                "SELECT rooms.*, owner.name AS owner_name, companion.kind AS companion_kind, companion.id AS companion_id, "
                "latest_assistant.id AS last_assistant_message_id, latest_assistant.created_at AS last_assistant_message_at, "
                "latest_message.id AS last_message_id, latest_message.created_at AS last_message_at, "
                "latest_message.rowid AS last_message_sequence FROM rooms "
                "JOIN devices AS owner ON owner.id=rooms.owner_id "
                "LEFT JOIN room_companions AS companion ON companion.room_id=rooms.id "
                "LEFT JOIN messages AS latest_assistant ON latest_assistant.rowid = ("
                "SELECT MAX(rowid) FROM messages WHERE room_id = rooms.id AND role = 'assistant')"
                " LEFT JOIN messages AS latest_message ON latest_message.rowid = ("
                "SELECT MAX(rowid) FROM messages WHERE room_id = rooms.id)"
                + where + " ORDER BY rooms.created_at ASC,rooms.id", args,
            )
            return [self._row(row) for row in rows]

    def get_room(self, device_id: str, room_id: str) -> dict[str, Any]:
        with self._lock:
            result = self._row(self._room(device_id, room_id))
            companion = self.companion_for_room(room_id)
            if companion:
                result.update(companion_kind=companion["kind"], companion_id=companion["id"])
            return result

    def create_room(
        self, device_id: str, title: str, cwd: str, model: str = "", effort: str = "",
        parent_room_id: Optional[str] = None, reply_quote: str = "",
        speed: str = "standard", tool_read_only: bool = False,
    ) -> dict[str, Any]:
        title = self._text(title, "title", 200)
        cwd = self._text(cwd, "working directory", 4096)
        if not Path(cwd).is_absolute():
            raise ValueError("Working directory must be absolute")
        model = self._text(model, "model", 200, empty=True)
        effort = self._text(effort, "effort", 80, empty=True)
        speed = self._speed(speed)
        tool_read_only = self._boolean(tool_read_only, "read-only setting")
        if not isinstance(reply_quote, str) or len(reply_quote) > 100000:
            raise ValueError("Invalid reply quote")
        with self._lock, self.db:
            actor = self._actor(device_id)
            if parent_room_id is not None:
                self._assert_room_writable(self._room(device_id, parent_room_id, own=True))
                if not reply_quote.strip():
                    raise ValueError("Side rooms require a selected quote")
            elif reply_quote:
                raise ValueError("Reply quote requires a parent room")
            room_id, now = self._id(), time.time()
            self.db.execute(
                "INSERT INTO rooms(id,owner_id,title,cwd,model,effort,parent_room_id,reply_quote,created_at,updated_at,speed,execution_origin,tool_read_only) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (room_id, device_id, title, cwd, model, effort, parent_room_id, reply_quote, now, now,
                 speed, actor["execution_mode"], tool_read_only),
            )
            return self._row(self._room(device_id, room_id))

    def update_room(self, device_id: str, room_id: str, **fields: Any) -> dict[str, Any]:
        if not fields or set(fields) - {"title", "cwd", "model", "effort", "thread_id", "speed", "tool_read_only"}:
            raise ValueError("Invalid room fields")
        clean = {}
        for key, value in fields.items():
            if key == "speed":
                clean[key] = self._speed(value)
            elif key == "tool_read_only":
                clean[key] = self._boolean(value, "read-only setting")
            elif key == "cwd":
                clean[key] = self._text(value, "working directory", 4096)
                if not Path(clean[key]).is_absolute():
                    raise ValueError("Working directory must be absolute")
            else:
                clean[key] = None if key == "thread_id" and value is None else self._text(
                    value, key, 512, empty=key in {"model", "effort"}
                )
        with self._lock, self.db:
            room = self._room(device_id, room_id)
            self._assert_room_writable(room)
            self.enforce_companion_settings(room_id, clean)
            if "cwd" in clean and clean["cwd"] != room["cwd"]:
                if room["owner_id"] != device_id:
                    raise PermissionError("Only the owning computer can change this room's workspace")
                if self.db.execute(
                    "SELECT 1 FROM runs WHERE room_id=? AND status IN ('queued','running') LIMIT 1",
                    (room_id,),
                ).fetchone():
                    raise RoomBusyError("Stop this room's active response before changing its workspace")
            clean["updated_at"] = time.time()
            try:
                self.db.execute(
                    "UPDATE rooms SET " + ",".join(f"{key} = ?" for key in clean) + " WHERE id = ?",
                    (*clean.values(), room_id),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError("Thread is already assigned to a room") from exc
            return self._row(self._room(device_id, room_id))

    def _message(self, message_id: str) -> dict[str, Any]:
        row = self.db.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
        if row is None:
            raise ValueError("Message no longer exists")
        return self._message_rows([row])[0]

    def _message_rows(self, rows) -> list[dict[str, Any]]:
        """CODEX: Load one page's metadata together instead of querying each saved answer."""
        messages = [self._row(row) for row in rows]
        run_ids = tuple({message["run_id"] for message in messages if message["role"] == "assistant"})
        if not run_ids:
            return messages
        placeholders = ",".join("?" for _ in run_ids)
        runs = {row["id"]: row for row in self.db.execute(
            "SELECT id,status,error FROM runs WHERE id IN (" + placeholders + ")", run_ids,
        )}
        ledgers = {row["run_id"]: row for row in self.db.execute(
            "SELECT * FROM usage_ledger WHERE run_id IN (" + placeholders + ")", run_ids,
        )}
        estimates = {row["run_id"]: row["credits_micros"] for row in self.db.execute(
            "SELECT run_id,credits_micros FROM quota_usage WHERE run_id IN (" + placeholders + ")", run_ids,
        )}
        for result in messages:
            if result["role"] != "assistant":
                continue
            run = runs.get(result["run_id"])
            result["run_status"] = run["status"] if run else None
            result["run_error"] = run["error"] if run else None
            ledger = ledgers.get(result["run_id"])
            result["usage"] = self._row(ledger) if ledger else None
            result["token_usage"] = ledger["total_tokens"] if ledger else None
            result["estimated_usage_credits_micros"] = estimates.get(result["run_id"])
        return messages

    def list_messages(
        self, device_id: str, room_id: str, before: Optional[str] = None, limit: int = 50,
    ) -> list[dict[str, Any]]:
        limit = self._count(limit, "message limit", 1, 500)
        with self._lock:
            self._room(device_id, room_id)
            extra, values = "", [room_id]
            if before is not None:
                anchor = self.db.execute(
                    "SELECT rowid FROM messages WHERE id = ? AND room_id = ?", (before, room_id)
                ).fetchone()
                if anchor is None:
                    raise PermissionError("Message cursor is not accessible")
                extra = " AND rowid < ?"
                values.append(anchor["rowid"])
            values.append(limit)
            rows = self.db.execute(
                "SELECT * FROM messages WHERE room_id = ?" + extra + " ORDER BY rowid DESC LIMIT ?", values
            ).fetchall()
            return self._message_rows(reversed(rows))

    def create_run(
        self, device_id: str, room_id: str, text: str, attachment_ids: list[str], request_id: str,
        tool_read_only: Optional[bool] = None,
    ) -> dict[str, Any]:
        request_id = self._text(request_id, "request ID", 200)
        if not isinstance(text, str) or len(text) > 1000000 or "\x00" in text:
            raise ValueError("Invalid message text")
        if not isinstance(attachment_ids, list) or len(attachment_ids) > 12:
            raise ValueError("Invalid attachments")
        if any(not isinstance(value, str) for value in attachment_ids) or len(set(attachment_ids)) != len(attachment_ids):
            raise ValueError("Invalid attachments")
        if not text.strip() and not attachment_ids:
            raise ValueError("Message cannot be empty")
        with self._lock, self.db:
            room = self._room(device_id, room_id, own=True)
            self._assert_room_writable(room)
            if tool_read_only is None:
                tool_read_only = bool(room["tool_read_only"])
            tool_read_only_value = self._boolean(tool_read_only, "read-only setting")
            self.enforce_companion_run(room_id, tool_read_only_value, attachment_ids)
            fingerprint = hashlib.sha256(json.dumps(
                [room_id, text, attachment_ids, bool(tool_read_only_value)],
                ensure_ascii=False, separators=(",", ":")
            ).encode()).hexdigest()
            device = self._actor(device_id)
            for attachment_id in attachment_ids:
                self._attachment(device_id, attachment_id, own=True)
            existing = self.db.execute(
                "SELECT * FROM runs WHERE owner_id = ? AND request_id = ?", (device_id, request_id)
            ).fetchone()
            if existing:
                if existing["request_fingerprint"] != fingerprint:
                    raise ValueError("Request ID was already used for different content")
                return {"run": self._row(existing), "user_message": self._message(existing["user_message_id"]),
                        "room": self._row(room), "deduplicated": True}
            active = self.db.execute(
                "SELECT COUNT(*) FROM runs WHERE owner_id = ? AND status IN ('queued','running')", (device_id,)
            ).fetchone()[0]
            if active >= device["max_concurrent"]:
                raise ValueError("Device concurrency limit reached")
            if self.db.execute(
                "SELECT 1 FROM runs WHERE room_id = ? AND status IN ('queued','running')", (room_id,)
            ).fetchone():
                raise ValueError("Room already has an active run")
            used = self.db.execute(
                "SELECT COALESCE(SUM(total_tokens),0) FROM usage_ledger WHERE owner_id = ?", (device_id,)
            ).fetchone()[0]
            if device["token_budget"] is not None and used >= device["token_budget"]:
                raise ValueError("Device token budget exhausted")
            self.quota.check(device)
            run_id, message_id, now = self._id(), self._id(), time.time()
            self.db.execute(
                "INSERT INTO runs(id,owner_id,room_id,request_id,request_fingerprint,thread_id,status,user_message_id,created_at,updated_at,model,effort,speed,tool_read_only) "
                "VALUES(?,?,?,?,?,?,'queued',?,?,?,?,?,?,?)",
                (run_id, device_id, room_id, request_id, fingerprint, room["thread_id"], message_id, now, now,
                 room["model"], room["effort"], room["speed"], tool_read_only_value),
            )
            self.db.execute(
                "INSERT INTO messages(id,room_id,run_id,role,content,attachment_ids,created_at) VALUES(?,?,?,'user',?,?,?)",
                (message_id, room_id, run_id, text, json.dumps(attachment_ids), now),
            )
            automatic_title = (
                room["title"] == DEFAULT_ROOM_TITLE
                and not self.db.execute(
                    "SELECT 1 FROM messages WHERE room_id=? AND id<>? LIMIT 1", (room_id, message_id)
                ).fetchone()
            )
            title = title_from_first_prompt(text) if automatic_title else room["title"]
            self.db.execute(
                "UPDATE rooms SET title=?,updated_at=? WHERE id=?", (title, now, room_id)
            )
            return {"run": self._row(self._run(run_id)), "user_message": self._message(message_id),
                    "room": self._row(self._room(device_id, room_id)), "deduplicated": False}

    def get_run(self, device_id: str, run_id: str) -> dict[str, Any]:
        with self._lock:
            actor = self._actor(device_id)
            row = self.db.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
            if row is None or (row["owner_id"] != device_id and not actor["is_admin"]):
                raise PermissionError("Run is not accessible")
            return self._row(row)

    def prepare_steering(
        self, device_id, room_id, run_id, text, attachment_ids, request_id,
        tool_read_only,
    ):
        request_id = self._text(request_id, "request ID", 200)
        run_id = self._text(run_id, "run ID", 200)
        tool_read_only_value = self._boolean(tool_read_only, "read-only setting")
        if not isinstance(text, str) or len(text) > 1000000 or "\x00" in text:
            raise ValueError("Invalid message text")
        if (not isinstance(attachment_ids, list) or len(attachment_ids) > 10
                or any(not isinstance(value, str) for value in attachment_ids)
                or len(set(attachment_ids)) != len(attachment_ids)):
            raise ValueError("Invalid attachments")
        if not text.strip() and not attachment_ids:
            raise ValueError("Message cannot be empty")
        fingerprint = hashlib.sha256(json.dumps(
            [room_id, run_id, text, attachment_ids, bool(tool_read_only_value)],
            ensure_ascii=False, separators=(",", ":")
        ).encode()).hexdigest()
        with self._lock, self.db:
            self._assert_room_writable(self._room(device_id, room_id, own=True))
            self.enforce_companion_run(room_id, tool_read_only_value, attachment_ids)
            run = self._run(run_id)
            if run["owner_id"] != device_id or run["room_id"] != room_id:
                raise PermissionError("Run is not owned by this computer and room")
            for attachment_id in attachment_ids:
                self._attachment(device_id, attachment_id, own=True)
            row = self.db.execute("SELECT * FROM steering_receipts WHERE owner_id=? AND request_id=?", (device_id, request_id)).fetchone()
            if row:
                if row["fingerprint"] != fingerprint:
                    raise ValueError("Request ID was already used for different follow-up content")
                if row["status"] == "queued":
                    self._check_steering_budgets(device_id)
                return self._row(row), False
            if self.db.execute(
                "SELECT 1 FROM runs WHERE owner_id=? AND request_id=?",
                (device_id, request_id),
            ).fetchone():
                raise ValueError("Request ID was already used for another prompt")
            if run["status"] not in LIVE_STATUSES:
                raise ValueError("The turn has already ended. Your follow-up was not sent.")
            self._check_steering_budgets(device_id)
            identifier, now = self._id(), time.time()
            self.db.execute(
                "INSERT INTO steering_receipts(id,owner_id,room_id,run_id,request_id,fingerprint,status,content,attachment_ids,tool_read_only,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,'queued',?,?,?,?,?)",
                (identifier, device_id, room_id, run_id, request_id, fingerprint,
                 text, json.dumps(attachment_ids), tool_read_only_value, now, now),
            )
            return self._row(self.db.execute("SELECT * FROM steering_receipts WHERE id=?", (identifier,)).fetchone()), True

    def _check_steering_budgets(self, device_id):
        """CODEX: New or queued follow-ups consume the same client's existing budgets."""
        device = self._actor(device_id)
        if device["token_budget"] is not None:
            used = self.db.execute("SELECT COALESCE(SUM(total_tokens),0) FROM usage_ledger WHERE owner_id=?", (device_id,)).fetchone()[0]
            if used >= device["token_budget"]:
                raise ValueError("Device token budget exhausted")
        self.quota.check(device)

    def configure_run_usage(self, run_id, model, effort, speed):
        """CODEX: Resolve a new run's actual provider configuration before execution, never reprice history."""
        model = self._text(model, "model", 200)
        effort = self._text(effort, "effort", 80, empty=True)
        speed = self._speed(speed)
        with self._lock, self.db:
            run = self._run(run_id)
            if (run["model"], run["effort"], run["speed"]) == (model, effort, speed):
                return self._row(run)
            if run["status"] != "queued" or self.db.execute("SELECT 1 FROM usage_ledger WHERE run_id=?", (run_id,)).fetchone():
                raise ValueError("Run usage configuration is already fixed")
            self.db.execute("UPDATE runs SET model=?,effort=?,speed=? WHERE id=?", (model, effort, speed, run_id))
            return self._row(self._run(run_id))

    def steering_result(self, receipt_id):
        """CODEX: Trusted runtime receipt lookup; public callers authorize via prepare_steering."""
        with self._lock:
            row = self.db.execute("SELECT * FROM steering_receipts WHERE id=?", (receipt_id,)).fetchone()
            if row is None:
                raise ValueError("Follow-up receipt no longer exists")
            receipt = {key: row[key] for key in (
                "id", "run_id", "replacement_run_id", "request_id", "status", "error",
                "tool_read_only", "created_at", "updated_at",
            )}
            receipt["tool_read_only"] = bool(receipt["tool_read_only"])
            message = self._message(row["user_message_id"]) if row["user_message_id"] else None
            result = {"steering": receipt, "user_message": message}
            if row["replacement_run_id"]:
                result["run"] = self._row(self._run(row["replacement_run_id"]))
            return result

    def unresolved_steering(self, device_id, room_id):
        with self._lock:
            self._room(device_id, room_id)
            return [self._row(row) for row in self.db.execute(
                "SELECT id,run_id,replacement_run_id,request_id,status,error,tool_read_only,created_at,updated_at "
                "FROM steering_receipts "
                "WHERE room_id=? AND status IN ('queued','sending','uncertain') ORDER BY created_at DESC LIMIT 20", (room_id,))]

    def finish_steering(self, receipt_id, status, error=None):
        if status not in {"sending", "accepted", "rejected", "uncertain"}:
            raise ValueError("Invalid follow-up status")
        with self._lock, self.db:
            row = self.db.execute("SELECT * FROM steering_receipts WHERE id=?", (receipt_id,)).fetchone()
            if row is None:
                raise ValueError("Follow-up receipt no longer exists")
            if row["status"] in {"accepted", "rejected", "uncertain"}:
                return self.steering_result(receipt_id)
            if status == "sending" and row["status"] == "queued":
                # CODEX: A queued follow-up is new work until its final pre-dispatch budget check succeeds.
                self._check_steering_budgets(row["owner_id"])
            message_id, now = row["user_message_id"], time.time()
            if status == "accepted" and not message_id:
                message_id = self._id()
                self.db.execute("INSERT INTO messages(id,room_id,run_id,role,content,attachment_ids,created_at) VALUES(?,?,?,'user',?,?,?)",
                                (message_id, row["room_id"], row["run_id"], row["content"], row["attachment_ids"], now))
                self.db.execute("UPDATE rooms SET updated_at=? WHERE id=?", (now, row["room_id"]))
            self.db.execute("UPDATE steering_receipts SET status=?,error=?,user_message_id=?,updated_at=? WHERE id=?",
                            (status, error, message_id, now, receipt_id))
            return self.steering_result(receipt_id)

    def finish_steering_handoff(self, receipt_id, replacement_run_id):
        """CODEX: Link an accepted access-mode handoff to its separately audited replacement run."""
        with self._lock, self.db:
            row = self.db.execute(
                "SELECT * FROM steering_receipts WHERE id=?", (receipt_id,)
            ).fetchone()
            if row is None:
                raise ValueError("Follow-up receipt no longer exists")
            if row["status"] == "accepted":
                if row["replacement_run_id"] != replacement_run_id:
                    raise ValueError("Follow-up is already linked to another run")
                return self.steering_result(receipt_id)
            if row["status"] != "sending":
                raise ValueError("Follow-up is not awaiting an access-mode handoff")
            replacement = self._run(replacement_run_id)
            if (replacement["owner_id"] != row["owner_id"]
                    or replacement["room_id"] != row["room_id"]
                    or replacement["request_id"] != row["request_id"]
                    or bool(replacement["tool_read_only"]) != bool(row["tool_read_only"])):
                raise ValueError("Replacement run does not match the follow-up")
            now = time.time()
            self.db.execute(
                "UPDATE steering_receipts SET status='accepted',error=NULL,user_message_id=?,"
                "replacement_run_id=?,updated_at=? WHERE id=?",
                (replacement["user_message_id"], replacement_run_id, now, receipt_id),
            )
            return self.steering_result(receipt_id)

    def get_run_internal(self, run_id: str) -> dict[str, Any]:
        """CODEX: Trusted worker lookup, including after its device is revoked."""
        with self._lock:
            return self._row(self._run(run_id))

    def list_runs(self, device_id: str, room_id: str) -> list[dict[str, Any]]:
        with self._lock:
            self._room(device_id, room_id)
            return [self._row(row) for row in self.db.execute(
                "SELECT * FROM runs WHERE room_id = ? ORDER BY created_at DESC,id LIMIT 100", (room_id,)
            )]

    def list_active_runs(self, device_id: Optional[str] = None) -> list[dict[str, Any]]:
        """CODEX: Internal worker enumeration; the optional device ID is a filter."""
        with self._lock:
            clause, params = ("", ()) if device_id is None else (" AND owner_id = ?", (device_id,))
            return [self._row(row) for row in self.db.execute(
                "SELECT * FROM runs WHERE status IN ('queued','running')" + clause + " ORDER BY created_at,id", params
            )]

    def update_run(self, run_id: str, **fields: Any) -> dict[str, Any]:
        """CODEX: Internal runtime mutation; authorization happens before scheduling."""
        if not fields or set(fields) - {"thread_id", "turn_id", "status", "error"}:
            raise ValueError("Invalid run fields")
        for key, value in fields.items():
            if key == "status":
                if not isinstance(value, str) or value not in LIVE_STATUSES | TERMINAL_STATUSES:
                    raise ValueError("Invalid run status")
            elif value is not None and (not isinstance(value, str) or len(value) > 100000):
                raise ValueError(f"Invalid {key}")
        with self._lock, self.db:
            run = self._run(run_id)
            if run["status"] in TERMINAL_STATUSES and fields.get("status", run["status"]) != run["status"]:
                raise ValueError("Finished runs cannot change status")
            fields["updated_at"] = time.time()
            self.db.execute(
                "UPDATE runs SET " + ",".join(f"{key} = ?" for key in fields) + " WHERE id = ?",
                (*fields.values(), run_id),
            )
            return self._row(self._run(run_id))

    def _append_event(
        self, run: sqlite3.Row, kind: str, payload: Any, event_key: Optional[str],
        encoded: Optional[str] = None, db: Optional[sqlite3.Connection] = None,
    ) -> dict[str, Any]:
        db = self.db if db is None else db
        if not run["room_id"]:
            raise ValueError("Run room was deleted")
        if event_key is not None:
            existing = db.execute(
                "SELECT * FROM events WHERE run_id = ? AND event_key = ?", (run["id"], event_key)
            ).fetchone()
            if existing:
                return self._row(existing)
        if encoded is None:
            encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        cursor = db.execute(
            "INSERT INTO events(run_id,room_id,kind,payload,event_key,created_at) VALUES(?,?,?,?,?,?)",
            (run["id"], run["room_id"], kind, encoded, event_key, time.time()),
        )
        return self._row(db.execute("SELECT * FROM events WHERE id = ?", (cursor.lastrowid,)).fetchone())

    def append_event(self, run_id: str, kind: str, payload: Any, event_key: Optional[str] = None) -> dict[str, Any]:
        kind = self._text(kind, "event kind", 200)
        if event_key is not None:
            event_key = self._text(event_key, "event key", 512)
        try:
            encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        except (TypeError, ValueError) as exc:
            raise ValueError("Event payload must be JSON serializable") from exc
        with self._lock, self.db:
            return self._append_event(self._run(run_id), kind, payload, event_key, encoded)

    def append_events(
        self,
        entries: list[tuple[str, str, Any, Optional[str]]],
    ) -> list[dict[str, Any]]:
        """CODEX: Commit a burst of ordered run events in one WAL transaction."""
        prepared = []
        for run_id, kind, payload, event_key in entries:
            kind = self._text(kind, "event kind", 200)
            if event_key is not None:
                event_key = self._text(event_key, "event key", 512)
            try:
                encoded = json.dumps(
                    payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")
                )
            except (TypeError, ValueError) as exc:
                raise ValueError("Event payload must be JSON serializable") from exc
            prepared.append((run_id, kind, payload, event_key, encoded))
        if not prepared:
            return []
        with self._lock, self.db:
            runs = {}
            result = []
            for run_id, kind, payload, event_key, encoded in prepared:
                run = runs.get(run_id)
                if run is None:
                    run = self._run(run_id)
                    runs[run_id] = run
                result.append(self._append_event(run, kind, payload, event_key, encoded))
            return result

    def persist_runtime_batch(
        self,
        entries: list[tuple[str, str, Any, Optional[str]]],
        usages: list[tuple[str, int, int, int, int]],
    ) -> list[Optional[dict[str, Any]]]:
        """CODEX: Persist live telemetry on the isolated WAL writer thread in one ordered transaction."""
        prepared = []
        for run_id, kind, payload, event_key in entries:
            kind = self._text(kind, "event kind", 200)
            if event_key is not None:
                event_key = self._text(event_key, "event key", 512)
            try:
                encoded = json.dumps(
                    payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")
                )
            except (TypeError, ValueError) as exc:
                raise ValueError("Event payload must be JSON serializable") from exc
            prepared.append((run_id, kind, payload, event_key, encoded))
        validated_usages = [self._validate_usage(*usage) for usage in usages]
        if not prepared and not validated_usages:
            return []
        with self._runtime_lock, self.runtime_db:
            self.runtime_db.execute("BEGIN IMMEDIATE")
            runs = {}
            result = []
            for run_id, kind, payload, event_key, encoded in prepared:
                run = runs.get(run_id)
                if run is None:
                    run = self.runtime_db.execute(
                        "SELECT * FROM runs WHERE id = ?", (run_id,)
                    ).fetchone()
                    runs[run_id] = run
                if run is None or not run["room_id"]:
                    result.append(None)
                    continue
                result.append(
                    self._append_event(
                        run, kind, payload, event_key, encoded, db=self.runtime_db
                    )
                )
            for usage in validated_usages:
                self._record_usage(self.runtime_db, self.runtime_quota, *usage, missing_ok=True)
            return result

    def events(self, device_id: str, room_id: str, after: int = 0, limit: int = 200) -> list[dict[str, Any]]:
        after = self._count(after, "event cursor")
        limit = self._count(limit, "event limit", 1, 1000)
        with self._lock:
            self._room(device_id, room_id)
            return [self._row(row) for row in self.db.execute(
                "SELECT * FROM events WHERE room_id = ? AND id > ? ORDER BY id LIMIT ?", (room_id, after, limit)
            )]

    def _finish_run(
        self, run_id: str, status: str, text: str, error: Optional[str],
        attachment_ids: Optional[list[str]] = None,
    ) -> dict[str, Any]:
        run = self._run(run_id)
        if run["assistant_message_id"]:
            return self._message(run["assistant_message_id"])
        if not run["room_id"]:
            raise ValueError("Run room was deleted")
        if run["status"] in TERMINAL_STATUSES and run["status"] != status:
            raise ValueError("Finished runs cannot change status")
        attachment_ids = list(attachment_ids or [])
        if (len(attachment_ids) > 12
                or any(not isinstance(value, str) for value in attachment_ids)
                or len(set(attachment_ids)) != len(attachment_ids)):
            raise ValueError("Invalid generated attachments")
        for attachment_id in attachment_ids:
            self._attachment(run["owner_id"], attachment_id, own=True)
        message_id, now = self._id(), time.time()
        self.db.execute(
            "INSERT INTO messages(id,room_id,run_id,role,content,attachment_ids,duration_seconds,created_at) "
            "VALUES(?,?,?,'assistant',?,?,?,?)",
            (message_id, run["room_id"], run_id, text, json.dumps(attachment_ids),
             max(0.0, now - run["created_at"]), now),
        )
        if run["thread_id"]:
            self.db.executemany(
                "INSERT OR IGNORE INTO artifact_deliveries("
                "owner_id,thread_id,attachment_id,status,created_at,updated_at) "
                "VALUES(?,?,?,'pending',?,?)",
                ((run["owner_id"], run["thread_id"], attachment_id, now, now)
                 for attachment_id in attachment_ids),
            )
        self.db.execute(
            "UPDATE runs SET status=?,error=?,assistant_message_id=?,updated_at=?,finished_at=? WHERE id=?",
            (status, error, message_id, now, now, run_id),
        )
        self.db.execute("UPDATE rooms SET updated_at = ? WHERE id = ?", (now, run["room_id"]))
        return self._message(message_id)

    def finish_run(
        self, run_id: str, status: str, text: str, error: Optional[str] = None,
        attachment_ids: Optional[list[str]] = None,
    ) -> dict[str, Any]:
        if not isinstance(status, str) or status not in TERMINAL_STATUSES:
            raise ValueError("Invalid final run status")
        if not isinstance(text, str) or (error is not None and not isinstance(error, str)):
            raise ValueError("Invalid run result")
        with self._lock, self.db:
            return self._finish_run(run_id, status, text, error, attachment_ids)

    def recover_runs(self) -> list[dict[str, Any]]:
        """CODEX: Called once before accepting requests after a server restart."""
        with self._lock, self.db:
            self.db.execute("UPDATE steering_receipts SET status='uncertain',error=?,updated_at=? WHERE status='sending'",
                            ("Server restarted before follow-up delivery was confirmed. It will not be resent automatically.", time.time()))
            self.db.execute("UPDATE steering_receipts SET status='rejected',error=?,updated_at=? WHERE status='queued'",
                            ("Server restarted before the follow-up was sent.", time.time()))
            live = self.db.execute("SELECT * FROM runs WHERE status IN ('queued','running') ORDER BY created_at,id").fetchall()
            result = []
            for run in live:
                error = "Server restarted before this run finished."
                recovered_text = self._recover_response_text(run["id"])
                message = self._finish_run(run["id"], "interrupted", recovered_text or error, error)
                recovered = self._run(run["id"])
                self._append_event(recovered, "completed", {
                    "message": message, "status": "interrupted", "error": error,
                }, "completed")
                result.append(self._row(recovered))
            return result

    def _recover_response_text(self, run_id: str) -> str:
        """CODEX: Bound legacy delta replay so one interrupted run cannot stall server startup."""
        fragments = []
        remaining = MAX_RECOVERED_RESPONSE_CHARACTERS
        truncated = False
        events = self.db.execute(
            "SELECT payload FROM events WHERE run_id = ? AND kind = 'delta' ORDER BY id LIMIT ?",
            (run_id, MAX_RECOVERED_DELTA_EVENTS + 1),
        )
        for index, event in enumerate(events):
            if index >= MAX_RECOVERED_DELTA_EVENTS:
                truncated = True
                break
            try:
                payload = json.loads(event["payload"])
            except (json.JSONDecodeError, TypeError):
                continue
            text = payload.get("text") if isinstance(payload, dict) else None
            if not isinstance(text, str):
                continue
            if remaining <= 0:
                truncated = True
                break
            accepted = text[:remaining]
            fragments.append(accepted)
            remaining -= len(accepted)
            if len(text) > len(accepted):
                truncated = True
                break
        response = "".join(fragments)
        if truncated:
            marker = RECOVERED_RESPONSE_OMITTED[:MAX_RECOVERED_RESPONSE_CHARACTERS]
            keep = max(0, MAX_RECOVERED_RESPONSE_CHARACTERS - len(marker))
            response = response[:keep] + marker
        return response

    def _validate_usage(
        self, run_id: str, input_tokens: int, output_tokens: int,
        cached_tokens: int = 0, total_tokens: Optional[int] = None,
    ) -> tuple[str, int, int, int, int]:
        input_tokens = self._count(input_tokens, "input tokens")
        output_tokens = self._count(output_tokens, "output tokens")
        cached_tokens = self._count(cached_tokens, "cached tokens")
        if cached_tokens > input_tokens:
            raise ValueError("Cached tokens cannot exceed input tokens")
        if total_tokens is None:
            total_tokens = input_tokens + output_tokens
        total_tokens = self._count(total_tokens, "total tokens")
        if total_tokens < input_tokens + output_tokens:
            raise ValueError("Total tokens cannot be below input plus output")
        return run_id, input_tokens, output_tokens, cached_tokens, total_tokens

    def _record_usage(
        self, db: sqlite3.Connection, quota: QuotaAccounting, run_id: str,
        input_tokens: int, output_tokens: int, cached_tokens: int, total_tokens: int,
        *, missing_ok: bool = False,
    ) -> Optional[dict[str, Any]]:
        run = db.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        if run is None:
            if missing_ok:
                return None
            raise ValueError("Unknown run")
        previous = db.execute("SELECT * FROM usage_ledger WHERE run_id = ?", (run_id,)).fetchone()
        db.execute(
            "INSERT INTO usage_ledger(run_id,owner_id,input_tokens,output_tokens,cached_tokens,total_tokens,updated_at) "
            "VALUES(?,?,?,?,?,?,?) ON CONFLICT(run_id) DO UPDATE SET "
            "input_tokens=MAX(input_tokens,excluded.input_tokens), "
            "output_tokens=MAX(output_tokens,excluded.output_tokens), "
            "cached_tokens=MAX(cached_tokens,excluded.cached_tokens), "
            "total_tokens=MAX(total_tokens,excluded.total_tokens,MAX(input_tokens,excluded.input_tokens)+MAX(output_tokens,excluded.output_tokens)), "
            "updated_at=excluded.updated_at",
            (run_id, run["owner_id"], input_tokens, output_tokens, cached_tokens, total_tokens, time.time()),
        )
        current = db.execute("SELECT * FROM usage_ledger WHERE run_id = ?", (run_id,)).fetchone()
        deltas = [current[key] - (previous[key] if previous else 0) for key in (
            "input_tokens", "output_tokens", "cached_tokens", "total_tokens"
        )]
        day = datetime.now(timezone.utc).date().isoformat()
        db.execute(
            "INSERT INTO usage_daily(owner_id,day,input_tokens,output_tokens,cached_tokens,total_tokens) "
            "VALUES(?,?,?,?,?,?) ON CONFLICT(owner_id,day) DO UPDATE SET "
            "input_tokens=input_tokens+excluded.input_tokens,output_tokens=output_tokens+excluded.output_tokens, "
            "cached_tokens=cached_tokens+excluded.cached_tokens,total_tokens=total_tokens+excluded.total_tokens",
            (run["owner_id"], day, *deltas),
        )
        quota.record(run, current)
        return self._row(current)

    def record_usage(
        self, run_id: str, input_tokens: int, output_tokens: int,
        cached_tokens: int = 0, total_tokens: Optional[int] = None,
    ) -> dict[str, Any]:
        usage = self._validate_usage(
            run_id, input_tokens, output_tokens, cached_tokens, total_tokens
        )
        with self._lock, self.db:
            return self._record_usage(self.db, self.quota, *usage)

    def observe_provider_usage(self, envelope):
        """CODEX: Internal observation of sanitized host-account readings, never a client-writable endpoint."""
        with self._lock, self.db:
            self.quota.observe(envelope)

    def quota_report(self, admin_id):
        with self._lock:
            self._actor(admin_id, admin=True)
            return self.quota.report()

    def acknowledge_unknown_usage(self, admin_id, device_id):
        """CODEX: Record an administrator's bounded acceptance of completed runs missing final usage."""
        with self._lock, self.db:
            self._actor(admin_id, admin=True)
            device = self._device(device_id)
            window, calibration = self.quota.summary()
            acknowledged = self.quota.acknowledge_unknown_usage(device_id, admin_id, window)
            return {"acknowledged_runs": acknowledged,
                    "quota": self.quota.device_report(device, window, calibration)}

    def device_quota(self, device_id):
        """CODEX: Self-only quota view; HTTP callers supply the authenticated device identity."""
        with self._lock:
            actor = self._actor(device_id)
            window, calibration = self.quota.summary()
            visible_calibration = {key: calibration[key] for key in ("status", "warning", "carried_forward", "learning_reason",
                                                                      "refresh_status", "using_last_known")}
            if actor["is_admin"]:
                # CODEX: Only the host administrator may compare local attribution with the shared official account meter.
                visible_calibration.update({key: calibration[key] for key in (
                    "provider_used_percent", "provisional_higher_capacity", "effective_capacity_source"
                )})
            return {"window": {key: window[key] for key in ("starts_at", "ends_at")} if window else None,
                    "calibration": visible_calibration,
                    "device": self.quota.device_report(actor, window, calibration), "estimated": True}

    def usage(self, admin_id: str) -> list[dict[str, Any]]:
        with self._lock:
            self._actor(admin_id, admin=True)
            today = datetime.now(timezone.utc).date().isoformat()
            return [self._row(row) for row in self.db.execute(
                "SELECT d.id AS device_id,d.name,d.status,d.max_concurrent,d.token_budget, "
                "COALESCE((SELECT SUM(input_tokens) FROM usage_ledger WHERE owner_id=d.id),0) AS input_tokens, "
                "COALESCE((SELECT SUM(output_tokens) FROM usage_ledger WHERE owner_id=d.id),0) AS output_tokens, "
                "COALESCE((SELECT SUM(cached_tokens) FROM usage_ledger WHERE owner_id=d.id),0) AS cached_tokens, "
                "COALESCE((SELECT SUM(total_tokens) FROM usage_ledger WHERE owner_id=d.id),0) AS total_tokens, "
                "COALESCE((SELECT SUM(total_tokens) FROM usage_daily WHERE owner_id=d.id AND day=?),0) AS today_tokens, "
                "COALESCE((SELECT SUM(total_tokens) FROM usage_daily WHERE owner_id=d.id AND day>=?),0) AS month_tokens, "
                "(SELECT COUNT(*) FROM runs WHERE owner_id=d.id) AS runs, "
                "(SELECT COUNT(*) FROM runs WHERE owner_id=d.id AND status IN ('queued','running')) AS active_runs, "
                "(SELECT COUNT(*) FROM runs r WHERE r.owner_id=d.id AND r.status NOT IN ('queued','running') "
                "AND NOT EXISTS(SELECT 1 FROM usage_ledger u WHERE u.run_id=r.id)) AS unknown_usage_runs, "
                "COALESCE((SELECT SUM(MAX(0,finished_at-created_at)) FROM runs WHERE owner_id=d.id AND finished_at IS NOT NULL),0) AS duration_seconds "
                "FROM devices d ORDER BY d.created_at,d.id", (today, today[:8] + "01")
            )]

    def add_attachment(self, device_id: str, name: str, mime: str, size: int, path: str) -> dict[str, Any]:
        name = self._text(name, "attachment name", 512)
        mime = self._text(mime, "attachment MIME type", 200)
        size = self._count(size, "attachment size")
        path = self._text(str(path), "attachment path", 4096)
        with self._lock, self.db:
            self._actor(device_id)
            attachment_id = self._id()
            inserted = self.db.execute(
                "INSERT INTO attachments(id,owner_id,name,mime,size,path,created_at) "
                "SELECT ?,?,?,?,?,?,? FROM ("
                "SELECT COALESCE(SUM(size),0) AS used_bytes,COUNT(*) AS stored_files "
                "FROM attachments WHERE owner_id=?) "
                "WHERE used_bytes + ? <= ? AND stored_files < ?",
                (
                    attachment_id, device_id, name, mime, size, path, time.time(),
                    device_id, size, MAX_ATTACHMENT_STORAGE_BYTES, MAX_ATTACHMENT_COUNT,
                ),
            ).rowcount
            if inserted != 1:
                raise ValueError(
                    "This device's attachment storage is full (byte or file-count limit). "
                    "Remove unused uploads or old rooms before uploading more."
                )
            return self._row(self._attachment(device_id, attachment_id))

    def get_attachment(self, device_id: str, attachment_id: str) -> dict[str, Any]:
        with self._lock:
            return self._row(self._attachment(device_id, attachment_id))

    def _artifact_delivery(self, owner_id: str, thread_id: str, attachment_id: str) -> sqlite3.Row:
        row = self.db.execute(
            "SELECT d.*,a.name,a.mime,a.size,a.path FROM artifact_deliveries AS d "
            "JOIN attachments AS a ON a.id=d.attachment_id AND a.owner_id=d.owner_id "
            "WHERE d.owner_id=? AND d.thread_id=? AND d.attachment_id=?",
            (owner_id, thread_id, attachment_id),
        ).fetchone()
        if row is None:
            raise ValueError("Unknown generated-image delivery")
        return row

    def pending_artifact_deliveries(self, owner_id: str, limit: int = 100) -> list[dict[str, Any]]:
        """CODEX: Return only committed delivery intents for an authenticated connector retry."""
        limit = self._count(limit, "artifact delivery limit", 1, 1000)
        with self._lock:
            self._actor(owner_id)
            return [self._row(row) for row in self.db.execute(
                "SELECT d.*,a.name,a.mime,a.size,a.path FROM artifact_deliveries AS d "
                "JOIN attachments AS a ON a.id=d.attachment_id AND a.owner_id=d.owner_id "
                "WHERE d.owner_id=? AND d.status='pending' "
                "ORDER BY d.attempts,d.updated_at,d.attachment_id LIMIT ?",
                (owner_id, limit),
            )]

    def defer_artifact_delivery(
        self, owner_id: str, thread_id: str, attachment_id: str, expected_attempts: int,
    ) -> bool:
        """CODEX: Fairly rotate a pending intent only if delivery did not already count an attempt."""
        expected_attempts = self._count(
            expected_attempts, "artifact delivery attempts", maximum=MAX_INTEGER - 1
        )
        with self._lock, self.db:
            self._actor(owner_id)
            return self.db.execute(
                "UPDATE artifact_deliveries SET attempts=attempts+1,updated_at=? "
                "WHERE owner_id=? AND thread_id=? AND attachment_id=? "
                "AND status='pending' AND attempts=?",
                (time.time(), owner_id, thread_id, attachment_id, expected_attempts),
            ).rowcount == 1

    def prepare_artifact_delivery(
        self, owner_id: str, thread_id: str, attachment_id: str, sha256: str,
    ) -> dict[str, Any]:
        """CODEX: Persist the exact source digest before sending an idempotent delivery request."""
        if (not isinstance(sha256, str) or len(sha256) != 64
                or any(character not in "0123456789abcdef" for character in sha256)):
            raise ValueError("Invalid generated-image digest")
        with self._lock, self.db:
            self._actor(owner_id)
            row = self._artifact_delivery(owner_id, thread_id, attachment_id)
            if row["sha256"] and row["sha256"] != sha256:
                raise ValueError("Generated-image content changed after it was committed")
            if row["status"] == "pending":
                self.db.execute(
                    "UPDATE artifact_deliveries SET sha256=?,attempts=attempts+1,updated_at=? "
                    "WHERE owner_id=? AND thread_id=? AND attachment_id=? AND status='pending'",
                    (sha256, time.time(), owner_id, thread_id, attachment_id),
                )
            return self._row(self._artifact_delivery(owner_id, thread_id, attachment_id))

    def complete_artifact_delivery(
        self, owner_id: str, thread_id: str, attachment_id: str, sha256: str,
    ) -> dict[str, Any]:
        """CODEX: Accept only the matching hash receipt and make repeated acknowledgements harmless."""
        if (not isinstance(sha256, str) or len(sha256) != 64
                or any(character not in "0123456789abcdef" for character in sha256)):
            raise ValueError("Invalid generated-image digest")
        with self._lock, self.db:
            self._actor(owner_id)
            row = self._artifact_delivery(owner_id, thread_id, attachment_id)
            if not row["sha256"] or row["sha256"] != sha256:
                raise ValueError("Generated-image receipt digest does not match")
            if row["status"] == "delivered":
                return self._row(row)
            self.db.execute(
                "UPDATE artifact_deliveries SET status='delivered',updated_at=? "
                "WHERE owner_id=? AND thread_id=? AND attachment_id=? AND status='pending'",
                (time.time(), owner_id, thread_id, attachment_id),
            )
            return self._row(self._artifact_delivery(owner_id, thread_id, attachment_id))

    def _attachment_referenced(self, attachment_id: str) -> bool:
        """CODEX: Treat both committed messages and delivery receipts as durable references."""
        if self.db.execute(
            "SELECT 1 FROM messages WHERE NOT json_valid(attachment_ids) LIMIT 1"
        ).fetchone() or self.db.execute(
            "SELECT 1 FROM steering_receipts "
            "WHERE status!='rejected' AND NOT json_valid(attachment_ids) LIMIT 1"
        ).fetchone():
            return True
        if self.db.execute(
            "SELECT 1 FROM messages,json_each(messages.attachment_ids) "
            "WHERE json_each.value=? LIMIT 1",
            (attachment_id,),
        ).fetchone():
            return True
        return self.db.execute(
            "SELECT 1 FROM steering_receipts,json_each(steering_receipts.attachment_ids) "
            "WHERE steering_receipts.status!='rejected' AND json_each.value=? LIMIT 1",
            (attachment_id,),
        ).fetchone() is not None

    def discard_attachment(self, device_id: str, attachment_id: str) -> dict[str, Any]:
        """CODEX: Release an owned upload only while no durable conversation references it."""
        with self._lock, self.db:
            attachment = self._attachment(device_id, attachment_id, own=True)
            if self._attachment_referenced(attachment_id):
                return {"deleted": False, "path": None}
            deleted = self.db.execute(
                "DELETE FROM attachments WHERE id=? AND owner_id=?",
                (attachment_id, device_id),
            ).rowcount
            path = attachment["path"] if deleted else None
            if path and self.db.execute(
                "SELECT 1 FROM attachments WHERE path=? LIMIT 1", (path,)
            ).fetchone():
                path = None
            return {"deleted": deleted == 1, "path": path}

    def prune_unreferenced_attachments(self, created_before: float) -> list[str]:
        """CODEX: Expire abandoned uploads conservatively without touching referenced images."""
        cutoff = float(created_before)
        with self._lock, self.db:
            rows = list(self.db.execute(
                "SELECT id,path FROM attachments WHERE created_at<=? ORDER BY id",
                (cutoff,),
            ))
            rows = [row for row in rows if not self._attachment_referenced(row["id"])]
            if rows:
                self.db.executemany("DELETE FROM attachments WHERE id=?", ((row["id"],) for row in rows))
            paths = {row["path"] for row in rows}
            return sorted(
                path for path in paths
                if not self.db.execute(
                    "SELECT 1 FROM attachments WHERE path=? LIMIT 1", (path,)
                ).fetchone()
            )

    def attachment_paths(self) -> set[str]:
        """CODEX: Snapshot known files so managed-root reconciliation can preserve every live row."""
        with self._lock:
            return {row["path"] for row in self.db.execute("SELECT path FROM attachments")}

    def _attachment_ids_for_rooms(self, room_ids: list[str]) -> set[str]:
        """CODEX: Capture attachment candidates before cascading room content is deleted."""
        placeholders = ",".join("?" for _ in room_ids)
        identifiers = set()
        for table in ("messages", "steering_receipts"):
            for row in self.db.execute(
                f"SELECT attachment_ids FROM {table} WHERE room_id IN ({placeholders})", room_ids
            ):
                identifiers.update(json.loads(row["attachment_ids"]))
        return identifiers

    def _delete_unreferenced_attachments(self, owner_id: str, identifiers: set[str]) -> list[str]:
        """CODEX: Remove only attachment rows no surviving message or steering receipt references."""
        paths = []
        for identifier in sorted(identifiers):
            if self._attachment_referenced(identifier):
                continue
            attachment = self.db.execute(
                "SELECT path FROM attachments WHERE id=? AND owner_id=?", (identifier, owner_id)
            ).fetchone()
            if attachment:
                paths.append(attachment["path"])
                self.db.execute(
                    "DELETE FROM attachments WHERE id=? AND owner_id=?", (identifier, owner_id)
                )
        return sorted({
            path for path in paths
            if not self.db.execute(
                "SELECT 1 FROM attachments WHERE path=? LIMIT 1", (path,)
            ).fetchone()
        })

    def _delete_room_with_cleanup(self, device_id: str, room_id: str) -> dict[str, Any]:
        room = self._room(device_id, room_id)
        owner_id = room["owner_id"]
        children = [row[0] for row in self.db.execute(
            "SELECT room_id FROM room_companions WHERE parent_room_id=?", (room_id,),
        )]
        targets = [room_id, *children]
        if self.db.execute(
            "SELECT 1 FROM runs WHERE room_id IN (" + ",".join("?" for _ in targets)
            + ") AND status IN ('queued','running')", targets,
        ).fetchone():
            raise ValueError("Stop active responses in this room and its panels before deleting")
        run_ids = [row["id"] for row in self.db.execute(
            "SELECT id FROM runs WHERE room_id IN (" + ",".join("?" for _ in targets) + ")",
            targets,
        )]
        threads = [
            {"owner_id": row["owner_id"], "thread_id": row["thread_id"]}
            for row in self.db.execute(
                "SELECT owner_id,thread_id FROM rooms WHERE id IN ("
                + ",".join("?" for _ in targets) + ") AND thread_id IS NOT NULL",
                targets,
            )
        ]
        attachment_ids = self._attachment_ids_for_rooms(targets)
        for child_id in children:
            self._redact_companion_runs(child_id)
            self.db.execute("DELETE FROM rooms WHERE id=?", (child_id,))
        if self.companion_for_room(room_id):
            self._redact_companion_runs(room_id)
        self.db.execute("DELETE FROM rooms WHERE id = ?", (room_id,))
        return {
            "deleted": True,
            "run_ids": run_ids,
            "threads": threads,
            "attachment_paths": self._delete_unreferenced_attachments(owner_id, attachment_ids),
        }

    def delete_room_with_cleanup(self, device_id: str, room_id: str) -> dict[str, Any]:
        """CODEX: Delete room data atomically and return committed files for safe unlinking."""
        with self._lock, self.db:
            return self._delete_room_with_cleanup(device_id, room_id)

    def delete_room(self, device_id: str, room_id: str) -> bool:
        return self.delete_room_with_cleanup(device_id, room_id)["deleted"]

    def close(self) -> None:
        with self._runtime_lock:
            self.runtime_db.close()
        with self._lock:
            self.db.close()
