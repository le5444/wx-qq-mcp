#!/usr/bin/env python3
"""Local QQNT chat MCP server for Codex.

The QQNT database key is read from an environment variable and is never stored
in this file or written to Codex config. Supported key env names:
QQNT_DB_KEY, QQ_MCP_DB_KEY, NTQQ_DB_KEY.
"""

from __future__ import annotations

import datetime as dt
import base64
import ctypes
import json
import os
import re
import shutil
import sys
import tempfile
import traceback
from collections import Counter
from pathlib import Path
from typing import Any

from unified_mcp.runtime_paths import RUNTIME
from unified_mcp.qq_cache_lock import cache_lock
from unified_mcp.qq_segments import parse_segments

try:
    import sqlcipher3
except Exception:  # pragma: no cover - surfaced by diagnose()
    sqlcipher3 = None  # type: ignore[assignment]


SERVER_NAME = "qq-local-mcp"
SERVER_VERSION = "0.2.0"
PROTOCOL_VERSION = "2024-11-05"
LOCAL_TZ = dt.timezone(dt.timedelta(hours=8), "Asia/Shanghai")

# A published checkout must never pick a private account implicitly. QQ stays
# optional until its database directory is explicitly selected by the operator.
DEFAULT_DB_ROOT = Path(os.environ["QQ_MCP_DB_ROOT"]).expanduser() if os.environ.get("QQ_MCP_DB_ROOT", "").strip() else None
STATE_HOME = RUNTIME
DEFAULT_EXT = Path(
    os.environ.get(
        "QQ_MCP_EXTENSION",
        str(Path(__file__).with_name("sqlite_ext_ntqq_db_pkg") / "sqlite_ext_ntqq_db.dll"),
    )
)
DEFAULT_KEY_FILE = Path(
    os.environ.get("QQ_MCP_KEY_FILE", str(STATE_HOME / "qq/key.dpapi"))
)
CACHE_DIR = Path(os.environ.get("QQ_MCP_CACHE_DIR", str(STATE_HOME / "qq/cache")))
CACHE_MEDIA_DIR = Path(os.environ.get("QQ_MCP_MEDIA_CACHE_DIR", str(CACHE_DIR / "media")))
DEFAULT_DATA_ROOT = (Path(os.environ["QQ_MCP_DATA_ROOT"]).expanduser()
                     if os.environ.get("QQ_MCP_DATA_ROOT", "").strip()
                     else DEFAULT_DB_ROOT.parent / "nt_data" if DEFAULT_DB_ROOT is not None else None)
KEY_ENV_NAMES = ("QQNT_DB_KEY", "QQ_MCP_DB_KEY", "NTQQ_DB_KEY")

_VFS_CONNECTION: Any | None = None


def sql_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def compact_text(text: str) -> str:
    text = text.replace("\x00", "")
    text = re.sub(r"[\x01-\x08\x0b\x0c\x0e-\x1f]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def row_dict(cursor: Any, row: tuple[Any, ...]) -> dict[str, Any]:
    return {desc[0]: value for desc, value in zip(cursor.description, row)}


def timestamp_to_iso(ts: Any) -> str | None:
    if ts in (None, "", 0):
        return None
    return dt.datetime.fromtimestamp(int(ts), LOCAL_TZ).isoformat(sep=" ", timespec="seconds")


class DataBlob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_ulong), ("pbData", ctypes.POINTER(ctypes.c_char))]


def decrypt_dpapi_file(path: Path) -> str:
    if os.name != "nt":
        raise RuntimeError("DPAPI key files are only supported on Windows.")
    raw = base64.b64decode(path.read_text(encoding="ascii").strip())
    in_buffer = ctypes.create_string_buffer(raw)
    in_blob = DataBlob(len(raw), ctypes.cast(in_buffer, ctypes.POINTER(ctypes.c_char)))
    out_blob = DataBlob()
    if not ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(in_blob),
        None,
        None,
        None,
        None,
        0,
        ctypes.byref(out_blob),
    ):
        raise ctypes.WinError()
    try:
        decrypted = ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out_blob.pbData)
    return decrypted.decode("utf-8").strip()


def get_key_status() -> tuple[str | None, bool]:
    for name in KEY_ENV_NAMES:
        if os.environ.get(name, "").strip():
            return name, True
    if DEFAULT_KEY_FILE.exists():
        return str(DEFAULT_KEY_FILE), True
    return None, False


def get_key() -> str:
    for name in KEY_ENV_NAMES:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    if DEFAULT_KEY_FILE.exists():
        return decrypt_dpapi_file(DEFAULT_KEY_FILE)
    raise RuntimeError(
        "QQNT database key is not configured. Set QQNT_DB_KEY before starting Codex, "
        "or create the DPAPI key file with the key setup step."
    )


def ensure_sqlcipher() -> Any:
    if sqlcipher3 is None:
        raise RuntimeError("Python package sqlcipher3 is not available in this Python environment.")
    return sqlcipher3


def ensure_vfs(ext_path: Path = DEFAULT_EXT) -> None:
    global _VFS_CONNECTION
    ensure_sqlcipher()
    if _VFS_CONNECTION is not None:
        return
    if not ext_path.exists():
        raise RuntimeError(f"SQLite QQNT offset extension not found: {ext_path}")
    mem = sqlcipher3.connect(":memory:")
    mem.enable_load_extension(True)
    mem.load_extension(str(ext_path))
    _VFS_CONNECTION = mem


def open_nt_db(db_root: Path, db_name: str, key: str) -> Any:
    if db_root is None:
        raise RuntimeError("QQ is not configured. Set QQ_MCP_DB_ROOT to the selected account's nt_qq/nt_db directory.")
    ensure_vfs()
    db_path = db_root / db_name
    if not db_path.exists():
        raise RuntimeError(f"QQNT database file not found: {db_path}")
    uri = "file:" + str(db_path).replace("\\", "/") + "?mode=ro"
    con = sqlcipher3.connect(uri, uri=True)
    con.text_factory = lambda value: value.decode("utf-8", errors="replace")
    con.execute(f"PRAGMA key = {sql_quote(key)}")
    con.execute("PRAGMA cipher_page_size = 4096")
    con.execute("PRAGMA kdf_iter = 4000")
    con.execute("PRAGMA cipher_hmac_algorithm = HMAC_SHA1")
    return con


def bounded_int(value: Any, default: int, min_value: int, max_value: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(min_value, min(parsed, max_value))


def parse_time_bound(value: Any, *, before: bool = False) -> int | None:
    if value in (None, ""):
        return None
    raw = str(value).strip()
    if not raw:
        return None
    if re.fullmatch(r"\d{10,13}", raw):
        ts = int(raw)
        return ts // 1000 if len(raw) == 13 else ts
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        parsed = dt.datetime.strptime(raw, "%Y-%m-%d").replace(tzinfo=LOCAL_TZ)
        if before:
            parsed += dt.timedelta(days=1)
        return int(parsed.timestamp())
    normalized = raw.replace("Z", "+00:00")
    parsed = dt.datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=LOCAL_TZ)
    return int(parsed.timestamp())


def extract_blob_hints(blob: Any) -> dict[str, Any]:
    if not blob:
        return {}
    if isinstance(blob, memoryview):
        blob = blob.tobytes()
    if not isinstance(blob, (bytes, bytearray)):
        return {}
    decoded = bytes(blob).decode("utf-8", errors="ignore")
    decoded = compact_text(decoded)
    images = sorted(set(re.findall(r"[A-Fa-f0-9]{16,64}\.(?:jpg|jpeg|png|gif|webp)", decoded)))
    files = sorted(
        set(
            re.findall(
                r"[\w\u4e00-\u9fff(). -]{1,120}\.(?:docx?|pptx?|xlsx?|pdf|zip|rar|7z|txt|mp4|mp3)",
                decoded,
                flags=re.IGNORECASE,
            )
        )
    )
    urls = sorted(set(re.findall(r"(?:https?://|/download\?)[^\s\"'<>]+", decoded)))
    cjk_chunks: list[str] = []
    for match in re.finditer(r"[\u4e00-\u9fff][\u4e00-\u9fff，。！？、；：,.!? A-Za-z0-9()（）\-/]{1,120}", decoded):
        chunk = compact_text(match.group(0))
        if len(chunk) >= 2 and chunk not in cjk_chunks:
            cjk_chunks.append(chunk)
        if len(cjk_chunks) >= 5:
            break
    result: dict[str, Any] = {"blob_len": len(blob)}
    if images:
        result["images"] = images
    if files:
        result["files"] = files
    if urls:
        result["urls"] = urls[:6]
    if cjk_chunks:
        result["text_hints"] = cjk_chunks
    return result


def bytes_from_blob(blob: Any) -> bytes:
    if not blob:
        return b""
    if isinstance(blob, memoryview):
        return blob.tobytes()
    if isinstance(blob, bytearray):
        return bytes(blob)
    if isinstance(blob, bytes):
        return blob
    return b""


