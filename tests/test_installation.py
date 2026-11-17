from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import setup_runtime as setup
import build_package as build

spec = importlib.util.spec_from_file_location("native_lifecycle", ROOT / "runtime/lifecycle.py")
lifecycle = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lifecycle)


class InstallationTests(unittest.TestCase):
    def setUp(self):
        work = ROOT / "work"
        work.mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix="installation-", dir=work)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def runtime_fixture(self):
        runtime = self.root / "runtime"
        files = {
            "lib/ld-musl-x86_64.so.1": b"loader", "usr/local/bin/node": b"node",
            "app/dist-final/app.js": b"program", "usr/share/nginx/html/index.html": b"frontend",
            "app/.env.example": b"JWT_SECRET=example-only\nTELEGRAM_BASE_URL=https://t.me/s\n",
            "app/release-content.local.default.json": b'{"version":"fixture"}',
        }
        entries = []
        for name, content in files.items():
            path = runtime / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
            entries.append({"path": name, "type": "file", "size": len(content),
                            "sha256": hashlib.sha256(content).hexdigest()})
        inventory = {"source": {"image_id": "sha256:" + "a" * 64}, "files": entries,
                     "file_count": len(entries)}
        (runtime / "runtime-inventory.json").write_text(json.dumps(inventory), encoding="utf-8")
        return runtime

    def test_runtime_inventory_detects_program_changes(self):
        runtime = self.runtime_fixture()
        setup.verify_runtime(runtime, "sha256:" + "a" * 64)
        (runtime / "app/dist-final/app.js").write_bytes(b"altered")
        with self.assertRaises(setup.SetupError):
            setup.verify_runtime(runtime, "sha256:" + "a" * 64)

    def test_wrong_version_rejected(self):
        with self.assertRaises(setup.SetupError):
            setup.verify_runtime(self.runtime_fixture(), "sha256:" + "b" * 64)

    @unittest.skipUnless(os.name == "posix", "Requires POSIX file modes")
    def test_program_tree_loses_fnos_group_write_without_changing_bytes(self):
        base = self.root / "installed"
        source = base / "runtime/service.py"
        source.parent.mkdir(parents=True)
        source.write_bytes(b"print('synthetic wrapper')\n")
        for path in (base, source.parent, source):
            path.chmod(0o775)
        setup.protect_program(base, expected_uid=os.getuid())
        self.assertEqual(source.read_bytes(), b"print('synthetic wrapper')\n")
        self.assertEqual(base.stat().st_mode & 0o777, 0o755)
        self.assertEqual(source.parent.stat().st_mode & 0o777, 0o755)
        self.assertEqual(source.stat().st_mode & 0o777, 0o644)

    @unittest.skipUnless(hasattr(os, "symlink"), "Requires symlink support")
    def test_configuration_is_generated_once_then_preserved(self):
        runtime = self.runtime_fixture()
        var = self.root / "state"
        var.mkdir()
        uid, gid = (os.getuid(), os.getgid()) if hasattr(os, "getuid") else (0, 0)
        with mock.patch.object(setup.os, "chown", create=True):
            setup.initialize_state(var, runtime, uid=uid, gid=gid)
            original = (var / "config/env").read_bytes()
            self.assertNotIn(b"example-only", original)
            self.assertEqual(original.count(b"JWT_SECRET="), 1)
            generated = next(line.split(b"=", 1)[1] for line in original.splitlines() if line.startswith(b"JWT_SECRET="))
            self.assertGreaterEqual(len(generated), 32)
            for pattern in (rb"[a-z]", rb"[A-Z]", rb"[0-9]", rb"[^A-Za-z0-9]"):
                self.assertIsNotNone(re.search(pattern, generated))
            personalized = b"JWT_SECRET=synthetic-kept\nOTHER_SETTING=fixture\n"
            (var / "config/env").write_bytes(personalized)
            (var / "config/release-content.local.json").write_bytes(b'{"version":"custom-fixture"}')
            setup.initialize_state(var, runtime, uid=uid, gid=gid)
            self.assertEqual((var / "config/env").read_bytes(), personalized)
            self.assertEqual((var / "config/release-content.local.json").read_bytes(), b'{"version":"custom-fixture"}')
            self.assertEqual((runtime / "app/data").resolve(), (var / "data").resolve())

    def test_existing_state_path_never_overwritten(self):
        link, target = self.root / "existing", self.root / "data"
        link.write_bytes(b"keep")
        target.mkdir()
        with self.assertRaises(setup.SetupError):
            setup.link_state(link, target)
        self.assertEqual(link.read_bytes(), b"keep")

    def test_pid_reuse_blocks_stop_signal(self):
        record = {"pid": 123, "start_ticks": "111"}
        with mock.patch.object(lifecycle, "read_record", return_value=record), \
             mock.patch.object(lifecycle, "identity", return_value={"start_ticks": "222"}), \
             mock.patch.object(lifecycle.os, "kill") as kill:
            with self.assertRaisesRegex(RuntimeError, "identity_changed"):
                lifecycle.stop(self.root, self.root, mock.Mock(pw_uid=10), self.root / "record")
            kill.assert_not_called()

    def test_dead_process_without_matching_receipt_blocks_stop(self):
        (self.root / "run").mkdir()
        receipt = self.root / "run/shutdown.json"
        receipt.write_text(json.dumps({"supervisor_pid": 124, "clean": True}), encoding="utf-8")
        with mock.patch.object(lifecycle, "read_record", return_value={"pid": 123}), \
             mock.patch.object(lifecycle, "identity", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "shutdown_not_confirmed"):
                lifecycle.stop(self.root, self.root, mock.Mock(pw_uid=10), self.root / "record")

    def test_pending_record_blocks_spawn_and_stop(self):
        record = self.root / "service.json"
        record.with_name(record.name + ".pending").write_bytes(b"interrupted fixture")
        with mock.patch.object(lifecycle, "read_record", return_value=None), \
             mock.patch.object(lifecycle.subprocess, "Popen") as spawn:
            with self.assertRaisesRegex(RuntimeError, "unfinished_lifecycle"):
                lifecycle.start(self.root, self.root, mock.Mock(pw_uid=10), record, 8008, 18009)
            with self.assertRaisesRegex(RuntimeError, "unfinished_lifecycle"):
                lifecycle.stop(self.root, self.root, mock.Mock(pw_uid=10), record)
            spawn.assert_not_called()

    def test_record_failure_never_releases_application_start_gate(self):
        (self.root / "private").mkdir()
        child = mock.Mock(pid=123)
        account = mock.Mock(pw_uid=10, pw_gid=10, pw_name="CloudSaver")
        with mock.patch.object(lifecycle, "read_record", return_value=None), \
             mock.patch.object(lifecycle, "reachable", return_value=False), \
             mock.patch.object(lifecycle, "identity", return_value={"start_ticks": "123"}), \
             mock.patch.object(lifecycle, "atomic_json", side_effect=OSError("synthetic disk error")), \
             mock.patch.object(setup, "setup"), \
             mock.patch.object(setup, "read_options", return_value={"node_env": "production"}), \
             mock.patch.object(lifecycle.subprocess, "Popen", return_value=child):
            with self.assertRaises(OSError):
                lifecycle.start(self.root, self.root, account, self.root / "private/service.json", 8008, 18009)
        child.stdin.write.assert_not_called()
        child.stdin.close.assert_called_once()
        child.wait.assert_called_once_with(timeout=10)

    def test_runtime_mode_is_explicit_and_validated_separately_from_personal_env(self):
        uid = os.getuid() if hasattr(os, "getuid") else 0
        path = self.root / "runtime-options.json"
        path.write_text('{"node_env":"development"}', encoding="utf-8")
        self.assertEqual(setup.read_options(self.root, expected_uid=uid), {"node_env": "development"})
        path.write_text('{"node_env":"untrusted-mode"}', encoding="utf-8")
        with self.assertRaises(setup.SetupError):
            setup.read_options(self.root, expected_uid=uid)

    def test_snapshot_holds_lifecycle_lock_for_whole_operation(self):
        import contextlib
        import snapshot_state
        held = {"value": False}
        @contextlib.contextmanager
        def lock(var):
            held["value"] = True
            try:
                yield var / "private/service.json"
            finally:
                held["value"] = False
        def copy(var):
            self.assertTrue(held["value"])
            return {"complete": True}
        sys.path.insert(0, str(ROOT / "runtime"))
        import lifecycle as native_module
        with mock.patch.object(native_module, "locked", side_effect=lock), \
             mock.patch.object(snapshot_state, "snapshot_locked", side_effect=copy):
            self.assertEqual(snapshot_state.snapshot(self.root), {"complete": True})
        self.assertFalse(held["value"])

    def test_fpk_allowlist_rejects_private_file(self):
        files = {name: b"source fixture" for name in build.SOURCES}
        files.update({"ui/config": b"{}", "ui/images/icon_64.png": b"png", "ui/images/icon_256.png": b"png"})
        files["data/database.sqlite"] = b"must not publish"
        app = build.tar_bytes(files, compressed=True)
        outer = {"app.tgz": app, "manifest": ("checksum = " + hashlib.md5(app).hexdigest() + "\n").encode()}
        fpk = self.root / "bad.fpk"
        fpk.write_bytes(build.tar_bytes(outer, compressed=False))
        with self.assertRaisesRegex(ValueError, "allowlist"):
            build.normalize_fpk(fpk)

    def test_archive_paths_and_linked_build_inputs_rejected(self):
        with self.assertRaises(ValueError):
            build.tar_bytes({"../escape": b"x"}, compressed=False)
        target = self.root / "target"
        target.write_bytes(b"fixture")
        link = self.root / "link"
        link.symlink_to(target)
        with self.assertRaises(ValueError):
            build.reject_links(link)


if __name__ == "__main__":
    unittest.main()
