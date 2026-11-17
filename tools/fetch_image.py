"""Fetch a digest-pinned official Docker Hub image without a Docker daemon.

Anonymous registry credentials remain in memory. Only verified content-addressed
blobs and a newly created Docker-save-layout archive are written to disk. The
archive retains compressed layer bytes for extract_runtime's diff_id checks.
"""
from __future__ import annotations

import argparse
import contextlib
from dataclasses import dataclass
import hashlib
import http.client
import io
import json
import os
from pathlib import Path
import re
import stat
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request


REGISTRY_HOST = "registry-1.docker.io"
AUTH_HOST = "auth.docker.io"
# Docker's published pull/push endpoints. Never permit arbitrary redirect hosts.
# https://docs.docker.com/desktop/enterprise/allow-list/
CDN_HOSTS = frozenset((
    "production.cloudfront.docker.com", "production.cloudflare.docker.com",
    "docker-images-prod.6aa30f8b08e16409b46e0173d6de2f56.r2.cloudflarestorage.com",
))
MANIFEST_TYPES = frozenset((
    "application/vnd.docker.distribution.manifest.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
))
INDEX_TYPES = frozenset((
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.index.v1+json",
))
CONFIG_TYPES = frozenset((
    "application/vnd.docker.container.image.v1+json",
    "application/vnd.oci.image.config.v1+json",
))
LAYER_TYPES = frozenset((
    "application/vnd.docker.image.rootfs.diff.tar",
    "application/vnd.docker.image.rootfs.diff.tar.gzip",
    "application/vnd.oci.image.layer.v1.tar",
    "application/vnd.oci.image.layer.v1.tar+gzip",
    "application/vnd.oci.image.layer.v1.tar+zstd",
))
ACCEPT_MANIFEST = ", ".join(sorted(MANIFEST_TYPES | INDEX_TYPES))


@dataclass(frozen=True)
class Limits:
    max_total_bytes: int = 1024 ** 3
    max_layer_bytes: int = 512 * 1024 ** 2
    max_json_bytes: int = 2 * 1024 ** 2
    max_layers: int = 256
    timeout: float = 30.0
    max_seconds: float = 1800.0


