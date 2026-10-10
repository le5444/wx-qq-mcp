"""Reuse the local QQ reader, with per-database keys and bounded SQL reads.

All overrides are process-local; the original QQ server file remains untouched.
"""

from __future__ import annotations

import json
import os
import sys
import time
from contextvars import ContextVar
from pathlib import Path

from .qq_keys import KEY_FILE, database_salt, legacy, open_raw_db

SCOPE = ContextVar("qq_query_scope", default={})
_original_open = legacy.open_nt_db
_original_key = legacy.get_key
_original_key_status = legacy.get_key_status
_original_sender = legacy.sender_name_for_record
_keys_stamp = None
_keys = {}
_self_identity = None


def key_map():
    global _keys_stamp, _keys
    path = Path(os.environ.get("QQ_MCP_RAW_KEY_FILE", str(KEY_FILE)))
    if not path.is_file():
        return {}
    stat = path.stat()
    stamp = (str(path), stat.st_mtime_ns, stat.st_size)
    if stamp != _keys_stamp:
        _keys = json.loads(legacy.decrypt_dpapi_file(path))["keys"]
        _keys_stamp = stamp
    return _keys


def get_key():
    if key_map():
        return "raw-key-map"
    return _original_key()


def key_status():
    if legacy.DEFAULT_DB_ROOT is not None and key_map():
        return str(Path(os.environ.get("QQ_MCP_RAW_KEY_FILE", str(KEY_FILE)))), True
    return _original_key_status()


def open_db(root, name, key):
    if root is None:
        raise RuntimeError("QQ is not configured. Set QQ_MCP_DB_ROOT to the intended account's nt_qq/nt_db directory.")
    path = root / name
    raw = key_map().get(database_salt(path))
    if raw:
        con = open_raw_db(root, name, raw)
    elif key == "raw-key-map":
        raise RuntimeError(f"No verified local QQ key for {name}; refresh the protected key map.")
    else:
        con = _original_open(root, name, key)
        con.execute("PRAGMA query_only = ON")
    deadline = time.monotonic() + 45
    con.set_progress_handler(lambda: int(time.monotonic() > deadline), 10000)
    return con


def sender_name(chat, chat_type, sender_uid, row, members):
    uid = str(sender_uid or "")
    sender_uin = str(row.get("sender_uin") or "")
    me = str(legacy.get_self_uin() or "")
    self_uid = (_self_identity or {}).get("uid") or os.environ.get("QQ_MCP_SELF_UID")
    if uid and self_uid and uid == self_uid:
        return "\u6211", "from_me"
    if sender_uin and me and sender_uin == me:
        return "\u6211", "from_me"
    if chat_type == "private":
        if uid and uid == str(chat.get("uid") or ""):
            return legacy.chat_display_name(chat, chat_type), "from_contact"
        if sender_uin and sender_uin == str(chat.get("uin") or ""):
            return legacy.chat_display_name(chat, chat_type), "from_contact"
        return uid or sender_uin or "unknown", "unknown"
    if not uid and sender_uin in {"", "0"}:
        return "unknown", "unknown"
    # The legacy reader aliases 40030/40033 incorrectly. Never use its direction test.
    clean_row = {**row, "self_uin": None}
    return _original_sender(chat, chat_type, sender_uid, clean_row, members)


def require_unambiguous(candidates, query):
    exact = [c for c in candidates if query in {
        str(c.get(k) or "") for k in ("uid", "uin", "group_id", "display_name", "remark", "nick")
    }]
    selected = exact or candidates
    if len(selected) != 1:
        raise ValueError("QQ chat not uniquely identified; use resolve_contact/resolve_group and pass its stable ID.")
    return selected[0]


def time_clause():
    args = SCOPE.get()
    parts, params = [], []
    for field, op, end in (("after", ">=", False), ("before", "<", True)):
        value = legacy.parse_time_bound(args.get(field), before=end)
        if value is not None:
            parts.append(f"[40050] {op} ?")
            params.append(value)
    return (" AND " + " AND ".join(parts) if parts else ""), params


