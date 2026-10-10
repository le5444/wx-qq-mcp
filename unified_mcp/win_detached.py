"""Launch the shared daemon outside an MCP client's kill-on-close Job Object.

Uses documented local Win32_Process.Create via CIM. A protected single-use
bootstrap file carries the environment; no secret values enter command lines.
"""

from __future__ import annotations

import ctypes
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time


class WindowsProcess:
    """Minimal handle-backed process monitor for a WMI-created process."""
    def __init__(self, pid):
        from ctypes import wintypes
        self.pid = int(pid)
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.kernel.OpenProcess.restype = wintypes.HANDLE
        self.kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        self.kernel.GetExitCodeProcess.restype = wintypes.BOOL
        self.kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self.kernel.WaitForSingleObject.restype = wintypes.DWORD
        self.kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        self.kernel.TerminateProcess.restype = wintypes.BOOL
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel.CloseHandle.restype = wintypes.BOOL
        self.handle = self.kernel.OpenProcess(0x100000 | 0x1000 | 0x0001, False, self.pid)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())

    def poll(self):
        from ctypes import wintypes
        code = wintypes.DWORD()
        if not self.kernel.GetExitCodeProcess(self.handle, ctypes.byref(code)):
            raise ctypes.WinError(ctypes.get_last_error())
        return None if code.value == 259 else int(code.value)

    def wait(self, timeout=None):
        status = self.kernel.WaitForSingleObject(self.handle, 0xFFFFFFFF if timeout is None else max(0, int(timeout * 1000)))
        if status == 258:
            raise subprocess.TimeoutExpired("WMI-created shared daemon", timeout)
        if status != 0:
            raise ctypes.WinError(ctypes.get_last_error())
        return self.poll()

    def terminate(self):
        if self.poll() is None and not self.kernel.TerminateProcess(self.handle, 1):
            raise ctypes.WinError(ctypes.get_last_error())

    def close(self):
        if getattr(self, "handle", None):
            self.kernel.CloseHandle(self.handle)
            self.handle = None

    def __del__(self):
        self.close()


# The command is constant: all paths are data read from the private handoff.
# The short-lived PowerShell may belong to the caller's Job; WMI's new Python
# process does not, as documented in the Windows Job Objects reference.
CIM_LAUNCH_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
try {
$bootstrapConfig = Get-Content -LiteralPath $env:WXQQ_PRIVATE_BOOTSTRAP -Encoding UTF8 -Raw | ConvertFrom-Json
$startupInfo = New-CimInstance -ClassName Win32_ProcessStartup -ClientOnly -Property @{ShowWindow=[uint16]0; CreateFlags=[uint32]8}
$createdProcess = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{CommandLine=[string]$bootstrapConfig.command_line; CurrentDirectory=[string]$bootstrapConfig.cwd; ProcessStartupInformation=$startupInfo}
if ($createdProcess.ReturnValue -ne 0) { [Console]::Error.WriteLine('WXQQ_WMI_STATUS=' + $createdProcess.ReturnValue); exit 1 }
[Console]::Out.WriteLine([string]$createdProcess.ProcessId)
} catch { [Console]::Error.WriteLine('WXQQ_WMI_TYPE=' + $_.Exception.GetType().FullName); exit 1 }
"""


def windowless_python(executable=None):
    """The venv console launcher does not preserve detached creation flags.

    Use the GUI-subsystem launcher and its GUI base interpreter for daemon
    bootstrap only. Stdio bridges still require their ordinary Python streams.
    """
    path = Path(executable or sys.executable)
    if path.name.lower() not in {"python.exe", "pythonw.exe"}:
        raise RuntimeError("Shared Windows daemon requires a standard Python installation with pythonw.exe")
    candidate = path.with_name("pythonw.exe")
    if not candidate.is_file():
        raise RuntimeError("Windowless pythonw.exe is missing; refusing a console-producing daemon fallback")
    return candidate


def launch_detached(command, *, directory, cwd):
    if os.name != "nt":
        raise RuntimeError("WMI shared launch is only available on Windows")
    directory, cwd = Path(directory), Path(cwd)
    bootstrap = Path(__file__).with_name("shared_bootstrap.py")
    fd, filename = tempfile.mkstemp(prefix="bootstrap-", suffix=".json", dir=directory)
    handoff = Path(filename)
    child_command = [str(windowless_python()), "-X", "utf8", str(bootstrap), str(handoff)]
    config = {"command_line": subprocess.list2cmdline(child_command), "cwd": str(cwd),
              "package_root": str(Path(__file__).resolve().parent.parent), "env": dict(os.environ),
              "log": str(directory / "daemon.log"), "daemon_args": command[5:]}
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(config, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        # Only a handoff filename is added to the launcher environment. Neither
        # that environment nor its contents are printed, returned or logged.
        launch_env = {**os.environ, "WXQQ_PRIVATE_BOOTSTRAP": str(handoff)}
        powershell = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        result = subprocess.run([str(powershell), "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", CIM_LAUNCH_SCRIPT],
                                env=launch_env, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, creationflags=subprocess.CREATE_NO_WINDOW, timeout=20)
        if result.returncode:
            # Do not echo PowerShell's diagnostic text: future versions might
            # include configuration values. Keep the public failure bounded.
            marker = re.search(rb"WXQQ_WMI_(?:STATUS=[0-9]+|TYPE=[A-Za-z0-9_.]+)", result.stderr)
            reason = marker.group().decode("ascii") if marker else "launcher_failed"
            raise RuntimeError(f"Windows WMI could not start the shared daemon ({reason}); verify the local Winmgmt service and namespace permission")
        raw_pid = result.stdout.decode("utf-8-sig").strip()
        if not raw_pid.isdigit() or int(raw_pid) <= 0:
            raise RuntimeError("Windows WMI did not return a valid process ID")
        process = WindowsProcess(int(raw_pid))
        process.bootstrap_path = handoff
        # Bootstrap acknowledges receipt by removing its own single-use file.
        # Never unlink immediately after WMI returns: Python may still be loading.
        deadline = time.monotonic() + 5
        while handoff.exists() and time.monotonic() < deadline and process.poll() is None:
            time.sleep(0.025)
        return process
    except BaseException:
        handoff.unlink(missing_ok=True)
        raise
