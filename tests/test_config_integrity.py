"""Synthetic-only tests; temporary material stays under this project's work/."""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from unittest import mock

from tools import config_integrity as integrity


class ConfigIntegrityTests(unittest.TestCase):
    def setUp(self):
        scratch = Path(__file__).resolve().parents[1] / "work" / "test-config-integrity"
        scratch.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    @contextlib.contextmanager
    def connection(self, path):
        database = sqlite3.connect(path)
        try:
            with database:
                yield database
        finally:
            database.close()

    def database(self, name="config.sqlite", *, rows=None, sql=None):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection(path) as db:
            db.execute(sql or "CREATE TABLE channels(id INTEGER PRIMARY KEY, name TEXT, "
                       "sort_order INTEGER, enabled INTEGER, category TEXT, payload BLOB, optional TEXT)")
            if rows is None and sql is None:
                rows = [(1, "synthetic-private-channel", 20, 1, "category-a", b"\x00\xff", None),
                        (2, "second-private-value", 10, 0, "category-b", b"", "token-test-only")]
            if rows:
                db.executemany("INSERT INTO channels VALUES(?,?,?,?,?,?,?)", rows)
        return path

    def assert_code(self, code, function, *args):
        with self.assertRaises(integrity.IntegrityError) as caught:
            function(*args)
        self.assertEqual(caught.exception.code, code)

    def test_directory_inventory_is_complete_sorted_and_value_redacted(self):
        (self.root / "nested" / "empty").mkdir(parents=True)
        content = b"SECRET-CONFIG-CONTENTS"
        (self.root / "nested" / "settings.json").write_bytes(content)
        (self.root / "a").write_bytes(b"")
        result = integrity.snapshot_directory(self.root)
        self.assertEqual([item["path"] for item in result["files"]], ["a", "nested/settings.json"])
        self.assertEqual(result["files"][1]["sha256"], hashlib.sha256(content).hexdigest())
        self.assertEqual(result["files"][1]["size"], len(content))
        self.assertEqual(result["directories"], ["nested", "nested/empty"])
        self.assertNotIn(content.decode(), json.dumps(result))
        self.assertFalse(result["backup_consistency_verified"])
        self.assertFalse(result["cross_file_atomicity_verified"])

    def test_directory_compare_catches_missing_changed_and_empty_directories(self):
        left, right = self.root / "left", self.root / "right"
        left.mkdir(); right.mkdir()
        (left / "same").write_bytes(b"same"); (right / "same").write_bytes(b"same")
        self.assertTrue(integrity.compare_directories(left, right)["equal"])
        (left / "only-left").write_bytes(b"one")
        (right / "only-right").write_bytes(b"two")
        (right / "same").write_bytes(b"changed")
        (left / "empty").mkdir()
        result = integrity.compare_directories(left, right)
        self.assertFalse(result["equal"])
        self.assertEqual(result["left_only"], ["only-left"])
        self.assertEqual(result["right_only"], ["only-right"])
        self.assertEqual(result["changed_files"], ["same"])
        self.assertFalse(result["directory_structure_equal"])

    def test_identical_databases_include_every_table_without_values(self):
        first = self.database("left.sqlite")
        with self.connection(first) as db:
            db.execute("CREATE TABLE settings(key TEXT PRIMARY KEY, value BLOB)")
            db.execute("INSERT INTO settings VALUES(?,?)", ("password", b"NOT-A-REAL-PASSWORD"))
        second = self.root / "right.sqlite"
        shutil.copyfile(first, second)
        result = integrity.compare_databases(first, second)
        self.assertTrue(result["equal"])
        self.assertEqual([item["name"] for item in result["tables"]], ["channels", "settings"])
        serialized = json.dumps(integrity.snapshot_database(first)) + json.dumps(result)
        for secret in ("synthetic-private-channel", "second-private-value", "token-test-only", "NOT-A-REAL-PASSWORD"):
            self.assertNotIn(secret, serialized)

    def test_channel_order_enabled_category_and_blob_changes_are_detected(self):
        first = self.database("left.sqlite")
        for column, value in (("sort_order", 21), ("enabled", 0), ("category", "category-new"),
                              ("name", "renamed-private-channel"), ("payload", b"different"),
                              ("optional", "changed-from-null")):
            with self.subTest(column=column):
                second = self.root / "right.sqlite"
                shutil.copyfile(first, second)
                with self.connection(second) as db:
                    db.execute(f"UPDATE channels SET {column}=? WHERE id=1", (value,))
                result = integrity.compare_databases(first, second)
                self.assertFalse(result["equal"])
                self.assertTrue(result["schema_equal"])
                self.assertFalse(result["tables"][0]["content_equal"])

    def test_row_order_is_normalized_with_stable_primary_keys(self):
        rows = [(1, "one", 1, 1, "a", b"b", None), (2, "two", 2, 0, "b", b"c", "x")]
        first = self.database("left.sqlite", rows=rows)
        second = self.database("right.sqlite", rows=list(reversed(rows)))
        self.assertTrue(integrity.compare_databases(first, second)["equal"])

    def test_storage_types_are_distinct_in_untyped_columns(self):
        first = self.database("left.sqlite", sql="CREATE TABLE channels(value)")
        second = self.database("right.sqlite", sql="CREATE TABLE channels(value)")
        for initial, replacement in ((1, 1.0), (1, "1"), ("1", b"1"), (None, ""), (b"", None)):
            with self.subTest(initial=initial, replacement=replacement):
                for path, value in ((first, initial), (second, replacement)):
                    with self.connection(path) as db:
                        db.execute("DELETE FROM channels")
                        db.execute("INSERT INTO channels VALUES(?)", (value,))
                self.assertFalse(integrity.compare_databases(first, second)["equal"])

    def test_implicit_rowid_is_checked_for_channel_insertion_identity(self):
        first = self.database("left.sqlite", sql="CREATE TABLE channels(value TEXT)")
        second = self.database("right.sqlite", sql="CREATE TABLE channels(value TEXT)")
        for path, rowid in ((first, 1), (second, 2)):
            with self.connection(path) as db:
                db.execute("INSERT INTO channels(rowid,value) VALUES(?,?)", (rowid, "same"))
        self.assertFalse(integrity.compare_databases(first, second)["equal"])
        self.assertTrue(integrity.snapshot_database(first)["tables"][0]["rowid_included"])

    def test_without_rowid_and_quoted_identifiers(self):
        path = self.database(sql='CREATE TABLE "quoted""table"("odd""column" TEXT PRIMARY KEY) WITHOUT ROWID')
        with self.connection(path) as db:
            db.execute('INSERT INTO "quoted""table" VALUES(?)', ("secret-value",))
        table = integrity.snapshot_database(path)["tables"][0]
        self.assertEqual(table["name"], 'quoted"table')
        self.assertEqual(table["row_count"], 1)
        self.assertFalse(table["rowid_included"])

    def test_schema_defaults_triggers_views_and_metadata_changes_are_detected(self):
        first = self.database("left.sqlite")
        for change in ("ALTER TABLE channels ADD COLUMN additional TEXT DEFAULT 'secret-default'",
                       "CREATE VIEW channel_view AS SELECT name FROM channels",
                       "CREATE TRIGGER channel_trigger AFTER INSERT ON channels BEGIN SELECT 1; END",
                       "PRAGMA user_version=5", "PRAGMA application_id=123"):
            with self.subTest(change=change):
                second = self.root / "right.sqlite"
                shutil.copyfile(first, second)
                with self.connection(second) as db:
                    db.execute(change)
                result = integrity.compare_databases(first, second)
                self.assertFalse(result["equal"])
                self.assertNotIn("secret-default", json.dumps(result))

    def test_table_addition_and_removal_are_visible(self):
        first = self.database("left.sqlite")
        second = self.root / "right.sqlite"
        shutil.copyfile(first, second)
        with self.connection(second) as db:
            db.execute("CREATE TABLE added(value TEXT)")
        result = integrity.compare_databases(first, second)
        added = next(item for item in result["tables"] if item["name"] == "added")
        self.assertFalse(result["equal"])
        self.assertIsNone(added["left_row_count"])
        self.assertEqual(added["right_row_count"], 0)

    def test_generated_columns_and_internal_sequence_are_included(self):
        path = self.database(sql="CREATE TABLE channels(id INTEGER PRIMARY KEY AUTOINCREMENT, "
                             "value INTEGER, doubled INTEGER GENERATED ALWAYS AS (value*2))")
        with self.connection(path) as db:
            db.execute("INSERT INTO channels(value) VALUES(5)")
        tables = {item["name"]: item for item in integrity.snapshot_database(path)["tables"]}
        self.assertEqual(tables["channels"]["column_count"], 3)
        self.assertEqual(tables["sqlite_sequence"]["row_count"], 1)

    def test_invalid_utf8_text_and_blob_are_supported_without_disclosure(self):
        path = self.database(sql="CREATE TABLE channels(value)")
        with self.connection(path) as db:
            db.execute("INSERT INTO channels VALUES(CAST(x'80ff' AS TEXT))")
            db.execute("INSERT INTO channels VALUES(x'80ff')")
        self.assertEqual(integrity.snapshot_database(path)["tables"][0]["row_count"], 2)

    def test_mode_ro_immutable_does_not_create_wal_or_shm_for_closed_wal_database(self):
        path = self.database("config#closed.sqlite")
        with self.connection(path) as db:
            self.assertEqual(db.execute("PRAGMA journal_mode=WAL").fetchone()[0], "wal")
        db.close()
        before = {item.name: item.read_bytes() for item in self.root.iterdir()}
        result = integrity.snapshot_database(path)
        after = {item.name: item.read_bytes() for item in self.root.iterdir()}
        self.assertEqual(before, after)
        self.assertTrue(result["readonly"])
        self.assertTrue(result["immutable"])
        self.assertEqual(result["tables"][0]["row_count"], 2)

    def test_active_wal_is_rejected_instead_of_silently_reading_stale_main_database(self):
        path = self.database()
        writer = sqlite3.connect(path)
        self.addCleanup(writer.close)
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("UPDATE channels SET enabled=0 WHERE id=1"); writer.commit()
        self.assertTrue(path.with_name(path.name + "-wal").exists())
        self.assert_code("sqlite_journal_present_use_consistent_backup", integrity.snapshot_database, path)
        self.assert_code("sqlite_journal_present_use_consistent_backup", integrity.snapshot_directory, self.root)

    def test_even_empty_journals_are_rejected(self):
        path = self.database()
        for suffix in ("-wal", "-journal"):
            journal = path.with_name(path.name + suffix)
            journal.touch()
            self.assert_code("sqlite_journal_present_use_consistent_backup", integrity.snapshot_database, path)
            journal.unlink()

    def test_sqlite_backup_output_is_supported(self):
        source = self.database("live/source.sqlite")
        writer = sqlite3.connect(source)
        self.addCleanup(writer.close)
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("UPDATE channels SET enabled=0 WHERE id=1"); writer.commit()
        destination = self.root / "backup.sqlite"
        copy = sqlite3.connect(destination)
        writer.backup(copy)
        copy.close()
        result = integrity.snapshot_database(destination)
        self.assertEqual(result["tables"][0]["row_count"], 2)
        self.assertFalse(result["backup_consistency_verified"])

    def test_directory_compares_logical_database_changes_as_well_as_physical_files(self):
        first = self.database("left/data.sqlite")
        second = self.database("right/data.sqlite")
        left, right = first.parent, second.parent
        self.assertTrue(integrity.compare_directories(left, right)["equal"])
        with self.connection(second) as db:
            db.execute("UPDATE channels SET sort_order=100 WHERE id=1")
        result = integrity.compare_directories(left, right)
        self.assertFalse(result["files_equal"])
        self.assertFalse(result["databases_equal"])
        self.assertEqual(result["databases"][0]["path"], "data.sqlite")
        self.assertEqual(result["changed_files"], ["data.sqlite"])

    def test_parent_traversal_and_symlinks_are_rejected(self):
        self.assert_code("parent_traversal_rejected", integrity.snapshot_directory,
                         self.root / "anything" / "..")
        target = self.root / "target"
        target.mkdir()
        link = self.root / "link"
        try:
            link.symlink_to(target, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("Platform does not permit creating a symlink")
        self.assert_code("symlink_or_reparse_point_rejected", integrity.snapshot_directory, link)
        self.assert_code("symlink_or_reparse_point_rejected", integrity.snapshot_directory, self.root)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "POSIX FIFO required")
    def test_special_files_are_rejected(self):
        os.mkfifo(self.root / "special")
        self.assert_code("special_file_rejected", integrity.snapshot_directory, self.root)

    def test_changes_during_directory_scan_are_rejected(self):
        path = self.root / "settings.json"
        path.write_bytes(b"before")
        original = integrity._file_record

        def change_after_hash(*args):
            result = original(*args)
            path.write_bytes(b"after")
            return result

        with mock.patch.object(integrity, "_file_record", side_effect=change_after_hash):
            self.assert_code("input_changed_during_read", integrity.snapshot_directory, self.root)

    def test_cli_json_exit_codes_and_fixed_error_messages(self):
        first = self.database("left.sqlite")
        second = self.database("right.sqlite")

        def invoke(*arguments):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                status = integrity.main(list(arguments))
            return status, json.loads(output.getvalue())

        arguments = ("compare", str(first), str(second), "--offline-snapshot")
        status, result = invoke(*arguments)
        self.assertEqual(status, 0); self.assertTrue(result["equal"])
        with self.connection(second) as db:
            db.execute("DELETE FROM channels WHERE id=1")
        status, result = invoke(*arguments)
        self.assertEqual(status, 1); self.assertFalse(result["equal"])
        second.write_bytes(b"SECRET-CORRUPT-CONFIG")
        status, result = invoke(*arguments)
        self.assertEqual(status, 2)
        self.assertEqual(result, {"ok": False, "error": "not_sqlite_database"})

    def test_empty_database_and_corruption_have_safe_results(self):
        empty = self.root / "empty.sqlite"
        empty.touch()
        self.assertEqual(integrity.snapshot_database(empty)["tables"], [])
        corrupt = self.root / "bad.sqlite"
        corrupt.write_bytes(integrity.SQLITE_HEADER + b"SECRET-CORRUPT-DATA" * 30)
        self.assert_code("sqlite_read_failed", integrity.snapshot_database, corrupt)


if __name__ == "__main__":
    unittest.main()