def read_varint(data: bytes, pos: int, end: int) -> tuple[int, int] | None:
    value = 0
    shift = 0
    while pos < end and shift <= 63:
        byte = data[pos]
        pos += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, pos
        shift += 7
    return None


def iter_proto_fields(
    data: bytes,
    *,
    depth: int = 0,
    max_depth: int = 5,
    max_fields: int = 256,
) -> list[tuple[int, int, Any]]:
    """Best-effort protobuf wire parser for QQNT message payloads."""
    fields: list[tuple[int, int, Any]] = []
    pos = 0
    end = len(data)
    seen = 0
    while pos < end and seen < max_fields:
        key = read_varint(data, pos, end)
        if key is None:
            break
        key_value, pos = key
        field_no = key_value >> 3
        wire_type = key_value & 0x07
        if field_no <= 0:
            break
        value: Any
        if wire_type == 0:
            parsed = read_varint(data, pos, end)
            if parsed is None:
                break
            value, pos = parsed
        elif wire_type == 1:
            if pos + 8 > end:
                break
            value = data[pos : pos + 8]
            pos += 8
        elif wire_type == 2:
            parsed = read_varint(data, pos, end)
            if parsed is None:
                break
            size, pos = parsed
            if size < 0 or pos + size > end:
                break
            value = data[pos : pos + size]
            pos += size
        elif wire_type == 5:
            if pos + 4 > end:
                break
            value = data[pos : pos + 4]
            pos += 4
        else:
            break
        fields.append((field_no, wire_type, value))
        seen += 1
        if wire_type == 2 and depth < max_depth and isinstance(value, bytes) and 2 <= len(value) <= 65536:
            fields.extend(iter_proto_fields(value, depth=depth + 1, max_depth=max_depth, max_fields=max_fields))
    return fields


def decode_proto_text(value: bytes) -> str | None:
    if not value or b"\x00" in value:
        return None
    text = compact_text(value.decode("utf-8", errors="ignore"))
    if not text:
        return None
    printable = sum(1 for ch in text if ch.isprintable())
    if printable / max(len(text), 1) < 0.85:
        return None
    return text


def collect_proto_text_fields(blob: Any, target_fields: set[int]) -> dict[int, list[str]]:
    data = bytes_from_blob(blob)
    if not data:
        return {}
    collected: dict[int, list[str]] = {field: [] for field in target_fields}
    for field_no, wire_type, value in iter_proto_fields(data):
        if wire_type != 2 or field_no not in target_fields or not isinstance(value, bytes):
            continue
        text = decode_proto_text(value)
        if text and text not in collected[field_no]:
            collected[field_no].append(text)
    return {field: values for field, values in collected.items() if values}


def extract_qq_payload_text(blob: Any) -> str | None:
    # QQNT text bodies have been observed in nested field 45101 when FTS is empty.
    texts = [s["text"] for s in parse_segments(blob)["segments"] if s["type"] == "text"]
    if not texts:
        return None
    return "\n".join(texts)


def first_text_field(fields: dict[int, list[str]], field_no: int) -> str | None:
    values = fields.get(field_no) or []
    return values[0] if values else None


def extract_recall_info(blob: Any) -> dict[str, Any]:
    fields = collect_proto_text_fields(blob, {47703, 47704, 47705, 47706, 47713, 47714, 47715})
    if not fields:
        return {}
    info: dict[str, Any] = {
        "prompt": first_text_field(fields, 47713),
        "actor_uid": first_text_field(fields, 47703),
        "target_uid": first_text_field(fields, 47704),
        "actor_nick": first_text_field(fields, 47705),
        "actor_display_name": first_text_field(fields, 47706),
        "target_nick": first_text_field(fields, 47714),
        "target_display_name": first_text_field(fields, 47715),
    }
    names = [
        value
        for value in (
            info.get("actor_display_name"),
            info.get("actor_nick"),
            info.get("target_display_name"),
            info.get("target_nick"),
        )
        if value
    ]
    if names:
        info["participants"] = list(dict.fromkeys(names))
    return {key: value for key, value in info.items() if value}


def message_kind(type_code: Any, sub_code: Any) -> str:
    try:
        t = int(type_code)
    except (TypeError, ValueError):
        t = -1
    try:
        s = int(sub_code)
    except (TypeError, ValueError):
        s = -1
    if t == 1:
        return "text"
    if t == 2:
        return "image"
    if t == 3:
        return "mixed/text-image"
    if t == 4 and s == 5:
        return "recall/system"
    if t == 17:
        return "reply/quote"
    if t == 33 or s == 33:
        return "forward/structured"
    return f"type_{t}"


def normalize_contact(row: dict[str, Any]) -> dict[str, Any]:
    display = str(row.get("remark") or row.get("nick") or row.get("uin") or row.get("uid") or "").strip()
    return {
        "uid": row.get("uid"),
        "uin": row.get("uin"),
        "nick": row.get("nick"),
        "remark": row.get("remark"),
        "signature": row.get("signature"),
        "avatar": row.get("avatar"),
        "display_name": display,
    }


def _profile_columns(con):
    return {str(row[1]) for row in con.execute("PRAGMA table_info(profile_info_v6)")}


def _exact_profile_rows(con, columns, where, params):
    """At most two identity pairs, not a truncated fuzzy candidate list."""
    wanted = {"1000": "uid", "1002": "uin", "20002": "nick", "20009": "remark",
              "20011": "signature", "20004": "avatar"}
    selected = [f"[{key}] AS {alias}" if key in columns else f"NULL AS {alias}" for key, alias in wanted.items()]
    uid = "[1000]" if "1000" in columns else "NULL"
    uin = "[1002]" if "1002" in columns else "NULL"
    cur = con.execute(f"SELECT {', '.join(selected)} FROM profile_info_v6 WHERE {where} "
                      f"GROUP BY {uid}, {uin} LIMIT 2", params)
    return [normalize_contact(row_dict(cur, row)) for row in cur]


def exact_contact(query: str, *, field: str | None = None) -> dict[str, Any]:
    """Resolve actual reads by exact IDs or an unambiguous exact name.

    Discovery stays separate. Missing stable IDs never fall back to a name or
    substring, and exact-name ambiguity is checked before any result limit.
    """
    value = str(query or "").strip()
    if not value:
        raise ValueError("QQ contact identity must be non-empty")
    if field not in {None, "uid", "uin", "name"}:
        raise ValueError("Unsupported QQ identity field")
    explicit = field
    if field is None and ":" in value and value.split(":", 1)[0] in {"uid", "uin", "name"}:
        explicit, value = value.split(":", 1)
        value = value.strip()
    if not value:
        raise ValueError("QQ contact identity must be non-empty")
    con = open_nt_db(DEFAULT_DB_ROOT, "profile_info.db", get_key())
    try:
        columns = _profile_columns(con)
        def by(fields):
            fields = [key for key in fields if key in columns]
            if not fields:
                return []
            return _exact_profile_rows(con, columns, " OR ".join(f"CAST([{key}] AS TEXT) = ?" for key in fields), [value] * len(fields))
        if explicit == "uid":
            found = by(["1000"])
        elif explicit == "uin" or (explicit is None and value.isdigit()):
            found = by(["1002"])
        elif explicit == "name":
            found = by(["20009", "20002"])
        else:
            found = by(["1000"])
            if not found and not value.startswith("u_"):
                found = by(["20009", "20002"])
        if len(found) != 1:
            raise ValueError("QQ contact has no unique exact identity; use resolve_contact and a verified UID/UIN.")
        contact = found[0]
        uid, uin = str(contact.get("uid") or "").strip(), str(contact.get("uin") or "").strip()
        if not uid:
            raise ValueError("QQ contact profile is missing UID; cannot establish the message-table identity.")
        # Validate both directions of the UID/UIN mapping, so conflicting rows
        # cannot bless a second person's number as this contact's alias.
        predicates, params = ["[1000] = ?"], [uid]
        if uin and "1002" in columns:
            predicates.append("CAST([1002] AS TEXT) = ?")
            params.append(uin)
        links = _exact_profile_rows(con, columns, " OR ".join(predicates), params)
        if len(links) != 1:
            raise ValueError("QQ profile contains conflicting UID/UIN mappings; identity is unresolved.")
        return {**contact, "uid": uid, "canonical_id": uid,
                "aliases": list(dict.fromkeys([uid, *([uin] if uin else [])])),
                "identity_verified": True, "identity_source": "profile_exact"}
    finally:
        con.close()


class ResolutionCandidates(list):
    def __init__(self, values, *, complete):
        super().__init__(values)
        self.complete = complete


