"""Run CloudSaver directly on Linux and expose its existing web application.

The official program is obtained separately, not bundled by this module. The
caller prepares its configuration/data links and launches this supervisor as
the dedicated application user. No credentials or migrations are created here.
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
import inspect
import json
import logging
import mimetypes
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
from urllib.parse import unquote

import aiohttp
from aiohttp import web
from multidict import CIMultiDict
from yarl import URL


MAX_BODY = 128 * 1024 * 1024
MAX_BRANDING_HTML = 2 * 1024 * 1024
BRANDING_SCRIPT_URL = "/_CloudSaver/branding.js"
BRANDING_SCRIPT_TAG = b'<script defer src="/_CloudSaver/branding.js"></script>'
FRONTEND_SHUTDOWN_TIMEOUT = 5.0
HOP_HEADERS = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "transfer-encoding", "upgrade",
})


def _is_link(path: Path) -> bool:
    return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())


def _private_directory(path: Path) -> Path:
    if _is_link(path):
        raise ValueError("Runtime output directory cannot be a link")
    path.mkdir(mode=0o700, exist_ok=True)
    if not path.is_dir():
        raise ValueError("Runtime output directory is unavailable")
    path.chmod(0o700)
    return path


def _private_file(path: Path):
    if _is_link(path):
        raise ValueError("Runtime log cannot be a link")
    flags = (os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
             | getattr(os, "O_NONBLOCK", 0))
    descriptor = os.open(path, flags, 0o600)
    info = os.fstat(descriptor)
    if not stat.S_ISREG(info.st_mode):
        os.close(descriptor)
        raise ValueError("Runtime log must be a regular file")
    if os.name == "posix" and info.st_uid != os.geteuid():
        os.close(descriptor)
        raise ValueError("Runtime log has an unexpected owner")
    os.fchmod(descriptor, 0o600) if hasattr(os, "fchmod") else path.chmod(0o600)
    return os.fdopen(descriptor, "a", encoding="utf-8", buffering=1)


def safe_logger(var: Path) -> logging.Logger:
    logger = logging.Logger("CloudSaver.native", level=logging.INFO)
    logger.propagate = False
    handler = logging.StreamHandler(_private_file(_private_directory(var / "logs") / "service.log"))
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    logger.addHandler(handler)
    return logger


def _close_logger(logger: logging.Logger) -> None:
    for handler in list(logger.handlers):
        stream = getattr(handler, "stream", None)
        handler.close()
        if stream is not None:
            stream.close()
        logger.removeHandler(handler)


@dataclass(frozen=True)
class Config:
    base: Path
    var: Path
    port: int = 8008
    backend_port: int = 18009
    host: str = "0.0.0.0"
    startup_timeout: float = 90.0
    shutdown_timeout: float = 20.0
    capture_upstream: bool = False
    node_env: str = "production"

    def __post_init__(self):
        if not (1 <= self.port <= 65535 and 1 <= self.backend_port <= 65535):
            raise ValueError("Invalid service port")
        if self.port == self.backend_port:
            raise ValueError("Frontend and backend ports must differ")
        if self.startup_timeout <= 0 or self.shutdown_timeout <= 0:
            raise ValueError("Invalid process timeout")
        if self.node_env not in ("production", "development"):
            raise ValueError("Invalid Node environment")
        # fnOS supplies canonical application links under /var/apps. Resolve
        # those once; log/run children are separately checked before writing.
        object.__setattr__(self, "base", Path(self.base).resolve(strict=True))
        object.__setattr__(self, "var", Path(self.var).resolve(strict=True))

    @property
    def runtime(self) -> Path:
        return self.var / "runtime"

    @property
    def frontend(self) -> Path:
        return self.runtime / "usr/share/nginx/html"

    def node_launcher(self) -> Path:
        folder = self.var / "bin"
        launcher = folder / ("node.exe" if os.name == "nt" else "node")
        try:
            if (_is_link(folder) or _is_link(launcher) or not folder.is_dir()
                    or not stat.S_ISREG(launcher.lstat().st_mode)
                    or launcher.resolve(strict=True).parent != folder.resolve(strict=True)
                    or not os.access(launcher, os.R_OK | (os.X_OK if os.name == "posix" else 0))):
                raise ValueError("Private native Node launcher is invalid")
        except OSError:
            raise ValueError("Private native Node launcher is invalid") from None
        return launcher

    def command(self) -> list[str]:
        self.node_launcher()
        runtime = self.runtime.resolve(strict=True)
        relative = ("lib/ld-musl-x86_64.so.1", "usr/local/bin/node", "app/dist-final/app.js",
                    "app/node_modules/dotenv/config.js")
        paths = [(runtime / part).resolve(strict=True) for part in relative]
        if any(not path.is_file() or not path.is_relative_to(runtime) for path in paths):
            raise ValueError("Official runtime program path is invalid")
        preload = (self.base / "runtime/loopback.cjs").resolve(strict=True)
        if not preload.is_file() or not preload.is_relative_to(self.base):
            raise ValueError("Native preload is invalid")
        libraries = [str(runtime / "usr/lib"), str(runtime / "lib")]
        return [str(paths[0]), "--library-path", os.pathsep.join(libraries),
                str(paths[1]), "-r", str(paths[3]), "-r", str(preload), str(paths[2])]

    def environment(self) -> dict[str, str]:
        environment = os.environ.copy()
        environment.pop("NODE_OPTIONS", None)
        environment.pop("NODE_PATH", None)
        environment.update(PORT=str(self.backend_port), NODE_ENV=self.node_env,
                           DOTENV_CONFIG_PATH=str(self.runtime / "app/config/env"),
                           CLOUDSAVER_BACKEND_PORT=str(self.backend_port),
                           CLOUDSAVER_NODE_LAUNCHER=str(self.node_launcher()),
                           NODE_TLS_REJECT_UNAUTHORIZED="1")
        ca = Path("/etc/ssl/certs/ca-certificates.crt")
        if ca.is_file():
            environment.setdefault("NODE_EXTRA_CA_CERTS", str(ca))
            environment.setdefault("SSL_CERT_FILE", str(ca))
        return environment


def process_start_time(pid: int, proc: Path = Path("/proc")) -> str | None:
    try:
        value = (proc / str(pid) / "stat").read_text(encoding="ascii")
        return value[value.rindex(")") + 2:].split()[19]
    except (OSError, ValueError, IndexError):
        return None


def _process_group_members(group: int) -> list[int]:
    """Find our private Linux session's members, refusing a foreign identity."""
    found = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdecimal():
            continue
        try:
            value = (entry / "stat").read_text(encoding="ascii")
            fields = value[value.rindex(")") + 2:].split()
            if int(fields[2]) != group:
                continue
            if entry.stat().st_uid != os.geteuid() or int(fields[3]) != group:
                raise RuntimeError("Backend process group identity changed")
            # Zombies have already stopped accessing application data.
            if fields[0] != "Z":
                found.append(int(entry.name))
        except (OSError, ValueError, IndexError):
            continue
    return found


