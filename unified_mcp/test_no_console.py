"""Windows GUI-daemon regression tests; synthetic tools, no private data.

Do not reproduce the old console launch on the desktop. The real-process test
only runs when the launcher explicitly selects a GUI-subsystem pythonw binary.
"""

from __future__ import annotations

import asyncio
import ctypes
import json
import os
from pathlib import Path
import signal
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch

import httpx
from mcp import ClientSession, types
from mcp.client.streamable_http import streamable_http_client

from unified_mcp import shared_service as service
from unified_mcp import win_detached


def pe_subsystem(path):
    """Check the actual executable image, not just its pythonw filename."""
    with Path(path).open("rb") as stream:
        if stream.read(2) != b"MZ":
            raise ValueError("Not a Windows executable")
        stream.seek(0x3C)
        offset = struct.unpack("<I", stream.read(4))[0]
        stream.seek(offset)
        if stream.read(4) != b"PE\0\0":
            raise ValueError("Invalid Windows executable header")
        stream.seek(offset + 24 + 68)
        return struct.unpack("<H", stream.read(2))[0]


def windows_console_state():
    """Introspect only this synthetic process and its launcher, not user UI."""
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    user = ctypes.WinDLL("user32", use_last_error=True)
    kernel.GetConsoleWindow.restype = wintypes.HWND
    kernel.GetConsoleProcessList.argtypes = [ctypes.POINTER(wintypes.DWORD), wintypes.DWORD]
    kernel.GetConsoleProcessList.restype = wintypes.DWORD
    kernel.GetModuleFileNameW.argtypes = [wintypes.HMODULE, wintypes.LPWSTR, wintypes.DWORD]
    kernel.GetModuleFileNameW.restype = wintypes.DWORD
    image = ctypes.create_unicode_buffer(32768)
    if not kernel.GetModuleFileNameW(None, image, len(image)):
        raise ctypes.WinError(ctypes.get_last_error())
    target_pids = {os.getpid(), os.getppid()}
    visible = []
    user.IsWindowVisible.argtypes = [wintypes.HWND]
    user.IsWindowVisible.restype = wintypes.BOOL
    user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user.GetWindowThreadProcessId.restype = wintypes.DWORD
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    @callback_type
    def visitor(window, _):
        pid = wintypes.DWORD()
        user.GetWindowThreadProcessId(window, ctypes.byref(pid))
        if pid.value in target_pids and user.IsWindowVisible(window):
            visible.append({"pid": pid.value, "handle": int(window)})
        return True
    user.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    user.EnumWindows.restype = wintypes.BOOL
    if not user.EnumWindows(visitor, 0):
        raise ctypes.WinError(ctypes.get_last_error())
    console_pids = (wintypes.DWORD * 32)()
    return {"worker_pid": os.getpid(), "launcher_pid": os.getppid(),
            "console_window": int(kernel.GetConsoleWindow() or 0),
            "console_process_count": int(kernel.GetConsoleProcessList(console_pids, len(console_pids))),
            "visible_own_windows": visible, "executable": sys.executable,
            "actual_image": image.value, "actual_image_subsystem": pe_subsystem(image.value),
            "prefix": sys.prefix, "stdout_is_redirected": sys.stdout is not None,
            "stderr_is_redirected": sys.stderr is not None}


class NoConsoleGateway:
    """Separate fixture factory: never creates the real Gateway or a model."""
    def tools(self):
        return [types.Tool(name="fixture_console_state", description="Synthetic own-process window check",
                           inputSchema={"type": "object", "additionalProperties": False})]

    async def call(self, name, args):
        result = windows_console_state()
        return types.CallToolResult(content=[types.TextContent(type="text", text=json.dumps(result))],
                                    structuredContent=result, isError=False)

    async def close(self):
        pass


