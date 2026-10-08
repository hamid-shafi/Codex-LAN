# -*- coding: utf-8 -*-
"""UTF-8-safe local persistence for rooms and visible messages."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


class Store:
    def __init__(self, data_dir: Optional[Path] = None) -> None:
        if data_dir is None:
            override = os.environ.get("HAMSOHBAT_DATA_DIR")
            base = Path(override) if override else Path(
                os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")
            )
            data_dir = base / "hamsohbat-codex"
        self.data_dir = Path(data_dir)
        self.upload_dir = self.data_dir / "uploads"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.upload_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.db = sqlite3.connect(
            self.data_dir / "hamsohbat.sqlite3", check_same_thread=False
        )
        self.db.row_factory = sqlite3.Row
        self._create_schema()

    def _create_schema(self) -> None:
        with self._lock, self.db:
            self.db.executescript(
                """
                PRAGMA journal_mode=WAL;
                PRAGMA foreign_keys=ON;

                CREATE TABLE IF NOT EXISTS rooms (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    thread_id TEXT,
                    model TEXT,
                    effort TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    room_id INTEGER NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
                    role TEXT NOT NULL CHECK(role IN ('user', 'assistant', 'system')),
                    content TEXT NOT NULL DEFAULT '',
                    image_path TEXT,
                    reply_to_id INTEGER REFERENCES messages(id) ON DELETE SET NULL,
                    reply_quote TEXT,
                    duration_seconds REAL,
                    token_usage INTEGER,
                    created_at REAL NOT NULL
                );

                CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_identity_room
                    ON messages(id, room_id);

                CREATE TABLE IF NOT EXISTS branches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    room_id INTEGER NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
                    parent_message_id INTEGER NOT NULL,
                    selected_quote TEXT NOT NULL
                        CHECK(length(trim(selected_quote)) > 0),
                    thread_id TEXT
                        CHECK(thread_id IS NULL OR length(trim(thread_id)) > 0),
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    FOREIGN KEY(parent_message_id, room_id)
                        REFERENCES messages(id, room_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS branch_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    branch_id INTEGER NOT NULL
                        REFERENCES branches(id) ON DELETE CASCADE,
                    role TEXT NOT NULL
                        CHECK(role IN ('user', 'assistant', 'system')),
                    content TEXT NOT NULL DEFAULT '',
                    duration_seconds REAL,
                    token_usage INTEGER,
                    created_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_messages_room
                    ON messages(room_id, created_at, id);
                CREATE INDEX IF NOT EXISTS idx_branches_parent
                    ON branches(room_id, parent_message_id, updated_at, id);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_branches_thread_unique
                    ON branches(thread_id) WHERE thread_id IS NOT NULL;
                CREATE INDEX IF NOT EXISTS idx_branch_messages_branch
                    ON branch_messages(branch_id, created_at, id);
                """
            )
            room_columns = {
                row["name"]
                for row in self.db.execute("PRAGMA table_info(rooms)").fetchall()
            }
            migrations = {
                "draft_text": "TEXT NOT NULL DEFAULT ''",
                "draft_image_path": "TEXT",
                "draft_image_label": "TEXT",
                "draft_image_paths": "TEXT NOT NULL DEFAULT '[]'",
                "draft_image_labels": "TEXT NOT NULL DEFAULT '[]'",
                "draft_reply_to_id": "INTEGER",
                "draft_reply_quote": "TEXT",
                "last_read_at": "REAL NOT NULL DEFAULT 0",
                "scroll_position": "REAL NOT NULL DEFAULT -1",
                "scroll_follow": "INTEGER NOT NULL DEFAULT 1",
            }
            for column, definition in migrations.items():
                if column not in room_columns:
                    self.db.execute(
                        f"ALTER TABLE rooms ADD COLUMN {column} {definition}"
                    )
                    if column == "last_read_at":
                        self.db.execute(
                            "UPDATE rooms SET last_read_at = updated_at WHERE last_read_at = 0"
                        )
            message_columns = {
                row["name"]
                for row in self.db.execute("PRAGMA table_info(messages)").fetchall()
            }
            message_migrations = {
                "image_paths": "TEXT NOT NULL DEFAULT '[]'",
                "reply_to_id": "INTEGER REFERENCES messages(id) ON DELETE SET NULL",
                "reply_quote": "TEXT",
                "duration_seconds": "REAL",
                "token_usage": "INTEGER",
            }
            for column, definition in message_migrations.items():
                if column not in message_columns:
                    self.db.execute(
                        f"ALTER TABLE messages ADD COLUMN {column} {definition}"
                    )
            # Routing requires a single local room per app-server thread.  Old
            # accidental duplicates are detached (their local history stays)
            # before enforcing that invariant for future writes.
            duplicates = self.db.execute(
                """SELECT thread_id FROM rooms
                   WHERE thread_id IS NOT NULL
                   GROUP BY thread_id HAVING COUNT(*) > 1"""
            ).fetchall()
            for duplicate in duplicates:
                rows = self.db.execute(
                    """SELECT id FROM rooms WHERE thread_id = ?
                       ORDER BY updated_at DESC, id DESC""",
                    (duplicate[0],),
                ).fetchall()
                for row in rows[1:]:
                    self.db.execute(
                        "UPDATE rooms SET thread_id = NULL WHERE id = ?", (row[0],)
                    )
            self.db.execute(
                """CREATE UNIQUE INDEX IF NOT EXISTS idx_rooms_thread_unique
                   ON rooms(thread_id) WHERE thread_id IS NOT NULL"""
            )
            self.db.execute(
                "UPDATE rooms SET title = 'New chat' WHERE title = ?",
                ("\u06af\u0641\u062a\u200c\u0648\u06af\u0648\u06cc \u062a\u0627\u0632\u0647",),
            )

    @staticmethod
    def _string_list(value: Any) -> List[str]:
        """Normalize a persisted JSON list or a public path sequence."""
        if value is None:
            return []
        if isinstance(value, os.PathLike):
            value = os.fspath(value)
        if isinstance(value, str):
            if not value:
                return []
            try:
                decoded = json.loads(value)
            except (TypeError, ValueError):
                decoded = None
            if isinstance(decoded, list):
                value = decoded
            else:
                value = [value]
        try:
            items: Iterable[Any] = iter(value)
        except TypeError:
            items = iter([value])
        result: List[str] = []
        for item in items:
            if isinstance(item, os.PathLike):
                item = os.fspath(item)
            if item is None:
                continue
            text = str(item)
            if text and text not in result:
                result.append(text)
        return result

    @classmethod
    def _row(cls, row: sqlite3.Row) -> Dict[str, Any]:
        result = {key: row[key] for key in row.keys()}
        if "image_path" in result:
            paths = cls._string_list(result.get("image_paths"))
            if not paths and result.get("image_path"):
                paths = [str(result["image_path"])]
            result["image_paths"] = paths
        if "draft_image_path" in result:
            paths = cls._string_list(result.get("draft_image_paths"))
            if not paths and result.get("draft_image_path"):
                paths = [str(result["draft_image_path"])]
            labels = cls._string_list(result.get("draft_image_labels"))
            if not labels and result.get("draft_image_label"):
                labels = [str(result["draft_image_label"])]
            while len(labels) < len(paths):
                labels.append(Path(paths[len(labels)]).name)
            result["draft_image_paths"] = paths
            result["draft_image_labels"] = labels[:len(paths)]
        if "reply_image_path" in result:
            paths = cls._string_list(result.get("reply_image_paths"))
            if not paths and result.get("reply_image_path"):
                paths = [str(result["reply_image_path"])]
            result["reply_image_paths"] = paths
        return result

    def list_rooms(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.db.execute(
                "SELECT * FROM rooms ORDER BY updated_at DESC, id DESC"
            ).fetchall()
        return [self._row(row) for row in rows]

    def list_room_summaries(self, query: str = "") -> List[Dict[str, Any]]:
        """Return rooms with a lightweight last-message preview for the sidebar."""
        needle = query.strip()
        escaped_needle = (
            needle.replace("!", "!!").replace("%", "!%").replace("_", "!_")
        )
        pattern = f"%{escaped_needle}%"
        with self._lock:
            rows = self.db.execute(
                """SELECT rooms.*,
                          (SELECT content FROM messages
                           WHERE messages.room_id = rooms.id
                           ORDER BY created_at DESC, id DESC LIMIT 1) AS last_message,
                          (SELECT role FROM messages
                           WHERE messages.room_id = rooms.id
                           ORDER BY created_at DESC, id DESC LIMIT 1) AS last_role
                   FROM rooms
                   WHERE ? = ''
                      OR rooms.title LIKE ? ESCAPE '!' COLLATE NOCASE
                      OR EXISTS (
                          SELECT 1 FROM messages
                          WHERE messages.room_id = rooms.id
                            AND messages.content LIKE ? ESCAPE '!' COLLATE NOCASE
                      )
                   ORDER BY rooms.updated_at DESC, rooms.id DESC""",
                (needle, pattern, pattern),
            ).fetchall()
        return [self._row(row) for row in rows]

    def create_room(self, title: str = "New chat") -> Dict[str, Any]:
        now = time.time()
        title = title.strip() or "New chat"
        with self._lock, self.db:
            cursor = self.db.execute(
                "INSERT INTO rooms(title, created_at, updated_at) VALUES(?, ?, ?)",
                (title, now, now),
            )
            row = self.db.execute(
                "SELECT * FROM rooms WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
        assert row is not None
        return self._row(row)

    def rename_room(self, room_id: int, title: str) -> Optional[Dict[str, Any]]:
        clean_title = " ".join(title.split())[:120] or "New chat"
        self.update_room(room_id, title=clean_title)
        return self.get_room(room_id)

    def delete_room(self, room_id: int) -> bool:
        """Delete one room and safely clean only its now-unreferenced uploads."""
        with self._lock:
            room = self.db.execute(
                """SELECT draft_image_path, draft_image_paths
                   FROM rooms WHERE id = ?""",
                (room_id,),
            ).fetchone()
            if room is None:
                return False
            image_rows = self.db.execute(
                "SELECT image_path, image_paths FROM messages WHERE room_id = ?",
                (room_id,),
            ).fetchall()
            candidates = {
                path
                for row in image_rows
                for path in self._string_list(row[1])
            }
            candidates.update(str(row[0]) for row in image_rows if row[0])
            if room[0]:
                candidates.add(str(room[0]))
            candidates.update(self._string_list(room[1]))
            with self.db:
                self.db.execute("DELETE FROM rooms WHERE id = ?", (room_id,))
        for image_path in candidates:
            self.remove_image_if_unused(image_path)
        return True

    def get_room(self, room_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self.db.execute(
                "SELECT * FROM rooms WHERE id = ?", (room_id,)
            ).fetchone()
        return self._row(row) if row else None

    def get_room_by_thread_id(self, thread_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self.db.execute(
                "SELECT * FROM rooms WHERE thread_id = ?", (thread_id,)
            ).fetchone()
        return self._row(row) if row else None

    def update_room(self, room_id: int, **values: Any) -> None:
        allowed = {
            "title", "thread_id", "model", "effort", "updated_at",
            "draft_text", "draft_image_path", "draft_image_label",
            "draft_image_paths", "draft_image_labels",
            "draft_reply_to_id",
            "draft_reply_quote",
            "last_read_at",
            "scroll_position", "scroll_follow",
        }
        clean = {key: value for key, value in values.items() if key in allowed}
        if "draft_image_paths" in clean:
            paths = self._string_list(clean["draft_image_paths"])
            labels = self._string_list(
                clean.get("draft_image_labels", clean.get("draft_image_label"))
            )
            while len(labels) < len(paths):
                labels.append(Path(paths[len(labels)]).name)
            labels = labels[:len(paths)]
            clean["draft_image_paths"] = json.dumps(paths, ensure_ascii=False)
            clean["draft_image_labels"] = json.dumps(labels, ensure_ascii=False)
            clean["draft_image_path"] = paths[0] if paths else None
            clean["draft_image_label"] = labels[0] if labels else None
        elif "draft_image_path" in clean:
            paths = self._string_list(clean["draft_image_path"])
            label = clean.get("draft_image_label")
            labels = [str(label) if label else Path(paths[0]).name] if paths else []
            clean["draft_image_path"] = paths[0] if paths else None
            clean["draft_image_label"] = labels[0] if labels else None
            clean["draft_image_paths"] = json.dumps(paths, ensure_ascii=False)
            clean["draft_image_labels"] = json.dumps(labels, ensure_ascii=False)
        elif "draft_image_labels" in clean:
            clean["draft_image_labels"] = json.dumps(
                self._string_list(clean["draft_image_labels"]), ensure_ascii=False
            )
        draft_fields = {
            "draft_text", "draft_image_path", "draft_image_label",
            "draft_image_paths", "draft_image_labels",
            "draft_reply_to_id",
            "draft_reply_quote",
        }
        passive_fields = draft_fields | {"last_read_at", "scroll_position", "scroll_follow"}
        if any(key not in passive_fields for key in clean):
            clean.setdefault("updated_at", time.time())
        if not clean:
            return
        columns = ", ".join(f"{key} = ?" for key in clean)
        params = list(clean.values()) + [room_id]
        with self._lock, self.db:
            self.db.execute(f"UPDATE rooms SET {columns} WHERE id = ?", params)

    def list_messages(self, room_id: int) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.db.execute(
                "SELECT * FROM messages WHERE room_id = ? ORDER BY created_at, id",
                (room_id,),
            ).fetchall()
        return [self._row(row) for row in rows]

    def list_messages_with_replies(
        self,
        room_id: int,
        limit: int = 120,
        before_id: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Page history newest-first internally, returned in display order."""
        safe_limit = max(1, min(int(limit), 500))
        before = int(before_id) if before_id is not None else 2**63 - 1
        with self._lock:
            rows = self.db.execute(
                """SELECT page.*, parent.content AS reply_content,
                          parent.role AS reply_role,
                          parent.image_path AS reply_image_path,
                          parent.image_paths AS reply_image_paths
                   FROM (
                       SELECT * FROM messages
                       WHERE room_id = ? AND id < ?
                       ORDER BY id DESC LIMIT ?
                   ) AS page
                   LEFT JOIN messages AS parent ON parent.id = page.reply_to_id
                   ORDER BY page.id ASC""",
                (room_id, before, safe_limit),
            ).fetchall()
        return [self._row(row) for row in rows]

    def has_messages_before(self, room_id: int, message_id: int) -> bool:
        with self._lock:
            row = self.db.execute(
                "SELECT 1 FROM messages WHERE room_id = ? AND id < ? LIMIT 1",
                (room_id, message_id),
            ).fetchone()
        return row is not None

    def list_unread_room_ids(self) -> set[int]:
        with self._lock:
            rows = self.db.execute(
                """SELECT DISTINCT rooms.id
                   FROM rooms JOIN messages ON messages.room_id = rooms.id
                   WHERE messages.role = 'assistant'
                     AND messages.created_at > rooms.last_read_at"""
            ).fetchall()
        return {int(row[0]) for row in rows}

    def mark_room_read(self, room_id: int) -> None:
        self.update_room(room_id, last_read_at=time.time())

    def add_message(
        self,
        room_id: int,
        role: str,
        content: str,
        image_path: Optional[str] = None,
        reply_to_id: Optional[int] = None,
        reply_quote: Optional[str] = None,
        duration_seconds: Optional[float] = None,
        token_usage: Optional[int] = None,
        image_paths: Optional[Iterable[str]] = None,
    ) -> int:
        paths = self._string_list(image_paths)
        legacy_paths = self._string_list(image_path)
        for path in reversed(legacy_paths):
            if path not in paths:
                paths.insert(0, path)
        primary_image_path = paths[0] if paths else None
        now = time.time()
        with self._lock, self.db:
            cursor = self.db.execute(
                """INSERT INTO messages(
                       room_id, role, content, image_path, image_paths,
                       reply_to_id, reply_quote, duration_seconds, token_usage,
                       created_at
                   ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    room_id, role, content, primary_image_path,
                    json.dumps(paths, ensure_ascii=False), reply_to_id,
                    (str(reply_quote).strip()[:8000] or None)
                    if reply_quote is not None
                    else None,
                    duration_seconds, token_usage, now,
                ),
            )
            self.db.execute(
                "UPDATE rooms SET updated_at = ? WHERE id = ?", (now, room_id)
            )
        return int(cursor.lastrowid)

    def get_message(self, message_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self.db.execute(
                "SELECT * FROM messages WHERE id = ?", (message_id,)
            ).fetchone()
        return self._row(row) if row else None

    def create_branch(
        self,
        room_id: int,
        parent_message_id: int,
        selected_quote: str,
        thread_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Create an isolated branch anchored to an assistant message."""
        quote = selected_quote.strip()
        if not quote:
            raise ValueError("selected_quote must not be empty")
        normalized_thread_id = (thread_id or "").strip() or None
        now = time.time()
        with self._lock, self.db:
            parent = self.db.execute(
                "SELECT role FROM messages WHERE id = ? AND room_id = ?",
                (parent_message_id, room_id),
            ).fetchone()
            if parent is None:
                raise ValueError("parent_message_id does not belong to room_id")
            if parent["role"] != "assistant":
                raise ValueError("parent_message_id must reference an assistant message")
            cursor = self.db.execute(
                """INSERT INTO branches(
                       room_id, parent_message_id, selected_quote, thread_id,
                       created_at, updated_at
                   ) VALUES(?, ?, ?, ?, ?, ?)""",
                (
                    room_id,
                    parent_message_id,
                    quote,
                    normalized_thread_id,
                    now,
                    now,
                ),
            )
            row = self.db.execute(
                "SELECT * FROM branches WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
        assert row is not None
        return self._row(row)

    def get_branch(self, branch_id: int) -> Optional[Dict[str, Any]]:
        """Return one branch without loading its private messages."""
        with self._lock:
            row = self.db.execute(
                "SELECT * FROM branches WHERE id = ?", (branch_id,)
            ).fetchone()
        return self._row(row) if row else None

    def get_branch_by_thread_id(self, thread_id: str) -> Optional[Dict[str, Any]]:
        """Return the branch associated with an app-server thread."""
        with self._lock:
            row = self.db.execute(
                "SELECT * FROM branches WHERE thread_id = ?", (thread_id,)
            ).fetchone()
        return self._row(row) if row else None

    def list_branches(
        self, room_id: int, parent_message_id: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """List branch threads for a room or one anchored message."""
        params: List[Any] = [room_id]
        where = "room_id = ?"
        if parent_message_id is not None:
            where += " AND parent_message_id = ?"
            params.append(parent_message_id)
        with self._lock:
            rows = self.db.execute(
                f"""SELECT * FROM branches WHERE {where}
                    ORDER BY updated_at DESC, id DESC""",
                params,
            ).fetchall()
        return [self._row(row) for row in rows]

    def set_branch_thread_id(
        self, branch_id: int, thread_id: Optional[str]
    ) -> Optional[Dict[str, Any]]:
        """Attach or clear the app-server thread used by a branch."""
        normalized_thread_id = (thread_id or "").strip() or None
        with self._lock, self.db:
            cursor = self.db.execute(
                "UPDATE branches SET thread_id = ?, updated_at = ? WHERE id = ?",
                (normalized_thread_id, time.time(), branch_id),
            )
            if cursor.rowcount == 0:
                return None
            row = self.db.execute(
                "SELECT * FROM branches WHERE id = ?", (branch_id,)
            ).fetchone()
        return self._row(row) if row else None

    def add_branch_message(
        self,
        branch_id: int,
        role: str,
        content: str,
        duration_seconds: Optional[float] = None,
        token_usage: Optional[int] = None,
    ) -> int:
        """Append a message to a branch without modifying main room history."""
        if role not in {"user", "assistant", "system"}:
            raise ValueError("unsupported branch message role")
        now = time.time()
        with self._lock, self.db:
            branch = self.db.execute(
                "SELECT 1 FROM branches WHERE id = ?", (branch_id,)
            ).fetchone()
            if branch is None:
                raise ValueError("branch_id does not exist")
            cursor = self.db.execute(
                """INSERT INTO branch_messages(
                       branch_id, role, content, duration_seconds,
                       token_usage, created_at
                   ) VALUES(?, ?, ?, ?, ?, ?)""",
                (
                    branch_id,
                    role,
                    content,
                    duration_seconds,
                    token_usage,
                    now,
                ),
            )
            self.db.execute(
                "UPDATE branches SET updated_at = ? WHERE id = ?",
                (now, branch_id),
            )
        return int(cursor.lastrowid)

    def list_branch_messages(self, branch_id: int) -> List[Dict[str, Any]]:
        """List only the messages owned by one branch in display order."""
        with self._lock:
            rows = self.db.execute(
                """SELECT * FROM branch_messages WHERE branch_id = ?
                   ORDER BY created_at, id""",
                (branch_id,),
            ).fetchall()
        return [self._row(row) for row in rows]

    def delete_branch(self, branch_id: int) -> bool:
        """Delete a branch and cascade deletion to its private messages."""
        with self._lock, self.db:
            cursor = self.db.execute(
                "DELETE FROM branches WHERE id = ?", (branch_id,)
            )
        return cursor.rowcount > 0

    def get_setting(self, key: str, default: Optional[str] = None) -> Optional[str]:
        with self._lock:
            row = self.db.execute(
                "SELECT value FROM settings WHERE key = ?", (key,)
            ).fetchone()
        return str(row[0]) if row else default

    def set_setting(self, key: str, value: str) -> None:
        with self._lock, self.db:
            self.db.execute(
                """INSERT INTO settings(key, value) VALUES(?, ?)
                   ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
                (key, value),
            )

    def import_image(self, source: str) -> str:
        path = Path(source)
        suffix = path.suffix.lower() or ".img"
        destination = self.upload_dir / f"{uuid.uuid4().hex}{suffix}"
        shutil.copy2(path, destination)
        return str(destination.resolve())

    def remove_image_if_unused(self, image_path: Optional[str]) -> bool:
        """Delete an imported image only when no message or draft references it."""
        if not image_path:
            return False
        candidate = Path(image_path)
        try:
            candidate.resolve().relative_to(self.upload_dir.resolve())
        except (OSError, ValueError):
            return False
        with self._lock:
            message_rows = self.db.execute(
                "SELECT image_path, image_paths FROM messages"
            ).fetchall()
            room_rows = self.db.execute(
                "SELECT draft_image_path, draft_image_paths FROM rooms"
            ).fetchall()
            message_ref = any(
                row[0] == image_path or image_path in self._string_list(row[1])
                for row in message_rows
            )
            draft_ref = any(
                row[0] == image_path or image_path in self._string_list(row[1])
                for row in room_rows
            )
        if message_ref or draft_ref:
            return False
        try:
            candidate.unlink()
            return True
        except FileNotFoundError:
            return False

    def cleanup_orphaned_uploads(self) -> int:
        with self._lock:
            message_rows = self.db.execute(
                "SELECT image_path, image_paths FROM messages"
            ).fetchall()
            room_rows = self.db.execute(
                "SELECT draft_image_path, draft_image_paths FROM rooms"
            ).fetchall()
            referenced = {
                path
                for row in message_rows
                for path in ([str(row[0])] if row[0] else [])
                + self._string_list(row[1])
            }
            referenced.update(
                path
                for row in room_rows
                for path in ([str(row[0])] if row[0] else [])
                + self._string_list(row[1])
            )
        removed = 0
        for candidate in self.upload_dir.iterdir():
            if candidate.is_file() and str(candidate.resolve()) not in referenced:
                try:
                    candidate.unlink()
                    removed += 1
                except OSError:
                    pass
        return removed

    def close(self) -> None:
        with self._lock:
            self.db.close()