def linux_listener_owned(pid: int, port: int, proc: Path = Path("/proc")) -> bool:
    """Require every TCP listener on this port to be this PID's IPv4 loopback.

    This is Linux-only evidence. A successful TCP connection alone is not
    accepted as proof that our Node process owns the backend listener.
    """
    try:
        descriptors = (proc / str(pid) / "fd").iterdir()
        owned = set()
        for descriptor in descriptors:
            try:
                target = os.readlink(descriptor)
            except OSError:
                continue
            if target.startswith("socket:[") and target.endswith("]"):
                owned.add(target[8:-1])
        listeners = []
        for family in ("tcp", "tcp6"):
            table = proc / "net" / family
            if family == "tcp6" and not table.exists():
                continue
            for line in table.read_text(encoding="ascii").splitlines()[1:]:
                fields = line.split()
                address, local_port = fields[1].split(":")
                if fields[3] == "0A" and int(local_port, 16) == port:
                    listeners.append((family, address, fields[9]))
        return bool(listeners) and all(
            family == "tcp" and address == "0100007F" and inode in owned
            for family, address, inode in listeners
        )
    except (OSError, ValueError, IndexError):
        return False


class ManagedBackend:
    def __init__(self, process: asyncio.subprocess.Process, start_time: str | None, log_file=None):
        self.process = process
        self.start_time = start_time
        self.log_file = log_file

    @property
    def pid(self):
        return self.process.pid

    @property
    def returncode(self):
        return self.process.returncode

    @classmethod
    async def spawn(cls, config: Config):
        output = _private_file(_private_directory(config.var / "logs") / "upstream.log") \
            if config.capture_upstream else None
        options = {"start_new_session": True} if os.name == "posix" else {
            "creationflags": subprocess.CREATE_NEW_PROCESS_GROUP,
        }
        try:
            process = await asyncio.create_subprocess_exec(
                *config.command(), cwd=config.runtime / "app", env=config.environment(),
                stdout=output if output is not None else subprocess.DEVNULL,
                stderr=output if output is not None else subprocess.DEVNULL, **options,
            )
        except BaseException:
            if output is not None:
                output.close()
            raise
        return cls(process, process_start_time(process.pid), output)

    async def wait(self):
        return await self.process.wait()

    def terminate(self):
        if os.name == "posix":
            members = _process_group_members(self.pid)
            if not members:
                return
            # A newly created session uses the backend PID for its group. A
            # surviving orphaned group retains that ID until its members exit.
            observed = process_start_time(self.pid)
            if ((self.returncode is not None and self.pid in members)
                    or (observed is not None and observed != self.start_time)
                    or (self.pid in members and (self.start_time is None or os.getpgid(self.pid) != self.pid))):
                raise RuntimeError("Backend process identity changed")
            try:
                os.killpg(self.pid, signal.SIGTERM)
            except ProcessLookupError:
                if _process_group_members(self.pid):
                    raise
        elif self.returncode is None:
            self.process.terminate()

    def stopped(self):
        return self.returncode is not None and (os.name != "posix" or not _process_group_members(self.pid))

    def close_log(self):
        if self.log_file is not None:
            self.log_file.close()


