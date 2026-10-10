"""Lossless source-tagged merging with independently advanced source cursors."""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json

from unified_mcp.read_contract import canonical_chat, page_source_complete, page_warnings, validate_record_chat, validated_page_projection

TZ = dt.timezone(dt.timedelta(hours=8))


def decoded_result(result):
    if isinstance(result, dict):
        data = result
    else:
        if result.isError:
            raise RuntimeError("Backend reported a tool failure; requested records were not verified")
        blocks = [b.text for b in result.content if b.type == "text"]
        data = json.loads(blocks[0]) if len(blocks) == 1 else None
    if not isinstance(data, dict):
        raise RuntimeError("Backend returned an unsupported result shape")
    if data.get("error") or data.get("errors") or data.get("status") in {"error", "failed", "partial"}:
        raise RuntimeError("Backend reported an incomplete or failed read")
    return data


def normal_message(source, row, chat):
    if source == "wechat":
        native_id = row.get("id") or {}
        message_id = str(native_id.get("server_id_str") or native_id.get("local_id") or "")
        sender_id = row.get("sender_wxid")
        from_me = row.get("is_from_me")
        direction = "outgoing" if from_me is True else "incoming" if from_me is False else "unknown"
        if not sender_id and row.get("kind") == "system":
            direction = "system"
        timestamp = row.get("create_time")
        # A text message and a related system record can share a server ID.
        # Preserve the native message_id, but expose a separate local record ID.
        record_id = f"{chat}:{message_id}:{native_id.get('local_id', '')}:{timestamp}:{row.get('kind', '')}"
    else:
        message_id = str(row.get("msg_id") or "")
        sender_id = row.get("sender_uid") or row.get("sender_uin")
        direction = {"from_me": "outgoing", "from_contact": "incoming", "from_member": "incoming"}.get(row.get("direction"), "unknown")
        timestamp = row.get("timestamp")
        record_id = f"{chat}:{message_id}"
    identity_conflict = any("sender_identity_conflict" in str(warning) for warning in row.get("warnings", []))
    if identity_conflict:
        sender_id, direction = None, "unknown"
    return {
        "source": source, "chat_id": chat, "message_id": message_id, "record_id": record_id,
        "time": row.get("time_iso") or row.get("time"), "timestamp": timestamp,
        "sender_id": sender_id, "sender_name": "unknown" if identity_conflict else row.get("sender"), "direction": direction,
        "identity_status": "conflict" if identity_conflict else row.get("identity_status", "unknown" if not sender_id else "reported"),
        "kind": row.get("kind"), "text": row.get("text", ""),
        "original": row,
    }


def fingerprint(args):
    scope = {k: args.get(k) for k in ("wechat_chat", "qq_chat", "qq_chat_type", "date", "after", "before", "keyword", "sender", "wechat_sender", "qq_sender", "kind_name", "order", "include_media", "include_image_text")}
    return hashlib.sha256(json.dumps(scope, sort_keys=True).encode()).hexdigest()


def read_cursor(cursor, args):
    if not cursor:
        return {"v": 1, "scope": fingerprint(args), "offsets": {"wechat": 0, "qq": 0},
                "snapshot_before": dt.datetime.now(TZ).isoformat(timespec="seconds")}
    if len(cursor) > 8192:
        raise ValueError("Cursor too large")
    try:
        data = json.loads(base64.urlsafe_b64decode(cursor))
        if data["v"] != 1 or data["scope"] != fingerprint(args):
            raise ValueError("Cursor belongs to a different query")
        if any(type(v) is not int or v < 0 for v in data["offsets"].values()):
            raise ValueError("Invalid cursor offset")
        dt.datetime.fromisoformat(data["snapshot_before"])
        return data
    except (ValueError, KeyError, TypeError) as exc:
        raise ValueError("Invalid or mismatched cursor") from exc


def write_cursor(data):
    return base64.urlsafe_b64encode(json.dumps(data, separators=(",", ":")).encode()).decode()


def end_bound(value, snapshot):
    end = dt.datetime.fromisoformat(snapshot)
    if not value:
        return str(int(end.timestamp()))
    if value.isdigit():
        n = int(value)
        other = dt.datetime.fromtimestamp(n / 1000 if len(value) == 13 else n, TZ)
    else:
        other = dt.datetime.fromisoformat(value)
        if len(value) == 10:
            other += dt.timedelta(days=1)
        if other.tzinfo is None:
            other = other.replace(tzinfo=TZ)
    return str(int(min(other, end).timestamp()))


def start_bound(value):
    if not value:
        return None
    if value.isdigit():
        return str(int(value) // 1000 if len(value) == 13 else int(value))
    parsed = dt.datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=TZ)
    return str(int(parsed.timestamp()))


