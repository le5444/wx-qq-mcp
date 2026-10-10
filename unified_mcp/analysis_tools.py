"""Bounded, source-scoped exact lookup, context and factual group aggregates."""
from __future__ import annotations

import datetime as dt
from collections import Counter

from unified_mcp.timeline import TZ, date_scope, start_bound, end_bound, decoded_result, normal_message


def bounded(value, default, maximum, name):
    value = default if value is None else value
    if type(value) is not int or not 1 <= value <= maximum:
        raise ValueError(f"{name} must be an integer between 1 and {maximum}")
    return value


def scope(args, *, group=False):
    args = date_scope(args)
    source, chat = args.get("source"), args.get("chat_id")
    if source not in {"wechat", "qq"} or not isinstance(chat, str) or not chat.strip():
        raise ValueError("source must be wechat/qq and chat_id must be a resolved stable ID")
    chat_type = args.get("chat_type", "group" if group else "private")
    if chat_type not in {"private", "group", "discuss"}:
        raise ValueError("Invalid chat_type")
    if group and ((source == "wechat" and not chat.endswith("@chatroom")) or
                  (source == "qq" and chat_type not in {"group", "discuss"})):
        raise ValueError("Group statistics require a group identity")
    snapshot = dt.datetime.now(TZ).isoformat(timespec="seconds")
    return {**args, "source": source, "chat_id": chat, "chat_type": chat_type,
            "after": start_bound(args.get("after")), "before": end_bound(args.get("before"), snapshot)}


class Scan:
    """A bounded iterator. Empty nonterminal pages are errors, never EOF."""
    def __init__(self, args, fetch, maximum, *, order="asc", media=False):
        self.args, self.fetch, self.maximum = args, fetch, maximum
        self.order, self.media = order, media
        self.scanned, self.complete, self.error, self.limited = 0, False, None, False

    @property
    def coverage(self):
        return {"complete": self.complete, "scanned": self.scanned, "limited": self.limited,
                "error": self.error, "scope": "locally synchronized records in requested range"}

    async def rows(self):
        a, offset, cursor = self.args, 0, None
        previous_time = None
        if a.get("after") and int(a["after"]) >= int(a["before"]):
            self.complete = True
            return
        while self.scanned < self.maximum:
            source, chat = a["source"], a["chat_id"]
            params = {"after": a.get("after"), "before": a.get("before"),
                      "order": self.order, "display_order": self.order,
                      "limit": min(256, self.maximum - self.scanned),
                      "include_image_text": a.get("include_image_text", True) if self.media else False}
            if source == "wechat":
                params.update(talker=chat, include_media_paths=self.media, include_images=self.media, offset=offset)
            else:
                params.update(contact=chat, chat_type=a["chat_type"], include_media=self.media)
                params.update({"cursor": cursor} if cursor else {"offset": offset})
            params = {k: v for k, v in params.items() if v is not None}
            try:
                page = decoded_result(await self.fetch(source, "chat_timeline", params))
                rows = page.get("messages")
                if not isinstance(rows, list) or page.get("errors") or page.get("status") == "partial":
                    raise RuntimeError("Incomplete or invalid source page")
                more = page.get("query", {}).get("has_more", page.get("has_more"))
                if type(more) is not bool:
                    raise RuntimeError("Source omitted an explicit pagination endpoint")
                if not rows and more:
                    raise RuntimeError("Empty nonterminal source page cannot advance")
                if len(rows) > params["limit"]:
                    raise RuntimeError("Source ignored the requested page limit")
                for row in rows:
                    if row.get("error"):
                        raise RuntimeError("Source record could not be read")
                    native_chat = row.get("talker") or (row.get("id") or {}).get("talker") if source == "wechat" else row.get("chat_id")
                    if native_chat and str(native_chat) != chat:
                        raise RuntimeError("Source record belongs to a different chat")
                    item = normal_message(source, row, chat)
                    timestamp = item.get("timestamp")
                    if timestamp is None or (a.get("after") and int(timestamp) < int(a["after"])) or int(timestamp) >= int(a["before"]):
                        raise RuntimeError("Source record escaped requested time scope")
                    if previous_time is not None and ((self.order == "asc" and timestamp < previous_time) or
                                                      (self.order == "desc" and timestamp > previous_time)):
                        raise RuntimeError("Source records are not in requested time order")
                    previous_time = timestamp
                    self.scanned += 1
                    yield item
                if not more:
                    self.complete = True
                    return
                next_cursor = page.get("query", {}).get("next_cursor")
                if next_cursor and next_cursor == cursor:
                    raise RuntimeError("Source cursor did not advance")
                cursor = next_cursor
                offset += len(rows)
            except Exception as exc:
                self.error = str(exc)
                return
        self.limited = True


def identity_scope(args):
    result = scope(args)
    record_id, message_id = args.get("record_id"), args.get("message_id")
    if bool(record_id) == bool(message_id):
        raise ValueError("Specify exactly one of record_id or message_id")
    if record_id:
        prefix = result["chat_id"] + ":"
        if not record_id.startswith(prefix):
            raise ValueError("record_id belongs to a different chat")
        if result["source"] == "wechat":
            # Only intersect the user's scope; a foreign date must stay empty.
            parts = record_id[len(prefix):].rsplit(":", 3)
            if len(parts) != 4:
                raise ValueError("Invalid WeChat record_id")
            timestamp = int(parts[-2])
            result["after"] = str(max(int(result.get("after") or timestamp), timestamp))
            result["before"] = str(min(int(result["before"]), timestamp + 1))
    return result


