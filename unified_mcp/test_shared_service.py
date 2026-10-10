"""Real loopback MCP sessions and synthetic Gateways; no chat data access."""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextvars
import gc
import json
import os
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import patch

import anyio
import httpx
import uvicorn
from mcp import ClientSession, types
from mcp import StdioServerParameters
import mcp.client.stdio as stdio_module
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.message import SessionMessage

from unified_mcp import shared_service as service

TOKEN = "t" * 43
INSTRUCTIONS = "Synthetic private/group/media fixture instructions."


class SyntheticSharedGateway:
    def __init__(self):
        self.closed = 0
        self.calls = 0
        self.started = asyncio.Event()
        self.finished = asyncio.Event()

    def tools(self):
        return [types.Tool(name="fixture", description="synthetic only", inputSchema={"type": "object"},
                           outputSchema={"type": "object", "properties": {"fixture": {"type": "boolean"}}},
                           annotations=types.ToolAnnotations(readOnlyHint=True),
                           _meta={"fixture_meta": "preserved"})]

    async def call(self, name, args):
        self.calls += 1
        if args.get("wait"):
            self.started.set()
            await self.finished.wait()
        return types.CallToolResult(content=[types.TextContent(type="text", text="合成 😀"),
                                             types.ImageContent(type="image", mimeType="image/png", data="c3ludGhldGlj")],
                                    structuredContent={"fixture": True}, isError=bool(args.get("error")),
                                    _meta={"fixture": "result meta preserved"})

    async def close(self):
        self.closed += 1


class ServiceSessionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.created = []
        def factory():
            result = SyntheticSharedGateway()
            self.created.append(result)
            return result
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(32)
        self.listener.setblocking(False)
        self.port = self.listener.getsockname()[1]
        self.app = service.create_shared_app(factory, token=TOKEN, port=self.port, fingerprint="fixture",
                                             version="test", instructions=INSTRUCTIONS)
        self.http_server = uvicorn.Server(uvicorn.Config(self.app, host="127.0.0.1", port=self.port,
                                                        access_log=False, log_level="critical", lifespan="on",
                                                        timeout_graceful_shutdown=2))
        self.task = asyncio.create_task(self.http_server.serve(sockets=[self.listener]))
        deadline = time.monotonic() + 5
        while not self.http_server.started:
            if self.task.done():
                await self.task
                self.fail("ASGI fixture failed to start")
            if time.monotonic() > deadline:
                self.fail("ASGI startup timeout")
            await asyncio.sleep(0.01)

    async def asyncTearDown(self):
        self.http_server.should_exit = True
        await asyncio.wait_for(self.task, 5)
        self.listener.close()
        self.assertEqual(len(self.created), 1)
        self.assertEqual(self.created[0].closed, 1)

    @asynccontextmanager
    async def session(self, relay=False):
        async with httpx.AsyncClient(headers={"Authorization": "Bearer " + TOKEN}, timeout=5, trust_env=False) as client:
            async with streamable_http_client(f"http://127.0.0.1:{self.port}/mcp", http_client=client) as (read, write, _):
                if not relay:
                    async with ClientSession(read, write) as session:
                        initialized = await session.initialize()
                        yield session, initialized
                else:
                    local_send, local_read = anyio.create_memory_object_stream(0)
                    local_write, local_receive = anyio.create_memory_object_stream(0)
                    task = asyncio.create_task(service.relay_streams(local_read, local_write, read, write))
                    try:
                        async with ClientSession(local_receive, local_send) as session:
                            initialized = await session.initialize()
                            yield session, initialized
                    finally:
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
                        for stream in (local_send, local_read, local_write, local_receive):
                            await stream.aclose()
                        await read.aclose()

    async def test_two_clients_one_gateway_disconnect_independent(self):
        async with self.session() as (second, _):
            async with self.session() as (first, _):
                await first.list_tools()
                result = await second.call_tool("fixture", {})
                self.assertFalse(result.isError)
                self.assertEqual(len(self.created), 1)
            self.assertEqual(self.created[0].closed, 0)
            result = await second.call_tool("fixture", {})
            self.assertEqual(result.structuredContent, {"fixture": True})
            self.assertEqual(self.created[0].calls, 2)

    async def test_disconnect_cancels_only_own_work(self):
        async with self.session() as (second, _):
            async with self.session() as (first, _):
                pending = asyncio.create_task(first.call_tool("fixture", {"wait": True}))
                await asyncio.wait_for(self.created[0].started.wait(), 2)
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
            self.assertEqual(self.created[0].closed, 0)
            result = await second.call_tool("fixture", {})
            self.assertFalse(result.isError)

    async def test_bridge_preserves_full_results_schemas_instructions(self):
        async with self.session(relay=True) as (session, initialized):
            self.assertEqual(initialized.instructions, INSTRUCTIONS)
            self.assertEqual(initialized.serverInfo.version, "test")
            listed = await session.list_tools()
            expected = self.created[0].tools()[0].model_dump(by_alias=True)
            self.assertEqual(listed.tools[0].model_dump(by_alias=True), expected)
            for error in (False, True):
                result = await session.call_tool("fixture", {"error": error})
                self.assertEqual(result.isError, error)
                self.assertEqual(result.structuredContent, {"fixture": True})
                self.assertEqual(result.content[1].type, "image")
                self.assertEqual(result.content[1].data, "c3ludGhldGlj")
                self.assertEqual(result.meta, {"fixture": "result meta preserved"})

    async def test_actual_bridge_stays_connected_after_idle(self):
        send, read = anyio.create_memory_object_stream(0)
        write, receive = anyio.create_memory_object_stream(0)
        @asynccontextmanager
        async def stdio_fixture():
            yield read, write
        endpoint = {"port": self.port, "token": TOKEN}
        with patch.object(service, "ensure_endpoint", return_value=endpoint), patch("mcp.server.stdio.stdio_server", stdio_fixture):
            bridge = asyncio.create_task(service.serve_shared_bridge())
            try:
                async with ClientSession(receive, send) as session:
                    await session.initialize()
                    await asyncio.sleep(6)
                    result = await session.call_tool("fixture", {})
                    self.assertFalse(result.isError)
                await send.aclose()
                await asyncio.wait_for(bridge, 5)
            finally:
                bridge.cancel()
                await asyncio.gather(bridge, return_exceptions=True)
                for stream in (send, read, write, receive):
                    await stream.aclose()

    async def test_auth_host_origin_and_remote_peer_rejected(self):
        url = f"http://127.0.0.1:{self.port}/health"
        async with httpx.AsyncClient(trust_env=False) as client:
            self.assertEqual((await client.get(url)).status_code, 401)
            self.assertEqual((await client.get(url, headers={"Authorization": "Bearer wrong"})).status_code, 401)
            auth = {"Authorization": "Bearer " + TOKEN}
            self.assertEqual((await client.get(url, headers={**auth, "Host": "evil.example"})).status_code, 403)
            self.assertEqual((await client.get(url, headers={**auth, "Origin": "http://evil.example"})).status_code, 403)
            self.assertEqual((await client.get(url, headers=auth)).status_code, 200)
            self.assertEqual((await client.get(url, headers=[("Authorization", "Bearer " + TOKEN), ("Authorization", "Bearer " + TOKEN)])).status_code, 400)
        transport = httpx.ASGITransport(app=self.app, client=("192.0.2.1", 1234))
        async with httpx.AsyncClient(transport=transport, base_url=f"http://127.0.0.1:{self.port}") as client:
            self.assertEqual((await client.get("/health", headers={"Authorization": "Bearer " + TOKEN})).status_code, 403)


