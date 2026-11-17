"""Privileged fnOS lifecycle entry; the service itself runs as the application user."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import signal
import socket
import stat
import subprocess
import sys
import time


def identity(pid: int) -> dict | None:
    try:
        folder = Path("/proc") / str(pid)
        raw = (folder / "stat").read_text(encoding="utf-8")
        fields = raw[raw.rindex(")") + 2:].split()
        if fields[0] == "Z":
            return None
        command = (folder / "cmdline").read_bytes().split(b"\0")
        return {"pid": pid, "start_ticks": fields[19], "uid": folder.stat().st_uid,
                "command": [part.decode("utf-8") for part in command if part]}
    except (OSError, ValueError, IndexError, UnicodeError):
        return None


def matches(record: dict, base: Path, var: Path, uid: int) -> bool:
    current = identity(record.get("pid", -1))
    return bool(current and current["start_ticks"] == record.get("start_ticks")
                and current["uid"] == uid
                and str(base / "runtime/supervisor.py") in current["command"]
                and str(base) in current["command"] and str(var) in current["command"])


def atomic_json(path: Path, value: dict) -> None:
    temporary = path.with_name(path.name + ".pending")
    with temporary.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.chmod(0o600)
    os.replace(temporary, path)


def read_record(path: Path) -> dict | None:
    if path.is_symlink():
        raise RuntimeError("unsafe_lifecycle_record")
    if not path.exists():
        return None
    metadata = path.stat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != 0 or metadata.st_size > 16384:
        raise RuntimeError("unsafe_lifecycle_record")
    return json.loads(path.read_text(encoding="utf-8"))


def reachable(port: int) -> bool:
    with socket.socket() as connection:
        connection.settimeout(0.5)
        return connection.connect_ex(("127.0.0.1", port)) == 0


def owns_port(pid: int, port: int) -> bool:
    inodes = set()
    try:
        for descriptor in (Path("/proc") / str(pid) / "fd").iterdir():
            try:
                value = os.readlink(descriptor)
                if value.startswith("socket:["):
                    inodes.add(value[8:-1])
            except OSError:
                continue
        listeners = []
        for table in ("tcp", "tcp6"):
            for line in (Path("/proc/net") / table).read_text(encoding="ascii").splitlines()[1:]:
                fields = line.split()
                if fields[3] == "0A" and int(fields[1].split(":")[1], 16) == port:
                    listeners.append(fields[9])
        return bool(listeners) and all(inode in inodes for inode in listeners)
    except (OSError, ValueError, IndexError):
        return False


@contextmanager
def locked(var: Path):
    import fcntl
    folder = var / "private"
    if folder.is_symlink():
        raise RuntimeError("unsafe_private_directory")
    folder.mkdir(mode=0o700, exist_ok=True)
    if not folder.is_dir() or folder.stat().st_uid != 0:
        raise RuntimeError("unsafe_private_directory")
    folder.chmod(0o700)
    descriptor = os.open(folder / "lifecycle.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "a") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield folder / "service.json"


def start(base: Path, var: Path, account, record_path: Path, port: int, backend_port: int) -> None:
    pending = record_path.with_name(record_path.name + ".pending")
    if pending.exists() or pending.is_symlink():
        raise RuntimeError("unfinished_lifecycle_record_transaction")
    old = read_record(record_path)
    if old and matches(old, base, var, account.pw_uid):
        if reachable(port) and owns_port(old["pid"], port):
            return
        raise RuntimeError("existing_service_unhealthy_stop_before_restart")
    if old and identity(old.get("pid", -1)):
        raise RuntimeError("recorded_process_identity_changed")
    if old:
        # A dead supervisor alone does not prove its descendants stopped.
        stop(base, var, account, record_path)
    if reachable(port) or reachable(backend_port):
        raise RuntimeError("application_port_in_use")
    sys.path.insert(0, str(base / "tools"))
    from setup_runtime import setup, read_options
    setup(base, var, account.pw_name)
    options = read_options(var)
    receipt = var / "run/shutdown.json"
    if receipt.is_symlink():
        raise RuntimeError("unsafe_shutdown_receipt")
    if receipt.exists():
        receipt.unlink()
    environment = os.environ.copy()
    environment.update(HOME=str(var / "home"), USER=account.pw_name, LOGNAME=account.pw_name,
                       TMPDIR=str(var / "tmp"), PATH=str(var / "bin") + ":/usr/bin:/bin")
    command = [sys.executable, "-u", str(base / "runtime/supervisor.py"), "--base", str(base),
               "--var", str(var), "--port", str(port), "--backend-port", str(backend_port),
               "--node-env", options["node_env"]]
    log = var / "private/lifecycle.log"
    if log.is_symlink():
        raise RuntimeError("unsafe_lifecycle_log")
    gate = "import os,sys\nif os.read(0,1)!=b'1': raise SystemExit(1)\nos.execv(sys.argv[1],sys.argv[1:])\n"
    with log.open("ab") as output:
        log.chmod(0o600)
        # The child cannot execute application code until its durable identity
        # record is published. Parent failure closes the pipe and denies start.
        process = subprocess.Popen([sys.executable, "-c", gate, *command], cwd=var,
                                   env=environment, stdin=subprocess.PIPE,
                                   stdout=output, stderr=output, start_new_session=True,
                                   user=account.pw_uid, group=account.pw_gid, extra_groups=[])
    try:
        current = identity(process.pid)
        if current is None:
            raise RuntimeError("service_exited_during_spawn")
        record = {"pid": process.pid, "start_ticks": current["start_ticks"], "uid": account.pw_uid,
                  "port": port, "backend_port": backend_port}
        atomic_json(record_path, record)
    except BaseException:
        process.stdin.close()
        process.wait(timeout=10)
        raise
    process.stdin.write(b"1")
    process.stdin.flush()
    process.stdin.close()
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("service_start_failed_check_private_log")
        if matches(record, base, var, account.pw_uid) and reachable(port) and owns_port(record["pid"], port):
            return
        time.sleep(0.5)
    stop(base, var, account, record_path)
    raise RuntimeError("service_start_timed_out")


def stop(base: Path, var: Path, account, record_path: Path) -> None:
    record = read_record(record_path)
    if record is None:
        pending = record_path.with_name(record_path.name + ".pending")
        if pending.exists() or pending.is_symlink():
            raise RuntimeError("unfinished_lifecycle_record_transaction")
        return
    if matches(record, base, var, account.pw_uid):
        os.kill(record["pid"], signal.SIGTERM)
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline and matches(record, base, var, account.pw_uid):
            time.sleep(0.25)
        if matches(record, base, var, account.pw_uid):
            raise RuntimeError("service_still_stopping_no_forced_kill")
    elif identity(record.get("pid", -1)):
        raise RuntimeError("recorded_process_identity_changed")
    receipt = var / "run/shutdown.json"
    if receipt.is_symlink() or not receipt.is_file() or receipt.stat().st_size > 16384:
        raise RuntimeError("shutdown_receipt_missing")
    result = json.loads(receipt.read_text(encoding="utf-8"))
    if not result.get("clean") or result.get("supervisor_pid") != record["pid"]:
        raise RuntimeError("shutdown_not_confirmed")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("start", "stop", "status", "prepare"))
    parser.add_argument("--base", type=Path, default=os.environ.get("TRIM_APPDEST"))
    parser.add_argument("--var", type=Path, default=os.environ.get("TRIM_PKGVAR"))
    parser.add_argument("--user", default=os.environ.get("TRIM_USERNAME", "CloudSaver"))
    parser.add_argument("--port", type=int, default=8008)
    parser.add_argument("--backend-port", type=int, default=18009)
    args = parser.parse_args(argv)
    try:
        import pwd
        if os.geteuid() != 0 or args.base is None or args.var is None or args.user != "CloudSaver":
            raise RuntimeError("invalid_lifecycle_context")
        base, var = args.base.resolve(strict=True), args.var.resolve(strict=True)
        account = pwd.getpwnam(args.user)
        if account.pw_uid == 0 or not base.is_dir() or not var.is_dir():
            raise RuntimeError("invalid_application_user_or_paths")
        with locked(var) as record_path:
            if args.action == "status":
                record = read_record(record_path)
                return 0 if record and matches(record, base, var, account.pw_uid) and reachable(record["port"]) and owns_port(record["pid"], record["port"]) else 3
            if args.action == "stop":
                stop(base, var, account, record_path)
            elif args.action == "prepare":
                sys.path.insert(0, str(base / "tools"))
                from setup_runtime import setup
                setup(base, var, args.user)
            else:
                start(base, var, account, record_path, args.port, args.backend_port)
        return 0
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
