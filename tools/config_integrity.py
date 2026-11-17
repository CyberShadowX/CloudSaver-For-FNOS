"""Read-only, value-redacted comparison of complete offline configuration snapshots.

Inputs must be quiescent complete copies or outputs of SQLite's backup API. This
tool does not create backups or establish consistency across files/databases.
SQLite is opened with mode=ro and immutable=1 to avoid creating journal/SHM files;
any adjacent WAL or rollback journal is therefore rejected, never ignored.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import struct
import sys


SQLITE_HEADER = b"SQLite format 3\x00"
INPUT_REQUIREMENT = "quiescent_complete_copy_or_sqlite_backup_output"


class IntegrityError(Exception):
    """Only a fixed error code is exposed; underlying errors may contain values."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class _SQLiteText(bytes):
    """Preserve arbitrary SQLite TEXT bytes, distinguishing them from BLOBs."""


def _signature(info: os.stat_result) -> tuple:
    # Windows fstat and lstat can expose different legacy st_ctime meanings.
    # Birth time has one meaning on both; POSIX ctime remains a change detector.
    change_time = getattr(info, "st_birthtime_ns", None) if os.name == "nt" else info.st_ctime_ns
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size,
            info.st_mtime_ns, change_time)


def _reject_link(info: os.stat_result) -> None:
    if stat.S_ISLNK(info.st_mode) or (
        getattr(info, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    ):
        raise IntegrityError("symlink_or_reparse_point_rejected")


def _plain_path(path: str | Path, *, directory: bool) -> Path:
    supplied = Path(path)
    if ".." in supplied.parts:
        raise IntegrityError("parent_traversal_rejected")
    absolute = Path(os.path.abspath(supplied))
    try:
        for part in [*reversed(absolute.parents), absolute]:
            _reject_link(part.lstat())
        info = absolute.lstat()
    except OSError:
        raise IntegrityError("path_unavailable") from None
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected(info.st_mode):
        raise IntegrityError("unsupported_path_type")
    return absolute


def _entries(root: Path) -> dict[str, os.stat_result]:
    found: dict[str, os.stat_result] = {".": root.lstat()}

    def visit(folder: Path) -> None:
        with os.scandir(folder) as stream:
            children = sorted(stream, key=lambda entry: entry.name)
        for entry in children:
            path = folder / entry.name
            # Windows DirEntry.stat may omit st_dev/st_ino; use lstat so identity
            # checks agree with fstat while still refusing links and junctions.
            info = path.lstat()
            _reject_link(info)
            if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
                raise IntegrityError("special_file_rejected")
            relative = path.relative_to(root).as_posix()
            found[relative] = info
            if stat.S_ISDIR(info.st_mode):
                visit(path)

    try:
        visit(root)
    except OSError:
        raise IntegrityError("filesystem_read_failed") from None
    return found


def _file_record(path: Path, relative: str, before: os.stat_result) -> dict:
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    digest = hashlib.sha256()
    header = b""
    try:
        with os.fdopen(os.open(path, flags), "rb") as source:
            if _signature(os.fstat(source.fileno())) != _signature(before):
                raise IntegrityError("input_changed_during_read")
            while chunk := source.read(1024 * 1024):
                if not header:
                    header = chunk[:len(SQLITE_HEADER)]
                digest.update(chunk)
            if _signature(os.fstat(source.fileno())) != _signature(before):
                raise IntegrityError("input_changed_during_read")
        after = path.lstat()
        _reject_link(after)
        if _signature(after) != _signature(before):
            raise IntegrityError("input_changed_during_read")
    except OSError:
        raise IntegrityError("filesystem_read_failed") from None
    return {"path": relative, "size": before.st_size, "sha256": digest.hexdigest(),
            "sqlite_header": header == SQLITE_HEADER}


def _cell(value) -> bytes:
    if value is None:
        return b"N"
    if isinstance(value, _SQLiteText):
        tag, body = b"T", bytes(value)
    elif isinstance(value, bytes):
        tag, body = b"B", value
    elif isinstance(value, str):
        tag, body = b"T", value.encode("utf-8")
    elif isinstance(value, int):
        tag, body = b"I", str(value).encode("ascii")
    elif isinstance(value, float):
        tag, body = b"F", struct.pack(">d", value)
    else:
        raise IntegrityError("unsupported_sqlite_value_type")
    return tag + struct.pack(">Q", len(body)) + body


def _record(values) -> bytes:
    return b"R" + struct.pack(">Q", len(values)) + b"".join(_cell(value) for value in values)


def _rows_digest(rows) -> tuple[str, int]:
    hashes = sorted(hashlib.sha256(_record(row)).digest() for row in rows)
    digest = hashlib.sha256(b"cloudsaver-config-rows-v1\0")
    digest.update(struct.pack(">Q", len(hashes)))
    for row_hash in hashes:
        digest.update(row_hash)
    return digest.hexdigest(), len(hashes)


def _identifier(value: bytes) -> str:
    try:
        decoded = value.decode("utf-8")
    except UnicodeError:
        raise IntegrityError("unsupported_sqlite_identifier") from None
    if "\x00" in decoded:
        raise IntegrityError("unsupported_sqlite_identifier")
    return decoded


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _reject_journals(path: Path) -> None:
    for suffix in ("-wal", "-journal"):
        journal = path.with_name(path.name + suffix)
        # lexists also catches broken symlinks; even an empty journal is refused.
        if os.path.lexists(journal):
            raise IntegrityError("sqlite_journal_present_use_consistent_backup")


def snapshot_database(path: str | Path) -> dict:
    """Fingerprint an offline database without printing values or changing files."""
    database = _plain_path(path, directory=False)
    _reject_journals(database)
    before = database.lstat()
    try:
        with database.open("rb") as source:
            if before.st_size and source.read(len(SQLITE_HEADER)) != SQLITE_HEADER:
                raise IntegrityError("not_sqlite_database")
    except OSError:
        raise IntegrityError("filesystem_read_failed") from None
    connection = None
    try:
        connection = sqlite3.connect(database.as_uri() + "?mode=ro&immutable=1", uri=True,
                                     isolation_level=None)
        connection.text_factory = _SQLiteText
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA trusted_schema=OFF")
        connection.execute("BEGIN")
        objects = connection.execute(
            "SELECT type,name,tbl_name,sql FROM main.sqlite_schema "
            "ORDER BY type COLLATE BINARY,name COLLATE BINARY"
        ).fetchall()
        schema_digest, _ = _rows_digest(objects)
        # wr is the persisted WITHOUT ROWID flag, rather than a SQL-text guess.
        table_flags = {_identifier(row[1]): row[4]
                       for row in connection.execute("PRAGMA main.table_list")}
        tables = []
        for kind, raw_name, _, sql in objects:
            if kind != b"table":
                continue
            name = _identifier(raw_name)
            columns = connection.execute(f"PRAGMA main.table_xinfo({_quote(name)})").fetchall()
            names = [_identifier(column[1]) for column in columns]
            selected = [_quote(column) for column in names]
            rowid_included = False
            if name not in table_flags:
                raise IntegrityError("sqlite_table_metadata_unavailable")
            if not table_flags[name]:
                occupied = {column.lower() for column in names}
                for rowid_name in ("_rowid_", "rowid", "oid"):
                    if rowid_name not in occupied:
                        selected.append(rowid_name)
                        rowid_included = True
                        break
            if not selected:
                raise IntegrityError("sqlite_column_metadata_unavailable")
            table_schema = hashlib.sha256(_record((sql,)) + b"".join(
                _record(column) for column in columns)).hexdigest()
            cursor = connection.execute(f"SELECT {','.join(selected)} FROM main.{_quote(name)}")
            content_digest, row_count = _rows_digest(cursor)
            tables.append({"name": name, "row_count": row_count, "column_count": len(columns),
                           "rowid_included": rowid_included, "schema_sha256": table_schema,
                           "content_sha256": content_digest})
        metadata = tuple(connection.execute("PRAGMA " + pragma).fetchone()[0]
                         for pragma in ("user_version", "application_id", "encoding"))
        metadata_digest = hashlib.sha256(_record(metadata)).hexdigest()
        connection.rollback()
    except sqlite3.Error:
        raise IntegrityError("sqlite_read_failed") from None
    finally:
        if connection is not None:
            connection.close()
    _reject_journals(database)
    after = database.lstat()
    _reject_link(after)
    if _signature(before) != _signature(after):
        raise IntegrityError("input_changed_during_read")
    return {"kind": "sqlite_snapshot", "format_version": 1,
            "input_requirement": INPUT_REQUIREMENT, "backup_consistency_verified": False,
            "readonly": True, "immutable": True, "sqlite_transaction_used": True,
            "schema_sha256": schema_digest, "metadata_sha256": metadata_digest,
            "tables": sorted(tables, key=lambda table: table["name"])}


def compare_database_snapshots(left: dict, right: dict) -> dict:
    before = {table["name"]: table for table in left["tables"]}
    after = {table["name"]: table for table in right["tables"]}
    comparisons = []
    for name in sorted(before.keys() | after.keys()):
        first, second = before.get(name), after.get(name)
        comparisons.append({"name": name, "left_row_count": first["row_count"] if first else None,
                            "right_row_count": second["row_count"] if second else None,
                            "schema_equal": bool(first and second and first["schema_sha256"] == second["schema_sha256"]),
                            "content_equal": bool(first and second and first["content_sha256"] == second["content_sha256"]),
                            "equal": first == second})
    schema_equal = left["schema_sha256"] == right["schema_sha256"]
    metadata_equal = left["metadata_sha256"] == right["metadata_sha256"]
    return {"kind": "sqlite_comparison", "format_version": 1,
            "input_requirement": INPUT_REQUIREMENT, "backup_consistency_verified": False,
            "cross_database_consistency_verified": False,
            "equal": schema_equal and metadata_equal and all(table["equal"] for table in comparisons),
            "schema_equal": schema_equal, "metadata_equal": metadata_equal, "tables": comparisons}


def compare_databases(left: str | Path, right: str | Path) -> dict:
    return compare_database_snapshots(snapshot_database(left), snapshot_database(right))


def snapshot_directory(path: str | Path) -> dict:
    """Inventory every regular file; a directory scan is not an atomic backup."""
    root = _plain_path(path, directory=True)
    before = _entries(root)
    files, databases = [], {}
    for relative, info in sorted(before.items()):
        if stat.S_ISREG(info.st_mode):
            item = _file_record(root / relative, relative, info)
            files.append(item)
            if item["sqlite_header"]:
                databases[relative] = snapshot_database(root / relative)
    after = _entries(root)
    if {key: _signature(value) for key, value in before.items()} != {
        key: _signature(value) for key, value in after.items()
    }:
        raise IntegrityError("input_changed_during_read")
    return {"kind": "directory_snapshot", "format_version": 1,
            "input_requirement": INPUT_REQUIREMENT, "backup_consistency_verified": False,
            "cross_file_atomicity_verified": False, "sqlite_detection": "file_header_only",
            "directories": sorted(key for key, value in before.items()
                                  if key != "." and stat.S_ISDIR(value.st_mode)),
            "files": files, "databases": databases}


def compare_directories(left: str | Path, right: str | Path) -> dict:
    first, second = snapshot_directory(left), snapshot_directory(right)
    before = {item["path"]: item for item in first["files"]}
    after = {item["path"]: item for item in second["files"]}
    changed = [name for name in sorted(before.keys() & after.keys()) if before[name] != after[name]]
    database_comparisons = []
    for name in sorted(first["databases"].keys() | second["databases"].keys()):
        if name in first["databases"] and name in second["databases"]:
            result = compare_database_snapshots(first["databases"][name], second["databases"][name])
        else:
            result = {"equal": False, "missing_left": name not in first["databases"],
                      "missing_right": name not in second["databases"]}
        database_comparisons.append({"path": name, **result})
    files_equal = before == after and first["directories"] == second["directories"]
    databases_equal = all(item["equal"] for item in database_comparisons)
    return {"kind": "directory_comparison", "format_version": 1,
            "input_requirement": INPUT_REQUIREMENT, "backup_consistency_verified": False,
            "cross_file_atomicity_verified": False, "equal": files_equal and databases_equal,
            "files_equal": files_equal, "databases_equal": databases_equal,
            "left_only": sorted(before.keys() - after.keys()),
            "right_only": sorted(after.keys() - before.keys()), "changed_files": changed,
            "directory_structure_equal": first["directories"] == second["directories"],
            "left_files": first["files"], "right_files": second["files"],
            "databases": database_comparisons}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("snapshot", "compare"))
    parser.add_argument("paths", nargs="+")
    parser.add_argument("--kind", choices=("auto", "directory", "sqlite"), default="auto")
    parser.add_argument("--offline-snapshot", action="store_true", required=True,
                        help="Declare the input requirement is satisfied; this is not verified.")
    args = parser.parse_args(argv)
    if len(args.paths) != (1 if args.operation == "snapshot" else 2):
        parser.error("snapshot requires one path; compare requires two paths")
    try:
        directories = [Path(path).is_dir() for path in args.paths]
        directory = args.kind == "directory" or (args.kind == "auto" and directories[0])
        if args.kind == "auto" and len(set(directories)) != 1:
            raise IntegrityError("input_kinds_differ")
        if args.operation == "snapshot":
            result = (snapshot_directory if directory else snapshot_database)(args.paths[0])
        else:
            result = (compare_directories if directory else compare_databases)(*args.paths)
        print(json.dumps({"ok": True, **result}, ensure_ascii=True, sort_keys=True))
        return 0 if result.get("equal", True) else 1
    except (IntegrityError, OSError, sqlite3.Error, UnicodeError) as exc:
        code = exc.code if isinstance(exc, IntegrityError) else "input_read_failed"
        print(json.dumps({"ok": False, "error": code}, sort_keys=True))
        return 2


if __name__ == "__main__":
    sys.exit(main())