def filtered_headers(headers, *, websocket=False) -> CIMultiDict:
    blocked = set(HOP_HEADERS)
    for value in headers.getall("Connection", []):
        blocked.update(part.strip().lower() for part in value.split(","))
    result = CIMultiDict()
    for key, value in headers.items():
        if key.lower() in blocked or (websocket and key.lower().startswith("sec-websocket-")):
            continue
        result.add(key, value)
    return result


def _raw_backend_target(request: web.Request) -> str:
    path = request.rel_url.raw_path
    if path == "/api":
        path = "/"
    elif path.startswith("/api/"):
        path = path[4:]
    query = request.rel_url.raw_query_string
    return path + ("?" + query if query else "")


def _static_path(root: Path, path: str) -> Path:
    for _ in range(4):
        decoded = unquote(path, errors="strict")
        if decoded == path:
            break
        path = decoded
    parts = path.split("/")
    if ("\\" in path or "\x00" in path
            or any(part in (".", "..") or part.startswith(".") for part in parts if part)):
        raise web.HTTPNotFound(text="Not found")
    target = (root / path.lstrip("/")).resolve()
    if not target.is_relative_to(root):
        raise web.HTTPNotFound(text="Not found")
    if not target.is_file():
        if target.suffix:
            raise web.HTTPNotFound(text="Not found")
        target = (root / "index.html").resolve()
        if not target.is_relative_to(root):
            raise web.HTTPNotFound(text="Not found")
    # FileResponse may select an adjacent precompressed representation. Check
    # those links too before handing the path to aiohttp (3.8 gzip; newer br).
    for extension in (".gz", ".br"):
        compressed = target.with_name(target.name + extension)
        if os.path.lexists(compressed) and not compressed.resolve().is_relative_to(root):
            raise web.HTTPNotFound(text="Not found")
    return target


def _small_asset(path: Path) -> bytes | None:
    with path.open("rb") as source:
        data = source.read(MAX_BRANDING_HTML)
    return data if len(data) < MAX_BRANDING_HTML else None


def _branded_index(path: Path) -> bytes | None:
    data = _small_asset(path)
    if data is None:
        return None
    position = data.lower().find(b"</head>")
    if position == -1 or BRANDING_SCRIPT_TAG in data:
        return None
    return data[:position] + BRANDING_SCRIPT_TAG + data[position:]


def _branded_manifest(path: Path) -> bytes | None:
    data = _small_asset(path)
    if data is None:
        return None
    try:
        manifest = json.loads(data)
    except (ValueError, UnicodeError):
        return None
    if not isinstance(manifest, dict):
        return None
    changed = False
    for field in ("name", "short_name"):
        if manifest.get(field) in ("Cloud Saver", "CloudSaver"):
            manifest[field] = "CloudSaver For FNOS"
            changed = True
    return json.dumps(manifest, ensure_ascii=False, separators=(",", ":")).encode("utf-8") if changed else None


class _BackendRedirectRefused(aiohttp.ClientError):
    """A backend handshake must never carry client headers to another URL."""


