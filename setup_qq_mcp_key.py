#!/usr/bin/env python3
"""Find and save the QQNT SQLCipher key for qq_mcp_server.py.

This script scans already-running QQ.exe processes for plausible QQNT database
keys, validates candidates against the local QQNT DB, then stores the matching
key encrypted with Windows DPAPI for the current Windows user.

It never prints the key.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import ctypes
import ctypes.wintypes as wt
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Iterator

import sqlcipher3

from unified_mcp.runtime_paths import RUNTIME


DEFAULT_DB_ROOT = Path(os.environ["QQ_MCP_DB_ROOT"]).expanduser() if os.environ.get("QQ_MCP_DB_ROOT", "").strip() else None
DEFAULT_EXT = Path(os.environ.get("QQ_MCP_EXTENSION") or str(Path(__file__).with_name("sqlite_ext_ntqq_db_pkg") / "sqlite_ext_ntqq_db.dll"))
STATE_HOME = RUNTIME
DEFAULT_KEY_FILE = Path(os.environ.get("QQ_MCP_KEY_FILE") or str(STATE_HOME / "qq/key.dpapi"))

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010
TH32CS_SNAPPROCESS = 0x00000002
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

MEM_COMMIT = 0x1000
MEM_PRIVATE = 0x20000
MEM_MAPPED = 0x40000
PAGE_NOACCESS = 0x01
PAGE_GUARD = 0x100
READABLE_PROTECTIONS = {0x02, 0x04, 0x08, 0x20, 0x40, 0x80}


class DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wt.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


class MEMORY_BASIC_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", ctypes.c_void_p),
        ("AllocationBase", ctypes.c_void_p),
        ("AllocationProtect", wt.DWORD),
        ("PartitionId", wt.WORD),
        ("RegionSize", ctypes.c_size_t),
        ("State", wt.DWORD),
        ("Protect", wt.DWORD),
        ("Type", wt.DWORD),
    ]


class PROCESSENTRY32(ctypes.Structure):
    _fields_ = [
        ("dwSize", wt.DWORD),
        ("cntUsage", wt.DWORD),
        ("th32ProcessID", wt.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wt.DWORD),
        ("cntThreads", wt.DWORD),
        ("th32ParentProcessID", wt.DWORD),
        ("pcPriClassBase", wt.LONG),
        ("dwFlags", wt.DWORD),
        ("szExeFile", wt.WCHAR * 260),
    ]


kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)

kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
kernel32.OpenProcess.restype = wt.HANDLE
kernel32.CloseHandle.argtypes = [wt.HANDLE]
kernel32.CloseHandle.restype = wt.BOOL
kernel32.VirtualQueryEx.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.POINTER(MEMORY_BASIC_INFORMATION), ctypes.c_size_t]
kernel32.VirtualQueryEx.restype = ctypes.c_size_t
kernel32.ReadProcessMemory.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
kernel32.ReadProcessMemory.restype = wt.BOOL
kernel32.CreateToolhelp32Snapshot.argtypes = [wt.DWORD, wt.DWORD]
kernel32.CreateToolhelp32Snapshot.restype = wt.HANDLE
kernel32.Process32FirstW.argtypes = [wt.HANDLE, ctypes.POINTER(PROCESSENTRY32)]
kernel32.Process32FirstW.restype = wt.BOOL
kernel32.Process32NextW.argtypes = [wt.HANDLE, ctypes.POINTER(PROCESSENTRY32)]
kernel32.Process32NextW.restype = wt.BOOL

crypt32.CryptProtectData.argtypes = [
    ctypes.POINTER(DataBlob),
    ctypes.c_wchar_p,
    ctypes.c_void_p,
    ctypes.c_void_p,
    ctypes.c_void_p,
    wt.DWORD,
    ctypes.POINTER(DataBlob),
]
crypt32.CryptProtectData.restype = wt.BOOL
kernel32.LocalFree.argtypes = [ctypes.c_void_p]
kernel32.LocalFree.restype = ctypes.c_void_p


def sql_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def protect_dpapi(text: str) -> str:
    raw = text.encode("utf-8")
    in_buffer = ctypes.create_string_buffer(raw)
    in_blob = DataBlob(len(raw), ctypes.cast(in_buffer, ctypes.POINTER(ctypes.c_char)))
    out_blob = DataBlob()
    if not crypt32.CryptProtectData(
        ctypes.byref(in_blob),
        "qq-local-mcp database key",
        None,
        None,
        None,
        0,
        ctypes.byref(out_blob),
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        encrypted = ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        kernel32.LocalFree(out_blob.pbData)
    return base64.b64encode(encrypted).decode("ascii")


def load_offset_vfs(ext_path: Path) -> sqlcipher3.Connection:
    mem = sqlcipher3.connect(":memory:")
    mem.enable_load_extension(True)
    mem.load_extension(str(ext_path))
    return mem


def test_db_key(db_root: Path, key: str) -> bool:
    db_path = db_root / "profile_info.db"
    uri = "file:" + str(db_path).replace("\\", "/") + "?mode=ro"
    con = sqlcipher3.connect(uri, uri=True)
    try:
        con.execute(f"PRAGMA key = {sql_quote(key)}")
        con.execute("PRAGMA cipher_page_size = 4096")
        con.execute("PRAGMA kdf_iter = 4000")
        con.execute("PRAGMA cipher_hmac_algorithm = HMAC_SHA1")
        row = con.execute("SELECT COUNT(*) FROM profile_info_v6").fetchone()
        return bool(row and int(row[0]) > 0)
    except Exception:
        return False
    finally:
        con.close()


@contextlib.contextmanager
def suppress_native_output() -> Iterator[None]:
    null = os.open(os.devnull, os.O_WRONLY)
    saved_stdout = os.dup(1)
    saved_stderr = os.dup(2)
    try:
        os.dup2(null, 1)
        os.dup2(null, 2)
        yield
    finally:
        os.dup2(saved_stdout, 1)
        os.dup2(saved_stderr, 2)
        os.close(saved_stdout)
        os.close(saved_stderr)
        os.close(null)


def qq_pids() -> list[int]:
    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snapshot == INVALID_HANDLE_VALUE:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        entry = PROCESSENTRY32()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32)
        pids: list[int] = []
        ok = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while ok:
            if entry.szExeFile.lower() == "qq.exe":
                pids.append(int(entry.th32ProcessID))
            ok = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
        return pids
    finally:
        kernel32.CloseHandle(snapshot)


def open_process(pid: int) -> int | None:
    handle = kernel32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_VM_READ, False, pid)
    if not handle:
        return None
    return int(handle)


def is_readable_region(mbi: MEMORY_BASIC_INFORMATION, *, include_mapped: bool = False) -> bool:
    if mbi.State != MEM_COMMIT:
        return False
    if mbi.Type != MEM_PRIVATE and not (include_mapped and mbi.Type == MEM_MAPPED):
        return False
    if mbi.Protect & PAGE_GUARD or mbi.Protect & PAGE_NOACCESS:
        return False
    return bool((mbi.Protect & 0xFF) in READABLE_PROTECTIONS)


def read_process_chunk(handle: int, address: int, size: int) -> bytes:
    buffer = ctypes.create_string_buffer(size)
    read = ctypes.c_size_t()
    if not kernel32.ReadProcessMemory(handle, ctypes.c_void_p(address), buffer, size, ctypes.byref(read)):
        return b""
    if read.value <= 0:
        return b""
    return buffer.raw[: read.value]


def candidate_strings(data: bytes, *, alnum_only: bool) -> Iterator[str]:
    if alnum_only:
        pattern = rb"(?<![0-9A-Za-z])([0-9A-Za-z]{16})(?![0-9A-Za-z])"
    else:
        pattern = rb"(?<![ -~])([ -~]{16})(?![ -~])"
    for match in re.finditer(pattern, data):
        try:
            text = match.group(1).decode("ascii")
        except UnicodeDecodeError:
            continue
        if len(set(text)) < 4:
            continue
        yield text


def targeted_candidates(data: bytes, patterns: list[bytes], *, window_size: int, alnum_only: bool) -> Iterator[str]:
    seen_offsets: set[int] = set()
    for pattern in patterns:
        offset = data.find(pattern)
        while offset != -1:
            start = max(0, offset - window_size)
            end = min(len(data), offset + len(pattern) + window_size)
            if start not in seen_offsets:
                seen_offsets.add(start)
                yield from candidate_strings(data[start:end], alnum_only=alnum_only)
            offset = data.find(pattern, offset + 1)


def scan_process_candidates(pid: int, *, alnum_only: bool, chunk_size: int, max_region_mb: int) -> Iterator[str]:
    handle = open_process(pid)
    if handle is None:
        return
    try:
        address = 0
        mbi = MEMORY_BASIC_INFORMATION()
        max_address = 0x00007FFFFFFF0000
        max_region = max_region_mb * 1024 * 1024
        while address < max_address:
            result = kernel32.VirtualQueryEx(handle, ctypes.c_void_p(address), ctypes.byref(mbi), ctypes.sizeof(mbi))
            if result == 0:
                break
            base = int(mbi.BaseAddress or address)
            region_size = int(mbi.RegionSize or 0)
            next_address = base + max(region_size, 0x1000)
            if region_size > 0 and region_size <= max_region and is_readable_region(mbi):
                tail = b""
                offset = 0
                while offset < region_size:
                    to_read = min(chunk_size, region_size - offset)
                    chunk = read_process_chunk(handle, base + offset, to_read)
                    if chunk:
                        window = tail + chunk
                        yield from candidate_strings(window, alnum_only=alnum_only)
                        tail = window[-32:]
                    offset += to_read
            address = next_address
    finally:
        kernel32.CloseHandle(handle)


def scan_process_targeted_candidates(
    pid: int,
    *,
    alnum_only: bool,
    chunk_size: int,
    max_region_mb: int,
    window_size: int,
) -> Iterator[str]:
    patterns = [
        b"nt_sqlite3_key_v2",
        b"profile_info.db",
        b"nt_msg.db",
        b"buddy_msg_fts.db",
        b"cipher_page_size",
        b"cipher_hmac_algorithm",
    ]
    patterns += [item.decode("ascii").encode("utf-16le") for item in patterns if all(32 <= b < 127 for b in item)]
    handle = open_process(pid)
    if handle is None:
        return
    try:
        address = 0
        mbi = MEMORY_BASIC_INFORMATION()
        max_address = 0x00007FFFFFFF0000
        max_region = max_region_mb * 1024 * 1024
        carry = b""
        while address < max_address:
            result = kernel32.VirtualQueryEx(handle, ctypes.c_void_p(address), ctypes.byref(mbi), ctypes.sizeof(mbi))
            if result == 0:
                break
            base = int(mbi.BaseAddress or address)
            region_size = int(mbi.RegionSize or 0)
            next_address = base + max(region_size, 0x1000)
            if region_size > 0 and region_size <= max_region and is_readable_region(mbi, include_mapped=True):
                offset = 0
                while offset < region_size:
                    to_read = min(chunk_size, region_size - offset)
                    chunk = read_process_chunk(handle, base + offset, to_read)
                    if chunk:
                        window = carry + chunk
                        yield from targeted_candidates(window, patterns, window_size=window_size, alnum_only=alnum_only)
                        carry = window[-max(window_size, 4096):]
                    offset += to_read
            address = next_address
    finally:
        kernel32.CloseHandle(handle)


def find_key(db_root: Path, pids: list[int], *, alnum_only: bool, max_candidates: int, chunk_size: int, max_region_mb: int) -> dict[str, object]:
    tested: set[str] = set()
    started = time.time()
    for pid in pids:
        pid_candidates = 0
        for candidate in scan_process_candidates(
            pid,
            alnum_only=alnum_only,
            chunk_size=chunk_size,
            max_region_mb=max_region_mb,
        ):
            if candidate in tested:
                continue
            tested.add(candidate)
            pid_candidates += 1
            with suppress_native_output():
                ok = test_db_key(db_root, candidate)
            if ok:
                return {
                    "found": True,
                    "pid": pid,
                    "tested_candidates": len(tested),
                    "pid_candidates": pid_candidates,
                    "elapsed_sec": round(time.time() - started, 3),
                    "key": candidate,
                }
            if len(tested) >= max_candidates:
                return {
                    "found": False,
                    "reason": "max_candidates_reached",
                    "tested_candidates": len(tested),
                    "elapsed_sec": round(time.time() - started, 3),
                }
    return {
        "found": False,
        "reason": "not_found",
        "tested_candidates": len(tested),
        "elapsed_sec": round(time.time() - started, 3),
    }


def find_key_targeted(
    db_root: Path,
    pids: list[int],
    *,
    alnum_only: bool,
    max_candidates: int,
    chunk_size: int,
    max_region_mb: int,
    window_size: int,
) -> dict[str, object]:
    tested: set[str] = set()
    started = time.time()
    for pid in pids:
        for candidate in scan_process_targeted_candidates(
            pid,
            alnum_only=alnum_only,
            chunk_size=chunk_size,
            max_region_mb=max_region_mb,
            window_size=window_size,
        ):
            if candidate in tested:
                continue
            tested.add(candidate)
            with suppress_native_output():
                ok = test_db_key(db_root, candidate)
            if ok:
                return {
                    "found": True,
                    "pid": pid,
                    "tested_candidates": len(tested),
                    "elapsed_sec": round(time.time() - started, 3),
                    "key": candidate,
                    "mode": "targeted",
                }
            if len(tested) >= max_candidates:
                return {
                    "found": False,
                    "reason": "max_candidates_reached",
                    "tested_candidates": len(tested),
                    "elapsed_sec": round(time.time() - started, 3),
                    "mode": "targeted",
                }
    return {
        "found": False,
        "reason": "not_found",
        "tested_candidates": len(tested),
        "elapsed_sec": round(time.time() - started, 3),
        "mode": "targeted",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Deploy qq-mcp key file without printing the key.")
    parser.add_argument("--db-root", type=Path, default=DEFAULT_DB_ROOT)
    parser.add_argument("--extension", type=Path, default=DEFAULT_EXT)
    parser.add_argument("--key-file", type=Path, default=DEFAULT_KEY_FILE)
    parser.add_argument("--pid", type=int, action="append", default=[])
    parser.add_argument("--printable-fallback", action="store_true", help="If alnum scan fails, scan all printable 16-byte strings.")
    parser.add_argument("--targeted", action="store_true", help="Only test candidates near QQNT/SQLCipher strings in QQ memory.")
    parser.add_argument("--max-candidates", type=int, default=5000)
    parser.add_argument("--chunk-size", type=int, default=1024 * 1024)
    parser.add_argument("--max-region-mb", type=int, default=256)
    parser.add_argument("--window-size", type=int, default=65536)
    args = parser.parse_args()

    if os.name != "nt":
        raise SystemExit("This setup script is Windows-only.")
    if args.db_root is None:
        raise SystemExit("Select a QQ account with --db-root or QQ_MCP_DB_ROOT before recovering its local key.")
    if not args.db_root.exists():
        raise SystemExit(f"QQNT DB root not found: {args.db_root}")
    if not args.extension.exists():
        raise SystemExit(f"QQNT SQLite extension not found: {args.extension}")

    _vfs = load_offset_vfs(args.extension)
    pids = args.pid or qq_pids()
    if not pids:
        raise SystemExit("No running QQ.exe process found. Start QQ first, then rerun this script.")

    finder = find_key_targeted if args.targeted else find_key
    common = {
        "db_root": args.db_root,
        "pids": pids,
        "alnum_only": True,
        "max_candidates": args.max_candidates,
        "chunk_size": args.chunk_size,
        "max_region_mb": args.max_region_mb,
    }
    if args.targeted:
        common["window_size"] = args.window_size
    result = finder(**common)
    if not result.get("found") and args.printable_fallback:
        common["alnum_only"] = False
        common["max_candidates"] = args.max_candidates * 2
        result = finder(**common)

    if not result.get("found"):
        safe = {k: v for k, v in result.items() if k != "key"}
        safe["pids"] = pids
        print(json.dumps({"saved": False, **safe}, ensure_ascii=False, indent=2))
        return 2

    key = str(result.pop("key"))
    args.key_file.parent.mkdir(parents=True, exist_ok=True)
    args.key_file.write_text(protect_dpapi(key), encoding="ascii")
    print(
        json.dumps(
            {
                "saved": True,
                "key_file": str(args.key_file),
                "key_length": len(key),
                "pids": pids,
                **result,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    _vfs.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