async def message(args, fetch):
    a = identity_scope(args)
    scan = Scan(a, fetch, bounded(args.get("max_scan"), 20000, 1000000, "max_scan"))
    matches = []
    async for row in scan.rows():
        if (row["record_id"] == args.get("record_id") or
                (args.get("message_id") is not None and row["message_id"] == str(args["message_id"]))):
            matches.append(row)
    result = {"status": "partial", "coverage": scan.coverage}
    if not scan.complete:
        if matches:
            result["candidates"] = matches
        return result
    if not matches:
        return {**result, "status": "not_found"}
    if len(matches) > 1:
        return {**result, "status": "ambiguous", "candidates": matches}
    selected = matches[0]
    if args.get("include_media", True):
        rich_scope = {**a, "after": str(selected["timestamp"]), "before": str(selected["timestamp"] + 1)}
        rich = Scan(rich_scope, fetch, 20000, media=True)
        async for candidate in rich.rows():
            if candidate["record_id"] == selected["record_id"]:
                selected = candidate
                break
        if rich.error:
            result["warnings"] = ["media lookup failed; original record preserved: " + rich.error]
    return {**result, "status": "ok", "message": selected}


async def context(args, fetch):
    a = scope(args)
    counts = {}
    for key in ("before_count", "after_count"):
        value = args.get(key, 10)
        if type(value) is not int or not 0 <= value <= 50:
            raise ValueError(f"{key} must be between 0 and 50")
        counts[key] = value
    located = await message({**args, "include_media": False}, fetch)
    if located["status"] != "ok":
        return located
    target = located["message"]
    neighbors, coverage = {}, {}
    maximum = bounded(args.get("max_scan"), 20000, 1000000, "max_scan")
    for side, order, bound in (("before", "desc", "before"), ("after", "asc", "after")):
        selected, seen = [], False
        requested = counts[side + "_count"]
        window = dict(a)
        window[bound] = str(target["timestamp"] + (1 if side == "before" else 0))
        scan = Scan(window, fetch, maximum, order=order, media=args.get("include_media", True))
        if requested:
            async for row in scan.rows():
                if row["record_id"] == target["record_id"]:
                    seen = True
                    target = row
                elif seen:
                    selected.append(row)
                    if len(selected) == requested:
                        break
        done = not requested or len(selected) == requested or (seen and scan.complete)
        coverage[side] = {**scan.coverage, "complete": done}
        neighbors[side] = list(reversed(selected)) if side == "before" else selected
    complete = all(item["complete"] for item in coverage.values())
    return {"status": "ok" if complete else "partial", "target": target, **neighbors,
            "coverage": {"complete": complete, "sides": coverage,
                         "scanned": located["coverage"]["scanned"] + sum(c["scanned"] for c in coverage.values())}}


async def group_stats(args, fetch):
    a = scope(args, group=True)
    scan = Scan(a, fetch, bounded(args.get("max_messages"), 20000, 1000000, "max_messages"))
    senders, kinds, days, names = Counter(), Counter(), Counter(), {}
    async for row in scan.rows():
        sender = row.get("sender_id") or None
        senders[sender] += 1
        if sender and row.get("sender_name"):
            names[sender] = row["sender_name"]
        kinds[row.get("kind") or "unknown"] += 1
        days[dt.datetime.fromtimestamp(row["timestamp"], TZ).date().isoformat()] += 1
    sender_rows = [{"sender_id": sender, "sender_name": names.get(sender), "count": count}
                   for sender, count in senders.most_common()]
    summary = {"total_messages": scan.scanned, "unknown_sender_count": senders.get(None, 0),
               "system_messages": kinds.get("system", 0), "senders": sender_rows,
               "by_kind": dict(kinds), "by_day": dict(sorted(days.items()))}
    return {"status": "ok" if scan.complete else "partial", "source": a["source"], "chat_id": a["chat_id"],
            "summary": summary, "coverage": scan.coverage,
            "note": "Counts include separately labeled system records; display names are not identities. Topic or relationship analysis requires reading supporting messages."}


def tool_definitions():
    from mcp.types import Tool
    common = {"source": {"type": "string", "enum": ["wechat", "qq"]}, "chat_id": {"type": "string"},
              "chat_type": {"type": "string", "enum": ["private", "group", "discuss"]},
              "date": {"type": "string", "description": "Local +08:00 calendar date YYYY-MM-DD; conflicts with after/before"},
              "after": {"type": "string"}, "before": {"type": "string"}}
    exact = {**common, "record_id": {"type": "string"}, "message_id": {"type": "string"},
             "max_scan": {"type": "integer", "minimum": 1, "maximum": 1000000, "default": 20000},
             "include_media": {"type": "boolean", "default": True}, "include_image_text": {"type": "boolean", "default": True}}
    specs = [("unified_message", exact, "Find one record by record_id or native message_id in one resolved chat. Ambiguous IDs return candidates. Partial scans never prove absence."),
             ("unified_context", {**exact, **{k: {"type": "integer", "minimum": 0, "maximum": 50, "default": 10} for k in ("before_count", "after_count")}}, "Read before/after context around one exact record, preserving same-second neighbors and the requested time scope."),
             ("unified_group_stats", {**common, "max_messages": {"type": "integer", "minimum": 1, "maximum": 1000000, "default": 20000}}, "Count group messages by stable sender identity, type and day. Labels incomplete scans; does not infer sentiment or relationships.")]
    return [Tool(name=name, description=description, inputSchema={"type": "object", "properties": properties,
                "required": ["source", "chat_id"], "additionalProperties": False}) for name, properties, description in specs]
