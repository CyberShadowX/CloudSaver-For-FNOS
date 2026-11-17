"""Synthetic registry tests; all filesystem fixtures stay in project work/."""
from __future__ import annotations

import contextlib
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest import mock
import urllib.error
import urllib.parse

from tools import fetch_image as fetch


TOKEN = "synthetic-secret-token-do-not-log"
REPOSITORY = "example/cloudsaver"
MANIFEST_TYPE = "application/vnd.docker.distribution.manifest.v2+json"
INDEX_TYPE = "application/vnd.oci.image.index.v1+json"
CONFIG_TYPE = "application/vnd.docker.container.image.v1+json"
LAYER_TYPE = "application/vnd.docker.image.rootfs.diff.tar.gzip"


def digest(raw):
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def descriptor(raw, media_type):
    return {"digest": digest(raw), "size": len(raw), "mediaType": media_type}


class Response(io.BytesIO):
    def __init__(self, raw=b"", status=200, headers=None):
        super().__init__(raw)
        self.status = status
        self.headers = {"Content-Length": str(len(raw)), **(headers or {})}

    def getcode(self):
        return self.status


class FakeHTTP:
    def __init__(self):
        self.routes = {}
        self.calls = []

    def add(self, url, raw, *, status=200, headers=None):
        self.routes[url] = lambda: Response(raw, status, headers)

    def __call__(self, request, *, timeout):
        self.calls.append(request)
        if request.full_url not in self.routes:
            raise AssertionError("Unexpected synthetic HTTP request")
        route = self.routes[request.full_url]
        return route()


