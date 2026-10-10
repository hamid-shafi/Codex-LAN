"""CODEX: Selective native history, immutable rollout offsets and fresh imports."""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


SOURCE = Path(__file__).resolve().parents[2] / "setup/maintenance/native_history_transfer.py"
SPEC = importlib.util.spec_from_file_location("native_history_transfer", SOURCE)
native = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(native)

STATE_SQL = """
CREATE TABLE _sqlx_migrations(version INTEGER PRIMARY KEY,success INTEGER);
CREATE TABLE threads(id TEXT PRIMARY KEY,rollout_path TEXT,cwd TEXT,sandbox_policy TEXT,
 history_mode TEXT,thread_section_id TEXT,project_id TEXT,title TEXT);
CREATE TABLE thread_dynamic_tools(thread_id TEXT,position INTEGER,name TEXT);
CREATE TABLE thread_attachments(id TEXT,thread_id TEXT,payload TEXT);
CREATE TABLE thread_spawn_edges(parent_thread_id TEXT,child_thread_id TEXT PRIMARY KEY,status TEXT);
CREATE TABLE thread_sections(id TEXT PRIMARY KEY,name TEXT);
CREATE TABLE projects(id TEXT PRIMARY KEY,name TEXT);
CREATE TABLE project_roots(project_id TEXT,position INTEGER,path TEXT);
CREATE TABLE backfill_state(id INTEGER,status TEXT);
CREATE TABLE remote_control_enrollments(id TEXT,credential TEXT);
CREATE TABLE external_agent_config_imports(id TEXT,credential TEXT);
CREATE TABLE project_idempotency_keys(id TEXT,credential TEXT);
CREATE TABLE rollout_migration_state(id TEXT,credential TEXT);
CREATE TABLE rollout_migration_skipped_rollouts(id TEXT,credential TEXT);
CREATE INDEX thread_cwd ON threads(cwd);
"""
HISTORY_SQL = """
CREATE TABLE _sqlx_migrations(version INTEGER PRIMARY KEY,success INTEGER);
CREATE TABLE thread_turns(thread_id TEXT,turn_id TEXT,rollout_ordinal INTEGER,
 rollout_end_ordinal INTEGER,rollout_byte_offset INTEGER,rollout_end_byte_offset INTEGER,status TEXT);
CREATE TABLE thread_items(thread_id TEXT,turn_id TEXT,item_id TEXT,rollout_ordinal INTEGER,item_json TEXT,item_type TEXT DEFAULT 'userMessage');
CREATE TABLE thread_realtime_items(thread_id TEXT,item_id TEXT,rollout_ordinal INTEGER,item_json TEXT);
CREATE TABLE thread_history_projection_state(thread_id TEXT PRIMARY KEY,next_rollout_byte_offset INTEGER,
 next_rollout_ordinal INTEGER);
CREATE INDEX thread_items_page ON thread_items(thread_id,rollout_ordinal);
"""


class NativeHistoryTransferTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.home, self.payload, self.target = self.root / "source", self.root / "payload", self.root / "target"
        self.home.mkdir()
        self.original_bytes = {}
        self.schema = {}
        for name, script in ((native.STATE, STATE_SQL), (native.HISTORY, HISTORY_SQL)):
            with sqlite3.connect(self.home / name) as connection:
                connection.executescript(script)
                connection.execute("INSERT INTO _sqlx_migrations VALUES (1,1)")
                self.schema[name] = native._schema(connection)
        self.addCleanup(patch.stopall)
        patch.object(native, "SUPPORTED_SCHEMAS", self.schema).start()
        patch.object(native, "MIGRATIONS", {native.STATE: 1, native.HISTORY: 1}).start()
        with sqlite3.connect(self.home / native.STATE) as state, sqlite3.connect(self.home / native.HISTORY) as history:
            for thread_id in ("root", "child", "grandchild", "unrelated"):
                relative = Path("sessions/2026/01/01") / ("rollout-" + thread_id + ".jsonl")
                rollout = self.home / relative
                rollout.parent.mkdir(parents=True, exist_ok=True)
                data = b"".join(json.dumps(value, ensure_ascii=False).encode() + b"\n" for value in (
                    {"type": "session_meta", "payload": {"id": thread_id, "cwd": "/home/example/work"}},
                    {"type": "response_item", "payload": {"text": "Historical /home/example/work \u062d\u0627\u0641\u0638\u0647 " + thread_id}},
                    {"type": "compacted", "payload": {"summary": "preserved context " + thread_id}},
                ))
                rollout.write_bytes(data)
                self.original_bytes[thread_id] = data
                state.execute("INSERT INTO threads VALUES (?,?,?,?,?,?,?,?)", (
                    thread_id, str(rollout), "/home/example/work", '{"type":"workspace-write","writable_roots":["/home/example/work"]}',
                    "paginated", "section" if thread_id != "unrelated" else "private-section", "project" if thread_id != "unrelated" else "private-project", "Historical /home/example title"))
                state.execute("INSERT INTO thread_dynamic_tools VALUES (?,0,?)", (thread_id, "tool-" + thread_id))
                history.execute("INSERT INTO thread_turns VALUES (?,?,?,?,?,?,?)", (thread_id, "turn", 1, 2, data.index(b"\n") + 1, len(data), "completed"))
                history.execute("INSERT INTO thread_items(thread_id,turn_id,item_id,rollout_ordinal,item_json) VALUES (?,?,?,?,?)", (thread_id, "turn", "item", 1, json.dumps({"text": "message " + thread_id})))
                history.execute("INSERT INTO thread_realtime_items VALUES (?,?,?,?)", (thread_id, "realtime", 2, "{}"))
                history.execute("INSERT INTO thread_history_projection_state VALUES (?,?,?)", (thread_id, len(data), 3))
            state.executemany("INSERT INTO thread_spawn_edges VALUES (?,?,'completed')", [("root", "child"), ("child", "grandchild")])
            state.executemany("INSERT INTO thread_sections VALUES (?,?)", [("section", "Selected"), ("private-section", "DO-NOT-EXPORT")])
            state.executemany("INSERT INTO projects VALUES (?,?)", [("project", "Selected"), ("private-project", "DO-NOT-EXPORT")])
            state.executemany("INSERT INTO project_roots VALUES (?,0,?)", [("project", "/home/example/work"), ("private-project", "/private/DO-NOT-EXPORT")])
            for table in native.EMPTY_STATE_TABLES:
                state.execute("INSERT INTO " + table + " VALUES (1,'DO-NOT-EXPORT-CREDENTIAL')")
        (self.home / "auth.json").write_text("DO-NOT-EXPORT-CREDENTIAL")
        (self.home / "config.toml").write_text("DO-NOT-EXPORT-CREDENTIAL")

    def export(self):
        return native.export_native(self.home, self.payload, ["root"])

    def rehash_database(self, metadata, name):
        metadata["files"] = [native._file_record(self.payload / name, Path(name)) if row["path"] == name else row for row in metadata["files"]]

    def test_selects_complete_descendant_graph_and_excludes_global_state(self):
        before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in self.home.rglob("*") if path.is_file()}
        metadata = self.export()
        self.assertEqual(metadata["thread_ids"], ["child", "grandchild", "root"])
        self.assertEqual(metadata["counts"], {"threads": 3, "spawn_edges": 2, "turns": 3, "items": 3})
        self.assertEqual(native.validate_native(self.payload, metadata), metadata["counts"])
        for row in metadata["rollouts"]:
            self.assertEqual((self.payload / row["path"]).read_bytes(), self.original_bytes[row["thread_id"]])
        for path in self.payload.rglob("*"):
            if path.is_file():
                self.assertNotIn(b"DO-NOT-EXPORT", path.read_bytes())
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(before, {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in before})
        with sqlite3.connect(self.payload / native.STATE) as state:
            self.assertEqual(state.execute("SELECT id FROM projects").fetchall(), [("project",)])
            self.assertEqual(state.execute("SELECT id FROM thread_sections").fetchall(), [("section",)])
            self.assertEqual(state.execute("SELECT count(*) FROM thread_dynamic_tools").fetchone(), (3,))

    def test_import_remaps_filesystem_metadata_only_and_preserves_bytes(self):
        metadata = self.export()
        self.target.mkdir()
        (self.target / "auth.json").write_text("destination-login")
        result = native.import_native(self.payload, self.target, metadata, "/home/example", "/home/user")
        self.assertEqual(result["thread_ids"], metadata["thread_ids"])
        self.assertEqual((self.target / "auth.json").read_text(), "destination-login")
        for row in metadata["rollouts"]:
            self.assertEqual((self.target / row["path"]).read_bytes(), self.original_bytes[row["thread_id"]])
        with sqlite3.connect(self.target / native.STATE) as state:
            for rollout, cwd, sandbox, title in state.execute("SELECT rollout_path,cwd,sandbox_policy,title FROM threads"):
                self.assertTrue(Path(rollout).is_relative_to(self.target))
                self.assertEqual(cwd, "/home/user/work")
                self.assertEqual(json.loads(sandbox)["writable_roots"], ["/home/user/work"])
                self.assertEqual(title, "Historical /home/example title")
            self.assertEqual(state.execute("SELECT path FROM project_roots").fetchone(), ("/home/user/work",))

    def test_concurrent_append_is_bounded_by_snapshot_projection(self):
        root_file = next(self.home.rglob("rollout-root.jsonl"))
        with root_file.open("ab") as outgoing:
            outgoing.write(b'{"type":"not-yet-projected"}\npartial')
        metadata = self.export()
        record = next(row for row in metadata["rollouts"] if row["thread_id"] == "root")
        self.assertEqual((self.payload / record["path"]).read_bytes(), self.original_bytes["root"])

    def test_rejects_mismatched_rollout_ordinal_and_cleans_new_payload(self):
        with sqlite3.connect(self.home / native.HISTORY) as history:
            history.execute("UPDATE thread_history_projection_state SET next_rollout_ordinal=99 WHERE thread_id='root'")
        with self.assertRaisesRegex(ValueError, "projections do not agree"):
            self.export()
        self.assertFalse(self.payload.exists())

    def test_rejects_truncated_rollout_and_missing_descendant(self):
        path = next(self.home.rglob("rollout-root.jsonl"))
        path.write_bytes(self.original_bytes["root"][:-3])
        with self.assertRaisesRegex(ValueError, "shorter"):
            self.export()
        path.write_bytes(self.original_bytes["root"])
        with sqlite3.connect(self.home / native.STATE) as state:
            state.execute("INSERT INTO thread_spawn_edges VALUES ('root','missing','completed')")
        with self.assertRaisesRegex(ValueError, "descendant is missing"):
            self.export()

    def test_new_schema_fails_closed(self):
        with sqlite3.connect(self.home / native.STATE) as state:
            state.execute("CREATE TABLE new_private_state(secret TEXT)")
        with self.assertRaisesRegex(ValueError, "Unsupported native Codex schema"):
            self.export()
        self.assertFalse(self.payload.exists())

    def test_symlink_rollout_and_outside_path_are_rejected(self):
        path = next(self.home.rglob("rollout-root.jsonl"))
        outside = self.root / "outside.jsonl"
        outside.write_bytes(path.read_bytes())
        path.unlink()
        path.symlink_to(outside)
        with self.assertRaisesRegex(ValueError, "symbolic links"):
            self.export()
        path.unlink()
        with sqlite3.connect(self.home / native.STATE) as state:
            state.execute("UPDATE threads SET rollout_path=? WHERE id='root'", (str(outside),))
        with self.assertRaisesRegex(ValueError, "outside"):
            self.export()

    def test_manifest_tamper_is_rejected_before_installation(self):
        metadata = self.export()
        path = self.payload / metadata["rollouts"][0]["path"]
        path.write_bytes(path.read_bytes() + b"tampered")
        with self.assertRaisesRegex(ValueError, "checksum"):
            native.import_native(self.payload, self.target, metadata, "/home/example", "/home/user")
        self.assertFalse(self.target.exists())

    def test_unexpected_files_and_traversal_are_rejected(self):
        metadata = self.export()
        (self.payload / "auth.json").write_text("never install")
        with self.assertRaisesRegex(ValueError, "unexpected"):
            native.validate_native(self.payload, metadata)
        (self.payload / "auth.json").unlink()
        metadata["files"][0]["path"] = "../escape"
        with self.assertRaisesRegex(ValueError, "relative path"):
            native.validate_native(self.payload, metadata)

    def test_rejects_unselected_history_even_with_rehashed_manifest(self):
        metadata = self.export()
        with sqlite3.connect(self.payload / native.HISTORY) as history:
            history.execute("INSERT INTO thread_items VALUES ('unrelated','turn','item',1,'{}','userMessage')")
        self.rehash_database(metadata, native.HISTORY)
        with self.assertRaisesRegex(ValueError, "unselected history"):
            native.validate_native(self.payload, metadata)

    def test_rejects_existing_native_history_without_modifying_it(self):
        metadata = self.export()
        self.target.mkdir()
        existing = self.target / native.STATE
        existing.write_bytes(b"existing destination history")
        before = existing.read_bytes()
        with self.assertRaisesRegex(ValueError, "already has history"):
            native.import_native(self.payload, self.target, metadata, "/home/example", "/home/user")
        self.assertEqual(existing.read_bytes(), before)
        self.assertEqual(list(self.target.iterdir()), [existing])

    def test_projection_beyond_rollout_is_rejected_even_after_rehash(self):
        metadata = self.export()
        with sqlite3.connect(self.payload / native.HISTORY) as history:
            history.execute("UPDATE thread_turns SET rollout_end_byte_offset=999999")
        self.rehash_database(metadata, native.HISTORY)
        with self.assertRaisesRegex(ValueError, "extends beyond"):
            native.validate_native(self.payload, metadata)

    def test_only_referenced_images_are_copied_and_structured_paths_remapped(self):
        uploads = self.home / "uploads"
        uploads.mkdir()
        image = uploads / "selected.png"
        image.write_bytes(b"selected-image")
        other = uploads / "omitted.png"
        other.write_bytes(b"DO-NOT-EXPORT-IMAGE")
        with sqlite3.connect(self.home / native.HISTORY) as history:
            for thread_id, path in (("root", image), ("unrelated", other)):
                value = {"type": "userMessage", "content": [{"type": "localImage", "path": str(path)}, {"type": "text", "text": str(path)}]}
                history.execute("UPDATE thread_items SET item_json=? WHERE thread_id=?", (json.dumps(value), thread_id))
        metadata = self.export()
        self.assertEqual(len(metadata["assets"]), 1)
        self.assertEqual((self.payload / "uploads/selected.png").read_bytes(), b"selected-image")
        self.assertFalse((self.payload / "uploads/omitted.png").exists())
        result = native.import_native(self.payload, self.target, metadata, "/home/example", "/home/user")
        self.assertIn(str(self.target / "uploads"), result["created_paths"])
        with sqlite3.connect(self.target / native.HISTORY) as history:
            value = json.loads(history.execute("SELECT item_json FROM thread_items WHERE thread_id='root'").fetchone()[0])
        self.assertEqual(value["content"][0]["path"], str(self.target / "uploads/selected.png"))
        self.assertEqual(value["content"][1]["text"], str(image))
        for row in metadata["rollouts"]:
            self.assertEqual((self.target / row["path"]).read_bytes(), self.original_bytes[row["thread_id"]])

    def test_empty_initialized_store_paths_can_be_moved_without_touching_login(self):
        self.target.mkdir()
        for name, sql in ((native.STATE, STATE_SQL), (native.HISTORY, HISTORY_SQL)):
            with sqlite3.connect(self.target / name) as connection:
                connection.executescript(sql)
                connection.execute("INSERT INTO _sqlx_migrations VALUES (1,1)")
        (self.target / "uploads").mkdir()
        (self.target / "sessions").mkdir()
        (self.target / "auth.json").write_text("preserve-login")
        paths = native.empty_store_paths(self.target)
        self.assertEqual({path.name for path in paths}, {native.STATE, native.HISTORY, "uploads", "sessions"})
        with sqlite3.connect(self.target / native.HISTORY) as history:
            history.execute("INSERT INTO thread_history_projection_state VALUES ('existing',1,1)")
        with self.assertRaisesRegex(ValueError, "already contains native history"):
            native.empty_store_paths(self.target)
        self.assertEqual((self.target / "auth.json").read_text(), "preserve-login")

    def test_unknown_native_artifacts_fail_closed(self):
        with sqlite3.connect(self.home / native.STATE) as state:
            state.execute("INSERT INTO thread_attachments VALUES ('artifact','root','{}')")
        with self.assertRaisesRegex(ValueError, "artifacts are not supported"):
            self.export()

    def test_legacy_image_root_requires_explicit_allowance(self):
        uploads = self.root / "legacy/uploads"
        uploads.mkdir(parents=True)
        image = uploads / "old.png"
        image.write_bytes(b"legacy-image")
        value = {"type": "userMessage", "content": [{"type": "localImage", "path": str(image)}]}
        with sqlite3.connect(self.home / native.HISTORY) as history:
            history.execute("UPDATE thread_items SET item_json=? WHERE thread_id='root'", (json.dumps(value),))
        with self.assertRaisesRegex(ValueError, "explicitly allowed legacy"):
            self.export()
        metadata = native.export_native(self.home, self.payload, ["root"], asset_roots=[uploads])
        self.assertTrue(metadata["assets"][0]["path"].startswith("uploads/legacy/"))
        self.assertEqual((self.payload / metadata["assets"][0]["path"]).read_bytes(), b"legacy-image")

    def test_additional_physical_records_do_not_renumber_native_ordinals(self):
        # Existing Codex histories can contain records the native parser skipped.
        path = next(self.home.rglob("rollout-root.jsonl"))
        path.write_bytes(self.original_bytes["root"] + b'{"type":"historical_unknown_record"}\n')
        with sqlite3.connect(self.home / native.HISTORY) as history:
            history.execute("UPDATE thread_history_projection_state SET next_rollout_byte_offset=? WHERE thread_id='root'", (path.stat().st_size,))
        metadata = self.export()
        rollout = next(row for row in metadata["rollouts"] if row["thread_id"] == "root")
        self.assertEqual(rollout["projection_ordinal"], 3)
        self.assertEqual((self.payload / rollout["path"]).read_bytes(), path.read_bytes())


if __name__ == "__main__":
    unittest.main()
