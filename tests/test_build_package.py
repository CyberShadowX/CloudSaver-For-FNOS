"""Check the archive contract consumed by fnOS App Center."""
from pathlib import Path
import gzip
import hashlib
import io
import sys
import tarfile
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import build_package


class FpkFormatTests(unittest.TestCase):
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
