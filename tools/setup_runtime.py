"""Prepare the pinned official program; device configuration is never a build input."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import stat
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from extract_runtime import extract_runtime


class SetupError(ValueError):
    pass


def plain(path: Path) -> Path:
    absolute = Path(os.path.abspath(path))
    for item in (absolute, *absolute.parents):
        if item.is_symlink() or (hasattr(item, "is_junction") and item.is_junction()):
            raise SetupError("unsafe_path_link")
    return absolute


def verify_runtime(runtime: Path, image_id: str) -> dict:
    plain(runtime)
    inventory_path = plain(runtime / "runtime-inventory.json")
    if not inventory_path.is_file() or inventory_path.stat().st_size > 16 * 1024 * 1024:
        raise SetupError("runtime_inventory_missing")
    inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
    if inventory.get("source", {}).get("image_id") != image_id:
        raise SetupError("runtime_version_mismatch")
    for item in inventory["files"]:
        relative = Path(item["path"])
        if relative.is_absolute() or ".." in relative.parts:
            raise SetupError("invalid_inventory_path")
        path = runtime / relative
        plain(path.parent)
        if item["type"] == "symlink":
            if not path.is_symlink() or os.readlink(path).replace("\\", "/") != item["target"]:
                raise SetupError("runtime_link_changed")
            if not path.resolve().is_relative_to(runtime):
                raise SetupError("runtime_link_escape")
        elif item["type"] == "file":
            plain(path)
            if not path.is_file() or path.stat().st_size != item["size"]:
                raise SetupError("runtime_file_changed")
            with path.open("rb") as stream:
                if hashlib.file_digest(stream, "sha256").hexdigest() != item["sha256"]:
                    raise SetupError("runtime_file_changed")
        else:
            raise SetupError("invalid_inventory_type")
    for required in ("lib/ld-musl-x86_64.so.1", "usr/local/bin/node", "app/dist-final/app.js",
                     "usr/share/nginx/html/index.html"):
        if not (runtime / required).is_file():
            raise SetupError("required_runtime_file_missing")
    return inventory


def link_state(link: Path, target: Path) -> None:
    if link.is_symlink():
        if link.resolve() != target.resolve():
            raise SetupError("existing_state_link_mismatch")
        return
    if link.exists():
        raise SetupError("state_link_would_overwrite_file")
    link.symlink_to(os.path.relpath(target, link.parent), target_is_directory=target.is_dir())


def read_options(var: Path, *, expected_uid: int = 0) -> dict:
    path = plain(var / "runtime-options.json")
    if not path.is_file() or path.stat().st_size > 4096 or path.stat().st_uid != expected_uid:
        raise SetupError("invalid_runtime_options_file")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or set(value) != {"node_env"} or value["node_env"] not in ("production", "development"):
        raise SetupError("invalid_runtime_options")
    return value


def protect_program(base: Path, *, expected_uid: int = 0) -> None:
    """Normalize fnOS's group-writable extracted wrapper tree before use."""
    pending = [plain(base)]
    entries = []
    while pending:
        path = plain(pending.pop())
        info = path.lstat()
        if info.st_uid != expected_uid or not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
            raise SetupError("invalid_program_tree_entry")
        entries.append((path, stat.S_ISDIR(info.st_mode)))
        if stat.S_ISDIR(info.st_mode):
            pending.extend(path.iterdir())
    for path, directory in entries:
        path.chmod(0o755 if directory else 0o644)


