"""CODEX: Exercise publication barriers and private diagnostic redaction with synthetic state."""

import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from tools import check_publication as publication
from tools import private_snapshot


class PublicationTests(unittest.TestCase):
    def test_unknown_secret_in_test_file_is_not_covered_by_fixture_exception(self):
        value = b"sk-" + b"Ab91Cd72Ef63Gh54Ij45Kl36"
        findings = publication.inspect_bytes("tests/test_lan_prepared_context.py", value)
        self.assertEqual([item.category for item in findings], ["provider-secret"])
        self.assertNotIn(value.decode(), json.dumps([publication.asdict(item) for item in findings]))

    def test_private_key_and_json_credentials_report_only_location(self):
        key = b"-----BEGIN " + b"PRIVATE KEY-----\nsynthetic key\n"
        credential = b'{"access_token": "' + b"synthetic-session-credential" + b'"}'
        categories = {item.category for item in publication.inspect_bytes("settings.py", key + credential)}
        self.assertEqual(categories, {"private-key", "literal-credential"})
        self.assertEqual(publication.inspect_bytes("ui.js", b'visible ? "Hide password" : "Show password";'), [])

    def test_every_private_filename_is_refused_and_example_env_is_allowed(self):
        for path in (
            "private-reports/report.html", "state/auth.json", "codex-pairing-0.3.13.json",
            "Install-Codex-Connector-0.3.13-private.sh", ".env", "server.sqlite3-wal",
            ".local/share/state.txt", "pip.pyz",
        ):
            with self.subTest(path=path):
                self.assertTrue(publication.private_path(path))
        self.assertFalse(publication.private_path(".env.example"))

    def git(self, root, *args):
        environment = {
            **os.environ, "GIT_AUTHOR_NAME": "Example", "GIT_COMMITTER_NAME": "Example",
            "GIT_AUTHOR_EMAIL": "example@users.noreply.github.com",
            "GIT_COMMITTER_EMAIL": "example@users.noreply.github.com",
        }
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, env=environment)

    def test_deleted_secrets_are_found_in_history_and_cli_does_not_echo_them(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.git(root, "init", "-q")
            value = "sk-" + "Ab91Cd72Ef63Gh54Ij45Kl36"
            (root / "auth.json").write_text(json.dumps({"access_token": value}))
            self.git(root, "add", "auth.json")
            self.git(root, "commit", "-qm", "Synthetic private fixture")
            (root / "auth.json").unlink()
            (root / "safe.py").write_text("print('public')\n")
            self.git(root, "add", "-A")
            self.git(root, "commit", "-qm", "Public source " + value)
            self.assertEqual(publication.inspect_worktree(root, publication.source_files(root)), [])
            findings, blobs = publication.inspect_history(root)
            self.assertGreaterEqual(blobs, 2)
            self.assertIn("provider-secret", {item.category for item in findings})
            self.assertIn("private-runtime-file", {item.category for item in findings})
            self.assertTrue(any(item.path == "[commit metadata]" and item.category == "provider-secret" for item in findings))
            result = subprocess.run(
                [sys.executable, str(Path(publication.__file__)), "--repo", str(root), "--history"],
                capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 1)
            self.assertNotIn(value, result.stdout + result.stderr)

    def test_export_copies_new_source_without_history_or_ignored_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "source"
            root.mkdir()
            self.git(root, "init", "-q")
            (root / ".gitignore").write_text("private-reports/\n.env\n")
            (root / "source.py").write_text("print('portable')\n")
            (root / ".env").write_text("SYNTHETIC_PRIVATE=1\n")
            (root / "private-reports").mkdir()
            (root / "private-reports/report.txt").write_text("private aggregate")
            paths = publication.source_files(root)
            self.assertEqual(publication.inspect_worktree(root, paths), [])
            destination = Path(directory) / "public"
            publication.export_source(root, paths, destination)
            self.assertTrue((destination / "source.py").is_file())
            self.assertFalse((destination / ".git").exists())
            self.assertFalse((destination / ".env").exists())
            self.assertFalse((destination / "private-reports").exists())
            with self.assertRaises(RuntimeError):
                publication.export_source(root, paths, destination)


class PrivateSnapshotTests(unittest.TestCase):
    def test_snapshot_is_read_only_aggregate_without_any_user_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "synthetic.sqlite3"
            secret_marker = "SYNTHETIC-PRIVATE-PERSON-CONTENT"
            connection = sqlite3.connect(database)
            connection.executescript(
                "CREATE TABLE devices(id TEXT,name TEXT,status TEXT,token_hash TEXT);"
                "CREATE TABLE rooms(id TEXT,title TEXT,cwd TEXT);"
                "CREATE TABLE runs(id TEXT,status TEXT);"
                "CREATE TABLE messages(content TEXT);"
                "CREATE TABLE attachments(name TEXT,path TEXT,size INTEGER);"
                "CREATE TABLE usage_ledger(input_tokens INTEGER,cached_tokens INTEGER,"
                "output_tokens INTEGER,total_tokens INTEGER);"
                "CREATE TABLE quota_usage(credits_micros INTEGER);"
            )
            connection.execute("INSERT INTO devices VALUES(?,?,?,?)", (secret_marker, secret_marker, "approved", secret_marker))
            connection.execute("INSERT INTO rooms VALUES(?,?,?)", (secret_marker, secret_marker, secret_marker))
            connection.execute("INSERT INTO messages VALUES(?)", (secret_marker,))
            connection.execute("INSERT INTO runs VALUES(?,?)", (secret_marker, "completed"))
            connection.execute("INSERT INTO attachments VALUES(?,?,?)", (secret_marker, secret_marker, 200))
            connection.execute("INSERT INTO usage_ledger VALUES(100,30,20,120)")
            connection.execute("INSERT INTO quota_usage VALUES(1250000)")
            connection.commit()
            connection.close()
            fingerprint = hashlib.sha256(database.read_bytes()).hexdigest()
            report = private_snapshot.collect(database)
            self.assertEqual(report["counts"]["messages"], 1)
            self.assertEqual(report["reported_usage"]["total_tokens"], 120)
            self.assertEqual(report["weighted_usage_units"], 1.25)
            self.assertEqual(hashlib.sha256(database.read_bytes()).hexdigest(), fingerprint)
            output = root / "private-reports"
            paths = private_snapshot.save(report, output, include_html=True)
            self.assertEqual(output.stat().st_mode & 0o777, 0o700)
            for path in paths:
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                self.assertNotIn(secret_marker, path.read_text())
                self.assertNotIn(str(database), path.read_text())

    def test_snapshot_does_not_create_a_missing_database(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "missing.sqlite3"
            with self.assertRaises(sqlite3.OperationalError):
                private_snapshot.collect(database)
            self.assertFalse(database.exists())


if __name__ == "__main__":
    unittest.main()
