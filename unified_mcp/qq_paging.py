"""Bounded QQ database reads, ordered keyset cursors and streaming exports.

Primary rows are authoritative; FTS-only rows remain visible with provenance.
Each source is fetched in small SQL pages. Keyword matching happens after
decoding (to include payload-only text), so an empty SQL batch is never an EOF.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import tempfile
import time
from collections import Counter, deque
from pathlib import Path

from .qq_cache_lock import cache_lock

CHUNK = 256
KEY_SQL = "(COALESCE([40050],0), COALESCE([40003],0), COALESCE([40001],0))"


def row_key(row):
    return tuple(int(row.get(k) or 0) for k in ("create_time", "seq", "msg_id"))


def order_for(args):
    value = str(args.get("order") or "desc").lower()
    return value if value in {"asc", "desc"} else "desc"


def fingerprint(chat, kind, args, db_root):
    scope = {k: args.get(k) for k in ("after", "before", "keyword", "sender", "kind_name")}
    scope.update(chat=chat, kind=kind, order=order_for(args), database=str(db_root))
    return hashlib.sha256(json.dumps(scope, sort_keys=True, default=str).encode()).hexdigest()


def decode_cursor(raw, scope):
    if not raw:
        return None
    if not isinstance(raw, str) or len(raw) > 4096:
        raise ValueError("Invalid QQ cursor")
    try:
        data = json.loads(base64.urlsafe_b64decode(raw))
        key = data["key"]
        if (data["v"] != 1 or data["scope"] != scope or not isinstance(key, list) or len(key) != 3
                or any(type(v) is not int for v in key) or type(data["consumed"]) is not int
                or data["consumed"] < 0 or type(data["fts"]) is not bool):
            raise ValueError("mismatched cursor")
        return data
    except (ValueError, KeyError, TypeError) as exc:
        raise ValueError("Invalid QQ cursor or cursor belongs to another query") from exc


def encode_cursor(scope, key, consumed, fts):
    return base64.urlsafe_b64encode(json.dumps({"v": 1, "scope": scope, "key": key,
                                              "consumed": consumed, "fts": fts},
                                             separators=(",", ":")).encode()).decode()


class Reader:
    """One call owns its connections; never shares SQLite handles across threads."""

    def __init__(self, adapter, args):
        self.adapter, self.legacy, self.args = adapter, adapter.legacy, args
        self.main = self.fts = None
        self.warnings = []
        self.metrics = {"sql_pages": 0, "max_rows_in_sql_page": 0, "decoded_records": 0}
        query = self.legacy.chat_query_from_args(args)
        if not query:
            raise ValueError("QQ query requires contact/chat/group.")
        self.chat, self.kind = self.legacy.resolve_chat_entity(query, self.legacy.chat_type_from_args(args))
        self.chat = {**self.chat, "chat_type": self.kind}
        self.ident = self.legacy.chat_identity_value(self.chat, self.kind)
        self.scope = fingerprint(self.ident, self.kind, args, self.legacy.DEFAULT_DB_ROOT)
        self.cursor = decode_cursor(args.get("cursor"), self.scope)
        if self.cursor and args.get("offset") not in (None, 0, "0"):
            raise ValueError("Use cursor or nonzero offset, not both")
        self.main_table = {"private": "c2c", "group": "group", "discuss": "discuss"}[self.kind] + "_msg_table"
        self.fts_db = {"private": "buddy_msg_fts.db", "group": "group_msg_fts.db", "discuss": "discuss_msg_fts.db"}[self.kind]
        self.fts_table = self.fts_db[:-3]
        self.member_map = self.legacy.load_group_member_map(self.chat.get("group_id")) if self.kind == "group" else {}
        try:
            self.main = adapter.open_db(self.legacy.DEFAULT_DB_ROOT, "nt_msg.db", adapter.get_key())
            self._execute(self.main, "PRAGMA temp_store = FILE", [])
            self._execute(self.main, "PRAGMA cache_size = -2048", [])
            # A failed primary table must never look like an empty FTS-only chat.
            self._execute(self.main, f"SELECT [40001] FROM {self.main_table} LIMIT 0", [])
            try:
                self.fts = adapter.open_db(self.legacy.DEFAULT_DB_ROOT, self.fts_db, adapter.get_key())
                self._execute(self.fts, "PRAGMA temp_store = FILE", [])
                self._execute(self.fts, "PRAGMA cache_size = -2048", [])
                self._execute(self.fts, f"SELECT [40001] FROM {self.fts_table} LIMIT 0", [])
            except Exception as exc:
                if self.fts is not None:
                    self.fts.close()
                self.fts = None
                self.warnings.append(f"QQ FTS unavailable; main payloads only: {exc}")
            if self.cursor and self.cursor["fts"] != (self.fts is not None):
                raise RuntimeError("QQ FTS availability changed while paging; restart to avoid missing records")
        except Exception:
            self.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def close(self):
        for con in (self.main, self.fts):
            if con is not None:
                con.close()
        self.main = self.fts = None

    def _execute(self, con, sql, params):
        # Reset the query budget per bounded operation, not once per long export.
        if hasattr(con, "set_progress_handler"):
            deadline = time.monotonic() + 45
            con.set_progress_handler(lambda: int(time.monotonic() > deadline), 10000)
        return con.execute(sql, params)

    def where(self, *, times=True):
        if self.kind == "private":
            sql, params = "([40021] = ? OR [40020] = ?)", [self.ident, self.ident]
        elif str(self.ident).isdigit():
            sql, params = "[40027] = ?", [int(self.ident)]
        else:
            sql, params = "[40021] = ?", [str(self.ident)]
        if times:
            for field, op, end in (("after", ">=", False), ("before", "<", True)):
                value = self.legacy.parse_time_bound(self.args.get(field), before=end)
                if value is not None:
                    sql += f" AND [40050] {op} ?"
                    params.append(value)
        return sql, params

    def fields(self, fts):
        common = "[40001] AS msg_id, [40050] AS create_time, [40020] AS sender_uid, [40021] AS peer_uid, [40003] AS seq"
        if self.kind != "private":
            common += ", [40027] AS peer_num"
        if fts:
            return common + ", [41701] AS text, [41702] AS aux_text, [41703] AS msg_code, [41704] AS type_code"
        return common + ", [40011] AS msg_code, [40012] AS type_code, [40013] AS status_code, [40800] AS payload, [40900] AS ext_payload, [40033] AS sender_uin, [40030] AS peer_uin"

    def batches(self, fts, start_key=None):
        con, table = (self.fts, self.fts_table) if fts else (self.main, self.main_table)
        if con is None:
            return
        order = order_for(self.args)
        last = start_key
        while True:
            where, params = self.where()
            if last is not None:
                where += f" AND {KEY_SQL} {'>' if order == 'asc' else '<'} (?, ?, ?)"
                params += list(last)
            # SQL limits memory even for a very broad time range. Temp sorting is
            # SQLite's responsibility; the account database is never re-indexed.
            ordering = ", ".join(f"COALESCE([{col}],0) {order.upper()}" for col in ("40050", "40003", "40001"))
            cur = self._execute(con, f"SELECT {self.fields(fts)} FROM {table} WHERE {where} ORDER BY {ordering} LIMIT ?", params + [CHUNK])
            rows = [self.legacy.row_dict(cur, row) for row in cur]
            self.metrics["sql_pages"] += 1
            self.metrics["max_rows_in_sql_page"] = max(self.metrics["max_rows_in_sql_page"], len(rows))
            if not rows:
                return
            last = row_key(rows[-1])
            yield rows
            if len(rows) < CHUNK:
                return

    def matching_ids(self, ids, *, fts=False):
        con, table = (self.fts, self.fts_table) if fts else (self.main, self.main_table)
        if con is None or not ids:
            return {}
        where, params = self.where(times=False)
        cur = self._execute(con, f"SELECT {self.fields(fts)} FROM {table} WHERE {where} AND [40001] IN ({','.join('?' for _ in ids)}) LIMIT ?", params + list(ids) + [len(ids)])
        rows = [self.legacy.row_dict(cur, row) for row in cur]
        return {int(row["msg_id"]): row for row in rows}

    def source_records(self, fts, start_key):
        for batch in self.batches(fts, start_key):
            ids = [int(row["msg_id"]) for row in batch]
            if fts:
                main = self.matching_ids(ids)
                orphan = {int(row["msg_id"]): row for row in batch if int(row["msg_id"]) not in main}
                records = self.legacy.build_records([], orphan, self.chat, self.kind, self.warnings, member_map=self.member_map)
                # Keep tiny skipped heads in sort order. Filtering all duplicate
                # FTS rows before yielding would scan the entire FTS source just
                # to find the first orphan on every otherwise small page.
                records.extend({"msg_id": str(row["msg_id"]), "timestamp": row["create_time"], "seq": row["seq"],
                                "_skip_authoritative": True} for row in batch if int(row["msg_id"]) in main)
                records.sort(key=self.legacy.record_sort_key)
            else:
                extras = self.matching_ids(ids, fts=True)
                records = self.legacy.build_records(batch, extras, self.chat, self.kind, self.warnings, member_map=self.member_map)
            self.metrics["decoded_records"] += len(records)
            if order_for(self.args) == "desc":
                records.reverse()
            yield from records

    def records(self):
        start = self.cursor["key"] if self.cursor else None
        streams = [iter(self.source_records(False, start)), iter(self.source_records(True, start))]
        heads = [next(stream, None) for stream in streams]
        asc = order_for(self.args) == "asc"
        while any(head is not None for head in heads):
            candidates = [i for i, head in enumerate(heads) if head is not None]
            pick = min(candidates, key=lambda i: self.legacy.record_sort_key(heads[i])) if asc else max(candidates, key=lambda i: self.legacy.record_sort_key(heads[i]))
            record = heads[pick]
            # Advance only on the next iteration; stopping a page does not decode
            # additional batches or drop this page's first unconsumed row.
            if not record.get("_skip_authoritative") and self.legacy.filter_records([record], self.args):
                yield record
            heads[pick] = next(streams[pick], None)

    def page(self, limit=50, max_limit=1000):
        size = self.legacy.bounded_int(self.args.get("limit"), limit, 1, max_limit)
        offset = self.legacy.bounded_int(self.args.get("offset"), 0, 0, 1_000_000_000)
        prior = self.cursor["consumed"] if self.cursor else 0
        skipped, window = 0, []
        for record in self.records():
            if skipped < offset:
                skipped += 1
                continue
            window.append(record)
            if len(window) > size:
                break
        more = len(window) > size
        window = window[:size]
        consumed = prior + skipped + len(window)
        last = self.legacy.record_sort_key(window[-1]) if window else None
        display = str(self.args.get("display_order") or self.args.get("displayOrder") or "query").lower()
        if display not in {"query", "asc", "desc"}:
            display = "query"
        if display != "query":
            window.sort(key=self.legacy.record_sort_key, reverse=display == "desc")
        return window, {"returned": len(window), "limit": size, "offset": prior + offset,
                        "has_more": more, "next_offset": consumed if more else None,
                        "next_cursor": encode_cursor(self.scope, last, consumed, self.fts is not None) if more else None,
                        "total": consumed if not more else None, "total_exact": not more,
                        "order": order_for(self.args), "display_order": display,
                        "pagination": "keyset", "read_metrics": dict(self.metrics),
                        "coverage": "Live local databases, not an immutable snapshot; restart after history migration."}


def messages(adapter, args):
    with Reader(adapter, args) as reader:
        rows, query = reader.page()
        return {"chat": reader.chat, "contact": reader.chat, "warnings": reader.warnings,
                "query": {"contact": adapter.legacy.chat_query_from_args(args), "chat_type": reader.kind,
                          "after": args.get("after"), "before": args.get("before"), "keyword": args.get("keyword"), **query},
                "messages": adapter.legacy.strip_media_if_needed(rows, args.get("include_media", True))}


def cache_recent(adapter, args):
    with Reader(adapter, args) as reader:
        rows, query = reader.page(limit=500, max_limit=10000)
        result = adapter.legacy.merge_cached_records(reader.chat, rows)
        return {"contact": reader.chat, "chat": reader.chat, "warnings": reader.warnings,
                "query": query, "cache": result,
                "messages": adapter.legacy.strip_media_if_needed(rows, args.get("include_media", False))}


class Summary:
    def __init__(self):
        self.months, self.senders, self.kinds = Counter(), Counter(), Counter()
        self.total = self.text = 0
        self.first = self.last = None

    def add(self, row):
        self.total += 1
        self.text += bool(row.get("text"))
        self.senders[str(row.get("sender") or "")] += 1
        self.kinds[str(row.get("kind") or "")] += 1
        if row.get("time"):
            self.months[str(row["time"])[:7]] += 1
        key = (int(row.get("timestamp") or 0), row.get("time"))
        if self.first is None or key[0] < self.first[0]:
            self.first = key
        if self.last is None or key[0] > self.last[0]:
            self.last = key

    def value(self, chat):
        return {"contact": chat, "total_messages": self.total, "text_indexed_messages": self.text,
                "media_or_unindexed_messages": self.total - self.text,
                "first_time": self.first[1] if self.first else None,
                "last_time": self.last[1] if self.last else None,
                "by_sender": dict(self.senders), "by_kind": dict(self.kinds), "by_month": dict(sorted(self.months.items()))}


def stats(adapter, args):
    summary = Summary()
    with Reader(adapter, args) as reader:
        for row in reader.records():
            summary.add(row)
        return {**summary.value(reader.chat), "warnings": reader.warnings, "read_metrics": reader.metrics}


def export_messages(adapter, args):
    target = str(args.get("path") or "").strip()
    if not target:
        raise ValueError("export_messages requires path.")
    fmt = str(args.get("format") or "jsonl").lower()
    if fmt not in {"jsonl", "markdown"}:
        raise ValueError("format must be jsonl or markdown.")
    path = Path(target).expanduser()
    overwrite = args.get("overwrite") is True
    path.parent.mkdir(parents=True, exist_ok=True)
    with cache_lock(path.with_name(path.name + ".export.lock")):
        if path.exists() and not overwrite:
            raise FileExistsError("Export target already exists; choose another path or explicitly set overwrite=true.")
        fd, temp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
        summary = Summary()
        limit = adapter.legacy.bounded_int(args.get("limit"), 1_000_000, 1, 1_000_000) if args.get("limit") not in (None, "") else None
        truncated = False
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream, Reader(adapter, {"order": "asc", **args}) as reader:
                if fmt == "markdown":
                    stream.write(f"# QQ 聊天导出: {adapter.legacy.chat_display_name(reader.chat, reader.kind)}\n\n")
                    stream.write(f"- 聊天类型: `{reader.kind}`\n- 聊天 ID: `{reader.ident}`\n\n")
                day = None
                for row in reader.records():
                    if limit is not None and summary.total >= limit:
                        truncated = True
                        break
                    summary.add(row)
                    if fmt == "jsonl":
                        stream.write(json.dumps(row, ensure_ascii=False) + "\n")
                    else:
                        next_day = str(row.get("time") or "")[:10]
                        if next_day != day:
                            day = next_day
                            stream.write(f"\n## {day}\n\n")
                        body = row.get("text") or f"[{row.get('kind')}]"
                        stream.write(f"- {str(row.get('time') or '')[11:19]} {row.get('sender')}: {body}\n")
                        if row.get("media"):
                            stream.write("  - 媒体线索（不等同文件可用）: " + json.dumps(row["media"], ensure_ascii=False) + "\n")
                if fmt == "markdown":
                    stream.write(f"\n---\n导出消息数: {summary.total}；达到上限而截断: {truncated}\n")
                stream.flush()
                os.fsync(stream.fileno())
                result = {"path": str(path), "format": fmt, "messages": summary.total,
                          "truncated": truncated, "complete": not truncated, "warnings": reader.warnings,
                          "read_metrics": reader.metrics, **summary.value(reader.chat)}
            if overwrite:
                os.replace(temp, path)
            else:
                # Atomic no-clobber publication; same-directory temp guarantees
                # same filesystem. An outside writer cannot be overwritten.
                os.link(temp, path)
            return result
        finally:
            Path(temp).unlink(missing_ok=True)


def recall_events(adapter, args):
    legacy = adapter.legacy
    limit = legacy.bounded_int(args.get("limit"), 20, 1, 200)
    size = legacy.bounded_int(args.get("context_window"), 3, 0, 20)
    cache_window = legacy.bounded_int(args.get("cache_window_seconds"), 3600, 0, 86400)
    include_media = args.get("include_media", False)
    events, active = [], []
    previous = deque(maxlen=size)
    with Reader(adapter, args) as reader:
        for row in reader.records():
            for item in active:
                item["following"].append(row)
            active = [item for item in active if len(item["following"]) < size]
            if len(events) < limit and legacy.is_recall_record(row):
                item = {"event": row, "preceding": list(previous), "following": []}
                events.append(item)
                if size:
                    active.append(item)
            previous.append(row)
            if len(events) >= limit and not active:
                break
        # Keep only cached IDs that actually occur in either current source.
        # This avoids holding every live message just to test membership.
        cached = legacy.load_cached_entries(reader.chat)
        cache_ids = [int(entry["record"]["msg_id"]) for entry in cached if str(entry.get("record", {}).get("msg_id", "")).lstrip("-").isdigit()]
        live = set()
        for start in range(0, len(cache_ids), CHUNK):
            ids = cache_ids[start:start + CHUNK]
            live.update(str(i) for i in reader.matching_ids(ids))
            live.update(str(i) for i in reader.matching_ids(ids, fts=True))
        for item in events:
            before, after = item.pop("preceding"), item.pop("following")
            if order_for(args) == "desc":
                before, after = list(reversed(after)), list(reversed(before))
            item["context"] = {"before": legacy.strip_media_if_needed(before, include_media), "after": legacy.strip_media_if_needed(after, include_media)}
            item["cache_lookup"] = legacy.find_cached_recall_candidates(reader.chat, item["event"], live, window_seconds=cache_window, limit=5)
            item["event"] = legacy.strip_media_if_needed([item["event"]], include_media)[0]
        return {"contact": reader.chat, "chat": reader.chat, "warnings": reader.warnings,
                "query": {"chat_type": reader.kind, "limit": limit, "order": order_for(args), "context_window": size,
                          "after": args.get("after"), "before": args.get("before"), "cache_window_seconds": cache_window},
                "returned": len(events), "events": events, "read_metrics": reader.metrics,
                "limitation": "Only locally present or previously cached originals can be recovered; candidates are not proof."}