def initialize_state(var: Path, runtime: Path, *, uid: int, gid: int) -> None:
    for folder in ("data", "config", "logs", "run", "cache", "home", "tmp"):
        path = plain(var / folder)
        path.mkdir(mode=0o700, exist_ok=True)
        if not path.is_dir():
            raise SetupError("state_path_not_directory")
        if hasattr(os, "chown"):
            os.chown(path, uid, gid)
        path.chmod(0o700)
    env_file = plain(var / "config/env")
    if not env_file.exists():
        template = (runtime / "app/.env.example").read_text(encoding="utf-8")
        lines = template.splitlines()
        lines = [line for line in lines if not line.lstrip().startswith("JWT_SECRET=")]
        lines.append("JWT_SECRET=" + secrets.token_urlsafe(48) + "aA1!")
        with env_file.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write("\n".join(lines) + "\n")
    if not env_file.is_file():
        raise SetupError("environment_not_regular_file")
    if hasattr(os, "chown"):
        os.chown(env_file, uid, gid)
    env_file.chmod(0o600)
    release = plain(var / "config/release-content.local.json")
    if not release.exists():
        with (runtime / "app/release-content.local.default.json").open("rb") as source:
            with release.open("xb") as target:
                shutil.copyfileobj(source, target)
    if hasattr(os, "chown"):
        os.chown(release, uid, gid)
    if not release.is_file():
        raise SetupError("release_configuration_not_regular_file")
    release.chmod(0o600)
    link_state(runtime / "app/data", var / "data")
    link_state(runtime / "app/config", var / "config")
    link_state(runtime / "app/.env", env_file)
    link_state(runtime / "app/logs", var / "logs")
    link_state(runtime / "app/cache", var / "cache")
    bin_folder = plain(var / "bin")
    bin_folder.mkdir(mode=0o755, exist_ok=True)
    bin_folder.chmod(0o755)
    launcher = plain(bin_folder / "node")
    import shlex
    command = [str(runtime / "lib/ld-musl-x86_64.so.1"), "--library-path",
               str(runtime / "usr/lib") + ":" + str(runtime / "lib"),
               str(runtime / "usr/local/bin/node")]
    script = "#!/bin/sh\nexec " + " ".join(shlex.quote(part) for part in command) + ' "$@"\n'
    if launcher.exists():
        if not launcher.is_file() or launcher.read_text(encoding="utf-8") != script:
            raise SetupError("existing_node_launcher_mismatch")
    else:
        with launcher.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(script)
    launcher.chmod(0o755)


def setup(base: Path, var: Path, user: str, archive: Path | None = None) -> dict:
    import pwd
    if os.geteuid() != 0:
        raise SetupError("administrator_required_for_installation")
    # fnOS supplies /var/apps aliases on some versions; resolve that platform
    # registration once, then all child paths must remain plain directories.
    base, var = plain(base.resolve(strict=True)), plain(var.resolve(strict=True))
    account = pwd.getpwnam(user)
    if account.pw_uid == 0:
        raise SetupError("application_user_must_be_unprivileged")
    if (not base.is_dir() or base.stat().st_uid != 0 or not var.is_dir()
            or var.stat().st_uid not in (0, account.pw_uid)):
        raise SetupError("invalid_installation_directories")
    # App Center applies group-write mode to the extracted wrapper, including
    # files. Only this application's root-owned program tree is normalized.
    protect_program(base)
    os.chown(var, 0, 0)
    metadata = json.loads((base / "upstream.json").read_text(encoding="utf-8"))
    runtime = var / "runtime"
    if not runtime.exists():
        if archive is None:
            from fetch_image import fetch_image
            private = plain(var / "private")
            private.mkdir(mode=0o700, exist_ok=True)
            private.chmod(0o700)
            cache = plain(private / "download-cache")
            archive = private / ("official-" + metadata["image_id"].split(":", 1)[1] + ".tar")
            if not archive.exists():
                fetch_image(cache, archive, metadata["repository"], metadata["manifest_digest"],
                            metadata["image_id"], metadata["architecture"])
        extract_runtime(archive, runtime, metadata["image_id"])
    inventory = verify_runtime(runtime, metadata["image_id"])
    initialize_state(var, runtime, uid=account.pw_uid, gid=account.pw_gid)
    options = plain(var / "runtime-options.json")
    if not options.exists():
        with options.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump({"node_env": "production"}, stream)
            stream.write("\n")
        options.chmod(0o600)
    read_options(var)
    var.chmod(0o755)
    return {"ok": True, "upstream_version": metadata["version"],
            "runtime_files_verified": inventory["file_count"],
            "personal_configuration_imported": False}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True, type=Path)
    parser.add_argument("--var", required=True, type=Path)
    parser.add_argument("--user", required=True)
    parser.add_argument("--archive", type=Path)
    args = parser.parse_args(argv)
    try:
        result = setup(args.base, args.var, args.user, args.archive)
    except Exception as exc:
        # Third-party exceptions may contain URLs, payload text or local paths.
        print(json.dumps({"ok": False, "error": type(exc).__name__}))
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
