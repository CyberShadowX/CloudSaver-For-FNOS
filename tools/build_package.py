"""Build a small native FPK from an explicit, configuration-free source allowlist."""
from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[1]
VERSION = "1.0.1"
APP_ID = "CloudSaver"
FNPACK_SHA256 = "d7af4bd716b009c58f5bcd931615f39db121e7d4b75dc759e575c4fb2879b6ee"
SOURCES = (
    "runtime/supervisor.py", "runtime/loopback.cjs", "runtime/branding.js", "runtime/lifecycle.py",
    "tools/setup_runtime.py", "tools/fetch_image.py", "tools/extract_runtime.py",
    "tools/config_integrity.py", "tools/snapshot_state.py", "upstream.json", "LICENSE",
    "docs/USER_GUIDE.md",
)
HOOK = '#!/bin/sh\nset -eu\numask 077\nexec python3 "${TRIM_APPDEST}/runtime/lifecycle.py" "$@"\n'


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def reject_links(path: Path) -> None:
    for part in (path, *path.parents):
        if part.is_symlink() or (hasattr(part, "is_junction") and part.is_junction()):
            raise ValueError("Build paths must not traverse links")


def save(path: Path, content: str | bytes | dict | list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, (dict, list)):
        content = json.dumps(content, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    if isinstance(content, str):
        content = content.replace("\r\n", "\n").encode("utf-8")
    path.write_bytes(content)


def tar_bytes(files: dict[str, bytes], *, compressed: bool) -> bytes:
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        directories = set()
        for name in files:
            directories.update(str(parent) for parent in PurePosixPath(name).parents if str(parent) != ".")
        for name in sorted(directories):
            member = tarfile.TarInfo(name + "/")
            member.type = tarfile.DIRTYPE
            member.mode = 0o755
            archive.addfile(member)
        for name, content in sorted(files.items()):
            path = PurePosixPath(name)
            if path.is_absolute() or ".." in path.parts:
                raise ValueError("Unsafe FPK archive member")
            member = tarfile.TarInfo(name)
            member.size = len(content)
            member.mode = 0o755 if name.startswith("cmd/") else 0o644
            archive.addfile(member, io.BytesIO(content))
    return gzip.compress(raw.getvalue(), compresslevel=9, mtime=0) if compressed else raw.getvalue()


def archive_files(content: bytes) -> dict[str, bytes]:
    result = {}
    with tarfile.open(fileobj=io.BytesIO(content), mode="r:*") as archive:
        for member in archive.getmembers():
            name = member.name.removeprefix("./")
            if member.isdir():
                continue
            if not member.isfile() or name in result:
                raise ValueError("Unsupported or duplicate FPK member")
            path = PurePosixPath(name)
            if path.is_absolute() or ".." in path.parts:
                raise ValueError("Unsafe FPK archive member")
            result[name] = archive.extractfile(member).read()
    return result


def normalize_fpk(path: Path) -> dict:
    outer = archive_files(path.read_bytes())
    application = archive_files(outer["app.tgz"])
    allowed = set(SOURCES) | {"ui/config", "ui/images/icon_64.png", "ui/images/icon_256.png",
                              "config/privilege", "config/resource"}
    if set(application) != allowed:
        raise ValueError("FPK application differs from its explicit source allowlist")
    expected_outer = {"manifest", "app.tgz", "ICON.PNG", "ICON_256.PNG", "LICENSE",
                      "config/privilege", "config/resource", "wizard/install"}
    expected_outer |= {"cmd/" + name for name in ("main", "install_init", "install_callback", "uninstall_init",
                      "uninstall_callback", "upgrade_init", "upgrade_callback", "config_init", "config_callback")}
    if set(outer) != expected_outer:
        raise ValueError("FPK outer archive differs from its allowlist")
    for metadata in ("config/privilege", "config/resource"):
        if application[metadata] != outer[metadata]:
            raise ValueError("fnpack platform configuration copies differ")
    manifest = outer["manifest"].decode("utf-8").replace("\r", "")
    match = re.search(r"(?m)^checksum\s*=\s*([0-9a-f]{32})\s*$", manifest)
    if not match or hashlib.md5(outer["app.tgz"]).hexdigest() != match[1]:
        raise ValueError("fnpack checksum verification failed")
    outer["app.tgz"] = tar_bytes(application, compressed=True)
    manifest = re.sub(r"(?m)^checksum\s*=.*$", "checksum = " + hashlib.md5(outer["app.tgz"]).hexdigest(), manifest)
    outer["manifest"] = manifest.encode("utf-8")
    # fnOS reads the outer FPK through gzip, as emitted by official fnpack.
    # A readable plain tar passes Python's r:* checks but App Center rejects it.
    path.write_bytes(tar_bytes(outer, compressed=True))
    verified = archive_files(path.read_bytes())
    if verified != outer:
        raise ValueError("Final FPK re-read differs")
    return {"file": path.name, "version": VERSION, "size": path.stat().st_size,
            "sha256": digest(path), "official_program_bundled": False,
            "personal_configuration_bundled": False,
            "files": [{"path": name, "size": len(value), "sha256": hashlib.sha256(value).hexdigest()}
                      for name, value in sorted(application.items())]}


def build(fnpack: Path, output: Path) -> dict:
    fnpack, output = fnpack.absolute(), output.absolute()
    reject_links(fnpack); reject_links(output)
    if digest(fnpack) != FNPACK_SHA256:
        raise ValueError("The fnpack binary differs from the pinned Windows 1.2.3 tool")
    output.mkdir(parents=True, exist_ok=True)
    final = output / f"{APP_ID}-{VERSION}-x86.fpk"
    if final.exists():
        raise ValueError("Refusing to overwrite an existing release artifact")
    work = ROOT / "build"
    work.mkdir(exist_ok=True)
    reject_links(work)
    stage = Path(tempfile.mkdtemp(prefix="native-fpk-", dir=work))
    for relative in SOURCES:
        source = ROOT / relative
        reject_links(source)
        if not source.is_file():
            raise ValueError("Required package source is missing: " + relative)
        save(stage / "app" / relative, source.read_bytes())
    for name in ("icon_64.png", "icon_256.png"):
        source = ROOT / "assets" / name
        reject_links(source)
        save(stage / "app/ui/images" / name, source.read_bytes())
    save(stage / "ICON.PNG", (ROOT / "assets/icon_64.png").read_bytes())
    save(stage / "ICON_256.PNG", (ROOT / "assets/icon_256.png").read_bytes())
    save(stage / "LICENSE", (ROOT / "LICENSE").read_bytes())
    manifest = {"appname": APP_ID, "version": VERSION, "display_name": "CloudSaver",
                "desc": "CloudSaver 原生封装，首次安装获取固定官方程序，运行不依赖 Docker。",
                "platform": "x86", "source": "thirdparty", "maintainer": "CyberShadowX",
                "distributor": "CyberShadowX", "maintainer_url": "https://github.com/CyberShadowX/CloudSaver",
                "os_min_version": "1.2.1010", "ctl_stop": "true", "desktop_uidir": "ui",
                "desktop_applaunchname": APP_ID + ".Application", "service_port": "8008",
                "checkport": "true", "disable_authorization_path": "true"}
    save(stage / "manifest", "".join(key + " = " + value + "\n" for key, value in manifest.items()))
    save(stage / "config/privilege", {"defaults": {"run-as": "root"}, "username": APP_ID, "groupname": APP_ID})
    save(stage / "config/resource", {})
    save(stage / "app/ui/config", {".url": {APP_ID + ".Application": {
        "title": "CloudSaver", "icon": "images/icon_{0}.png", "type": "url", "protocol": "http",
        "port": "8008", "url": "/", "allUsers": False, "control": {"accessPerm": "readonly"}}}})
    save(stage / "wizard/install", [{"stepTitle": "安装说明", "items": [{"type": "tips", "helpText":
        "首次安装需联网，从固定官方来源获取 CloudSaver 0.9.3 程序。运行不依赖 Docker。安装包不含个人频道、账号或网盘配置；已有部署请先备份并完成迁移验收。"}]}])
    save(stage / "cmd/main", HOOK)
    save(stage / "cmd/install_init", '#!/bin/sh\nset -eu\nif ! python3 -c "import aiohttp" >/dev/null 2>&1; then\n apt-get update\n apt-get install -y --no-install-recommends python3-aiohttp\nfi\n')
    save(stage / "cmd/install_callback", HOOK.replace('"$@"', 'prepare'))
    save(stage / "cmd/uninstall_init", HOOK.replace('"$@"', 'stop'))
    save(stage / "cmd/upgrade_init", '#!/bin/sh\nset -eu\numask 077\npython3 "${TRIM_APPDEST}/runtime/lifecycle.py" stop\nexec python3 "${TRIM_APPDEST}/tools/snapshot_state.py" --var "${TRIM_PKGVAR}"\n')
    save(stage / "cmd/upgrade_callback", HOOK.replace('"$@"', 'prepare'))
    for hook in ("uninstall_callback", "config_init", "config_callback"):
        save(stage / "cmd" / hook, "#!/bin/sh\nexit 0\n")
    subprocess.run([str(fnpack), "build", "--directory", str(stage)], cwd=stage, check=True)
    generated = stage / (APP_ID + ".fpk")
    inventory = normalize_fpk(generated)
    inventory["file"] = final.name
    with generated.open("rb") as source, final.open("xb") as target:
        while block := source.read(1024 * 1024):
            target.write(block)
    save(output / "build-inventory.json", inventory)
    save(output / "SHA256SUMS.txt", inventory["sha256"] + "  " + final.name + "\n")
    return inventory


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fnpack", required=True, type=Path)
    parser.add_argument("--output", type=Path, default=ROOT / "dist")
    arguments = parser.parse_args()
    result = build(arguments.fnpack, arguments.output)
    print(json.dumps({key: value for key, value in result.items() if key != "files"}, indent=2))