def resolve_contacts(keyword: str, limit: int = 10) -> list[dict[str, Any]]:
    keyword = str(keyword or "").strip()
    if not keyword:
        raise ValueError("resolve_contact requires a non-empty query/contact keyword.")
    limit = bounded_int(limit, 10, 1, 100)
    key = get_key()
    con = open_nt_db(DEFAULT_DB_ROOT, "profile_info.db", key)
    try:
        columns = [r[1] for r in con.execute("PRAGMA table_info(profile_info_v6)").fetchall()]
        wanted = {
            "1000": "uid",
            "1002": "uin",
            "20002": "nick",
            "20009": "remark",
            "20011": "signature",
            "20004": "avatar",
        }
        select_parts = [
            f"[{col}] AS {alias}" if col in columns else f"NULL AS {alias}"
            for col, alias in wanted.items()
        ]
        search_cols = [
            col
            for col in ("1000", "1001", "1002", "20002", "20009", "20011", "24106", "24107", "24108", "24109")
            if col in columns
        ]
        if not search_cols:
            raise RuntimeError("profile_info_v6 does not expose searchable text columns.")
        wheres = " OR ".join(f"[{col}] LIKE ?" for col in search_cols)
        params = [f"%{keyword}%"] * len(search_cols)
        cursor = con.execute(
            f"""
            SELECT {", ".join(select_parts)}
            FROM profile_info_v6
            WHERE {wheres}
            LIMIT ?
            """,
            (*params, limit * 4 + 1),
        )
        rows = [normalize_contact(row_dict(cursor, row)) for row in cursor.fetchall()]
    finally:
        con.close()

    def score(contact: dict[str, Any]) -> tuple[int, str]:
        hay = [
            str(contact.get("remark") or ""),
            str(contact.get("nick") or ""),
            str(contact.get("uin") or ""),
            str(contact.get("uid") or ""),
        ]
        if any(item == keyword for item in hay):
            return (0, str(contact.get("display_name") or ""))
        if any(keyword in item for item in hay[:2]):
            return (1, str(contact.get("display_name") or ""))
        return (2, str(contact.get("display_name") or ""))

    deduped: dict[str, dict[str, Any]] = {}
    for row in sorted(rows, key=score):
        uid = str(row.get("uid") or row.get("uin") or row.get("display_name"))
        deduped.setdefault(uid, row)
    values = list(deduped.values())
    return ResolutionCandidates(values[:limit], complete=len(rows) <= limit * 4 and len(values) <= limit)


def find_contact(keyword: str) -> dict[str, Any]:
    return exact_contact(keyword)


def get_self_uin() -> str | None:
    configured = os.environ.get("QQ_MCP_SELF_UIN", "").strip()
    if configured:
        return configured
    try:
        candidate = DEFAULT_DB_ROOT.parent.parent.name
    except Exception:
        return None
    return candidate if candidate.isdigit() else None


def normalize_chat_type(value: Any) -> str:
    raw = str(value or "private").strip().lower()
    aliases = {
        "private": "private",
        "friend": "private",
        "c2c": "private",
        "person": "private",
        "私聊": "private",
        "group": "group",
        "groups": "group",
        "群": "group",
        "群聊": "group",
        "discuss": "discuss",
        "discussion": "discuss",
        "讨论组": "discuss",
    }
    if raw not in aliases:
        raise ValueError("Unsupported QQ chat_type; expected private, group, or discuss")
    return aliases[raw]


def chat_query_from_args(args: dict[str, Any]) -> str:
    present = [(key, str(args[key]).strip()) for key in ("contact", "chat", "query", "group", "discuss")
               if args.get(key) not in (None, "")]
    if len({value for _, value in present}) > 1:
        raise ValueError("Conflicting QQ chat identity arguments; pass one contact, group, or discuss target")
    typed = {key for key, _ in present} & {"contact", "group", "discuss"}
    if len(typed) > 1:
        raise ValueError("Do not combine QQ private/group/discuss target fields")
    return present[0][1] if present else ""


def chat_type_from_args(args: dict[str, Any]) -> str:
    chat_query_from_args(args)
    declared = args.get("chat_type") or args.get("chatType")
    if args.get("group") not in (None, ""):
        if declared and normalize_chat_type(declared) != "group":
            raise ValueError("QQ group target conflicts with chat_type")
        return "group"
    if args.get("discuss") not in (None, ""):
        if declared and normalize_chat_type(declared) != "discuss":
            raise ValueError("QQ discussion target conflicts with chat_type")
        return "discuss"
    return normalize_chat_type(declared)


def normalize_group(row: dict[str, Any]) -> dict[str, Any]:
    group_id = row.get("group_id")
    name = str(row.get("name") or group_id or "").strip()
    return {
        "chat_type": "group",
        "id": str(group_id),
        "group_id": group_id,
        "group_id_str": str(group_id) if group_id is not None else None,
        "name": name,
        "display_name": name,
        "description": row.get("desc_text"),
        "alias": row.get("alias"),
    }


def resolve_groups(keyword: str, limit: int = 10) -> list[dict[str, Any]]:
    keyword = str(keyword or "").strip()
    if not keyword:
        raise ValueError("resolve_group requires a non-empty query/group keyword.")
    limit = bounded_int(limit, 10, 1, 100)
    numeric_id = int(keyword) if keyword.isdigit() else None
    key = get_key()
    con = open_nt_db(DEFAULT_DB_ROOT, "group_info.db", key)
    try:
        columns = [r[1] for r in con.execute("PRAGMA table_info(group_detail_info_ver1)").fetchall()]
        select_parts = [
            "[60001] AS group_id",
            "[60007] AS name" if "60007" in columns else "NULL AS name",
            "[60026] AS desc_text" if "60026" in columns else "NULL AS desc_text",
            "[60267] AS alias" if "60267" in columns else "NULL AS alias",
        ]
        wheres: list[str] = []
        params: list[Any] = []
        if numeric_id is not None:
            wheres.append("[60001] = ?")
            params.append(numeric_id)
        for col in ("60007", "60026", "60002", "60267"):
            if col in columns:
                wheres.append(f"[{col}] LIKE ?")
                params.append(f"%{keyword}%")
        cursor = con.execute(
            f"""
            SELECT {", ".join(select_parts)}
            FROM group_detail_info_ver1
            WHERE {" OR ".join(wheres)}
            LIMIT ?
            """,
            (*params, limit * 4 + 1),
        )
        rows = [normalize_group(row_dict(cursor, row)) for row in cursor.fetchall()]
    finally:
        con.close()

    def score(group: dict[str, Any]) -> tuple[int, str]:
        group_id = str(group.get("group_id") or "")
        name = str(group.get("name") or "")
        if group_id == keyword or name == keyword:
            return (0, name)
        if keyword in name:
            return (1, name)
        return (2, name)

    deduped: dict[str, dict[str, Any]] = {}
    for row in sorted(rows, key=score):
        deduped.setdefault(str(row.get("group_id")), row)
    values = list(deduped.values())
    return ResolutionCandidates(values[:limit], complete=len(rows) <= limit * 4 and len(values) <= limit)


def find_group(keyword: str) -> dict[str, Any]:
    value = str(keyword or "").strip()
    if not value:
        raise ValueError("QQ group identity must be non-empty")
    con = open_nt_db(DEFAULT_DB_ROOT, "group_info.db", get_key())
    try:
        columns = {str(row[1]) for row in con.execute("PRAGMA table_info(group_detail_info_ver1)")}
        fields = ["[60001] AS group_id", "[60007] AS name" if "60007" in columns else "NULL AS name"]
        if value.isdigit():
            where, params = "[60001] = ?", [int(value)]
        elif "60007" in columns:
            where, params = "[60007] = ?", [value]
        else:
            raise ValueError("QQ group name cannot be resolved; use its numeric ID")
        cur = con.execute(f"SELECT {', '.join(fields)} FROM group_detail_info_ver1 WHERE {where} GROUP BY [60001] LIMIT 2", params)
        found = [normalize_group(row_dict(cur, row)) for row in cur]
        if len(found) != 1:
            raise ValueError("QQ group has no unique exact identity; use resolve_group and its numeric ID.")
        group = found[0]
        ident = str(group["group_id"])
        return {**group, "canonical_id": ident, "aliases": list(dict.fromkeys([ident, *([value] if value.isdigit() else [])])),
                "identity_verified": True, "identity_source": "group_profile_exact"}
    finally:
        con.close()


def find_discuss(keyword: str) -> dict[str, Any]:
    discuss_id = str(keyword or "").strip()
    if not discuss_id:
        raise ValueError("discuss chat requires a discussion id.")
    if not discuss_id.isdigit():
        raise RuntimeError("QQ discuss chat lookup currently requires a numeric discussion id.")
    return {
        "chat_type": "discuss",
        "id": discuss_id,
        "discuss_id": int(discuss_id),
        "display_name": f"讨论组 {discuss_id}",
        "name": f"讨论组 {discuss_id}",
        "canonical_id": str(int(discuss_id)), "aliases": list(dict.fromkeys([str(int(discuss_id)), discuss_id])),
        "identity_verified": True, "identity_source": "explicit_numeric_discussion_id",
    }


