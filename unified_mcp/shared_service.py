"""Per-user authenticated loopback MCP daemon and lossless stdio bridge.

The daemon owns the Gateway in the ASGI process lifespan. MCP session teardown
never owns or closes that Gateway. Local clients retain independent sessions.
"""

from __future__ import annotations

import argparse
import asyncio
import ctypes
import hashlib
import hmac
import importlib
import json
import os
import re
import secrets
import socket
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from contextlib import asynccontextmanager
from pathlib import Path

from .qq_cache_lock import cache_lock
from .runtime_paths import RUNTIME

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_FACTORY = "unified_mcp.server:Gateway"
_spawned_processes = {}


def current_user_sid():
    """No credentials are passed to external commands or printed."""
    if os.name != "nt":
        return str(os.getuid())
    raw = subprocess.check_output(["whoami", "/user", "/fo", "csv", "/nh"],
                                  creationflags=subprocess.CREATE_NO_WINDOW, timeout=5)
    match = re.search(rb"S-1-\d+(?:-\d+)+", raw)
    if not match:
        raise RuntimeError("Unable to determine the current Windows account SID")
    return match.group().decode("ascii")


def private_directory(path: Path):
    """Create or verify a user-owned directory with an exact private DACL."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and (path.is_symlink() or getattr(path.lstat(), "st_file_attributes", 0) & 0x400):
        raise PermissionError("Shared-service credentials cannot be stored through a reparse point")
    if os.name != "nt":
        path.mkdir(mode=0o700, exist_ok=True)
        if path.stat().st_uid != os.getuid():
            raise PermissionError("Shared-service directory is owned by another user")
        path.chmod(0o700)
        return
    from ctypes import wintypes
    sid = current_user_sid()
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    convert = advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW
    convert.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.DWORD)]
    convert.restype = wintypes.BOOL
    free = kernel.LocalFree
    free.argtypes, free.restype = [ctypes.c_void_p], ctypes.c_void_p
    descriptor = ctypes.c_void_p()
    # Protected DACL: only this account and LocalSystem. Inheritable ACEs make
    # token, lock and log files private from their creation, not after a write.
    if not convert(f"O:{sid}D:P(A;OICI;FA;;;{sid})(A;OICI;FA;;;SY)", 1, ctypes.byref(descriptor), None):
        raise ctypes.WinError(ctypes.get_last_error())
    class Attributes(ctypes.Structure):
        _fields_ = [("length", wintypes.DWORD), ("descriptor", ctypes.c_void_p), ("inherit", wintypes.BOOL)]
    attributes = Attributes(ctypes.sizeof(Attributes), descriptor, False)
    create = kernel.CreateDirectoryW
    create.argtypes, create.restype = [wintypes.LPCWSTR, ctypes.POINTER(Attributes)], wintypes.BOOL
    secure = advapi.SetFileSecurityW
    secure.argtypes, secure.restype = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p], wintypes.BOOL
    try:
        if not create(str(path), ctypes.byref(attributes)):
            if ctypes.get_last_error() != 183 or not path.is_dir():
                raise ctypes.WinError(ctypes.get_last_error())
            # Refuse to adopt a directory pre-created by another principal.
            get_info = advapi.GetNamedSecurityInfoW
            get_info.argtypes = [wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD,
                                 ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p,
                                 ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
            get_info.restype = wintypes.DWORD
            owner, existing = ctypes.c_void_p(), ctypes.c_void_p()
            error = get_info(str(path), 1, 1, ctypes.byref(owner), None, None, None, ctypes.byref(existing))
            if error:
                raise ctypes.WinError(error)
            owner_text = ctypes.c_void_p()
            to_string = advapi.ConvertSidToStringSidW
            to_string.argtypes, to_string.restype = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)], wintypes.BOOL
            try:
                if not to_string(owner, ctypes.byref(owner_text)):
                    raise ctypes.WinError(ctypes.get_last_error())
                if ctypes.wstring_at(owner_text) != sid:
                    raise PermissionError("Shared-service directory is owned by another account")
            finally:
                if owner_text.value:
                    free(owner_text)
                free(existing)
        if not secure(str(path), 0x80000004, descriptor):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        free(descriptor)


def implementation_digest(root=ROOT):
    digest = hashlib.sha256()
    files = [root / "qq_mcp_server.py", root / "setup_qq_mcp_key.py"]
    files += sorted((root / "unified_mcp").glob("*.py"))
    files += [root / "unified_mcp/wechat_legacy_tools.json"]
    for path in files:
        if path.name.startswith("test_") or not path.is_file():
            continue
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def configuration_fingerprint(env=None, *, factory=DEFAULT_FACTORY):
    env = os.environ if env is None else env
    prefixes = ("WX_", "WXQQ_", "WECHAT_", "UNIFIED_", "QQ_", "QQNT_", "NTQQ_")
    selected = {k: v for k, v in env.items() if k.startswith(prefixes)}
    # File-backed settings/keys may change without the environment changing.
    paths = [Path.home() / ".config/wxcli/config.json", Path.home() / ".wx-mcp/config.json",
             RUNTIME / "qq/key.dpapi", RUNTIME / "qq/raw-keys.dpapi"]
    paths += [Path(v).expanduser() for k, v in selected.items() if k.endswith(("_CONFIG", "_KEY_FILE", "_RAW_KEY_FILE")) and v]
    settings = []
    for path in paths:
        if path.is_file():
            settings.append((str(path.resolve()), hashlib.sha256(path.read_bytes()).hexdigest()))
    scope = {"root": str(ROOT.resolve()), "python": str(Path(sys.executable).resolve()),
             "implementation": implementation_digest(), "env": selected, "settings": settings, "factory": factory}
    return hashlib.sha256(json.dumps(scope, sort_keys=True).encode()).hexdigest()


def service_directory(fingerprint, root=None):
    base = Path(root) if root is not None else RUNTIME / "shared-service"
    private_directory(base)
    directory = base / fingerprint
    private_directory(directory)
    return directory


def read_endpoint(directory, fingerprint):
    path = Path(directory) / "endpoint.json"
    if not path.is_file():
        return None
    if path.is_symlink() or getattr(path.lstat(), "st_file_attributes", 0) & 0x400:
        raise PermissionError("Refusing redirected shared-service endpoint file")
    if path.stat().st_size > 4096:
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if (data.get("fingerprint") != fingerprint or type(data.get("port")) is not int
                or not 1 <= data["port"] <= 65535 or type(data.get("pid")) is not int or data["pid"] <= 0
                or not re.fullmatch(r"[A-Za-z0-9_-]{40,100}", data.get("token", ""))
                or not re.fullmatch(r"[a-f0-9]{32}", data.get("instance", ""))):
            return None
        return data
    except (ValueError, TypeError, OSError):
        return None


def write_endpoint(directory, endpoint):
    path = Path(directory) / "endpoint.json"
    fd, temporary = tempfile.mkstemp(prefix="endpoint.", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(endpoint, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def endpoint_alive(endpoint, timeout=0.7):
    if endpoint is None:
        return False
    request = urllib.request.Request(f"http://127.0.0.1:{endpoint['port']}/health",
                                     headers={"Authorization": "Bearer " + endpoint["token"]})
    try:
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *_):
                return None
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        with opener.open(request, timeout=timeout) as response:
            if response.status != 200:
                return False
            body = json.loads(response.read(4096))
        return (body.get("fingerprint") == endpoint["fingerprint"] and body.get("pid") == endpoint["pid"]
                and body.get("instance") == endpoint["instance"] and body.get("ready") is True)
    except (OSError, ValueError, urllib.error.URLError):
        return False


def ensure_endpoint(*, timeout=25, factory=DEFAULT_FACTORY, state_root=None):
    """Synchronous startup coordination; invoke via asyncio.to_thread."""
    for pid, child in list(_spawned_processes.items()):
        if child.poll() is not None:
            _spawned_processes.pop(pid, None)
    fingerprint = configuration_fingerprint(factory=factory)
    directory = service_directory(fingerprint, state_root)
    endpoint = read_endpoint(directory, fingerprint)
    if endpoint_alive(endpoint):
        return endpoint
    with cache_lock(directory / "startup.lock", timeout=timeout):
        endpoint = read_endpoint(directory, fingerprint)
        if endpoint_alive(endpoint):
            return endpoint
        command = [sys.executable, "-X", "utf8", "-m", "unified_mcp.shared_service", "--daemon",
                   "--fingerprint", fingerprint, "--factory", factory]
        if state_root is not None:
            command += ["--state-root", str(state_root)]
        if os.name == "nt":
            # MCP SDK stdio clients use KILL_ON_JOB_CLOSE. A normal Popen child
            # inherits that Job and dies when its first client exits. WMI creates
            # the long-lived worker outside the calling client's Job Object.
            from .win_detached import launch_detached
            process = launch_detached(command, directory=directory, cwd=ROOT)
        else:
            with (directory / "daemon.log").open("ab") as log:
                process = subprocess.Popen(command, cwd=ROOT, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                           start_new_session=True, close_fds=True)
        _spawned_processes[process.pid] = process
        deadline = time.monotonic() + timeout
        try:
            while time.monotonic() < deadline:
                endpoint = read_endpoint(directory, fingerprint)
                if endpoint_alive(endpoint):
                    # Windows virtual-environment launchers may own a different PID
                    # than the Python worker. Keep the launch handle under both IDs.
                    _spawned_processes[endpoint["pid"]] = process
                    return endpoint
                if process.poll() is not None:
                    raise RuntimeError("Shared MCP daemon could not start; inspect its private daemon.log. The current request was not replayed.")
                time.sleep(0.1)
            raise TimeoutError("Shared MCP daemon startup timed out; no active process was killed. Inspect its private daemon.log.")
        finally:
            # Success normally already consumed this file; failed/late startup
            # must not leave a saved copy of the caller's environment behind.
            bootstrap_path = getattr(process, "bootstrap_path", None)
            if bootstrap_path is not None:
                bootstrap_path.unlink(missing_ok=True)


class Gatekeeper:
    """Protect every endpoint, including health, before parsing a request."""
    def __init__(self, app, *, token, port):
        self.app, self.token, self.port = app, token, port

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        from starlette.responses import PlainTextResponse
        headers = {}
        for key, value in scope.get("headers", []):
            name = key.lower()
            if name in headers and name in {b"host", b"authorization", b"origin"}:
                return await PlainTextResponse("Duplicate security header", 400)(scope, receive, send)
            headers[name] = value.decode("latin1")
        address = (scope.get("client") or ("", 0))[0]
        authority = f"127.0.0.1:{self.port}"
        if address != "127.0.0.1" or headers.get(b"host") != authority:
            return await PlainTextResponse("Loopback host required", 403)(scope, receive, send)
        origin = headers.get(b"origin")
        if origin is not None and origin != "http://" + authority:
            return await PlainTextResponse("Origin rejected", 403)(scope, receive, send)
        expected = "Bearer " + self.token
        if not hmac.compare_digest(headers.get(b"authorization", "").encode("latin1"), expected.encode("ascii")):
            return await PlainTextResponse("Authentication required", 401)(scope, receive, send)
        return await self.app(scope, receive, send)


def create_shared_app(gateway_factory, *, token, port, fingerprint, version, instructions,
                      instance=None, shutdown=None, idle_seconds=120):
    """An ASGI app with exactly one process-owned Gateway and MCP manager."""
    from mcp.server import Server
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
    from mcp.server.transport_security import TransportSecuritySettings
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    state = {"gateway": None, "ready": False, "last_activity": time.monotonic(),
             "instance": instance or secrets.token_hex(16), "closed": False}
    # Default MCP session lifespan does nothing. Only ASGI lifespan below closes
    # shared resources; one DELETE/disconnect cannot close other clients' work.
    server = Server("wx-mcp-unified", version=version, instructions=instructions)
    @server.list_tools()
    async def list_tools():
        return state["gateway"].tools()
    @server.call_tool()
    async def call_tool(name, arguments):
        state["last_activity"] = time.monotonic()
        return await state["gateway"].call(name, arguments)

    # SSE also handles canceled requests cleanly. SDK 1.30's JSON-response mode
    # may otherwise turn a deliberately canceled in-flight call into HTTP 500.
    manager = StreamableHTTPSessionManager(server, json_response=False, stateless=False,
        security_settings=TransportSecuritySettings(enable_dns_rebinding_protection=True,
            allowed_hosts=[f"127.0.0.1:{port}"], allowed_origins=[f"http://127.0.0.1:{port}"]),
        max_sessions=32, session_idle_timeout=180)

    async def monitor():
        while True:
            await asyncio.sleep(1)
            # SDK 1.30 keeps live sessions here. Access is read-only and tested
            # against the pinned version; a daemon never exits with a session.
            if manager._server_instances:
                state["last_activity"] = time.monotonic()
            elif shutdown and time.monotonic() - state["last_activity"] >= idle_seconds:
                shutdown()
                return

    @asynccontextmanager
    async def lifespan(_):
        state["gateway"] = gateway_factory()
        watcher = None
        try:
            async with manager.run():
                state["ready"] = True
                watcher = asyncio.create_task(monitor())
                yield
        finally:
            state["ready"] = False
            if watcher:
                watcher.cancel()
                await asyncio.gather(watcher, return_exceptions=True)
            await state["gateway"].close()
            state["closed"] = True

    class MCPRoute:
        async def __call__(self, scope, receive, send):
            state["last_activity"] = time.monotonic()
            await manager.handle_request(scope, receive, send)

    async def health(_):
        return JSONResponse({"ready": state["ready"], "pid": os.getpid(), "fingerprint": fingerprint,
                             "instance": state["instance"], "sessions": len(manager._server_instances)})

    app = Starlette(routes=[Route("/mcp", MCPRoute()), Route("/health", health)], lifespan=lifespan)
    app.state.shared = state
    app.state.manager = manager
    protected = Gatekeeper(app, token=token, port=port)
    protected.state = app.state
    return protected


def load_factory(spec):
    module, name = spec.split(":", 1)
    return getattr(importlib.import_module(module), name)


async def run_daemon(*, expected_fingerprint=None, factory=DEFAULT_FACTORY, state_root=None):
    import uvicorn
    from .version import VERSION
    from .server import INSTRUCTIONS
    fingerprint = configuration_fingerprint(factory=factory)
    if expected_fingerprint is not None and expected_fingerprint != fingerprint:
        raise RuntimeError("Configuration changed during shared daemon startup; retry connection")
    directory = service_directory(fingerprint, state_root)
    # Separate from the bridge's startup lock: the daemon owns this lock for its
    # entire life, so direct/concurrent launch attempts cannot create duplicates.
    with cache_lock(directory / "instance.lock", timeout=2):
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if os.name == "nt":
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(128)
        listener.setblocking(False)
        port = listener.getsockname()[1]
        token, instance = secrets.token_urlsafe(32), secrets.token_hex(16)
        endpoint = {"port": port, "token": token, "pid": os.getpid(), "fingerprint": fingerprint,
                    "instance": instance, "started_at": time.time()}
        server = None
        def shutdown():
            if server:
                server.should_exit = True
        app = create_shared_app(load_factory(factory), token=token, port=port, fingerprint=fingerprint,
                                version=VERSION, instructions=INSTRUCTIONS, instance=instance, shutdown=shutdown,
                                idle_seconds=max(5, float(os.environ.get("WXQQ_SHARED_IDLE_SECONDS", "120"))))
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, access_log=False,
                                             log_level="warning", lifespan="on", timeout_graceful_shutdown=20))
        task = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            deadline = time.monotonic() + 15
            while not server.started:
                if task.done():
                    await task
                    raise RuntimeError("Shared MCP ASGI startup failed")
                if time.monotonic() >= deadline:
                    raise TimeoutError("Shared MCP ASGI startup timed out")
                await asyncio.sleep(0.05)
            # Only publish an already-bound, already-ready server. No free-port
            # discovery/rebind interval permits another process to take its port.
            write_endpoint(directory, endpoint)
            await task
        finally:
            server.should_exit = True
            if not task.done():
                try:
                    await asyncio.wait_for(task, timeout=25)
                except asyncio.TimeoutError:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
            listener.close()
            saved = read_endpoint(directory, fingerprint)
            if saved and saved["instance"] == instance:
                (directory / "endpoint.json").unlink(missing_ok=True)


async def relay_streams(local_read, local_write, remote_read, remote_write, *, initialize_timeout=20):
    """Forward complete SessionMessages, preserving every protocol field."""
    initializing, initialized = asyncio.Event(), asyncio.Event()
    initialization_id = []
    async def pump(read, write, outbound=False):
        async for message in read:
            if isinstance(message, Exception):
                raise message
            root = message.message.root
            if outbound and getattr(root, "method", None) == "initialize" and not initialization_id:
                initialization_id.append(root.id)
                initializing.set()
            elif not outbound and initialization_id and getattr(root, "id", None) == initialization_id[0]:
                initialized.set()
            await write.send(message)
    async def check_initialization():
        await initializing.wait()
        try:
            await asyncio.wait_for(initialized.wait(), timeout=initialize_timeout)
        except asyncio.TimeoutError as exc:
            raise TimeoutError("Shared MCP initialize timed out; reconnect to retry. No tool call was replayed.") from exc
    incoming = asyncio.create_task(pump(local_read, remote_write, outbound=True))
    outgoing = asyncio.create_task(pump(remote_read, local_write))
    handshake = asyncio.create_task(check_initialization())
    try:
        pending = {incoming, outgoing, handshake}
        while pending:
            done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
            if incoming in done or outgoing in done:
                break
    finally:
        for task in (incoming, outgoing, handshake):
            task.cancel()
        await asyncio.gather(incoming, outgoing, handshake, return_exceptions=True)


async def serve_shared_bridge(*, version=None, instructions=None):
    """Root's --shared entry point. Initialization metadata comes from daemon."""
    import httpx
    from mcp.client.streamable_http import streamable_http_client
    from mcp.server.stdio import stdio_server
    endpoint = await asyncio.to_thread(ensure_endpoint)
    url = f"http://127.0.0.1:{endpoint['port']}/mcp"
    remote_read = None
    try:
        async with httpx.AsyncClient(headers={"Authorization": "Bearer " + endpoint["token"]},
                                    timeout=httpx.Timeout(None, connect=5, write=30, pool=5), trust_env=False) as client:
            async with streamable_http_client(url, http_client=client) as (remote_read, remote_write, _):
                async with stdio_server() as (local_read, local_write):
                    await relay_streams(local_read, local_write, remote_read, remote_write)
    finally:
        # A ClientSession normally owns this receive endpoint. Our lossless
        # bridge intentionally has no ClientSession, so close it explicitly.
        if remote_read is not None:
            await remote_read.aclose()


def main():
    parser = argparse.ArgumentParser(description="Private shared wx-qq MCP backend")
    parser.add_argument("--daemon", action="store_true")
    parser.add_argument("--fingerprint")
    parser.add_argument("--factory", default=DEFAULT_FACTORY)
    parser.add_argument("--state-root")
    options = parser.parse_args()
    if not options.daemon:
        parser.error("Use the main MCP entry point with --shared")
    asyncio.run(run_daemon(expected_fingerprint=options.fingerprint, factory=options.factory, state_root=options.state_root))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
