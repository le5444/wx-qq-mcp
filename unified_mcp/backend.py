"""Lazy stdio client; the SDK owns transport, initialization and child cleanup."""

from __future__ import annotations

import asyncio
import datetime as dt
import os
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def error_detail(exc):
    nested = getattr(exc, "exceptions", None)
    if nested:
        return "; ".join(error_detail(child) for child in nested)
    return f"{type(exc).__name__}: {exc}"


class WeChatBackend:
    def __init__(self, command=None, args=None, idle_seconds=60, timeout_seconds=120,
                 shutdown_timeout_seconds=5, startup_timeout_seconds=None):
        startup_timeout_seconds = timeout_seconds if startup_timeout_seconds is None else startup_timeout_seconds
        if min(idle_seconds, timeout_seconds, shutdown_timeout_seconds, startup_timeout_seconds) <= 0:
            raise ValueError("Backend timeouts must be positive")
        self.command = command or str(Path(os.environ.get("LOCALAPPDATA") or Path.home() / ".local/share") / "wx-mcp/wx-mcp.exe")
        self.args = args or []
        self.idle_seconds = idle_seconds
        self.timeout_seconds = timeout_seconds
        self.startup_timeout_seconds = startup_timeout_seconds
        self.shutdown_timeout_seconds = shutdown_timeout_seconds
        self.queue = asyncio.Queue()
        self.task = None
        self.active = False
        self.starts = 0
        self.server_info = None
        self._closed = False
        self._closing = None
        self._requests = set()

    async def call(self, name, arguments):
        if self._closed:
            raise RuntimeError("WeChat backend is closed")
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self._worker())
        future = asyncio.get_running_loop().create_future()
        self._requests.add(future)
        self.queue.put_nowait((name, arguments, future))
        try:
            return await future
        finally:
            self._requests.discard(future)

    async def _worker(self):
        item = None
        try:
            while not self._closed:
                item = await self.queue.get()
                if item[2].done():
                    item = None
                    continue
                try:
                    params = StdioServerParameters(command=self.command, args=self.args, env=dict(os.environ))
                    # Enter and leave both SDK contexts in this one task. Moving
                    # __aexit__ into wait_for/create_task breaks AnyIO cancel scopes.
                    async with stdio_client(params) as (read, write):
                        async with ClientSession(read, write, read_timeout_seconds=dt.timedelta(seconds=self.startup_timeout_seconds)) as client:
                            # Callers may give initialization its own budget when
                            # using short deadlines for requests on a ready child.
                            async with asyncio.timeout(self.startup_timeout_seconds):
                                initialized = await client.initialize()
                            self.server_info = initialized.serverInfo.model_dump(mode="json")
                            self.active = True
                            self.starts += 1
                            while item is not None:
                                name, arguments, future = item
                                if not future.done():
                                    # The SDK's read timeout does not cover sending
                                    # to a child whose stdin is blocked.
                                    async with asyncio.timeout(self.timeout_seconds):
                                        result = await client.call_tool(
                                            name, arguments,
                                            read_timeout_seconds=dt.timedelta(seconds=self.timeout_seconds))
                                    if not future.done():
                                        future.set_result(result)
                                item = None
                                try:
                                    item = await asyncio.wait_for(self.queue.get(), timeout=self.idle_seconds)
                                except asyncio.TimeoutError:
                                    break
                except Exception as exc:
                    self._fail(item, f"WeChat backend failed: {error_detail(exc)}")
                    item = None
                finally:
                    self.active = False
        finally:
            self._fail(item, "WeChat backend closed before the request completed")
            self._fail_queued()
            self.active = False

    @staticmethod
    def _fail(item, message):
        if item is not None and not item[2].done():
            item[2].set_exception(RuntimeError(message))

    def _fail_queued(self):
        while not self.queue.empty():
            self._fail(self.queue.get_nowait(), "WeChat backend closed before the request completed")

    async def close(self):
        if self._closing is None:
            self._closed = True
            # Resolve callers even if the SDK itself cannot finish cleanup before
            # our shutdown deadline. Never report a successful close in that case.
            for future in self._requests:
                if not future.done():
                    future.set_exception(RuntimeError("WeChat backend closed before the request completed"))
            # A sentinel behind a hung tool call can wait the full request timeout.
            # Cancel once; the worker still exits the SDK transport in its own task.
            if self.task and not self.task.done():
                self.task.cancel()
            self._closing = asyncio.create_task(self._finish_close())
        # One cancelled close caller must not interrupt shared child cleanup.
        await asyncio.shield(self._closing)

    async def _finish_close(self):
        try:
            if self.task:
                # wait() has a hard deadline; wait_for() may wait indefinitely for
                # cancellation if a future SDK cleanup path becomes unresponsive.
                done, _ = await asyncio.wait({self.task}, timeout=self.shutdown_timeout_seconds)
                if not done:
                    raise RuntimeError(
                        f"WeChat backend shutdown exceeded {self.shutdown_timeout_seconds} seconds")
                if not self.task.cancelled():
                    self.task.result()
        finally:
            self._fail_queued()

    def status(self):
        return {"command": self.command, "available": Path(self.command).is_file(),
                "process_active": self.active, "starts_this_session": self.starts,
                "idle_shutdown_seconds": self.idle_seconds, "server_info": self.server_info}
