"""Synthetic image-save fixtures: no network, real runtime or user data."""
from __future__ import annotations

from contextlib import redirect_stdout
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock
import tarfile

from tools import extract_runtime as extract


ROOT = Path(__file__).resolve().parents[1]
TEST_ROOT = ROOT / "work" / "extract-runtime-tests"


def layer(entries: list[tuple]) -> bytes:
    content = io.BytesIO()
    with tarfile.open(fileobj=content, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for entry in entries:
            name, value, *options = entry
            kind = options[0] if options else "file"
            member = tarfile.TarInfo(name)
            member.mode = 0o755 if name.endswith("/node") else 0o644
            if kind == "directory":
                member.type = tarfile.DIRTYPE
                archive.addfile(member)
            elif kind in ("symlink", "hardlink"):
                member.type = tarfile.SYMTYPE if kind == "symlink" else tarfile.LNKTYPE
                member.linkname = value
                archive.addfile(member)
            elif kind == "fifo":
                member.type = tarfile.FIFOTYPE
                archive.addfile(member)
            else:
                value = value.encode() if isinstance(value, str) else value
                member.size = len(value)
                archive.addfile(member, io.BytesIO(value))
    return content.getvalue()


def image(path: Path, layers: list[bytes], *, compressed: bool = False,
          bad_diff: bool = False, config_extra: dict | None = None) -> str:
    diff_ids = ["sha256:" + hashlib.sha256(value).hexdigest() for value in layers]
    if bad_diff:
        diff_ids[-1] = "sha256:" + "0" * 64
    config = {"architecture": "amd64", "os": "linux", "rootfs": {"type": "layers", "diff_ids": diff_ids}}
    config.update(config_extra or {})
    config_bytes = json.dumps(config, sort_keys=True).encode()
    config_digest = hashlib.sha256(config_bytes).hexdigest()
    config_path = "blobs/sha256/" + config_digest
    packed = [gzip.compress(value, mtime=0) if compressed else value for value in layers]
    layer_paths = ["blobs/sha256/" + hashlib.sha256(value).hexdigest() for value in packed]
    manifest = [{"Config": config_path, "RepoTags": ["synthetic/cloudsaver:test"], "Layers": layer_paths}]
    with tarfile.open(path, "w") as archive:
        data = [("manifest.json", json.dumps(manifest).encode()), (config_path, config_bytes), *zip(layer_paths, packed)]
        for name, value in data:
            member = tarfile.TarInfo(name)
            member.size = len(value)
            archive.addfile(member, io.BytesIO(value))
    return "sha256:" + config_digest


class RuntimeExtractionTests(unittest.TestCase):
    def setUp(self):
        TEST_ROOT.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix="case-", dir=TEST_ROOT)
        self.base = Path(self.temporary.name).resolve()
        self.addCleanup(self.temporary.cleanup)
        self.archive = self.base / "image.tar"
        self.output = self.base / "native-runtime"

    def run_image(self, entries, *, more_layers=(), compressed=False):
        identifier = image(self.archive, [layer(entries), *(layer(x) for x in more_layers)], compressed=compressed)
        return extract.extract_runtime(self.archive, self.output, identifier)

    def make_link(self, path: Path, target: str, *, directory=False):
        try:
            path.symlink_to(target, target_is_directory=directory)
        except OSError as exc:
            self.skipTest("OS account cannot create symbolic links: " + type(exc).__name__)

    def test_pristine_program_allowlist_excludes_configuration(self):
        result = self.run_image([
            ("app/dist-final/app.js", "program"), ("app/node_modules/module/index.js", "dependency"),
            ("app/.env.example", "PUBLIC_DEFAULT=example"),
            ("app/.env", "private"), ("app/.env.local", "private"),
            ("app/data/database.sqlite", "private"), ("app/config/env", "private"),
            ("app/runtime.db-wal", "private"), ("app/runtime.sqlite-shm", "private"),
            ("app/server.log", "private"), ("usr/local/bin/node", "binary"),
            ("lib/ld-musl-x86_64.so.1", "loader"), ("usr/lib/libstdc++.so.6.0", "library"),
            ("usr/share/nginx/html/index.html", "public UI"), ("etc/shadow", "excluded"),
            ("usr/lib/not-needed.a", "excluded"), ("root/.ssh/id_rsa", "excluded"),
        ])
        actual = {x["path"] for x in result["files"]}
        self.assertEqual(actual, {"app/dist-final/app.js", "app/node_modules/module/index.js", "app/.env.example",
            "usr/local/bin/node", "lib/ld-musl-x86_64.so.1", "usr/lib/libstdc++.so.6.0",
            "usr/share/nginx/html/index.html"})
        self.assertFalse((self.output / "app/config").exists())
        self.assertFalse((self.output / "app/data").exists())
        self.assertEqual((self.output / "usr/local/bin/node").stat().st_size, 6)
        self.assertTrue((self.output / "runtime-inventory.json").is_file())
        serialized = json.dumps(result)
        self.assertNotIn('"content"', serialized)
        self.assertEqual(result["source"]["archive_sha256"], hashlib.sha256(self.archive.read_bytes()).hexdigest())

    def test_layer_replacement_deletion_and_opaque_order(self):
        self.run_image([
            ("app/replace.js", "old"), ("app/delete.js", "old"),
            ("app/assets/old.js", "old"), ("app/assets/preserve.js", "old"),
        ], more_layers=[[
            ("app/replace.js", "new"), ("app/.wh.delete.js", b""),
            ("app/assets/new.js", "new"), ("app/assets/.wh..wh..opq", b""),
        ]])
        self.assertEqual((self.output / "app/replace.js").read_text(), "new")
        self.assertFalse((self.output / "app/delete.js").exists())
        self.assertEqual(sorted(p.name for p in (self.output / "app/assets").iterdir()), ["new.js"])

    def test_gzip_layers_hash_uncompressed_bytes(self):
        result = self.run_image([("app/app.js", "compressed layer")], compressed=True)
        self.assertEqual((self.output / "app/app.js").read_text(), "compressed layer")
        self.assertEqual(result["file_count"], 1)

    @unittest.skipUnless(os.name == "posix", "POSIX umask and permission semantics required")
    def test_umask_077_leaves_new_runtime_directories_traversable(self):
        identifier = image(self.archive, [layer([
            ("app/dist-final/nested/app.js", "program"),
            ("app/node_modules/pkg/index.js", "dependency"),
            ("usr/local/bin/node", "node"),
            ("lib/ld-musl-x86_64.so.1", "loader"),
            ("usr/lib/libexample.so.1", "library"),
            ("usr/share/nginx/html/assets/app.js", "frontend"),
        ]), layer([("app/second/deep/file.js", "second layer")])])
        ancestor_mode = stat.S_IMODE(self.base.stat().st_mode)
        input_mode = stat.S_IMODE(self.archive.stat().st_mode)
        previous_umask = os.umask(0o077)
        try:
            extract.extract_runtime(self.archive, self.output, identifier)
        finally:
            os.umask(previous_umask)
        directories = [self.output, *(p for p in self.output.rglob("*") if p.is_dir())]
        for directory in directories:
            with self.subTest(directory=directory.relative_to(self.output).as_posix()):
                self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o755)
        self.assertEqual(stat.S_IMODE((self.output / "usr/local/bin/node").stat().st_mode), 0o755)
        self.assertEqual(stat.S_IMODE((self.output / "app/dist-final/nested/app.js").stat().st_mode), 0o644)
        self.assertEqual(stat.S_IMODE(self.base.stat().st_mode), ancestor_mode)
        self.assertEqual(stat.S_IMODE(self.archive.stat().st_mode), input_mode)

    def test_wrong_image_id_creates_no_output(self):
        image(self.archive, [layer([("app/app.js", "program")])])
        with self.assertRaisesRegex(extract.ExtractionError, "configuration digest"):
            extract.extract_runtime(self.archive, self.output, "sha256:" + "0" * 64)
        self.assertFalse(self.output.exists())

    def test_wrong_layer_digest_creates_no_output(self):
        identifier = image(self.archive, [layer([("app/app.js", "program")])], bad_diff=True)
        with self.assertRaisesRegex(extract.ExtractionError, "layer digest"):
            extract.extract_runtime(self.archive, self.output, identifier)
        self.assertFalse(self.output.exists())

    def test_invalid_expected_digest_is_rejected(self):
        with self.assertRaisesRegex(extract.ExtractionError, "explicit SHA-256"):
            extract.extract_runtime(self.archive, self.output, "latest")

    def test_output_existing_even_if_empty_is_rejected(self):
        identifier = image(self.archive, [layer([("app/app.js", "program")])])
        self.output.mkdir()
        with self.assertRaisesRegex(extract.ExtractionError, "already exist"):
            extract.extract_runtime(self.archive, self.output, identifier)
        self.assertEqual(list(self.output.iterdir()), [])

    def test_output_ancestor_link_is_rejected(self):
        identifier = image(self.archive, [layer([("app/app.js", "program")])])
        real = self.base / "real"
        real.mkdir()
        alias = self.base / "alias"
        self.make_link(alias, "real", directory=True)
        with self.assertRaisesRegex(extract.ExtractionError, "traverse"):
            extract.extract_runtime(self.archive, alias / "new", identifier)
        self.assertFalse((real / "new").exists())

    def test_layer_traversal_and_windows_streams_rejected(self):
        for name in ("app/../../outside", "/app/absolute", "app/a:stream", "app\\escape"):
            with self.subTest(name=name), tempfile.TemporaryDirectory(dir=self.base) as case:
                archive = Path(case) / "image.tar"
                identifier = image(archive, [layer([(name, "bad")])])
                with self.assertRaises(extract.ExtractionError):
                    extract.extract_runtime(archive, Path(case) / "out", identifier)
        self.assertFalse((self.base / "outside").exists())

    def test_relative_node_modules_symlink_and_rebased_absolute_library(self):
        probe = self.base / "probe"
        self.make_link(probe, "nonexistent")
        probe.unlink()
        result = self.run_image([
            ("app/node_modules/pkg/bin.js", "program"),
            ("app/node_modules/.bin/pkg", "../pkg/bin.js", "symlink"),
            ("lib/libexample.so.1", "library"),
            ("usr/lib/libexample.so", "/lib/libexample.so.1", "symlink"),
        ])
        self.assertEqual((self.output / "app/node_modules/.bin/pkg").read_text(), "program")
        self.assertEqual((self.output / "usr/lib/libexample.so").read_text(), "library")
        self.assertFalse((self.output / "usr/lib/libexample.so").readlink().is_absolute())
        self.assertEqual(sum(x["type"] == "symlink" for x in result["files"]), 2)

    def test_links_to_private_or_outside_content_are_rejected(self):
        for target in ("../../outside", "/etc/passwd", "data/database.sqlite", "config/env"):
            with self.subTest(target=target), tempfile.TemporaryDirectory(dir=self.base) as case:
                archive = Path(case) / "image.tar"
                identifier = image(archive, [layer([("app/link", target, "symlink")])])
                with self.assertRaises(extract.ExtractionError):
                    extract.extract_runtime(archive, Path(case) / "out", identifier)

    def test_future_write_through_symlink_is_rejected(self):
        probe = self.base / "probe"
        self.make_link(probe, "nonexistent")
        probe.unlink()
        identifier = image(self.archive, [layer([
            ("app/real", b"", "directory"), ("app/alias", "real", "symlink"),
            ("app/alias/app.js", "must not write through alias"),
        ])])
        with self.assertRaisesRegex(extract.ExtractionError, "traverse a link"):
            extract.extract_runtime(self.archive, self.output, identifier)
        self.assertFalse((self.output / "app/real/app.js").exists())

    def test_hardlinks_copy_after_target_and_do_not_alias_later_layers(self):
        self.run_image([
            ("app/second.js", "app/first.js", "hardlink"), ("app/first.js", "original"),
        ], more_layers=[[("app/first.js", "new")]])
        self.assertEqual((self.output / "app/first.js").read_text(), "new")
        self.assertEqual((self.output / "app/second.js").read_text(), "original")

    def test_missing_hardlink_target_fails(self):
        identifier = image(self.archive, [layer([("app/link.js", "app/absent.js", "hardlink")])])
        with self.assertRaisesRegex(extract.ExtractionError, "hardlink target"):
            extract.extract_runtime(self.archive, self.output, identifier)

    def test_selected_special_file_is_rejected(self):
        identifier = image(self.archive, [layer([("app/pipe", b"", "fifo")])])
        with self.assertRaisesRegex(extract.ExtractionError, "Special runtime"):
            extract.extract_runtime(self.archive, self.output, identifier)

    def test_duplicate_selected_tar_name_is_rejected(self):
        identifier = image(self.archive, [layer([("app/a.js", "first"), ("./app/a.js", "second")])])
        with self.assertRaisesRegex(extract.ExtractionError, "Duplicate layer"):
            extract.extract_runtime(self.archive, self.output, identifier)

    def test_cli_output_contains_only_summary(self):
        identifier = image(self.archive, [layer([("app/app.js", "PROGRAM_CONTENT_SHOULD_NOT_BE_PRINTED")])])
        capture = io.StringIO()
        with redirect_stdout(capture):
            status = extract.main(["--archive", str(self.archive), "--output", str(self.output), "--image-id", identifier])
        summary = json.loads(capture.getvalue())
        self.assertEqual(status, 0)
        self.assertTrue(summary["ok"])
        self.assertEqual(summary["file_count"], 1)
        self.assertNotIn("PROGRAM_CONTENT", capture.getvalue())
        self.assertNotIn("files", summary)


if __name__ == "__main__":
    unittest.main()
