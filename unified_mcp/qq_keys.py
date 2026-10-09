"""Read-only NTQQ raw-key recovery. Only verified keys are saved with DPAPI."""

from __future__ import annotations

import ctypes
import json
import os
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import qq_mcp_server as legacy
import setup_qq_mcp_key as win

KEY_FILE = Path(os.environ.get("QQ_MCP_RAW_KEY_FILE") or str(legacy.STATE_HOME / "qq/raw-keys.dpapi"))
PATTERN = re.compile(rb"x'([0-9a-fA-F]{96})'")


def database_salt(path: Path) -> str:
    with path.open("rb") as stream:
        stream.seek(1024)
        return stream.read(16).hex()


def open_raw_db(root: Path, name: str, raw: str):
    if root is None:
        raise RuntimeError("QQ is not configured. Set QQ_MCP_DB_ROOT to the intended account's nt_qq/nt_db directory.")
    if not re.fullmatch(r"[0-9a-fA-F]{96}", raw):
        raise ValueError("Invalid QQ raw-key format")
    legacy.ensure_vfs()
    path = root / name
    if not path.is_file():
        raise FileNotFoundError(name)
    con = legacy.sqlcipher3.connect(path.as_uri() + "?mode=ro", uri=True)
    try:
        con.text_factory = lambda value: value.decode("utf-8", errors="replace")
        con.execute('PRAGMA key = "x\'' + raw + '\'"')
        con.execute("PRAGMA cipher_page_size = 4096")
        con.execute("PRAGMA kdf_iter = 4000")
        con.execute("PRAGMA cipher_hmac_algorithm = HMAC_SHA1")
        con.execute("PRAGMA query_only = ON")
        con.execute("PRAGMA busy_timeout = 5000")
        con.execute("SELECT count(*) FROM sqlite_master").fetchone()
        return con
    except Exception:
        con.close()
        raise


def recover(root: Path, *, timeout: float = 90) -> dict:
    if root is None or not root.is_dir():
        raise ValueError("Select an existing QQ account directory with QQ_MCP_DB_ROOT before recovering keys.")
    salts = {}
    for path in root.glob("*.db"):
        if path.stat().st_size > 1024:
            salts.setdefault(database_salt(path), []).append(path.name)
    verified = {}
    stats = []
    deadline = time.monotonic() + timeout
    legacy.ensure_vfs()
    for pid in win.qq_pids():
        handle = win.open_process(pid)
        if not handle:
            stats.append({"pid": pid, "status": "unreadable"})
            continue
        address = 0
        scanned = 0
        try:
            mbi = win.MEMORY_BASIC_INFORMATION()
            while time.monotonic() < deadline and win.kernel32.VirtualQueryEx(
                handle, ctypes.c_void_p(address), ctypes.byref(mbi), ctypes.sizeof(mbi)
            ):
                base, size = int(mbi.BaseAddress or 0), int(mbi.RegionSize)
                if size <= 0 or base + size <= address:
                    break
                if win.is_readable_region(mbi, include_mapped=True):
                    offset, carry = 0, b""
                    while offset < size and time.monotonic() < deadline:
                        amount = min(4 * 1024 * 1024, size - offset)
                        data = win.read_process_chunk(handle, base + offset, amount)
                        scanned += len(data)
                        window = carry + data
                        for match in PATTERN.finditer(window):
                            raw = match.group(1).decode("ascii").lower()
                            salt = raw[-32:]
                            if salt not in salts or salt in verified:
                                continue
                            try:
                                with win.suppress_native_output():
                                    con = open_raw_db(root, salts[salt][0], raw)
                                    con.close()
                                verified[salt] = raw
                            except Exception:
                                continue
                        carry = window[-100:] if data else b""
                        offset += amount
                address = base + size
        finally:
            win.kernel32.CloseHandle(handle)
        stats.append({"pid": pid, "scanned_mb": round(scanned / 1048576, 1)})
        required = {database_salt(root / name) for name in (
            "profile_info.db", "nt_msg.db", "buddy_msg_fts.db", "group_msg_fts.db", "group_info.db"
        ) if (root / name).is_file()}
        if required.issubset(verified) or time.monotonic() >= deadline:
            break
    verified_names = []
    for salt, raw in verified.items():
        for name in salts[salt]:
            try:
                with win.suppress_native_output():
                    con = open_raw_db(root, name, raw)
                    con.close()
                verified_names.append(name)
            except Exception:
                pass
    if verified:
        KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
        KEY_FILE.write_text(win.protect_dpapi(json.dumps({"version": 1, "keys": verified})), encoding="ascii")
    return {
        "saved": bool(verified), "protected_key_file": str(KEY_FILE),
        "verified_databases": sorted(verified_names),
        "missing_databases": sorted(name for names in salts.values() for name in names if name not in verified_names),
        "processes": stats,
    }


if __name__ == "__main__":
    print(json.dumps(recover(legacy.DEFAULT_DB_ROOT), ensure_ascii=False, indent=2))
