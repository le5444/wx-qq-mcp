"""Process-level lifecycle regressions using a synthetic MCP server (no chat data)."""

import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest

from unified_mcp.backend import WeChatBackend


SERVER = r'''
import json, os, sys, time
from pathlib import Path
for line in sys.stdin:
    request = json.loads(line)
    if "id" not in request:
        continue
    method = request["method"]
    if method == "initialize":
        if "slow_init" in sys.argv:
            time.sleep(.6)
        result = {"protocolVersion": request["params"]["protocolVersion"],
                  "capabilities": {"tools": {}},
                  "serverInfo": {"name": "backend-test", "version": "1"}}
    elif method == "tools/list":
        result = {"tools": [{"name": "status", "inputSchema": {"type": "object"}},
                            {"name": "hang", "inputSchema": {"type": "object"}},
                            {"name": "hold", "inputSchema": {"type": "object"}},
                            {"name": "crash", "inputSchema": {"type": "object"}}]}
    elif method == "tools/call":
        arguments = request["params"].get("arguments", {})
        if arguments.get("received"):
            Path(arguments["received"]).write_text("received", encoding="ascii")
        if request["params"]["name"] == "hang":
            continue
        if request["params"]["name"] == "hold":
            while not Path(arguments["release"]).exists():
                time.sleep(.01)
        if request["params"]["name"] == "crash":
            os._exit(3)
        result = {"content": [{"type": "text", "text": json.dumps({"pid": os.getpid()})}]}
    else:
        result = {}
    print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}), flush=True)
if "linger" in sys.argv:
    time.sleep(120)
'''


async def until(predicate, timeout=5):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(.02)


def process_exists(pid):
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.windll.kernel32
        kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


class BackendTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="unified-backend-test-")
        self.script = Path(self.directory.name) / "server.py"
        self.script.write_text(SERVER, encoding="utf-8")
        self.backends = []

    def backend(self, *, linger=False, slow_init=False, **kwargs):
        args = [str(self.script)]
        if linger:
            args.append("linger")
        if slow_init:
            args.append("slow_init")
        backend = WeChatBackend(sys.executable, args, **kwargs)
        self.backends.append(backend)
        return backend

    async def asyncTearDown(self):
        for backend in self.backends:
            try:
                await asyncio.wait_for(backend.close(), 7)
            finally:
                if backend.task and not backend.task.done():
                    backend.task.cancel()
        self.directory.cleanup()

    async def test_idle_stops_owned_process_and_restarts(self):
        backend = self.backend(idle_seconds=.05)
        first = await asyncio.wait_for(backend.call("status", {}), 5)
        first_pid = json.loads(first.content[0].text)["pid"]
        await until(lambda: not backend.active)
        self.assertFalse(process_exists(first_pid))
        second = await asyncio.wait_for(backend.call("status", {}), 5)
        second_pid = json.loads(second.content[0].text)["pid"]
        self.assertNotEqual(first_pid, second_pid)
        self.assertEqual(backend.starts, 2)

    async def test_sdk_kills_child_that_ignores_stdin_eof(self):
        backend = self.backend(linger=True, idle_seconds=.05)
        result = await asyncio.wait_for(backend.call("status", {}), 5)
        pid = json.loads(result.content[0].text)["pid"]
        await until(lambda: not backend.active)
        self.assertFalse(process_exists(pid))

    async def test_close_interrupts_unresponsive_call_and_rejects_queued_calls(self):
        backend = self.backend(timeout_seconds=30)
        result = await asyncio.wait_for(backend.call("status", {}), 5)
        pid = json.loads(result.content[0].text)["pid"]
        received = Path(self.directory.name) / "hang-received"
        running = asyncio.create_task(backend.call("hang", {"received": str(received)}))
        await until(received.exists)
        queued = asyncio.create_task(backend.call("status", {}))
        await until(lambda: backend.queue.qsize() == 1)
        started = time.monotonic()
        await asyncio.wait_for(backend.close(), 5)
        outcomes = await asyncio.wait_for(asyncio.gather(running, queued, return_exceptions=True), 1)
        self.assertTrue(all(isinstance(outcome, RuntimeError) for outcome in outcomes), outcomes)
        self.assertLess(time.monotonic() - started, 5)
        self.assertFalse(process_exists(pid))
        self.assertFalse(backend.active)
        with self.assertRaisesRegex(RuntimeError, "closed"):
            await backend.call("status", {})

    async def test_timeout_recovers_for_next_queued_call(self):
        # Initialization deliberately takes longer than the tool deadline. Both
        # initial startup and recovery must still complete without retries.
        backend = self.backend(slow_init=True, timeout_seconds=.5, startup_timeout_seconds=5)
        await asyncio.wait_for(backend.call("status", {}), 5)
        received = Path(self.directory.name) / "hang-received"
        running = asyncio.create_task(backend.call("hang", {"received": str(received)}))
        await until(received.exists)
        results = await asyncio.wait_for(asyncio.gather(
            running, backend.call("status", {}), return_exceptions=True), 5)
        self.assertIsInstance(results[0], RuntimeError)
        self.assertFalse(isinstance(results[1], Exception), results[1])
        self.assertEqual(backend.starts, 2)

    async def test_crashed_process_does_not_strand_next_call(self):
        backend = self.backend(timeout_seconds=5)
        await asyncio.wait_for(backend.call("status", {}), 5)
        results = await asyncio.wait_for(asyncio.gather(
            backend.call("crash", {}), backend.call("status", {}), return_exceptions=True), 5)
        self.assertIsInstance(results[0], RuntimeError)
        self.assertFalse(isinstance(results[1], Exception), results[1])
        self.assertEqual(backend.starts, 2)

    async def test_cancelled_queued_call_is_skipped(self):
        # Queue cancellation has no need for a short response/startup timer.
        # Hold the server explicitly until the queued call has been cancelled.
        backend = self.backend(timeout_seconds=5)
        await asyncio.wait_for(backend.call("status", {}), 5)
        received = Path(self.directory.name) / "hold-received"
        release = Path(self.directory.name) / "hold-release"
        skipped_received = Path(self.directory.name) / "skipped-received"
        running = asyncio.create_task(backend.call("hold", {
            "received": str(received), "release": str(release)}))
        try:
            await until(received.exists)
            skipped = asyncio.create_task(backend.call("status", {"received": str(skipped_received)}))
            await until(lambda: backend.queue.qsize() == 1)
            skipped.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await skipped
        finally:
            release.write_text("release", encoding="ascii")
        await asyncio.wait_for(running, 5)
        result = await asyncio.wait_for(backend.call("status", {}), 5)
        self.assertTrue(result.content)
        self.assertFalse(skipped_received.exists())
        self.assertEqual(backend.starts, 1)

    async def test_concurrent_close_is_idempotent(self):
        backend = self.backend(linger=True)
        result = await asyncio.wait_for(backend.call("status", {}), 5)
        pid = json.loads(result.content[0].text)["pid"]
        await asyncio.wait_for(asyncio.gather(backend.close(), backend.close()), 5)
        await backend.close()
        self.assertFalse(process_exists(pid))

    async def test_shutdown_deadline_is_not_extended_by_unresponsive_cleanup(self):
        backend = self.backend(shutdown_timeout_seconds=.05)
        release = asyncio.Event()
        started = asyncio.Event()

        async def blocked_cleanup():
            started.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                await release.wait()

        backend.task = asyncio.create_task(blocked_cleanup())
        await started.wait()
        try:
            with self.assertRaisesRegex(RuntimeError, "shutdown exceeded"):
                await asyncio.wait_for(backend.close(), 1)
        finally:
            release.set()
            await backend.task
            self.backends.remove(backend)

    async def test_cancelled_close_caller_does_not_cancel_cleanup(self):
        backend = self.backend(shutdown_timeout_seconds=2)
        release = asyncio.Event()
        started = asyncio.Event()

        async def cleanup():
            started.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                await release.wait()

        backend.task = asyncio.create_task(cleanup())
        await started.wait()
        closing = asyncio.create_task(backend.close())
        await until(lambda: backend._closing is not None)
        closing.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await closing
        self.assertFalse(backend.task.done())
        release.set()
        await asyncio.wait_for(backend.close(), 1)
        self.assertTrue(backend.task.done())


if __name__ == "__main__":
    unittest.main()
