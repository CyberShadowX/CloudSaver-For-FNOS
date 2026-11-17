"""Append a verified, private configuration checkpoint after the service stops."""
from __future__ import annotations

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import shutil
import stat
import sys
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config_integrity import snapshot_directory, compare_directories
from setup_runtime import read_options


def snapshot_locked(var: Path) -> dict:
    if os.geteuid() != 0:
        raise RuntimeError("administrator_required")
    var = var.resolve(strict=True)
    private = var / "private"
    if private.is_symlink() or private.stat().st_uid != 0:
        raise RuntimeError("unsafe_private_directory")
    record = private / "service.json"
    if record.is_symlink():
        raise RuntimeError("unsafe_service_record")
    pending = record.with_name(record.name + ".pending")
    if pending.exists() or pending.is_symlink():
        raise RuntimeError("unfinished_lifecycle_record_transaction")
    if record.exists():
        state = json.loads(record.read_text(encoding="utf-8"))
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "runtime"))
        from lifecycle import identity
        if identity(state["pid"]):
            raise RuntimeError("service_must_be_stopped")
        receipt = var / "run/shutdown.json"
        if receipt.is_symlink() or not receipt.is_file():
            raise RuntimeError("clean_shutdown_not_confirmed")
        result = json.loads(receipt.read_text(encoding="utf-8"))
        if not result.get("clean") or result.get("supervisor_pid") != state["pid"]:
            raise RuntimeError("clean_shutdown_not_confirmed")
    options = read_options(var)
    options_bytes = (var / "runtime-options.json").read_bytes()
    expected = {name: snapshot_directory(var / name) for name in ("data", "config")}
    parent = private / "upgrade-backups"
    if parent.is_symlink():
        raise RuntimeError("unsafe_backup_directory")
    parent.mkdir(mode=0o700, exist_ok=True)
    target = parent / datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d-%H%M%S-%f")
    target.mkdir(mode=0o700)
    for name in ("data", "config"):
        destination = target / name
        destination.mkdir(mode=0o700)
        for directory in expected[name]["directories"]:
            (destination / directory).mkdir(mode=0o700, parents=True, exist_ok=True)
        for item in expected[name]["files"]:
            source = var / name / item["path"]
            output = destination / item["path"]
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            with os.fdopen(os.open(source, flags), "rb") as incoming, output.open("xb") as outgoing:
                if not stat.S_ISREG(os.fstat(incoming.fileno()).st_mode):
                    raise RuntimeError("backup_input_not_regular")
                shutil.copyfileobj(incoming, outgoing)
            output.chmod(0o600)
        if snapshot_directory(var / name) != expected[name]:
            raise RuntimeError("configuration_changed_during_backup")
        if not compare_directories(var / name, destination)["equal"]:
            raise RuntimeError("backup_verification_failed")
    with (target / "runtime-options.json").open("xb") as stream:
        stream.write(options_bytes)
    (target / "runtime-options.json").chmod(0o600)
    if ((var / "runtime-options.json").read_bytes() != options_bytes
            or (target / "runtime-options.json").read_bytes() != options_bytes
            or read_options(var) != options):
        raise RuntimeError("runtime_options_changed_during_backup")
    metadata = {"complete": True, "service_stopped": True, "configuration_verified": True,
                "runtime_options_included": True,
                "folders": {name: len(expected[name]["files"]) for name in expected}}
    (target / "checkpoint.json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    return metadata


def snapshot(var: Path) -> dict:
    var = var.resolve(strict=True)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "runtime"))
    from lifecycle import locked
    with locked(var):
        return snapshot_locked(var)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--var", required=True, type=Path)
    args = parser.parse_args()
    try:
        print(json.dumps(snapshot(args.var)))
    except Exception as exc:
        print(json.dumps({"complete": False, "error": type(exc).__name__}))
        raise SystemExit(1)