@unittest.skipUnless(os.name == "nt", "Windows pythonw/console behavior")
class NoConsoleIdentityTests(unittest.TestCase):
    def test_same_venv_python_and_pythonw_share_fingerprint(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in ("one", "two"):
                folder = root / name / "Scripts"
                folder.mkdir(parents=True)
                (folder / "python.exe").touch()
                (folder / "pythonw.exe").touch()
            with patch.object(service, "implementation_digest", return_value="synthetic"), \
                    patch.object(service, "RUNTIME", root / "runtime"), \
                    patch.object(Path, "home", return_value=root / "home"):
                def fingerprint(name, executable):
                    with patch.object(sys, "executable", str(root / name / "Scripts" / executable)):
                        return service.configuration_fingerprint(env={})
                self.assertEqual(fingerprint("one", "python.exe"), fingerprint("one", "pythonw.exe"))
                self.assertNotEqual(fingerprint("one", "python.exe"), fingerprint("two", "pythonw.exe"))

    def test_missing_pythonw_fails_before_creating_a_console_process(self):
        with tempfile.TemporaryDirectory() as temporary:
            executable = Path(temporary) / "python.exe"
            executable.touch()
            with patch.object(sys, "executable", str(executable)):
                with self.assertRaises((FileNotFoundError, RuntimeError)):
                    win_detached.windowless_python()


@unittest.skipUnless(os.name == "nt", "Windows WMI GUI daemon")
class NoConsoleDaemonTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_wmi_daemon_has_no_console_and_serves_mcp(self):
        # Never fall back to launching a console image when the GUI interpreter
        # is absent. Inspect the base image too, covering venv redirectors.
        gui_python = Path(win_detached.windowless_python())
        self.assertEqual(gui_python.name.lower(), "pythonw.exe")
        self.assertEqual(pe_subsystem(gui_python), 2, "Launcher must use Windows GUI subsystem")
        base_gui = Path(sys._base_executable).with_name("pythonw.exe")
        self.assertTrue(base_gui.is_file())
        self.assertEqual(pe_subsystem(base_gui), 2, "Redirected interpreter must also use GUI subsystem")
        factory = "unified_mcp.test_no_console:NoConsoleGateway"
        endpoint = None
        process = None
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch.dict(os.environ, {"WXQQ_DATA_DIR": str(root / "runtime"), "WXQQ_SHARED_IDLE_SECONDS": "60"}), \
                    patch.object(service, "RUNTIME", root / "runtime"):
                try:
                    endpoint = await asyncio.to_thread(service.ensure_endpoint, factory=factory,
                                                       state_root=root / "state", timeout=25)
                    process = service._spawned_processes.get(endpoint["pid"])
                    url = f"http://127.0.0.1:{endpoint['port']}/mcp"
                    async with httpx.AsyncClient(headers={"Authorization": "Bearer " + endpoint["token"]},
                            timeout=httpx.Timeout(None, connect=5, write=10, pool=5), trust_env=False) as client:
                        async with streamable_http_client(url, http_client=client) as (read, write, _):
                            async with ClientSession(read, write) as session:
                                await session.initialize()
                                reply = await session.call_tool("fixture_console_state", {})
                                self.assertFalse(reply.isError)
                                data = reply.structuredContent
                                self.assertEqual(data["worker_pid"], endpoint["pid"])
                                self.assertEqual(data["console_window"], 0)
                                self.assertEqual(data["console_process_count"], 0)
                                self.assertEqual(data["visible_own_windows"], [])
                                self.assertEqual(Path(data["executable"]).name.lower(), "pythonw.exe")
                                self.assertEqual(Path(data["actual_image"]).name.lower(), "pythonw.exe")
                                self.assertEqual(data["actual_image_subsystem"], 2)
                                self.assertEqual(Path(data["prefix"]).resolve(), Path(sys.prefix).resolve())
                                self.assertTrue(data["stdout_is_redirected"])
                                self.assertTrue(data["stderr_is_redirected"])
                                print("Synthetic WMI daemon: pythonw GUI image, no console, no visible own windows, same venv, MCP responsive")
                finally:
                    # Only the authenticated synthetic endpoint can be stopped;
                    # never target arbitrary python/terminal processes by name.
                    if endpoint and await asyncio.to_thread(service.endpoint_alive, endpoint):
                        os.kill(endpoint["pid"], signal.SIGTERM)
                    if process is not None:
                        if process.poll() is None:
                            process.terminate()
                        await asyncio.to_thread(process.wait, 5)


if __name__ == "__main__":
    unittest.main()