class FetchImageTests(unittest.TestCase):
    def setUp(self):
        work = Path(__file__).resolve().parents[1] / "work" / "test-fetch-image"
        work.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=work)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.cache = self.root / "cache"
        self.output = self.root / "download.tar"

    def fixture(self, *, index=False, manifest_change=None, config_change=None,
                index_change=None, layer_count=1):
        raw = io.BytesIO()
        with tarfile.open(fileobj=raw, mode="w") as layer:
            item = tarfile.TarInfo("app/server.js")
            content = b"console.log('synthetic official runtime');\n"
            item.size = len(content)
            layer.addfile(item, io.BytesIO(content))
        layer_raw = gzip.compress(raw.getvalue(), mtime=0)
        config = {"os": "linux", "architecture": "amd64",
                  "rootfs": {"type": "layers", "diff_ids": [digest(raw.getvalue())] * layer_count}}
        if config_change:
            config_change(config)
        config_raw = encoded(config)
        manifest = {"schemaVersion": 2, "mediaType": MANIFEST_TYPE,
                    "config": descriptor(config_raw, CONFIG_TYPE),
                    "layers": [descriptor(layer_raw, LAYER_TYPE) for _ in range(layer_count)]}
        if manifest_change:
            manifest_change(manifest)
        manifest_raw = encoded(manifest)
        manifest_id = digest(manifest_raw)
        root_raw = manifest_raw
        index_value = None
        if index:
            child = {**descriptor(manifest_raw, MANIFEST_TYPE),
                     "platform": {"os": "linux", "architecture": "amd64"}}
            index_value = {"schemaVersion": 2, "mediaType": INDEX_TYPE, "manifests": [child]}
            if index_change:
                index_change(index_value)
            root_raw = encoded(index_value)
        root_id = digest(root_raw)
        http = FakeHTTP()
        auth_url = "https://auth.docker.io/token?" + urllib.parse.urlencode({
            "service": "registry.docker.io", "scope": f"repository:{REPOSITORY}:pull"})
        http.add(auth_url, encoded({"token": TOKEN}))
        prefix = f"https://registry-1.docker.io/v2/{REPOSITORY}/"
        http.add(prefix + "manifests/" + root_id, root_raw)
        http.add(prefix + "manifests/" + manifest_id, manifest_raw)
        http.add(prefix + "blobs/" + digest(config_raw), config_raw)
        http.add(prefix + "blobs/" + digest(layer_raw), layer_raw)
        return {"repository": REPOSITORY, "manifest_digest": root_id,
                "expected_image_id": digest(config_raw), "http_open": http}, {
            "http": http, "auth_url": auth_url, "prefix": prefix, "root_raw": root_raw,
            "manifest_raw": manifest_raw, "manifest_id": manifest_id, "layer_raw": layer_raw,
            "config_raw": config_raw, "index": index_value}

    def run_fetch(self, arguments, *, output=None, limits=None):
        return fetch.fetch_image(self.cache, output or self.output, limits=limits, **arguments)

    def assert_error(self, code, arguments, *, limits=None):
        with self.assertRaises(fetch.FetchError) as caught:
            self.run_fetch(arguments, limits=limits)
        self.assertEqual(caught.exception.code, code)
        self.assertNotIn(TOKEN, str(caught.exception))
        self.assertFalse(self.output.exists())
        if self.cache.exists():
            self.assertFalse(list(self.cache.glob(".fetch-*.part")))

    def test_direct_manifest_archive_preserves_config_and_compressed_layers(self):
        arguments, data = self.fixture()
        receipt = self.run_fetch(arguments)
        self.assertEqual(receipt["image_id"], arguments["expected_image_id"])
        self.assertEqual(receipt["resolved_manifest_digest"], arguments["manifest_digest"])
        self.assertEqual(receipt["layer_count"], 1)
        self.assertEqual(receipt["archive_sha256"], hashlib.sha256(self.output.read_bytes()).hexdigest())
        self.assertEqual(receipt["archive_size"], self.output.stat().st_size)
        with tarfile.open(self.output) as archive:
            manifest = json.load(archive.extractfile("manifest.json"))[0]
            self.assertEqual(archive.extractfile(manifest["Config"]).read(), data["config_raw"])
            self.assertEqual(archive.extractfile(manifest["Layers"][0]).read(), data["layer_raw"])
            self.assertEqual(len(archive.getmembers()), 3)
            self.assertTrue(all(member.isfile() and not member.name.startswith("/") for member in archive))
        self.assertNotIn(TOKEN.encode(), self.output.read_bytes())
        self.assertFalse(list(self.cache.glob(".fetch-*.part")))

    def test_index_selects_unique_linux_architecture_and_validates_child(self):
        def add_other(index):
            index["manifests"].append({**index["manifests"][0],
                                      "platform": {"os": "linux", "architecture": "arm64"}})
        arguments, data = self.fixture(index=True, index_change=add_other)
        receipt = self.run_fetch(arguments)
        self.assertEqual(receipt["resolved_manifest_digest"], data["manifest_id"])
        requested = [request.full_url for request in data["http"].calls]
        self.assertIn(data["prefix"] + "manifests/" + data["manifest_id"], requested)

    def test_ambiguous_and_missing_platform_are_rejected(self):
        for modify in (lambda index: index["manifests"].append(index["manifests"][0]),
                       lambda index: index["manifests"][0]["platform"].update(os="windows")):
            with self.subTest(modify=modify):
                arguments, _ = self.fixture(index=True, index_change=modify)
                self.assert_error("image_platform_not_unique", arguments)

    def test_pinned_root_manifest_digest_is_checked(self):
        arguments, data = self.fixture()
        url = data["prefix"] + "manifests/" + arguments["manifest_digest"]
        data["http"].add(url, data["root_raw"] + b" ")
        self.assert_error("download_digest_mismatch", arguments)

    def test_child_manifest_digest_is_checked(self):
        arguments, data = self.fixture(index=True)
        data["http"].add(data["prefix"] + "manifests/" + data["manifest_id"],
                         b" " + data["manifest_raw"][1:])
        self.assert_error("download_digest_mismatch", arguments)

    def test_child_descriptor_size_is_checked(self):
        arguments, _ = self.fixture(index=True, index_change=lambda index:
                                   index["manifests"][0].update(size=index["manifests"][0]["size"] + 1))
        self.assert_error("http_size_mismatch", arguments)

    def test_config_image_id_must_match_independently_pinned_id(self):
        arguments, data = self.fixture()
        arguments["expected_image_id"] = "sha256:" + "f" * 64
        self.assert_error("image_id_mismatch", arguments)
        self.assertFalse(any("/blobs/" in request.full_url for request in data["http"].calls))

    def test_config_digest_and_size_are_checked(self):
        arguments, data = self.fixture()
        data["http"].add(data["prefix"] + "blobs/" + arguments["expected_image_id"],
                         data["config_raw"][:-1] + b" ")
        self.assert_error("download_digest_mismatch", arguments)

    def test_config_os_and_architecture_are_checked(self):
        for field, value in (("os", "windows"), ("architecture", "arm64")):
            with self.subTest(field=field):
                arguments, _ = self.fixture(config_change=lambda config: config.update({field: value}))
                self.assert_error("image_platform_mismatch", arguments)

    def test_rootfs_diff_ids_shape_and_sha_are_checked(self):
        for modify, expected in ((lambda config: config["rootfs"].update(diff_ids=[]), "invalid_rootfs"),
                                 (lambda config: config["rootfs"].update(diff_ids=["bad"]), "invalid_digest")):
            with self.subTest(expected=expected):
                arguments, _ = self.fixture(config_change=modify)
                self.assert_error(expected, arguments)

    def test_layer_digest_and_length_are_checked(self):
        for corrupt, expected in ((lambda raw: raw[:-1], "http_size_mismatch"),
                                  (lambda raw: b"x" + raw[1:], "download_digest_mismatch")):
            with self.subTest(expected=expected):
                arguments, data = self.fixture()
                data["http"].add(data["prefix"] + "blobs/" + digest(data["layer_raw"]), corrupt(data["layer_raw"]))
                self.assert_error(expected, arguments)

    def test_streamed_length_is_checked_when_content_length_is_absent(self):
        arguments, data = self.fixture()
        url = data["prefix"] + "blobs/" + digest(data["layer_raw"])
        def short_stream():
            response = Response(data["layer_raw"][:-1])
            del response.headers["Content-Length"]
            return response
        data["http"].routes[url] = short_stream
        self.assert_error("http_size_mismatch", arguments)

    def test_descriptor_size_total_size_and_layer_count_limits(self):
        arguments, _ = self.fixture()
        self.assert_error("descriptor_size_limit", arguments, limits=fetch.Limits(max_layer_bytes=1))
        self.assert_error("total_size_limit", arguments, limits=fetch.Limits(max_total_bytes=1))
        arguments, _ = self.fixture(layer_count=3)
        self.assert_error("layer_count_limit", arguments, limits=fetch.Limits(max_layers=2))

    def test_boolean_sizes_foreign_urls_and_unknown_media_types_are_rejected(self):
        for modify, code in ((lambda manifest: manifest["layers"][0].update(size=True), "descriptor_size_limit"),
                             (lambda manifest: manifest["layers"][0].update(urls=["https://evil.invalid/"]), "unsupported_descriptor"),
                             (lambda manifest: manifest["layers"][0].update(mediaType="unknown"), "unsupported_descriptor")):
            with self.subTest(code=code):
                arguments, _ = self.fixture(manifest_change=modify)
                self.assert_error(code, arguments)

    def test_json_response_size_limit_is_enforced(self):
        arguments, data = self.fixture()
        data["http"].add(data["auth_url"], b" " * 2048)
        self.assert_error("http_size_mismatch", arguments, limits=fetch.Limits(max_json_bytes=1024))

    def test_valid_cache_is_rehashed_and_can_be_used_without_network(self):
        arguments, _ = self.fixture()
        self.run_fetch(arguments)
        def forbidden(*args, **kwargs):
            raise AssertionError("A complete valid cache needs no network")
        arguments["http_open"] = forbidden
        receipt = self.run_fetch(arguments, output=self.root / "second.tar")
        self.assertEqual(receipt["cache_hits"], 3)
        self.assertEqual((self.root / "second.tar").read_bytes(), self.output.read_bytes())

    def test_invalid_cache_blob_is_preserved_and_never_overwritten(self):
        arguments, data = self.fixture()
        self.run_fetch(arguments)
        self.output.unlink()
        path = self.cache / digest(data["layer_raw"]).split(":")[1]
        corrupt = b"x" + path.read_bytes()[1:]
        path.write_bytes(corrupt)
        self.assert_error("cache_blob_digest_mismatch", arguments)
        self.assertEqual(path.read_bytes(), corrupt)

    def test_duplicate_layers_have_one_tar_member_and_ordered_manifest_entries(self):
        arguments, _ = self.fixture(layer_count=2)
        receipt = self.run_fetch(arguments)
        self.assertEqual(receipt["layer_count"], 2)
        with tarfile.open(self.output) as archive:
            saved = json.load(archive.extractfile("manifest.json"))[0]
            self.assertEqual(len(saved["Layers"]), 2)
            self.assertEqual(saved["Layers"][0], saved["Layers"][1])
            self.assertEqual(len(archive.getmembers()), 3)

    def test_existing_output_is_never_overwritten(self):
        arguments, data = self.fixture()
        self.output.write_bytes(b"existing-output")
        with self.assertRaises(fetch.FetchError) as caught:
            self.run_fetch(arguments)
        self.assertEqual(caught.exception.code, "output_already_exists")
        self.assertEqual(self.output.read_bytes(), b"existing-output")
        self.assertEqual(data["http"].calls, [])

    def test_repository_and_digest_validation_happens_before_network(self):
        for repository in ("../cloudsaver", "example/../cloudsaver", "example/image?secret", "/image", "example//image"):
            with self.subTest(repository=repository):
                arguments, data = self.fixture()
                arguments["repository"] = repository
                self.assert_error("invalid_repository", arguments)
                self.assertFalse(data["http"].calls)
        arguments, _ = self.fixture()
        arguments["manifest_digest"] = "sha256:../not-a-digest"
        self.assert_error("invalid_digest", arguments)

    def test_parent_traversal_and_symlink_ancestors_are_rejected(self):
        arguments, _ = self.fixture()
        with self.assertRaises(fetch.FetchError) as caught:
            fetch.fetch_image(self.root / "missing" / ".." / "cache", self.output, **arguments)
        self.assertEqual(caught.exception.code, "parent_traversal_rejected")
        target = self.root / "target"; target.mkdir()
        linked = self.root / "link"
        try:
            linked.symlink_to(target, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("Platform cannot create symlinks")
        with self.assertRaises(fetch.FetchError) as caught:
            fetch.fetch_image(linked / "cache", self.output, **arguments)
        self.assertEqual(caught.exception.code, "symlink_or_reparse_point_rejected")

    def test_cached_symlink_is_rejected_even_when_content_matches(self):
        arguments, data = self.fixture()
        self.cache.mkdir()
        target = self.root / "manifest"
        target.write_bytes(data["root_raw"])
        linked = self.cache / arguments["manifest_digest"].split(":")[1]
        try:
            linked.symlink_to(target)
        except (OSError, NotImplementedError):
            self.skipTest("Platform cannot create symlinks")
        self.assert_error("symlink_or_reparse_point_rejected", arguments)

    def test_cdn_redirect_strips_authorization_and_keeps_digest_checks(self):
        arguments, data = self.fixture()
        blob_url = data["prefix"] + "blobs/" + digest(data["layer_raw"])
        cdn_url = "https://production.cloudfront.docker.com/blob?signature=synthetic-signed-query"
        data["http"].add(blob_url, b"", status=307, headers={"Location": cdn_url})
        data["http"].add(cdn_url, data["layer_raw"])
        self.run_fetch(arguments)
        for request in data["http"].calls:
            if request.full_url.startswith("https://registry-1.docker.io/"):
                self.assertEqual(request.get_header("Authorization"), "Bearer " + TOKEN)
            else:
                self.assertIsNone(request.get_header("Authorization"))
                self.assertIsNone(request.get_header("Referer"))
        self.assertNotIn(TOKEN.encode(), self.output.read_bytes())

    def test_non_https_unknown_host_credentials_and_ports_in_redirect_are_rejected(self):
        urls = ("http://production.cloudfront.docker.com/blob", "https://evil.invalid/blob",
                "https://user:pass@production.cloudfront.docker.com/blob",
                "https://production.cloudfront.docker.com:444/blob", "https://127.0.0.1/blob")
        for url in urls:
            with self.subTest(url=url):
                arguments, data = self.fixture()
                blob_url = data["prefix"] + "blobs/" + digest(data["layer_raw"])
                data["http"].add(blob_url, b"", status=307, headers={"Location": url})
                self.assert_error("redirect_rejected", arguments)
                self.assertFalse(any(request.full_url == url for request in data["http"].calls))

    def test_manifest_redirects_are_not_followed(self):
        arguments, data = self.fixture()
        url = data["prefix"] + "manifests/" + arguments["manifest_digest"]
        data["http"].add(url, b"", status=302, headers={"Location": "https://production.cloudfront.docker.com/manifest"})
        self.assert_error("redirect_rejected", arguments)

    def test_auth_redirect_and_external_auth_realm_are_never_used(self):
        arguments, data = self.fixture()
        data["http"].add(data["auth_url"], b"", status=302, headers={"Location": "https://evil.invalid/token"})
        self.assert_error("authentication_failed", arguments)
        self.assertEqual(len(data["http"].calls), 1)
        arguments, data = self.fixture()
        url = data["prefix"] + "manifests/" + arguments["manifest_digest"]
        data["http"].add(url, b"", status=401,
                         headers={"WWW-Authenticate": 'Bearer realm="https://evil.invalid/token"'})
        self.assert_error("http_request_failed", arguments)
        self.assertTrue(all(urllib.parse.urlsplit(request.full_url).hostname in
                            ("auth.docker.io", "registry-1.docker.io") for request in data["http"].calls))

    def test_real_urllib_http_redirect_errors_are_handled_without_forwarding(self):
        arguments, data = self.fixture()
        url = data["prefix"] + "blobs/" + digest(data["layer_raw"])
        cdn = "https://production.cloudflare.docker.com/verified-blob"
        def redirect_error():
            raise urllib.error.HTTPError(url, 307, "Redirect", {"Location": cdn}, io.BytesIO())
        data["http"].routes[url] = redirect_error
        data["http"].add(cdn, data["layer_raw"])
        self.run_fetch(arguments)
        request = next(request for request in data["http"].calls if request.full_url == cdn)
        self.assertIsNone(request.get_header("Authorization"))

    def test_network_errors_and_cli_errors_never_print_tokens_or_queries(self):
        arguments, data = self.fixture()
        def network_error():
            raise urllib.error.URLError("https://secret.invalid/?token=" + TOKEN)
        data["http"].routes[data["auth_url"]] = network_error
        with self.assertRaises(fetch.FetchError) as caught:
            self.run_fetch(arguments)
        self.assertEqual(str(caught.exception), "http_request_failed")
        declaration = self.root / "upstream.json"
        declaration.write_bytes(encoded({"repository": REPOSITORY,
                                         "manifest_digest": arguments["manifest_digest"],
                                         "image_id": arguments["expected_image_id"], "architecture": "amd64"}))
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(fetch, "fetch_image", side_effect=fetch.FetchError("http_request_failed")):
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                status = fetch.main(["--upstream", str(declaration), "--cache", str(self.cache),
                                     "--output", str(self.output)])
        self.assertEqual(status, 2)
        self.assertEqual(json.loads(stdout.getvalue()), {"ok": False, "error": "http_request_failed"})
        self.assertNotIn(TOKEN, stdout.getvalue() + stderr.getvalue())
        self.assertNotIn("https://", stdout.getvalue() + stderr.getvalue())

    def test_token_is_only_in_registry_request_memory_and_cache_contains_no_token(self):
        arguments, _ = self.fixture()
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            receipt = self.run_fetch(arguments)
        serialized = encoded(receipt) + stdout.getvalue().encode() + stderr.getvalue().encode()
        serialized += b"".join(path.read_bytes() for path in self.cache.iterdir())
        self.assertNotIn(TOKEN.encode(), serialized)

    def test_duplicate_json_keys_and_invalid_auth_token_are_rejected(self):
        arguments, data = self.fixture()
        data["http"].add(data["auth_url"], b'{"token":"one","token":"two"}')
        self.assert_error("duplicate_json_key", arguments)
        arguments, data = self.fixture()
        data["http"].add(data["auth_url"], encoded({"token": "bad\r\nHeader: injected"}))
        self.assert_error("invalid_authentication_response", arguments)

    def test_http_content_encoding_and_invalid_content_length_are_rejected(self):
        for headers, expected in (({"Content-Encoding": "gzip"}, "http_content_encoding_rejected"),
                                  ({"Content-Length": "-1"}, "invalid_content_length")):
            with self.subTest(expected=expected):
                arguments, data = self.fixture()
                data["http"].add(data["auth_url"], encoded({"token": TOKEN}), headers=headers)
                self.assert_error(expected, arguments)

    def test_download_deadline_and_invalid_limits_are_enforced(self):
        arguments, _ = self.fixture()
        with mock.patch.object(fetch.time, "monotonic", side_effect=[0, 2]):
            self.assert_error("download_time_limit", arguments, limits=fetch.Limits(max_seconds=1))
        self.assert_error("invalid_limits", arguments, limits=fetch.Limits(max_json_bytes=0))


if __name__ == "__main__":
    unittest.main()