def resolve_chat_entity(chat_query: str, chat_type: str = "auto") -> tuple[dict[str, Any], str]:
    query = str(chat_query or "").strip()
    if not query:
        raise ValueError("resolve chat requires a non-empty query.")
    normalized = str(chat_type or "private").strip().lower()
    if normalized == "auto":
        pass
    else:
        normalized = normalize_chat_type(normalized)
    if normalized == "private":
        return find_contact(query), "private"
    if normalized == "group":
        return find_group(query), "group"
    if normalized == "discuss":
        return find_discuss(query), "discuss"

    attempts: list[tuple[str, Any]] = [
        ("private", find_contact),
        ("group", find_group),
        ("discuss", find_discuss),
    ]
    errors: list[str] = []
    for candidate_type, resolver in attempts:
        try:
            return resolver(query), candidate_type
        except Exception as exc:
            errors.append(f"{candidate_type}: {exc}")
    raise RuntimeError(f"No QQ chat matched: {query}. Tried private, group, and discuss.")


def load_group_member_map(group_id: Any) -> dict[str, Any]:
    try:
        group_id_int = int(group_id)
    except (TypeError, ValueError):
        return {"members": [], "by_uid": {}, "by_uin": {}}
    key = get_key()
    con = open_nt_db(DEFAULT_DB_ROOT, "group_info.db", key)
    try:
        cursor = con.execute(
            """
            SELECT [64003] AS card, [20002] AS nick, [60001] AS group_id,
                   [1000] AS uid, [1001] AS alias, [1002] AS uin
            FROM group_member3
            WHERE [60001] = ?
            """,
            (group_id_int,),
        )
        members = [row_dict(cursor, row) for row in cursor.fetchall()]
    finally:
        con.close()

    by_uid: dict[str, dict[str, Any]] = {}
    by_uin: dict[str, dict[str, Any]] = {}
    for member in members:
        display = str(
            member.get("card")
            or member.get("nick")
            or member.get("alias")
            or member.get("uid")
            or member.get("uin")
            or ""
        ).strip()
        member["display_name"] = display
        uid = str(member.get("uid") or "").strip()
        uin = str(member.get("uin") or "").strip()
        if uid:
            by_uid[uid] = member
        if uin:
            by_uin[uin] = member
    return {"members": members, "by_uid": by_uid, "by_uin": by_uin}


def chat_identity_value(chat: dict[str, Any], chat_type: str) -> str:
    if chat_type == "private":
        uid = str(chat.get("uid") or "").strip()
        if not uid:
            raise ValueError("QQ contact UID is unresolved; refusing a potentially false empty message query")
        return uid
    if chat_type == "group":
        return str(chat.get("group_id") or chat.get("id") or chat.get("display_name") or "")
    if chat_type == "discuss":
        return str(chat.get("discuss_id") or chat.get("id") or chat.get("display_name") or "")
    return str(chat.get("id") or chat.get("display_name") or "")


def chat_display_name(chat: dict[str, Any], chat_type: str) -> str:
    if chat_type == "private":
        return str(chat.get("display_name") or chat.get("remark") or chat.get("nick") or chat.get("uid") or chat.get("uin") or "对方").strip()
    if chat_type == "group":
        return str(chat.get("display_name") or chat.get("name") or chat.get("group_id") or "群聊").strip()
    if chat_type == "discuss":
        return str(chat.get("display_name") or chat.get("name") or chat.get("discuss_id") or "讨论组").strip()
    return str(chat.get("display_name") or chat.get("name") or chat.get("id") or "聊天").strip()


def load_fts_rows(uid: str) -> dict[int, dict[str, Any]]:
    key = get_key()
    con = open_nt_db(DEFAULT_DB_ROOT, "buddy_msg_fts.db", key)
    try:
        cursor = con.execute(
            """
            SELECT [40001] AS msg_id, [40050] AS create_time, [40020] AS sender_uid,
                   [40021] AS peer_uid, [40003] AS seq, [41701] AS text,
                   [41702] AS aux_text, [41703] AS msg_code, [41704] AS type_code
            FROM buddy_msg_fts
            WHERE [40021] = ? OR [40020] = ?
            """,
            (uid, uid),
        )
        return {int(row["msg_id"]): row for row in (row_dict(cursor, r) for r in cursor.fetchall())}
    finally:
        con.close()


def load_main_rows(uid: str) -> list[dict[str, Any]]:
    key = get_key()
    con = open_nt_db(DEFAULT_DB_ROOT, "nt_msg.db", key)
    try:
        cursor = con.execute(
            """
            SELECT [40001] AS msg_id, [40050] AS create_time, [40020] AS sender_uid,
                   [40021] AS peer_uid, [40003] AS seq, [40011] AS msg_code,
                   [40012] AS type_code, [40013] AS status_code, [40800] AS payload,
                   [40900] AS ext_payload, [40030] AS sender_uin, [40033] AS self_uin
            FROM c2c_msg_table
            WHERE [40021] = ? OR [40020] = ? OR [40090] = ? OR [40093] = ?
            ORDER BY [40050] ASC, [40003] ASC, [40001] ASC
            """,
            (uid, uid, uid, uid),
        )
        return [row_dict(cursor, row) for row in cursor.fetchall()]
    finally:
        con.close()


def load_chat_fts_rows(chat: dict[str, Any], chat_type: str) -> dict[int, dict[str, Any]]:
    if chat_type == "private":
        return load_fts_rows(chat_identity_value(chat, chat_type))
    if chat_type not in {"group", "discuss"}:
        raise ValueError(f"Unsupported chat_type: {chat_type}")
    chat_id = chat_identity_value(chat, chat_type)
    db_name = "group_msg_fts.db" if chat_type == "group" else "discuss_msg_fts.db"
    table = "group_msg_fts" if chat_type == "group" else "discuss_msg_fts"
    chat_id_int = int(chat_id) if str(chat_id).isdigit() else None
    if chat_id_int is not None:
        where_clause = "[40027] = ?"
        params: tuple[Any, ...] = (chat_id_int,)
    else:
        where_clause = "[40021] = ?"
        params = (str(chat_id),)
    key = get_key()
    con = open_nt_db(DEFAULT_DB_ROOT, db_name, key)
    try:
        cursor = con.execute(
            f"""
            SELECT [40001] AS msg_id, [40050] AS create_time, [40020] AS sender_uid,
                   [40021] AS peer_uid, [40003] AS seq, [41701] AS text,
                   [41702] AS aux_text, [41703] AS msg_code, [41704] AS type_code,
                   [40027] AS peer_num
            FROM {table}
            WHERE {where_clause}
            """,
            params,
        )
        return {int(row["msg_id"]): row for row in (row_dict(cursor, r) for r in cursor.fetchall())}
    finally:
        con.close()


def load_chat_main_rows(chat: dict[str, Any], chat_type: str) -> tuple[list[dict[str, Any]], list[str]]:
    if chat_type == "private":
        return load_main_rows(chat_identity_value(chat, chat_type)), []
    if chat_type not in {"group", "discuss"}:
        raise ValueError(f"Unsupported chat_type: {chat_type}")
    chat_id = chat_identity_value(chat, chat_type)
    table = "group_msg_table" if chat_type == "group" else "discuss_msg_table"
    chat_id_int = int(chat_id) if str(chat_id).isdigit() else None
    if chat_id_int is not None:
        where_clause = "[40027] = ?"
        params: tuple[Any, ...] = (chat_id_int,)
    else:
        where_clause = "[40021] = ?"
        params = (str(chat_id),)
    key = get_key()
    con = open_nt_db(DEFAULT_DB_ROOT, "nt_msg.db", key)
    warnings: list[str] = []
    try:
        cursor = con.execute(
            f"""
            SELECT [40001] AS msg_id, [40050] AS create_time, [40020] AS sender_uid,
                   [40021] AS peer_uid, [40027] AS peer_num, [40003] AS seq,
                   [40011] AS msg_code, [40012] AS type_code, [40013] AS status_code,
                   [40800] AS payload, [40900] AS ext_payload, [40030] AS sender_uin,
                   [40033] AS self_uin
            FROM {table}
            WHERE {where_clause}
            ORDER BY [40050] ASC, [40003] ASC, [40001] ASC
            """,
            params,
        )
        return [row_dict(cursor, row) for row in cursor.fetchall()], warnings
    except Exception as exc:
        warnings.append(
            f"{table} read failed; using FTS-only records without payload/media hints: {exc}"
        )
        return [], warnings
    finally:
        con.close()