async def _refuse_backend_redirect(_session, _context, parameters):
    # aiohttp 3.8's ws_connect has no allow_redirects option. This public trace
    # runs before any redirect target is requested, including WS handshakes.
    # Close the current response explicitly before aborting the request.
    parameters.response.close()
    raise _BackendRedirectRefused("Backend redirects are disabled")


def create_app(frontend: Path, backend_port: int, logger: logging.Logger) -> web.Application:
    root = frontend.resolve(strict=True)
    index = (root / "index.html").resolve()
    if not root.is_dir() or not index.is_relative_to(root) or not index.is_file():
        raise ValueError("Static frontend is unavailable")
    branding_source = Path(__file__).with_name("branding.js")
    if _is_link(branding_source) or not branding_source.is_file():
        raise ValueError("Native branding script is unavailable")
    branding_script = _small_asset(branding_source)
    if not branding_script:
        raise ValueError("Native branding script is unavailable")
    session = None

    @web.middleware
    async def guarded_errors(request, handler):
        try:
            return await handler(request)
        except web.HTTPException:
            raise
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.error("request_failed")
            return web.Response(status=502, text="Service unavailable")

    app = web.Application(client_max_size=MAX_BODY, middlewares=[guarded_errors])

    async def open_session(_app):
        nonlocal session
        redirects = aiohttp.TraceConfig()
        redirects.on_request_redirect.append(_refuse_backend_redirect)
        session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=None, sock_connect=5, sock_read=None),
            auto_decompress=False, cookie_jar=aiohttp.DummyCookieJar(), trust_env=False,
            skip_auto_headers={"Accept-Encoding", "User-Agent", "Content-Type"},
            trace_configs=[redirects],
        )

    async def close_session(_app):
        if session is not None:
            await session.close()

    async def websocket(request, target, headers):
        protocols = tuple(p.strip() for p in request.headers.get("Sec-WebSocket-Protocol", "").split(",") if p.strip())
        try:
            upstream = await session.ws_connect(
                target, headers=filtered_headers(headers, websocket=True), protocols=protocols,
                autoping=False, autoclose=False, max_msg_size=MAX_BODY,
            )
        except _BackendRedirectRefused:
            return web.Response(status=502, text="WebSocket unavailable")
        except aiohttp.WSServerHandshakeError as error:
            status = error.status if 400 <= error.status <= 599 else 502
            return web.Response(status=status, text="WebSocket unavailable")
        downstream = web.WebSocketResponse(
            protocols=(upstream.protocol,) if upstream.protocol else (),
            autoping=False, autoclose=False, max_msg_size=MAX_BODY,
        )
        tasks = []
        closing = (1000, b"")

        async def relay(source, destination):
            while True:
                message = await source.receive()
                if message.type == aiohttp.WSMsgType.TEXT:
                    await destination.send_str(message.data)
                elif message.type == aiohttp.WSMsgType.BINARY:
                    await destination.send_bytes(message.data)
                elif message.type == aiohttp.WSMsgType.PING:
                    await destination.ping(message.data)
                elif message.type == aiohttp.WSMsgType.PONG:
                    await destination.pong(message.data)
                elif message.type == aiohttp.WSMsgType.CLOSE:
                    code = message.data
                    if not isinstance(code, int) or not 1000 <= code <= 4999 or code in (1004, 1005, 1006, 1015):
                        code = 1000
                    reason = str(message.extra or "").encode("utf-8")[:123].decode("utf-8", "ignore").encode("utf-8")
                    return code, reason
                else:
                    return

        try:
            await downstream.prepare(request)
            tasks = [asyncio.create_task(relay(downstream, upstream)),
                     asyncio.create_task(relay(upstream, downstream))]
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                if not task.cancelled() and task.exception() is None and task.result() is not None:
                    closing = task.result()
                    break
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            for connection in (upstream, downstream):
                try:
                    await asyncio.wait_for(connection.close(code=closing[0], message=closing[1]), timeout=5)
                except (asyncio.TimeoutError, OSError, aiohttp.ClientError):
                    pass
        return downstream

    async def proxy(request):
        raw_path = request.rel_url.raw_path
        if (request.path.startswith("/api/") and not raw_path.startswith("/api/")):
            raise web.HTTPBadRequest(text="Invalid API path")
        target = URL(f"http://127.0.0.1:{backend_port}" + _raw_backend_target(request), encoded=True)
        headers = filtered_headers(request.headers)
        headers["X-Real-IP"] = request.remote or ""
        headers["X-Forwarded-For"] = request.remote or ""
        headers["X-Forwarded-Proto"] = request.scheme
        headers["X-Forwarded-Host"] = request.headers.get("Host", "")
        headers["X-Original-URI"] = request.raw_path
        if request.headers.get("Upgrade", "").lower() == "websocket":
            return await websocket(request, target, headers)
        response = None
        try:
            async with session.request(
                request.method, target, headers=headers,
                data=request.content.iter_chunked(65536) if request.can_read_body else None,
                allow_redirects=False,
            ) as upstream:
                response = web.StreamResponse(status=upstream.status, reason=upstream.reason,
                                              headers=filtered_headers(upstream.headers))
                await response.prepare(request)
                async for chunk in upstream.content.iter_chunked(65536):
                    await response.write(chunk)
                await response.write_eof()
                return response
        except (aiohttp.ClientError, OSError, asyncio.TimeoutError):
            logger.warning("backend_io_failed")
            if response is not None and response.prepared:
                response.force_close()
                return response
            return web.Response(status=502, text="Backend unavailable")

    async def dispatch(request):
        if request.path == "/__native_health":
            if request.method not in ("GET", "HEAD"):
                raise web.HTTPMethodNotAllowed(request.method, ["GET", "HEAD"])
            return web.json_response({"status": "ok"})
        if (request.path == "/api" or request.path.startswith("/api/")
                or request.path.startswith("/tele-images/") or request.path.startswith("/socket.io/")):
            return await proxy(request)
        if request.method not in ("GET", "HEAD"):
            raise web.HTTPMethodNotAllowed(request.method, ["GET", "HEAD"])
        if request.path == BRANDING_SCRIPT_URL:
            return web.Response(body=branding_script, content_type="application/javascript", charset="utf-8",
                                headers={"Cache-Control": "no-cache", "X-Content-Type-Options": "nosniff"})
        try:
            path = _static_path(root, request.rel_url.raw_path)
        except UnicodeError:
            raise web.HTTPNotFound(text="Not found") from None
        content_type = "application/manifest+json" if path.suffix == ".webmanifest" else mimetypes.guess_type(path.name)[0]
        headers = {"Cache-Control": "no-cache"}
        if content_type:
            headers["Content-Type"] = content_type
        adapted = _branded_index(path) if path == index else (
            _branded_manifest(path) if path == root / "manifest.webmanifest" else None)
        if adapted is not None:
            if path == index:
                headers["Content-Type"] = "text/html; charset=utf-8"
            return web.Response(body=adapted, headers=headers)
        return web.FileResponse(path, headers=headers)

    app.on_startup.append(open_session)
    app.on_cleanup.append(close_session)
    app.router.add_route("*", "/{tail:.*}", dispatch)
    return app


