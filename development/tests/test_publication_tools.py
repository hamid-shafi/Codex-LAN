"""CODEX: Exercise publication barriers and private diagnostic redaction with synthetic state."""

import hashlib
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import zipfile
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from project_layout import configure_imports

configure_imports(__file__)

from tools import check_publication as publication
from tools import private_snapshot


class PublicationTests(unittest.TestCase):
    def test_unknown_secret_in_test_file_is_not_covered_by_fixture_exception(self):
        value = b"sk-" + b"Ab91Cd72Ef63Gh54Ij45Kl36"
        for path in ("tests/test_lan_prepared_context.py",
                     "development/tests/test_lan_prepared_context.py"):
            with self.subTest(path=path):
                findings = publication.inspect_bytes(path, value)
                self.assertEqual([item.category for item in findings], ["provider-secret"])
                self.assertNotIn(value.decode(), json.dumps([publication.asdict(item) for item in findings]))

    def test_reviewed_fixture_stays_specific_to_its_value_and_moved_path(self):
        reviewed = b"hunter" + b"123"
        content = b'password="' + reviewed + b'"'
        for path in ("tests/test_lan_activity.py", "development/tests/test_lan_activity.py"):
            with self.subTest(path=path):
                self.assertEqual(publication.inspect_bytes(path, content), [])
                changed = b'password="' + reviewed + b'4"'
                self.assertEqual(
                    [item.category for item in publication.inspect_bytes(path, changed)],
                    ["literal-credential"],
                )
        self.assertEqual(
            [item.category for item in publication.inspect_bytes("development/tests/other.py", content)],
            ["literal-credential"],
        )

    def test_relocated_checker_defaults_to_the_entire_checkout(self):
        with mock.patch.object(sys, "argv", ["check_publication.py"]), \
                mock.patch.object(publication, "source_files", return_value=[]) as source_files, \
                redirect_stdout(io.StringIO()):
            self.assertEqual(publication.main(), 0)
        source_files.assert_called_once_with(ROOT)

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
            "support.zip", "snapshot.tar.gz", "history.gz", "export.7z",
        ):
            with self.subTest(path=path):
                self.assertTrue(publication.private_path(path))
        self.assertFalse(publication.private_path(".env.example"))

    def test_compressed_credentials_are_blocked_even_with_a_source_filename(self):
        archive = io.BytesIO()
        value = "-".join(("SYNTHETIC", "LOCAL", "CREDENTIAL"))
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
            bundle.writestr("auth.json", json.dumps({"access_token": value}))
        for prefix in (b"", b"# Synthetic archive note\n"):
            for name in ("support.zip", "support.txt"):
                with self.subTest(name=name, prefixed=bool(prefix)):
                    findings = publication.inspect_bytes(name, prefix + archive.getvalue())
                    self.assertIn("opaque-archive", {item.category for item in findings})
                    self.assertNotIn(value, json.dumps([publication.asdict(item) for item in findings]))

    def test_compressed_source_cannot_be_exported_or_hidden_in_history(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "source"
            root.mkdir()
            self.git(root, "init", "-q")
            archive = io.BytesIO()
            with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
                bundle.writestr("server.sqlite3", b"synthetic private database")
            (root / "notes.txt").write_bytes(b"# Synthetic archive note\n" + archive.getvalue())
            paths = publication.source_files(root)
            with self.assertRaises(RuntimeError):
                publication.export_source(root, paths, Path(directory) / "public")
            self.assertFalse((Path(directory) / "public").exists())
            self.git(root, "add", "notes.txt")
            self.git(root, "commit", "-qm", "Synthetic archive fixture")
            (root / "notes.txt").unlink()
            self.git(root, "add", "-u")
            self.git(root, "commit", "-qm", "Remove fixture")
            findings, _ = publication.inspect_history(root)
            self.assertIn("opaque-archive", {item.category for item in findings})

    def test_known_compressed_formats_are_blocked_but_bundled_fonts_are_not(self):
        for signature in publication.ARCHIVE_SIGNATURES:
            with self.subTest(signature=signature):
                findings = publication.inspect_bytes("payload.txt", signature + b"synthetic")
                self.assertIn("opaque-archive", {item.category for item in findings})
        for signature in (b"ustar\x00", b"ustar "):
            self.assertIn("opaque-archive", {item.category for item in publication.inspect_bytes(
                "payload.txt", b"\x00" * 257 + signature,
            )})
        self.assertEqual(publication.inspect_bytes("font.woff2", b"wOF2synthetic font"), [])

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
    def test_snapshot_refuses_every_product_area_before_any_filesystem_write(self):
        for output in (ROOT, ROOT / "apps", ROOT / "apps/workspace", ROOT / "setup"):
            with self.subTest(output=output), \
                    mock.patch.object(Path, "mkdir", side_effect=AssertionError("Unexpected filesystem write")), \
                    self.assertRaisesRegex(ValueError, "outside the project checkout"):
                private_snapshot.save({}, output)

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
