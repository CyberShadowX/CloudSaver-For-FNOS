"""Extract an authenticated, pristine Docker image into a private native runtime.

This tool never reads a container or a mounted application directory and never
executes the extracted files.  Its input must be ``docker image save`` output.
The caller must obtain the expected image ID from a trusted upstream source.
Only application programs, Node, shared libraries and the public web UI survive
the allowlist.  Device configuration is imported separately after installation.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import gzip
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import tarfile
from typing import BinaryIO, Iterator


class ExtractionError(ValueError):
    """The image cannot be authenticated or safely materialized."""


_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_SO = re.compile(r".+\.so(?:\..*)?\Z")
_RESERVED = re.compile(r"(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?\Z", re.I)
_STATIC = PurePosixPath("usr/share/nginx/html")
_NODE = PurePosixPath("usr/local/bin/node")
_LIBS = (PurePosixPath("lib"), PurePosixPath("usr/lib"))
_CHUNK = 1024 * 1024


def _linked(path: Path) -> bool:
    return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())


def _check_ancestors(path: Path) -> None:
    for item in (path, *path.parents):
        if _linked(item):
            raise ExtractionError("Paths must not traverse symbolic links or junctions")


def _name(value: str, *, allow_root: bool = False) -> PurePosixPath:
    if not isinstance(value, str) or not value or "\x00" in value or "\\" in value:
        raise ExtractionError("Invalid archive path")
    if value.startswith("/") or ":" in value:
        raise ExtractionError("Absolute paths and drive/stream names are forbidden")
    parts = value.split("/")
    if ".." in parts:
        raise ExtractionError("Parent traversal in archive path")
    result = PurePosixPath(*[p for p in parts if p and p != "."])
    if result == PurePosixPath(".") and not allow_root:
        raise ExtractionError("Archive path must name a member")
    return result


def _portable(path: PurePosixPath) -> None:
    for part in path.parts:
        if (part.endswith((".", " ")) or any(c in part for c in '<>:"\\|?*')
                or any(ord(c) < 32 for c in part) or _RESERVED.fullmatch(part)):
            raise ExtractionError("Selected runtime path is not portable")


def _private(path: PurePosixPath) -> bool:
    if path.parts[:2] in (("app", "data"), ("app", "config")):
        return True
    name = path.name.lower()
    if name.startswith(".env") and name != ".env.example":
        return True
    return bool(re.search(r"\.(?:sqlite(?:3)?|db)(?:[-.].*)?\Z", name)
                or name.endswith(".log"))


def _selected(path: PurePosixPath, *, directory: bool = False) -> bool:
    if path == PurePosixPath(".") or _private(path):
        return False
    if path == PurePosixPath("app") or PurePosixPath("app") in path.parents:
        return True
    if path == _NODE or path == _STATIC or _STATIC in path.parents:
        return True
    if any(path == library or library in path.parents for library in _LIBS):
        return directory or bool(_SO.fullmatch(path.name))
    return directory and (path in _NODE.parents or path in _STATIC.parents
                          or any(path in lib.parents for lib in _LIBS))


def _catalog(package: tarfile.TarFile) -> dict[str, tarfile.TarInfo]:
    result: dict[str, tarfile.TarInfo] = {}
    for member in package.getmembers():
        path = _name(member.name, allow_root=True)
        if path == PurePosixPath("."):
            if member.isdir():
                continue
            raise ExtractionError("Invalid outer archive root")
        canonical = path.as_posix()
        if canonical in result and not (member.isdir() and result[canonical].isdir()):
            raise ExtractionError("Duplicate outer archive member")
        result[canonical] = member
    return result


def _regular(catalog: dict[str, tarfile.TarInfo], name: str) -> tarfile.TarInfo:
    path = _name(name).as_posix()
    member = catalog.get(path)
    if member is None or not member.isfile():
        raise ExtractionError("Image metadata and layers must be regular archive members")
    return member


def _read_small(package: tarfile.TarFile, member: tarfile.TarInfo, limit: int) -> bytes:
    if member.size > limit:
        raise ExtractionError("Image metadata exceeds its size limit")
    stream = package.extractfile(member)
    if stream is None:
        raise ExtractionError("Missing image metadata")
    with stream:
        content = stream.read(limit + 1)
    if len(content) != member.size or len(content) > limit:
        raise ExtractionError("Truncated image metadata")
    return content


@contextmanager
def _layer_bytes(package: tarfile.TarFile, member: tarfile.TarInfo) -> Iterator[BinaryIO]:
    raw = package.extractfile(member)
    if raw is None:
        raise ExtractionError("Missing layer bytes")
    with raw:
        prefix = raw.read(2)
        raw.seek(0)
        if prefix == b"\x1f\x8b":
            with gzip.GzipFile(fileobj=raw, mode="rb") as decoded:
                yield decoded
        else:
            yield raw


def _stream_hash(stream: BinaryIO) -> str:
    digest = hashlib.sha256()
    while block := stream.read(_CHUNK):
        digest.update(block)
    return digest.hexdigest()


def _authenticated_layers(package: tarfile.TarFile, expected: str) -> tuple[list[tarfile.TarInfo], list[str]]:
    catalog = _catalog(package)
    manifest = json.loads(_read_small(package, _regular(catalog, "manifest.json"), 1024 * 1024))
    if not isinstance(manifest, list) or len(manifest) != 1 or not isinstance(manifest[0], dict):
        raise ExtractionError("Exactly one image is required")
    image = manifest[0]
    if not isinstance(image.get("Config"), str):
        raise ExtractionError("Missing image configuration reference")
    config_bytes = _read_small(package, _regular(catalog, image["Config"]), 8 * 1024 * 1024)
    if "sha256:" + hashlib.sha256(config_bytes).hexdigest() != expected:
        raise ExtractionError("Image configuration digest mismatch")
    config = json.loads(config_bytes)
    rootfs = config.get("rootfs") if isinstance(config, dict) else None
    diffs = rootfs.get("diff_ids") if isinstance(rootfs, dict) else None
    layer_names = image.get("Layers")
    if (not isinstance(layer_names, list) or not layer_names
            or not all(isinstance(x, str) for x in layer_names)
            or not isinstance(diffs, list) or len(diffs) != len(layer_names)
            or not all(isinstance(x, str) and _DIGEST.fullmatch(x) for x in diffs)):
        raise ExtractionError("Layer references and rootfs diff IDs do not match")
    layers = [_regular(catalog, name) for name in layer_names]
    for member, expected_diff in zip(layers, diffs):
        with _layer_bytes(package, member) as content:
            if "sha256:" + _stream_hash(content) != expected_diff:
                raise ExtractionError("Uncompressed layer digest mismatch")
    return layers, diffs


def _inside(root: Path, path: Path) -> None:
    if path == root or not path.is_relative_to(root):
        raise ExtractionError("Operation outside the new runtime directory")
    _check_ancestors(path.parent)


def _remove(root: Path, path: Path) -> None:
    _inside(root, path)
    if _linked(path) or path.is_file():
        path.unlink()
    elif path.exists():
        # Resolve before any recursive removal, without following a link target.
        actual = path.resolve(strict=True)
        if actual == root or not actual.is_relative_to(root):
            raise ExtractionError("Unsafe recursive removal target")
        shutil.rmtree(path)


def _directories(root: Path, relative: PurePosixPath) -> Path:
    current = root
    for part in relative.parts:
        current = current / part
        _inside(root, current)
        if _linked(current):
            raise ExtractionError("A runtime write would traverse a link")
        if current.exists() and not current.is_dir():
            _remove(root, current)
        current.mkdir(mode=0o755, exist_ok=True)
        # mkdir's mode is filtered by the operator's umask (often 077).
        # Only directories in this newly created runtime tree are normalized.
        current.chmod(0o755)
    return current


def _target(root: Path, path: PurePosixPath) -> Path:
    _portable(path)
    _directories(root, path.parent)
    result = root.joinpath(*path.parts)
    _inside(root, result)
    return result


def _link_target(path: PurePosixPath, value: str, *, symbolic: bool) -> PurePosixPath:
    if not isinstance(value, str) or not value or "\x00" in value or "\\" in value or ":" in value:
        raise ExtractionError("Invalid link target")
    parts = [] if value.startswith("/") or not symbolic else list(path.parent.parts)
    for part in value.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if not parts:
                raise ExtractionError("Link escapes the runtime root")
            parts.pop()
        else:
            parts.append(part)
    result = PurePosixPath(*parts)
    _portable(result)
    if not _selected(result, directory=True):
        raise ExtractionError("Link points outside permitted runtime content")
    return result


def _whiteout(root: Path, path: PurePosixPath) -> None:
    parent = path.parent
    if path.name == ".wh..wh..opq":
        if not _selected(parent, directory=True):
            return
        directory = root.joinpath(*parent.parts)
        _inside(root, directory)
        if _linked(directory):
            raise ExtractionError("Opaque directory is a link")
        if directory.is_dir():
            for child in directory.iterdir():
                _remove(root, child)
    else:
        filename = path.name.removeprefix(".wh.")
        if filename in ("", ".", ".."):
            raise ExtractionError("Invalid whiteout name")
        victim = parent / filename
        if _selected(victim, directory=True):
            _portable(victim)
            _remove(root, root.joinpath(*victim.parts))


def _apply_layer(package: tarfile.TarFile, layer: tarfile.TarInfo, root: Path) -> None:
    with _layer_bytes(package, layer) as content, tarfile.open(fileobj=content, mode="r:") as source:
        members = source.getmembers()
        planned: list[tuple[tarfile.TarInfo, PurePosixPath]] = []
        names: dict[str, tarfile.TarInfo] = {}
        for member in members:
            path = _name(member.name, allow_root=True)
            if path == PurePosixPath("."):
                if member.isdir():
                    continue
                raise ExtractionError("Invalid layer root member")
            canonical = path.as_posix()
            if canonical in names and not (member.isdir() and names[canonical].isdir()):
                raise ExtractionError("Duplicate layer member")
            names[canonical] = member
            if path.name.startswith(".wh."):
                if not member.isfile() or member.size != 0:
                    raise ExtractionError("Whiteouts must be empty regular files")
                continue
            if not _selected(path, directory=member.isdir()):
                continue
            _portable(path)
            if not (member.isdir() or member.isfile() or member.issym() or member.islnk()):
                raise ExtractionError("Special runtime members are forbidden")
            if member.issym() or member.islnk():
                _link_target(path, member.linkname, symbolic=member.issym())
            planned.append((member, path))
        # Overlay whiteouts apply to lower layers, regardless of tar entry order.
        for member in members:
            path = _name(member.name, allow_root=True)
            if path.name.startswith(".wh."):
                _whiteout(root, path)
        hardlinks: list[tuple[tarfile.TarInfo, PurePosixPath]] = []
        for member, path in planned:
            if member.isdir():
                _directories(root, path)
                continue
            if member.islnk():
                hardlinks.append((member, path))
                continue
            destination = _target(root, path)
            _remove(root, destination)
            if member.issym():
                link = _link_target(path, member.linkname, symbolic=True)
                # Rebase permitted absolute image links into this private bundle.
                relative = os.path.relpath(root.joinpath(*link.parts), destination.parent)
                destination.symlink_to(relative)
            else:
                original = source.extractfile(member)
                if original is None:
                    raise ExtractionError("Missing regular runtime file bytes")
                with original, destination.open("xb") as copied:
                    shutil.copyfileobj(original, copied, _CHUNK)
                if destination.stat().st_size != member.size:
                    raise ExtractionError("Truncated runtime member")
                destination.chmod(0o755 if member.mode & 0o111 else 0o644)
        while hardlinks:
            remaining: list[tuple[tarfile.TarInfo, PurePosixPath]] = []
            for member, path in hardlinks:
                original_relative = _link_target(path, member.linkname, symbolic=False)
                original = root.joinpath(*original_relative.parts)
                _inside(root, original)
                _check_ancestors(original)
                if not original.is_file():
                    remaining.append((member, path))
                    continue
                destination = _target(root, path)
                if original == destination:
                    raise ExtractionError("A hardlink cannot refer to itself")
                _remove(root, destination)
                # Copy instead of creating aliases that later layers could mutate.
                shutil.copyfile(original, destination)
                destination.chmod(0o755 if original.stat().st_mode & 0o111 else 0o644)
            if len(remaining) == len(hardlinks):
                raise ExtractionError("Missing or cyclic runtime hardlink target")
            hardlinks = remaining


def _inventory(root: Path) -> list[dict]:
    entries: list[dict] = []
    for current, directories, files in os.walk(root, followlinks=False):
        directory = Path(current)
        for name in sorted([*directories, *files]):
            path = directory / name
            relative = path.relative_to(root).as_posix()
            if _linked(path):
                value = os.readlink(path).replace("\\", "/")
                _link_target(PurePosixPath(relative), value, symbolic=True)
                encoded = value.encode("utf-8")
                entries.append({"path": relative, "type": "symlink", "target": value,
                                "size": len(encoded), "sha256": hashlib.sha256(encoded).hexdigest()})
            elif path.is_file():
                with path.open("rb") as stream:
                    digest = _stream_hash(stream)
                entries.append({"path": relative, "type": "file", "size": path.stat().st_size,
                                "sha256": digest})
    return sorted(entries, key=lambda item: item["path"])


def extract_runtime(archive: Path | str, output: Path | str, expected_image_id: str) -> dict:
    """Authenticate all layers, then create a new, allowlisted native runtime.

    Existing output directories (including empty ones) are rejected.  On an
    extraction error a partial new directory may remain; it is never reused or
    treated as a completed runtime.  Completion requires runtime-inventory.json.
    """
    if not isinstance(expected_image_id, str) or not _DIGEST.fullmatch(expected_image_id):
        raise ExtractionError("An explicit SHA-256 image ID is required")
    source = Path(os.path.abspath(archive))
    destination = Path(os.path.abspath(output))
    _check_ancestors(source)
    _check_ancestors(destination)
    if not source.is_file() or not stat.S_ISREG(source.stat().st_mode):
        raise ExtractionError("Input must be a regular image-save archive")
    if destination.exists() or _linked(destination):
        raise ExtractionError("Output must not already exist")
    if not destination.parent.is_dir():
        raise ExtractionError("Output parent directory must already exist")
    with source.open("rb") as content:
        archive_digest = _stream_hash(content)
    with tarfile.open(source, mode="r:*") as package:
        layers, diffs = _authenticated_layers(package, expected_image_id)
        # Digest failures cannot leave a partially materialized runtime.
        destination.mkdir(mode=0o755)
        root = destination.resolve(strict=True)
        _check_ancestors(root)
        root.chmod(0o755)
        for layer in layers:
            _apply_layer(package, layer, root)
    files = _inventory(root)
    inventory = {
        "inventory_version": 1,
        "source": {"image_id": expected_image_id, "archive_sha256": archive_digest,
                   "archive_bytes": source.stat().st_size, "rootfs_diff_ids": diffs},
        "file_count": len(files),
        "regular_bytes": sum(entry["size"] for entry in files if entry["type"] == "file"),
        "files": files,
    }
    with (root / "runtime-inventory.json").open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(inventory, stream, ensure_ascii=True, sort_keys=True, indent=2)
        stream.write("\n")
    return inventory


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--image-id", required=True)
    args = parser.parse_args(argv)
    try:
        result = extract_runtime(args.archive, args.output, args.image_id)
    except (ExtractionError, OSError, tarfile.TarError, EOFError, json.JSONDecodeError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=True))
        return 2
    print(json.dumps({"ok": True, "image_id": args.image_id,
                      "file_count": result["file_count"], "regular_bytes": result["regular_bytes"],
                      "inventory": str(args.output / "runtime-inventory.json")}, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