def _write_shutdown(config: Config, backend, clean: bool) -> None:
    folder = _private_directory(config.var / "run")
    destination = folder / "shutdown.json"
    if _is_link(destination):
        raise ValueError("Shutdown receipt cannot be a link")
    temporary = folder / f"shutdown.{os.getpid()}.pending"
    with temporary.open("x", encoding="utf-8") as output:
        temporary.chmod(0o600)
        json.dump({"clean": clean, "supervisor_pid": os.getpid(), "backend_pid": backend.pid,
                   "backend_start_time": backend.start_time}, output)
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, destination)


class _StopService(Exception):
    def __init__(self, result):
        self.result = result


def _frontend_shutdown_options() -> tuple[dict, dict]:
    # aiohttp 3.8 keeps this option on BaseSite/TCPSite. Its AppRunner forwards
    # unknown kwargs to RequestHandler, where shutdown_timeout is invalid.
    # Newer aiohttp consumes it on BaseRunner instead (and deprecates the site
    # option). Inspect the public constructor rather than guessing a version.
    if "shutdown_timeout" in inspect.signature(web.BaseRunner.__init__).parameters:
        return {"shutdown_timeout": FRONTEND_SHUTDOWN_TIMEOUT}, {}
    return {}, {"shutdown_timeout": FRONTEND_SHUTDOWN_TIMEOUT}


