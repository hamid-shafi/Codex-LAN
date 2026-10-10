"""CODEX: Selective, credential-free native Codex history transfer.

Only the explicitly reviewed Codex 0.159.3 SQLite schemas are supported. Rollout
bytes are never rewritten: paginated history stores byte offsets into these files.
This module never invokes Codex, starts a turn, or changes a running installation.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import time
from contextlib import ExitStack, closing
from pathlib import Path, PurePosixPath

FORMAT = "codex-lan-native-history-v1"
CODEX_VERSION = "0.159.3"
STATE = "state_5.sqlite"
HISTORY = "thread_history_1.sqlite"
SUPPORTED_SCHEMAS = {
    STATE: "a9e4138fe60d4ac5e36ea08571d91ee6f67873278574387caea26fe23860cc19",
    HISTORY: "48a2f3e3062c1b1945701eb8ae7026a73239f334a004fbc00081575efdef5cc3",
}
MIGRATIONS = {STATE: 58, HISTORY: 7}
HISTORY_TABLES = ("thread_turns", "thread_items", "thread_realtime_items", "thread_history_projection_state")
EMPTY_STATE_TABLES = (
    "backfill_state", "remote_control_enrollments", "external_agent_config_imports",
    "project_idempotency_keys", "rollout_migration_state", "rollout_migration_skipped_rollouts",
)


def _path(path: Path) -> Path:
    path = Path(path).absolute()
    if path.resolve() != path or path.is_symlink():
        raise ValueError("Native history paths must not contain symbolic links")
    return path


def _regular(path: Path, root: Path) -> Path:
    path = _path(path)
    if not path.is_relative_to(root) or not path.is_file():
        raise ValueError("Native history file is missing or outside its private directory")
    return path


def _relative(value: str) -> Path:
    if not isinstance(value, str) or "\\" in value:
        raise ValueError("Invalid native history relative path")
    posix = PurePosixPath(value)
    if posix.is_absolute() or ".." in posix.parts or str(posix) != value:
        raise ValueError("Invalid native history relative path")
    return Path(*posix.parts)


def _rollout_relative(value: str) -> Path:
    path = _relative(value)
    if len(path.parts) < 2 or path.parts[0] not in {"sessions", "archived_sessions"} or path.suffix != ".jsonl":
        raise ValueError("Native rollouts must be JSONL files inside session directories")
    return path


def _read(path: Path):
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)
    connection.row_factory = sqlite3.Row
    return connection


def _schema(connection) -> str:
    rows = [tuple(row) for row in connection.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
    )]
    return hashlib.sha256(json.dumps(rows, separators=(",", ":")).encode()).hexdigest()


def _check_schema(connection, name: str):
    if _schema(connection) != SUPPORTED_SCHEMAS[name]:
        raise ValueError("Unsupported native Codex schema; use the reviewed Codex version " + CODEX_VERSION)
    versions = [tuple(row) for row in connection.execute("SELECT version,success FROM _sqlx_migrations ORDER BY version")]
    if versions != [(version, 1) for version in range(1, MIGRATIONS[name] + 1)]:
        raise ValueError("Unsupported or incomplete native Codex migrations")
    if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
        raise ValueError("Native history SQLite integrity check failed")


def _snapshot(source: Path, target: Path):
    deadline = time.monotonic() + 180

    def progress(status, remaining, total):
        if time.monotonic() > deadline:
            raise TimeoutError("Native SQLite snapshot timed out; retry after active prompts finish")

    with closing(_read(source)) as incoming, closing(sqlite3.connect(target)) as outgoing:
        incoming.backup(outgoing, pages=1024, progress=progress, sleep=0.05)
    target.chmod(0o600)


def _new_database(source, target: Path):
    connection = sqlite3.connect(target)
    connection.row_factory = sqlite3.Row
    # Construct fresh pages instead of deleting unrelated private rows from a copy.
    for row in source.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"):
        connection.execute(row[0])
    return connection


def _copy_rows(source, target, table: str, condition: str = "1", params=()):
    cursor = source.execute('SELECT * FROM "' + table + '" WHERE ' + condition, params)
    placeholders = ",".join("?" for _ in cursor.description)
    target.executemany('INSERT INTO "' + table + '" VALUES (' + placeholders + ')', cursor)


def _finish_database(source, target):
    for row in source.execute("SELECT sql FROM sqlite_master WHERE type IN ('index','trigger') AND sql IS NOT NULL ORDER BY type,name"):
        target.execute(row[0])
    target.commit()
    target.execute("PRAGMA journal_mode=DELETE")
    if target.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise ValueError("Native history contains incomplete relational references")


def _file_record(path: Path, relative: Path):
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as incoming:
        for chunk in iter(lambda: incoming.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
    return {"path": relative.as_posix(), "size": size, "sha256": digest.hexdigest()}


def _copy_rollout(source: Path, target: Path, thread_id: str, size: int, ordinal: int):
    if not isinstance(size, int) or size < 1 or not isinstance(ordinal, int) or ordinal < 1:
        raise ValueError("Invalid native rollout projection bounds")
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    digest, lines, last_byte = hashlib.sha256(), 0, b""
    with source.open("rb") as incoming, target.open("xb") as outgoing:
        before = os.fstat(incoming.fileno())
        if before.st_size < size:
            raise ValueError("Native rollout is shorter than its history projection")
        first = incoming.readline(min(size, 16 * 1024 * 1024))
        try:
            metadata = json.loads(first)
        except (ValueError, UnicodeError):
            raise ValueError("Invalid native rollout session metadata") from None
        if metadata.get("type") != "session_meta" or metadata.get("payload", {}).get("id") != thread_id:
            raise ValueError("Native rollout does not match its thread")
        incoming.seek(0)
        remaining = size
        while remaining:
            chunk = incoming.read(min(1024 * 1024, remaining))
            if not chunk:
                raise ValueError("Native rollout changed while being copied")
            outgoing.write(chunk)
            digest.update(chunk)
            remaining -= len(chunk)
            lines += chunk.count(b"\n")
            last_byte = chunk[-1:]
        after = os.fstat(incoming.fileno())
        if before.st_ino != after.st_ino or before.st_dev != after.st_dev or after.st_size < size:
            raise ValueError("Native rollout changed while being copied")
    target.chmod(0o600)
    # Codex can skip historical records it cannot deserialize, so its ordinal
    # may be smaller than the physical line count. Never renumber that history.
    if last_byte != b"\n" or lines < ordinal:
        raise ValueError("Native rollout byte and ordinal projections do not agree")
    return digest.hexdigest()


def _selected_threads(state, roots):
    selected = set(roots)
    edges = [tuple(row) for row in state.execute("SELECT parent_thread_id,child_thread_id FROM thread_spawn_edges")]
    while True:
        previous = len(selected)
        selected.update(child for parent, child in edges if parent in selected)
        if len(selected) == previous:
            break
    known = {row[0] for row in state.execute("SELECT id FROM threads")}
    if not selected <= known:
        raise ValueError("A requested native thread or descendant is missing")
    return sorted(selected)


def export_native(source_home: Path, destination: Path, thread_ids: list[str], *, asset_roots: list[Path] | None = None) -> dict:
    """Export selected roots and descendants into a new private payload directory.

    Each rollout is bounded by the history snapshot's projection. Events appended
    after that snapshot stay on the source, so the two exported stores agree.
    """
    source_home, destination = _path(source_home), _path(destination)
    if not thread_ids or len(thread_ids) != len(set(thread_ids)) or any(not isinstance(item, str) or not item for item in thread_ids):
        raise ValueError("Specify unique native root thread IDs")
    if destination.exists() or destination.is_relative_to(source_home):
        raise ValueError("Native export needs a new directory outside its source")
    source_paths = {name: _regular(source_home / name, source_home) for name in SUPPORTED_SCHEMAS}
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    destination.mkdir(mode=0o700)
    try:
        with tempfile.TemporaryDirectory(prefix=".native-snapshot-", dir=destination.parent) as temporary, ExitStack() as stack:
            snapshots = {}
            # Take state first so each known thread exists when history is captured.
            for name in (STATE, HISTORY):
                snapshot = Path(temporary) / name
                _snapshot(source_paths[name], snapshot)
                snapshots[name] = stack.enter_context(closing(_read(snapshot)))
                _check_schema(snapshots[name], name)
            state, history = snapshots[STATE], snapshots[HISTORY]
            selected = _selected_threads(state, thread_ids)
            with closing(_read(source_paths[STATE])) as current_state:
                if _selected_threads(current_state, thread_ids) != selected:
                    raise ValueError("Native descendants changed during snapshot; retry after active prompts finish")
            for connection in (state, history):
                connection.execute("CREATE TEMP TABLE selected_threads(id TEXT PRIMARY KEY)")
                connection.executemany("INSERT INTO selected_threads VALUES (?)", ((item,) for item in selected))
            filtered = {}
            for name in (STATE, HISTORY):
                filtered[name] = stack.enter_context(closing(_new_database(snapshots[name], destination / name)))
                _copy_rows(snapshots[name], filtered[name], "_sqlx_migrations")
            target_state, target_history = filtered[STATE], filtered[HISTORY]
            chosen = "thread_id IN (SELECT id FROM selected_threads)"
            _copy_rows(state, target_state, "threads", "id IN (SELECT id FROM selected_threads)")
            for table in ("thread_dynamic_tools", "thread_attachments"):
                _copy_rows(state, target_state, table, chosen)
            if target_state.execute("SELECT count(*) FROM thread_attachments").fetchone()[0]:
                raise ValueError("Native thread attachment artifacts are not supported by this transfer version")
            _copy_rows(state, target_state, "thread_spawn_edges",
                       "parent_thread_id IN (SELECT id FROM selected_threads) AND child_thread_id IN (SELECT id FROM selected_threads)")
            _copy_rows(state, target_state, "thread_sections",
                       "id IN (SELECT thread_section_id FROM threads WHERE id IN (SELECT id FROM selected_threads))")
            _copy_rows(state, target_state, "projects",
                       "id IN (SELECT project_id FROM threads WHERE id IN (SELECT id FROM selected_threads))")
            _copy_rows(state, target_state, "project_roots",
                       "project_id IN (SELECT project_id FROM threads WHERE id IN (SELECT id FROM selected_threads))")
            for table in HISTORY_TABLES:
                _copy_rows(history, target_history, table, chosen)
            # Check all asset boundaries before streaming potentially large rollouts.
            _asset_sources(target_history, source_home, asset_roots)
            rollouts = []
            paths = set()
            for row in target_state.execute("SELECT id,rollout_path,history_mode FROM threads ORDER BY id").fetchall():
                if row["history_mode"] != "paginated":
                    raise ValueError("Only reviewed paginated native history is supported")
                source = _regular(Path(row["rollout_path"]), source_home)
                relative = _rollout_relative(source.relative_to(source_home).as_posix())
                if relative in paths:
                    raise ValueError("Multiple native threads refer to one rollout")
                paths.add(relative)
                projection = target_history.execute(
                    "SELECT next_rollout_byte_offset,next_rollout_ordinal FROM thread_history_projection_state WHERE thread_id=?", (row["id"],)
                ).fetchone()
                if projection is None:
                    raise ValueError("Native paginated history projection is missing")
                size, ordinal = projection
                digest = _copy_rollout(source, destination / relative, row["id"], size, ordinal)
                rollouts.append({"thread_id": row["id"], "path": relative.as_posix(), "size": size,
                                 "sha256": digest, "projection_offset": size, "projection_ordinal": ordinal})
                target_state.execute("UPDATE threads SET rollout_path=? WHERE id=?", (relative.as_posix(), row["id"]))
            for name in (STATE, HISTORY):
                _finish_database(snapshots[name], filtered[name])
                (destination / name).chmod(0o600)
            counts = {"threads": len(selected), "spawn_edges": target_state.execute("SELECT count(*) FROM thread_spawn_edges").fetchone()[0]}
            counts.update({label: target_history.execute("SELECT count(*) FROM " + table).fetchone()[0]
                           for label, table in (("turns", "thread_turns"), ("items", "thread_items"))})
        files = [_file_record(destination / name, Path(name)) for name in (STATE, HISTORY)]
        files += [{key: row[key] for key in ("path", "size", "sha256")} for row in rollouts]
        metadata = {"format": FORMAT, "codex_version": CODEX_VERSION, "root_thread_ids": list(thread_ids),
                    "thread_ids": selected, "counts": counts, "schemas": dict(SUPPORTED_SCHEMAS),
                    "files": files, "rollouts": rollouts}
        add_native_assets(source_home, destination, metadata, asset_roots=asset_roots)
        _validate_databases(destination, metadata)
        return metadata
    except BaseException:
        shutil.rmtree(destination)
        raise


def _image_objects(value):
    if isinstance(value, dict):
        if value.get("type") == "localImage" and isinstance(value.get("path"), str):
            yield value
        for child in value.values():
            yield from _image_objects(child)
    elif isinstance(value, list):
        for child in value:
            yield from _image_objects(child)


def _asset_relative(value: str) -> Path:
    relative = _relative(value)
    if len(relative.parts) < 2 or relative.parts[0] != "uploads":
        raise ValueError("Native image assets must be inside their uploads directory")
    return relative


def _asset_sources(history, source_home: Path, asset_roots=None):
    roots = [source_home / "uploads"] + [_path(root) for root in (asset_roots or [])]
    if any(root.name != "uploads" for root in roots):
        raise ValueError("Explicit native asset roots must be upload directories")
    references = set()
    for row in history.execute("SELECT item_json FROM thread_items WHERE item_type='userMessage'"):
        references.update(image["path"] for image in _image_objects(json.loads(row[0])))
    sources = []
    for original in sorted(references):
        path = _path(Path(original))
        root = next((root for root in roots if path.is_relative_to(root)), None)
        if root is None:
            raise ValueError("A selected native image needs an explicitly allowed legacy upload root")
        source = _regular(path, root)
        if source.stat().st_uid != os.getuid():
            raise ValueError("A native image is not owned by the current user")
        relative = (source.relative_to(source_home) if root == roots[0]
                    else Path("uploads/legacy") / (hashlib.sha256(original.encode()).hexdigest() + source.suffix))
        sources.append((original, source, _asset_relative(relative.as_posix())))
    return sources


def add_native_assets(source_home: Path, payload: Path, metadata: dict, *, asset_roots: list[Path] | None = None):
    """Copy only projected local images; model context images stay inline.

    This also supports enriching an already captured private snapshot without
    rereading multi-gigabyte rollouts. No unreferenced upload is included.
    """
    source_home, payload = _path(source_home), _path(payload)
    if "assets" in metadata:
        raise ValueError("Native image assets have already been captured")
    with closing(_read(payload / HISTORY)) as history:
        sources = _asset_sources(history, source_home, asset_roots)
    assets = []
    for original, source, relative in sources:
        target = payload / relative
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with source.open("rb") as incoming, target.open("xb") as outgoing:
            shutil.copyfileobj(incoming, outgoing, length=1024 * 1024)
        target.chmod(0o600)
        record = _file_record(target, relative)
        metadata["files"].append(record)
        assets.append({**record, "original_path": original})
    metadata["assets"] = assets


def _validate_databases(payload: Path, metadata: dict):
    with closing(_read(payload / STATE)) as state, closing(_read(payload / HISTORY)) as history:
        for name, connection in ((STATE, state), (HISTORY, history)):
            _check_schema(connection, name)
            if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise ValueError("Native history foreign key check failed")
        selected = set(metadata["thread_ids"])
        actual = {row[0] for row in state.execute("SELECT id FROM threads")}
        if actual != selected or not set(metadata["root_thread_ids"]) <= selected:
            raise ValueError("Native payload thread selection does not match its manifest")
        for table in EMPTY_STATE_TABLES:
            if state.execute("SELECT count(*) FROM " + table).fetchone()[0]:
                raise ValueError("Native payload contains unrelated installation state")
        for table in ("thread_dynamic_tools", "thread_attachments"):
            if any(row[0] not in selected for row in state.execute("SELECT DISTINCT thread_id FROM " + table)):
                raise ValueError("Native payload contains unselected thread metadata")
        if state.execute("SELECT count(*) FROM thread_attachments").fetchone()[0]:
            raise ValueError("Native thread attachment artifacts are not supported by this transfer version")
        for table, column in (("thread_sections", "thread_section_id"), ("projects", "project_id")):
            if state.execute("SELECT count(*) FROM " + table + " WHERE id NOT IN (SELECT " + column + " FROM threads WHERE " + column + " IS NOT NULL)").fetchone()[0]:
                raise ValueError("Native payload contains unselected project or section metadata")
        for table in HISTORY_TABLES:
            if any(row[0] not in selected for row in history.execute("SELECT DISTINCT thread_id FROM " + table)):
                raise ValueError("Native payload contains unselected history")
        for parent, child in state.execute("SELECT parent_thread_id,child_thread_id FROM thread_spawn_edges"):
            if parent not in selected or child not in selected:
                raise ValueError("Native payload contains an unselected spawn edge")
        rollouts = {row["thread_id"]: row for row in metadata["rollouts"]}
        if set(rollouts) != selected or len(rollouts) != len(metadata["rollouts"]):
            raise ValueError("Native payload rollout selection does not match its manifest")
        for row in state.execute("SELECT id,rollout_path,history_mode FROM threads"):
            record = rollouts[row["id"]]
            if record["path"] != row["rollout_path"] or row["history_mode"] != "paginated":
                raise ValueError("Native rollout metadata is inconsistent")
            projection = history.execute("SELECT next_rollout_byte_offset,next_rollout_ordinal FROM thread_history_projection_state WHERE thread_id=?", (row["id"],)).fetchone()
            if projection is None or tuple(projection) != (record["size"], record["projection_ordinal"]) or record["projection_offset"] != record["size"]:
                raise ValueError("Native rollout projection is inconsistent")
            bounds = history.execute(
                "SELECT max(rollout_ordinal),max(rollout_end_ordinal),max(rollout_byte_offset),max(rollout_end_byte_offset) FROM thread_turns WHERE thread_id=?", (row["id"],)
            ).fetchone()
            if any(value is not None and value >= record["projection_ordinal"] for value in bounds[:2]) or any(value is not None and value > record["size"] for value in bounds[2:]):
                raise ValueError("Native turn projection extends beyond its rollout")
            for table in ("thread_items", "thread_realtime_items"):
                ordinal = history.execute("SELECT max(rollout_ordinal) FROM " + table + " WHERE thread_id=?", (row["id"],)).fetchone()[0]
                if ordinal is not None and ordinal >= record["projection_ordinal"]:
                    raise ValueError("Native item projection extends beyond its rollout")


def validate_native(payload: Path, metadata: dict) -> dict:
    """Validate a fully extracted payload before any installation mutation."""
    payload = _path(payload)
    if metadata.get("format") != FORMAT or metadata.get("codex_version") != CODEX_VERSION or metadata.get("schemas") != SUPPORTED_SCHEMAS:
        raise ValueError("Unsupported native history manifest")
    expected = {}
    for record in metadata["files"]:
        relative = _relative(record["path"])
        if str(relative) not in SUPPORTED_SCHEMAS:
            if relative.parts[0] == "uploads":
                _asset_relative(record["path"])
            else:
                _rollout_relative(record["path"])
        if relative in expected:
            raise ValueError("Duplicate native payload file")
        expected[relative] = record
        source = _regular(payload / relative, payload)
        if _file_record(source, relative) != record:
            raise ValueError("Native history file checksum or size mismatch")
    wanted = ({Path(STATE), Path(HISTORY)} | {_rollout_relative(row["path"]) for row in metadata["rollouts"]}
              | {_asset_relative(row["path"]) for row in metadata.get("assets", [])})
    actual = {path.relative_to(payload) for path in payload.rglob("*") if path.is_file() or path.is_symlink()}
    if set(expected) != wanted or actual != wanted:
        raise ValueError("Native history contains missing or unexpected files")
    for row in metadata["rollouts"] + metadata.get("assets", []):
        if {key: row[key] for key in ("path", "size", "sha256")} != expected[Path(row["path"])]:
            raise ValueError("Native rollout checksums disagree with the file manifest")
    with closing(_read(payload / HISTORY)) as history:
        references = set()
        for row in history.execute("SELECT item_json FROM thread_items WHERE item_type='userMessage'"):
            references.update(image["path"] for image in _image_objects(json.loads(row[0])))
    originals = [row["original_path"] for row in metadata.get("assets", [])]
    if references != set(originals) or len(originals) != len(set(originals)):
        raise ValueError("Native image assets do not match selected history references")
    _validate_databases(payload, metadata)
    return dict(metadata["counts"])


def preflight_native(target_home: Path, metadata: dict | None = None) -> dict:
    """Require an unused native history destination; existing logins are untouched."""
    target_home = _path(target_home)
    if target_home.exists() and not target_home.is_dir():
        raise ValueError("Native target must be a directory")
    if target_home.exists():
        for pattern in ("state_*.sqlite*", "thread_history_*.sqlite*", "sessions", "archived_sessions", "session_index.jsonl", "history.jsonl"):
            if list(target_home.glob(pattern)):
                raise ValueError("Native target already has history; import into a fresh installation before starting its worker")
        if metadata and metadata.get("assets") and (target_home / "uploads").exists():
            raise ValueError("Native target already has an uploads directory; move an empty one aside before import")
    return {"target_home": str(target_home), "new_native_store": True}


def empty_native_store_paths(target_home: Path) -> list[Path]:
    """Return exact empty-store paths callers may move into a rollback backup.

    Call only after stopping the destination worker. This is read-only and does
    not authorize removal of existing conversations. Unknown schemas fail closed.
    """
    target_home = _path(target_home)
    paths = []
    if not target_home.exists():
        return paths
    expected = {name + suffix for name in SUPPORTED_SCHEMAS for suffix in ("", "-wal", "-shm")}
    for pattern in ("state_*.sqlite*", "thread_history_*.sqlite*"):
        for path in target_home.glob(pattern):
            _regular(path, target_home)
            if path.name not in expected:
                raise ValueError("Destination contains an unsupported native database")
            paths.append(path)
    for name in SUPPORTED_SCHEMAS:
        path = target_home / name
        if not path.exists():
            if any(item.name.startswith(name + "-") for item in paths):
                raise ValueError("Destination contains orphaned native database sidecars")
            continue
        with closing(_read(path)) as connection:
            _check_schema(connection, name)
            ignored = {"_sqlx_migrations", "sqlite_sequence", "backfill_state", "thread_sections"}
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'"):
                table = row[0]
                if table not in ignored and connection.execute('SELECT count(*) FROM "' + table + '"').fetchone()[0]:
                    raise ValueError("Destination already contains native history or installation state")
    for name in ("sessions", "archived_sessions", "uploads"):
        path = target_home / name
        if path.exists() or path.is_symlink():
            _path(path)
            if not path.is_dir() or any(item.is_file() or item.is_symlink() for item in path.rglob("*")):
                raise ValueError("Destination already contains native rollout files")
            paths.append(path)
    for name in ("history.jsonl", "session_index.jsonl"):
        path = target_home / name
        if path.exists() or path.is_symlink():
            if _regular(path, target_home).stat().st_size:
                raise ValueError("Destination already contains native history indexes")
            paths.append(path)
    return sorted(paths)


# Short public spelling used by the standalone transfer orchestrator.
empty_store_paths = empty_native_store_paths


def _remap(value: str, old_home: str, new_home: str) -> str:
    if value == old_home:
        return new_home
    if value.startswith(old_home.rstrip("/") + "/"):
        return new_home.rstrip("/") + value[len(old_home.rstrip("/")):]
    return value


def import_native(payload: Path, target_home: Path, metadata: dict, old_home: str, new_home: str) -> dict:
    """Install into an unused native store, preserving history bytes and thread IDs.

    Only SQLite filesystem metadata is remapped. Historical messages, tool
    outputs, compacted context and session metadata in rollouts remain verbatim.
    """
    payload, target_home = _path(payload), _path(target_home)
    if not Path(old_home).is_absolute() or not Path(new_home).is_absolute() or old_home == "/" or new_home == "/":
        raise ValueError("Home remapping needs non-root absolute directories")
    validate_native(payload, metadata)
    preflight_native(target_home, metadata)
    target_home.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    created = not target_home.exists()
    target_home.mkdir(mode=0o700, exist_ok=True)
    installed = []
    try:
        with tempfile.TemporaryDirectory(prefix=".native-import-", dir=target_home.parent) as temporary:
            staging = Path(temporary)
            for record in metadata["files"]:
                relative = _relative(record["path"])
                target = staging / relative
                target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                shutil.copyfile(payload / relative, target)
                target.chmod(0o600)
            with closing(sqlite3.connect(staging / STATE)) as state:
                for thread_id, rollout, cwd, sandbox in state.execute("SELECT id,rollout_path,cwd,sandbox_policy FROM threads").fetchall():
                    policy = json.loads(sandbox)
                    if isinstance(policy, dict) and isinstance(policy.get("writable_roots"), list):
                        policy["writable_roots"] = [_remap(value, old_home, new_home) if isinstance(value, str) else value for value in policy["writable_roots"]]
                    state.execute("UPDATE threads SET rollout_path=?,cwd=?,sandbox_policy=? WHERE id=?",
                                  (str(target_home / _rollout_relative(rollout)), _remap(cwd, old_home, new_home), json.dumps(policy, separators=(",", ":")), thread_id))
                for project_id, position, path in state.execute("SELECT project_id,position,path FROM project_roots").fetchall():
                    state.execute("UPDATE project_roots SET path=? WHERE project_id=? AND position=?", (_remap(path, old_home, new_home), project_id, position))
                state.commit()
            replacements = {row["original_path"]: str(target_home / _asset_relative(row["path"])) for row in metadata.get("assets", [])}
            with closing(sqlite3.connect(staging / HISTORY)) as history:
                for thread_id, turn_id, item_id, item_json in history.execute("SELECT thread_id,turn_id,item_id,item_json FROM thread_items WHERE item_type='userMessage'").fetchall():
                    value = json.loads(item_json)
                    changed = False
                    for image in _image_objects(value):
                        image["path"] = replacements[image["path"]]
                        changed = True
                    if changed:
                        history.execute("UPDATE thread_items SET item_json=? WHERE thread_id=? AND turn_id=? AND item_id=?",
                                        (json.dumps(value, ensure_ascii=False, separators=(",", ":")), thread_id, turn_id, item_id))
                history.commit()
            preflight_native(target_home, metadata)
            for source in sorted(staging.iterdir()):
                destination = target_home / source.name
                if destination.exists() or destination.is_symlink():
                    raise ValueError("Native destination changed during import")
                source.rename(destination)
                installed.append(destination)
        return {**metadata["counts"], "thread_ids": list(metadata["thread_ids"]), "root_thread_ids": list(metadata["root_thread_ids"]),
                "codex_version": CODEX_VERSION, "created_paths": [str(path) for path in installed]}
    except BaseException:
        for path in reversed(installed):
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
        if created and not any(target_home.iterdir()):
            target_home.rmdir()
        raise