def build_main_record_index(main_rows: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    return {int(row["msg_id"]): row for row in main_rows if row.get("msg_id") is not None}


def merge_main_and_fts_rows(main_rows: list[dict[str, Any]], fts_rows: dict[int, dict[str, Any]]) -> list[dict[str, Any]]:
    rows_by_id = build_main_record_index(main_rows)
    for msg_id, fts in fts_rows.items():
        rows_by_id.setdefault(
            int(msg_id),
            {
                "msg_id": int(msg_id),
                "create_time": fts.get("create_time"),
                "sender_uid": fts.get("sender_uid"),
                "peer_uid": fts.get("peer_uid"),
                "peer_num": fts.get("peer_num"),
                "seq": fts.get("seq"),
                "msg_code": fts.get("msg_code"),
                "type_code": fts.get("type_code"),
                "status_code": None,
                "payload": None,
                "ext_payload": None,
                "sender_uin": None,
                "self_uin": None,
                "fts_only": True,
            },
        )
    return sorted(rows_by_id.values(), key=lambda row: (int(row.get("create_time") or 0), int(row.get("seq") or 0), int(row.get("msg_id") or 0)))


def sender_name_for_record(
    chat: dict[str, Any],
    chat_type: str,
    sender_uid: Any,
    row: dict[str, Any],
    member_map: dict[str, Any] | None,
) -> tuple[str, str]:
    sender_uid_str = str(sender_uid or "").strip()
    if chat_type == "private":
        uid = chat_identity_value(chat, chat_type)
        contact_name = chat_display_name(chat, chat_type)
        direction = "from_contact" if sender_uid_str == uid else "from_me"
        return ("我" if direction == "from_me" else contact_name), direction

    if member_map:
        member = (member_map.get("by_uid") or {}).get(sender_uid_str)
        if member is None:
            sender_uin = str(row.get("sender_uin") or "").strip()
            member = (member_map.get("by_uin") or {}).get(sender_uin)
        if member:
            sender = str(member.get("display_name") or member.get("nick") or member.get("card") or sender_uid_str).strip()
        else:
            sender = sender_uid_str or str(row.get("sender_uin") or "").strip() or "未知成员"
    else:
        sender = sender_uid_str or str(row.get("sender_uin") or "").strip() or "未知成员"

    self_uin = get_self_uin()
    direction = "from_me" if self_uin and str(row.get("self_uin") or "") == self_uin else "from_member"
    if direction == "from_me":
        sender = "我"
    return sender, direction


def build_records(
    main_rows: list[dict[str, Any]],
    fts_rows: dict[int, dict[str, Any]],
    contact: dict[str, Any],
    chat_type: str = "private",
    warnings: list[str] | None = None,
    member_map: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    if member_map is None and chat_type == "group":
        member_map = load_group_member_map(contact.get("group_id"))
    records: list[dict[str, Any]] = []
    for row in merge_main_and_fts_rows(main_rows, fts_rows):
        msg_id = int(row["msg_id"])
        fts = fts_rows.get(msg_id, {})
        sender_uid = row.get("sender_uid")
        sender_name, direction = sender_name_for_record(contact, chat_type, sender_uid, row, member_map)
        text = compact_text(str(fts.get("text") or ""))
        text_source = "fts" if text else None
        kind = message_kind(row.get("type_code"), row.get("msg_code"))
        if not text and fts.get("aux_text"):
            text = compact_text(str(fts.get("aux_text") or ""))
            text_source = "fts_aux"
        if not text:
            payload_text = extract_qq_payload_text(row.get("payload"))
            if payload_text:
                text = payload_text
                text_source = "payload.45101"
        recall_info = extract_recall_info(row.get("payload")) if kind == "recall/system" else {}
        if not text and recall_info.get("prompt"):
            text = str(recall_info["prompt"])
            text_source = "recall.prompt"
        hints = extract_blob_hints(row.get("payload"))
        ext_hints = extract_blob_hints(row.get("ext_payload"))
        if hints.get("images") and kind == "text":
            kind = "mixed/text-image"
        elif hints.get("images"):
            kind = "image" if not text else "mixed/text-image"
        record: dict[str, Any] = {
            "msg_id": str(msg_id),
            "time": timestamp_to_iso(row.get("create_time")),
            "timestamp": row.get("create_time"),
            "direction": direction,
            "sender": sender_name,
            "text": text,
            "kind": kind,
            "type_code": row.get("type_code"),
            "msg_code": row.get("msg_code"),
            "seq": row.get("seq"),
            "sender_uid": sender_uid,
            "peer_uid": row.get("peer_uid"),
            "chat_type": chat_type,
            "chat_id": chat_identity_value(contact, chat_type),
            "chat_name": chat_display_name(contact, chat_type),
            **parse_segments(row.get("payload")),
        }
        if row.get("peer_num") is not None:
            record["peer_num"] = row.get("peer_num")
        if row.get("sender_uin") is not None:
            record["sender_uin"] = row.get("sender_uin")
        if row.get("self_uin") is not None:
            record["self_uin"] = row.get("self_uin")
        if row.get("fts_only"):
            record["source"] = "fts_only"
        if text_source:
            record["text_source"] = text_source
        if row.get("_identity_warnings"):
            record["identity_status"] = "unresolved"
            record["warnings"] = list(row["_identity_warnings"])
        else:
            record["identity_status"] = "verified" if direction != "unknown" else "unknown"
        if recall_info:
            record["recall"] = {
                **recall_info,
                "recovered_original": False,
                "note": "This is the QQ recall prompt. Original withdrawn content is only available if it still exists in local payload/DB or was cached before recall.",
            }
        media: dict[str, Any] = {}
        for key in ("images", "files", "urls", "text_hints"):
            values: list[Any] = []
            for source in (hints, ext_hints):
                values.extend(source.get(key, []))
            if values:
                media[key] = list(dict.fromkeys(values))
        if media:
            record["media"] = media
        if warnings and row.get("fts_only"):
            record.setdefault("warnings", []).extend(warnings)
        records.append(record)
    return records


def load_contact_records(contact_query: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    contact = find_contact(contact_query)
    uid = str(contact.get("uid") or "")
    if not uid:
        raise RuntimeError(f"Matched contact has no UID: {contact}")
    fts_rows = load_fts_rows(uid)
    main_rows = load_main_rows(uid)
    return contact, build_records(main_rows, fts_rows, contact)


def load_chat_records(chat_query: str, chat_type: str = "private") -> tuple[dict[str, Any], list[dict[str, Any]], list[str]]:
    chat, resolved_type = resolve_chat_entity(chat_query, chat_type)
    fts_rows = load_chat_fts_rows(chat, resolved_type)
    main_rows, warnings = load_chat_main_rows(chat, resolved_type)
    return chat, build_records(main_rows, fts_rows, chat, resolved_type, warnings), warnings


def filter_records(records: list[dict[str, Any]], args: dict[str, Any]) -> list[dict[str, Any]]:
    after_ts = parse_time_bound(args.get("after"))
    before_ts = parse_time_bound(args.get("before"), before=True)
    keyword = str(args.get("keyword") or "").strip().lower()
    sender_field, sender = sender_filter(args)
    kind = str(args.get("kind_name") or "").strip()
    result: list[dict[str, Any]] = []
    for rec in records:
        if sender and sender != str(rec.get(sender_field) or ""):
            continue
        if kind and kind != str(rec.get("kind") or ""):
            continue
        ts = rec.get("timestamp")
        if after_ts is not None and (ts is None or int(ts) < after_ts):
            continue
        if before_ts is not None and (ts is None or int(ts) >= before_ts):
            continue
        if keyword:
            haystack = json.dumps(
                {"text": rec.get("text"), "kind": rec.get("kind"), "media": rec.get("media")},
                ensure_ascii=False,
            ).lower()
            if keyword not in haystack:
                continue
        result.append(rec)
    return result


def sender_filter(args):
    """Use one namespace per selector; numeric nicknames require sender_name."""
    named = [(key, str(args[key]).strip()) for key in ("sender_uid", "sender_uin", "sender_name", "sender_direction")
             if args.get(key) not in (None, "")]
    raw = str(args.get("sender") or "").strip()
    if len(named) > 1 or (named and raw):
        raise ValueError("Use one QQ sender selector: UID, UIN, name, or direction")
    mapping = {"sender_uid": "sender_uid", "sender_uin": "sender_uin", "sender_name": "sender", "sender_direction": "direction"}
    if named:
        key, value = named[0]
        return mapping[key], value
    if not raw:
        return None, ""
    if ":" in raw:
        prefix, value = raw.split(":", 1)
        if prefix in {"uid", "uin", "name", "direction"}:
            if not value.strip():
                raise ValueError("QQ sender selector must not be empty")
            return {"uid": "sender_uid", "uin": "sender_uin", "name": "sender", "direction": "direction"}[prefix], value.strip()
    if raw.isdigit():
        return "sender_uin", raw
    if raw.startswith("u_"):
        return "sender_uid", raw
    if raw in {"from_me", "from_contact", "from_member", "unknown"}:
        return "direction", raw
    return "sender", raw


def window_records(records: list[dict[str, Any]], args: dict[str, Any], *, default_limit: int = 50) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    order = str(args.get("order") or "desc").lower()
    if order not in {"asc", "desc"}:
        order = "desc"
    display_order = str(args.get("display_order") or args.get("displayOrder") or "query").lower()
    if display_order not in {"query", "asc", "desc"}:
        display_order = "query"
    limit = bounded_int(args.get("limit"), default_limit, 1, 1000)
    offset = bounded_int(args.get("offset"), 0, 0, 1_000_000_000)

    ordered = sorted(records, key=lambda rec: (int(rec.get("timestamp") or 0), int(rec.get("seq") or 0), int(rec.get("msg_id") or 0)))
    if order == "desc":
        ordered.reverse()
    window = ordered[offset : offset + limit]
    if display_order == "asc":
        window = sorted(window, key=lambda rec: (int(rec.get("timestamp") or 0), int(rec.get("seq") or 0), int(rec.get("msg_id") or 0)))
    elif display_order == "desc":
        window = sorted(window, key=lambda rec: (int(rec.get("timestamp") or 0), int(rec.get("seq") or 0), int(rec.get("msg_id") or 0)), reverse=True)
    return window, {
        "returned": len(window),
        "total": len(records),
        "limit": limit,
        "offset": offset,
        "has_more": offset + limit < len(records),
        "next_offset": offset + limit if offset + limit < len(records) else None,
        "order": order,
        "display_order": display_order,
    }


def strip_media_if_needed(records: list[dict[str, Any]], include_media: bool) -> list[dict[str, Any]]:
    if include_media:
        return records
    stripped = []
    for rec in records:
        item = dict(rec)
        item.pop("media", None)
        stripped.append(item)
    return stripped


def record_sort_key(rec: dict[str, Any]) -> tuple[int, int, int]:
    def as_int(value: Any) -> int:
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    return (as_int(rec.get("timestamp")), as_int(rec.get("seq")), as_int(rec.get("msg_id")))


def is_recall_record(rec: dict[str, Any]) -> bool:
    if rec.get("kind") == "recall/system":
        return True
    text = str(rec.get("text") or "")
    try:
        type_code = int(rec.get("type_code") or -1)
    except (TypeError, ValueError):
        type_code = -1
    try:
        msg_code = int(rec.get("msg_code") or -1)
    except (TypeError, ValueError):
        msg_code = -1
    if type_code == 4 and msg_code == 5:
        return True
    return bool(text and "撤回" in text and type_code == 4)


def has_recoverable_text(rec: dict[str, Any]) -> bool:
    return bool(str(rec.get("text") or "").strip()) and not is_recall_record(rec)


def has_recoverable_content(rec: dict[str, Any]) -> bool:
    if is_recall_record(rec):
        return False
    if str(rec.get("text") or "").strip():
        return True
    media = rec.get("media") or {}
    return bool(media.get("images") or media.get("files") or media.get("cached_images"))


def possible_image_cache_paths(image_name: str) -> list[Path]:
    data_root = DEFAULT_DATA_ROOT
    if data_root is None:
        return []
    lower_name = image_name.lower()
    stem = Path(image_name).stem.lower()
    suffix = Path(image_name).suffix.lower()
    candidates: list[Path] = []
    for base in (
        data_root / "Pic",
        data_root / "Pic" / dt.datetime.now(LOCAL_TZ).strftime("%Y-%m"),
    ):
        if not base.exists():
            continue
        for family in ("Ori", "OriTemp", "Thumb", "ThumbTemp"):
            folder = base / family
            if not folder.exists():
                continue
            patterns = [
                lower_name,
                lower_name.upper(),
                stem + suffix,
                stem + "_0" + suffix,
                stem + "_720" + suffix,
                stem + "_0.*",
                stem + "_720.*",
            ]
            for pattern in patterns:
                candidates.extend(folder.glob(pattern))
    readable: list[Path] = []
    seen: set[str] = set()
    for path in candidates:
        if not path.is_file():
            continue
        key = str(path).lower()
        if key in seen:
            continue
        seen.add(key)
        readable.append(path)
    readable.sort(key=lambda p: (0 if "ori" in str(p).lower() else 1, -p.stat().st_size))
    return readable


def attach_cached_media(contact: dict[str, Any], rec: dict[str, Any]) -> dict[str, Any]:
    media = rec.get("media") or {}
    image_names = media.get("images") or []
    if not image_names:
        return rec
    cached_images: list[dict[str, Any]] = []
    contact_key = safe_cache_name(rec.get("chat_id") or contact.get("uid") or contact.get("uin") or contact.get("display_name"))
    msg_id = safe_cache_name(rec.get("msg_id"))
    for image_name in image_names:
        source_paths = possible_image_cache_paths(str(image_name))
        if not source_paths:
            continue
        src = source_paths[0]
        ext = src.suffix or Path(str(image_name)).suffix or ".img"
        dest_dir = CACHE_MEDIA_DIR / contact_key
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / f"{safe_cache_name(msg_id)}_{safe_cache_name(Path(str(image_name)).stem)}{ext.lower()}"
        if not dest.exists() or dest.stat().st_size != src.stat().st_size:
            shutil.copy2(src, dest)
        cached_images.append(
            {
                "image": image_name,
                "source_path": str(src),
                "cache_path": str(dest),
                "bytes": dest.stat().st_size,
            }
        )
    if cached_images:
        rec = dict(rec)
        media = dict(media)
        media["cached_images"] = cached_images
        rec["media"] = media
    return rec


def safe_cache_name(value: Any) -> str:
    raw = str(value or "unknown")
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", raw).strip("._")
    return cleaned[:96] or "unknown"


def cache_path_for_contact(contact: dict[str, Any]) -> Path:
    chat_type = normalize_chat_type(contact.get("chat_type") or "private")
    if chat_type == "group":
        identity = contact.get("group_id") or contact.get("id") or contact.get("display_name")
        prefix = "group"
    elif chat_type == "discuss":
        identity = contact.get("discuss_id") or contact.get("id") or contact.get("display_name")
        prefix = "discuss"
    else:
        identity = contact.get("uid") or contact.get("uin") or contact.get("display_name")
        prefix = "private"
    return CACHE_DIR / f"{prefix}_{safe_cache_name(identity)}.jsonl"


def load_cached_entries(contact: dict[str, Any]) -> list[dict[str, Any]]:
    path = cache_path_for_contact(contact)
    with cache_lock(path.with_suffix(path.suffix + ".lock")):
        return _load_cached_entries_unlocked(contact)


def _load_cached_entries_unlocked(contact: dict[str, Any]) -> list[dict[str, Any]]:
    path = cache_path_for_contact(contact)
    if not path.exists():
        return []
    entries: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"QQ cache is malformed; preserve and inspect {path.name} before retrying") from exc
            if isinstance(entry, dict) and isinstance(entry.get("record"), dict):
                entries.append(entry)
            else:
                raise ValueError(f"QQ cache entry has an invalid shape; preserve and inspect {path.name} before retrying")
    return entries


def write_cached_entries(contact: dict[str, Any], entries: list[dict[str, Any]]) -> Path:
    path = cache_path_for_contact(contact)
    with cache_lock(path.with_suffix(path.suffix + ".lock")):
        return _write_cached_entries_unlocked(contact, entries)


def _write_cached_entries_unlocked(contact: dict[str, Any], entries: list[dict[str, Any]]) -> Path:
    path = cache_path_for_contact(contact)
    path.parent.mkdir(parents=True, exist_ok=True)
    ordered = sorted(entries, key=lambda entry: record_sort_key(entry.get("record") or {}))
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            for entry in ordered:
                f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return path


def merge_cached_records(contact: dict[str, Any], records: list[dict[str, Any]]) -> dict[str, Any]:
    path = cache_path_for_contact(contact)
    with cache_lock(path.with_suffix(path.suffix + ".lock")):
        return _merge_cached_records_unlocked(contact, records)


def _merge_cached_records_unlocked(contact: dict[str, Any], records: list[dict[str, Any]]) -> dict[str, Any]:
    now = dt.datetime.now(LOCAL_TZ).isoformat(sep=" ", timespec="seconds")
    existing = {str(entry.get("record", {}).get("msg_id")): entry for entry in _load_cached_entries_unlocked(contact)}
    added = 0
    updated = 0
    preserved = 0
    for rec in records:
        rec = attach_cached_media(contact, rec)
        msg_id = str(rec.get("msg_id") or "")
        if not msg_id:
            continue
        prior = existing.get(msg_id)
        if prior is None:
            existing[msg_id] = {
                "msg_id": msg_id,
                "first_cached_at": now,
                "last_cached_at": now,
                "seen_count": 1,
                "record": rec,
            }
            added += 1
            continue
        prior_record = prior.get("record") or {}
        prior["last_cached_at"] = now
        prior["seen_count"] = int(prior.get("seen_count") or 1) + 1
        if has_recoverable_content(prior_record) and not has_recoverable_content(rec):
            preserved += 1
            continue
        if rec != prior_record:
            prior["record"] = rec
            updated += 1
    path = _write_cached_entries_unlocked(contact, list(existing.values()))
    return {
        "cache_path": str(path),
        "cached_total": len(existing),
        "added": added,
        "updated": updated,
        "preserved_original_text": preserved,
    }


def find_cached_recall_candidates(
    contact: dict[str, Any],
    event: dict[str, Any],
    live_ids: set[str],
    *,
    window_seconds: int,
    limit: int,
) -> dict[str, Any]:
    entries = load_cached_entries(contact)
    cache_by_id = {str(entry.get("record", {}).get("msg_id")): entry for entry in entries}
    same_entry = cache_by_id.get(str(event.get("msg_id")))
    exact_record = same_entry.get("record") if same_entry else None
    result: dict[str, Any] = {
        "cache_available": bool(entries),
        "recovered_original": False,
        "exact": None,
        "candidates": [],
    }
    if isinstance(exact_record, dict) and has_recoverable_content(exact_record):
        result["recovered_original"] = True
        result["exact"] = {
            "confidence": "same_msg_id",
            "cached_at": same_entry.get("first_cached_at"),
            "record": exact_record,
        }
        return result

    event_ts = int(event.get("timestamp") or 0)
    candidates: list[dict[str, Any]] = []
    if event_ts > 0 and window_seconds > 0:
        for entry in entries:
            rec = entry.get("record") or {}
            if not isinstance(rec, dict) or not has_recoverable_content(rec):
                continue
            msg_id = str(rec.get("msg_id") or "")
            if not msg_id or msg_id in live_ids:
                continue
            rec_ts = int(rec.get("timestamp") or 0)
            delta = event_ts - rec_ts
            if 0 <= delta <= window_seconds:
                candidates.append(
                    {
                        "confidence": "cached_disappeared_before_recall",
                        "seconds_before_recall": delta,
                        "cached_at": entry.get("first_cached_at"),
                        "record": rec,
                    }
                )
    candidates.sort(key=lambda item: int(item.get("seconds_before_recall") or 0))
    result["candidates"] = candidates[:limit]
    return result


def summarize_records(records: list[dict[str, Any]], contact: dict[str, Any]) -> dict[str, Any]:
    by_month: Counter[str] = Counter()
    by_sender: Counter[str] = Counter()
    by_kind: Counter[str] = Counter()
    for rec in records:
        if rec.get("time"):
            by_month[str(rec["time"])[:7]] += 1
        by_sender[str(rec.get("sender") or "")] += 1
        by_kind[str(rec.get("kind") or "")] += 1
    return {
        "contact": contact,
        "total_messages": len(records),
        "text_indexed_messages": sum(1 for rec in records if rec.get("text")),
        "media_or_unindexed_messages": sum(1 for rec in records if not rec.get("text")),
        "first_time": records[0]["time"] if records else None,
        "last_time": records[-1]["time"] if records else None,
        "by_sender": dict(by_sender),
        "by_kind": dict(by_kind),
        "by_month": dict(sorted(by_month.items())),
    }


def tool_diagnose(args: dict[str, Any]) -> dict[str, Any]:
    key_env, key_present = get_key_status()
    active_key_env = key_env if key_env in KEY_ENV_NAMES else None
    checks: dict[str, Any] = {
        "python": sys.executable,
        "sqlcipher3_available": sqlcipher3 is not None,
        "db_root": str(DEFAULT_DB_ROOT) if DEFAULT_DB_ROOT is not None else None,
        "db_root_configured": DEFAULT_DB_ROOT is not None,
        "db_root_exists": DEFAULT_DB_ROOT is not None and DEFAULT_DB_ROOT.is_dir(),
        "extension": str(DEFAULT_EXT),
        "extension_exists": DEFAULT_EXT.exists(),
        "dpapi_key_file": str(DEFAULT_KEY_FILE),
        "dpapi_key_file_exists": DEFAULT_KEY_FILE.exists(),
        "key_env_candidates": list(KEY_ENV_NAMES),
        "key_configured": key_present,
        "active_key_env": active_key_env,
        "active_key_source": key_env,
    }
    if DEFAULT_DB_ROOT is None:
        checks.update(profile_opened=False, ready=False,
                      next_step="Set QQ_MCP_DB_ROOT to the intended account's nt_qq/nt_db directory. QQ is optional; no account is selected automatically.")
        return checks
    if sqlcipher3 is not None and DEFAULT_DB_ROOT.exists() and DEFAULT_EXT.exists():
        try:
            ensure_vfs()
            checks["offset_vfs_loaded"] = True
        except Exception as exc:
            checks["offset_vfs_loaded"] = False
            checks["offset_vfs_error"] = str(exc)
    if key_present:
        try:
            con = open_nt_db(DEFAULT_DB_ROOT, "profile_info.db", get_key())
            try:
                count = con.execute("SELECT COUNT(*) FROM profile_info_v6").fetchone()[0]
                checks["profile_opened"] = True
                checks["profile_count"] = count
            finally:
                con.close()
        except Exception as exc:
            checks["profile_opened"] = False
            checks["profile_error"] = str(exc)
    else:
        checks["next_step"] = "Set QQNT_DB_KEY before starting Codex, or create the DPAPI key file. The MCP intentionally does not store plaintext keys."
    return checks


def tool_resolve_contact(args: dict[str, Any]) -> dict[str, Any]:
    query = str(args.get("query") or args.get("contact") or args.get("keyword") or "").strip()
    limit = bounded_int(args.get("limit"), 10, 1, 100)
    candidates = resolve_contacts(query, limit)
    return {"query": query, "returned": len(candidates), "candidates": candidates,
            "complete": candidates.complete, "truncated": not candidates.complete,
            "identity_verified": False, "note": "Discovery candidates only; actual reads require exact unique UID/UIN or exact name."}


def tool_resolve_group(args: dict[str, Any]) -> dict[str, Any]:
    query = str(args.get("query") or args.get("group") or args.get("keyword") or "").strip()
    limit = bounded_int(args.get("limit"), 10, 1, 100)
    candidates = resolve_groups(query, limit)
    return {"query": query, "returned": len(candidates), "candidates": candidates,
            "complete": candidates.complete, "truncated": not candidates.complete,
            "identity_verified": False, "note": "Discovery candidates only; actual reads require exact unique group ID or exact name."}


def tool_messages(args: dict[str, Any]) -> dict[str, Any]:
    # Standalone QQ and the unified gateway share one bounded implementation.
    from unified_mcp import qq_adapter
    return qq_adapter.call("messages", args)


def tool_chat_timeline(args: dict[str, Any]) -> dict[str, Any]:
    # Standalone QQ and the unified gateway share one bounded implementation.
    from unified_mcp import qq_adapter
    return qq_adapter.call("chat_timeline", args)


def tool_search(args: dict[str, Any]) -> dict[str, Any]:
    # Standalone QQ and the unified gateway share one bounded implementation.
    from unified_mcp import qq_adapter
    return qq_adapter.call("search", args)


def tool_cache_recent(args: dict[str, Any]) -> dict[str, Any]:
    # Standalone QQ and the unified gateway share one bounded implementation.
    from unified_mcp import qq_adapter
    return qq_adapter.call("cache_recent", args)


def tool_recall_events(args: dict[str, Any]) -> dict[str, Any]:
    # Standalone QQ and the unified gateway share one bounded implementation.
    from unified_mcp import qq_adapter
    return qq_adapter.call("recall_events", args)


def tool_stats(args: dict[str, Any]) -> dict[str, Any]:
    # Standalone QQ and the unified gateway share one bounded implementation.
    from unified_mcp import qq_adapter
    return qq_adapter.call("stats", args)


def tool_export_messages(args: dict[str, Any]) -> dict[str, Any]:
    # Standalone QQ and the unified gateway share one bounded implementation.
    from unified_mcp import qq_adapter
    return qq_adapter.call("export_messages", args)


TOOLS: list[dict[str, Any]] = [
    {
        "name": "diagnose",
        "description": "Check QQ MCP readiness without exposing the QQNT database key.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": True},
    },
    {
        "name": "resolve_contact",
        "description": "Resolve a QQ private contact by remark, nickname, QQ number, or UID.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer", "default": 10},
            },
            "required": ["query"],
            "additionalProperties": True,
        },
    },
    {
        "name": "resolve_group",
        "description": "Resolve a QQ group by group name or group number.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer", "default": 10},
            },
            "required": ["query"],
            "additionalProperties": True,
        },
    },
    {
        "name": "messages",
        "description": "Read QQNT messages for one private contact, group, or discuss chat.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "contact": {"type": "string"},
                "group": {"type": "string"},
                "chat_type": {"type": "string", "enum": ["private", "group", "discuss"], "default": "private"},
                "after": {"type": "string", "description": "Unix seconds or YYYY-MM-DD"},
                "before": {"type": "string", "description": "Unix seconds or YYYY-MM-DD"},
                "keyword": {"type": "string"},
                "limit": {"type": "integer", "default": 50},
                "offset": {"type": "integer", "default": 0},
                "order": {"type": "string", "enum": ["asc", "desc"], "default": "desc"},
                "display_order": {"type": "string", "enum": ["query", "asc", "desc"], "default": "query"},
                "include_media": {"type": "boolean", "default": True},
            },
            "required": [],
            "additionalProperties": True,
        },
    },
    {
        "name": "chat_timeline",
        "description": "Agent-friendly QQ chat timeline; defaults to recent window in chat order.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "contact": {"type": "string"},
                "group": {"type": "string"},
                "chat_type": {"type": "string", "enum": ["private", "group", "discuss"], "default": "private"},
                "after": {"type": "string"},
                "before": {"type": "string"},
                "keyword": {"type": "string"},
                "limit": {"type": "integer", "default": 50},
                "offset": {"type": "integer", "default": 0},
                "order": {"type": "string", "enum": ["asc", "desc"], "default": "desc"},
                "display_order": {"type": "string", "enum": ["query", "asc", "desc"], "default": "asc"},
            },
            "required": [],
            "additionalProperties": True,
        },
    },
    {
        "name": "search",
        "description": "Search within one QQNT private, group, or discuss chat.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "contact": {"type": "string"},
                "group": {"type": "string"},
                "chat_type": {"type": "string", "enum": ["private", "group", "discuss"], "default": "private"},
                "keyword": {"type": "string"},
                "after": {"type": "string"},
                "before": {"type": "string"},
                "limit": {"type": "integer", "default": 50},
                "offset": {"type": "integer", "default": 0},
            },
            "required": ["keyword"],
            "additionalProperties": True,
        },
    },
    {
        "name": "cache_recent",
        "description": "Snapshot current QQ private/group/discuss messages into a local cache for future recall recovery.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "contact": {"type": "string"},
                "group": {"type": "string"},
                "chat_type": {"type": "string", "enum": ["private", "group", "discuss"], "default": "private"},
                "after": {"type": "string"},
                "before": {"type": "string"},
                "keyword": {"type": "string"},
                "limit": {"type": "integer", "default": 500},
                "order": {"type": "string", "enum": ["asc", "desc"], "default": "desc"},
                "include_media": {"type": "boolean", "default": False},
            },
            "required": [],
            "additionalProperties": True,
        },
    },
    {
        "name": "recall_events",
        "description": "List QQ private/group/discuss recall events with nearby context and cached original candidates when available.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "contact": {"type": "string"},
                "group": {"type": "string"},
                "chat_type": {"type": "string", "enum": ["private", "group", "discuss"], "default": "private"},
                "after": {"type": "string"},
                "before": {"type": "string"},
                "limit": {"type": "integer", "default": 20},
                "order": {"type": "string", "enum": ["asc", "desc"], "default": "desc"},
                "context_window": {"type": "integer", "default": 3},
                "cache_window_seconds": {"type": "integer", "default": 3600},
                "include_media": {"type": "boolean", "default": False},
            },
            "required": [],
            "additionalProperties": True,
        },
    },
    {
        "name": "stats",
        "description": "Summarize one QQNT private, group, or discuss chat by sender, month, and message kind.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "contact": {"type": "string"},
                "group": {"type": "string"},
                "chat_type": {"type": "string", "enum": ["private", "group", "discuss"], "default": "private"},
                "after": {"type": "string"},
                "before": {"type": "string"},
                "keyword": {"type": "string"},
            },
            "required": [],
            "additionalProperties": True,
        },
    },
    {
        "name": "export_messages",
        "description": "Export one QQNT private, group, or discuss chat to jsonl or markdown.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "contact": {"type": "string"},
                "group": {"type": "string"},
                "chat_type": {"type": "string", "enum": ["private", "group", "discuss"], "default": "private"},
                "path": {"type": "string"},
                "overwrite": {"type": "boolean", "default": False,
                              "description": "Explicitly allow replacing an existing export; otherwise refuse."},
                "format": {"type": "string", "enum": ["jsonl", "markdown"], "default": "jsonl"},
                "after": {"type": "string"},
                "before": {"type": "string"},
                "keyword": {"type": "string"},
                "limit": {"type": "integer"},
            },
            "required": ["path"],
            "additionalProperties": True,
        },
    },
]