async def merged_timeline(args, fetch):
    args = date_scope(args)
    chats = {source: args.get(source + "_chat") for source in ("wechat", "qq") if args.get(source + "_chat")}
    if not chats:
        raise ValueError("Specify wechat_chat and/or qq_chat using resolved stable IDs")
    limit = args.get("limit", 100)
    if type(limit) is not int or not 1 <= limit <= 500:
        raise ValueError("limit must be between 1 and 500; use next_cursor for the rest")
    order = args.get("order", "desc")
    if order not in {"asc", "desc"}:
        raise ValueError("order must be asc or desc")
    if args.get("sender") and len(chats) > 1:
        raise ValueError("For two platforms use wechat_sender and qq_sender separately; sender identities are platform-specific")
    if args.get("sender") and any(args.get(source + "_sender") for source in chats):
        raise ValueError("Use sender or a platform-specific sender, not both")
    state = read_cursor(args.get("cursor"), args)
    pages, errors, pool = {}, {}, []
    for source, chat in chats.items():
        params = {"chat": chat, "contact": chat, "after": start_bound(args.get("after")),
                  "before": end_bound(args.get("before"), state["snapshot_before"]),
                  "keyword": args.get("keyword"), "sender": args.get(source + "_sender") or args.get("sender"),
                  "kind_name": args.get("kind_name"), "limit": limit + 1,
                  "offset": state["offsets"].get(source, 0), "order": order,
                  "display_order": order}
        if source == "qq":
            params["chat_type"] = args.get("qq_chat_type", "private")
            params["include_media"] = args.get("include_media", True)
        else:
            params.pop("contact")
            if chat.startswith("wxid_") or chat.endswith("@chatroom"):
                params["talker"] = params.pop("chat")
            params["include_media_paths"] = args.get("include_media", True)
            params["include_images"] = args.get("include_media", True)
        params = {k: v for k, v in params.items() if v is not None}
        params["include_image_text"] = args.get("include_image_text", True)
        try:
            page = decoded_result(await fetch(source, "chat_timeline", params))
            if not isinstance(page.get("messages"), list):
                raise RuntimeError("Backend omitted messages")
            if not page["messages"] and page.get("query", {}).get("has_more"):
                raise RuntimeError("Backend returned an empty nonterminal page; pagination cannot advance safely")
            if any(m.get("error") for m in page["messages"]):
                raise RuntimeError("Some records failed to read; inspect the native source tool")
            if type(page.get("query", {}).get("has_more")) is not bool:
                raise RuntimeError("Backend omitted an explicit pagination endpoint")
            if len(page["messages"]) > params["limit"]:
                raise RuntimeError("Backend ignored requested page size")
            canonical = canonical_chat(source, page, chat, state.get("canonical_chats", {}).get(source), chat_type=args.get("qq_chat_type", "private") if source == "qq" else None)
            source_pool = []
            previous_time = None
            for index, row in enumerate(page["messages"]):
                validate_record_chat(source, row, canonical, chat_type=args.get("qq_chat_type", "private") if source == "qq" else None)
                msg = normal_message(source, row, canonical)
                if msg["timestamp"] is None:
                    raise RuntimeError("Missing timestamp; cannot merge reliably")
                timestamp = int(msg["timestamp"])
                if ((params.get("after") and timestamp < int(params["after"])) or timestamp >= int(params["before"])):
                    raise RuntimeError("Backend returned a record outside the requested time range")
                if previous_time is not None and ((order == "asc" and timestamp < previous_time) or (order == "desc" and timestamp > previous_time)):
                    raise RuntimeError("Backend did not respect requested time order")
                previous_time = timestamp
                # Index preserves the backend's ordering for same-second messages.
                source_pool.append((source, index, msg))
            pages[source] = page
            state.setdefault("canonical_chats", {})[source] = canonical
            if not page_source_complete(page):
                raise RuntimeError("Source is degraded; pagination alone cannot prove complete history")
            pool.extend(source_pool)
        except Exception as exc:
            errors[source] = str(exc)
    if errors:
        return {"status": "partial", "errors": errors, "messages": [],
                "available_source_pages": {source: validated_page_projection(page) for source, page in pages.items()}, "next_cursor": None,
                "note": "No cursor advanced. Fix the failed source or explicitly query the available source alone."}
    pool.sort(key=lambda item: ((-1 if order == "desc" else 1) * int(item[2]["timestamp"]), item[0], item[1]))
    chosen = pool[:limit]
    consumed = {source: sum(1 for s, _, _ in chosen if s == source) for source in chats}
    more = False
    metadata = {}
    warnings = []
    for source, page in pages.items():
        state["offsets"][source] += consumed[source]
        more |= consumed[source] < len(page["messages"]) or bool(page.get("query", {}).get("has_more"))
        metadata[source] = {"query": page.get("query"), "freshness": page.get("freshness"), "consumed": consumed[source]}
        warnings += [{"source": source, "warning": warning} for warning in page_warnings(page)]
    return {"status": "ok", "messages": [m for _, _, m in chosen], "returned": len(chosen),
            "has_more": more, "next_cursor": write_cursor(state) if more else None,
            "snapshot_before": state["snapshot_before"], "sources": metadata, "warnings": warnings,
            "coverage": "Local synchronized records only. This is a time-bounded live read, not an immutable DB snapshot; restart pagination after history migration."}


def date_scope(args):
    """Normalize a local calendar date without silently overriding a range."""
    args = dict(args)
    if args.get("date"):
        if args.get("after") or args.get("before"):
            raise ValueError("date cannot be combined with after/before")
        value = args.pop("date")
        day = dt.date.fromisoformat(value)
        if len(value) != 10:
            raise ValueError("date must be YYYY-MM-DD")
        args["after"] = day.isoformat()
        args["before"] = day.isoformat()
    return args