class FetchError(Exception):
    """A fixed error code without response text, headers, tokens or URLs."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        return None


def _digest(value) -> str:
    if not isinstance(value, str) or re.fullmatch(r"sha256:[0-9a-f]{64}", value) is None:
        raise FetchError("invalid_digest")
    return value


def _repository(value) -> str:
    component = r"[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*"
    if (not isinstance(value, str) or len(value) > 255
            or re.fullmatch(component + r"(?:/" + component + r")*", value) is None):
        raise FetchError("invalid_repository")
    return value


def _architecture(value) -> str:
    if value not in ("amd64", "arm64"):
        raise FetchError("unsupported_architecture")
    return value


def _reject_link(info) -> None:
    if stat.S_ISLNK(info.st_mode) or (
        getattr(info, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    ):
        raise FetchError("symlink_or_reparse_point_rejected")


def _absolute(path) -> Path:
    candidate = Path(path)
    if ".." in candidate.parts:
        raise FetchError("parent_traversal_rejected")
    return Path(os.path.abspath(candidate))


def _directory(path, *, create=False) -> Path:
    directory = _absolute(path)
    for part in [*reversed(directory.parents), directory]:
        try:
            info = part.lstat()
        except FileNotFoundError:
            if not create:
                raise FetchError("directory_unavailable") from None
            try:
                part.mkdir(mode=0o700)
            except FileExistsError:
                pass
            info = part.lstat()
        _reject_link(info)
        if not stat.S_ISDIR(info.st_mode):
            raise FetchError("directory_required")
    return directory


def _regular(path: Path):
    _directory(path.parent)
    info = path.lstat()
    _reject_link(info)
    if not stat.S_ISREG(info.st_mode):
        raise FetchError("regular_file_required")
    return info


def _signature(info):
    change_time = getattr(info, "st_birthtime_ns", None) if os.name == "nt" else info.st_ctime_ns
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, change_time)


@contextlib.contextmanager
def _read_regular(path: Path):
    before = _regular(path)
    flags = (os.O_RDONLY | getattr(os, "O_BINARY", 0)
             | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(os.open(path, flags), "rb") as source:
        if _signature(before) != _signature(os.fstat(source.fileno())):
            raise FetchError("input_changed_during_read")
        yield source, before.st_size
        if _signature(before) != _signature(os.fstat(source.fileno())):
            raise FetchError("input_changed_during_read")
    if _signature(before) != _signature(_regular(path)):
        raise FetchError("input_changed_during_read")


@contextlib.contextmanager
def _temporary(parent: Path):
    _directory(parent)
    descriptor, name = tempfile.mkstemp(prefix=".fetch-", suffix=".part", dir=parent)
    identity = os.fstat(descriptor)
    path = Path(name)
    try:
        with os.fdopen(descriptor, "w+b") as target:
            yield path, target
    finally:
        # Delete only our exclusive temporary inode, never an existing cache blob.
        try:
            current = path.lstat()
            if (current.st_dev, current.st_ino) == (identity.st_dev, identity.st_ino):
                path.unlink()
        except FileNotFoundError:
            pass


def _parse_json(raw: bytes) -> dict:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise FetchError("duplicate_json_key")
            result[key] = value
        return result

    try:
        value = json.loads(raw, object_pairs_hook=unique)
    except (ValueError, UnicodeError, RecursionError):
        raise FetchError("invalid_json") from None
    if not isinstance(value, dict):
        raise FetchError("json_object_required")
    return value


def _descriptor(value, media_types, limit: int) -> dict:
    if (not isinstance(value, dict) or value.get("mediaType") not in media_types
            or value.get("urls")):
        raise FetchError("unsupported_descriptor")
    digest = _digest(value.get("digest"))
    size = value.get("size")
    if type(size) is not int or size < 0 or size > limit:
        raise FetchError("descriptor_size_limit")
    return {"digest": digest, "size": size, "mediaType": value["mediaType"]}


def _validate_https(url, allowed_hosts):
    if not isinstance(url, str) or len(url) > 16384 or any(ord(char) < 32 for char in url):
        raise FetchError("redirect_rejected")
    try:
        parsed = urllib.parse.urlsplit(url)
        valid = (parsed.scheme == "https" and parsed.hostname in allowed_hosts
                 and parsed.port in (None, 443) and parsed.username is None
                 and parsed.password is None and not parsed.fragment)
    except ValueError:
        valid = False
    if not valid:
        raise FetchError("redirect_rejected")


class _Registry:
    def __init__(self, repository: str, limits: Limits, http_open=None):
        self.repository = repository
        self.limits = limits
        self.http_open = http_open or urllib.request.build_opener(_NoRedirect()).open
        self.token = None
        self.deadline = time.monotonic() + limits.max_seconds

    def check_deadline(self):
        if time.monotonic() > self.deadline:
            raise FetchError("download_time_limit")

    def _open(self, url: str, headers: dict):
        self.check_deadline()
        try:
            response = self.http_open(urllib.request.Request(url, headers=headers),
                                      timeout=self.limits.timeout)
        except urllib.error.HTTPError as error:
            # Redirects are handled explicitly, never by urllib's header forwarding.
            if error.code in (301, 302, 303, 307, 308, 401):
                return error
            error.close()
            raise FetchError("http_request_failed") from None
        except (OSError, urllib.error.URLError, http.client.HTTPException):
            raise FetchError("http_request_failed") from None
        return response

    @staticmethod
    def _status(response):
        return getattr(response, "status", None) or response.getcode()

    def read(self, response, *, expected_size=None, maximum: int, target=None):
        encoding = response.headers.get("Content-Encoding", "identity").strip().lower()
        if encoding not in ("", "identity"):
            raise FetchError("http_content_encoding_rejected")
        declared = response.headers.get("Content-Length")
        if declared is not None:
            if not re.fullmatch(r"[0-9]+", declared):
                raise FetchError("invalid_content_length")
            try:
                declared = int(declared)
            except ValueError:
                raise FetchError("invalid_content_length") from None
            if declared > maximum or (expected_size is not None and declared != expected_size):
                raise FetchError("http_size_mismatch")
        digest, count, parts = hashlib.sha256(), 0, []
        try:
            while True:
                self.check_deadline()
                chunk = response.read(min(1024 * 1024, maximum - count + 1))
                if not chunk:
                    break
                count += len(chunk)
                if count > maximum or (expected_size is not None and count > expected_size):
                    raise FetchError("download_size_limit")
                digest.update(chunk)
                if target is not None:
                    target.write(chunk)
                else:
                    parts.append(chunk)
        except (OSError, http.client.HTTPException):
            raise FetchError("http_read_failed") from None
        if (expected_size is not None and count != expected_size) or (declared is not None and count != declared):
            raise FetchError("http_size_mismatch")
        return b"".join(parts), count, "sha256:" + digest.hexdigest()

    def authenticate(self):
        query = urllib.parse.urlencode({"service": "registry.docker.io",
                                      "scope": f"repository:{self.repository}:pull"})
        url = f"https://{AUTH_HOST}/token?{query}"
        _validate_https(url, (AUTH_HOST,))
        with contextlib.closing(self._open(url, {"Accept": "application/json",
                                                "Accept-Encoding": "identity"})) as response:
            if self._status(response) != 200:
                raise FetchError("authentication_failed")
            raw, _, _ = self.read(response, maximum=self.limits.max_json_bytes)
        payload = _parse_json(raw)
        token = payload.get("token") or payload.get("access_token")
        if (not isinstance(token, str) or not token or len(token) > 65536
                or not token.isascii() or any(ord(char) < 33 or ord(char) == 127 for char in token)):
            raise FetchError("invalid_authentication_response")
        self.token = token

    def open_object(self, kind: str, digest: str):
        if self.token is None:
            self.authenticate()
        url = f"https://{REGISTRY_HOST}/v2/{self.repository}/{kind}/{digest}"
        _validate_https(url, (REGISTRY_HOST,))
        accept = ACCEPT_MANIFEST if kind == "manifests" else "application/octet-stream"
        headers = {"Accept": accept, "Accept-Encoding": "identity",
                   "Authorization": "Bearer " + self.token}
        response = self._open(url, headers)
        if self._status(response) == 401:
            response.close()
            self.authenticate()
            headers["Authorization"] = "Bearer " + self.token
            response = self._open(url, headers)
        redirects = 0
        while self._status(response) in (301, 302, 303, 307, 308):
            location = response.headers.get("Location")
            response.close()
            if kind != "blobs" or not location or redirects >= 3:
                raise FetchError("redirect_rejected")
            url = urllib.parse.urljoin(url, location)
            _validate_https(url, CDN_HOSTS | {REGISTRY_HOST})
            # No Authorization, Cookie, Referer or challenge-derived headers go to a CDN.
            response = self._open(url, {"Accept": "application/octet-stream",
                                        "Accept-Encoding": "identity"})
            redirects += 1
        if self._status(response) != 200:
            response.close()
            raise FetchError("http_request_failed")
        return response


def _verify_cached(path: Path, digest: str, size, maximum: int) -> int:
    with _read_regular(path) as (source, actual_size):
        if actual_size > maximum or (size is not None and actual_size != size):
            raise FetchError("cache_blob_size_mismatch")
        actual = hashlib.sha256()
        count = 0
        while chunk := source.read(1024 * 1024):
            actual.update(chunk)
            count += len(chunk)
            if count > maximum:
                raise FetchError("cache_blob_size_mismatch")
        if count != actual_size or "sha256:" + actual.hexdigest() != digest:
            raise FetchError("cache_blob_digest_mismatch")
        return actual_size


class _Cache:
    def __init__(self, directory: Path, registry: _Registry):
        self.directory, self.registry, self.hits = directory, registry, 0

    def obtain(self, kind: str, digest: str, *, size=None, maximum: int) -> Path:
        path = self.directory / digest.split(":", 1)[1]
        if os.path.lexists(path):
            _verify_cached(path, digest, size, maximum)
            self.hits += 1
            return path
        with _temporary(self.directory) as (temporary, target):
            with contextlib.closing(self.registry.open_object(kind, digest)) as response:
                _, actual_size, actual_digest = self.registry.read(
                    response, expected_size=size, maximum=maximum, target=target)
            if actual_digest != digest:
                raise FetchError("download_digest_mismatch")
            target.flush()
            os.fsync(target.fileno())
            _directory(self.directory)
            try:
                os.link(temporary, path, follow_symlinks=False)
            except FileExistsError:
                # Another downloader may have completed the same blob; never replace it.
                _verify_cached(path, digest, actual_size, maximum)
        return path

    def json(self, kind: str, digest: str, *, size=None) -> tuple[dict, int]:
        path = self.obtain(kind, digest, size=size, maximum=self.registry.limits.max_json_bytes)
        with _read_regular(path) as (source, actual_size):
            raw = source.read(self.registry.limits.max_json_bytes + 1)
        if len(raw) != actual_size or "sha256:" + hashlib.sha256(raw).hexdigest() != digest:
            raise FetchError("cache_blob_digest_mismatch")
        return _parse_json(raw), actual_size


def _resolve(cache: _Cache, manifest_digest: str, architecture: str):
    manifest, total = cache.json("manifests", manifest_digest)
    resolved = manifest_digest
    if manifest.get("schemaVersion") != 2:
        raise FetchError("unsupported_manifest")
    if manifest.get("mediaType") in INDEX_TYPES:
        entries = manifest.get("manifests")
        if not isinstance(entries, list) or len(entries) > cache.registry.limits.max_layers:
            raise FetchError("invalid_image_index")
        matches = [item for item in entries if isinstance(item, dict)
                   and isinstance(item.get("platform"), dict)
                   and item["platform"].get("os") == "linux"
                   and item["platform"].get("architecture") == architecture]
        if len(matches) != 1:
            raise FetchError("image_platform_not_unique")
        descriptor = _descriptor(matches[0], MANIFEST_TYPES, cache.registry.limits.max_json_bytes)
        resolved = descriptor["digest"]
        manifest, actual_size = cache.json("manifests", resolved, size=descriptor["size"])
        total += actual_size
    if manifest.get("schemaVersion") != 2 or manifest.get("mediaType") not in MANIFEST_TYPES:
        raise FetchError("unsupported_manifest")
    return manifest, resolved, total


class _HashingReader:
    def __init__(self, source):
        self.source, self.digest, self.count = source, hashlib.sha256(), 0

    def read(self, size=-1):
        raw = self.source.read(size)
        self.digest.update(raw)
        self.count += len(raw)
        return raw


def _tar_bytes(archive, name: str, raw: bytes):
    info = tarfile.TarInfo(name)
    info.size, info.mode = len(raw), 0o600
    archive.addfile(info, io.BytesIO(raw))


def _write_archive(cache: _Cache, output: Path, repository: str, config: dict, layers: list[dict]):
    config_name = config["digest"].split(":", 1)[1] + ".json"
    layer_names = ["layers/" + layer["digest"].split(":", 1)[1] + "/layer.tar" for layer in layers]
    saved = [{"Config": config_name, "RepoTags": [], "Layers": layer_names}]
    saved_raw = json.dumps(saved, sort_keys=True, separators=(",", ":")).encode("utf-8")
    with _temporary(output.parent) as (temporary, target):
        with tarfile.open(fileobj=target, mode="w", format=tarfile.PAX_FORMAT) as archive:
            _tar_bytes(archive, "manifest.json", saved_raw)
            emitted = set()
            for descriptor, name in [(config, config_name), *zip(layers, layer_names)]:
                if name in emitted:
                    continue
                emitted.add(name)
                path = cache.directory / descriptor["digest"].split(":", 1)[1]
                with _read_regular(path) as (source, size):
                    if size != descriptor["size"]:
                        raise FetchError("cache_blob_size_mismatch")
                    info = tarfile.TarInfo(name)
                    info.size, info.mode = size, 0o600
                    checked = _HashingReader(source)
                    archive.addfile(info, checked)
                    if checked.count != size or "sha256:" + checked.digest.hexdigest() != descriptor["digest"]:
                        raise FetchError("cache_blob_digest_mismatch")
        target.flush()
        os.fsync(target.fileno())
        target.seek(0)
        archive_hash, archive_size = hashlib.sha256(), 0
        while chunk := target.read(1024 * 1024):
            archive_hash.update(chunk)
            archive_size += len(chunk)
        _directory(output.parent)
        # Hard-link publication is atomic and fails if the destination already exists.
        try:
            os.link(temporary, output, follow_symlinks=False)
        except FileExistsError:
            raise FetchError("output_already_exists") from None
    return archive_hash.hexdigest(), archive_size


def fetch_image(cache, output_archive, repository, manifest_digest, expected_image_id,
                architecture="amd64", *, http_open=None, limits=None) -> dict:
    """Return a non-sensitive receipt; http_open is a non-redirecting test hook."""
    repository = _repository(repository)
    manifest_digest, expected_image_id = _digest(manifest_digest), _digest(expected_image_id)
    architecture = _architecture(architecture)
    limits = limits or Limits()
    if (not isinstance(limits, Limits) or any(type(value) is not int or value <= 0
            for value in (limits.max_total_bytes, limits.max_layer_bytes, limits.max_json_bytes, limits.max_layers))
            or limits.timeout <= 0 or limits.max_seconds <= 0):
        raise FetchError("invalid_limits")
    try:
        cache_path = _directory(cache, create=True)
        output = _absolute(output_archive)
        _directory(output.parent, create=True)
        if os.path.lexists(output):
            raise FetchError("output_already_exists")
        registry = _Registry(repository, limits, http_open)
        storage = _Cache(cache_path, registry)
        manifest, resolved, total = _resolve(storage, manifest_digest, architecture)
        config = _descriptor(manifest.get("config"), CONFIG_TYPES, limits.max_json_bytes)
        if config["digest"] != expected_image_id:
            raise FetchError("image_id_mismatch")
        declared_layers = manifest.get("layers")
        if not isinstance(declared_layers, list) or len(declared_layers) > limits.max_layers:
            raise FetchError("layer_count_limit")
        layers = [_descriptor(layer, LAYER_TYPES, limits.max_layer_bytes) for layer in declared_layers]
        total += config["size"] + sum(layer["size"] for layer in layers)
        if total > limits.max_total_bytes:
            raise FetchError("total_size_limit")
        image_config, _ = storage.json("blobs", config["digest"], size=config["size"])
        if image_config.get("os") != "linux" or image_config.get("architecture") != architecture:
            raise FetchError("image_platform_mismatch")
        rootfs = image_config.get("rootfs")
        diff_ids = rootfs.get("diff_ids") if isinstance(rootfs, dict) else None
        if not isinstance(diff_ids, list) or rootfs.get("type") != "layers" or len(diff_ids) != len(layers):
            raise FetchError("invalid_rootfs")
        for diff_id in diff_ids:
            _digest(diff_id)
        for layer in layers:
            storage.obtain("blobs", layer["digest"], size=layer["size"], maximum=limits.max_layer_bytes)
        archive_sha, archive_size = _write_archive(storage, output, repository, config, layers)
        registry.token = None
        return {"archive_sha256": archive_sha, "archive_size": archive_size,
                "repository": repository, "manifest_digest": manifest_digest,
                "resolved_manifest_digest": resolved, "image_id": expected_image_id,
                "os": "linux", "architecture": architecture, "layer_count": len(layers),
                "compressed_layer_bytes": sum(layer["size"] for layer in layers),
                "cache_hits": storage.hits}
    except FetchError:
        raise
    except (OSError, ValueError, UnicodeError, http.client.HTTPException, urllib.error.URLError, tarfile.TarError):
        raise FetchError("image_fetch_failed") from None


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--upstream", required=True)
    parser.add_argument("--cache", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    try:
        metadata = _absolute(args.upstream)
        with _read_regular(metadata) as (source, size):
            if size > Limits().max_json_bytes:
                raise FetchError("upstream_metadata_size_limit")
            declaration = _parse_json(source.read(Limits().max_json_bytes + 1))
        result = fetch_image(args.cache, args.output, declaration.get("repository"),
                             declaration.get("manifest_digest"), declaration.get("image_id"),
                             declaration.get("architecture", "amd64"))
        print(json.dumps({"ok": True, **result}, sort_keys=True))
        return 0
    except (FetchError, OSError, ValueError, UnicodeError) as error:
        print(json.dumps({"ok": False, "error": error.code if isinstance(error, FetchError)
                          else "image_fetch_failed"}, sort_keys=True))
        return 2


if __name__ == "__main__":
    sys.exit(main())