# The same schemas are visible before the first data call in standalone mode.
for _tool in TOOLS:
    if _tool["name"] in {"messages", "chat_timeline", "search"}:
        _tool["inputSchema"]["properties"]["cursor"] = {
            "type": "string", "description": "Opaque query.next_cursor from the previous page; do not combine with nonzero offset."}
    if _tool["name"] in {"messages", "chat_timeline", "search", "stats", "export_messages", "cache_recent", "recall_events"}:
        _tool["inputSchema"]["properties"]["sender"] = {
            "type": "string", "description": "One namespace: uid:/uin:/name:/direction:. Bare digits mean UIN; u_ prefix means UID; other text means exact name."}
        for _field in ("sender_uid", "sender_uin", "sender_name", "sender_direction"):
            _tool["inputSchema"]["properties"][_field] = {"type": "string", "description": "Exact sender selector; choose one field and do not combine with sender."}
        _tool["inputSchema"]["properties"]["kind_name"] = {
            "type": "string", "description": "Exact normalized kind (text, image, mixed/text-image, recall/system, reply/quote, etc.)."}


TOOL_HANDLERS = {
    "diagnose": tool_diagnose,
    "resolve_contact": tool_resolve_contact,
    "resolve_group": tool_resolve_group,
    "messages": tool_messages,
    "chat_timeline": tool_chat_timeline,
    "search": tool_search,
    "cache_recent": tool_cache_recent,
    "recall_events": tool_recall_events,
    "stats": tool_stats,
    "export_messages": tool_export_messages,
}