class ServiceStateTests(unittest.TestCase):
    def test_real_startup_race_and_dead_daemon_recovery(self):
        # Only our synthetic daemon is terminated. Random private directories
        # and a different factory fingerprint isolate all real client services.
        factory = "unified_mcp.test_shared_service:SyntheticSharedGateway"
        tracked = []
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.dict(os.environ, {"WXQQ_DATA_DIR": str(root / "data"), "WXQQ_SHARED_IDLE_SECONDS": "5"}), patch.object(service, "RUNTIME", root / "data"):
                try:
                    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                        futures = [pool.submit(service.ensure_endpoint, factory=factory, state_root=root / "state", timeout=20) for _ in range(2)]
                        try:
                            first, second = [future.result(timeout=25) for future in futures]
                        except Exception as exc:
                            logs = "\n".join(p.read_text(encoding="utf-8", errors="replace")[-3000:] for p in (root / "state").glob("*/daemon.log"))
                            raise AssertionError("Synthetic daemon startup failed: " + logs) from exc
                    tracked.append((first, service._spawned_processes[first["pid"]]))
                    self.assertEqual(first["pid"], second["pid"])
                    self.assertNotEqual(first["pid"], os.getpid())
                    self.assertTrue(service.endpoint_alive(first))
                    # The authenticated PID is the worker; Windows venv launch
                    # handles can identify a separate supervising executable.
                    os.kill(first["pid"], signal.SIGTERM)
                    tracked[0][1].wait(timeout=5)
                    deadline = time.monotonic() + 5
                    while service.endpoint_alive(first) and time.monotonic() < deadline:
                        time.sleep(0.05)
                    recovered = service.ensure_endpoint(factory=factory, state_root=root / "state", timeout=20)
                    tracked.append((recovered, service._spawned_processes[recovered["pid"]]))
                    self.assertNotEqual(recovered["instance"], first["instance"])
                    self.assertNotEqual(recovered["token"], first["token"])
                    self.assertTrue(service.endpoint_alive(recovered))
                    # No-session idle exit exercises lifespan cleanup, not an
                    # untracked permanently running test service.
                    deadline = time.monotonic() + 10
                    while service.endpoint_alive(recovered) and time.monotonic() < deadline:
                        time.sleep(0.2)
                    self.assertFalse(service.endpoint_alive(recovered))
                    tracked[-1][1].wait(timeout=5)
                finally:
                    for endpoint, process in tracked:
                        if process.poll() is None:
                            if service.endpoint_alive(endpoint):
                                os.kill(endpoint["pid"], signal.SIGTERM)
                            else:
                                process.terminate()
                            process.wait(timeout=5)
                    time.sleep(0.2)

    def test_fingerprint_isolates_config_and_implementation(self):
        with patch.object(service, "implementation_digest", return_value="a"):
            first = service.configuration_fingerprint({"QQ_MCP_DB_ROOT": "account-A"})
            second = service.configuration_fingerprint({"QQ_MCP_DB_ROOT": "account-B"})
            self.assertNotEqual(first, second)
            self.assertEqual(first, service.configuration_fingerprint({"QQ_MCP_DB_ROOT": "account-A", "UNRELATED_TEST_VAR": "ignored"}))
        with patch.object(service, "implementation_digest", return_value="b"):
            self.assertNotEqual(first, service.configuration_fingerprint({"QQ_MCP_DB_ROOT": "account-A"}))

    def test_private_credentials_and_endpoint_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = service.service_directory("fixture", Path(tmp) / "private")
            entry = {"fingerprint": "fixture", "port": 12345, "token": TOKEN, "pid": os.getpid(), "instance": "a" * 32}
            service.write_endpoint(directory, entry)
            self.assertEqual(service.read_endpoint(directory, "fixture"), entry)
            self.assertIsNone(service.read_endpoint(directory, "wrong-scope"))
            if os.name == "nt":
                # Inspect the actual resulting Windows ACL, including inherited
                # files; no broad Everyone/Users/Authenticated Users principal.
                output = subprocess.check_output(["icacls", str(directory / "endpoint.json")], creationflags=subprocess.CREATE_NO_WINDOW)
                self.assertNotIn(b"Everyone", output)
                self.assertNotIn(b"BUILTIN\\Users", output)
                self.assertNotIn(b"Authenticated Users", output)
            else:
                self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
                self.assertEqual(stat.S_IMODE((directory / "endpoint.json").stat().st_mode), 0o600)
            entry["port"] = "12345"
            service.write_endpoint(directory, entry)
            self.assertIsNone(service.read_endpoint(directory, "fixture"))

    def test_startup_lock_reuses_live_endpoint_without_launch(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(service, "configuration_fingerprint", return_value="fixture"):
            directory = service.service_directory("fixture", Path(tmp) / "private")
            entry = {"fingerprint": "fixture", "port": 12345, "token": TOKEN, "pid": os.getpid(), "instance": "a" * 32}
            service.write_endpoint(directory, entry)
            sid = service.current_user_sid()
            with patch.object(service, "current_user_sid", return_value=sid), patch.object(service, "endpoint_alive", return_value=True), patch.object(service.subprocess, "Popen") as start:
                result = service.ensure_endpoint(state_root=Path(tmp) / "private")
                self.assertEqual(result, entry)
                start.assert_not_called()


@unittest.skipUnless(os.name == "nt", "Windows SDK Job Object lifecycle regression")
class WindowsCreatorLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_creator_stdio_process_exits_but_second_client_keeps_same_daemon(self):
        """Actual SDK stdio clients create/close Jobs; TCP DELETE alone is insufficient."""
        label = contextvars.ContextVar("synthetic_stdio_label")
        ready_a, ready_b, stop_a, closed_a = [asyncio.Event() for _ in range(4)]
        bridge_pids, jobs, endpoints = {}, {}, {}
        real_create = stdio_module._create_platform_compatible_process

        async def capture(*args, **kwargs):
            process = await real_create(*args, **kwargs)
            bridge_pids[label.get()] = process.pid
            jobs[label.get()] = bool(getattr(process, "_job_object", None))
            # Only scalar fields are retained. Holding the Process here would
            # keep its Job handle alive and mask KILL_ON_JOB_CLOSE defects.
            return process

        with tempfile.TemporaryDirectory(prefix="wxqq-sdk-job-") as temporary:
            root = Path(temporary)
            state_root = root / "private-state"
            bootstrap = (
                "import asyncio,os; from unified_mcp import shared_service as s; "
                "original=s.ensure_endpoint; "
                "s.ensure_endpoint=lambda:original(factory='unified_mcp.test_shared_service:SyntheticSharedGateway',"
                "state_root=os.environ['WXQQ_TEST_STATE_ROOT']); "
                "asyncio.run(s.serve_shared_bridge())"
            )
            environment = {**os.environ, "WXQQ_DATA_DIR": str(root / "data"),
                           "WXQQ_TEST_STATE_ROOT": str(state_root), "WXQQ_SHARED_IDLE_SECONDS": "5"}
            params = StdioServerParameters(command=sys.executable, args=["-X", "utf8", "-c", bootstrap],
                                           env=environment, cwd=str(service.ROOT))

            def endpoint():
                files = list(state_root.glob("*/endpoint.json"))
                self.assertEqual(len(files), 1)
                return json.loads(files[0].read_text(encoding="utf-8"))

            async def first():
                label.set("a")
                try:
                    with (root / "a.stderr").open("w", encoding="utf-8") as err:
                        async with stdio_module.stdio_client(params, errlog=err) as (read, write):
                            async with ClientSession(read, write) as client:
                                await client.initialize()
                                await client.list_tools()
                                endpoints["a"] = endpoint()
                                ready_a.set()
                                await stop_a.wait()
                finally:
                    closed_a.set()

            async def second():
                label.set("b")
                await ready_a.wait()
                with (root / "b.stderr").open("w", encoding="utf-8") as err:
                    async with stdio_module.stdio_client(params, errlog=err) as (read, write):
                        async with ClientSession(read, write) as client:
                            await client.initialize()
                            await client.list_tools()
                            endpoints["b"] = endpoint()
                            self.assertEqual(endpoints["a"]["pid"], endpoints["b"]["pid"])
                            ready_b.set()
                            await closed_a.wait()
                            gc.collect()  # Release SDK A's Job handle, not merely its TCP session.
                            self.assertTrue(jobs["a"], "The SDK did not assign a Job; this would not exercise the regression")
                            self.assertTrue(await asyncio.to_thread(service.endpoint_alive, endpoints["a"]),
                                            "Creator stdio Job closed and killed the shared daemon")
                            result = await asyncio.wait_for(client.call_tool("fixture", {}), timeout=5)
                            self.assertEqual(result.structuredContent, {"fixture": True})
                            self.assertEqual(endpoint()["pid"], endpoints["a"]["pid"])

            tasks = []
            try:
                with patch.object(stdio_module, "_create_platform_compatible_process", side_effect=capture):
                    tasks = [asyncio.create_task(first()), asyncio.create_task(second())]
                    ready = asyncio.create_task(ready_b.wait())
                    done, _ = await asyncio.wait({*tasks, ready}, timeout=45, return_when=asyncio.FIRST_COMPLETED)
                    if ready not in done:
                        ready.cancel()
                        await asyncio.gather(ready, return_exceptions=True)
                        for task in done:
                            try:
                                task.result()
                            except BaseException as exc:
                                log = root / "a.stderr"
                                detail = log.read_text(encoding="utf-8", errors="replace")[-4000:] if log.is_file() else ""
                                self.fail("Synthetic stdio initialization failed: " + type(exc).__name__ + "\n" + detail)
                        self.fail("Both installed-style stdio clients did not become ready")
                    stop_a.set()
                    await asyncio.wait_for(asyncio.gather(*tasks), timeout=20)
            finally:
                stop_a.set()
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                gc.collect()
                if endpoints.get("a"):
                    # This PID belongs to the synthetic daemon created in this
                    # fresh private directory; no user/production process is touched.
                    if await asyncio.to_thread(service.endpoint_alive, endpoints["a"]):
                        os.kill(endpoints["a"]["pid"], signal.SIGTERM)
                    for _ in range(50):
                        try:
                            (root / "cleanup-probe").write_text("ok", encoding="ascii")
                            for log in state_root.glob("*/daemon.log"):
                                with log.open("ab"):
                                    pass
                            break
                        except OSError:
                            await asyncio.sleep(0.1)
                    await asyncio.sleep(0.25)


class RelayDeadlineTests(unittest.IsolatedAsyncioTestCase):
    async def test_initialization_timeout_is_bounded_but_not_idle_timeout(self):
        send, read = anyio.create_memory_object_stream(0)
        write, receive = anyio.create_memory_object_stream(0)
        remote_send, remote_read = anyio.create_memory_object_stream(0)
        remote_write, remote_receive = anyio.create_memory_object_stream(0)
        task = asyncio.create_task(service.relay_streams(read, write, remote_read, remote_write, initialize_timeout=0.05))
        try:
            await asyncio.sleep(0.1)
            self.assertFalse(task.done(), "No timeout before the client requests initialize")
            request = SessionMessage(types.JSONRPCMessage(types.JSONRPCRequest(jsonrpc="2.0", id=1, method="initialize", params={})))
            sending = asyncio.create_task(send.send(request))
            await remote_receive.receive()
            await sending
            with self.assertRaisesRegex(TimeoutError, "initialize timed out"):
                await asyncio.wait_for(task, 1)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            for stream in (send, read, write, receive, remote_send, remote_read, remote_write, remote_receive):
                await stream.aclose()


if __name__ == "__main__":
    unittest.main()