async def run_service(config: Config, *, spawn_backend=None, listener_check=linux_listener_owned,
                      shutdown_event: asyncio.Event | None = None, ready_callback=None) -> int:
    """Run until signalled or the backend exits. Factories/checks enable safe tests.

    If graceful backend termination exceeds the deadline, this guardian stays
    alive awaiting actual termination. A lifecycle caller can time out without
    being given a false successful-cleanup receipt.
    """
    logger = safe_logger(config.var)
    backend = None
    backend_wait = stop_wait = None
    runner = None
    frontend_clean = True
    result = 0
    stop = shutdown_event if shutdown_event is not None else asyncio.Event()
    loop = asyncio.get_running_loop()
    installed_signals = []
    if shutdown_event is None:
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, stop.set)
                installed_signals.append(sig)
            except NotImplementedError:
                pass
    try:
        backend = await (spawn_backend(config) if spawn_backend else ManagedBackend.spawn(config))
        backend_wait = asyncio.create_task(backend.wait())
        _write_shutdown(config, backend, False)
        deadline = loop.time() + config.startup_timeout
        while True:
            if stop.is_set():
                raise _StopService(0)
            if backend_wait.done():
                logger.error("backend_exited_before_ready")
                raise _StopService(1)
            if listener_check(backend.pid, config.backend_port):
                try:
                    reader, writer = await asyncio.wait_for(
                        asyncio.open_connection("127.0.0.1", config.backend_port), timeout=0.5)
                    writer.close()
                    await writer.wait_closed()
                    break
                except (OSError, asyncio.TimeoutError):
                    pass
            if loop.time() >= deadline:
                logger.error("backend_start_timeout")
                raise _StopService(1)
            await asyncio.sleep(0.1)
        frontend = config.frontend.resolve(strict=True)
        if not frontend.is_relative_to(config.runtime.resolve(strict=True)):
            raise ValueError("Static frontend escapes runtime")
        app = create_app(frontend, config.backend_port, logger)
        silent = logging.Logger("CloudSaver.native.http")
        silent.disabled = True
        runner_shutdown, site_shutdown = _frontend_shutdown_options()
        runner = web.AppRunner(app, access_log=None, logger=silent,
                               auto_decompress=False, **runner_shutdown)
        await runner.setup()
        await web.TCPSite(runner, config.host, config.port, **site_shutdown).start()
        logger.info("service_ready")
        if ready_callback is not None:
            ready_callback()
        stop_wait = asyncio.create_task(stop.wait())
        done, _ = await asyncio.wait((backend_wait, stop_wait), return_when=asyncio.FIRST_COMPLETED)
        if backend_wait in done and not stop.is_set():
            logger.error("backend_exited")
            result = 1
    except _StopService as stop_service:
        result = stop_service.result
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.error("service_failed")
        result = 1
    finally:
        try:
            if stop_wait is not None:
                stop_wait.cancel()
                await asyncio.gather(stop_wait, return_exceptions=True)
            if runner is not None:
                try:
                    await runner.cleanup()
                except Exception:
                    logger.error("frontend_cleanup_failed")
                    frontend_clean = False
                    result = 1
            if backend is not None:
                try:
                    backend.terminate()
                    try:
                        await asyncio.wait_for(asyncio.shield(backend_wait), timeout=config.shutdown_timeout)
                    except asyncio.TimeoutError:
                        logger.error("backend_stop_pending")
                        await asyncio.shield(backend_wait)
                    if not backend.stopped():
                        logger.error("backend_group_stop_pending")
                        while not backend.stopped():
                            await asyncio.sleep(0.2)
                    _write_shutdown(config, backend, frontend_clean)
                    logger.info("service_stopped" if frontend_clean else "cleanup_failed")
                except Exception:
                    logger.error("cleanup_failed")
                    result = 1
                finally:
                    backend.close_log()
            _close_logger(logger)
        finally:
            # Keep TERM/INT idempotent throughout pending cleanup. Removing
            # these earlier would let a repeated stop kill this guardian while
            # an unresponsive backend still owns the configuration database.
            for sig in installed_signals:
                loop.remove_signal_handler(sig)
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Run the native CloudSaver web service")
    parser.add_argument("--base", type=Path, required=True)
    parser.add_argument("--var", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8008)
    parser.add_argument("--backend-port", type=int, default=18009)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--node-env", choices=("production", "development"), default="production",
                        help="Use the recorded Node environment without modifying application configuration")
    parser.add_argument("--upstream-log", action="store_true",
                        help="Capture upstream diagnostics in private logs/upstream.log (may contain sensitive data)")
    arguments = parser.parse_args(argv)
    try:
        config = Config(arguments.base, arguments.var, arguments.port, arguments.backend_port,
                        arguments.host, capture_upstream=arguments.upstream_log, node_env=arguments.node_env)
        return asyncio.run(run_service(config))
    except Exception:
        # Underlying exceptions may contain paths, URLs, or configuration data.
        print("Native CloudSaver service could not start; check its private status log.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