def json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


def read_message() -> dict[str, Any] | None:
    first = sys.stdin.buffer.readline()
    while first == b"\r\n" or first == b"\n":
        first = sys.stdin.buffer.readline()
    if not first:
        return None
    if first.lower().startswith(b"content-length:"):
        headers = [first]
        while True:
            line = sys.stdin.buffer.readline()
            if not line:
                return None
            if line in (b"\r\n", b"\n"):
                break
            headers.append(line)
        length: int | None = None
        for header in headers:
            name, _, value = header.decode("ascii", errors="ignore").partition(":")
            if name.lower() == "content-length":
                length = int(value.strip())
                break
        if length is None:
            raise ValueError("Missing Content-Length header.")
        body = sys.stdin.buffer.read(length)
        return json.loads(body.decode("utf-8"))
    return json.loads(first.decode("utf-8"))


def send_message(message: dict[str, Any]) -> None:
    body = json.dumps(message, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")
    sys.stdout.buffer.write(f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body)
    sys.stdout.buffer.flush()


def success_response(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def error_response(request_id: Any, code: int, message: str, data: Any | None = None) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


def handle_tool_call(params: dict[str, Any]) -> dict[str, Any]:
    name = params.get("name")
    args = params.get("arguments") or {}
    if not isinstance(args, dict):
        raise ValueError("tools/call arguments must be an object.")
    handler = TOOL_HANDLERS.get(str(name))
    if handler is None:
        raise KeyError(f"Unknown tool: {name}")
    result = handler(args)
    return {"content": [{"type": "text", "text": json_text(result)}], "isError": False}


def handle_request(message: dict[str, Any]) -> dict[str, Any] | None:
    method = message.get("method")
    request_id = message.get("id")
    params = message.get("params") or {}
    if method and str(method).startswith("notifications/"):
        return None
    try:
        if method == "initialize":
            result = {
                "protocolVersion": params.get("protocolVersion") or PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            }
            return success_response(request_id, result)
        if method == "ping":
            return success_response(request_id, {})
        if method == "tools/list":
            return success_response(request_id, {"tools": TOOLS})
        if method == "tools/call":
            return success_response(request_id, handle_tool_call(params))
        return error_response(request_id, -32601, f"Method not found: {method}")
    except Exception as exc:
        data = {"traceback": traceback.format_exc(limit=5)}
        if method == "tools/call":
            tool_result = {"content": [{"type": "text", "text": json_text({"error": str(exc)})}], "isError": True}
            return success_response(request_id, tool_result)
        return error_response(request_id, -32603, str(exc), data)


def main() -> int:
    while True:
        try:
            message = read_message()
            if message is None:
                return 0
            response = handle_request(message)
            if response is not None:
                send_message(response)
        except Exception as exc:
            print(f"{SERVER_NAME}: fatal protocol error: {exc}", file=sys.stderr)
            print(traceback.format_exc(limit=5), file=sys.stderr)
            return 1


if __name__ == "__main__":
    raise SystemExit(main())