def query_rows(chat, chat_type, *, fts):
    if chat_type not in {"private", "group", "discuss"}:
        raise ValueError("Unsupported QQ chat type")
    ident = legacy.chat_identity_value(chat, chat_type)
    table_prefix = {"private": "c2c", "group": "group", "discuss": "discuss"}[chat_type]
    if fts:
        db = {"private": "buddy_msg_fts.db", "group": "group_msg_fts.db", "discuss": "discuss_msg_fts.db"}[chat_type]
        table = db[:-3]
        fields = "[41701] AS text, [41702] AS aux_text, [41703] AS msg_code, [41704] AS type_code"
    else:
        db, table = "nt_msg.db", table_prefix + "_msg_table"
        fields = "[40011] AS msg_code, [40012] AS type_code, [40013] AS status_code, [40800] AS payload, [40900] AS ext_payload, [40033] AS sender_uin, [40030] AS peer_uin"
    if chat_type == "private":
        columns = ("40021", "40020")
        where = " OR ".join(f"[{column}] = ?" for column in columns)
        params = [ident] * len(columns)
    elif str(ident).isdigit():
        where, params = "[40027] = ?", [int(ident)]
    else:
        where, params = "[40021] = ?", [str(ident)]
    time_sql, time_params = time_clause()
    extra = ", [40027] AS peer_num" if chat_type != "private" else ""
    con = open_db(legacy.DEFAULT_DB_ROOT, db, get_key())
    try:
        cur = con.execute(
            f"SELECT [40001] AS msg_id, [40050] AS create_time, [40020] AS sender_uid, "
            f"[40021] AS peer_uid, [40003] AS seq{extra}, {fields} FROM {table} "
            f"WHERE ({where}){time_sql} ORDER BY [40050], [40003], [40001]",
            params + time_params,
        )
        rows = [legacy.row_dict(cur, row) for row in cur]
        return {int(row["msg_id"]): row for row in rows} if fts else rows
    finally:
        con.close()


def load_main(chat, kind):
    # A missing primary database is an error, never a silently complete FTS result.
    return query_rows(chat, kind, fts=False), []


def load_records(query, kind="private"):
    chat, resolved = legacy.resolve_chat_entity(query, kind)
    main_rows, warnings = load_main(chat, resolved)
    try:
        fts = query_rows(chat, resolved, fts=True)
    except Exception as exc:
        fts = {}
        warnings.append(f"QQ FTS unavailable; main payloads only: {exc}")
    return chat, legacy.build_records(main_rows, fts, chat, resolved, warnings), warnings


def install():
    legacy.open_nt_db = open_db
    legacy.get_key = get_key
    legacy.get_key_status = key_status
    legacy.sender_name_for_record = sender_name
    legacy.find_contact = lambda query: require_unambiguous(legacy.resolve_contacts(query, 100), query)
    legacy.find_group = lambda query: require_unambiguous(legacy.resolve_groups(query, 100), query)
    legacy.load_chat_records = load_records
    legacy.load_fts_rows = lambda uid: query_rows({"uid": uid}, "private", fts=True)
    legacy.load_main_rows = lambda uid: query_rows({"uid": uid}, "private", fts=False)
    legacy.load_chat_fts_rows = lambda chat, kind: query_rows(chat, kind, fts=True)
    legacy.load_chat_main_rows = load_main


install()


def call(name, args):
    global _self_identity
    if name != "diagnose" and legacy.DEFAULT_DB_ROOT is None:
        raise RuntimeError("QQ is not configured. Set QQ_MCP_DB_ROOT to the intended account's nt_qq/nt_db directory; WeChat remains available.")
    token = SCOPE.set(args)
    try:
        if _self_identity is None and name not in {"diagnose", "resolve_contact", "resolve_group"}:
            own = legacy.resolve_contacts(str(legacy.get_self_uin()), 10)
            _self_identity = require_unambiguous(own, str(legacy.get_self_uin()))
        from . import qq_paging
        if name in {"messages", "chat_timeline", "search"}:
            if name == "search" and not str(args.get("keyword") or "").strip():
                raise ValueError("search requires keyword.")
            effective = {"order": "desc", "display_order": "asc", "limit": 50, **args} if name == "chat_timeline" else args
            result = qq_paging.messages(sys.modules[__name__], effective)
        elif name in {"cache_recent", "stats", "export_messages", "recall_events"}:
            result = getattr(qq_paging, name)(sys.modules[__name__], args)
        else:
            result = legacy.TOOL_HANDLERS[name](args)
        if name == "diagnose":
            result["per_database_keys_configured"] = bool(key_map()) if legacy.DEFAULT_DB_ROOT is not None else False
        if isinstance(result, dict) and isinstance(result.get("messages"), list):
            if args.get("include_media", True):
                from .qq_media import resolve_page_images
                if legacy.DEFAULT_DATA_ROOT is not None:
                    result["messages"] = resolve_page_images(result["messages"], legacy.DEFAULT_DATA_ROOT)
                else:
                    result.setdefault("warnings", []).append("QQ media directory is not configured; media hints are unverified.")
            result["freshness"] = {"message_source": "local_qq_database", "read_at": legacy.dt.datetime.now(legacy.LOCAL_TZ).isoformat()}
            result.setdefault("warnings", []).append("Only locally synchronized QQ records are available. images[].path is pixel-validated; media hints alone do not establish availability.")
        return result
    finally:
        SCOPE.reset(token)
