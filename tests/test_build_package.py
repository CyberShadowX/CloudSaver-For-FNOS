"""Check the archive contract consumed by fnOS App Center."""
from pathlib import Path
import gzip
import hashlib
import io
import json
import sys
import tarfile
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import build_package


class FpkFormatTests(unittest.TestCase):
    def test_new_display_brand_keeps_the_existing_app_identity(self):
        work = Path(__file__).resolve().parents[1] / "work"
        work.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="fpk-brand-", dir=work) as folder:
            root = Path(folder)
            for name in build_package.SOURCES:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("Synthetic packaged source: " + name + "\n", encoding="utf-8")
            (root / "assets").mkdir()
            for size in (64, 256):
                (root / "assets" / f"icon_{size}.png").write_bytes(f"synthetic-png-{size}".encode())
            fnpack = root / "fnpack.exe"
            fnpack.write_bytes(b"synthetic fnpack executable")

            def fake_fnpack(arguments, *, cwd, check):
                self.assertTrue(check)
                self.assertEqual(arguments, [str(fnpack), "build", "--directory", str(cwd)])
                stage = Path(cwd)
                payload = {path.relative_to(stage / "app").as_posix(): path.read_bytes()
                           for path in (stage / "app").rglob("*") if path.is_file()}
                outer = {path.relative_to(stage).as_posix(): path.read_bytes()
                         for path in stage.rglob("*")
                         if path.is_file() and "app" not in path.relative_to(stage).parts}
                for name in ("config/privilege", "config/resource"):
                    payload[name] = outer[name]
                outer["app.tgz"] = build_package.tar_bytes(payload, compressed=True)
                outer["manifest"] += ("checksum = " + hashlib.md5(outer["app.tgz"]).hexdigest() + "\n").encode()
                # fnpack chooses this filename from the appname, not the display brand.
                (stage / "CloudSaver.fpk").write_bytes(build_package.tar_bytes(outer, compressed=True))

            with mock.patch.object(build_package, "ROOT", root), \
                    mock.patch.object(build_package, "FNPACK_SHA256", hashlib.sha256(fnpack.read_bytes()).hexdigest()), \
                    mock.patch.object(build_package.subprocess, "run", side_effect=fake_fnpack):
                inventory = build_package.build(fnpack, root / "output")

            package_name = f"CloudSaver-For-FNOS-{build_package.VERSION}-x86.fpk"
            self.assertEqual(inventory["file"], package_name)
            self.assertFalse((root / "output" / f"CloudSaver-{build_package.VERSION}-x86.fpk").exists())
            outer = build_package.archive_files((root / "output" / package_name).read_bytes())
            manifest = dict(line.split(" = ", 1) for line in outer["manifest"].decode().splitlines())
            self.assertEqual(manifest["appname"], "CloudSaver")
            self.assertEqual(manifest["desktop_applaunchname"], "CloudSaver.Application")
            self.assertEqual(manifest["display_name"], "CloudSaver For FNOS")
            self.assertTrue(manifest["desc"].startswith("CloudSaver For FNOS "))
            self.assertEqual(manifest["maintainer_url"], "https://github.com/CyberShadowX/CloudSaver-For-FNOS")
            self.assertEqual(manifest["service_port"], "8008")
            self.assertEqual(json.loads(outer["config/privilege"]), {
                "defaults": {"run-as": "root"}, "username": "CloudSaver", "groupname": "CloudSaver"})
            payload = build_package.archive_files(outer["app.tgz"])
            desktop = json.loads(payload["ui/config"])[".url"]
            self.assertEqual(set(desktop), {"CloudSaver.Application"})
            self.assertEqual(desktop["CloudSaver.Application"]["title"], "CloudSaver For FNOS")
            self.assertEqual(desktop["CloudSaver.Application"]["icon"], "images/icon_{0}.png")
            self.assertEqual(payload["THIRD_PARTY_NOTICES.md"], (root / "THIRD_PARTY_NOTICES.md").read_bytes())
            for size, name in ((64, "ICON.PNG"), (256, "ICON_256.PNG")):
                icon = (root / "assets" / f"icon_{size}.png").read_bytes()
                self.assertEqual(outer[name], icon)
                self.assertEqual(payload[f"ui/images/icon_{size}.png"], icon)

    def test_normalized_fpk_remains_gzip_with_valid_payload_checksum(self):
        payload = {name: (name + "\n").encode() for name in build_package.SOURCES}
        payload.update({"ui/config": b"{}", "ui/images/icon_64.png": b"png64",
                        "ui/images/icon_256.png": b"png256",
                        "config/privilege": b"{}", "config/resource": b"{}"})
        app = build_package.tar_bytes(payload, compressed=True)
        outer = {name: b"" for name in ["ICON.PNG", "ICON_256.PNG", "LICENSE",
                 "config/privilege", "config/resource", "wizard/install"]}
        outer.update({"app.tgz": app, "manifest":
                      ("appname = CloudSaver\nchecksum = " + hashlib.md5(app).hexdigest() + "\n").encode()})
        outer.update({"config/privilege": b"{}", "config/resource": b"{}"})
        outer.update({"cmd/" + name: b"#!/bin/sh\nexit 0\n" for name in [
            "main", "install_init", "install_callback", "uninstall_init", "uninstall_callback",
            "upgrade_init", "upgrade_callback", "config_init", "config_callback"]})
        work = Path(__file__).resolve().parents[1] / "work"
        work.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="fpk-format-", dir=work) as folder:
            package = Path(folder) / "CloudSaver.fpk"
            package.write_bytes(build_package.tar_bytes(outer, compressed=True))
            build_package.normalize_fpk(package)
            # Use a gzip-only decoder, matching the platform contract rather
            # than tarfile's permissive automatic compression detection.
            decoded = gzip.decompress(package.read_bytes())
            with tarfile.open(fileobj=io.BytesIO(decoded), mode="r:") as archive:
                manifest = archive.extractfile("manifest").read().decode()
                content = archive.extractfile("app.tgz").read()
                self.assertIn(hashlib.md5(content).hexdigest(), manifest)
                self.assertEqual(archive.getmember("cmd/main").mode, 0o755)
                self.assertEqual(archive.getmember("manifest").mode, 0o644)
            self.assertEqual(build_package.archive_files(content), payload)


if __name__ == "__main__":
    unittest.main()
